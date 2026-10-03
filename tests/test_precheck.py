"""装卸前温控兼容性预检与确认 API 集成测试。

覆盖：
* 预检分段：既有装卸 ∪ 本批拟登记叠加出的各时段货物集合、温度上限差异、
  既有车载读数覆盖（有效温度与版本）与潜在越温风险（含同刻自有测温取代标记）；
* 预检只读：不写入任何装卸记录；
* 预期车次修订号校验（过期拒绝）；未知车次 / 货物、非法时段明确报错；
* 确认携预检摘要：通过后沿用批量原子校验（任一时段冲突整批不落库）；
* 摘要过期：车次修订号变化（装卸登记 / 车载读数写入 / 车载读数更正）、
  摘要被挪用到其他批次，一律拒绝且不留部分记录；
* 车次修订号随装卸登记 / 读数写入 / 读数更正单调递增并在车次查询中可见。
"""
from app.precheck import BoardWindow, boarding_segments


def post_trip(client, trip_id="T1", vehicle_no="京A-001"):
    return client.post(
        "/trips", json={"trip_id": trip_id, "vehicle_no": vehicle_no}
    )


def post_manifest(client, trip_id, items):
    return client.post(f"/trips/{trip_id}/manifest", json={"items": items})


def post_precheck(client, trip_id, items, expected=0):
    return client.post(
        f"/trips/{trip_id}/manifest/precheck",
        json={"expected_trip_revision": expected, "items": items},
    )


def post_confirm(client, trip_id, items, digest):
    return client.post(
        f"/trips/{trip_id}/manifest/confirm",
        json={"digest": digest, "items": items},
    )


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


def make_cargo(client, cargo_id, limit="4.0", holder="北京冷库"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": "2026-01-01T08:00:00+00:00",
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


W1 = {  # 既有：C1 09:00-18:00
    "cargo_id": "C1",
    "loaded_at": "2026-01-01T09:00:00+00:00",
    "unloaded_at": "2026-01-01T18:00:00+00:00",
}
P2 = {  # 拟登记：C2 09:00-18:00
    "cargo_id": "C2",
    "loaded_at": "2026-01-01T09:00:00+00:00",
    "unloaded_at": "2026-01-01T18:00:00+00:00",
}
P3 = {  # 拟登记：C3 12:00-14:00
    "cargo_id": "C3",
    "loaded_at": "2026-01-01T12:00:00+00:00",
    "unloaded_at": "2026-01-01T14:00:00+00:00",
}


def setup_trip_with_reading(client):
    """车次 T1：C1（上限 4）已登记 09:00-18:00；10:00 车载读数 7.0。

    返回当前车次修订号（登记 1 次 + 读数 1 笔 -> 2）。
    """
    make_cargo(client, "C1", "4.0")
    make_cargo(client, "C2", "8.0")
    make_cargo(client, "C3", "4.0")
    post_trip(client)
    assert post_manifest(client, "T1", [W1]).status_code == 201
    assert post_reading(
        client, "vr-1", "T1", "2026-01-01T10:00:00+00:00", "7.0"
    ).status_code == 201
    return 2


# ------------------------------------------------------------- 预检分析


def test_precheck_segments_limits_coverage_and_risks(client):
    revision = setup_trip_with_reading(client)
    r = post_precheck(client, "T1", [P2, P3], expected=revision)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["trip_revision"] == revision
    assert body["digest"].startswith("sha256:")

    segs = body["segments"]
    # 既有 [09,18) C1 ∪ 拟登记 [09,18) C2、[12,14) C3 -> 三段
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-01T09:00:00Z", "2026-01-01T12:00:00Z"),
        ("2026-01-01T12:00:00Z", "2026-01-01T14:00:00Z"),
        ("2026-01-01T14:00:00Z", "2026-01-01T18:00:00Z"),
    ]
    assert [s["cargo_ids"] for s in segs] == [
        ["C1", "C2"],
        ["C1", "C2", "C3"],
        ["C1", "C2"],
    ]
    assert [s["proposed_cargo_ids"] for s in segs] == [
        ["C2"],
        ["C2", "C3"],
        ["C2"],
    ]
    # 温度上限差异：第一段 {4, 8} -> min 4 / max 8 / 极差 4；第二段 {4,8,4} 同
    assert segs[0]["temp_limit_min"] == "4.000"
    assert segs[0]["temp_limit_max"] == "8.000"
    assert segs[0]["temp_limit_spread"] == "4.000"
    # 既有车载读数覆盖：10:00 的 vr-1 落在第一段（左闭右开）
    assert [rd["reading_id"] for rd in segs[0]["readings"]] == ["vr-1"]
    assert segs[0]["readings"][0]["effective_temperature"] == "7.000"
    assert segs[0]["readings"][0]["version"] == 0
    assert segs[0]["reading_count"] == 1
    assert segs[1]["reading_count"] == 0
    # 潜在越温风险：7.0 > C1 上限 4（C2 上限 8 不越）
    assert [(x["cargo_id"], x["reading_id"]) for x in segs[0]["risks"]] == [
        ("C1", "vr-1")
    ]
    risk = segs[0]["risks"][0]
    assert risk["temp_upper_limit"] == "4.000"
    assert risk["effective_temperature"] == "7.000"
    assert risk["supplanted_by_own"] is False


def test_precheck_does_not_write_loadings(client):
    revision = setup_trip_with_reading(client)
    before = client.get("/trips/T1").json()["loadings"]
    r = post_precheck(client, "T1", [P2, P3], expected=revision)
    assert r.status_code == 200, r.text
    after = client.get("/trips/T1").json()["loadings"]
    assert after == before  # 预检不写入任何装卸记录
    assert [l["cargo_id"] for l in after] == ["C1"]


def test_precheck_supplanted_by_own_flag(client):
    revision = setup_trip_with_reading(client)
    # C2 在 10:00 有自有测温 -> 同刻车载读数对 C2 被取代
    r = client.post(
        "/events",
        json={
            "event_id": "c2-own-10",
            "cargo_id": "C2",
            "occurred_at": "2026-01-01T10:00:00+00:00",
            "temperature": "9.5",
        },
    )
    assert r.status_code == 201, r.text
    # 把 C2 的下限调低不现实（货物上限创建后不变），改用上限 4 的 C3 在 10:00
    # 有自有测温来验证取代标记：C3 拟登记 09:00-18:00，与 vr-1 同时段
    r = client.post(
        "/events",
        json={
            "event_id": "c3-own-10",
            "cargo_id": "C3",
            "occurred_at": "2026-01-01T10:00:00+00:00",
            "temperature": "3.0",
        },
    )
    assert r.status_code == 201, r.text
    p3_full = dict(P3, loaded_at=W1["loaded_at"], unloaded_at=W1["unloaded_at"])
    r = post_precheck(client, "T1", [p3_full], expected=revision)
    assert r.status_code == 200, r.text
    risks = r.json()["segments"][0]["risks"]
    # 7.0 > C1(4) 与 C3(4)：两条风险；C3 在 10:00 有自有测温 -> 取代标记
    by_cargo = {x["cargo_id"]: x for x in risks}
    assert by_cargo["C1"]["supplanted_by_own"] is False
    assert by_cargo["C3"]["supplanted_by_own"] is True


def test_precheck_reading_coverage_uses_effective_value(client):
    revision = setup_trip_with_reading(client)
    # 更正 vr-1：7.0 -> 2.0（版本 1）；覆盖与风险按有效值展示 / 判定
    r = client.post(
        "/corrections",
        json={
            "correction_id": "corr-1",
            "target_type": "vehicle_reading",
            "target_id": "vr-1",
            "expected_version": 0,
            "new_temperature": "2.0",
        },
    )
    assert r.status_code == 201, r.text
    revision += 1  # 车载读数更正使车次修订号 +1
    r = post_precheck(client, "T1", [P2], expected=revision)
    assert r.status_code == 200, r.text
    seg = r.json()["segments"][0]
    assert seg["readings"][0]["effective_temperature"] == "2.000"
    assert seg["readings"][0]["version"] == 1
    assert seg["risks"] == []  # 2.0 不超任何上限


def test_precheck_left_closed_right_open(client):
    """读数恰在时段边界：左闭（装入时刻在读数覆盖内）右开（卸货时刻不在）。"""
    make_cargo(client, "C1", "4.0")
    make_cargo(client, "C2", "4.0")
    post_trip(client)
    # 既有 C1 [09:00, 12:00)；读数恰在 12:00（C1 卸货时刻）与 09:00（装入时刻）
    assert post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T09:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ]).status_code == 201
    post_reading(client, "vr-open", "T1", "2026-01-01T09:00:00+00:00", "7.0")
    post_reading(client, "vr-close", "T1", "2026-01-01T12:00:00+00:00", "7.0")
    revision = 3
    # 拟登记 C2 [12:00, 18:00)：与 C1 首尾相接
    r = post_precheck(client, "T1", [
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
    ], expected=revision)
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    assert [s["cargo_ids"] for s in segs] == [["C1"], ["C2"]]
    # 09:00 读数落在 C1 段（左闭）；12:00 读数落在 C2 段（右开，不属于 C1 段）
    assert [rd["reading_id"] for rd in segs[0]["readings"]] == ["vr-open"]
    assert [rd["reading_id"] for rd in segs[1]["readings"]] == ["vr-close"]
    assert [x["cargo_id"] for x in segs[0]["risks"]] == ["C1"]
    assert [x["cargo_id"] for x in segs[1]["risks"]] == ["C2"]


# ------------------------------------------------------------- 预检报错


def test_precheck_unknown_trip(client):
    make_cargo(client, "C1")
    r = post_precheck(client, "NOPE", [P2], expected=0)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_precheck_unknown_cargo(client):
    make_cargo(client, "C1")
    post_trip(client)
    r = post_precheck(client, "T1", [P2], expected=0)  # C2 不存在
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"


def test_precheck_invalid_window(client):
    make_cargo(client, "C1")
    post_trip(client)
    r = post_precheck(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ], expected=0)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"


def test_precheck_stale_expected_revision(client):
    revision = setup_trip_with_reading(client)
    r = post_precheck(client, "T1", [P2], expected=revision - 1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_trip_revision"


# ------------------------------------------------------------- 确认登记


def test_confirm_happy_path_registers_and_bumps_revision(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2, P3], expected=revision).json()[
        "digest"
    ]
    r = post_confirm(client, "T1", [P2, P3], digest)
    assert r.status_code == 201, r.text
    assert {l["cargo_id"] for l in r.json()["items"]} == {"C2", "C3"}
    detail = client.get("/trips/T1").json()
    assert {l["cargo_id"] for l in detail["loadings"]} == {"C1", "C2", "C3"}
    assert detail["revision"] == revision + 1  # 确认登记使修订号 +1


def test_confirm_stale_digest_after_other_manifest(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2], expected=revision).json()[
        "digest"
    ]
    # 预检后另一批装卸登记上车（车次修订号变化）-> 摘要过期
    make_cargo(client, "C9", "6.0")
    assert post_manifest(client, "T1", [
        {"cargo_id": "C9",
         "loaded_at": "2026-01-02T09:00:00+00:00",
         "unloaded_at": "2026-01-02T18:00:00+00:00"},
    ]).status_code == 201
    r = post_confirm(client, "T1", [P2], digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"
    # 整批不落库
    assert {l["cargo_id"] for l in client.get("/trips/T1").json()["loadings"]} == {
        "C1",
        "C9",
    }


def test_confirm_stale_digest_after_new_reading(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2], expected=revision).json()[
        "digest"
    ]
    # 预检后该车次新增车载读数（读数版本集合变化）-> 摘要过期
    assert post_reading(
        client, "vr-late", "T1", "2026-01-01T11:00:00+00:00", "5.0"
    ).status_code == 201
    r = post_confirm(client, "T1", [P2], digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"
    assert {l["cargo_id"] for l in client.get("/trips/T1").json()["loadings"]} == {
        "C1"
    }


def test_confirm_stale_digest_after_reading_correction(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2], expected=revision).json()[
        "digest"
    ]
    # 预检后更正车载读数（读数版本变化）-> 摘要过期
    assert client.post(
        "/corrections",
        json={
            "correction_id": "corr-stale",
            "target_type": "vehicle_reading",
            "target_id": "vr-1",
            "expected_version": 0,
            "new_temperature": "2.0",
        },
    ).status_code == 201
    r = post_confirm(client, "T1", [P2], digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"
    assert {l["cargo_id"] for l in client.get("/trips/T1").json()["loadings"]} == {
        "C1"
    }


def test_confirm_digest_bound_to_items(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2], expected=revision).json()[
        "digest"
    ]
    # 摘要对批次内容敏感：挪用到其他时段即视为过期
    r = post_confirm(client, "T1", [P3], digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"
    assert {l["cargo_id"] for l in client.get("/trips/T1").json()["loadings"]} == {
        "C1"
    }


def test_confirm_overlap_rejected_after_digest_passes(client):
    """摘要通过仍沿用批量原子校验：与既有时段重叠整批拒绝、不留部分记录。"""
    revision = setup_trip_with_reading(client)
    # 同一货物 C1 的拟登记时段与其既有时段 [09:00,18:00) 重叠
    overlapping = dict(W1, loaded_at="2026-01-01T10:00:00+00:00")
    # 预检只分析不拦截重叠
    digest = post_precheck(
        client, "T1", [overlapping], expected=revision
    ).json()["digest"]
    r = post_confirm(client, "T1", [overlapping], digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"
    assert {l["cargo_id"] for l in client.get("/trips/T1").json()["loadings"]} == {
        "C1"
    }


def test_confirm_in_batch_overlap_rejected(client):
    revision = setup_trip_with_reading(client)
    items = [
        P2,
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T17:00:00+00:00",
         "unloaded_at": "2026-01-01T19:00:00+00:00"},
    ]
    digest = post_precheck(client, "T1", items, expected=revision).json()[
        "digest"
    ]
    r = post_confirm(client, "T1", items, digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"
    assert {l["cargo_id"] for l in client.get("/trips/T1").json()["loadings"]} == {
        "C1"
    }


def test_confirm_digest_single_use(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2], expected=revision).json()[
        "digest"
    ]
    assert post_confirm(client, "T1", [P2], digest).status_code == 201
    # 确认已使车次修订号 +1：同一摘要再次确认即过期
    r = post_confirm(client, "T1", [P2], digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"


def test_confirm_errors(client):
    revision = setup_trip_with_reading(client)
    digest = post_precheck(client, "T1", [P2], expected=revision).json()[
        "digest"
    ]
    # 未知车次 / 未知货物 / 非法时段：明确报错
    r = post_confirm(client, "NOPE", [P2], digest)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"

    r = post_confirm(client, "T1", [
        {"cargo_id": "NOPE",
         "loaded_at": "2026-01-01T09:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
    ], digest)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"

    r = post_confirm(client, "T1", [
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T18:00:00+00:00",
         "unloaded_at": "2026-01-01T09:00:00+00:00"},
    ], digest)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"


# ------------------------------------------------------------- 车次修订号


def test_trip_revision_visible_and_monotonic(client):
    make_cargo(client, "C1")
    post_trip(client)
    assert client.get("/trips/T1").json()["revision"] == 0
    assert client.get("/trips").json()[0]["revision"] == 0

    assert post_manifest(client, "T1", [W1]).status_code == 201
    assert client.get("/trips/T1").json()["revision"] == 1

    assert post_reading(
        client, "vr-1", "T1", "2026-01-01T10:00:00+00:00", "7.0"
    ).status_code == 201
    assert client.get("/trips/T1").json()["revision"] == 2

    # 读数幂等重传不递增（无新状态）
    assert post_reading(
        client, "vr-1", "T1", "2026-01-01T10:00:00+00:00", "7.0"
    ).status_code == 200
    assert client.get("/trips/T1").json()["revision"] == 2

    assert client.post(
        "/corrections",
        json={
            "correction_id": "corr-rev",
            "target_type": "vehicle_reading",
            "target_id": "vr-1",
            "expected_version": 0,
            "new_temperature": "2.0",
        },
    ).status_code == 201
    assert client.get("/trips/T1").json()["revision"] == 3


def test_failed_manifest_does_not_bump_revision(client):
    make_cargo(client, "C1")
    post_trip(client)
    # 非法时段整批拒绝 -> 修订号不变
    assert post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T10:00:00+00:00"},
    ]).status_code == 422
    assert client.get("/trips/T1").json()["revision"] == 0


# ------------------------------------------------------------- 纯函数


def test_boarding_segments_refcount_and_merge():
    """扫描线：同货物多段窗口引用计数、左闭右开、相邻同集合段合并。"""
    windows = [
        BoardWindow("A", 9, 12, False),
        BoardWindow("A", 12, 18, False),   # 与上一段首尾相接 -> 合并
        BoardWindow("B", 10, 11, True),
        BoardWindow("B", 11, 13, True),    # 同货物相邻拟登记段 -> 仍在车
    ]
    segs = boarding_segments(windows)
    assert [(s.start, s.end, s.cargo_ids, s.proposed_ids) for s in segs] == [
        (9, 10, ("A",), ()),
        (10, 13, ("A", "B"), ("B",)),
        (13, 18, ("A",), ()),
    ]


def test_boarding_segments_empty_windows():
    assert boarding_segments([]) == []
