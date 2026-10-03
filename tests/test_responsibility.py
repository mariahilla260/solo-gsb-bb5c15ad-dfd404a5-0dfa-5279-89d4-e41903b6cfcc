"""责任确认 API 集成测试。

覆盖：
* 查询：区间/结束事件ID、责任片段、快照标识、最新有效表态、汇总状态；
* 提交：确认 / 异议（理由必填）、责任方须在当前片段、快照过期拒绝；
* 幂等：同编号同内容重传返回原记录（即使快照已失效）、异内容冲突；
* 改口：新编号、历次表态保留、历史接口可查；
* 晚到事件重建：变化片段上的旧表态失效（历史仍在），未变化片段继续有效；
* 开放区间 / 区间不存在拒绝；与事件写入的串行化（同货物行锁）。
"""
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app


def setup_cargo(client, cargo="C1", limit="5", holder="A", start="2026-01-01T10:00:00+00:00"):
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


def closed_interval(client, cargo="C1", idx=0):
    r = client.get(f"/cargo/{cargo}/responsibilities")
    assert r.status_code == 200
    intervals = r.json()["intervals"]
    assert len(intervals) > idx
    return intervals[idx]


def build_excursion(client):
    """10:10 超限(A) -> 10:30 A交B -> 10:50 恢复。返回区间视图。"""
    setup_cargo(client)
    assert m(client, "m1", 10, 8).status_code == 201
    assert h(client, "h1", 30, "A", "B").status_code == 201
    assert m(client, "m2", 50, 4).status_code == 201
    return closed_interval(client)


def stance(client, iid, body, cargo="C1"):
    return client.post(
        f"/cargo/{cargo}/responsibilities/{iid}/stances", json=body
    )


# --------------------------------------------------------------- 查询


def test_closed_interval_query_shape(client):
    iv = build_excursion(client)
    assert iv["interval_id"] == "m1"          # 区间标识 = 开启测温事件ID
    assert iv["start_event_id"] == "m1"
    assert iv["end_event_id"] == "m2"         # 结束测温事件ID
    assert iv["start"] == "2026-01-01T10:10:00Z"
    assert iv["end"] == "2026-01-01T10:50:00Z"
    assert iv["snapshot_id"].startswith("sha256:")
    assert len(iv["snapshot_id"]) == 7 + 64
    assert [(s["start"][11:16], s["end"][11:16], s["responsible_party"])
            for s in iv["segments"]] == [
        ("10:10", "10:30", "A"),
        ("10:30", "10:50", "B"),
    ]
    assert iv["latest_stances"] == []
    assert iv["summary"] == "pending"


def test_snapshot_deterministic_and_sensitive_to_segments(client):
    iv = build_excursion(client)
    # 重新查询（无新事件）-> 快照稳定
    again = closed_interval(client)
    assert again["snapshot_id"] == iv["snapshot_id"]


def test_open_interval_not_listed_and_submit_rejected(client):
    setup_cargo(client)
    m(client, "m1", 10, 9)  # 开放越温
    rpt = client.get("/cargo/C1/responsibilities").json()
    assert rpt["intervals"] == []
    # 按开启事件直查 -> 422 interval_open
    r = client.get("/cargo/C1/responsibilities/m1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "interval_open"
    # 对开放区间表态 -> 422
    r2 = stance(
        client, "m1",
        {"snapshot_id": "sha256:" + "0" * 64, "responsible_party": "A",
         "stance": "confirm", "stance_id": "s1"},
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "interval_open"


def test_interval_not_found(client):
    setup_cargo(client)
    m(client, "m1", 10, 3)  # 未越温
    r = client.get("/cargo/C1/responsibilities/m1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "interval_not_found"
    r2 = stance(
        client, "m1",
        {"snapshot_id": "sha256:" + "0" * 64, "responsible_party": "A",
         "stance": "confirm", "stance_id": "s1"},
    )
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "interval_not_found"


def test_responsibilities_unknown_cargo_404(client):
    r = client.get("/cargo/NOPE/responsibilities")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"


# --------------------------------------------------------------- 提交


def test_all_confirmed_summary(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    assert stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                                 "stance": "confirm", "stance_id": "sa"}).status_code == 201
    r = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                              "stance": "confirm", "stance_id": "sb"})
    assert r.status_code == 201
    iv2 = closed_interval(client)
    assert iv2["summary"] == "all_confirmed"
    assert {s["responsible_party"] for s in iv2["latest_stances"]} == {"A", "B"}
    assert all(s["stance"] == "confirm" for s in iv2["latest_stances"])


def test_dispute_requires_reason_and_confirm_rejects_reason(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    # 异议无理由 -> 422，不落库
    r = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                              "stance": "dispute", "stance_id": "s1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "dispute_reason_required"
    # 空白理由同样拒绝
    r0 = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                               "stance": "dispute", "reason": "   ", "stance_id": "s1"})
    assert r0.status_code == 422
    # 确认带理由 -> 422
    r1 = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                               "stance": "confirm", "reason": "无理由也要写", "stance_id": "s2"})
    assert r1.status_code == 422
    assert r1.json()["error"]["code"] == "bad_stance_shape"
    # 未知取值 -> 422
    r2 = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                               "stance": "maybe", "stance_id": "s3"})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "bad_stance_shape"
    # 合法异议 -> 201，汇总 has_dispute
    r3 = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                               "stance": "dispute", "reason": "交接前温控正常",
                               "stance_id": "s4"})
    assert r3.status_code == 201
    iv2 = closed_interval(client)
    assert iv2["summary"] == "has_dispute"
    b = next(s for s in iv2["latest_stances"] if s["responsible_party"] == "B")
    assert b["stance"] == "dispute"
    assert b["reason"] == "交接前温控正常"
    assert b["segments"][0]["responsible_party"] == "B"


def test_party_must_be_in_current_segment(client):
    iv = build_excursion(client)
    r = stance(client, "m1", {"snapshot_id": iv["snapshot_id"],
                              "responsible_party": "Z仓库",
                              "stance": "confirm", "stance_id": "s1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "party_not_in_segment"


def test_stale_snapshot_rejected_and_not_persisted(client):
    iv = build_excursion(client)
    stale = iv["snapshot_id"]
    # 晚到一笔事件不改变区间（区间外的不超限测温）——快照不变；这里先制造真正变化：
    # 补一笔 10:40 B->C 的合法交接，责任片段由 A/B 变为 A/B/C
    assert h(client, "h2", 40, "B", "C").status_code == 201
    iv2 = closed_interval(client)
    assert iv2["snapshot_id"] != stale

    r = stance(client, "m1", {"snapshot_id": stale, "responsible_party": "A",
                              "stance": "confirm", "stance_id": "s1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_snapshot"
    # 过期拒绝不落库
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["history"] == []


# --------------------------------------------------------------- 幂等 / 改口


def test_same_stance_id_same_content_replays_original_even_when_stale(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    body = {"snapshot_id": snap, "responsible_party": "A",
            "stance": "confirm", "stance_id": "s1"}
    first = stance(client, "m1", body)
    assert first.status_code == 201
    # 晚到事件改变责任片段 -> 旧快照失效
    assert h(client, "h2", 40, "B", "C").status_code == 201
    # 同编号同内容重传：仍返回原记录（200 replayed），即使该表态已失效
    again = stance(client, "m1", body)
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["stance"]["stance_id"] == "s1"
    assert again.json()["stance"]["snapshot_id"] == snap
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1  # 没有新增行


def test_same_stance_id_different_content_conflicts(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    assert stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                                 "stance": "confirm", "stance_id": "s1"}).status_code == 201
    r = stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                              "stance": "dispute", "reason": "改主意", "stance_id": "s1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stance_id_conflict"
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1
    assert hist["history"][0]["stance"] == "confirm"  # 原记录不变


def test_change_of_mind_uses_new_id_and_history_kept(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                          "stance": "dispute", "reason": "温控设备无异常",
                          "stance_id": "s-dispute"})
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                          "stance": "confirm", "stance_id": "s-confirm"})
    iv2 = closed_interval(client)
    # 最新有效表态是改口后的确认
    b = next(s for s in iv2["latest_stances"] if s["responsible_party"] == "B")
    assert b["stance_id"] == "s-confirm"
    assert b["stance"] == "confirm"
    # 历史中两次表态都在，旧异议标记 superseded
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert [x["stance_id"] for x in hist["history"]] == ["s-dispute", "s-confirm"]
    assert hist["history"][0]["superseded"] is True
    assert hist["history"][1]["superseded"] is False
    assert all(x["is_current"] for x in hist["history"])


# ----------------------------------------------------- 晚到事件 / 失效


def test_late_event_changed_segments_invalidates_only_affected_stances(client):
    iv = build_excursion(client)  # A[10:10,10:30) B[10:30,10:50)
    snap = iv["snapshot_id"]
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                          "stance": "confirm", "stance_id": "sa"})
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                          "stance": "confirm", "stance_id": "sb"})
    # 晚到 10:20 A->C：合法（当时保管方是 A）；其后 10:30 的 A->B 将交出方失配
    # -> 该写入整体拒绝，链路不变。改用会重排责任且不矛盾的晚到事件：
    # 10:40 B->C（合法）：A 的片段不变，B 的片段由 [10:30,10:50) 缩为 [10:30,10:40)
    assert h(client, "h2", 40, "B", "C").status_code == 201

    iv2 = closed_interval(client)
    parties = [s["responsible_party"] for s in iv2["segments"]]
    assert parties == ["A", "B", "C"]
    # A 片段未变化 -> 表态继续有效；B 片段变化 -> 失效；C 未表态
    latest = {s["responsible_party"]: s for s in iv2["latest_stances"]}
    assert set(latest) == {"A"}
    assert iv2["summary"] == "pending"

    # 历史仍可查：sb 失效（is_current=false），sa 仍有效
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    flags = {x["stance_id"]: x["is_current"] for x in hist["history"]}
    assert flags == {"sa": True, "sb": False}
    assert hist["current_snapshot_id"] == iv2["snapshot_id"]


def test_late_event_shifting_boundaries_invalidates_all(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                          "stance": "confirm", "stance_id": "sa"})
    # 晚到 10:40 一笔超限测温：区间仍 [10:10,10:50) 闭合，片段不变（无新交接）
    assert m(client, "m3", 40, 9).status_code == 201
    iv2 = closed_interval(client)
    assert iv2["snapshot_id"] == snap  # 边界/片段都没变
    assert iv2["summary"] == "pending"

    # 晚到更早的开启测温改变边界：在 m1 之前补 10:05 超限测温 m0
    # -> m1 变为区间内重复超限测温，区间开启点前移，区间标识变为 m0
    assert m(client, "m0", 5, 9).status_code == 201
    rpt = client.get("/cargo/C1/responsibilities").json()["intervals"]
    assert [x["interval_id"] for x in rpt] == ["m0"]
    iv3 = rpt[0]
    assert iv3["start"] == "2026-01-01T10:05:00Z"
    assert iv3["snapshot_id"] != snap
    # 旧表态历史仍在，且标记失效；区间头部标识不存在
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["interval_exists"] is False
    assert hist["current_snapshot_id"] is None
    assert hist["history"][0]["is_current"] is False


def test_invalidated_party_can_reconfirm_under_new_snapshot(client):
    iv = build_excursion(client)
    old_snap = iv["snapshot_id"]
    stance(client, "m1", {"snapshot_id": old_snap, "responsible_party": "B",
                          "stance": "confirm", "stance_id": "sb-old"})
    h(client, "h2", 40, "B", "C")
    iv2 = closed_interval(client)
    # B 用新快照重新确认
    r = stance(client, "m1", {"snapshot_id": iv2["snapshot_id"],
                              "responsible_party": "B", "stance": "confirm",
                              "stance_id": "sb-new"})
    assert r.status_code == 201
    iv3 = closed_interval(client)
    latest = {s["responsible_party"]: s["stance_id"] for s in iv3["latest_stances"]}
    assert latest.get("B") == "sb-new"
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    by_id = {x["stance_id"]: x for x in hist["history"]}
    assert by_id["sb-old"]["is_current"] is False
    assert by_id["sb-old"]["superseded"] is False  # 失效表态不算被改口
    assert by_id["sb-new"]["is_current"] is True


def test_disputed_then_reconfirmed_summary_transitions(client):
    iv = build_excursion(client)
    snap = iv["snapshot_id"]
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "A",
                          "stance": "confirm", "stance_id": "sa"})
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                          "stance": "dispute", "reason": "有疑问", "stance_id": "sb1"})
    assert closed_interval(client)["summary"] == "has_dispute"
    # B 改口确认；A 已确认 -> 全确认
    stance(client, "m1", {"snapshot_id": snap, "responsible_party": "B",
                          "stance": "confirm", "stance_id": "sb2"})
    assert closed_interval(client)["summary"] == "all_confirmed"


# --------------------------------------------------------------- 其他


def test_only_closed_intervals_listed_with_two_excursions(client):
    setup_cargo(client, holder="A")
    # 第一段闭合区间
    m(client, "m1", 10, 8); m(client, "m2", 20, 4)
    h(client, "h1", 25, "A", "B")
    # 第二段开放区间（不应出现在责任确认列表）
    m(client, "m3", 30, 9)
    rpt = client.get("/cargo/C1/responsibilities").json()
    assert [x["interval_id"] for x in rpt["intervals"]] == ["m1"]


def test_stance_rows_survive_rejected_event_write(client):
    # 事件写入的“整体拒绝”不得影响既有表态
    iv = build_excursion(client)
    stance(client, "m1", {"snapshot_id": iv["snapshot_id"],
                          "responsible_party": "A", "stance": "confirm",
                          "stance_id": "sa"})
    # 晚到 10:20 A->X 使 10:30 的 A->B 失配 -> 整体拒绝
    r = h(client, "hx", 20, "A", "X")
    assert r.status_code == 422
    # 快照不变，表态仍有效
    iv2 = closed_interval(client)
    assert iv2["snapshot_id"] == iv["snapshot_id"]
    assert iv2["latest_stances"][0]["stance_id"] == "sa"


def test_serialized_with_event_write(client):
    """表态与事件写入共用货物行锁：串行化下快照校验始终基于最新链。

    这里用顺序操作验证契约；真正多线程压测见 test_concurrency.py 风格。
    """
    iv = build_excursion(client)
    stale = iv["snapshot_id"]
    # 事件写入提交后，旧快照立即不可用
    h(client, "h2", 40, "B", "C")
    r = stance(client, "m1", {"snapshot_id": stale, "responsible_party": "C",
                              "stance": "confirm", "stance_id": "sc"})
    assert r.status_code == 409  # stale_snapshot，而非把 C 当成新责任方写入
