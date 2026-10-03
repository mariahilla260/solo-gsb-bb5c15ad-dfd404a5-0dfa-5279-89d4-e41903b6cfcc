"""HTTP API 集成测试：校验、幂等、晚到重算、责任拆分。"""
from tests.conftest import event_ids_for


def post_measurement(client, event_id, at, temp, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": event_id,
            "cargo_id": cargo,
            "occurred_at": at,
            "temperature": str(temp),
        },
    )


def post_handover(client, event_id, at, giver, receiver, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": event_id,
            "cargo_id": cargo,
            "occurred_at": at,
            "giver": giver,
            "receiver": receiver,
        },
    )


# ------------------------------------------------------------- 基础流程


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_scenario_report(client, make_cargo):
    make_cargo(limit="5", holder="A")
    # 10:10 超限，10:30 A->B，10:50 恢复
    assert post_measurement(client, "m1", "2026-01-01T10:10:00+00:00", 8).status_code == 201
    assert post_handover(client, "h1", "2026-01-01T10:30:00+00:00", "A", "B").status_code == 201
    assert post_measurement(client, "m2", "2026-01-01T10:50:00+00:00", 4).status_code == 201

    rpt = client.get("/cargo/C1/report").json()
    assert rpt["current_holder"] == "B"
    assert len(rpt["excursions"]) == 1
    ex = rpt["excursions"][0]
    assert ex["is_open"] is False
    assert ex["start"] == "2026-01-01T10:10:00Z"
    assert ex["end"] == "2026-01-01T10:50:00Z"
    segs = ex["segments"]
    assert [(s["responsible_party"], s["end"]) for s in segs] == [
        ("A", "2026-01-01T10:30:00Z"),
        ("B", "2026-01-01T10:50:00Z"),
    ]


def test_open_interval_is_flagged(client, make_cargo):
    make_cargo(limit="5", holder="A")
    post_measurement(client, "m1", "2026-01-01T10:10:00+00:00", 9)
    rpt = client.get("/cargo/C1/report").json()
    ex = rpt["excursions"][0]
    assert ex["is_open"] is True
    assert ex["end"] is None
    assert ex["segments"][0]["end"] is None


# ------------------------------------------------------------- 校验


def test_event_before_start_rejected(client, make_cargo):
    make_cargo()
    r = post_measurement(client, "m0", "2026-01-01T09:59:59+00:00", 9)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "before_start"
    assert event_ids_for("C1") == set()


def test_wrong_giver_rejected_entire_write(client, make_cargo):
    make_cargo(holder="A")
    r = post_handover(client, "h1", "2026-01-01T11:00:00+00:00", "X", "B")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    # 既有结果不变：没有任何事件入库，当前保管方仍是 A
    assert event_ids_for("C1") == set()
    assert client.get("/cargo/C1/report").json()["current_holder"] == "A"


def test_late_handover_invalidating_existing_one_rejected(client, make_cargo):
    make_cargo(holder="A")
    # 既有：11:00 A->B，12:00 B->C
    post_handover(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    post_handover(client, "h2", "2026-01-01T12:00:00+00:00", "B", "C")
    # 晚到一条 10:30 的 A->X，将使 11:00 的交出方不再是保管方
    r = post_handover(client, "h0", "2026-01-01T10:30:00+00:00", "A", "X")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "giver_mismatch"
    # 晚到记录未生效，链路保持原样
    assert event_ids_for("C1") == {"h1", "h2"}
    assert client.get("/cargo/C1/report").json()["current_holder"] == "C"


def test_late_valid_handover_recomputes_responsibility(client, make_cargo):
    make_cargo(limit="5", holder="A")
    post_measurement(client, "m1", "2026-01-01T10:10:00+00:00", 8)
    post_handover(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    post_measurement(client, "m2", "2026-01-01T12:00:00+00:00", 4)
    # 区间 [10:10,12:00) 当前按 11:00 拆为 A/B
    # 晚到一条 10:30 的 A->C 会使 11:00 的 A->B 交出方失配 -> 拒绝
    r = post_handover(client, "h0", "2026-01-01T10:30:00+00:00", "A", "C")
    assert r.status_code == 422
    assert event_ids_for("C1") == {"m1", "h1", "m2"}

    # 晚到一条 11:30 的合法交接 B->C（不与既有交接矛盾）-> 接受并重算拆分
    r2 = post_handover(client, "h0b", "2026-01-01T11:30:00+00:00", "B", "C")
    assert r2.status_code == 201
    rpt = client.get("/cargo/C1/report").json()
    ex = rpt["excursions"][0]
    assert ex["start"] == "2026-01-01T10:10:00Z"
    assert ex["end"] == "2026-01-01T12:00:00Z"
    assert [(s["start"][11:16], s["end"][11:16], s["responsible_party"])
            for s in ex["segments"]] == [
        ("10:10", "11:00", "A"),
        ("11:00", "11:30", "B"),
        ("11:30", "12:00", "C"),
    ]
    assert rpt["current_holder"] == "C"


def test_same_timestamp_measurement_conflict_rejected(client, make_cargo):
    make_cargo()
    ts = "2026-01-01T11:00:00+00:00"
    assert post_measurement(client, "m1", ts, 8).status_code == 201
    r = post_measurement(client, "m2", ts, 9)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    assert event_ids_for("C1") == {"m1"}


def test_same_timestamp_handover_conflict_rejected(client, make_cargo):
    make_cargo(holder="A")
    ts = "2026-01-01T11:00:00+00:00"
    assert post_handover(client, "h1", ts, "A", "B").status_code == 201
    r = post_handover(client, "h2", ts, "B", "C")
    # 两笔交接同刻：沿链模拟时交出方合法，但同刻交接冲突兜底拒绝
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    assert event_ids_for("C1") == {"h1"}


def test_measurement_and_handover_can_share_timestamp(client, make_cargo):
    make_cargo(limit="5", holder="A")
    ts = "2026-01-01T11:00:00+00:00"
    assert post_measurement(client, "m1", ts, 8).status_code == 201
    assert post_handover(client, "h1", ts, "A", "B").status_code == 201
    rpt = client.get("/cargo/C1/report").json()
    assert rpt["current_holder"] == "B"
    # 同刻开启越温并交接 -> 接收方 B 自该时刻起担责
    assert [s["responsible_party"] for s in rpt["excursions"][0]["segments"]] == ["B"]


# ------------------------------------------------------------- 幂等


def test_same_id_same_content_replay_is_idempotent(client, make_cargo):
    make_cargo()
    body = {
        "event_id": "m1",
        "cargo_id": "C1",
        "occurred_at": "2026-01-01T11:00:00+00:00",
        "temperature": "8",
    }
    first = client.post("/events", json=body)
    assert first.status_code == 201
    second = client.post("/events", json=body)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert event_ids_for("C1") == {"m1"}
    # 不同时区写法表示同一时刻也视为同内容
    body["occurred_at"] = "2026-01-01T12:00:00+01:00"
    third = client.post("/events", json=body)
    assert third.status_code == 200
    assert len(client.get("/cargo/C1/events").json()) == 1


def test_same_id_different_content_rejected(client, make_cargo):
    make_cargo()
    assert post_measurement(client, "m1", "2026-01-01T11:00:00+00:00", 8).status_code == 201
    r = post_measurement(client, "m1", "2026-01-01T11:05:00+00:00", 8)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "event_id_conflict"
    # 原内容不变
    ev = client.get("/cargo/C1/events").json()[0]
    assert ev["occurred_at"] == "2026-01-01T11:00:00Z"


# ------------------------------------------------------------- 其他


def test_event_for_unknown_cargo_404(client):
    r = post_measurement(client, "m1", "2026-01-01T11:00:00+00:00", 8, cargo="NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"


def test_event_must_be_either_measurement_or_handover(client, make_cargo):
    make_cargo()
    # 既无温度又无交接双方
    r = client.post(
        "/events",
        json={
            "event_id": "x1",
            "cargo_id": "C1",
            "occurred_at": "2026-01-01T11:00:00+00:00",
        },
    )
    assert r.status_code == 422
    # 同时包含温度与双方
    r2 = client.post(
        "/events",
        json={
            "event_id": "x2",
            "cargo_id": "C1",
            "occurred_at": "2026-01-01T11:00:00+00:00",
            "temperature": "8",
            "giver": "A",
            "receiver": "B",
        },
    )
    assert r2.status_code == 422


def test_duplicate_cargo_id_rejected(client, make_cargo):
    make_cargo(cargo_id="C9")
    r = client.post(
        "/cargo",
        json={
            "cargo_id": "C9",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "cargo_exists"


def test_events_listed_in_time_order(client, make_cargo):
    make_cargo(limit="5", holder="A")
    post_measurement(client, "late", "2026-01-01T12:00:00+00:00", 3)
    post_measurement(client, "early", "2026-01-01T10:30:00+00:00", 8)
    ids = [e["event_id"] for e in client.get("/cargo/C1/events").json()]
    assert ids == ["early", "late"]


def test_boundary_temp_equal_limit_is_not_over(client, make_cargo):
    make_cargo(limit="5", holder="A")
    post_measurement(client, "m1", "2026-01-01T10:30:00+00:00", 5)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
