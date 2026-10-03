"""测温值更正 API 集成测试。

覆盖：
* 自有测温更正：版本递增、有效温度生效、追责报告/责任确认/时间线按有效值重建；
* 原始记录查询 temperature 恒为原值，另显示 effective_temperature / version；
* 原始记录及历次更正可查（事件 / 车载读数两处历史端点，交接为空历史）；
* 车载读数更正同时影响读数时刻在车的全部货物（各自上限、左闭右开）；
* 同刻自有测温优先、装卸时段左闭右开在更正后继续适用；
* 原事件 / 原读数按原内容重传仍幂等；
* 更正ID同内容重传返回原结果、异内容冲突；
* 版本过期、目标不存在、目标为交接均拒绝且不落部分结果；
* 历史表态保留，其当前有效性按重建后的责任片段判断；
* 车次同时越温查询按有效值重建。
"""
def post_correction(
    client,
    cid,
    target_type,
    target_id,
    expected_version,
    temp,
):
    return client.post(
        "/corrections",
        json={
            "correction_id": cid,
            "target_type": target_type,
            "target_id": target_id,
            "expected_version": expected_version,
            "new_temperature": str(temp),
        },
    )


def m(client, eid, at, temp, cargo="C1"):
    return client.post(
        "/events",
        json={"event_id": eid, "cargo_id": cargo,
              "occurred_at": at, "temperature": str(temp)},
    )


def h(client, eid, at, giver, receiver, cargo="C1"):
    return client.post(
        "/events",
        json={"event_id": eid, "cargo_id": cargo, "occurred_at": at,
              "giver": giver, "receiver": receiver},
    )


def post_reading(client, rid, trip_id, at, temp):
    return client.post(
        "/vehicle-readings",
        json={"reading_id": rid, "trip_id": trip_id,
              "read_at": at, "temperature": str(temp)},
    )


def post_trip(client, trip_id="T1", vehicle_no="京A-001"):
    return client.post("/trips", json={"trip_id": trip_id, "vehicle_no": vehicle_no})


def post_manifest(client, trip_id, items):
    return client.post(f"/trips/{trip_id}/manifest", json={"items": items})


def make_cargo(client, cid="C1", limit="5", holder="A",
               started="2026-01-01T08:00:00+00:00"):
    r = client.post("/cargo", json={
        "cargo_id": cid, "started_at": started,
        "temp_upper_limit": limit, "initial_holder": holder})
    assert r.status_code == 201, r.text


# ----------------------------------------------------------- 自有测温更正


def test_own_event_correction_basic(client):
    make_cargo(client)
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    m(client, "m2", "2026-01-01T11:00:00+00:00", 3)
    # 原报告：[10:00, 11:00) 越温
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert ex["interval_id"] == "m1"

    # 更正开启测温 9 -> 3（不超限）：越温区间消失
    r = post_correction(client, "corr-1", "own_event", "m1", 0, 3)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["effective_temperature"] == "3.000"
    assert body["affected_cargo_ids"] == ["C1"]
    assert body["correction"]["expected_version"] == 0
    assert body["correction"]["cargo_id"] == "C1"
    assert client.get("/cargo/C1/report").json()["excursions"] == []

    # 原始记录查询：temperature 仍为原值，另显示有效值与版本
    ev = next(e for e in client.get("/cargo/C1/events").json()
              if e["event_id"] == "m1")
    assert ev["temperature"] == "9.000"
    assert ev["effective_temperature"] == "3.000"
    assert ev["version"] == 1


def test_own_event_correction_opens_interval_retroactively(client):
    make_cargo(client)
    # 10:00 录 3（不越温），12:00 录 3：原本无区间
    m(client, "m1", "2026-01-01T10:00:00+00:00", 3)
    m(client, "m2", "2026-01-01T12:00:00+00:00", 3)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    # 10:00 更正为 9 -> 区间 [10:00,12:00)
    r = post_correction(client, "corr-1", "own_event", "m1", 0, 9)
    assert r.status_code == 201
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert (ex["start"], ex["end"]) == (
        "2026-01-01T10:00:00Z", "2026-01-01T12:00:00Z")
    assert ex["start_source"] == "own"


def test_successive_corrections_version_chain(client):
    make_cargo(client)
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    assert post_correction(client, "c1", "own_event", "m1", 0, 8).status_code == 201
    # 期望版本必须递增；过期的 0 被拒
    stale = post_correction(client, "c2", "own_event", "m1", 0, 7)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_version"
    # 不落部分结果：版本仍为 1、有效值仍为 8
    ev = next(e for e in client.get("/cargo/C1/events").json()
              if e["event_id"] == "m1")
    assert (ev["version"], ev["effective_temperature"]) == (1, "8.000")

    ok = post_correction(client, "c3", "own_event", "m1", 1, 3)
    assert ok.status_code == 201
    assert ok.json()["version"] == 2
    hist = client.get("/events/m1/corrections").json()
    assert [c["version"] for c in hist["corrections"]] == [1, 2]
    assert [c["new_temperature"] for c in hist["corrections"]] == [
        "8.000", "3.000"]
    assert hist["original_temperature"] == "9.000"
    assert hist["effective_temperature"] == "3.000"
    assert hist["version"] == 2
    assert client.get("/cargo/C1/report").json()["excursions"] == []


def test_correction_time_ownership_and_id_unchanged(client):
    make_cargo(client)
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    h(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    m(client, "m2", "2026-01-01T12:00:00+00:00", 3)
    post_correction(client, "c1", "own_event", "m1", 0, 9)  # 仍越温
    ev = next(e for e in client.get("/cargo/C1/events").json()
              if e["event_id"] == "m1")
    # 时间、归属、原编号不变
    assert ev["occurred_at"] == "2026-01-01T10:00:00Z"
    assert ev["cargo_id"] == "C1"
    assert ev["event_id"] == "m1"
    rpt = client.get("/cargo/C1/report").json()
    assert rpt["current_holder"] == "B"


# ----------------------------------------------------------- 拒绝场景


def test_correction_target_not_found(client, make_cargo):
    make_cargo()
    r1 = post_correction(client, "c1", "own_event", "nope", 0, 3)
    assert r1.status_code == 404
    assert r1.json()["error"]["code"] == "event_not_found"
    r2 = post_correction(client, "c2", "vehicle_reading", "nope", 0, 3)
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "reading_not_found"


def test_handover_not_correctable(client, make_cargo):
    make_cargo(holder="A")
    h(client, "h1", "2026-01-01T10:00:00+00:00", "A", "B")
    r = post_correction(client, "c1", "own_event", "h1", 0, 9)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "handover_not_correctable"
    # 交接历史仍可查：空更正、版本 0、有效温度 null
    hist = client.get("/events/h1/corrections").json()
    assert hist["corrections"] == []
    assert hist["version"] == 0
    assert hist["original_temperature"] is None
    assert hist["effective_temperature"] is None
    ev = next(e for e in client.get("/cargo/C1/events").json()
              if e["event_id"] == "h1")
    assert ev["version"] == 0 and ev["effective_temperature"] is None


def test_bad_target_type_rejected(client, make_cargo):
    make_cargo()
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    r = post_correction(client, "c1", "vehicle", "m1", 0, 3)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_correction_shape"


def test_correction_id_same_content_replays_diff_conflicts(client):
    make_cargo(client)
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    body = {
        "correction_id": "c1", "target_type": "own_event",
        "target_id": "m1", "expected_version": 0, "new_temperature": "3",
    }
    first = client.post("/corrections", json=body)
    assert first.status_code == 201
    second = client.post("/corrections", json=body)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["version"] == 1
    # 同ID不同目标 / 期望版本 / 新温度 -> 冲突
    for patch in ({"target_id": "m2"}, {"expected_version": 1},
                  {"new_temperature": "2"}):
        mutated = dict(body)
        mutated.update(patch)
        r = client.post("/corrections", json=mutated)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "correction_id_conflict"
    # 原更正结果不变
    hist = client.get("/events/m1/corrections").json()
    assert len(hist["corrections"]) == 1


def test_original_event_replay_still_idempotent_after_correction(client):
    make_cargo(client)
    body = {"event_id": "m1", "cargo_id": "C1",
            "occurred_at": "2026-01-01T10:00:00+00:00", "temperature": "9"}
    client.post("/events", json=body)
    post_correction(client, "c1", "own_event", "m1", 0, 3)
    # 原内容重传仍幂等
    r = client.post("/events", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    # 用更正后的温度重传原事件ID -> 异内容冲突
    body2 = dict(body, temperature="3")
    r2 = client.post("/events", json=body2)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "event_id_conflict"
    ev = next(e for e in client.get("/cargo/C1/events").json()
              if e["event_id"] == "m1")
    assert ev["temperature"] == "9.000"
    assert ev["effective_temperature"] == "3.000"


# ----------------------------------------------------------- 责任确认 / 表态


def test_stance_history_retained_but_invalidated_after_correction(client):
    make_cargo(client, limit="5", holder="A")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    m(client, "m2", "2026-01-01T12:00:00+00:00", 3)
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0][
        "snapshot_id"]
    assert client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={"snapshot_id": snap, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s1"}).status_code == 201

    # 更正开启测温为不超限：区间消失，历史表态保留但失效
    post_correction(client, "c1", "own_event", "m1", 0, 3)
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["interval_exists"] is False
    assert hist["history"][0]["is_current"] is False
    # 旧快照再表态 -> 区间不存在 404
    r = client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={"snapshot_id": snap, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s2"})
    assert r.status_code == 404


def test_correction_changes_segments_stale_snapshot_rejected(client):
    make_cargo(client, limit="5", holder="A")
    # 10:00 超限；11:00 A->B；12:00 不超限闭合
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    h(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    m(client, "m2", "2026-01-01T12:00:00+00:00", 3)
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    old_snap = iv["snapshot_id"]
    assert [s["responsible_party"] for s in iv["segments"]] == ["A", "B"]
    # A 先确认（供更正后验证旧表态保留但失效）
    assert client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={"snapshot_id": old_snap, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s1"}).status_code == 201

    # 把闭合测温 12:00 更正为 9（仍超限）：区间重新开放
    post_correction(client, "c1", "own_event", "m2", 0, 9)
    assert client.get("/cargo/C1/report").json()["excursions"][0]["is_open"] is True
    # 旧快照表态 -> 区间开放 422
    r = client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={"snapshot_id": old_snap, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s2"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "interval_open"

    # 再补 13:00 不超限测温：区间 [10:00,13:00)。A 的片段 [10:00,11:00) 未变化，
    # 其表态继续有效（延长区间只改变末段责任方）
    m(client, "m3", "2026-01-01T13:00:00+00:00", 3)
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["interval_exists"] is True
    assert hist["history"][0]["is_current"] is True
    current = client.get("/cargo/C1/responsibilities/m1").json()
    assert current["snapshot_id"] != old_snap

    # 再把开启测温 m1 更正为不超限，并补一笔更早（09:30）的超限测温：
    # 原 m1 区间消失，新的区间标识为 m0 —— 旧表态随旧区间失效但历史保留
    post_correction(client, "c2", "own_event", "m1", 0, 3)
    m(client, "m0", "2026-01-01T09:30:00+00:00", 9)
    hist2 = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist2["interval_exists"] is False
    assert hist2["history"][0]["is_current"] is False
    assert len(hist2["history"]) == 1
    new_ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert new_ex["interval_id"] == "m0"
    assert new_ex["start"] == "2026-01-01T09:30:00Z"
    # 已更正为 3℃ 的 m1（10:00）成为区间后的首个不超限测温，在 10:00 闭合
    assert new_ex["end"] == "2026-01-01T10:00:00Z"


def test_correction_unchanged_snapshot_keeps_stance_current(client):
    make_cargo(client, limit="5", holder="A")
    # 9 -> 8 的更正仍超限，区间快照不变
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    m(client, "m2", "2026-01-01T12:00:00+00:00", 3)
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0][
        "snapshot_id"]
    client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={"snapshot_id": snap, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s1"})
    post_correction(client, "c1", "own_event", "m1", 0, 8)
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["current_snapshot_id"] == snap
    assert hist["history"][0]["is_current"] is True


# ----------------------------------------------------------- 车载读数更正


def _setup_two_cargo_trip(client):
    for cid, limit in (("C1", "5"), ("C2", "8")):
        make_cargo(client, cid=cid, limit=limit)
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])


def test_vehicle_reading_correction_affects_all_onboard_cargo(client):
    _setup_two_cargo_trip(client)
    # 原始 7℃：C1（上限5）越温，C2（上限8）不越温
    post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 7)
    assert len(client.get("/cargo/C1/report").json()["excursions"]) == 1
    assert client.get("/cargo/C2/report").json()["excursions"] == []

    # 更正为 9℃：两票都越温（同时影响读数时刻在车的全部货物）
    r = post_correction(client, "vc1", "vehicle_reading", "vr1", 0, 9)
    assert r.status_code == 201
    assert r.json()["affected_cargo_ids"] == ["C1", "C2"]
    assert r.json()["correction"]["trip_id"] == "T1"
    for cid in ("C1", "C2"):
        exs = client.get(f"/cargo/{cid}/report").json()["excursions"]
        assert len(exs) == 1 and exs[0]["is_open"] is True

    # 车次详情：原始温度仍为 7，读数级 effective_temperature=9/version=1，
    # 两票 over_limit 均按有效值判定为 true
    detail = client.get("/trips/T1").json()["readings"][0]
    assert detail["temperature"] == "7.000"
    assert detail["effective_temperature"] == "9.000"
    assert detail["version"] == 1
    assert all(a["over_limit"] is True for a in detail["applications"])

    # 再更正为 2℃：两票均不越温
    post_correction(client, "vc2", "vehicle_reading", "vr1", 1, 2)
    for cid in ("C1", "C2"):
        assert client.get(f"/cargo/{cid}/report").json()["excursions"] == []
    hist = client.get("/vehicle-readings/vr1/corrections").json()
    assert [c["version"] for c in hist["corrections"]] == [1, 2]
    assert hist["target_type"] == "vehicle_reading"
    assert hist["original_temperature"] == "7.000"
    assert hist["effective_temperature"] == "2.000"


def test_vehicle_correction_respects_half_open_and_own_priority(client):
    _setup_two_cargo_trip(client)
    # 读数时刻 12:00（恰好卸货时刻）：无货物在车，回执为空
    post_reading(client, "vr-edge", "T1", "2026-01-01T12:00:00+00:00", 3)
    r = post_correction(client, "vc0", "vehicle_reading", "vr-edge", 0, 9)
    assert r.status_code == 201
    assert r.json()["affected_cargo_ids"] == []
    assert client.get("/cargo/C1/report").json()["excursions"] == []

    # 11:00 车载原始 3℃（不越温），C1 同刻自有 3℃
    post_reading(client, "vr2", "T1", "2026-01-01T11:00:00+00:00", 3)
    m(client, "own-1", "2026-01-01T11:00:00+00:00", 3, cargo="C1")
    # 车载更正为 9℃：C1 同刻自有优先（3℃，不越温），C2 越温
    post_correction(client, "vc1", "vehicle_reading", "vr2", 0, 9)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    c2_exs = client.get("/cargo/C2/report").json()["excursions"]
    assert len(c2_exs) == 1
    detail = client.get("/trips/T1").json()
    by_rid = {r["reading_id"]: r for r in detail["readings"]}
    c1_app = next(a for a in by_rid["vr2"]["applications"]
                  if a["cargo_id"] == "C1")
    assert c1_app["adopted"] is False
    assert c1_app["reason"] == "supplanted_by_own"
    # over_limit 按有效温度 9 判定（即使被取代）
    assert c1_app["over_limit"] is True
    # 时间线：被取代的车载点携带有效温度与版本
    tl = client.get("/cargo/C1/timeline").json()["points"]
    veh = [p for p in tl if p["reading_id"] == "vr2"][0]
    assert veh["adopted"] is False
    assert veh["temperature"] == "9.000"
    assert veh["version"] == 1


def test_vehicle_correction_original_reading_replay_idempotent(client):
    _setup_two_cargo_trip(client)
    body = {"reading_id": "vr1", "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00", "temperature": "7"}
    client.post("/vehicle-readings", json=body)
    post_correction(client, "vc1", "vehicle_reading", "vr1", 0, 9)
    # 原读数按原内容重传仍幂等（回执读数 temperature=原值，有效值=9）
    r = client.post("/vehicle-readings", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["reading"]["temperature"] == "7.000"
    assert r.json()["reading"]["effective_temperature"] == "9.000"
    assert r.json()["reading"]["version"] == 1
    assert r.json()["affected_cargo_ids"] == ["C1", "C2"]
    # 异内容重传仍冲突
    body2 = dict(body, temperature="2")
    r2 = client.post("/vehicle-readings", json=body2)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "reading_id_conflict"


def test_vehicle_correction_rebuilds_simultaneous_excursions(client):
    _setup_two_cargo_trip(client)
    # 原始 4℃：只 C1 越温（C2 上限 8），同时越温查询为空
    post_reading(client, "vr1", "T1", "2026-01-01T10:30:00+00:00", 4)
    params = (
        ("start_at", "2026-01-01T00:00:00+00:00"),
        ("end_at", "2026-01-02T00:00:00+00:00"),
    )
    r0 = client.get("/trips/T1/simultaneous-excursions", params=params)
    assert r0.json()["segments"] == []
    # 更正为 9℃：10:30 起两票同时越温（开放区间被查询终点/卸货时刻截断）
    post_correction(client, "vc1", "vehicle_reading", "vr1", 0, 9)
    seg = client.get(
        "/trips/T1/simultaneous-excursions", params=params).json()["segments"]
    assert len(seg) == 1
    assert seg[0]["start"] == "2026-01-01T10:30:00Z"
    assert seg[0]["end"] == "2026-01-01T12:00:00Z"
    assert seg[0]["cargo_ids"] == ["C1", "C2"]


def test_vehicle_correction_opens_and_closes_interval_identifiers(client):
    make_cargo(client, limit="5")
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    # 原始 3℃ 不越温
    post_reading(client, "vr-open", "T1", "2026-01-01T10:30:00+00:00", 3)
    post_reading(client, "vr-close", "T1", "2026-01-01T12:00:00+00:00", 3)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    # 开启点更正为 9、闭合点仍为 3 -> veh:vr-open 区间出现
    post_correction(client, "vc1", "vehicle_reading", "vr-open", 0, 9)
    ivs = client.get("/cargo/C1/responsibilities").json()["intervals"]
    assert [iv["interval_id"] for iv in ivs] == ["veh:vr-open"]
    iv = ivs[0]
    assert iv["start_reading_id"] == "vr-open"
    assert iv["end_reading_id"] == "vr-close"
    # 对该区间表态
    r = client.post(
        "/cargo/C1/responsibilities/veh:vr-open/stances",
        json={"snapshot_id": iv["snapshot_id"], "responsible_party": "A",
              "stance": "confirm", "stance_id": "s1"})
    assert r.status_code == 201
    # 开启点再更正回 3：区间消失，表态失效但历史保留
    post_correction(client, "vc2", "vehicle_reading", "vr-open", 1, 3)
    hist = client.get(
        "/cargo/C1/responsibilities/veh:vr-open/stances").json()
    assert hist["interval_exists"] is False
    assert hist["history"][0]["is_current"] is False


# ----------------------------------------------------------- 历史端点


def test_correction_history_endpoints_404_and_empty(client, make_cargo):
    make_cargo()
    r1 = client.get("/events/missing/corrections")
    assert r1.status_code == 404
    assert r1.json()["error"]["code"] == "event_not_found"
    r2 = client.get("/vehicle-readings/missing/corrections")
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "reading_not_found"

    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    hist = client.get("/events/m1/corrections").json()
    assert hist["target_exists"] is True
    assert hist["corrections"] == []
    assert hist["version"] == 0
    assert hist["original_temperature"] == "9.000"
    assert hist["effective_temperature"] == "9.000"


def test_timeline_reports_effective_temperature_and_version(client):
    make_cargo(client)
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    post_correction(client, "c1", "own_event", "m1", 0, 3)
    points = client.get("/cargo/C1/timeline").json()["points"]
    p = points[0]
    assert p["source"] == "own"
    assert p["temperature"] == "3.000"
    assert p["version"] == 1
