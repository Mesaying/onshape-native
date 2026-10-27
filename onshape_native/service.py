from __future__ import annotations

import asyncio
import copy
import json
import re
from urllib.parse import urlsplit, parse_qs

from .catalog import Catalog
from .client import Client, OnshapeError, Settings
from .geometry import FEATURE_ID, SUMMARY_SCRIPT, decode_fs, delta, feature_rows, report_script
from .store import Store

HEX24 = re.compile(r"[a-f0-9]{24}")
# A feature that built: INFO/WARNING carry its own messages (e.g. a summary), they are not failures.
BUILT = ("OK", "INFO", "WARNING")


class Service:
    def __init__(self, settings=None, transport=None):
        self.settings = settings or Settings.load()
        self.client = Client(self.settings, transport)
        self.catalog = Catalog()
        self.store = Store(self.settings.cache_dir)
        self.locks = {}
        # (did, workspace id) -> newest microversion seen in this server's own responses.
        # An open editor tab can lag a REST write for a moment; never trust a tab older than this.
        self.latest = {}

    def remember(self, did, wid, micro):
        if did and wid and isinstance(micro, str) and HEX24.fullmatch(micro):
            self.latest[(did, wid)] = micro

    async def tab_microversion(self, target, configuration):
        """Microversion shown by an already-open editor tab for this element. Local only: no REST request."""
        if target["wvm"] != "w" or configuration:
            return None
        native = {"did": target["did"], "wid": target["wvmid"], "eid": target["eid"]}
        for kind in ("microversion", "state"):
            try:
                data = await self.client.command({"kind": kind, "target": native})
            except OnshapeError as error:
                # Older extensions/bridges lack the light "microversion" command; the full state read still works.
                if kind == "microversion" and "nknown" in str(error):
                    continue
                return None
            micro = data.get("microversion") if isinstance(data, dict) else None
            return micro if isinstance(micro, str) and HEX24.fullmatch(micro) else None
        return None

    def snapshot_from_features(self, url, raw, base=None):
        """Snapshot built from an already-fetched feature list (no request). base keeps target metadata."""
        target, configuration = self.target(url)
        micro = raw.get("sourceMicroversion")
        if not micro:
            raise OnshapeError("Feature list lacks sourceMicroversion; cannot build a snapshot.")
        data = {"kind": "onshape-snapshot", "url": url, "target": target, "configuration": configuration,
                "microversion": micro, "libraryVersion": raw.get("libraryVersion", (base or {}).get("libraryVersion")),
                "serializationVersion": raw.get("serializationVersion", (base or {}).get("serializationVersion")),
                "features": feature_rows(raw), "feature_artifact": self.store.put(raw)["artifact"],
                "frame": {"coordinates": "Part Studio world, right-handed XYZ", "length": "mm", "volume": "mm^3"},
                "geometry_complete": False}
        return self.store.put(data)["artifact"]

    @staticmethod
    def fold(raw, response, action):
        """Apply one feature write response (full feature + featureState + microversion) to a feature list."""
        feature = response.get("feature") or {}
        fid = feature.get("featureId")
        if not fid:
            return
        features = raw.setdefault("features", [])
        states = raw.setdefault("featureStates", {})
        index = next((i for i, f in enumerate(features) if f.get("featureId") == fid), None)
        if index is None:
            # New features land at the rollback bar (the end unless the user rolled back).
            at = raw.get("rollbackIndex", len(features))
            at = at if isinstance(at, int) and 0 <= at < len(features) else len(features)
            features.insert(at, feature)
            if isinstance(raw.get("rollbackIndex"), int):
                raw["rollbackIndex"] = at + 1
        else:
            features[index] = feature
            if action == "edit":
                # Downstream features regenerate too; their new states were not part of this response.
                for later in features[index + 1:]:
                    lid = later.get("featureId")
                    if lid:
                        states[lid] = {**states.get(lid, {}), "featureStatus": "UNVERIFIED"}
        if response.get("featureState"):
            states[fid] = response["featureState"]
        for key in ("sourceMicroversion", "libraryVersion", "serializationVersion"):
            if response.get(key):
                raw[key] = response[key]

    async def call(self, operation, path=None, query=None, body=None):
        method, url = self.catalog.resolve(operation, path or {}, query)
        return await self.client.request(method, url, query, body, read_only=operation == "evalFeatureScript")

    def target(self, url):
        u = urlsplit(url)
        if u.netloc != urlsplit(self.settings.base_url).netloc or u.scheme != "https":
            raise OnshapeError("Document URL must match the configured Onshape HTTPS stack.")
        m = re.fullmatch(r"/documents/([a-f0-9]{24})/([wvm])/([a-f0-9]{24})/e/([a-f0-9]{24})/?", u.path)
        if not m:
            raise OnshapeError("Provide an Onshape element URL: /documents/DID/w/WID/e/EID (or v/m).")
        return dict(zip(("did", "wvm", "wvmid", "eid"), m.groups())), parse_qs(u.query).get("configuration", [""])[0]

    def snapshot_data(self, snapshot):
        data = self.store.read(snapshot)
        if data.get("kind") != "onshape-snapshot":
            raise OnshapeError("Expected a snapshot artifact from inspect_model.")
        return data

    @staticmethod
    def pinned(data):
        return {**data["target"], "wvm": "m", "wvmid": data["microversion"]}

    async def eval(self, data, script, queries=None):
        result = await self.call("evalFeatureScript", self.pinned(data),
                                 {"configuration": data["configuration"]},
                                 {"script": script, "queries": queries or {}, "libraryVersion": data["libraryVersion"]})
        parsed = decode_fs(result.get("result"))
        if parsed is None:
            # Notices are bounded by artifact paging; they may contain user model text.
            return {"evaluation_failed": True, "details": self.store.response(result)}
        return {"result": parsed, "notices": result.get("notices", [])}

    async def inspect(self, url, geometry=True, previous=None):
        target, configuration = self.target(url)
        old = self.snapshot_data(previous) if previous else None
        if old and (old["target"] != target or old["configuration"] != configuration):
            raise OnshapeError("Previous snapshot refers to another element or configuration.")
        doc = (target["did"], target["wvmid"])
        tab = await self.tab_microversion(target, configuration) if old else None
        if old and tab and tab == old["microversion"] and self.latest.get(doc) in (None, tab):
            if not geometry or old.get("geometry_complete"):
                return {"snapshot": previous, "microversion": tab, "unchanged": True, "api_calls": 0,
                        "source": "open editor tab (no REST request)"}
            raw, micro = self.store.read(old["feature_artifact"]), tab
        else:
            raw = await self.call("getPartStudioFeatures", target, {"configuration": configuration, "noSketchGeometry": True})
            micro = raw.get("sourceMicroversion")
            if not micro:
                raise OnshapeError("Feature response lacks sourceMicroversion; cannot construct a consistent snapshot.")
            if target["wvm"] == "w":
                self.remember(target["did"], target["wvmid"], micro)
            if old and micro == old["microversion"] and (not geometry or old.get("geometry_complete")):
                return {"snapshot": previous, "microversion": micro, "unchanged": True, "api_calls": 1}
        data = {"kind": "onshape-snapshot", "url": url, "target": target, "configuration": configuration,
                "microversion": micro, "libraryVersion": raw["libraryVersion"],
                "serializationVersion": raw["serializationVersion"], "features": feature_rows(raw),
                "feature_artifact": self.store.put(raw)["artifact"],
                "frame": {"coordinates": "Part Studio world, right-handed XYZ", "length": "mm", "volume": "mm^3"},
                "geometry_complete": False}
        if geometry:
            # One pinned (cacheable) evaluate returns solids with names; no separate parts request.
            # Sheet/wire/composite parts are not listed here: use api_read getPartsWMVE for those.
            measured = await self.eval(data, SUMMARY_SCRIPT)
            if isinstance(measured.get("result"), list):
                data["solids"] = measured["result"]
                data["parts"] = [{"id": p.get("id"), "name": p.get("name"), "type": "solid"} for p in data["solids"]]
                data["part_count"] = len(data["parts"])
                data["geometry_complete"] = True
            else:
                data["geometry_error"] = measured
        saved = self.store.put(data)
        output = {"snapshot": saved["artifact"], "microversion": micro, "frame": data["frame"],
                  "feature_count": len(data["features"]), "geometry_complete": data["geometry_complete"],
                  "feature_errors": [f for f in data["features"] if f["status"] not in ("OK", "SUPPRESSED")],
                  "artifact_path": saved["path"]}
        if old and old.get("geometry_complete") == data.get("geometry_complete"):
            output["delta"] = delta(old, data)
        else:
            output.update({k: data[k] for k in ("features", "solids", "parts", "geometry_error") if k in data})
        if len(json.dumps(output)) > 9000:
            for field in ("features", "solids", "parts", "feature_errors", "delta"):
                if field in output:
                    output[field] = {"omitted": True, "retrieve": "artifact_page", "pointer": "/" + field}
            output["note"] = "Full snapshot is saved. Page features, parts, and solids from snapshot; delta can be derived from snapshots."
        return output

    def prepare(self, data, raw, action, changes):
        """Patched full feature definitions for a feature write (no request unless a sketch needs its full definition)."""
        originals = {f["featureId"]: f for f in raw.get("features", [])}
        work = []
        for change in changes:
            if action == "add":
                if change.get("featureId"):
                    raise OnshapeError("Do not reuse an existing featureId when adding a feature.")
                if not change.get("featureType") or not change.get("name"):
                    raise OnshapeError("Added feature needs featureType and name; use current feature schema.")
                work.append(copy.deepcopy(change))
                continue
            if change.get("id") not in originals:
                raise OnshapeError("Feature ID is absent from this snapshot.")
            if set(change) - {"id", "parameters", "name", "suppressed"}:
                raise OnshapeError("Edit keys: id, parameters, name, suppressed.")
            if "suppressed" in change and not isinstance(change["suppressed"], bool):
                raise OnshapeError("suppressed must be true or false.")
            f = copy.deepcopy(originals[change["id"]])
            work.append((change, f))
        return work, originals

    async def patched(self, data, change, f, full_sketch):
        f = copy.deepcopy(f)
        if full_sketch and f.get("featureType") == "newSketch":
            # A lightweight snapshot omits sketch entities. Fetch a complete definition
            # at the same immutable revision before editing, never erase constraints.
            full = await self.call("getPartStudioFeatures", self.pinned(data),
                                   {"featureId": [change["id"]], "configuration": data["configuration"], "noSketchGeometry": False})
            f = next(x for x in full["features"] if x["featureId"] == change["id"])
        for k in ("name", "suppressed"):
            if k in change:
                f[k] = change[k]
        params = {p["parameterId"]: p for p in f.get("parameters", [])}
        for pid, patch in change.get("parameters", {}).items():
            if pid not in params:
                raise OnshapeError(f"Unknown parameter {pid}; inspect the feature definition.")
            if isinstance(patch, str):
                if "expression" not in params[pid]:
                    raise OnshapeError("String shorthand requires an expression parameter.")
                params[pid]["expression"] = patch
            elif isinstance(patch, dict) and not set(patch) - {"expression", "value", "queries"}:
                params[pid].update(patch)
            else:
                raise OnshapeError("Use expression string or a patch with expression/value/queries.")
        return f

    async def feature(self, snapshot, action, changes, report=False, variables=None):
        data = self.snapshot_data(snapshot)
        if data["target"]["wvm"] != "w":
            raise OnshapeError("Versions and microversions are immutable; choose a workspace URL.")
        if not 1 <= len(changes) <= 20:
            raise OnshapeError("Supply 1-20 changes. Batch is sequential, not atomic.")
        if action not in ("add", "edit"):
            raise OnshapeError("Use add or edit.")
        if action == "edit" and len({c.get("id") for c in changes}) != len(changes):
            raise OnshapeError("Combine all changes to the same feature into one edit entry.")
        if report:
            report_script([], ["checksText"] if variables is None else variables)  # reject bad names before writing
        raw = self.store.read(data["feature_artifact"])
        prepared, _ = self.prepare(data, raw, action, changes)
        # Several parameter/suppression edits go out as ONE updateFeatures request. Renames need per-feature
        # updates, and sketches stay sequential (their full definition is sent) until batch merging is verified.
        fallback = None
        if (action == "edit" and len(changes) > 1 and not any("name" in c for c in changes)
                and not any(f.get("featureType") == "newSketch" or f.get("btType", "BTMFeature-134") != "BTMFeature-134" for _, f in prepared)):
            work = [await self.patched(data, change, f, False) for change, f in prepared]
            out = await self.batch_edit(data, raw, work, changes)
            if out is not None:
                return await self.finish(out, report, variables)
            fallback = "updateFeatures rejected the request shape (HTTP 400, nothing applied); edits were sent one by one, so they are not all-or-nothing."
        if action == "edit":
            work = [await self.patched(data, change, f, True) for change, f in prepared]
        else:
            work = prepared
        out = await self.sequential(data, raw, action, work)
        if fallback:
            out["batch_fallback"] = fallback
        return await self.finish(out, report, variables)

    def outcome_unknown(self, target, error):
        """A write whose outcome is unknown (bridge timeout/disconnect) may have landed: stop trusting the
        open tab's microversion for this workspace until a REST read establishes the state again."""
        if getattr(error, "status", None) is None:
            self.latest[(target["did"], target["wvmid"])] = "UNKNOWN"
            return True
        return False

    async def sequential(self, data, raw, action, work):
        result, micro = [], data["microversion"]
        after = copy.deepcopy(raw)
        lock = self.locks.setdefault(data["target"]["did"] + data["target"]["wvmid"], asyncio.Lock())
        async with lock:
            for f in work:
                body = {"btType": "BTFeatureDefinitionCall-1406", "feature": f,
                        "sourceMicroversion": micro, "rejectMicroversionSkew": True,
                        "libraryVersion": data["libraryVersion"], "serializationVersion": data["serializationVersion"]}
                target = data["target"]
                op, path = "addPartStudioFeature", target
                if action == "edit":
                    op, path = "updatePartStudioFeature", {"did": target["did"], "wid": target["wvmid"], "eid": target["eid"], "fid": f["featureId"]}
                try:
                    response = await self.call(op, path, body=body)
                except OnshapeError as error:
                    unknown = self.outcome_unknown(target, error)
                    out = {"completed": result, "stopped": True, "error": str(error), "requests": len(result) + 1,
                           "next": ("The last write's outcome is unknown: inspect_model(url) without previous before continuing; "
                                    if unknown else "Inspect current state before continuing; ") + "completed changes remain."}
                    if result:
                        out["snapshot"] = self.snapshot_from_features(data["url"], after, data)
                        out["snapshot_covers"] = "completed writes only"
                    return out
                result.append({"id": response.get("feature", {}).get("featureId"),
                               "name": f["name"], "state": response.get("featureState"),
                               "response": self.store.put(response)["artifact"]})
                micro = response.get("sourceMicroversion")
                if micro and not response.get("microversionSkew"):
                    self.fold(after, response, action)
                    self.remember(target["did"], target["wvmid"], micro)
                if not micro or response.get("microversionSkew") or response.get("featureState", {}).get("featureStatus") not in BUILT:
                    out = {"completed": result, "stopped": True, "requests": len(result), "next": "Inspect regeneration errors; do not replay the batch."}
                    if micro and not response.get("microversionSkew"):
                        out["snapshot"] = self.snapshot_from_features(data["url"], after, data)
                    return out
        return {"completed": result, "microversion": micro, "requests": len(work),
                "snapshot": self.snapshot_from_features(data["url"], after, data),
                "next": "Use snapshot for the next feature or evaluate call: it already holds the written features and their states (no inspect needed). Geometry is not re-measured: pass report=true, or evaluate(preset='report')."}

    async def batch_edit(self, data, raw, work, changes):
        """One POST .../features/updates for several edits. Returns None when Onshape rejects the request shape
        (HTTP 400: nothing applied) so the caller can fall back to per-feature updates."""
        target = data["target"]
        suppress = any("suppressed" in c for c in changes)
        features = []
        for f, change in zip(work, changes):
            changed = set(change.get("parameters", {}))
            entry = {"btType": f.get("btType", "BTMFeature-134"), "featureId": f["featureId"],
                     "parameters": [p for p in f.get("parameters", []) if p.get("parameterId") in changed]}
            if suppress:
                entry["suppressed"] = bool(f.get("suppressed", False))
            features.append(entry)
        body = {"btType": "BTUpdateFeaturesCall-1748", "features": features,
                "sourceMicroversion": data["microversion"], "rejectMicroversionSkew": True,
                "libraryVersion": data["libraryVersion"], "serializationVersion": data["serializationVersion"]}
        if suppress:
            body["updateSuppressionAttributes"] = True
        lock = self.locks.setdefault(target["did"] + target["wvmid"], asyncio.Lock())
        async with lock:
            try:
                response = await self.call("updateFeatures", {"did": target["did"], "wid": target["wvmid"], "eid": target["eid"]}, body=body)
            except OnshapeError as error:
                if getattr(error, "status", None) == 400:
                    self.batch_fallbacks = getattr(self, "batch_fallbacks", 0) + 1
                    return None
                if self.outcome_unknown(target, error):
                    return {"completed": [], "stopped": True, "error": str(error),
                            "next": "Batch outcome unknown (it may have been applied): inspect_model(url) without previous before retrying."}
                return {"completed": [], "stopped": True, "error": str(error),
                        "next": "Onshape refused the batch; nothing was applied. Inspect before retrying."}
        micro = response.get("sourceMicroversion")
        states = response.get("featureStates") or {}
        artifact = self.store.put(response)["artifact"]
        if not micro or response.get("microversionSkew"):
            return {"completed": [], "stopped": True, "response": artifact,
                    "next": "Microversion skew: the batch was refused. Inspect, then edit against the fresh snapshot."}
        after = copy.deepcopy(raw)
        returned = {f.get("featureId"): f for f in response.get("features") or [] if isinstance(f, dict)}
        index = {f.get("featureId"): i for i, f in enumerate(after.get("features", []))}
        first = min(index[f["featureId"]] for f in work)
        for f in work:
            echo = returned.get(f["featureId"])
            # Prefer Onshape's echo when it is a complete definition; else our patched full definition.
            after["features"][index[f["featureId"]]] = echo if echo and len(echo.get("parameters", [])) >= len(f.get("parameters", [])) else f
        feature_states = after.setdefault("featureStates", {})
        for later in after["features"][first:]:
            lid = later.get("featureId")
            if lid:
                feature_states[lid] = {**feature_states.get(lid, {}), "featureStatus": "UNVERIFIED"}
        feature_states.update({k: v for k, v in states.items() if isinstance(v, dict)})
        for key in ("sourceMicroversion", "libraryVersion", "serializationVersion"):
            if response.get(key):
                after[key] = response[key]
        self.remember(target["did"], target["wvmid"], micro)
        completed = [{"id": f["featureId"], "name": f.get("name"), "state": feature_states.get(f["featureId"])} for f in work]
        out = {"completed": completed, "microversion": micro, "requests": 1, "batched": True, "response": artifact,
               "snapshot": self.snapshot_from_features(data["url"], after, data),
               "next": "One updateFeatures request applied all edits. Downstream features are UNVERIFIED until a report or inspect."}
        bad = [c["id"] for c in completed if (c["state"] or {}).get("featureStatus") not in BUILT + ("UNVERIFIED", None)]
        if bad:
            out["next"] = f"Edits applied, but {', '.join(bad)} did not regenerate cleanly. Fix forward; do not replay the batch."
        return out

    async def report(self, data, variables=None):
        """One pinned evaluate: feature errors/warnings, solids and published variables for a snapshot."""
        ids = [f["id"] for f in data["features"] if f.get("id") and not f.get("suppressed") and FEATURE_ID.match(f["id"])]
        names = ["checksText"] if variables is None else variables
        measured = await self.eval(data, report_script(ids, names))
        measured["checked_ids"] = ids
        return measured

    def apply_report(self, data, result, checked_ids):
        problems = result.get("problems") if isinstance(result.get("problems"), dict) else None
        complete = problems is not None and result.get("checked") == len(checked_ids)
        checked = set(checked_ids) if complete else set()
        for f in data["features"]:
            problem = (problems or {}).get(f["id"])
            if isinstance(problem, dict):
                f["status"], f["message"] = problem.get("status", "ERROR"), problem.get("message")
            elif f["status"] == "UNVERIFIED" and f["id"] in checked:
                f["status"] = "OK"
        if isinstance(result.get("solids"), list):
            data["solids"] = result["solids"]
            data["parts"] = [{"id": p.get("id"), "name": p.get("name"), "type": "solid"} for p in data["solids"]]
            data["part_count"] = len(data["parts"])
            data["geometry_complete"] = True
        data["variables"] = result.get("variables") or {}
        data["states_source"] = "write responses + report" if complete else "write responses (report incomplete)"
        return data

    async def finish(self, out, report, variables):
        if not report or not out.get("snapshot"):
            return out
        data = self.snapshot_data(out["snapshot"])
        try:
            measured = await self.report(data, variables)
        except Exception as error:  # the write already happened: never let the report hide it
            out["report_error"] = str(error)
            out["next"] = "The write succeeded (see completed/snapshot); only the report failed. Do NOT repeat the write; evaluate(snapshot, preset='report') later."
            return out
        if not isinstance(measured.get("result"), dict):
            out["report_error"] = measured
            return out
        data = self.apply_report(data, measured["result"], measured["checked_ids"])
        out["snapshot"] = self.store.put(data)["artifact"]
        out["requests"] = out.get("requests", 0) + 1
        out["report"] = {"feature_problems": [{"id": f["id"], "name": f["name"], "status": f["status"], "message": f.get("message")}
                                              for f in data["features"] if f["status"] not in ("OK", "SUPPRESSED")],
                         "solids": [{"name": s.get("name"), "size_mm": s.get("size_mm"), "volume_mm3": s.get("volume_mm3")} for s in data.get("solids", [])],
                         "variables": data["variables"]}
        out["next"] = "Snapshot includes report results (feature states, solids, variables). No inspect needed."
        return out

    async def write(self, operation, path, query, body, detail="summary"):
        method, _ = self.catalog.resolve(operation, path, query)
        if method == "GET" or operation == "evalFeatureScript":
            raise OnshapeError("Use api_read or evaluate for reads.")
        if path.get("wvm") in ("v", "m"):
            raise OnshapeError("Mutation requires a workspace, not a version/microversion.")
        if operation in ("addPartStudioFeature", "updatePartStudioFeature", "updateFeatures", "updateFeatureStudioContents"):
            if not body or not body.get("sourceMicroversion") or body.get("rejectMicroversionSkew") is not True:
                raise OnshapeError("Feature writes require sourceMicroversion and rejectMicroversionSkew=true. Prefer feature tool.")
        if operation == "createDocument":
            body = {**(body or {}), "isPublic": (body or {}).get("isPublic", False)}
        from .responses import respond
        raw = await self.call(operation, path, query, body)
        remember = getattr(self, "remember", None)
        if remember and isinstance(raw, dict):
            remember(path.get("did"), path.get("wid") or path.get("wvmid"), raw.get("sourceMicroversion"))
        return respond(self.store, raw, detail, "write")
