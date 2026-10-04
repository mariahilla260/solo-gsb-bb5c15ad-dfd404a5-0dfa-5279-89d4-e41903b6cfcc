"""车次探头故障登记 / 撤销的端到端测试。

覆盖：
* 登记留痕（状态/版本/原因）、左闭右开、带时区强约束、非法时段/原因拒绝；
* 生效故障覆盖的车载读数隔离（不参与越温重建；原始读数、温度更正历史仍可查；
  时间线/车次详情标明隔离状态与覆盖故障）；晚到读数也按生效故障隔离；
* 故障时段允许重叠；撤销一条后另一条覆盖的读数仍隔离；撤销最后一条后按
  当前有效温度恢复采用；同刻自有测温仍优先，交接不受影响；
* 越温报告、责任确认、同时越温随登记/撤销重算；表态历史保留；
  已封存证据包原样不变、封存现况对照反映隔离变化；
* 撤销乐观版本（生效为 0；已撤销/期望过期 -> stale_fault_version）；
* 编号同请求重传幂等、异内容冲突；未知车次/故障 404；失败不留部分写入；
* 成功推进车次修订号，使旧装卸预检摘要失效；查询接口（车次列表/单条）。
"""
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app


def _setup(client):
    """两票货物 + 一个车次 + 同窗装卸 [09:00,18:00)。"""
    for cid, limit in (("PF1", "4.0"), ("PF2", "8.0")):
        r = client.post(
            "/cargo",
            json={
                "cargo_id": cid,
                "started_at": "2026-09-30T08:00:00+08:00",
                "temp_upper_limit": limit,
                "initial_holder": "北京冷库",
            },
        )
        assert r.status_code == 201, r.text
    r = client.post(
        "/trips",
        json={"trip_id": "PTRIP", "vehicle_no": "京B-冷001", "note": "探头失准测试"},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/trips/PTRIP/manifest",
        json={
            "items": [
                {
                    "cargo_id": cid,
                    "loaded_at": "2026-09-30T09:00:00+08:00",
                    "unloaded_at": "2026-09-30T18:00:00+08:00",
                }
                for cid in ("PF1", "PF2")
            ]
        },
    )
    assert r.status_code == 201, r.text


def _reading(client, rid, at, temp):
    r = client.post(
        "/vehicle-readings",
        json={
            "reading_id": rid,
            "trip_id": "PTRIP",
            "read_at": f"2026-09-30T{at}+08:00",
            "temperature": temp,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r


def _fault(client, fid, start, end, reason="探头标定失准，读数偏高", status=201,
           trip="PTRIP"):
    return client.post(
        f"/trips/{trip}/probe-faults",
        json={
            "fault_id": fid,
            "fault_start_at": f"2026-09-30T{start}+08:00",
            "fault_end_at": f"2026-09-30T{end}+08:00",
            "reason": reason,
        },
    )


def _excursion_ids(client, cargo):
    r = client.get(f"/cargo/{cargo}/report")
    assert r.status_code == 200, r.text
    return [e["interval_id"] for e in r.json()["excursions"]]


def test_register_fault_shape_and_audit(client):
    _setup(client)
    # 非法时段（起点不早于终点）-> 422，不留记录
    r = _fault(client, "bad-1", "12:00:00", "12:00:00")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_fault_window"
    r = _fault(client, "bad-2", "13:00:00", "12:00:00")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_fault_window"
    # 空白原因 -> 422
    r = _fault(client, "bad-3", "12:00:00", "13:00:00", reason="   ")
    assert r.status_code == 422
    # 缺时区 -> 422 timezone_required
    r = client.post(
        "/trips/PTRIP/probe-faults",
        json={
            "fault_id": "bad-4",
            "fault_start_at": "2026-09-30T12:00:00",
            "fault_end_at": "2026-09-30T13:00:00",
            "reason": "x",
        },
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "timezone_required"
    # 未知车次 -> 404，不留记录
    r = _fault(client, "bad-5", "12:00:00", "13:00:00", trip="NOPE")
    assert r.status_code == 404 and r.json()["error"]["code"] == "trip_not_found"

    # 正常登记：回执与查询
    r = _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["fault"]["fault_id"] == "pf-1"
    assert body["fault"]["status"] == "active"
    assert body["fault"]["version"] == 0
    assert body["fault"]["revoked_at"] is None
    assert body["affected_cargo_ids"] == ["PF1", "PF2"]

    # 无货物落在窗口内时 affected 为空（故障仍登记）
    r = _fault(client, "pf-empty", "20:00:00", "21:00:00")
    assert r.status_code == 201
    assert r.json()["affected_cargo_ids"] == []

    # 车次故障列表 + 单条查询
    r = client.get("/trips/PTRIP/probe-faults")
    assert r.status_code == 200
    ids = [f["fault_id"] for f in r.json()]
    assert ids == ["pf-1", "pf-empty"]
    r = client.get("/probe-faults/pf-1")
    assert r.status_code == 200 and r.json()["fault_id"] == "pf-1"
    r = client.get("/probe-faults/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "probe_fault_not_found"
    # 未知车次的故障列表 404
    assert client.get("/trips/NOPE/probe-faults").status_code == 404


def test_register_idempotent_and_conflict(client):
    _setup(client)
    r1 = _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert r1.status_code == 201
    # 同请求重传 -> 200 replayed 原结果
    r2 = _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["fault"]["fault_id"] == "pf-1"
    # 只有一条登记
    assert len(client.get("/trips/PTRIP/probe-faults").json()) == 1
    # 异内容 -> 409
    r3 = _fault(client, "pf-1", "10:00:00", "13:00:00")
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "probe_fault_id_conflict"


def test_active_fault_isolates_vehicle_readings(client):
    _setup(client)
    # 10:00 车载 7℃（PF1 上限 4 -> 越温；PF2 上限 8 -> 不越温）
    _reading(client, "vr-10", "10:00:00", "7")
    # 11:00 车载 3℃ 闭合
    _reading(client, "vr-11", "11:00:00", "3")
    assert _excursion_ids(client, "PF1") == ["veh:vr-10"]

    # 登记覆盖 [10:00,12:00) 的故障 -> vr-10 被隔离，越温区间消失
    r = _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert r.status_code == 201
    assert _excursion_ids(client, "PF1") == []
    # PF2 本就不越温，仍无区间
    assert _excursion_ids(client, "PF2") == []

    # 时间线：vr-10 仍列出但 adopted=false / isolated=true / 覆盖故障；
    # vr-11 同样落在故障窗 [10:00,12:00) 内，一并隔离（故区间无从闭合）
    tl = client.get("/cargo/PF1/timeline").json()["points"]
    by_id = {p["reading_id"]: p for p in tl if p["reading_id"]}
    assert by_id["vr-10"]["adopted"] is False
    assert by_id["vr-10"]["isolated"] is True
    assert by_id["vr-10"]["covering_fault_ids"] == ["pf-1"]
    assert by_id["vr-11"]["adopted"] is False
    assert by_id["vr-11"]["isolated"] is True
    assert by_id["vr-11"]["covering_fault_ids"] == ["pf-1"]
    # 原始读数仍可查
    corrections = client.get("/vehicle-readings/vr-10/corrections").json()
    assert corrections["original_temperature"] == "7.000"
    assert corrections["version"] == 0

    # 车次详情：applications 标明隔离
    detail = client.get("/trips/PTRIP").json()
    rd = next(r for r in detail["readings"] if r["reading_id"] == "vr-10")
    app = {a["cargo_id"]: a for a in rd["applications"]}
    assert app["PF1"]["adopted"] is False
    assert app["PF1"]["isolated"] is True
    assert app["PF1"]["covering_fault_ids"] == ["pf-1"]
    assert app["PF1"]["reason"] == "isolated_by_probe_fault"
    # over_limit 仍按有效温度/上限给出（只隔离重建，事实判定照查）
    assert app["PF1"]["over_limit"] is True
    assert app["PF2"]["over_limit"] is False
    assert {f["fault_id"] for f in detail["probe_faults"]} == {"pf-1"}


def test_half_open_boundary(client):
    _setup(client)
    _reading(client, "vr-10", "10:00:00", "7")
    _reading(client, "vr-12", "12:00:00", "7")
    _reading(client, "vr-13", "13:00:00", "3")
    # 故障 [10:00,12:00)：10:00 含（隔离），12:00 不含（仍采用 -> 开启区间）
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    ids = _excursion_ids(client, "PF1")
    assert ids == ["veh:vr-12"]
    tl = {p["reading_id"]: p for p in client.get("/cargo/PF1/timeline").json()["points"]
          if p["reading_id"]}
    assert tl["vr-10"]["isolated"] is True
    assert tl["vr-12"]["isolated"] is False


def test_late_reading_also_isolated(client):
    _setup(client)
    # 先登记故障，再晚到补录读数 -> 同样隔离
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    _reading(client, "vr-late", "10:30:00", "9")
    assert _excursion_ids(client, "PF1") == []
    tl = {p["reading_id"]: p for p in client.get("/cargo/PF1/timeline").json()["points"]
          if p["reading_id"]}
    assert tl["vr-late"]["isolated"] is True
    assert tl["vr-late"]["covering_fault_ids"] == ["pf-1"]


def test_overlapping_faults_and_partial_revoke(client):
    _setup(client)
    _reading(client, "vr-10", "10:00:00", "7")
    _reading(client, "vr-11", "11:00:00", "7")
    _reading(client, "vr-12", "12:00:00", "3")
    # 两条重叠故障：pf-a [10:00,11:30)、pf-b [10:30,12:00)
    assert _fault(client, "pf-a", "10:00:00", "11:30:00").status_code == 201
    assert _fault(client, "pf-b", "10:30:00", "12:00:00").status_code == 201
    assert _excursion_ids(client, "PF1") == []

    def cover_of(rid):
        pts = [p for p in client.get("/cargo/PF1/timeline").json()["points"]
               if p["reading_id"] == rid]
        return pts[0]["covering_fault_ids"], pts[0]["isolated"]

    assert cover_of("vr-10") == (["pf-a"], True)
    assert cover_of("vr-11") == (["pf-a", "pf-b"], True)

    # 撤销 pf-a：vr-10 恢复采用（开启区间），vr-11 仍被 pf-b 覆盖隔离
    r = client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-a", "fault_id": "pf-a", "expected_version": 0},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["fault"]["status"] == "revoked"
    assert body["fault"]["version"] == 1
    assert body["fault"]["revocation_id"] == "rev-a"
    assert body["revocation"]["version"] == 1
    assert _excursion_ids(client, "PF1") == ["veh:vr-10"]
    assert cover_of("vr-11") == (["pf-b"], True)

    # 再撤销 pf-b（最后一条覆盖）：vr-11 恢复但不改变既有区间标识；vr-12 闭合
    r = client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-b", "fault_id": "pf-b", "expected_version": 0},
    )
    assert r.status_code == 201
    ids = _excursion_ids(client, "PF1")
    assert ids == ["veh:vr-10"]
    ex = client.get("/cargo/PF1/report").json()["excursions"][0]
    assert ex["end_reading_id"] == "vr-12"


def test_revoke_restores_effective_temperature_and_keeps_own_priority(client):
    _setup(client)
    _reading(client, "vr-10", "10:00:00", "7")
    _reading(client, "vr-11", "11:00:00", "7")
    # 故障 [10:00,12:00)：两笔读数均被隔离，PF1/PF2 均无越温
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert _excursion_ids(client, "PF1") == []
    # 故障生效期间把两笔读数均更正为 3.5（不越限）；更正历史照常可查
    for rid in ("vr-10", "vr-11"):
        r = client.post(
            "/corrections",
            json={
                "correction_id": f"corr-{rid}",
                "target_type": "vehicle_reading",
                "target_id": rid,
                "expected_version": 0,
                "new_temperature": "3.5",
            },
        )
        assert r.status_code in (200, 201), r.text
    # PF2 在 11:00 有同刻自有测温 3℃：即使撤销故障，车载 vr-11 对 PF2 仍被取代
    r = client.post(
        "/events",
        json={
            "event_id": "own-pf2-11",
            "cargo_id": "PF2",
            "occurred_at": "2026-09-30T11:00:00+08:00",
            "temperature": "3",
        },
    )
    assert r.status_code == 201, r.text

    # 撤销故障：两笔读数按当前有效温度 3.5 恢复采用 -> 均不越温
    r = client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 0},
    )
    assert r.status_code == 201
    assert _excursion_ids(client, "PF1") == []
    tl = {p["reading_id"]: p for p in client.get("/cargo/PF1/timeline").json()["points"]
          if p["reading_id"]}
    assert tl["vr-10"]["adopted"] is True and tl["vr-10"]["isolated"] is False
    assert tl["vr-10"]["temperature"] == "3.500"
    assert tl["vr-10"]["version"] == 1
    hist = client.get("/vehicle-readings/vr-10/corrections").json()
    assert hist["original_temperature"] == "7.000"
    assert hist["effective_temperature"] == "3.500"
    # 同刻自有优先不因故障撤销改变
    tl2 = {p["reading_id"]: p for p in client.get("/cargo/PF2/timeline").json()["points"]
           if p["reading_id"]}
    assert tl2["vr-11"]["adopted"] is False
    assert tl2["vr-11"]["isolated"] is False


def test_handover_unaffected_and_recompute_responsibility(client):
    _setup(client)
    # 交接在故障时段内仍正常切分保管方
    client.post(
        "/events",
        json={
            "event_id": "h1",
            "cargo_id": "PF1",
            "occurred_at": "2026-09-30T11:00:00+08:00",
            "giver": "北京冷库",
            "receiver": "上海冷库",
        },
    )
    _reading(client, "vr-10", "10:00:00", "7")
    _reading(client, "vr-12", "12:00:00", "3")
    assert _excursion_ids(client, "PF1") == ["veh:vr-10"]

    # 登记故障 -> 区间消失；旧表态历史保留可查
    snap = client.get("/cargo/PF1/responsibilities").json()["intervals"][0]["snapshot_id"]
    client.post(
        "/cargo/PF1/responsibilities/veh:vr-10/stances",
        json={
            "snapshot_id": snap,
            "responsible_party": "北京冷库",
            "stance": "confirm",
            "stance_id": "st-1",
        },
    )
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert _excursion_ids(client, "PF1") == []
    hist = client.get("/cargo/PF1/responsibilities/veh:vr-10/stances").json()
    assert hist["interval_exists"] is False
    assert len(hist["history"]) == 1 and hist["history"][0]["is_current"] is False

    # 撤销故障 -> 区间重算恢复，责任确认重新可见，历史表态按片段判定有效性
    client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 0},
    )
    assert _excursion_ids(client, "PF1") == ["veh:vr-10"]
    resp = client.get("/cargo/PF1/responsibilities").json()["intervals"][0]
    assert [s["responsible_party"] for s in resp["segments"]] == ["北京冷库", "上海冷库"]
    hist = client.get("/cargo/PF1/responsibilities/veh:vr-10/stances").json()
    assert hist["interval_exists"] is True
    assert hist["history"][0]["is_current"] is True
    # 当前保管方仍由交接推进（交接不受故障影响）
    assert client.get("/cargo/PF1/report").json()["current_holder"] == "上海冷库"


def test_simultaneous_excursions_recomputed(client):
    _setup(client)
    _reading(client, "vr-10", "10:00:00", "9")
    _reading(client, "vr-11", "11:00:00", "3")
    q = (
        "/trips/PTRIP/simultaneous-excursions"
        "?start_at=2026-09-30T00:00:00%2B08:00"
        "&end_at=2026-10-01T00:00:00%2B08:00&min_cargo=2"
    )
    segs = client.get(q).json()["segments"]
    assert len(segs) == 1 and segs[0]["cargo_count"] == 2
    _fault(client, "pf-1", "09:30:00", "12:00:00")
    assert client.get(q).json()["segments"] == []
    client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 0},
    )
    segs = client.get(q).json()["segments"]
    assert len(segs) == 1 and segs[0]["cargo_count"] == 2


def test_revoke_conflicts(client):
    _setup(client)
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    payload = {"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 0}
    r = client.post("/probe-fault-revocations", json=payload)
    assert r.status_code == 201
    # 同请求重传 -> 200 replayed
    r = client.post("/probe-fault-revocations", json=payload)
    assert r.status_code == 200 and r.json()["replayed"] is True
    # 只有一条撤销留痕
    db = SessionLocal()
    try:
        from app.models import ProbeFaultRevocation
        assert db.query(ProbeFaultRevocation).count() == 1
    finally:
        db.close()
    # 已撤销再用过期期望版本 -> stale_fault_version
    r = client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-2", "fault_id": "pf-1", "expected_version": 0},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_fault_version"
    # 撤销编号异请求冲突
    r = client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 1},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "revocation_id_conflict"
    # 未知故障 -> 404
    r = client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-x", "fault_id": "nope", "expected_version": 0},
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "probe_fault_not_found"
    # 撤销失败不留部分写入（rev-x 未落库）
    db = SessionLocal()
    try:
        from app.models import ProbeFaultRevocation
        assert {r.revocation_id for r in db.query(ProbeFaultRevocation).all()} == {"rev-1"}
    finally:
        db.close()


def test_revision_bump_invalidates_precheck_digest(client):
    _setup(client)
    rev_before = client.get("/trips/PTRIP").json()["revision"]
    # 先做预检（无新批次时段，仅查看既有货物集合）
    pre = client.post(
        "/trips/PTRIP/manifest/precheck",
        json={
            "expected_trip_revision": rev_before,
            "items": [
                {
                    "cargo_id": "PF1",
                    "loaded_at": "2026-09-30T18:00:00+08:00",
                    "unloaded_at": "2026-09-30T20:00:00+08:00",
                }
            ],
        },
    )
    assert pre.status_code == 200, pre.text
    digest = pre.json()["digest"]
    # 登记故障推进修订号
    r = _fault(client, "pf-1", "10:00:00", "12:00:00")
    assert r.status_code == 201
    assert client.get("/trips/PTRIP").json()["revision"] == rev_before + 1
    # 旧摘要确认 -> 拒绝
    confirm = client.post(
        "/trips/PTRIP/manifest/confirm",
        json={
            "digest": digest,
            "items": [
                {
                    "cargo_id": "PF1",
                    "loaded_at": "2026-09-30T18:00:00+08:00",
                    "unloaded_at": "2026-09-30T20:00:00+08:00",
                }
            ],
        },
    )
    assert confirm.status_code == 409
    assert confirm.json()["error"]["code"] == "stale_precheck_digest"
    # 撤销再次推进修订号
    rev_after = client.get("/trips/PTRIP").json()["revision"]
    client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 0},
    )
    assert client.get("/trips/PTRIP").json()["revision"] == rev_after + 1


def test_sealed_package_unchanged_diff_reflects_isolation(client):
    _setup(client)
    _reading(client, "vr-10", "10:00:00", "7")
    _reading(client, "vr-11", "11:00:00", "3")
    snap = client.get("/cargo/PF1/responsibilities").json()["intervals"][0]["snapshot_id"]
    # 封存
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": "CASE-PF-1",
            "cargo_id": "PF1",
            "interval_id": "veh:vr-10",
            "snapshot_id": snap,
        },
    )
    assert r.status_code == 201, r.text
    sealed_digest = r.json()["content_digest"]
    sealed_readings = {x["reading_id"]: x for x in r.json()["content"]["vehicle_readings"]}
    assert sealed_readings["vr-10"]["adopted"] is True

    # 登记故障：区间消失；原封存包内容 / 摘要不变
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    got = client.get("/evidence-seals/CASE-PF-1").json()
    assert got["content_digest"] == sealed_digest
    assert {x["reading_id"]: x["adopted"]
            for x in got["content"]["vehicle_readings"]}["vr-10"] is True

    diff = client.get("/evidence-seals/CASE-PF-1/diff").json()
    assert diff["status"] == "disappeared"
    rd = next(
        d for d in diff["evidence_changes"]["vehicle_readings"]
        if d["key"] == "vr-10"
    )
    assert rd["change"] == "changed"
    assert "adopted" in rd["fields"]
    assert "isolated" in rd["fields"]
    assert "covering_fault_ids" in rd["fields"]
    assert rd["current"]["isolated"] is True
    assert rd["current"]["covering_fault_ids"] == ["pf-1"]

    # 撤销故障：区间恢复但快照一致（vr-10 开启、vr-11 闭合均未变）-> unchanged
    client.post(
        "/probe-fault-revocations",
        json={"revocation_id": "rev-1", "fault_id": "pf-1", "expected_version": 0},
    )
    diff = client.get("/evidence-seals/CASE-PF-1/diff").json()
    assert diff["status"] == "unchanged", diff
    assert diff["evidence_changes"]["vehicle_readings"] == []
    # 原包仍不变
    assert client.get("/evidence-seals/CASE-PF-1").json()["content_digest"] == sealed_digest


def test_new_sealed_package_includes_isolation_marks(client):
    _setup(client)
    _reading(client, "vr-10", "10:00:00", "7")
    _fault(client, "pf-1", "10:00:00", "12:00:00")
    # 自有测温在 13:00 开启、14:00 闭合一个越温区间；区间内放一笔被隔离的
    # 车载读数（13:30 落在第二个故障窗内）与一笔正常读数（15:00 不在区间）
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "PF1",
        "occurred_at": "2026-09-30T13:00:00+08:00", "temperature": "7"})
    _fault(client, "pf-2", "13:00:00", "14:00:00")
    _reading(client, "vr-iso", "13:30:00", "2")
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "PF1",
        "occurred_at": "2026-09-30T14:00:00+08:00", "temperature": "3"})
    snap = client.get("/cargo/PF1/responsibilities").json()["intervals"][0]["snapshot_id"]
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": "CASE-PF-2",
            "cargo_id": "PF1",
            "interval_id": "m1",
            "snapshot_id": snap,
        },
    )
    assert r.status_code == 201, r.text
    readings = {x["reading_id"]: x for x in r.json()["content"]["vehicle_readings"]}
    # 被故障隔离的原始读数随包封存并标记
    assert readings["vr-iso"]["adopted"] is False
    assert readings["vr-iso"]["isolated"] is True
    assert readings["vr-iso"]["covering_fault_ids"] == ["pf-2"]
    # 区间外（10:00、被另一故障隔离）的读数不属本区间封存范围
    assert "vr-10" not in readings
