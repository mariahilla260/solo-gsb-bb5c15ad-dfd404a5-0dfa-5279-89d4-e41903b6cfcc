"""车次 / 装卸时段 / 车载测温读数 API 集成测试。

覆盖：
* 车次登记、重复车次冲突、车次不存在；
* 装卸时段左闭右开、起点早于终点（非法整体拒绝不留部分记录）、
  同货物跨车次重叠拒绝、首尾相接允许、批次内重叠拒绝、货物不存在整批拒绝；
* 车载读数晚到、编号幂等/异内容冲突、同车次同刻冲突、车次不存在；
* 车载读数仅作用于读数时刻仍在车上的货物（左闭右开边界），按各自上限越温；
* 同刻货物自有测温优先采用，车载原始读数仍可查；
* 交接仍只由货物自有事件改变保管方；
* 追责报告/读数来源时间线、车载开启区间的稳定标识 veh:<读数ID>；
* 车载区间的责任表态、快照、改口与晚到读数后旧表态失效。
"""
def post_trip(client, trip_id="T1", vehicle_no="京A-001", note=None):
    body = {"trip_id": trip_id, "vehicle_no": vehicle_no}
    if note is not None:
        body["note"] = note
    return client.post("/trips", json=body)


def post_manifest(client, trip_id, items):
    return client.post(f"/trips/{trip_id}/manifest", json={"items": items})


def post_reading(client, rid, trip_id, at, temp):
    return client.post(
        "/vehicle-readings",
        json={
            "reading_id": rid,
            "trip_id": trip_id,
            "read_at": at,
            "temperature": str(temp),
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


def make_two_cargo(client, c1_limit="5", c2_limit="8", holder="A"):
    for cid, limit in (("C1", c1_limit), ("C2", c2_limit)):
        r = client.post("/cargo", json={
            "cargo_id": cid,
            "started_at": "2026-01-01T08:00:00+00:00",
            "temp_upper_limit": limit,
            "initial_holder": holder,
        })
        assert r.status_code == 201, r.text


# ------------------------------------------------------------- 车次


def test_create_and_get_trip(client, make_cargo):
    r = post_trip(client, "T1", "京A-1", "京沪线")
    assert r.status_code == 201, r.text
    assert r.json()["vehicle_no"] == "京A-1"
    assert client.get("/trips").json()[0]["trip_id"] == "T1"


def test_duplicate_trip_conflict(client, make_cargo):
    post_trip(client)
    r = post_trip(client)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "trip_exists"


def test_trip_not_found(client, make_cargo):
    r = client.get("/trips/NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


# ------------------------------------------------------------- 装卸时段


def test_invalid_loading_window_rejected_entire_batch(client, make_cargo):
    make_cargo(cargo_id="C1")
    make_cargo(cargo_id="C2")
    post_trip(client)
    # 起点 == 终点 非法；整批中另一条合法也不留记录
    r = post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T11:00:00+00:00",
         "unloaded_at": "2026-01-01T11:00:00+00:00"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
    detail = client.get("/trips/T1").json()
    assert detail["loadings"] == []  # 不留部分记录


def test_loading_start_after_end_rejected(client, make_cargo):
    make_cargo()
    post_trip(client)
    r = post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T10:00:00+00:00"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"


def test_manifest_unknown_cargo_or_trip_rejected(client, make_cargo):
    make_cargo()
    post_trip(client)
    r1 = post_manifest(client, "T1", [
        {"cargo_id": "NOPE",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    assert r1.status_code == 404
    assert r1.json()["error"]["code"] == "cargo_not_found"
    r2 = post_manifest(client, "NOPE", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "trip_not_found"


def test_cargo_cannot_overlap_across_trips_but_touching_ok(client, make_cargo):
    make_cargo()
    post_trip(client, "T1")
    post_trip(client, "T2")
    assert post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ]).status_code == 201
    # 跨车次重叠 -> 拒绝（同货物同时属于两个车次）
    r = post_manifest(client, "T2", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T11:00:00+00:00",
         "unloaded_at": "2026-01-01T13:00:00+00:00"},
    ])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"
    # 首尾相接（左闭右开）允许
    r2 = post_manifest(client, "T2", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    assert r2.status_code == 201, r2.text


def test_overlap_within_same_batch_rejected(client, make_cargo):
    make_cargo()
    post_trip(client)
    r = post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T11:00:00+00:00",
         "unloaded_at": "2026-01-01T13:00:00+00:00"},
    ])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"


def test_different_cargo_can_share_trip_window(client):
    make_two_cargo(client)
    post_trip(client)
    r = post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    assert r.status_code == 201
    assert len(r.json()["items"]) == 2


# ------------------------------------------------------------- 车载读数写入


def test_reading_unknown_trip_rejected(client, make_cargo):
    r = post_reading(client, "r1", "NOPE", "2026-01-01T11:00:00+00:00", 9)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_reading_idempotent_and_conflict(client, make_cargo):
    make_cargo()
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    body = {"reading_id": "r1", "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00", "temperature": "9"}
    first = client.post("/vehicle-readings", json=body)
    assert first.status_code == 201
    assert first.json()["affected_cargo_ids"] == ["C1"]
    # 同内容重传 -> 200 replayed
    second = client.post("/vehicle-readings", json=body)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["affected_cargo_ids"] == ["C1"]
    # 异内容 -> 409
    body["temperature"] = "3"
    conflict = client.post("/vehicle-readings", json=body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "reading_id_conflict"
    detail = client.get("/trips/T1").json()
    assert len(detail["readings"]) == 1
    assert detail["readings"][0]["temperature"] == "9.000"


def test_reading_same_timestamp_conflict(client, make_cargo):
    make_cargo()
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    post_reading(client, "r1", "T1", "2026-01-01T11:00:00+00:00", 9)
    r = post_reading(client, "r2", "T1", "2026-01-01T11:00:00+00:00", 7)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"


def test_reading_when_nobody_on_board_still_stored(client, make_cargo):
    make_cargo()
    post_trip(client)
    # 未登记任何装卸时段：读数仍可写入（原始记录可查），只是没有受影响货物
    r = post_reading(client, "r0", "T1", "2026-01-01T11:00:00+00:00", 9)
    assert r.status_code == 201
    assert r.json()["affected_cargo_ids"] == []
    assert client.get("/cargo/C1/report").json()["excursions"] == []


# ----------------------------------------------- 适用性：左闭右开 / 各自上限


def test_reading_applies_half_open_boundaries_and_per_cargo_limit(client):
    make_two_cargo(client, c1_limit="5", c2_limit="8")
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    # 7℃：对 C1（上限 5）越温，对 C2（上限 8）不越温
    post_reading(client, "r-on", "T1", "2026-01-01T11:00:00+00:00", 7)
    # 恰好装载时刻 -> 在车上；恰好卸载时刻 -> 已不在车上（边界读数只登记 C2 不在车，
    # 用 7℃ 避免对上限 8 的 C2 额外开启区间；C1 在 10:00 已被该读数开启）
    post_reading(client, "r-load", "T1", "2026-01-01T10:00:00+00:00", 7)
    post_reading(client, "r-unload", "T1", "2026-01-01T12:00:00+00:00", 9)

    detail = client.get("/trips/T1").json()
    apps = {r["reading_id"]: {a["cargo_id"]: a for a in r["applications"]}
            for r in detail["readings"]}

    c1_on, c2_on = apps["r-on"]["C1"], apps["r-on"]["C2"]
    assert (c1_on["on_board"], c1_on["adopted"], c1_on["over_limit"]) == (
        True, True, True)
    assert (c2_on["on_board"], c2_on["adopted"], c2_on["over_limit"]) == (
        True, True, False)

    assert apps["r-load"]["C1"]["on_board"] is True
    assert apps["r-unload"]["C1"]["on_board"] is False
    assert apps["r-unload"]["C1"]["reason"] == "not_on_board"

    # C1 因车载读数越温（11:00 与 10:00 两笔在车上）；C2 不越温
    exs = client.get("/cargo/C1/report").json()["excursions"]
    assert len(exs) == 1
    assert exs[0]["start"] == "2026-01-01T10:00:00Z"
    assert exs[0]["is_open"] is True
    assert client.get("/cargo/C2/report").json()["excursions"] == []


def test_vehicle_reading_does_not_change_holder(client):
    make_two_cargo(client)
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    post_reading(client, "r1", "T1", "2026-01-01T11:00:00+00:00", 9)
    for cid in ("C1", "C2"):
        rpt = client.get(f"/cargo/{cid}/report").json()
        assert rpt["current_holder"] == "A"


# ------------------------------------------------- 自有测温优先 / 原始可查


def test_own_measurement_takes_precedence_but_vehicle_kept(client):
    make_two_cargo(client)
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    ts = "2026-01-01T11:00:00+00:00"
    # 车载 9℃（越温），但 C1 同刻自有测温 3℃（不越温）
    post_reading(client, "r1", "T1", ts, 9)
    m(client, "m-own-1", ts, 3, cargo="C1")

    # C1：采用自有值，无越温
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    # C2：无自有测温，采用车载 9℃，越温
    assert len(client.get("/cargo/C2/report").json()["excursions"]) == 1

    # 车次读数查询：对 C1 adopted=false，对 C2 adopted=true，原始读数仍在
    detail = client.get("/trips/T1").json()
    app1 = detail["readings"][0]["applications"][0]
    by_cargo = {a["cargo_id"]: a for a in detail["readings"][0]["applications"]}
    assert by_cargo["C1"]["adopted"] is False
    assert by_cargo["C1"]["reason"] == "supplanted_by_own"
    assert by_cargo["C2"]["adopted"] is True

    # 货物合并时间线：车载点仍列出但 adopted=false
    tl = client.get("/cargo/C1/timeline").json()["points"]
    at_11 = [p for p in tl if p["occurred_at"].startswith("2026-01-01T11:00")]
    kinds = {(p["source"], p["adopted"]) for p in at_11}
    assert ("own", True) in kinds and ("vehicle", False) in kinds

    # 货物自有事件列表只列自有事件，不含车载读数
    own_ids = [e["event_id"] for e in client.get("/cargo/C1/events").json()]
    assert own_ids == ["m-own-1"]


def test_vehicle_closes_after_own_opener_uses_vehicle_close(client):
    make_cargo_limit = None  # 占位
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    # 自有测温 9℃ 开启
    m(client, "m1", "2026-01-01T10:30:00+00:00", 9)
    # 车载 3℃ 闭合（车上）
    post_reading(client, "r1", "T1", "2026-01-01T12:00:00+00:00", 3)
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert ex["is_open"] is False
    assert ex["start_source"] == "own"
    assert ex["end_source"] == "vehicle"
    assert ex["end_reading_id"] == "r1"
    assert ex["end_trip_id"] == "T1"
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    assert iv["interval_id"] == "m1"
    assert iv["start_event_id"] == "m1"


# --------------------------------------------------------- 晚到读数重算


def test_late_reading_retroactively_opens_interval(client):
    # 先有自有闭合测温，后补车载读数（晚到），区间应重建为车载开启
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client)
    m(client, "m1", "2026-01-01T12:00:00+00:00", 3)  # 无区间，无影响
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    # 读数时刻 11:00 晚于装卸登记写入（读数晚到），且早于既有自有测温
    post_reading(client, "vr-late", "T1", "2026-01-01T11:00:00+00:00", 9)
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert ex["interval_id"] == "veh:vr-late"
    assert ex["start"] == "2026-01-01T11:00:00Z"
    assert ex["end"] == "2026-01-01T12:00:00Z"
    assert ex["start_source"] == "vehicle"


def test_reading_outside_window_never_applies_even_late(client):
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    # 13:00 已下车：即使读数越温也不影响该货物
    post_reading(client, "r-off", "T1", "2026-01-01T13:00:00+00:00", 9)
    assert client.get("/cargo/C1/report").json()["excursions"] == []


# ------------------------------------------------- 车载区间的责任确认 / 表态


def test_vehicle_opened_interval_stances(client):
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    # 车载 9℃ 开启；11:00 A->B（自有交接）；车载 3℃ 闭合
    post_reading(client, "vr1", "T1", "2026-01-01T10:30:00+00:00", 9)
    h(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    post_reading(client, "vr2", "T1", "2026-01-01T12:00:00+00:00", 3)

    intervals = client.get("/cargo/C1/responsibilities").json()["intervals"]
    assert [iv["interval_id"] for iv in intervals] == ["veh:vr1"]
    iv = intervals[0]
    assert iv["start_source"] == "vehicle"
    assert iv["start_reading_id"] == "vr1"
    assert iv["end_source"] == "vehicle"
    assert iv["end_reading_id"] == "vr2"
    assert [(s["responsible_party"]) for s in iv["segments"]] == ["A", "B"]

    # 直查 + 表态
    snap = iv["snapshot_id"]
    r = client.post("/cargo/C1/responsibilities/veh:vr1/stances", json={
        "snapshot_id": snap, "responsible_party": "A",
        "stance": "confirm", "stance_id": "s1"})
    assert r.status_code == 201, r.text
    assert r.json()["opener_source"] == "vehicle"
    assert r.json()["vehicle_reading_id"] == "vr1"

    # 历史可查
    hist = client.get("/cargo/C1/responsibilities/veh:vr1/stances").json()
    assert hist["interval_exists"] is True
    assert hist["history"][0]["stance_id"] == "s1"


def test_vehicle_interval_not_found_and_open(client):
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    # 不存在的车载区间
    r = client.get("/cargo/C1/responsibilities/veh:nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "interval_not_found"
    # 开放的车载区间
    post_reading(client, "vr1", "T1", "2026-01-01T10:30:00+00:00", 9)
    r2 = client.get("/cargo/C1/responsibilities/veh:vr1")
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "interval_open"


def test_late_own_measurement_invalidates_vehicle_stance(client):
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    post_reading(client, "vr1", "T1", "2026-01-01T10:30:00+00:00", 9)
    post_reading(client, "vr2", "T1", "2026-01-01T12:00:00+00:00", 3)
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    assert client.post("/cargo/C1/responsibilities/veh:vr1/stances", json={
        "snapshot_id": snap, "responsible_party": "A",
        "stance": "confirm", "stance_id": "s1"}).status_code == 201

    # 晚到一笔同刻（10:30）自有测温 3℃：车载开启读数被取代，区间消失
    m(client, "m-late", "2026-01-01T10:30:00+00:00", 3)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    hist = client.get("/cargo/C1/responsibilities/veh:vr1/stances").json()
    assert hist["interval_exists"] is False
    assert hist["history"][0]["is_current"] is False
    # 用旧快照再表态 -> 区间不存在 404
    r = client.post("/cargo/C1/responsibilities/veh:vr1/stances", json={
        "snapshot_id": snap, "responsible_party": "A",
        "stance": "confirm", "stance_id": "s2"})
    assert r.status_code == 404


def test_reserved_event_id_prefix_rejected(client, make_cargo):
    make_cargo()
    r = m(client, "veh:x", "2026-01-01T11:00:00+00:00", 9)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "reserved_event_id"


def test_second_trip_later_reading_uses_second_window(client):
    # 同一货物先后由两个车次承运（首尾相接），各自读数只作用于在车时段
    client.post("/cargo", json={
        "cargo_id": "C1", "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": "5", "initial_holder": "A"})
    post_trip(client, "T1")
    post_trip(client, "T2")
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    post_manifest(client, "T2", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    # T1 11:00 越温开启；T1 12:00（已下车，恰好边界）不适用；T2 13:00 恢复闭合
    post_reading(client, "r1", "T1", "2026-01-01T11:00:00+00:00", 9)
    post_reading(client, "r2", "T1", "2026-01-01T12:00:00+00:00", 3)
    post_reading(client, "r3", "T2", "2026-01-01T13:00:00+00:00", 3)
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert ex["interval_id"] == "veh:r1"
    assert ex["start_trip_id"] == "T1"
    assert ex["end"] == "2026-01-01T13:00:00Z"
    assert ex["end_reading_id"] == "r3"
    assert ex["end_trip_id"] == "T2"
