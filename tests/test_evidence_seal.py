"""责任证据封存 API 集成测试。

覆盖：
* 封存：成功封存内容（区间片段、当时有效表态、自有事件、车载读数有效温度/
  版本/是否采用、当前装卸窗口及版本、货物温控、内容摘要）；
* 拒绝不留记录：区间不存在 404 / 区间开放 422 / 快照已变 stale_snapshot；
* 幂等与冲突：同案件编号同请求重试即使现况已变（变化/重开/消失）也返回
  原包（200 replayed），异请求 case_id_conflict；失败不留封存记录；
* 查询：GET 返回原内容与摘要；未知案件 seal_not_found；
* 现况对照：unchanged / changed / reopened / disappeared 四态，证据项
  （自有事件 / 车载读数 / 装卸窗口）增删变更、责任片段增删变更，区间
  重开 / 消失明确状态；晚到记录与测温 / 窗口更正不改写原包；
* 车载区间（veh:<读数ID>）封存、被同刻自有测温取代读数的 adopted=false、
  车载读数更正与装卸窗口更正在对照中的体现。
"""
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EvidenceSeal


def setup_cargo(client, cargo="C1", limit="5", holder="A",
                start="2026-01-01T10:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def m(client, eid, minute, temp, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": f"2026-01-01T10:{minute:02d}:00+00:00",
            "temperature": str(temp),
        },
    )


def h(client, eid, minute, giver, receiver, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": f"2026-01-01T10:{minute:02d}:00+00:00",
            "giver": giver,
            "receiver": receiver,
        },
    )


def first_interval(client, cargo="C1"):
    r = client.get(f"/cargo/{cargo}/responsibilities")
    assert r.status_code == 200
    intervals = r.json()["intervals"]
    assert intervals, "预期至少一个已闭合区间"
    return intervals[0]


def seal(client, case_id, cargo, interval_id, snapshot, expected=201):
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": case_id,
            "cargo_id": cargo,
            "interval_id": interval_id,
            "snapshot_id": snapshot,
        },
    )
    assert r.status_code == expected, r.text
    return r


def build_two_party_interval(client):
    """10:10 超限(A) -> 10:30 A交B -> 10:50 恢复。返回 (iid, snap)。"""
    setup_cargo(client)
    assert m(client, "m1", 10, 8).status_code == 201
    assert h(client, "h1", 30, "A", "B").status_code == 201
    assert m(client, "m2", 50, 4).status_code == 201
    iv = first_interval(client)
    return iv["interval_id"], iv["snapshot_id"]


def seal_count() -> int:
    db = SessionLocal()
    try:
        return db.query(EvidenceSeal).count()
    finally:
        db.close()


# --------------------------------------------------------------- 封存内容


def test_seal_content_shape_and_digest(client):
    iid, snap = build_two_party_interval(client, )
    # A 表态确认
    r = client.post(
        f"/cargo/C1/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap,
            "responsible_party": "A",
            "stance": "confirm",
            "stance_id": "st-a",
        },
    )
    assert r.status_code == 201, r.text

    r = seal(client, "CASE-1", "C1", iid, snap)
    body = r.json()
    assert body["cargo_id"] == "C1"
    assert body["interval_id"] == iid
    assert body["snapshot_id"] == snap
    assert body["content_digest"].startswith("sha256:")
    assert len(body["content_digest"]) == 7 + 64

    content = body["content"]
    assert content["case_id"] == "CASE-1"
    assert content["cargo"] == {
        "cargo_id": "C1",
        "started_at": "2026-01-01T10:00:00Z",
        "temp_upper_limit": "5.000",
        "initial_holder": "A",
    }
    interval = content["interval"]
    assert interval["interval_id"] == iid == "m1"
    assert interval["start"] == "2026-01-01T10:10:00Z"
    assert interval["end"] == "2026-01-01T10:50:00Z"
    assert interval["is_open"] is False
    assert interval["snapshot_id"] == snap
    assert [
        (s["start"][11:16], s["end"][11:16], s["responsible_party"])
        for s in interval["segments"]
    ] == [("10:10", "10:30", "A"), ("10:30", "10:50", "B")]

    # 当时有效表态
    stances = content["current_valid_stances"]
    assert len(stances) == 1
    assert stances[0]["stance_id"] == "st-a"
    assert stances[0]["stance"] == "confirm"
    assert stances[0]["snapshot_id"] == snap
    assert content["summary"] == "pending"

    # 自有事件：不晚于闭合时刻的全部事件，测温带有效温度 / 版本
    assert [e["event_id"] for e in content["own_events"]] == ["h1", "m1", "m2"]
    m1_item = next(e for e in content["own_events"] if e["event_id"] == "m1")
    assert m1_item["temperature"] == "8.000"
    assert m1_item["effective_temperature"] == "8.000"
    assert m1_item["version"] == 0
    h_item = next(e for e in content["own_events"] if e["event_id"] == "h1")
    assert h_item["event_type"] == "handover"
    assert h_item["temperature"] is None
    assert h_item["effective_temperature"] is None

    # 无车次：读数 / 窗口为空
    assert content["vehicle_readings"] == []
    assert content["loading_windows"] == []


def test_seal_then_get_returns_identical_content(client):
    iid, snap = build_two_party_interval(client, )
    created = seal(client, "CASE-GET", "C1", iid, snap).json()
    got = client.get("/evidence-seals/CASE-GET")
    assert got.status_code == 200
    assert got.json() == created
    # 未知案件
    assert client.get("/evidence-seals/NOPE").status_code == 404
    assert client.get("/evidence-seals/NOPE/diff").status_code == 404


# --------------------------------------------------------------- 拒绝场景


def test_seal_interval_not_found_and_open(client):
    setup_cargo(client, )
    assert m(client, "m1", 10, 9).status_code == 201  # 开放越温
    # 未知区间标识
    r = seal(client, "CASE-X", "C1", "nope", "sha256:" + "0" * 64, expected=404)
    assert r.json()["error"]["code"] == "interval_not_found"
    assert seal_count() == 0
    # 开放区间
    r = seal(client, "CASE-Y", "C1", "m1", "sha256:" + "0" * 64, expected=422)
    assert r.json()["error"]["code"] == "interval_open"
    assert seal_count() == 0


def test_seal_unknown_cargo(client):
    r = seal(client, "CASE-C", "NOPE", "m1", "sha256:" + "0" * 64, expected=404)
    assert r.json()["error"]["code"] == "cargo_not_found"


def test_seal_stale_snapshot_rejected_without_record(client):
    iid, snap = build_two_party_interval(client, )
    # 区间内补一笔不超限测温 -> 区间提前闭合于 10:20，快照变化
    assert m(client, "m1b", 20, 4).status_code == 201
    new_iv = first_interval(client, )
    assert new_iv["snapshot_id"] != snap

    r = seal(client, "CASE-STALE", "C1", iid, snap, expected=409)
    assert r.json()["error"]["code"] == "stale_snapshot"
    assert seal_count() == 0  # 拒绝不留记录

    # 用当前快照可正常封存
    assert seal(client, "CASE-NOW", "C1", iid, new_iv["snapshot_id"]).status_code == 201


# --------------------------------------------------------------- 幂等 / 冲突


def test_seal_idempotent_replay_even_after_state_changes(client):
    iid, snap = build_two_party_interval(client, )
    created = seal(client, "CASE-R", "C1", iid, snap).json()

    # 快照变化（区间提前闭合）
    assert m(client, "m1b", 20, 4).status_code == 201
    r = seal(client, "CASE-R", "C1", iid, snap, expected=200)
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True
    assert body["seal"]["content"] == created["content"]
    assert body["seal"]["content_digest"] == created["content_digest"]
    assert seal_count() == 1  # 重放不新增

    # 区间重开
    assert client.post(
        "/corrections",
        json={
            "correction_id": "c-m1b",
            "target_type": "own_event",
            "target_id": "m1b",
            "expected_version": 0,
            "new_temperature": "8",
        },
    ).status_code == 201
    assert client.post(
        "/corrections",
        json={
            "correction_id": "c-m2",
            "target_type": "own_event",
            "target_id": "m2",
            "expected_version": 0,
            "new_temperature": "9",
        },
    ).status_code == 201
    r = seal(client, "CASE-R", "C1", iid, snap, expected=200)
    assert r.status_code == 200 and r.json()["replayed"] is True

    # 区间消失
    assert client.post(
        "/corrections",
        json={
            "correction_id": "c-m1",
            "target_type": "own_event",
            "target_id": "m1",
            "expected_version": 0,
            "new_temperature": "3",
        },
    ).status_code == 201
    r = seal(client, "CASE-R", "C1", iid, snap, expected=200)
    assert r.status_code == 200
    assert r.json()["seal"]["content"] == created["content"]
    assert seal_count() == 1


def test_seal_same_case_different_request_conflicts(client):
    iid, snap = build_two_party_interval(client, )
    seal(client, "CASE-K", "C1", iid, snap)
    # 异请求（不同快照）-> 冲突，原包不变
    other = "sha256:" + "f" * 64
    r = seal(client, "CASE-K", "C1", iid, other, expected=409)
    assert r.json()["error"]["code"] == "case_id_conflict"
    # 异货物同样冲突
    setup_cargo(client, cargo="C2")
    r = seal(client, "CASE-K", "C2", iid, snap, expected=409)
    assert r.json()["error"]["code"] == "case_id_conflict"
    # 原包仍可查、内容不变
    got = client.get("/evidence-seals/CASE-K").json()
    assert got["cargo_id"] == "C1" and got["snapshot_id"] == snap


# --------------------------------------------------------------- 现况对照


def test_diff_unchanged(client):
    iid, snap = build_two_party_interval(client, )
    seal(client, "CASE-U", "C1", iid, snap)
    d = client.get("/evidence-seals/CASE-U/diff").json()
    assert d["status"] == "unchanged"
    assert d["current_snapshot_id"] == snap
    assert d["segment_changes"] == []
    assert d["evidence_changes"]["own_events"] == []
    assert d["evidence_changes"]["vehicle_readings"] == []
    assert d["evidence_changes"]["loading_windows"] == []
    assert d["current_interval"]["is_open"] is False


def test_diff_changed_by_late_close_and_segment_shift(client):
    iid, snap = build_two_party_interval(client, )
    seal(client, "CASE-CH", "C1", iid, snap)
    # 10:20 提前闭合：终点与 B 片段删除，新增 m1b 事件
    assert m(client, "m1b", 20, 4).status_code == 201
    d = client.get("/evidence-seals/CASE-CH/diff").json()
    assert d["status"] == "changed"
    seg_changes = {(x["start"][11:16], x["change"]) for x in d["segment_changes"]}
    # 10:10 片段终点由 10:30 变 10:20（changed）；10:30 的 B 片段删除
    assert ("10:10", "changed") in seg_changes
    assert ("10:30", "removed") in seg_changes
    own = d["evidence_changes"]["own_events"]
    added = next(x for x in own if x["key"] == "m1b")
    assert added["change"] == "added" and added["sealed"] is None
    assert added["current"]["event_id"] == "m1b"


def test_diff_reopened_status(client):
    iid, snap = build_two_party_interval(client, )
    seal(client, "CASE-O", "C1", iid, snap)
    # m2 更正为超限 -> 区间重新开放
    assert client.post(
        "/corrections",
        json={
            "correction_id": "c-m2",
            "target_type": "own_event",
            "target_id": "m2",
            "expected_version": 0,
            "new_temperature": "9",
        },
    ).status_code == 201
    d = client.get("/evidence-seals/CASE-O/diff").json()
    assert d["status"] == "reopened"
    assert d["current_snapshot_id"] is None
    assert d["current_interval"] is not None
    assert d["current_interval"]["is_open"] is True
    assert d["current_interval"]["end"] is None
    # 封存片段：A 段（10:10-10:30）仍在；末段开放
    starts = {x["start"][11:16] for x in d["segment_changes"]}
    assert "10:30" in starts
    # m2 由不超限变超限（changed，版本/有效温度）
    m2d = next(
        x for x in d["evidence_changes"]["own_events"] if x["key"] == "m2"
    )
    assert m2d["change"] == "changed"
    assert "effective_temperature" in m2d["fields"]
    assert "version" in m2d["fields"]


def test_diff_disappeared_status_and_original_immutable(client):
    iid, snap = build_two_party_interval(client, )
    created = seal(client, "CASE-D", "C1", iid, snap).json()
    # 开启测温更正为不超限 -> 区间消失
    assert client.post(
        "/corrections",
        json={
            "correction_id": "c-m1",
            "target_type": "own_event",
            "target_id": "m1",
            "expected_version": 0,
            "new_temperature": "3",
        },
    ).status_code == 201
    d = client.get("/evidence-seals/CASE-D/diff").json()
    assert d["status"] == "disappeared"
    assert d["current_interval"] is None
    assert d["current_snapshot_id"] is None
    assert all(x["change"] == "removed" for x in d["segment_changes"])
    m1d = next(
        x for x in d["evidence_changes"]["own_events"] if x["key"] == "m1"
    )
    assert m1d["change"] == "changed"
    assert m1d["sealed"]["effective_temperature"] == "8.000"
    assert m1d["current"]["effective_temperature"] == "3.000"
    assert m1d["current"]["version"] == 1

    # 原包不改写、摘要不变
    got = client.get("/evidence-seals/CASE-D").json()
    assert got["content"] == created["content"]
    assert got["content_digest"] == created["content_digest"]


def test_late_events_and_corrections_never_rewrite_seal(client):
    iid, snap = build_two_party_interval(client, )
    created = seal(client, "CASE-IMM", "C1", iid, snap).json()
    # 晚到事件 + 更正 + 任意读操作
    assert m(client, "late1", 5, 2).status_code == 201
    d1 = client.get("/evidence-seals/CASE-IMM/diff").json()
    assert d1["status"] == "unchanged"  # 区间前的不超限点不影响区间
    assert any(
        x["key"] == "late1" and x["change"] == "added"
        for x in d1["evidence_changes"]["own_events"]
    )
    client.get("/cargo/C1/report")
    client.get("/cargo/C1/timeline")
    got = client.get("/evidence-seals/CASE-IMM").json()
    assert got["content"] == created["content"]


# --------------------------------------------------------------- 车载区间


def _setup_vehicle_interval(client, cargo="CV"):
    assert client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": "2026-01-01T00:00:00+00:00",
            "temp_upper_limit": "4.0",
            "initial_holder": "A",
        },
    ).status_code == 201
    assert client.post(
        "/trips", json={"trip_id": "TV", "vehicle_no": "京B9"}
    ).status_code == 201
    assert client.post(
        "/trips/TV/manifest",
        json={
            "items": [
                {
                    "cargo_id": cargo,
                    "loaded_at": "2026-01-01T09:00:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
            ]
        },
    ).status_code == 201
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr1",
            "trip_id": "TV",
            "read_at": "2026-01-01T10:00:00+00:00",
            "temperature": "7",
        },
    ).status_code == 201
    assert client.post(
        "/events",
        json={
            "event_id": "h1",
            "cargo_id": cargo,
            "occurred_at": "2026-01-01T12:00:00+00:00",
            "giver": "A",
            "receiver": "B",
        },
    ).status_code == 201
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr2",
            "trip_id": "TV",
            "read_at": "2026-01-01T14:00:00+00:00",
            "temperature": "3",
        },
    ).status_code == 201
    return first_interval(client, cargo)


def test_seal_vehicle_interval_content(client):
    iv = _setup_vehicle_interval(client)
    assert iv["interval_id"] == "veh:vr1"
    r = seal(client, "VCASE-1", "CV", iv["interval_id"], iv["snapshot_id"])
    content = r.json()["content"]
    vrs = {x["reading_id"]: x for x in content["vehicle_readings"]}
    assert set(vrs) == {"vr1", "vr2"}
    assert vrs["vr1"]["effective_temperature"] == "7.000"
    assert vrs["vr1"]["version"] == 0
    assert vrs["vr1"]["adopted"] is True
    assert vrs["vr2"]["trip_id"] == "TV"
    windows = content["loading_windows"]
    assert len(windows) == 1
    assert windows[0]["trip_id"] == "TV"
    assert windows[0]["window_version"] == 0
    assert windows[0]["loaded_at"] == "2026-01-01T09:00:00Z"
    assert [s["responsible_party"] for s in content["interval"]["segments"]] == [
        "A",
        "B",
    ]
    assert content["interval"]["start_source"] == "vehicle"
    assert content["interval"]["start_reading_id"] == "vr1"


def test_seal_vehicle_reading_supplanted_marked_in_diff(client):
    iv = _setup_vehicle_interval(client)
    seal(client, "VCASE-2", "CV", iv["interval_id"], iv["snapshot_id"])
    # 区间内新增一笔车载读数，同刻补自有测温 -> 该读数被取代
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr3",
            "trip_id": "TV",
            "read_at": "2026-01-01T11:30:00+00:00",
            "temperature": "6",
        },
    ).status_code == 201
    assert client.post(
        "/events",
        json={
            "event_id": "m9",
            "cargo_id": "CV",
            "occurred_at": "2026-01-01T11:30:00+00:00",
            "temperature": "9",
        },
    ).status_code == 201
    d = client.get("/evidence-seals/VCASE-2/diff").json()
    # 区间内的额外超限点不改变区间边界 / 片段 -> 快照不变
    assert d["status"] == "unchanged"
    vr3 = next(
        x
        for x in d["evidence_changes"]["vehicle_readings"]
        if x["key"] == "vr3"
    )
    assert vr3["change"] == "added"
    assert vr3["current"]["adopted"] is False


def test_seal_diff_after_vehicle_reading_correction_disappears(client):
    iv = _setup_vehicle_interval(client)
    created = seal(client, "VCASE-3", "CV", iv["interval_id"], iv["snapshot_id"]).json()
    # 开启读数更正为不超限 -> veh:vr1 区间消失
    r = client.post(
        "/corrections",
        json={
            "correction_id": "vc1",
            "target_type": "vehicle_reading",
            "target_id": "vr1",
            "expected_version": 0,
            "new_temperature": "2",
        },
    )
    assert r.status_code == 201, r.text
    d = client.get("/evidence-seals/VCASE-3/diff").json()
    assert d["status"] == "disappeared"
    vr1d = next(
        x
        for x in d["evidence_changes"]["vehicle_readings"]
        if x["key"] == "vr1"
    )
    assert vr1d["change"] == "changed"
    assert vr1d["fields"] == ["effective_temperature", "version"]
    assert vr1d["current"]["version"] == 1
    # 原包不被更正改写
    got = client.get("/evidence-seals/VCASE-3").json()
    old = next(
        x for x in got["content"]["vehicle_readings"] if x["reading_id"] == "vr1"
    )
    assert old["effective_temperature"] == "7.000" and old["version"] == 0
    assert got["content"] == created["content"]


def test_seal_diff_after_loading_window_correction(client):
    iv = _setup_vehicle_interval(client)
    seal(client, "VCASE-4", "CV", iv["interval_id"], iv["snapshot_id"])
    # 补一笔早于当前装车时刻的读数（当时不在车），再把窗口起点提前到 08:00
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr0",
            "trip_id": "TV",
            "read_at": "2026-01-01T08:30:00+00:00",
            "temperature": "6",
        },
    ).status_code == 201
    r = client.post(
        "/trips/TV/loading-corrections",
        json={
            "correction_id": "lc1",
            "cargo_id": "CV",
            "origin_loaded_at": "2026-01-01T09:00:00+00:00",
            "expected_version": 0,
            "new_loaded_at": "2026-01-01T08:00:00+00:00",
            "new_unloaded_at": "2026-01-01T18:00:00+00:00",
            "reason": "装车时刻录晚一小时",
        },
    )
    assert r.status_code == 201, r.text
    d = client.get("/evidence-seals/VCASE-4/diff").json()
    # 08:30 读数先开启区间 -> veh:vr1 区间消失
    assert d["status"] == "disappeared"
    changes = d["evidence_changes"]
    vr0 = next(x for x in changes["vehicle_readings"] if x["key"] == "vr0")
    assert vr0["change"] == "added" and vr0["current"]["adopted"] is True
    win = changes["loading_windows"][0]
    assert win["change"] == "changed"
    assert "loaded_at" in win["fields"]
    assert "window_version" in win["fields"]
    assert win["current"]["window_version"] == 1
    # 稳定身份不变
    assert (
        win["sealed"]["origin_loaded_at"] == win["current"]["origin_loaded_at"]
    )
