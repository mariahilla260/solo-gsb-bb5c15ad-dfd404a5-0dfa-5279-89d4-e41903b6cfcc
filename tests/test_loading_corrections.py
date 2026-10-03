"""装卸时段更正测试。

覆盖：
* 只改该条装卸窗口（车次 / 货物归属与稳定身份 origin_loaded_at 不变），
  车次详情展示当前窗口、原始窗口与历次更正可追溯；
* 新窗口左闭右开、起点严格早于终点；重叠校验排除该条旧窗口本身，
  跨车次（同一货物）重叠 / 同车次重叠整次拒绝，首尾相接允许；
* 乐观版本（expected_version）、版本过期、原窗口不存在、无原因 / 无时刻非法；
* 同更正编号同请求重传幂等、异请求冲突；
* 车载读数适用性按当前窗口重算（装车时刻纳入、卸车时刻排除），
  越温报告 / 责任确认 / 同时越温查询 / 车次详情反映当前窗口，
  表态历史保留并按重建后的责任片段判断有效性；
* 成功推进车次修订号（使旧装卸预检摘要失效），失败不推进、不留部分变更。
"""
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Loading, LoadingCorrection


def _reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _cargo(c, cargo_id="C1", limit="5", holder="A", start="2026-01-01T08:00:00+00:00"):
    r = c.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def _trip(c, trip_id="T1", vehicle_no="V"):
    assert c.post(
        "/trips", json={"trip_id": trip_id, "vehicle_no": vehicle_no}
    ).status_code == 201


def _manifest(c, trip_id, cargo_id, lo, un):
    r = c.post(
        f"/trips/{trip_id}/manifest",
        json={"items": [{"cargo_id": cargo_id, "loaded_at": lo, "unloaded_at": un}]},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _correct(
    c,
    trip_id,
    *,
    correction_id,
    cargo_id="C1",
    origin_loaded_at,
    expected_version,
    new_loaded_at,
    new_unloaded_at,
    reason,
):
    return c.post(
        f"/trips/{trip_id}/loading-corrections",
        json={
            "correction_id": correction_id,
            "cargo_id": cargo_id,
            "origin_loaded_at": origin_loaded_at,
            "expected_version": expected_version,
            "new_loaded_at": new_loaded_at,
            "new_unloaded_at": new_unloaded_at,
            "reason": reason,
        },
    )


def _history(c, trip_id, cargo_id, origin_loaded_at):
    return c.get(
        f"/trips/{trip_id}/loading-corrections",
        params={
            "cargo_id": cargo_id,
            "origin_loaded_at": origin_loaded_at,
        },
    )


def setup_function(_):
    _reset()


def test_correction_updates_only_that_window_and_keeps_identity():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")

        r = _correct(
            c, "T1",
            correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00",
            expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00",
            reason="装车时刻录错一小时",
        )
        assert r.status_code == 201, r.text
        ack = r.json()
        assert ack["version"] == 1
        assert ack["loaded_at"] == "2026-01-01T11:00:00Z"
        assert ack["unloaded_at"] == "2026-01-01T13:00:00Z"
        # 稳定身份与原始窗口保留
        assert ack["origin_loaded_at"] == "2026-01-01T10:00:00Z"
        assert ack["origin_unloaded_at"] == "2026-01-01T12:00:00Z"
        assert ack["affected_cargo_ids"] == ["C1"]
        corr = ack["correction"]
        assert corr["previous_loaded_at"] == "2026-01-01T10:00:00Z"
        assert corr["trip_id"] == "T1" and corr["cargo_id"] == "C1"

        detail = c.get("/trips/T1").json()
        assert len(detail["loadings"]) == 1
        loading = detail["loadings"][0]
        # 车次详情展示当前窗口 + 稳定身份 + 版本
        assert loading["loaded_at"] == "2026-01-01T11:00:00Z"
        assert loading["unloaded_at"] == "2026-01-01T13:00:00Z"
        assert loading["origin_loaded_at"] == "2026-01-01T10:00:00Z"
        assert loading["window_version"] == 1


def test_original_window_and_full_history_are_traceable():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")

        _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="r1",
        )
        _correct(
            c, "T1", correction_id="lc2",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=1,
            new_loaded_at="2026-01-01T11:30:00+00:00",
            new_unloaded_at="2026-01-01T13:30:00+00:00", reason="r2",
        )

        h = _history(c, "T1", "C1", "2026-01-01T10:00:00+00:00").json()
        assert h["window_exists"] is True
        # 原始窗口可追溯
        assert h["origin_loaded_at"] == "2026-01-01T10:00:00Z"
        assert h["origin_unloaded_at"] == "2026-01-01T12:00:00Z"
        # 当前窗口为第二次更正结果
        assert h["loaded_at"] == "2026-01-01T11:30:00Z"
        assert h["unloaded_at"] == "2026-01-01T13:30:00Z"
        assert h["version"] == 2
        versions = [row["version"] for row in h["corrections"]]
        assert versions == [1, 2]
        # 第二次更正的“旧窗口”正是第一次更正后的窗口
        second = h["corrections"][1]
        assert second["previous_loaded_at"] == "2026-01-01T11:00:00Z"
        assert second["previous_unloaded_at"] == "2026-01-01T13:00:00Z"


def test_correction_does_not_change_other_window_nor_attribution():
    with TestClient(app) as c:
        _cargo(c, "C1")
        _cargo(c, "C2")
        _trip(c, "T1")
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        # 同货物另一窗口（不重叠）
        _manifest(c, "T1", "C1", "2026-01-02T10:00:00+00:00", "2026-01-02T12:00:00+00:00")
        # 另一货物的窗口
        _manifest(c, "T1", "C2", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")

        r = _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T10:30:00+00:00",
            new_unloaded_at="2026-01-01T11:30:00+00:00", reason="x",
        )
        assert r.status_code == 201

        by_key = {
            (l["cargo_id"], l["origin_loaded_at"]): l
            for l in c.get("/trips/T1").json()["loadings"]
        }
        # 被改窗口
        changed = by_key[("C1", "2026-01-01T10:00:00Z")]
        assert changed["loaded_at"] == "2026-01-01T10:30:00Z"
        # 同货物另一窗口不动
        assert by_key[("C1", "2026-01-02T10:00:00Z")]["loaded_at"] == "2026-01-02T10:00:00Z"
        # 另一货物的窗口不动
        assert by_key[("C2", "2026-01-01T10:00:00Z")]["loaded_at"] == "2026-01-01T10:00:00Z"


def test_invalid_new_window_shape_rejected():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        # 起点 == 终点
        r = _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T13:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="x",
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_loading_window"
        # 起点晚于终点
        r = _correct(
            c, "T1", correction_id="lc2",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T14:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="x",
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_loading_window"
        # 失败不留任何更正，版本不推进
        h = _history(c, "T1", "C1", "2026-01-01T10:00:00+00:00").json()
        assert h["version"] == 0 and h["corrections"] == []
        assert c.get("/trips/T1").json()["revision"] == 1  # 仅登记 +1


def test_timezone_required_and_reason_required():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        # 缺时区
        r = _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00",
            new_unloaded_at="2026-01-01T12:00:00", reason="x",
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "timezone_required"
        # 空原因（Pydantic 422）
        r = _correct(
            c, "T1", correction_id="lc2",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T12:00:00+00:00", reason="   ",
        )
        assert r.status_code == 422


def test_overlap_excludes_self_but_rejects_others():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c, "T1")
        _trip(c, "T2")
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        _manifest(c, "T2", "C1", "2026-01-02T08:00:00+00:00", "2026-01-02T10:00:00+00:00")

        # 把新窗口改成与自身完全相同（旧窗口排除自身）：允许
        r = _correct(
            c, "T1", correction_id="lc-same",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T10:00:00+00:00",
            new_unloaded_at="2026-01-01T12:00:00+00:00", reason="仅补登记原因",
        )
        assert r.status_code == 201

        # 移动 T2 窗口与 T1 当前窗口重叠（同一货物跨车次）：整次拒绝
        r = _correct(
            c, "T2", correction_id="lc-cross",
            origin_loaded_at="2026-01-02T08:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="撞 T1",
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "loading_overlap"

        # 首尾相接允许（新窗口 [12,14) 与 T1 [10,12)）
        r = _correct(
            c, "T2", correction_id="lc-abut",
            origin_loaded_at="2026-01-02T08:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T12:00:00+00:00",
            new_unloaded_at="2026-01-01T14:00:00+00:00", reason="首尾相接",
        )
        assert r.status_code == 201


def test_stale_version_and_missing_window_and_trip_rejected():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        body = dict(
            correction_id="lc1", cargo_id="C1",
            origin_loaded_at="2026-01-01T10:00:00+00:00",
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T12:30:00+00:00", reason="x",
        )
        # 期望版本 5（当前 0）：过期
        r = c.post("/trips/T1/loading-corrections", json={**body, "expected_version": 5})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_window_version"

        # 原窗口不存在（错误的原装车时刻）
        r = c.post(
            "/trips/T1/loading-corrections",
            json={**body, "correction_id": "lc2",
                  "origin_loaded_at": "2026-01-01T10:30:00+00:00",
                  "expected_version": 0},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "loading_window_not_found"

        # 车次不存在
        r = c.post(
            "/trips/NOPE/loading-corrections",
            json={**body, "correction_id": "lc3", "expected_version": 0},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "trip_not_found"

        # 历史查询：窗口不存在但车次存在 -> window_exists=false
        h = _history(c, "T1", "C1", "2026-01-03T00:00:00+00:00")
        assert h.status_code == 200 and h.json()["window_exists"] is False


def test_idempotent_replay_and_different_request_conflict():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        kwargs = dict(
            correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="r",
        )
        assert _correct(c, "T1", **kwargs).status_code == 201
        r2 = _correct(c, "T1", **kwargs)
        assert r2.status_code == 200
        body = r2.json()
        assert body["replayed"] is True and body["version"] == 1
        assert body["loaded_at"] == "2026-01-01T11:00:00Z"

        # 同编号、异请求（改原因 / 新窗口 / 期望版本 任一）-> 冲突
        r3 = _correct(c, "T1", **{**kwargs, "reason": "不同原因"})
        assert r3.status_code == 409
        assert r3.json()["error"]["code"] == "correction_id_conflict"

        # 只有一条更正落库
        h = _history(c, "T1", "C1", "2026-01-01T10:00:00+00:00").json()
        assert [row["correction_id"] for row in h["corrections"]] == ["lc1"]


def test_vehicle_reading_applicability_recomputed_on_current_window():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        # 恰在装车时刻 10:00（纳入）与恰在卸车时刻 12:00（排除）
        assert c.post("/vehicle-readings", json={
            "reading_id": "vr-load", "trip_id": "T1",
            "read_at": "2026-01-01T10:00:00+00:00", "temperature": "9"}).status_code == 201
        assert c.post("/vehicle-readings", json={
            "reading_id": "vr-unload", "trip_id": "T1",
            "read_at": "2026-01-01T12:00:00+00:00", "temperature": "9"}).status_code == 201

        def apps():
            readings = c.get("/trips/T1").json()["readings"]
            return {
                r["reading_id"]: r["applications"][0]
                for r in readings
            }

        before = apps()
        assert before["vr-load"]["on_board"] is True and before["vr-load"]["over_limit"] is True
        assert before["vr-unload"]["on_board"] is False and before["vr-unload"]["over_limit"] is None

        # 窗口整体后移 1 小时 -> [11:00,13:00)：10:00 读数变为不在车，12:00 变为在车
        r = _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="整体后移",
        )
        assert r.status_code == 201

        after = apps()
        assert after["vr-load"]["on_board"] is False
        assert after["vr-load"]["reason"] == "not_on_board"
        assert after["vr-unload"]["on_board"] is True
        assert after["vr-unload"]["over_limit"] is True


def test_excursion_report_reflects_current_window():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T14:00:00+00:00")
        # 车载越温读数 11:00 开启区间，自有不超限 12:00 闭合
        c.post("/vehicle-readings", json={
            "reading_id": "vr1", "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00", "temperature": "9"})
        c.post("/events", json={
            "event_id": "m2", "cargo_id": "C1",
            "occurred_at": "2026-01-01T12:00:00+00:00", "temperature": "4"})
        assert len(c.get("/cargo/C1/report").json()["excursions"]) == 1

        # 窗口改到 12:30 起：11:00 的车载读数不再在车，区间消失
        r = _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T12:30:00+00:00",
            new_unloaded_at="2026-01-01T14:00:00+00:00", reason="读数在装车前",
        )
        assert r.status_code == 201
        assert c.get("/cargo/C1/report").json()["excursions"] == []

        # 时间线中该车载点不再出现（不适用于当前窗口）
        sources = {p["reading_id"] for p in c.get("/cargo/C1/timeline").json()["points"]}
        assert "vr1" not in sources


def test_stance_history_preserved_but_invalidated_after_rebuild():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T14:00:00+00:00")
        c.post("/vehicle-readings", json={
            "reading_id": "vr1", "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00", "temperature": "9"})
        c.post("/events", json={
            "event_id": "m2", "cargo_id": "C1",
            "occurred_at": "2026-01-01T12:00:00+00:00", "temperature": "4"})
        snap = c.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
        assert c.post(
            "/cargo/C1/responsibilities/veh:vr1/stances",
            json={"snapshot_id": snap, "responsible_party": "A",
                  "stance": "confirm", "stance_id": "st1"},
        ).status_code == 201

        # 更正使车载区间消失
        assert _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T12:30:00+00:00",
            new_unloaded_at="2026-01-01T14:00:00+00:00", reason="x",
        ).status_code == 201

        # 当前责任列表为空；表态历史保留但 is_current=false
        assert c.get("/cargo/C1/responsibilities").json()["intervals"] == []
        hist = c.get("/cargo/C1/responsibilities/veh:vr1/stances").json()
        assert hist["interval_exists"] is False
        assert len(hist["history"]) == 1
        assert hist["history"][0]["is_current"] is False


def test_simultaneous_excursions_reflect_current_window():
    with TestClient(app) as c:
        _cargo(c, "C1")
        _cargo(c, "C2")
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T14:00:00+00:00")
        _manifest(c, "T1", "C2", "2026-01-01T10:00:00+00:00", "2026-01-01T14:00:00+00:00")
        c.post("/vehicle-readings", json={
            "reading_id": "vr1", "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00", "temperature": "9"})
        params = {
            "start_at": "2026-01-01T00:00:00+00:00",
            "end_at": "2026-01-02T00:00:00+00:00",
        }
        segs = c.get("/trips/T1/simultaneous-excursions", params=params).json()["segments"]
        assert len(segs) == 1
        assert segs[0]["cargo_ids"] == ["C1", "C2"]

        # 只把 C1 的窗口移出读数时刻：同时越温消失（仅剩 C2，不达门槛 2）
        assert _correct(
            c, "T1", correction_id="lc1", cargo_id="C1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T12:00:00+00:00",
            new_unloaded_at="2026-01-01T14:00:00+00:00", reason="C1 后装",
        ).status_code == 201
        segs = c.get("/trips/T1/simultaneous-excursions", params=params).json()["segments"]
        assert segs == []


def test_success_bumps_revision_and_invalidates_precheck_digest():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        rev_after_manifest = c.get("/trips/T1").json()["revision"]
        assert rev_after_manifest == 1

        # 先对一个“新批次”做预检并拿到摘要（绑定当前修订号 1）
        pre = c.post(
            "/trips/T1/manifest/precheck",
            json={
                "expected_trip_revision": 1,
                "items": [{
                    "cargo_id": "C1",
                    "loaded_at": "2026-01-02T10:00:00+00:00",
                    "unloaded_at": "2026-01-02T12:00:00+00:00",
                }],
            },
        )
        assert pre.status_code == 200, pre.text
        digest = pre.json()["digest"]

        # 对既有窗口做更正 -> 修订号 +1
        r = _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T10:30:00+00:00",
            new_unloaded_at="2026-01-01T12:00:00+00:00", reason="x",
        )
        assert r.status_code == 201
        assert c.get("/trips/T1").json()["revision"] == 2

        # 旧预检摘要失效
        conf = c.post(
            "/trips/T1/manifest/confirm",
            json={
                "digest": digest,
                "items": [{
                    "cargo_id": "C1",
                    "loaded_at": "2026-01-02T10:00:00+00:00",
                    "unloaded_at": "2026-01-02T12:00:00+00:00",
                }],
            },
        )
        assert conf.status_code == 409
        assert conf.json()["error"]["code"] == "stale_precheck_digest"


def test_failed_correction_leaves_no_partial_change():
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        rev_before = c.get("/trips/T1").json()["revision"]

        # 非法（起点==终点）：当前窗口不变、无更正行、修订号不增
        r = _correct(
            c, "T1", correction_id="lc-bad",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T13:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="x",
        )
        assert r.status_code == 422

        loading = c.get("/trips/T1").json()["loadings"][0]
        assert loading["loaded_at"] == "2026-01-01T10:00:00Z"
        assert loading["window_version"] == 0
        assert c.get("/trips/T1").json()["revision"] == rev_before
        h = _history(c, "T1", "C1", "2026-01-01T10:00:00+00:00").json()
        assert h["corrections"] == []

        # 更正编号在失败后可复用（未被占用）
        r = _correct(
            c, "T1", correction_id="lc-bad",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T10:30:00+00:00",
            new_unloaded_at="2026-01-01T12:00:00+00:00", reason="合法重用编号",
        )
        assert r.status_code == 201


def test_orm_persistence_shape_after_correction():
    """直接核对 ORM：稳定身份不动、当前窗口就地更新、更正只追加。"""
    with TestClient(app) as c:
        _cargo(c)
        _trip(c)
        _manifest(c, "T1", "C1", "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00")
        assert _correct(
            c, "T1", correction_id="lc1",
            origin_loaded_at="2026-01-01T10:00:00+00:00", expected_version=0,
            new_loaded_at="2026-01-01T11:00:00+00:00",
            new_unloaded_at="2026-01-01T13:00:00+00:00", reason="x",
        ).status_code == 201

        db = SessionLocal()
        try:
            from app.timeutil import ensure_utc

            rows = list(db.query(Loading).all())
            assert len(rows) == 1  # 仍是一行（就地更新，不新增窗口）
            row = rows[0]
            assert ensure_utc(row.origin_loaded_at).isoformat() == "2026-01-01T10:00:00+00:00"
            assert ensure_utc(row.loaded_at).isoformat() == "2026-01-01T11:00:00+00:00"
            assert ensure_utc(row.unloaded_at).isoformat() == "2026-01-01T13:00:00+00:00"
            assert row.window_version == 1
            corrections = list(db.query(LoadingCorrection).all())
            assert len(corrections) == 1 and corrections[0].version == 1
        finally:
            db.close()
