"""保管链 / 越温责任拆分单元测试。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.chain import holder_at, rebuild_chain
from app.models import Event, EventType

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def meas(eid, minute, temp):
    return Event(
        event_id=eid,
        cargo_id="C1",
        event_type=EventType.measurement,
        occurred_at=T0 + timedelta(minutes=minute),
        temperature=Decimal(temp),
    )


def handover(eid, minute, giver, receiver):
    return Event(
        event_id=eid,
        cargo_id="C1",
        event_type=EventType.handover,
        occurred_at=T0 + timedelta(minutes=minute),
        giver=giver,
        receiver=receiver,
    )


def test_simple_excursion_split_across_handover():
    # 10:10 超限开启 -> 10:30 A 交 B -> 10:50 不超限关闭
    events = [meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")]
    r = rebuild_chain("A", Decimal("5"), events)
    assert r.current_holder == "B"
    assert len(r.excursions) == 1
    ex = r.excursions[0]
    assert (ex.start, ex.end) == (
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=50),
    )
    assert ex.is_open is False
    assert [(s.start.minute, s.end.minute, s.party) for s in ex.segments] == [
        (10, 30, "A"),
        (30, 50, "B"),
    ]


def test_open_excursion_marked_open():
    r = rebuild_chain("A", Decimal("5"), [meas("m1", 10, "8")])
    ex = r.excursions[0]
    assert ex.is_open is True
    assert ex.end is None
    assert ex.segments[0].end is None
    assert ex.segments[0].party == "A"


def test_temp_equal_limit_does_not_open():
    r = rebuild_chain("A", Decimal("5"), [meas("m1", 10, "5"), meas("m2", 20, "4")])
    assert r.excursions == ()


def test_repeated_over_measurements_keep_one_interval():
    events = [meas("m1", 10, "8"), meas("m2", 20, "9"), meas("m3", 30, "4")]
    r = rebuild_chain("A", Decimal("5"), events)
    assert len(r.excursions) == 1
    assert r.excursions[0].start == T0 + timedelta(minutes=10)
    assert r.excursions[0].end == T0 + timedelta(minutes=30)


def test_multiple_handovers_split_into_more_segments():
    events = [
        meas("m1", 10, "8"),
        handover("h1", 20, "A", "B"),
        handover("h2", 30, "B", "C"),
        meas("m2", 40, "4"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    seg = r.excursions[0].segments
    assert [(s.start.minute, s.end and s.end.minute, s.party) for s in seg] == [
        (10, 20, "A"),
        (20, 30, "B"),
        (30, 40, "C"),
    ]


def test_handover_same_instant_as_open_measurement_receiver_liable():
    events = [
        meas("m1", 30, "8"),
        handover("h1", 30, "A", "B"),
        meas("m2", 40, "4"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    seg = r.excursions[0].segments
    assert [(s.start.minute, s.end.minute, s.party) for s in seg] == [(30, 40, "B")]


def test_close_same_instant_as_handover_no_zero_segment_for_giver():
    # 10:10 开启；10:30 交接 A->B，且同刻测温不超限关闭：交接时刻起归 B，
    # B 的片段为零长 -> 仅保留 A 的 [10:10,10:30)
    events = [
        meas("m1", 10, "8"),
        meas("m2", 30, "4"),
        handover("h1", 30, "A", "B"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    seg = r.excursions[0].segments
    assert [(s.start.minute, s.end.minute, s.party) for s in seg] == [(10, 30, "A")]
    assert r.current_holder == "B"


def test_two_separate_excursions():
    events = [
        meas("m1", 10, "8"),
        meas("m2", 20, "4"),
        handover("h1", 25, "A", "B"),
        meas("m3", 30, "9"),
        meas("m4", 40, "4"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    assert len(r.excursions) == 2
    assert [s.party for s in r.excursions[1].segments] == ["B"]


def test_holder_at_with_handovers():
    events = [handover("h1", 30, "A", "B"), handover("h2", 60, "B", "C")]
    assert holder_at("A", events, T0) == "A"
    assert holder_at("A", events, T0 + timedelta(minutes=30)) == "B"
    assert holder_at("A", events, T0 + timedelta(minutes=45)) == "B"
    assert holder_at("A", events, T0 + timedelta(minutes=60)) == "C"
