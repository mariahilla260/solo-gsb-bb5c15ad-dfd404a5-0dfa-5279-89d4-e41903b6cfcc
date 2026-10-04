"""车次探头故障登记 / 撤销 / 隔离 / 重算 / 留痕测试。"""
import pytest


@pytest.fixture()
def setup_trip(client, make_cargo):
    make_cargo("C1", limit="4.0", holder="A仓")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"})
    client.post(
        "/trips/T1/manifest",
        json={"items": [{
            "cargo_id": "C1",
            "loaded_at": "2026-10-04T08:00:00+08:00",
            "unloaded_at": "2026-10-04T20:00:00+08:00",
        }]},
    )


def _reading(client, rid, at, temp):
    r = client.post("/vehicle-readings", json={
        "reading_id": rid, "trip_id": "T1",
        "read_at": f"2026-10-04T{at}:00+08:00", "temperature": temp,
    })
    assert r.status_code == 201, r.text


def _fault(client, fid="pf-1", start="08:30", end="10:30", reason="探头漂移", status=201):
    r = client.post("/trips/T1/probe-faults", json={
        "fault_id": fid,
        "fault_start": f"2026-10-04T{start}:00+08:00",
        "fault_end": f"2026-10-04T{end}:00+08:00",
        "reason": reason,
    })
    assert r.status_code == status, r.text
    return r


# ----------------------------------------------------------- 隔离与重算


def test_fault_quarantines_coverred_reading_and_recomputes(client, setup_trip):
    _reading(client, "vr-1", "09", "8")   # 8 > 4 开启区间
    _reading(client, "vr-2", "11", "2")   # 闭合区间
    assert len(client.get("/cargo/C1/report").json()["excursions"]) == 1

    r = _fault(client)
    body = r.json()
    assert body["quarantined_reading_ids"] == ["vr-1"]
    assert body["affected_cargo_ids"] == ["C1"]
    assert body["fault"]["active"] is True
    assert body["fault"]["version"] == 0
    assert body["fault"]["revocation"] is None

    # 开启读数被隔离 -> 越温区间消失
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    assert client.get("/cargo/C1/responsibilities").json()["intervals"] == []

    # 时间线保留原始读数但标明隔离状态与覆盖故障
    tl = client.get("/cargo/C1/timeline").json()["points"]
    v1 = next(p for p in tl if p["reading_id"] == "vr-1")
    assert v1["adopted"] is False
    assert v1["quarantined"] is True
    assert v1["covering_fault_ids"] == ["pf-1"]
    # 温度取当前有效值（无更正值即原始值），原始读数照常可查
    assert v1["temperature"] == "8.000"
    # 未覆盖的读数照常采用
    v2 = next(p for p in tl if p["reading_id"] == "vr-2")
    assert v2["adopted"] is True and v2["quarantined"] is False

    # 车次详情：读数级别 + 适用性均标明隔离
    td = client.get("/trips/T1").json()
    r1 = next(x for x in td["readings"] if x["reading_id"] == "vr-1")
    assert r1["quarantined"] is True
    assert r1["covering_fault_ids"] == ["pf-1"]
    assert r1["temperature"] == "8.000"  # 原始值不变
    app = r1["applications"][0]
    assert app["cargo_id"] == "C1"
    assert app["on_board"] is True
    assert app["adopted"] is False
    assert app["quarantined"] is True
    assert app["reason"] == "quarantined"
    # over_limit 仍按有效温度给出（仅供查阅）
    assert app["over_limit"] is True

    # 故障列表留痕
    faults = client.get("/trips/T1/probe-faults").json()["faults"]
    assert [f["fault_id"] for f in faults] == ["pf-1"]
    assert faults[0]["fault_start"].endswith("Z")


def test_late_reading_inside_fault_window_is_quarantined(client, setup_trip):
    _fault(client)
    # 故障登记之后才补录的旧时刻（晚到）读数同样隔离
    _reading(client, "vr-late", "09", "9")
    p = next(
        p for p in client.get("/cargo/C1/timeline").json()["points"]
        if p["reading_id"] == "vr-late"
    )
    assert p["quarantined"] is True and p["adopted"] is False
    assert client.get("/cargo/C1/report").json()["excursions"] == []


def test_fault_window_is_half_open(client, setup_trip):
    """故障时段左闭右开：恰在起点纳入、恰在终点排除。"""
    _reading(client, "vr-start", "08:30", "8")
    _reading(client, "vr-end", "10:30", "8")
    _fault(client)  # [08:30, 10:30)
    tl = {p["reading_id"]: p for p in client.get("/cargo/C1/timeline").json()["points"]}
    assert tl["vr-start"]["quarantined"] is True
    assert tl["vr-end"]["quarantined"] is False
    assert tl["vr-end"]["adopted"] is True


def test_same_time_own_measurement_still_takes_priority(client, setup_trip):
    _fault(client)
    _reading(client, "vr-1", "09", "8")
    # 同刻自有测温（也超限）：自有优先开启区间，车载读数仍列隔离
    r = client.post("/events", json={
        "event_id": "e-1", "cargo_id": "C1",
        "occurred_at": "2026-10-04T09:00:00+08:00", "temperature": "9",
    })
    assert r.status_code == 201
    _reading(client, "vr-2", "11", "2")

    rep = client.get("/cargo/C1/report").json()["excursions"]
    assert len(rep) == 1
    assert rep[0]["interval_id"] == "e-1" and rep[0]["start_source"] == "own"

    td = client.get("/trips/T1").json()
    app = next(
        a for x in td["readings"] if x["reading_id"] == "vr-1" for a in x["applications"]
    )
    # 同时被故障覆盖且同刻有自有测温：首要原因标被自有取代，故障信息仍给出
    assert app["reason"] == "supplanted_by_own"
    assert app["quarantined"] is True
    assert app["covering_fault_ids"] == ["pf-1"]


def test_fault_does_not_affect_handovers(client, setup_trip):
    _reading(client, "vr-1", "09", "8")
    r = client.post("/events", json={
        "event_id": "h-1", "cargo_id": "C1",
        "occurred_at": "2026-10-04T09:30:00+08:00",
        "giver": "A仓", "receiver": "B仓",
    })
    assert r.status_code == 201
    _fault(client)
    # 交接照常改变保管方
    assert client.get("/cargo/C1/report").json()["current_holder"] == "B仓"


def test_reading_outside_loading_unaffected(client, make_cargo):
    """故障只隔离读数时刻在车的货物：不在车的读数适用性不变。"""
    make_cargo("C1", limit="4.0")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"})
    client.post(
        "/trips/T1/manifest",
        json={"items": [{
            "cargo_id": "C1",
            "loaded_at": "2026-10-04T12:00:00+08:00",
            "unloaded_at": "2026-10-04T20:00:00+08:00",
        }]},
    )
    _reading(client, "vr-1", "09", "8")  # 货物尚未装车
    _fault(client)
    td = client.get("/trips/T1").json()
    app = td["readings"][0]["applications"][0]
    assert app["on_board"] is False
    assert app["reason"] == "not_on_board"
    assert app["quarantined"] is False
    # 时间线不含无关读数
    assert client.get("/cargo/C1/timeline").json()["points"] == []


# ----------------------------------------------------------- 撤销与恢复


def test_revoke_restores_reading_by_current_effective_temperature(client, setup_trip):
    _reading(client, "vr-1", "09", "8")
    _reading(client, "vr-2", "11", "2")
    _fault(client)
    assert client.get("/cargo/C1/report").json()["excursions"] == []

    # 隔离期间把读数更正为不超限；撤销后按**当前有效温度**恢复（不越温）
    r = client.post("/corrections", json={
        "correction_id": "corr-1", "target_type": "vehicle_reading",
        "target_id": "vr-1", "expected_version": 0, "new_temperature": "2",
    })
    assert r.status_code == 201, r.text
    assert client.get("/cargo/C1/report").json()["excursions"] == []

    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    assert r.status_code == 201, r.text
    assert r.json()["restored_reading_ids"] == ["vr-1"]
    assert r.json()["fault"]["active"] is False
    assert r.json()["fault"]["version"] == 1
    assert r.json()["fault"]["revocation"]["revocation_id"] == "rv-1"
    # 按有效温度 2℃ 恢复 -> 仍无越温区间
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    p = next(
        p for p in client.get("/cargo/C1/timeline").json()["points"]
        if p["reading_id"] == "vr-1"
    )
    assert p["adopted"] is True and p["quarantined"] is False
    assert p["temperature"] == "2.000" and p["version"] == 1


def test_overlapping_faults_revoke_one_keeps_other_isolating(client, setup_trip):
    _reading(client, "vr-1", "09", "8")
    _reading(client, "vr-2", "11", "8")
    _fault(client, "pf-1", "08:30", "10:30", "f1")
    _fault(client, "pf-2", "09:00", "12:00", "f2")

    tl = {p["reading_id"]: p for p in client.get("/cargo/C1/timeline").json()["points"]}
    assert tl["vr-1"]["covering_fault_ids"] == ["pf-1", "pf-2"]
    assert tl["vr-2"]["covering_fault_ids"] == ["pf-2"]

    # 撤销 pf-2：vr-2 恢复，vr-1 仍被 pf-1 覆盖
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-2", "fault_id": "pf-2", "expected_version": 0,
    })
    assert r.status_code == 201
    assert r.json()["restored_reading_ids"] == ["vr-2"]
    tl = {p["reading_id"]: p for p in client.get("/cargo/C1/timeline").json()["points"]}
    assert tl["vr-1"]["quarantined"] is True
    assert tl["vr-1"]["covering_fault_ids"] == ["pf-1"]
    assert tl["vr-2"]["quarantined"] is False and tl["vr-2"]["adopted"] is True

    # 撤销最后一条覆盖故障 pf-1 -> vr-1 恢复
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    assert r.status_code == 201
    assert r.json()["restored_reading_ids"] == ["vr-1"]
    tl = {p["reading_id"]: p for p in client.get("/cargo/C1/timeline").json()["points"]}
    assert tl["vr-1"]["quarantined"] is False and tl["vr-1"]["adopted"] is True


def test_recompute_after_revoke_includes_simultaneous_excursions(
    client, make_cargo
):
    make_cargo("C1", limit="4.0")
    make_cargo("C2", limit="4.0")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"})
    client.post("/trips/T1/manifest", json={"items": [
        {"cargo_id": "C1", "loaded_at": "2026-10-04T08:00:00+08:00",
         "unloaded_at": "2026-10-04T20:00:00+08:00"},
        {"cargo_id": "C2", "loaded_at": "2026-10-04T08:00:00+08:00",
         "unloaded_at": "2026-10-04T20:00:00+08:00"},
    ]})
    _reading(client, "vr-1", "09", "8")
    _reading(client, "vr-2", "11", "2")
    params = {
        "start_at": "2026-10-04T00:00:00+08:00",
        "end_at": "2026-10-05T00:00:00+08:00",
    }
    sim = client.get("/trips/T1/simultaneous-excursions", params=params).json()
    assert len(sim["segments"]) == 1

    _fault(client)
    sim = client.get("/trips/T1/simultaneous-excursions", params=params).json()
    assert sim["segments"] == []

    client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    sim = client.get("/trips/T1/simultaneous-excursions", params=params).json()
    assert len(sim["segments"]) == 1
    assert sim["segments"][0]["cargo_ids"] == ["C1", "C2"]


# ----------------------------------------------------------- 留痕与证据


def test_registration_and_revocation_are_traced(client, setup_trip):
    _reading(client, "vr-1", "09", "8")
    _fault(client)
    client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    faults = client.get("/trips/T1/probe-faults").json()["faults"]
    assert len(faults) == 1
    f = faults[0]
    assert f["active"] is False and f["version"] == 1
    assert f["revocation"] == {
        "revocation_id": "rv-1",
        "fault_id": "pf-1",
        "trip_id": "T1",
        "expected_version": 0,
        "version": 1,
        "revoked_at": f["revocation"]["revoked_at"],
        "created_at": f["revocation"]["created_at"],
    }


def test_old_stance_history_and_sealed_package_unchanged_diff_reflects(
    client, setup_trip
):
    _reading(client, "vr-1", "09", "8")
    _reading(client, "vr-2", "11", "2")
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    iid, snap = iv["interval_id"], iv["snapshot_id"]
    client.post(
        f"/cargo/C1/responsibilities/{iid}/stances",
        json={"snapshot_id": snap, "responsible_party": "A仓",
              "stance": "confirm", "stance_id": "st-1"},
    )
    seal = client.post("/evidence-seals", json={
        "case_id": "CASE-1", "cargo_id": "C1",
        "interval_id": iid, "snapshot_id": snap,
    })
    assert seal.status_code == 201
    sealed_digest = seal.json()["content_digest"]
    sealed_readings = [
        x["reading_id"] for x in seal.json()["content"]["vehicle_readings"]
    ]
    assert "vr-1" in sealed_readings

    # 故障隔离开启读数 -> 区间消失；旧表态历史保留但失效
    _fault(client)
    hist = client.get(f"/cargo/C1/responsibilities/{iid}/stances").json()
    assert hist["interval_exists"] is False
    assert hist["history"][0]["is_current"] is False

    diff = client.get("/evidence-seals/CASE-1/diff").json()
    assert diff["status"] == "disappeared"
    vr1_change = next(
        x for x in diff["evidence_changes"]["vehicle_readings"]
        if x["key"] == "vr-1"
    )
    assert vr1_change["change"] == "removed"

    # 已封存原包保持原样
    got = client.get("/evidence-seals/CASE-1").json()
    assert got["content_digest"] == sealed_digest
    assert [x["reading_id"] for x in got["content"]["vehicle_readings"]] == sealed_readings

    # 撤销最后一条覆盖故障 -> 区间按相同片段恢复，旧表态重新有效
    client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    hist = client.get(f"/cargo/C1/responsibilities/{iid}/stances").json()
    assert hist["interval_exists"] is True and hist["is_open"] is False
    assert hist["history"][0]["is_current"] is True
    diff = client.get("/evidence-seals/CASE-1/diff").json()
    assert diff["status"] == "unchanged"


def test_original_reading_and_correction_history_still_available(client, setup_trip):
    _reading(client, "vr-1", "09", "8")
    client.post("/corrections", json={
        "correction_id": "corr-1", "target_type": "vehicle_reading",
        "target_id": "vr-1", "expected_version": 0, "new_temperature": "7",
    })
    _fault(client)
    h = client.get("/vehicle-readings/vr-1/corrections").json()
    assert h["original_temperature"] == "8.000"
    assert h["effective_temperature"] == "7.000"
    assert h["version"] == 1
    assert [x["correction_id"] for x in h["corrections"]] == ["corr-1"]


# ----------------------------------------------------------- 修订号


def test_fault_register_and_revoke_bump_trip_revision(client, setup_trip):
    rev0 = client.get("/trips/T1").json()["revision"]
    _fault(client)
    rev1 = client.get("/trips/T1").json()["revision"]
    assert rev1 == rev0 + 1
    client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    assert client.get("/trips/T1").json()["revision"] == rev1 + 1


def test_precheck_digest_invalidated_by_fault_registration(
    client, make_cargo
):
    """故障登记推进修订号 -> 旧预检摘要过期（既有预检语义不变）。"""
    make_cargo("C1", limit="4.0")
    make_cargo("C2", limit="4.0")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"})
    client.post(
        "/trips/T1/manifest",
        json={"items": [{
            "cargo_id": "C1",
            "loaded_at": "2026-10-04T08:00:00+08:00",
            "unloaded_at": "2026-10-04T12:00:00+08:00",
        }]},
    )
    rev = client.get("/trips/T1").json()["revision"]
    pre = client.post("/trips/T1/manifest/precheck", json={
        "expected_trip_revision": rev,
        "items": [{
            "cargo_id": "C2",
            "loaded_at": "2026-10-04T08:00:00+08:00",
            "unloaded_at": "2026-10-04T12:00:00+08:00",
        }],
    })
    assert pre.status_code == 200, pre.text
    digest = pre.json()["digest"]

    # 故障登记使车次修订号 +1，旧摘要过期
    _fault(client, "pf-1", "13:00", "14:00", "不影响拟登记时段但推进修订号")
    r = client.post("/trips/T1/manifest/confirm", json={
        "digest": digest,
        "items": [{
            "cargo_id": "C2",
            "loaded_at": "2026-10-04T08:00:00+08:00",
            "unloaded_at": "2026-10-04T12:00:00+08:00",
        }],
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"


# ----------------------------------------------------------- 幂等 / 冲突 / 拒绝


def test_fault_idempotent_replay_and_content_conflict(client, setup_trip):
    body = {
        "fault_id": "pf-1",
        "fault_start": "2026-10-04T08:30:00+08:00",
        "fault_end": "2026-10-04T10:30:00+08:00",
        "reason": "漂移",
    }
    assert client.post("/trips/T1/probe-faults", json=body).status_code == 201
    r = client.post("/trips/T1/probe-faults", json=body)
    assert r.status_code == 200 and r.json()["replayed"] is True

    changed = dict(body, fault_start="2026-10-04T08:00:00+08:00")
    r = client.post("/trips/T1/probe-faults", json=changed)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "fault_id_conflict"

    # 同编号故障挂到另一车次也按异内容冲突
    client.post("/trips", json={"trip_id": "T2", "vehicle_no": "v2"})
    r = client.post("/trips/T2/probe-faults", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "fault_id_conflict"


def test_fault_validation_rejections(client, setup_trip):
    def post(body, trip="T1"):
        return client.post(f"/trips/{trip}/probe-faults", json=body)

    base = {
        "fault_id": "pf-x",
        "fault_start": "2026-10-04T09:30:00+08:00",
        "fault_end": "2026-10-04T10:30:00+08:00",
        "reason": "r",
    }
    r = post(dict(base, fault_end="2026-10-04T09:30:00+08:00"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_fault_window"

    r = post(dict(base, fault_start="2026-10-04T09:30:00"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"

    r = post(dict(base, reason="   "))
    assert r.status_code == 422  # 原因为空（pydantic 形状校验，统一 422）

    r = post(base, trip="NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_revoke_rejections(client, setup_trip):
    _fault(client)

    # 版本过期
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-x", "fault_id": "pf-1", "expected_version": 5,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_fault_version"

    # 故障不存在 / 不属于该车次
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-y", "fault_id": "pf-nope", "expected_version": 0,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "probe_fault_not_found"
    client.post("/trips", json={"trip_id": "T2", "vehicle_no": "v2"})
    r = client.post("/trips/T2/probe-faults/revoke", json={
        "revocation_id": "rv-z", "fault_id": "pf-1", "expected_version": 0,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "probe_fault_not_found"

    # 成功撤销后再次撤销（版本已是 1）-> stale_fault_version
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0,
    })
    assert r.status_code == 201
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-2", "fault_id": "pf-1", "expected_version": 0,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_fault_version"

    # 未知车次
    r = client.post("/trips/NOPE/probe-faults/revoke", json={
        "revocation_id": "rv-3", "fault_id": "pf-1", "expected_version": 1,
    })
    # 故障先于车次校验被定位到（属于 T1），故仍返回 404
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "probe_fault_not_found"


def test_revoke_idempotent_replay_and_conflict(client, setup_trip):
    _reading(client, "vr-1", "09", "8")
    _fault(client)
    body = {"revocation_id": "rv-1", "fault_id": "pf-1", "expected_version": 0}
    assert client.post("/trips/T1/probe-faults/revoke", json=body).status_code == 201
    r = client.post("/trips/T1/probe-faults/revoke", json=body)
    assert r.status_code == 200 and r.json()["replayed"] is True
    assert r.json()["restored_reading_ids"] == ["vr-1"]

    changed = dict(body, expected_version=1)
    r = client.post("/trips/T1/probe-faults/revoke", json=changed)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "revocation_id_conflict"


def test_failure_leaves_no_partial_write_and_id_reusable(client, setup_trip):
    """非法时段 / 版本过期等失败不留部分写入，编号可被后续合法请求复用。"""
    r = client.post("/trips/T1/probe-faults", json={
        "fault_id": "pf-bad",
        "fault_start": "2026-10-04T12:00:00+08:00",
        "fault_end": "2026-10-04T11:00:00+08:00",
        "reason": "r",
    })
    assert r.status_code == 422
    rev_after_fail = client.get("/trips/T1").json()["revision"]

    # 同一编号可用于合法请求
    r = client.post("/trips/T1/probe-faults", json={
        "fault_id": "pf-bad",
        "fault_start": "2026-10-04T08:30:00+08:00",
        "fault_end": "2026-10-04T10:30:00+08:00",
        "reason": "r",
    })
    assert r.status_code == 201
    assert client.get("/trips/T1").json()["revision"] == rev_after_fail + 1

    # 撤销失败（版本过期）不留撤销行，撤销编号可复用
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-bad", "fault_id": "pf-bad", "expected_version": 9,
    })
    assert r.status_code == 409
    r = client.post("/trips/T1/probe-faults/revoke", json={
        "revocation_id": "rv-bad", "fault_id": "pf-bad", "expected_version": 0,
    })
    assert r.status_code == 201


def test_fault_listing_unknown_trip_404(client):
    r = client.get("/trips/NOPE/probe-faults")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_fault_without_onboard_cargo_quarantines_readings_still(client, make_cargo):
    """故障时段无在车货物时登记仍成功：时段内读数对该车次标记隔离，
    之后晚到的装卸 / 读数重建即时生效。"""
    make_cargo("C1", limit="4.0")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"})
    r = _fault(client)
    assert r.json()["affected_cargo_ids"] == []
    assert r.json()["quarantined_reading_ids"] == []

    _reading(client, "vr-1", "09", "8")  # 晚到读数，时段内无货物
    # 读数本身仍被车次级故障覆盖
    td = client.get("/trips/T1").json()
    assert td["readings"][0]["quarantined"] is True
    # 货物时间线无点（读数时刻不在车）
    assert client.get("/cargo/C1/timeline").json()["points"] == []
