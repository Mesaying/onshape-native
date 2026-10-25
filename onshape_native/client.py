"""Browser-session transport. No Onshape keys, cookies, or HMAC in this package."""
from dataclasses import dataclass, field
from pathlib import Path
import asyncio
import base64
import hashlib
import json
import os
import re
import time

import httpx

from .config import load_config, runtime_dir


class OnshapeError(Exception):
    def __init__(self, message="", status=None):
        super().__init__(message)
        self.status = status


@dataclass
class Settings:
    base_url: str = "https://cad.onshape.com"
    cache_dir: Path = field(default_factory=lambda: runtime_dir() / "artifacts")

    @classmethod
    def load(cls):
        return cls()


FAMILY = re.compile(r"^/api/(?:v\d+/)?([A-Za-z]+)")
# Paths pinned to a microversion (/m/...) or version (/v/...) describe immutable state.
IMMUTABLE = re.compile(r"/[mv]/[a-f0-9]{24}(?:/|$)")
CACHE_LIMIT = 32 * 1024 * 1024
HEX24 = re.compile(r"^[a-f0-9]{24}$")
ID_AFTER = {"featureid", "partid", "fid", "nodeid"}
MARKERS = {"d", "w", "v", "m", "e", "wv", "wvm"}
SAMPLES = 40
STALE_S = 900
MAX_ENDPOINTS = 60
MAX_ETAGS = 300
DEFAULT_PACING = {"reserve": 5, "burst": 30, "refill_per_min": 30.0, "max_wait_s": 20.0}
PACING_LIMITS = {"reserve": (0, 1000), "burst": (1, 1000), "refill_per_min": (0.1, 6000.0), "max_wait_s": (0.0, 120.0)}


def family(path):
    """Endpoint family used for rate-limit bookkeeping, e.g. /api/v10/partstudios/... -> partstudios.
    Observed 2026-10-25: /partstudios/../features and /partstudios/../featurescript shared one
    Retry-After deadline while /featurestudios and /documents kept working."""
    match = FAMILY.match(path or "")
    return match.group(1).lower() if match else "other"


def endpoint(method, path):
    """Endpoint key without IDs, e.g. 'GET partstudios/features'. Onshape reports X-Rate-Limit-Remaining per endpoint."""
    rest = re.sub(r"^/api/(?:v\d+/)?", "", (path or "").split("?")[0])
    out, skip = [], False
    for seg in rest.split("/"):
        low = seg.lower()
        if skip:
            skip = False
            continue
        if not low or HEX24.match(low) or low in MARKERS:
            continue
        out.append(low)
        skip = low in ID_AFTER
    return f"{method} {'/'.join(out) or 'other'}"


def clock(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def header_int(headers, *names):
    for name in names:
        if name in headers:
            try:
                return int(headers[name])
            except (TypeError, ValueError):
                return None
    return None


class Ledger:
    """Rate-limit bookkeeping from Onshape's own headers (limits are unpublished):
    X-Rate-Limit-Remaining (every response) = calls left for that endpoint in the current fixed window;
    Retry-After (with 429) = seconds until that counter resets. Per family: requests, 429s, Retry-After
    block and reset time. Per endpoint: remaining, largest remaining seen (~ the allowance) and recent
    samples, so the refill behaviour can be learned. Deadlines and counters persist across restarts;
    request counts are per process."""

    def __init__(self, path=None):
        self.path = path
        self.data = {}
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text())
                self.data = {k: {**v, "requests": 0, "cache_hits": 0, "revalidated": 0}
                             for k, v in loaded.items() if isinstance(v, dict)}
            except (ValueError, AttributeError, OSError):
                self.data = {}

    def entry(self, fam):
        e = self.data.setdefault(fam, {})
        for key, value in (("requests", 0), ("cache_hits", 0), ("revalidated", 0), ("errors_429", 0),
                           ("remaining", None), ("blocked_until", None), ("reset_at", None), ("endpoints", {})):
            e.setdefault(key, value)
        return e

    def blocked(self, fam, now=None):
        until = self.entry(fam).get("blocked_until")
        return until if until and until > (now or time.time()) else None

    def hit(self, fam):
        self.entry(fam)["cache_hits"] += 1

    def record(self, fam, status, headers, now=None, key=None):
        now = now or time.time()
        e = self.entry(fam)
        if e.get("reset_at") and e["reset_at"] <= now:
            e["last_reset"], e["reset_at"] = e["reset_at"], None
        e["requests"] += 1
        if status == 304:
            e["revalidated"] += 1
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        remaining = header_int(h, "x-rate-limit-remaining", "x-ratelimit-remaining")
        if status == 429 and remaining is None:
            remaining = 0
        if remaining is not None:
            e["remaining"], e["remaining_at"] = remaining, now
            if key:
                ep = e["endpoints"].setdefault(key, {})
                previous = ep.get("remaining")
                if previous is not None and remaining > previous:
                    # The counter went up between two responses: the window reset (or refilled) in between.
                    e["resets_seen"] = (e.get("resets_seen") or [])[-4:] + [round(now)]
                ep["remaining"], ep["at"] = remaining, now
                ep["allowance"] = max(ep.get("allowance") or 0, remaining + 1)
                ep["samples"] = (ep.get("samples") or [])[-(SAMPLES - 1):] + [[round(now), remaining]]
                if len(e["endpoints"]) > MAX_ENDPOINTS:
                    oldest = min(e["endpoints"], key=lambda k: e["endpoints"][k].get("at", 0))
                    e["endpoints"].pop(oldest, None)
        if status == 429:
            e["errors_429"] += 1
            try:
                wait = float(h.get("retry-after", 60))
            except (TypeError, ValueError):
                wait = 60.0
            e["blocked_until"] = e["reset_at"] = now + max(wait, 1.0)
        elif 200 <= status < 400:
            e["blocked_until"] = None
        self.save()

    def current(self, fam, key, now=None):
        """(remaining, reset_at) for key, falling back to the family's latest reading, when that reading still
        belongs to the current window. None when unknown, reset since, or stale with an unknown reset time."""
        now = now or time.time()
        e = self.data.get(fam) or {}
        ep = (e.get("endpoints") or {}).get(key) or {}
        remaining, at = (ep.get("remaining"), ep.get("at")) if ep.get("remaining") is not None else (e.get("remaining"), e.get("remaining_at"))
        if remaining is None or at is None:
            return None
        # A reading from before a reset (Retry-After deadline passed, or a counter seen rising) is void.
        boundaries = [t for t in [e.get("last_reset"), e.get("reset_at"), *(e.get("resets_seen") or [])] if t and t <= now]
        if boundaries and at < max(boundaries):
            return None
        reset = e.get("reset_at") if e.get("reset_at") and e["reset_at"] > now else None
        if not reset and now - at > STALE_S:
            return None
        return remaining, reset, at

    def hints(self, now=None):
        """Endpoints whose remaining count is low (<= 20 or <= 15% of the largest seen) in the current window."""
        try:
            return self._hints(now)
        except Exception:  # advisory only; never let bookkeeping break a tool result
            return {}

    def _hints(self, now=None):
        now = now or time.time()
        out = {}
        for fam, e in self.data.items():
            for key, ep in (e.get("endpoints") or {}).items():
                state = self.current(fam, key, now)
                if not state or ep.get("remaining") is None:
                    continue
                left, reset, _ = state
                allowance = ep.get("allowance") or 0
                if left <= max(20, 0.15 * allowance):
                    row = {"left": left}
                    if allowance > left + 1:
                        row["of"] = allowance
                    if reset:
                        row["resets"] = clock(reset)
                    out[key] = row
        return out

    def save(self):
        if not self.path:
            return
        try:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data))
            os.replace(temp, self.path)
        except OSError:
            pass

    def summary(self, now=None):
        now = now or time.time()
        out = {}
        for fam, e in sorted(self.data.items()):
            row = {"requests": e.get("requests", 0), "cache_hits": e.get("cache_hits", 0), "remaining": e.get("remaining")}
            if e.get("revalidated"):
                row["revalidated_304"] = e["revalidated"]
            if e.get("errors_429"):
                row["errors_429"] = e["errors_429"]
            if e.get("blocked_until") and e["blocked_until"] > now:
                row["blocked_until"] = clock(e["blocked_until"])
                row["blocked_for_s"] = int(e["blocked_until"] - now)
            elif e.get("reset_at") and e["reset_at"] > now:
                row["resets_at"] = clock(e["reset_at"])
            if e.get("resets_seen"):
                row["resets_seen"] = [clock(t) for t in e["resets_seen"]]
            endpoints = {k: {"remaining": v.get("remaining"), "max_seen": v.get("allowance")}
                         for k, v in (e.get("endpoints") or {}).items() if v.get("remaining") is not None}
            if endpoints:
                row["endpoints"] = endpoints
            out[fam] = row
        return out


class Pacer:
    """Short-term burst limit per family (token bucket, in memory). A safety net against runaway loops;
    the real budget is the window counter tracked by Ledger."""

    def __init__(self, config):
        self.config = config
        self.tokens = {}
        self.stamp = {}

    def delay(self, fam, now=None):
        now = time.monotonic() if now is None else now
        burst, rate = self.config["burst"], self.config["refill_per_min"] / 60.0
        tokens = min(burst, self.tokens.get(fam, burst) + (now - self.stamp.get(fam, now)) * rate)
        self.tokens[fam], self.stamp[fam] = tokens, now
        return 0.0 if tokens >= 1 else (1 - tokens) / rate

    def take(self, fam):
        self.tokens[fam] = self.tokens.get(fam, self.config["burst"]) - 1


class Client:
    def __init__(self, settings=None, transport=None):
        self.settings = settings or Settings.load()
        config = load_config()
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{config['port']}",
                                      headers={"Authorization": "Bearer " + config["token"]},
                                      timeout=110, follow_redirects=False, trust_env=False, transport=transport)
        self.calls = 0
        root = Path(self.settings.cache_dir)
        self.ledger = Ledger(root.parent / "ratelimit.json")
        self.rate = self.ledger.data
        self.cache_dir = root / "immutable"
        self.etag_dir = root / "etag"
        self.pacing_path = root.parent / "pacing.json"
        self.pacing = dict(DEFAULT_PACING)
        try:
            stored = json.loads(self.pacing_path.read_text()) if self.pacing_path.is_file() else {}
            self.set_pacing(stored, save=False)
        except (ValueError, OSError, OnshapeError):
            pass
        self.pacer = Pacer(self.pacing)

    async def close(self):
        await self.http.aclose()

    def set_pacing(self, changes, save=False):
        """Update reserve / burst / refill_per_min / max_wait_s, all-or-nothing. Tool calls change this server
        process only; defaults that survive restarts live in <runtime>/pacing.json (edited by the user)."""
        if not isinstance(changes, dict) or set(changes) - set(DEFAULT_PACING):
            raise OnshapeError("pacing keys: " + ", ".join(DEFAULT_PACING))
        for key, value in changes.items():
            low, high = PACING_LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise OnshapeError(f"pacing.{key} must be a number from {low} to {high}. Nothing was changed.")
        for key, value in changes.items():
            self.pacing[key] = int(value) if key in ("reserve", "burst") else float(value)
        if save:
            try:
                self.pacing_path.write_text(json.dumps(self.pacing))
            except OSError:
                pass
        return dict(self.pacing)

    def guard(self, fam, key=None):
        until = self.ledger.blocked(fam)
        if until:
            open_families = [f for f in self.ledger.data if f != fam and not self.ledger.blocked(f)]
            raise OnshapeError(f"Onshape rate limit: /{fam} requests are blocked until {clock(until)} (Retry-After, "
                               f"{int(until - time.time())} s left). Nothing was sent."
                               + (f" Still open: {', '.join('/' + f for f in open_families)}." if open_families else ""), 429)
        state = self.ledger.current(fam, key) if key else None
        if state and state[0] <= self.pacing["reserve"]:
            left, reset, at = state
            when = f"it resets at {clock(reset)}" if reset else f"reset time unknown; this reading expires in {int(STALE_S - (time.time() - at))} s"
            raise OnshapeError(f"Onshape budget: {left} requests left for {key} in this window (reserve {self.pacing['reserve']}); {when}. "
                               "Nothing was sent. Spend the reserve only for something essential: bridge_status(pacing={'reserve': 0}) lowers it until the MCP server restarts.", 429)

    async def pace(self, fam):
        wait = self.pacer.delay(fam)
        if wait > self.pacing["max_wait_s"]:
            raise OnshapeError(f"Pacing: burst limit for /{fam} reached ({self.pacing['burst']} requests, refill "
                               f"{self.pacing['refill_per_min']:g}/min); next request in {wait:.0f} s. Nothing was sent. "
                               "Check for a loop, or adjust bridge_status(pacing=...).", 429)
        if wait:
            await asyncio.sleep(wait)
            self.pacer.delay(fam)
        self.pacer.take(fam)

    async def command(self, job):
        rest = job.get("kind") == "rest"
        fam = family(job.get("path")) if rest else None
        key = endpoint(job.get("method"), job.get("path")) if rest else None
        if fam:
            self.guard(fam, key)
            await self.pace(fam)
        try:
            response = await self.http.post("/command", json=job)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise OnshapeError("Local bridge unavailable or command outcome unknown. Inspect state before retrying.") from None
        if not isinstance(data, dict):
            raise OnshapeError("Page returned no structured result; outcome unknown. Inspect state before retrying.")
        if response.status_code != 200 or data.get("error"):
            raise OnshapeError(data.get("error", "Bridge rejected the command."))
        if fam and isinstance(data.get("status"), int):
            self.ledger.record(fam, data["status"], data.get("response_headers"), key=key)
        return data

    def cache_path(self, method, path, query, body):
        key = hashlib.sha256(json.dumps([method, path, query or {}, body], sort_keys=True, default=str).encode()).hexdigest()
        return self.cache_dir / key

    async def status(self):
        try:
            response = await self.http.get("/health")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            return {"bridge_connected": False, "extension_connected": False}

    def etag_entry(self, path, query):
        file = self.etag_dir / hashlib.sha256(json.dumps([path, query or {}], sort_keys=True, default=str).encode()).hexdigest()
        try:
            stored = json.loads(file.with_suffix(".json").read_text())
            return file, stored if isinstance(stored, dict) and stored.get("etag") else None
        except (OSError, ValueError):
            return file, None

    async def request(self, method, path, query=None, body=None, *, read_only=False, accept=None):
        fam = family(path)
        cacheable = (method == "GET" or read_only) and bool(IMMUTABLE.search(path or ""))
        base = self.cache_path(method, path, query, body) if cacheable else None
        if base:
            for suffix, binary in ((".json", False), (".bin", True)):
                cached = base.with_suffix(suffix)
                if cached.is_file():
                    self.ledger.hit(fam)
                    return cached.read_bytes() if binary else json.loads(cached.read_text())
        job = {"kind": "rest", "method": method, "path": path, "query": query or {}, "body": body}
        if accept:
            job["accept"] = accept
        etag_file, known = self.etag_entry(path, query) if method == "GET" and not base and not accept else (None, None)
        if known:
            # Revalidate a mutable read: an unchanged resource answers 304 with no body.
            job["request_headers"] = {"If-None-Match": known["etag"]}
        self.calls += 1
        data = await self.command(job)
        status = data.get("status", 0)
        if status == 304 and known:
            return known["body"]
        if status == 429:
            until = self.ledger.blocked(fam)
            raise OnshapeError(f"Onshape HTTP 429 (rate limit) on /{fam}"
                               + (f": blocked until {clock(until)} (Retry-After {int(until - time.time())} s)." if until else ".")
                               + " Do not retry before then; other endpoint families may still work (see bridge_status).", 429)
        if not 200 <= status < 300:
            raise OnshapeError(f"Onshape browser HTTP {status}. Inspect state before retrying writes.", status)
        if data.get("encoding") == "base64":
            result = base64.b64decode(data["body"])
        else:
            result = data.get("body", {})
        if base:
            self.store_cache(base, result)
        elif etag_file is not None and not isinstance(result, bytes):
            tag = {str(k).lower(): v for k, v in (data.get("response_headers") or {}).items()}.get("etag")
            if tag:
                self.store_json(etag_file.with_suffix(".json"), {"etag": tag, "body": result})
                self.prune(self.etag_dir, MAX_ETAGS)
        return result

    @staticmethod
    def prune(folder, keep):
        try:
            files = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime)
            for old in files[:-keep]:
                old.unlink()
        except OSError:
            pass

    def store_json(self, target, value):
        try:
            raw = json.dumps(value).encode()
            if len(raw) > CACHE_LIMIT:
                return
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = target.with_suffix(".tmp")
            temp.write_bytes(raw)
            os.replace(temp, target)
        except OSError:
            pass

    def store_cache(self, base, result):
        try:
            raw = result if isinstance(result, bytes) else json.dumps(result).encode()
            if len(raw) > CACHE_LIMIT:
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = base.with_suffix(".bin" if isinstance(result, bytes) else ".json")
            temp = target.with_suffix(".tmp")
            temp.write_bytes(raw)
            os.replace(temp, target)
        except OSError:
            pass
