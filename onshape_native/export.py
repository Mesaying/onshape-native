"""Exports without tool-call polling.

STL: one tessellatedfaces read (pinned to a snapshot microversion, so repeats are cached) converted locally,
with a watertight check. STEP: create a translation, poll its state here with backoff, download the result.
Measured 2026-10-25: tessellation STL is finer and keeps parts separate; translation STL merged parts."""
import asyncio
import collections
import math
import re
import struct

from .client import OnshapeError

POLL_DELAYS = (3, 3, 4, 5, 8, 10, 15)


def bodies(payload):
    """Accept the tessellation response in its known shapes: a list of bodies, {bodies:[...]} or a wrapped {body:...}."""
    for _ in range(4):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            break
        payload = payload.get("bodies", payload.get("body", payload.get("data")))
    raise OnshapeError("Unexpected tessellation response shape; inspect it with api_read getPartStudioFaces.")


def xyz(point):
    return [point["x"], point["y"], point["z"]] if isinstance(point, dict) else list(point)


def triangles(part):
    tris = []
    for face in part.get("faces", []):
        for facet in face.get("facets", []):
            vertices = [[c * 1000.0 for c in xyz(p)] for p in facet.get("vertices", [])]
            if len(vertices) != 3:
                continue
            normal = xyz(facet["normal"]) if facet.get("normal") else [0.0, 0.0, 0.0]
            tris.append((normal, vertices))
    return tris


def stl_bytes(tris):
    out = bytearray(80) + struct.pack("<I", len(tris))
    for normal, v in tris:
        out += struct.pack("<12fH", *normal, *v[0], *v[1], *v[2], 0)
    return bytes(out)


def open_edges(tris, decimals=5):
    """Edges not shared by exactly two facets (vertices rounded to 1e-5 mm); 0 means watertight."""
    edges = collections.Counter()
    for _, v in tris:
        a, b, c = (tuple(round(x, decimals) for x in p) for p in v)
        for edge in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted(edge))] += 1
    return sum(1 for n in edges.values() if n != 2)


def bbox(tris):
    points = [p for _, v in tris for p in v]
    if not points:
        return None
    return [round(max(p[i] for p in points) - min(p[i] for p in points), 3) for i in range(3)]


def safe(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name or "part").strip("_") or "part"


def stl_files(payload, parts=None, include_ref=False, merge=False, prefix=""):
    """[(filename, bytes, stats)] from a tessellation response. parts filters by part id or name."""
    wanted = set(parts or [])
    chosen = []
    for part in bodies(payload):
        name = part.get("name") or part.get("id") or "part"
        if wanted and part.get("id") not in wanted and name not in wanted:
            continue
        if not wanted and name.startswith("REF ") and not include_ref:
            continue
        chosen.append((name, part.get("id"), triangles(part)))
    if not chosen:
        raise OnshapeError("No matching parts in the tessellation (REF parts are skipped unless include_ref=true).")
    if merge:
        chosen = [("+".join(n for n, _, _ in chosen), None, [t for _, _, tris in chosen for t in tris])]
    out, used = [], set()
    for name, part_id, tris in chosen:
        filename = prefix + (safe(name) if not merge else "merged") + ".stl"
        if filename.lower() in used:  # "Part 1" and "Part/1" (or two parts with one name) must not overwrite
            filename = prefix + safe(name) + "_" + safe(part_id or str(len(used))) + ".stl"
        used.add(filename.lower())
        out.append((filename, stl_bytes(tris), {"part": name, "id": part_id, "triangles": len(tris),
                                                "bbox_mm": bbox(tris), "open_edges": open_edges(tris)}))
    return out


async def tessellation(service, data, chord_mm, angle_deg):
    target = data["target"]
    path = service.pinned(data) if data.get("microversion") else target
    query = {"chordTolerance": chord_mm / 1000.0, "angleTolerance": math.radians(angle_deg), "outputFacetNormals": True}
    if data.get("configuration"):
        query["configuration"] = data["configuration"]
    return await service.call("getPartStudioFaces", path, query)


async def step(service, data, parts=None, timeout=180, sleep=asyncio.sleep):
    """Create a STEP translation, poll it with backoff and download it. Returns (bytes, info)."""
    target = data["target"]
    if target["wvm"] not in ("w", "v"):
        raise OnshapeError("STEP translations need a workspace or version URL.")
    body = {"formatName": "STEP", "storeInDocument": False}
    if parts:
        body["partIds"] = ",".join(parts)
    if data.get("configuration"):
        body["configuration"] = data["configuration"]
    info = await service.call("createPartStudioTranslation",
                              {"did": target["did"], "wv": target["wvm"], "wvid": target["wvmid"], "eid": target["eid"]}, body=body)
    requests, waited, i = 1, 0, 0
    while info.get("requestState") not in ("DONE", "FAILED"):
        if waited >= timeout:
            raise OnshapeError(f"Translation {info.get('id')} still running after {waited} s ({requests} requests). "
                               "Check it later with api_read getTranslation; do not start a new one.")
        delay = POLL_DELAYS[min(i, len(POLL_DELAYS) - 1)]
        i += 1
        await sleep(delay)
        waited += delay
        info = await service.call("getTranslation", {"tid": info["id"]})
        requests += 1
    if info["requestState"] == "FAILED":
        raise OnshapeError("Translation failed: " + str(info.get("failureReason") or "no reason given"))
    ids = info.get("resultExternalDataIds") or []
    if not ids:
        raise OnshapeError("Translation finished without a result file.")
    _, url = service.catalog.resolve("downloadExternalData", {"did": target["did"], "fid": ids[0]})
    raw = await service.client.request("GET", url, accept="application/octet-stream")
    requests += 1
    if isinstance(raw, str):
        raw = raw.encode()
    if not isinstance(raw, bytes):
        raise OnshapeError("Expected the STEP file, got a JSON response; inspect the translation with api_read getTranslation.")
    return raw, {"translation_id": info.get("id"), "requests": requests, "waited_s": waited}
