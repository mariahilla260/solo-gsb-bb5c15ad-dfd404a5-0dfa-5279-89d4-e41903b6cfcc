"""快照标识与表态有效性的纯逻辑单元测试。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import responsibility as resp
from app.chain import rebuild_chain
from app.chain import interval_id
from app.models import Event, EventType, IntervalSource, Stance, StanceKind

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def meas(eid, minute, temp):
    return Event(
        event_id=eid, cargo_id="C1", event_type=EventType.measurement,
        occurred_at=T0 + timedelta(minutes=minute), temperature=Decimal(temp),
    )


def handover(eid, minute, giver, receiver):
    return Event(
        event_id=eid, cargo_id="C1", event_type=EventType.handover,
        occurred_at=T0 + timedelta(minutes=minute), giver=giver, receiver=receiver,
    )


def build(events):
    r = rebuild_chain("A", Decimal("5"), events)
    return r.excursions[0]


def test_snapshot_stable_for_identical_shape():
    ex1 = build([meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")])
    ex2 = build([meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")])
    assert resp.snapshot_id(ex1) == resp.snapshot_id(ex2)
    assert resp.snapshot_id(ex1).startswith("sha256:")
    assert len(resp.snapshot_id(ex1)) == 71


def test_snapshot_changes_when_segments_change():
    ex1 = build([meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")])
    ex2 = build([
        meas("m1", 10, "8"),
        handover("h1", 30, "A", "B"),
        handover("h2", 40, "B", "C"),
        meas("m2", 50, "4"),
    ])
    assert resp.snapshot_id(ex1) != resp.snapshot_id(ex2)


def test_snapshot_changes_when_boundary_changes():
    ex1 = build([meas("m1", 10, "8"), meas("m2", 50, "4")])
    # 更早的开启测温 -> 区间起点变化
    ex2 = build([meas("m0", 5, "9"), meas("m1", 10, "8"), meas("m2", 50, "4")])
    assert ex2.start_event_id == "m0"
    assert resp.snapshot_id(
        type(ex1)(ex1.start, ex1.end, ex1.segments)
    ) != resp.snapshot_id(ex2)


def test_attested_segments_per_party():
    ex = build([
        meas("m1", 10, "8"),
        handover("h1", 30, "A", "B"),
        handover("h2", 40, "B", "C"),
        meas("m2", 50, "4"),
    ])
    a = resp.attested_segments_json(ex, "A")
    b = resp.attested_segments_json(ex, "B")
    c = resp.attested_segments_json(ex, "C")
    assert a[0] == "[" and '"A"' in a and '"B"' not in a
    assert '"B"' in b and '"A"' not in b
    assert a != b != c
    # 责任方不在片段中 -> 空列表
    assert resp.attested_segments_json(ex, "Z") == "[]"


def _stance(party, ex, sid="s1", kind=StanceKind.confirm, reason=None, at=T0):
    return Stance(
        stance_id=sid, cargo_id="C1", interval_key=interval_id(ex),
        opener_source=IntervalSource.own,
        start_event_id=ex.start_event_id, vehicle_reading_id=None,
        responsible_party=party, kind=kind, reason=reason,
        snapshot_id=resp.snapshot_id(ex),
        attested_segments=resp.attested_segments_json(ex, party),
        created_at=at,
    )


def test_stance_current_when_party_segment_unchanged_only():
    ex = build([meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")])
    s_a = _stance("A", ex)
    s_b = _stance("B", ex)

    # 插入 10:40 B->C：A 的片段不变，B 的片段 [10:30,10:50) 变为 [10:30,10:40)
    ex2 = build([
        meas("m1", 10, "8"),
        handover("h1", 30, "A", "B"),
        handover("h2", 40, "B", "C"),
        meas("m2", 50, "4"),
    ])
    assert resp.is_stance_current(s_a, ex2) is True
    assert resp.is_stance_current(s_b, ex2) is False


def test_stance_invalid_when_interval_disappears_or_reopens():
    ex = build([meas("m1", 10, "8"), meas("m2", 50, "4")])
    s = _stance("A", ex)
    # 区间不存在
    assert resp.is_stance_current(s, None) is False
    # 开放区间
    open_ex = rebuild_chain("A", Decimal("5"), [meas("m1", 10, "8")]).excursions[0]
    assert resp.is_stance_current(s, open_ex) is False


def test_summary_all_confirmed_has_dispute_pending():
    ex = build([meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")])
    v0 = resp.build_interval_view("C1", ex, [])
    assert v0.summary == resp.PENDING

    s_a = _stance("A", ex, sid="sa")
    v1 = resp.build_interval_view("C1", ex, [s_a])
    assert v1.summary == resp.PENDING  # B 未表态

    s_b_dispute = _stance("B", ex, sid="sb1", kind=StanceKind.dispute, reason="r")
    v2 = resp.build_interval_view("C1", ex, [s_a, s_b_dispute])
    assert v2.summary == resp.HAS_DISPUTE

    # B 改口确认（新编号）-> 全确认
    s_b_ok = _stance(
        "B", ex, sid="sb2", at=T0 + timedelta(minutes=1)
    )
    v3 = resp.build_interval_view("C1", ex, [s_a, s_b_dispute, s_b_ok])
    assert v3.summary == resp.ALL_CONFIRMED
    b = next(x for x in v3.stances if x.responsible_party == "B")
    assert b.stance_id == "sb2"
