"""质控：同一车次多票货物**同时越温时段**查询。

覆盖：
* 多票货物按各自上限同时越温 -> 按时间排序的不重叠片段 + 当段货物编号；
* 货物集合变化即分段、相邻同集合片段合并；
* 数量门槛 min_cargo（默认 2，非法门槛 422）；
* 查询窗裁剪（左闭右开）、未闭合区间裁剪至查询终点；
* 与该车次装卸时段相交：越温发生在装车前 / 卸货后不计，左闭右开边界；
* 货物从上一车次带着未闭合区间装车 -> 自装车时刻起即越温（须用全部车次上下文重建）；
* 同刻自有测温优先（车载高温被取代的货物不进入同时段）；
* 晚到读数写入后查询反映新结果；
* 同货物同车次首尾相接两段装卸时段 -> 相邻同集合片段合并；
* 无匹配空结果；未知车次 404；终点不晚于起点、缺时区、门槛非法 422。
"""
import pytest

from app.overlap import cargo_excursion_windows, simultaneous_segments


# ------------------------------------------------------------ 纯逻辑


class _FakeLoading:
    def __init__(self, loaded_at, unloaded_at, trip_id="T1"):
        self.trip_id = trip_id
        self.loaded_at = loaded_at
        self.unloaded_at = unloaded_at


class _FakeExcursion:
    def __init__(self, start, end=None):
        self.start = start
        self.end = end

    @property
    def is_open(self):
        return self.end is None


def test_simultaneous_basic_sweep_and_split():
    # A: [1,5), B: [2,6), C: [3,4)
    windows = {
        "A": [(1, 5)],
        "B": [(2, 6)],
        "C": [(3, 4)],
    }
    segs = simultaneous_segments(windows, min_cargo=2)
    assert [(s.start, s.end, s.cargo_count) for s in segs] == [
        (2, 3, 2),
        (3, 4, 3),
        (4, 5, 2),
    ]
    assert segs[0].cargo_ids == ("A", "B")
    assert segs[1].cargo_ids == ("A", "B", "C")


def test_simultaneous_threshold_filters_and_ordering():
    windows = {"B": [(1, 4)], "A": [(1, 4)], "C": [(2, 3)]}
    assert [(s.start, s.end) for s in simultaneous_segments(windows, 3)] == [(2, 3)]
    # cargo_ids 按编号排序（B,A -> A,B）
    assert simultaneous_segments(windows, 2)[0].cargo_ids == ("A", "B")


def test_simultaneous_half_open_boundaries_do_not_overlap():
    # A [1,2), B [2,3)：首尾相接，t=2 时 A 已不在、B 刚进入 -> 无任何同段
    segs = simultaneous_segments({"A": [(1, 2)], "B": [(2, 3)]}, 2)
    assert segs == []


def test_simultaneous_adjacent_same_set_merge():
    # 同一货物（同车次首尾相接两段）+ 另一持续货物：t=3 处分段集合不变，应合并
    windows = {"A": [(1, 5)], "B": [(1, 3), (3, 5)]}
    segs = simultaneous_segments(windows, 2)
    assert len(segs) == 1
    assert (segs[0].start, segs[0].end, segs[0].cargo_ids) == (1, 5, ("A", "B"))


def test_simultaneous_zero_length_dropped():
    assert simultaneous_segments({"A": [(1, 1)], "B": [(1, 1)]}, 2) == []


def test_simultaneous_min_cargo_below_two_rejected():
    with pytest.raises(ValueError):
        simultaneous_segments({}, min_cargo=1)


def test_cargo_excursion_windows_open_clipped_to_query_end():
    loadings = [_FakeLoading(2, 10)]
    # 未闭合区间 [5, None)：与装卸时段、查询窗相交后裁至查询终点 8
    wins = cargo_excursion_windows([_FakeExcursion(5)], loadings, 0, 8)
    assert wins == [(5, 8)]


def test_cargo_excursion_windows_open_unload_before_query_end():
    loadings = [_FakeLoading(2, 6)]
    wins = cargo_excursion_windows([_FakeExcursion(5)], loadings, 0, 10)
    assert wins == [(5, 6)]


def test_cargo_excursion_windows_excludes_outside_loading():
    loadings = [_FakeLoading(2, 4)]
    # 越温区间整体在装车之前 / 卸货之后
    assert cargo_excursion_windows([_FakeExcursion(0, 1.5)], loadings, 0, 10) == []
    assert cargo_excursion_windows([_FakeExcursion(5, 7)], loadings, 0, 10) == []
    # 部分相交：左闭右开边界（1.5 开区间、4 卸货点不含）
    assert cargo_excursion_windows([_FakeExcursion(1.5, 4.5)], loadings, 0, 10) == [
        (2, 4)
    ]
    # 查询窗再裁剪
    assert cargo_excursion_windows(
        [_FakeExcursion(1.5, 4.5)], loadings, 2.5, 3.5
    ) == [(2.5, 3.5)]


# ------------------------------------------------------------ API


def _make_cargo(client, cargo_id, limit="4", started_at="2026-01-01T00:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": started_at,
            "temp_upper_limit": limit,
            "initial_holder": "A",
        },
    )
    assert r.status_code == 201, r.text


def _manifest(client, trip, items):
    r = client.post(f"/trips/{trip}/manifest", json={"items": items})
    assert r.status_code == 201, r.text
    return r


def _vr(client, rid, trip, at, temp):
    r = client.post(
        "/vehicle-readings",
        json={"reading_id": rid, "trip_id": trip, "read_at": at,
              "temperature": str(temp)},
    )
    assert r.status_code in (200, 201), r.text
    return r


def _own(client, eid, cargo, at, temp):
    r = client.post(
        "/events",
        json={"event_id": eid, "cargo_id": cargo, "occurred_at": at,
              "temperature": str(temp)},
    )
    assert r.status_code == 201, r.text
    return r


def _query(client, trip, start="2026-01-01T00:00:00+00:00",
           end="2026-01-02T00:00:00+00:00", **extra):
    params = {"start_at": start, "end_at": end, **extra}
    return client.get(f"/trips/{trip}/simultaneous-excursions", params=params)


def _setup_three_cargo_trip(client, trip="T1"):
    _make_cargo(client, "C1", "4")
    _make_cargo(client, "C2", "4")
    _make_cargo(client, "C3", "8")       # 上限更高
    assert client.post("/trips", json={"trip_id": trip, "vehicle_no": "v"}).status_code == 201
    _manifest(client, trip, [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T16:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
        {"cargo_id": "C3",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
    ])


def test_full_flow_segments_by_set_change(client):
    _setup_three_cargo_trip(client)
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00", 7)   # C1,C2
    _vr(client, "r2", "T1", "2026-01-01T10:00:00+00:00", 9)   # C1,C2,C3
    _vr(client, "r3", "T1", "2026-01-01T11:00:00+00:00", 3)   # 全部闭合
    _vr(client, "r4", "T1", "2026-01-01T12:00:00+00:00", 7)   # C1,C2 再开
    _own(client, "e-c2", "C2", "2026-01-01T13:00:00+00:00", 3)  # C2 闭合
    _vr(client, "r5", "T1", "2026-01-01T14:00:00+00:00", 7)   # C1,C2（车载再开）
    _vr(client, "r6", "T1", "2026-01-01T17:00:00+00:00", 7)   # 仅 C2（C3 限 8）

    data = _query(client, "T1").json()
    segs = [(s["start"], s["end"], s["cargo_ids"]) for s in data["segments"]]
    assert segs == [
        ("2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z", ["C1", "C2"]),
        ("2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z", ["C1", "C2", "C3"]),
        ("2026-01-01T12:00:00Z", "2026-01-01T13:00:00Z", ["C1", "C2"]),
        ("2026-01-01T14:00:00Z", "2026-01-01T16:00:00Z", ["C1", "C2"]),
    ]
    # 17:00 仅一票，被门槛过滤；每段 count 与列表一致
    assert all(s["cargo_count"] == len(s["cargo_ids"]) for s in data["segments"])


def test_min_cargo_threshold(client):
    _setup_three_cargo_trip(client)
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00", 9)
    segs = _query(client, "T1", min_cargo="3").json()["segments"]
    assert len(segs) == 1 and segs[0]["cargo_count"] == 3
    assert _query(client, "T1", min_cargo="4").json()["segments"] == []


def test_query_window_clipping(client):
    _setup_three_cargo_trip(client)
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00", 7)
    _vr(client, "r2", "T1", "2026-01-01T11:00:00+00:00", 3)
    segs = _query(
        client, "T1",
        start="2026-01-01T09:30:00+00:00",
        end="2026-01-01T10:30:00+00:00",
    ).json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-01T09:30:00Z", "2026-01-01T10:30:00Z"),
    ]


def test_open_excursion_clipped_to_query_end(client):
    _make_cargo(client, "C1", "4")
    _make_cargo(client, "C2", "4")
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    _manifest(client, "T1", [
        {"cargo_id": c,
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T20:00:00+00:00"}
        for c in ("C1", "C2")
    ])
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00", 7)  # 此后再无闭合读数
    segs = _query(
        client, "T1",
        start="2026-01-01T00:00:00+00:00",
        end="2026-01-01T12:00:00+00:00",
    ).json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-01T09:00:00Z", "2026-01-01T12:00:00Z"),
    ]


def test_excursion_before_loading_excluded_and_half_open_unload(client):
    _make_cargo(client, "C1", "4", started_at="2025-12-31T00:00:00+00:00")
    _make_cargo(client, "C2", "4", started_at="2025-12-31T00:00:00+00:00")
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    _manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T16:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T16:00:00+00:00"},
    ])
    # 装车前两票自有测温已越温（区间开放着装上车）
    _own(client, "o1", "C1", "2026-01-01T07:00:00+00:00", 9)
    _own(client, "o2", "C2", "2026-01-01T07:00:00+00:00", 9)
    # 15:00 车载 3℃ 闭合两票；16:00（卸货点，左闭右开不含）再有 7℃ 读数，
    # 两票已下车 -> 不适用
    _vr(client, "r1", "T1", "2026-01-01T15:00:00+00:00", 3)
    _vr(client, "r2", "T1", "2026-01-01T16:00:00+00:00", 7)
    segs = _query(client, "T1").json()["segments"]
    # 开放区间自装车时刻 08:00 起算（不是 07:00），15:00 闭合
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-01T08:00:00Z", "2026-01-01T15:00:00Z"),
    ]


def test_own_measurement_takes_precedence_in_segment(client):
    _make_cargo(client, "C1", "4")
    _make_cargo(client, "C2", "4")
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    _manifest(client, "T1", [
        {"cargo_id": c,
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"}
        for c in ("C1", "C2")
    ])
    _vr(client, "r0", "T1", "2026-01-01T08:30:00+00:00", 7)  # 两票开放
    # 10:00 车载 7℃，但 C1 同刻自有 3℃ -> C1 在 10:00 闭合，仅 C2 越温
    _own(client, "o1", "C1", "2026-01-01T10:00:00+00:00", 3)
    _vr(client, "r1", "T1", "2026-01-01T10:00:00+00:00", 7)
    segs = _query(client, "T1").json()["segments"]
    assert [(s["start"], s["end"], s["cargo_ids"]) for s in segs] == [
        ("2026-01-01T08:30:00Z", "2026-01-01T10:00:00Z", ["C1", "C2"]),
    ]


def test_late_reading_reflected_in_later_query(client):
    _make_cargo(client, "C1", "4")
    _make_cargo(client, "C2", "4")
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    _manifest(client, "T1", [
        {"cargo_id": c,
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"}
        for c in ("C1", "C2")
    ])
    assert _query(client, "T1").json()["segments"] == []
    # 晚到补录一笔历史时刻的高温读数 -> 下次查询即反映；
    # 区间开放，但货物 18:00 卸货，开放后沿先被装卸时段裁到 18:00
    _vr(client, "late-1", "T1", "2026-01-01T09:00:00+00:00", 9)
    segs = _query(client, "T1").json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-01T09:00:00Z", "2026-01-01T18:00:00Z"),
    ]


def test_touching_loading_segments_same_set_merged(client):
    _make_cargo(client, "C1", "4", started_at="2025-12-31T00:00:00+00:00")
    _make_cargo(client, "C2", "4", started_at="2025-12-31T00:00:00+00:00")
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    # C1 连续承运 [08,12)+[12,18)；C2 单段
    _manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
    ])
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00", 7)
    _vr(client, "r2", "T1", "2026-01-01T15:00:00+00:00", 3)
    segs = _query(client, "T1").json()["segments"]
    assert [(s["start"], s["end"], s["cargo_ids"]) for s in segs] == [
        ("2026-01-01T09:00:00Z", "2026-01-01T15:00:00Z", ["C1", "C2"]),
    ]


def test_no_manifest_empty(client, make_cargo):
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    body = _query(client, "T1").json()
    assert body["segments"] == []
    assert body["min_cargo"] == 2


def test_other_trip_readings_not_applied(client):
    _make_cargo(client, "C1", "4")
    _make_cargo(client, "C2", "4")
    for trip in ("T1", "T2"):
        assert client.post("/trips", json={"trip_id": trip, "vehicle_no": "v"}).status_code == 201
    _manifest(client, "T1", [
        {"cargo_id": c,
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T10:00:00+00:00"}
        for c in ("C1", "C2")
    ])
    _manifest(client, "T2", [
        {"cargo_id": c,
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"}
        for c in ("C1", "C2")
    ])
    # 高温读数只属于 T2；查询 T1 不应受影响
    _vr(client, "r1", "T2", "2026-01-01T13:00:00+00:00", 9)
    assert _query(client, "T1").json()["segments"] == []
    segs = _query(client, "T2").json()["segments"]
    assert len(segs) == 1 and segs[0]["cargo_ids"] == ["C1", "C2"]


# ------------------------------------------------------------ 错误


def test_unknown_trip_404(client):
    r = _query(client, "NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_end_not_after_start_422(client):
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    for start, end in [
        ("2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00"),
        ("2026-01-01T11:00:00+00:00", "2026-01-01T10:00:00+00:00"),
    ]:
        r = _query(client, "T1", start=start, end=end)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_query_window"


def test_naive_datetime_rejected(client):
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    r = _query(client, "T1", start="2026-01-01T00:00:00", end="2026-01-02T00:00:00+00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"


def test_invalid_min_cargo_422(client):
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    for bad in ("1", "0", "-2", "abc"):
        r = _query(client, "T1", min_cargo=bad)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_min_cargo"


def test_missing_parameter_422(client):
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    r = client.get(
        "/trips/T1/simultaneous-excursions",
        params={"end_at": "2026-01-02T00:00:00+00:00"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "missing_parameter"
