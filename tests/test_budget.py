"""Request-budget behaviour: ledger/guard, immutable cache, write-derived snapshots, tab freshness, Feature Studio patch."""
import json
import time

import httpx
import pytest

from onshape_native.client import Client, Ledger, OnshapeError, Settings, family
from onshape_native.service import Service

DID, WID, EID, MV1, MV2 = "a" * 24, "b" * 24, "c" * 24, "1" * 24, "2" * 24
URL = f"https://cad.onshape.com/documents/{DID}/w/{WID}/e/{EID}"


def bridge(tmp_path, monkeypatch, reply):
    """Client whose local bridge is replaced by reply(job) -> dict; returns (client, jobs)."""
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    jobs = []

    def handler(request):
        job = json.loads(request.content)
        jobs.append(job)
        return httpx.Response(200, json=reply(job))
    return Client(Settings(cache_dir=tmp_path / "runtime" / "artifacts"), httpx.MockTransport(handler)), jobs


def test_family_groups_part_studio_endpoints():
    assert family("/api/v10/partstudios/d/x/w/y/e/z/features") == "partstudios"
    assert family("/api/partstudios/d/x/w/y/e/z/featurescript") == "partstudios"
    assert family("/api/v10/featurestudios/d/x") == "featurestudios"


def test_ledger_reads_onshape_header_and_blocks_on_retry_after(tmp_path):
    ledger = Ledger(tmp_path / "ratelimit.json")
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "41"}, now=1000)
    assert ledger.entry("partstudios")["remaining"] == 41
    ledger.record("partstudios", 429, {"retry-after": "600"}, now=1000)
    assert ledger.blocked("partstudios", now=1599) == 1600 and not ledger.blocked("partstudios", now=1601)
    assert Ledger(tmp_path / "ratelimit.json").entry("partstudios")["blocked_until"] == 1600  # survives a restart


async def test_guard_refuses_locally_while_blocked(tmp_path, monkeypatch):
    client, jobs = bridge(tmp_path, monkeypatch, lambda job: {"status": 429, "response_headers": {"retry-after": "300"}, "body": {}})
    with pytest.raises(OnshapeError, match="blocked until"):
        await client.request("GET", f"/api/v10/partstudios/d/{DID}/w/{WID}/e/{EID}/features")
    with pytest.raises(OnshapeError, match="Nothing was sent"):
        await client.request("GET", f"/api/v10/partstudios/d/{DID}/w/{WID}/e/{EID}/features")
    assert len(jobs) == 1
    assert client.ledger.summary()["partstudios"]["blocked_for_s"] > 290


async def test_immutable_reads_and_pinned_evaluates_are_cached(tmp_path, monkeypatch):
    client, jobs = bridge(tmp_path, monkeypatch, lambda job: {"status": 200, "body": {"result": len(jobs)}})
    pinned = f"/api/v10/partstudios/d/{DID}/m/{MV1}/e/{EID}/featurescript"
    first = await client.request("POST", pinned, {}, {"script": "s"}, read_only=True)
    again = await client.request("POST", pinned, {}, {"script": "s"}, read_only=True)
    assert first == again and len(jobs) == 1
    await client.request("POST", pinned, {}, {"script": "other"}, read_only=True)
    workspace = f"/api/v10/partstudios/d/{DID}/w/{WID}/e/{EID}/features"
    await client.request("GET", workspace); await client.request("GET", workspace)
    assert len(jobs) == 4  # different script + two uncached workspace reads
    assert client.ledger.summary()["partstudios"]["cache_hits"] == 1


def features(micro, extra=()):
    return {"sourceMicroversion": micro, "libraryVersion": 3095, "serializationVersion": "1.2.23",
            "features": [{"featureId": "F1", "name": "Sketch", "featureType": "newSketch", "parameters": []}, *extra],
            "featureStates": {"F1": {"featureStatus": "OK"}}}


async def test_feature_add_returns_fresh_snapshot_without_inspect(tmp_path, monkeypatch):
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    s = Service(Settings(cache_dir=tmp_path / "artifacts"), httpx.MockTransport(lambda r: httpx.Response(500)))
    snap = s.snapshot_from_features(URL, features(MV1))
    added = {"featureId": "F2", "name": "Box", "featureType": "extrude", "parameters": []}
    calls = []

    async def call(operation, path=None, query=None, body=None):
        calls.append(operation)
        return {"feature": added, "featureState": {"featureStatus": "OK"}, "sourceMicroversion": MV2, "libraryVersion": 3095}
    s.call = call
    out = await s.feature(snap, "add", [{"featureType": "extrude", "name": "Box", "parameters": []}])
    assert calls == ["addPartStudioFeature"]
    data = s.snapshot_data(out["snapshot"])
    assert data["microversion"] == MV2 and [f["id"] for f in data["features"]] == ["F1", "F2"]
    assert data["features"][1]["status"] == "OK" and s.latest[(DID, WID)] == MV2


async def test_inspect_unchanged_costs_no_request_when_tab_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    s = Service(Settings(cache_dir=tmp_path / "artifacts"), httpx.MockTransport(lambda r: httpx.Response(500)))
    snap = s.snapshot_from_features(URL, features(MV1))
    rest = []

    async def command(job):
        if job["kind"] == "microversion":
            return {"microversion": MV1}
        rest.append(job)
        return {"status": 200, "body": features(MV2)}
    s.client.command = command
    out = await s.inspect(URL, geometry=False, previous=snap)
    assert out["unchanged"] and out["api_calls"] == 0 and rest == []
    s.remember(DID, WID, MV2)          # our own write is newer than the tab: do not trust the tab
    out = await s.inspect(URL, geometry=False, previous=snap)
    assert len(rest) == 1 and out["microversion"] == MV2


async def test_featurestudio_patch_requires_unique_matches(tmp_path, monkeypatch):
    from onshape_native import server
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    s = Service(Settings(cache_dir=tmp_path / "artifacts"), httpx.MockTransport(lambda r: httpx.Response(500)))
    sent = []

    async def request(method, path, query=None, body=None, **kw):
        if method == "POST":
            sent.append(body)
            return {"sourceMicroversion": MV2, "contents": body["contents"]}
        if path.endswith("/featurespecs"):
            return {"featureSpecs": [{"featureType": "boxT2", "featureTypeName": "Box", "namespace": "e::m"}]}
        return {"contents": "a = 1;\nb = 1;\n", "sourceMicroversion": MV1, "serializationVersion": "1.2.23"}
    s.client.request = request
    monkeypatch.setattr(server, "service", s)
    with pytest.raises(OnshapeError, match="matches 2 times"):
        await server.featurestudio_patch(URL, [{"old": "1;", "new": "2;"}])
    assert sent == []
    out = json.loads(await server.featurestudio_patch(URL, [{"old": "a = 1;", "new": "a = 2;"}]))
    assert sent[0]["contents"] == "a = 2;\nb = 1;\n" and sent[0]["sourceMicroversion"] == MV1
    assert out["feature_types"][0]["featureType"] == "boxT2" and s.latest[(DID, WID)] == MV2


def test_body_files_only_from_allowed_folders(tmp_path, monkeypatch):
    from onshape_native import server
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    allowed = tmp_path / "allowed"; allowed.mkdir()
    (allowed / "body.json").write_text('{"x": 1}')
    (tmp_path / "secret.json").write_text('{"x": 2}')
    monkeypatch.setenv("ONSHAPE_NATIVE_BODY_ROOTS", str(allowed))
    assert server.json_file(str(allowed / "body.json")) == {"x": 1}
    with pytest.raises(OnshapeError, match="outside the allowed"):
        server.json_file(str(tmp_path / "secret.json"))
