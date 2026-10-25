"""Round-2 budget features: endpoint ledger, reserve guard, burst pacing, ETag revalidation, batched edits,
report, feature-spec cache, export."""
import json

import httpx
import pytest

from onshape_native.client import Client, Ledger, OnshapeError, Settings, endpoint
from onshape_native.geometry import report_script
from onshape_native.service import Service

DID, WID, EID, MV1, MV2, MV3 = "a" * 24, "b" * 24, "c" * 24, "1" * 24, "2" * 24, "3" * 24
URL = f"https://cad.onshape.com/documents/{DID}/w/{WID}/e/{EID}"
FEATURES = f"/api/v10/partstudios/d/{DID}/w/{WID}/e/{EID}/features"


def bridge(tmp_path, monkeypatch, reply):
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    jobs = []

    def handler(request):
        job = json.loads(request.content)
        jobs.append(job)
        return httpx.Response(200, json=reply(job))
    return Client(Settings(cache_dir=tmp_path / "runtime" / "artifacts"), httpx.MockTransport(handler)), jobs


def service(tmp_path, monkeypatch):
    monkeypatch.setenv("ONSHAPE_NATIVE_RUNTIME", str(tmp_path / "runtime"))
    return Service(Settings(cache_dir=tmp_path / "artifacts"), httpx.MockTransport(lambda r: httpx.Response(500)))


def test_endpoint_keys_drop_ids():
    assert endpoint("GET", FEATURES) == "GET partstudios/features"
    assert endpoint("POST", f"/api/v10/partstudios/d/{DID}/m/{MV1}/e/{EID}/featurescript") == "POST partstudios/featurescript"
    assert endpoint("POST", f"/api/partstudios/d/{DID}/w/{WID}/e/{EID}/features/featureid/F1x_0") == "POST partstudios/features/featureid"
    assert endpoint("GET", f"/api/v10/translations/{MV1}") == "GET translations"


def test_ledger_learns_allowance_resets_and_low_hints(tmp_path):
    ledger = Ledger(tmp_path / "r.json")
    key = "GET partstudios/features"
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "250"}, now=1000, key=key)
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "12"}, now=1100, key=key)
    assert ledger.hints(now=1100)[key] == {"left": 12, "of": 251}
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "249"}, now=5000, key=key)  # counter went up
    assert ledger.data["partstudios"]["resets_seen"] == [5000] and ledger.hints(now=5000) == {}
    ledger.record("partstudios", 429, {"Retry-After": "600"}, now=6000, key=key)
    assert ledger.current("partstudios", key, now=6001)[:2] == (0, 6600)
    assert ledger.current("partstudios", key, now=6601) is None       # window has reset since
    assert ledger.current("partstudios", "POST partstudios/featurescript", now=6001)[0] == 0  # family fallback
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "3"}, now=6700, key=key)
    assert ledger.current("partstudios", key, now=6701)[0] == 3 and key in ledger.hints(now=6701)  # post-reset reading counts
    other = "POST partstudios/featurescript"
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "100"}, now=6800, key=other)
    ledger.record("partstudios", 200, {"X-Rate-Limit-Remaining": "299"}, now=7000, key=other)       # rises: a reset
    assert ledger.current("partstudios", key, now=7001) is None      # the low reading predates that reset


async def test_reserve_refuses_locally_and_can_be_spent(tmp_path, monkeypatch):
    client, jobs = bridge(tmp_path, monkeypatch, lambda job: {"status": 200, "body": {}, "response_headers": {"x-rate-limit-remaining": "3"}})
    await client.request("GET", FEATURES)
    with pytest.raises(OnshapeError, match="3 requests left .*Nothing was sent"):
        await client.request("GET", FEATURES)
    assert len(jobs) == 1
    client.set_pacing({"reserve": 0})
    await client.request("GET", FEATURES)
    assert len(jobs) == 2
    assert not (tmp_path / "runtime" / "pacing.json").exists()        # tool changes do not survive a restart
    with pytest.raises(OnshapeError, match="pacing keys"):
        client.set_pacing({"bogus": 1})
    with pytest.raises(OnshapeError, match="Nothing was changed"):
        client.set_pacing({"reserve": 9, "burst": 0})                  # all-or-nothing
    assert client.pacing["reserve"] == 0


async def test_burst_limit_refuses_runaway_loops(tmp_path, monkeypatch):
    client, jobs = bridge(tmp_path, monkeypatch, lambda job: {"status": 200, "body": {}})
    client.set_pacing({"burst": 2, "refill_per_min": 0.1, "max_wait_s": 0})
    await client.request("GET", FEATURES)
    await client.request("GET", FEATURES)
    with pytest.raises(OnshapeError, match="burst limit"):
        await client.request("GET", FEATURES)
    assert len(jobs) == 2


async def test_mutable_reads_revalidate_with_etag(tmp_path, monkeypatch):
    def reply(job):
        if job.get("request_headers", {}).get("If-None-Match") == '"v1"':
            return {"status": 304, "body": None, "response_headers": {"etag": '"v1"'}}
        return {"status": 200, "body": {"features": [1]}, "response_headers": {"etag": '"v1"'}}
    client, jobs = bridge(tmp_path, monkeypatch, reply)
    assert await client.request("GET", FEATURES) == {"features": [1]}
    assert await client.request("GET", FEATURES) == {"features": [1]}
    assert jobs[1]["request_headers"] == {"If-None-Match": '"v1"'}
    assert client.ledger.summary()["partstudios"]["revalidated_304"] == 1


def raw_features():
    return {"sourceMicroversion": MV1, "libraryVersion": 3095, "serializationVersion": "1.2.23",
            "features": [{"featureId": "F1", "name": "Sketch", "featureType": "newSketch", "btType": "BTMFeature-134",
                          "parameters": [{"parameterId": "sketchPlane", "queries": []}]},
                         {"featureId": "F2", "name": "Pad", "featureType": "extrude", "btType": "BTMFeature-134",
                          "parameters": [{"parameterId": "depth", "expression": "5 mm"}, {"parameterId": "other", "expression": "1"}]},
                         {"featureId": "F3", "name": "Wall", "featureType": "extrude", "btType": "BTMFeature-134",
                          "parameters": [{"parameterId": "depth", "expression": "2 mm"}]},
                         {"featureId": "F4", "name": "Fillet", "featureType": "fillet", "btType": "BTMFeature-134", "parameters": []}],
            "featureStates": {f: {"featureStatus": "OK"} for f in ("F1", "F2", "F3", "F4")}}


async def test_several_edits_go_out_as_one_update_features_request(tmp_path, monkeypatch):
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())
    calls = []

    async def call(operation, path=None, query=None, body=None):
        calls.append((operation, body))
        return {"features": [{"featureId": "F2", "parameters": body["features"][0]["parameters"]}],
                "featureStates": {"F2": {"featureStatus": "OK"}, "F3": {"featureStatus": "OK"}}, "sourceMicroversion": MV2}
    s.call = call
    out = await s.feature(snap, "edit", [{"id": "F2", "parameters": {"depth": "9 mm"}}, {"id": "F3", "parameters": {"depth": "3 mm"}}])
    assert [c[0] for c in calls] == ["updateFeatures"] and out["requests"] == 1 and out["batched"]
    sent = calls[0][1]
    assert sent["features"][0] == {"btType": "BTMFeature-134", "featureId": "F2", "parameters": [{"parameterId": "depth", "expression": "9 mm"}]}
    assert sent["sourceMicroversion"] == MV1 and sent["rejectMicroversionSkew"] is True and "updateSuppressionAttributes" not in sent
    data = s.snapshot_data(out["snapshot"])
    rows = {f["id"]: f for f in data["features"]}
    assert data["microversion"] == MV2 and rows["F2"]["parameters"] == {"depth": "9 mm", "other": "1"}  # full definition kept
    assert rows["F3"]["parameters"]["depth"] == "3 mm" and rows["F4"]["status"] == "UNVERIFIED" and rows["F1"]["status"] == "OK"
    assert s.latest[(DID, WID)] == MV2


async def test_batch_falls_back_to_sequential_when_onshape_rejects_the_shape(tmp_path, monkeypatch):
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())
    calls = []

    async def call(operation, path=None, query=None, body=None):
        calls.append(operation)
        if operation == "updateFeatures":
            raise OnshapeError("Onshape browser HTTP 400.", 400)
        micro = MV2 if len(calls) == 2 else MV3
        return {"feature": body["feature"], "featureState": {"featureStatus": "OK"}, "sourceMicroversion": micro}
    s.call = call
    out = await s.feature(snap, "edit", [{"id": "F2", "suppressed": True}, {"id": "F3", "parameters": {"depth": "3 mm"}}])
    assert calls == ["updateFeatures", "updatePartStudioFeature", "updatePartStudioFeature"]
    assert out["microversion"] == MV3 and s.batch_fallbacks == 1


async def test_report_after_write_replaces_inspect(tmp_path, monkeypatch):
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())

    async def call(operation, path=None, query=None, body=None):
        if operation == "evalFeatureScript":
            assert "getFeatureError" in body["script"] and '"checksText"' in body["script"] and path["wvmid"] == MV2
            return {"result": {"typeName": "BTFSValueMap", "message": {"value": [
                {"message": {"key": {"message": {"value": "problems"}, "typeName": "BTFSValueString"},
                             "value": {"typeName": "BTFSValueMap", "message": {"value": [
                                 {"message": {"key": {"message": {"value": "F4"}, "typeName": "BTFSValueString"},
                                              "value": {"typeName": "BTFSValueMap", "message": {"value": [
                                                  {"message": {"key": {"message": {"value": "status"}, "typeName": "BTFSValueString"},
                                                               "value": {"message": {"value": "ERROR"}, "typeName": "BTFSValueString"}}}]}}}}]}}}},
                {"message": {"key": {"message": {"value": "solids"}, "typeName": "BTFSValueString"},
                             "value": {"typeName": "BTFSValueArray", "message": {"value": []}}}},
                {"message": {"key": {"message": {"value": "checked"}, "typeName": "BTFSValueString"},
                             "value": {"message": {"value": 4}, "typeName": "BTFSValueNumber"}}}]}}}
        return {"feature": body["feature"], "featureState": {"featureStatus": "OK"}, "sourceMicroversion": MV2}
    s.call = call
    out = await s.feature(snap, "edit", [{"id": "F2", "parameters": {"depth": "9 mm"}}], report=True)
    assert out["requests"] == 2 and out["report"]["feature_problems"][0]["id"] == "F4"
    rows = {f["id"]: f["status"] for f in s.snapshot_data(out["snapshot"])["features"]}
    assert rows == {"F1": "OK", "F2": "OK", "F3": "OK", "F4": "ERROR"}


def test_report_script_validates_variable_names():
    assert '"checks"' in report_script(["F1"], ["checks"])
    with pytest.raises(OnshapeError):
        report_script(["F1"], ["bad name"])


async def test_feature_specs_survive_microversion_changes(tmp_path, monkeypatch):
    from onshape_native.templates import discover
    s = service(tmp_path, monkeypatch)
    calls, paths = [], []
    spec = {"featureType": "extrude", "featureTypeName": "Extrude", "namespace": "", "parameters": []}

    async def call(operation, path=None, query=None, body=None):
        calls.append(operation)
        return {"libraryVersion": 3095, "featureSpecs": [spec]}

    async def request(method, path, query=None, body=None, **kw):
        paths.append(path)
        return {"featureSpecs": [{"featureType": "boxT2", "featureTypeName": "Box", "namespace": f"e{EID}::m{MV3}", "parameters": []}]}
    s.call, s.client.request = call, request
    first = await discover(s, s.snapshot_from_features(URL, raw_features()), "extrude", "")
    edited = dict(raw_features(), sourceMicroversion=MV2)
    again = await discover(s, s.snapshot_from_features(URL, edited), "extrude", "")
    assert calls == ["getPartStudioFeatureSpecs"] and first["api_calls"] == 1 and again["api_calls"] == 0
    custom = await discover(s, s.snapshot_from_features(URL, edited), "boxT2", f"e{EID}::m{MV3}")
    assert paths == [f"/api/v10/featurestudios/d/{DID}/w/{WID}/e/{EID}/featurespecs"] and custom["source"] == "feature studio"
    listed = await discover(s, s.snapshot_from_features(URL, edited))
    cached = await discover(s, s.snapshot_from_features(URL, dict(edited, sourceMicroversion=MV3)))
    assert cached["cached"] and calls == ["getPartStudioFeatureSpecs"] * 2 and listed["features"][0]["feature_type"] == "extrude"


def tetra(name, drop_face=False):
    p = [{"x": 0, "y": 0, "z": 0}, {"x": 0.01, "y": 0, "z": 0}, {"x": 0, "y": 0.01, "z": 0}, {"x": 0, "y": 0, "z": 0.01}]
    faces = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)][: 3 if drop_face else 4]
    return {"id": name[:3].upper(), "name": name,
            "faces": [{"facets": [{"vertices": [p[i] for i in f], "normal": {"x": 0, "y": 0, "z": 1}}]} for f in faces]}


def test_stl_conversion_checks_watertightness_and_skips_ref_parts():
    from onshape_native.export import stl_files
    files = stl_files({"bodies": [tetra("Shell"), tetra("REF board"), tetra("Leaky", drop_face=True)]})
    stats = {f[2]["part"]: f[2] for f in files}
    assert set(stats) == {"Shell", "Leaky"} and stats["Shell"]["open_edges"] == 0 and stats["Leaky"]["open_edges"] == 3
    assert stats["Shell"]["bbox_mm"] == [10.0, 10.0, 10.0] and len(files[0][1]) == 84 + 50 * 4
    merged = stl_files([tetra("A"), tetra("B")], merge=True)
    assert len(merged) == 1 and merged[0][2]["triangles"] == 8


async def test_step_export_polls_here_with_backoff(tmp_path, monkeypatch):
    from onshape_native.export import step
    s = service(tmp_path, monkeypatch)
    data = s.snapshot_data(s.snapshot_from_features(URL, raw_features()))
    states, calls, slept = iter(["ACTIVE", "ACTIVE", "DONE"]), [], []

    async def call(operation, path=None, query=None, body=None):
        calls.append(operation)
        if operation == "createPartStudioTranslation":
            assert body == {"formatName": "STEP", "storeInDocument": False, "partIds": "JHD,JHK"}
        state = next(states)
        return {"id": MV3, "requestState": state, "resultExternalDataIds": ["f" * 24] if state == "DONE" else []}

    async def request(method, path, query=None, body=None, accept=None, **kw):
        assert accept == "application/octet-stream" and path.endswith("/externaldata/" + "f" * 24)
        return b"ISO-10303-21;"

    async def sleep(seconds):
        slept.append(seconds)
    s.call, s.client.request = call, request
    raw, info = await step(s, data, ["JHD", "JHK"], sleep=sleep)
    assert raw == b"ISO-10303-21;" and info["requests"] == 4 and slept == [3, 3]
    assert calls == ["createPartStudioTranslation", "getTranslation", "getTranslation"]


async def test_export_tool_writes_stl_only_to_allowed_folders(tmp_path, monkeypatch):
    from onshape_native import server
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())
    seen = []

    async def call(operation, path=None, query=None, body=None):
        seen.append((operation, path, query))
        return [tetra("Shell")]
    s.call = call
    monkeypatch.setattr(server, "service", s)
    allowed = tmp_path / "allowed"; allowed.mkdir()
    monkeypatch.setenv("ONSHAPE_NATIVE_BODY_ROOTS", str(allowed))
    out = json.loads(await server.export(snap, name="T2", out_dir=str(allowed)))
    assert out["files"][0]["path"].endswith("T2_Shell.stl") and (allowed / "T2_Shell.stl").stat().st_size == 284
    assert seen[0][0] == "getPartStudioFaces" and seen[0][1]["wvmid"] == MV1 and seen[0][2]["chordTolerance"] == pytest.approx(1.5e-5)
    with pytest.raises(OnshapeError, match="outside the allowed"):
        await server.export(snap, out_dir=str(tmp_path / "elsewhere"))


async def test_evaluate_report_preset_returns_updated_snapshot(tmp_path, monkeypatch):
    from onshape_native import server
    s = service(tmp_path, monkeypatch)
    raw = raw_features(); raw["featureStates"]["F4"] = {"featureStatus": "UNVERIFIED"}
    snap = s.snapshot_from_features(URL, raw)

    results = iter([{}, {"problems": {}, "checked": 4}])

    async def call(operation, path=None, query=None, body=None):
        return {"result": fs(next(results))}
    s.call = call
    monkeypatch.setattr(server, "service", s)
    snapshot_of = lambda out: s.snapshot_data(out["data"]["snapshot"] if "data" in out else out["snapshot"])
    data = snapshot_of(json.loads(await server.evaluate(snap, preset="report")))
    assert data["features"][3]["status"] == "UNVERIFIED" and "incomplete" in data["states_source"]  # empty result proves nothing
    data = snapshot_of(json.loads(await server.evaluate(snap, preset="report")))
    assert [f["status"] for f in data["features"]] == ["OK"] * 4 and data["states_source"].endswith("report")


def fs(value):
    """Encode a Python value the way Onshape returns FeatureScript values."""
    if isinstance(value, dict):
        return {"typeName": "BTFSValueMap", "message": {"value": [
            {"message": {"key": fs(k), "value": fs(v)}} for k, v in value.items()]}}
    if isinstance(value, list):
        return {"typeName": "BTFSValueArray", "message": {"value": [fs(v) for v in value]}}
    kind = "BTFSValueString" if isinstance(value, str) else "BTFSValueNumber"
    return {"typeName": kind, "message": {"value": value}}


async def test_report_failure_never_hides_a_successful_write(tmp_path, monkeypatch):
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())

    async def call(operation, path=None, query=None, body=None):
        if operation == "evalFeatureScript":
            raise OnshapeError("Onshape budget: 3 requests left ... Nothing was sent.", 429)
        return {"feature": body["feature"], "featureState": {"featureStatus": "OK"}, "sourceMicroversion": MV2}
    s.call = call
    out = await s.feature(snap, "edit", [{"id": "F2", "parameters": {"depth": "9 mm"}}], report=True)
    assert out["completed"][0]["id"] == "F2" and out["snapshot"] and "Nothing was sent" in out["report_error"]
    assert "Do NOT repeat the write" in out["next"]
    with pytest.raises(OnshapeError):   # bad variable names are rejected before anything is written
        await s.feature(snap, "edit", [{"id": "F2", "parameters": {"depth": "1 mm"}}], report=True, variables=["no spaces"])


async def test_sketches_and_suppression_in_batches(tmp_path, monkeypatch):
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())
    calls = []

    async def call(operation, path=None, query=None, body=None):
        calls.append((operation, body))
        if operation == "getPartStudioFeatures":
            return {"features": [raw_features()["features"][0]]}
        if operation == "updateFeatures":
            return {"features": [], "featureStates": {}, "sourceMicroversion": MV2}
        return {"feature": body["feature"], "featureState": {"featureStatus": "OK"}, "sourceMicroversion": MV3}
    s.call = call
    await s.feature(snap, "edit", [{"id": "F1", "parameters": {"sketchPlane": {"queries": []}}}, {"id": "F2", "parameters": {"depth": "1 mm"}}])
    assert [c[0] for c in calls] == ["getPartStudioFeatures", "updatePartStudioFeature", "updatePartStudioFeature"]  # sketch: sequential, full definition
    calls.clear()
    await s.feature(snap, "edit", [{"id": "F2", "suppressed": True}, {"id": "F3", "parameters": {"depth": "4 mm"}}])
    body = calls[0][1]
    assert calls[0][0] == "updateFeatures" and body["updateSuppressionAttributes"] is True
    assert [f["suppressed"] for f in body["features"]] == [True, False]
    with pytest.raises(OnshapeError, match="true or false"):
        await s.feature(snap, "edit", [{"id": "F2", "suppressed": "false"}, {"id": "F3", "parameters": {"depth": "4 mm"}}])


async def test_unknown_write_outcome_disables_the_tab_shortcut(tmp_path, monkeypatch):
    s = service(tmp_path, monkeypatch)
    snap = s.snapshot_from_features(URL, raw_features())

    async def call(operation, path=None, query=None, body=None):
        raise OnshapeError("Local bridge unavailable or command outcome unknown.")
    s.call = call
    out = await s.feature(snap, "add", [{"featureType": "extrude", "name": "Box", "parameters": []}])
    assert "outcome is unknown" in out["next"] and s.latest[(DID, WID)] == "UNKNOWN"
    rest = []

    async def command(job):
        if job["kind"] == "microversion":
            return {"microversion": MV1}
        rest.append(job)
        return {"status": 200, "body": raw_features()}
    s.client.command = command
    del s.call                                              # back to the real REST path
    out = await s.inspect(URL, geometry=False, previous=snap)
    assert len(rest) == 1 and s.latest[(DID, WID)] == MV1   # re-read over REST, then trusted again


def test_duplicate_part_names_get_distinct_files():
    from onshape_native.export import stl_files
    names = [f[0] for f in stl_files([tetra("Part 1"), dict(tetra("Part/1"), id="JHK")])]
    assert names == ["Part_1.stl", "Part_1_JHK.stl"]
