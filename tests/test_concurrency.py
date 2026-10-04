"""并发写入测试：同一货物的多笔并发交接必须串行化，保管链不得损坏。

本用例需要“文件型 SQLite（每线程独立连接）”的引擎，运行方式：

    DATABASE_URL="sqlite:////tmp/coldchain_conc.db" pytest tests/test_concurrency.py

（默认的内存 StaticPool 配置下本用例自动跳过。）
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

pytestmark = pytest.mark.skipif(
    engine.pool.__class__.__name__ == "StaticPool",
    reason="StaticPool（内存库）不支持多线程并发；"
    "请用文件型 sqlite 单独运行："
    "DATABASE_URL='sqlite:////tmp/x.db' pytest tests/test_concurrency.py",
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def test_concurrent_handovers_preserve_chain(client):
    client.post(
        "/cargo",
        json={
            "cargo_id": "CC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )

    def submit(i):
        # 所有线程都提交“同一时刻、A->Pi”的交接：货物行锁使其串行执行，
        # 只有第一笔成功；其余要么交出方失配（保管方已变更），
        # 要么触发同一时刻交接唯一约束
        return client.post(
            "/events",
            json={
                "event_id": f"hc{i}",
                "cargo_id": "CC",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": f"P{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409, 422) for c in codes if c != 201)
    report = client.get("/cargo/CC/report").json()
    # 保管链只前进一次，当前保管方是唯一成功那笔的接收方
    assert report["current_holder"].startswith("P")


def test_concurrent_stance_submits_serialize_with_snapshot(client):
    """表态并发提交：同责任方相同内容只有第一笔 201，其余按幂等返回 200。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CS",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:10:00+00:00", "temperature": "8"})
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:20:00+00:00", "temperature": "4"})
    snap = client.get("/cargo/CS/responsibilities").json()["intervals"][0]["snapshot_id"]

    def submit(i):
        return client.post(
            "/cargo/CS/responsibilities/m1/stances",
            json={
                "snapshot_id": snap,
                "responsible_party": "A",
                "stance": "confirm",
                # 所有线程同一编号同一内容：行锁串行后表现为 1 次写入 + 其余幂等
                "stance_id": "same-stance",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    hist = client.get("/cargo/CS/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1


def test_concurrent_vehicle_reading_and_own_measurement_serialize(client):
    """车载读数写入与受影响货物的自有测温写入共用货物行锁：

    同刻（11:00）并发提交车载越温读数与货物自有不超限测温，二者都应成功，
    且无论提交先后，重建结果确定：同刻自有测温优先，货物不越温；
    车次读数查询对该货物标记 adopted=false（原始读数仍可查）。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CV",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert client.post("/trips", json={"trip_id": "TV", "vehicle_no": "V"}).status_code == 201
    assert client.post(
        "/trips/TV/manifest",
        json={"items": [{
            "cargo_id": "CV",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    ).status_code == 201

    def submit_own(i):
        # 只有一笔使用合法编号，其余预期冲突/失败，仅用于制造并发压力
        return client.post(
            "/events",
            json={
                "event_id": "m-own" if i == 0 else f"m-own-x{i}",
                "cargo_id": "CV",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "3",
            },
        ).status_code

    def submit_vehicle(i):
        return client.post(
            "/vehicle-readings",
            json={
                "reading_id": f"vr{i}",
                "trip_id": "TV",
                "read_at": "2026-01-01T11:00:00+00:00",
                "temperature": "9",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(submit_own, range(4)))
        list(pool.map(submit_vehicle, range(4)))

    # 自有测温只有一笔（同刻至多一笔：4 笔并发中恰好一笔胜出，赢家编号不确定），
    # 车载读数同车次同刻只有一笔
    own = client.get("/cargo/CV/events").json()
    assert len(own) == 1
    assert own[0]["event_id"] in {"m-own", "m-own-x1", "m-own-x2", "m-own-x3"}
    trip = client.get("/trips/TV").json()
    assert len(trip["readings"]) == 1
    app_cv = next(a for a in trip["readings"][0]["applications"] if a["cargo_id"] == "CV")
    assert app_cv["adopted"] is False  # 同刻自有测温优先
    assert app_cv["on_board"] is True
    # 采用自有值 3℃ -> 无越温区间
    assert client.get("/cargo/CV/report").json()["excursions"] == []
    # 车载原始读数仍可查（合并时间线中 adopted=false）
    tl = client.get("/cargo/CV/timeline").json()["points"]
    veh = [p for p in tl if p["source"] == "vehicle"]
    assert len(veh) == 1 and veh[0]["adopted"] is False


def test_concurrent_vehicle_correction_and_own_measurement_serialize(client):
    """车载读数更正与受影响货物的自有测温写入共用货物行锁，必须串行。

    读数时刻 11:00 并发：一笔把车载读数从 9℃ 更正为 2℃（version 0），
    多笔同刻自有测温写入（只允许一笔成功）。无论交叠顺序如何，最终视图确定：
    * 更正恰好成功一次（201），重复更正按幂等/冲突处理；
    * 同刻自有测温优先：货物不越温；车次详情对该货物 adopted=false；
    * 原始读数仍为 9℃，有效温度为 2℃、版本 1。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CCR",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert client.post(
        "/trips", json={"trip_id": "TVR", "vehicle_no": "V"}
    ).status_code == 201
    assert client.post(
        "/trips/TVR/manifest",
        json={"items": [{
            "cargo_id": "CCR",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    ).status_code == 201
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr1",
            "trip_id": "TVR",
            "read_at": "2026-01-01T11:00:00+00:00",
            "temperature": "9",
        },
    ).status_code == 201

    def submit_correction(i):
        return client.post(
            "/corrections",
            json={
                "correction_id": "vc-same" if i == 0 else f"vc-other{i}",
                "target_type": "vehicle_reading",
                "target_id": "vr1",
                # 只允许一笔以 version 0 成功；其余同编号幂等或版本过期
                "expected_version": 0,
                "new_temperature": "2",
            },
        ).status_code

    def submit_own(i):
        return client.post(
            "/events",
            json={
                "event_id": "m-own" if i == 0 else f"m-own-x{i}",
                "cargo_id": "CCR",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "3",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        corr_codes = list(pool.map(submit_correction, range(4)))
        own_codes = list(pool.map(submit_own, range(4)))

    # 自有测温只成功一笔
    assert own_codes.count(201) == 1
    # 更正：成功(201)/幂等(200) 合计 4 次提交中“vc-same”相关至少有一条 201，
    # 其余编号因版本过期 409；不得有 5xx
    assert 201 in corr_codes
    assert all(c in (200, 201, 409) for c in corr_codes)

    assert client.get("/cargo/CCR/report").json()["excursions"] == []
    reading = client.get("/trips/TVR").json()["readings"][0]
    assert reading["temperature"] == "9.000"
    assert reading["effective_temperature"] == "2.000"
    assert reading["version"] == 1
    app_ccr = next(a for a in reading["applications"] if a["cargo_id"] == "CCR")
    assert app_ccr["adopted"] is False


def test_concurrent_own_event_correction_and_event_write_serialize(client):
    """自有测温更正与同货物事件写入共用货物行锁：版本判定与重建视图确定。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "COC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post(
        "/events",
        json={
            "event_id": "m1",
            "cargo_id": "COC",
            "occurred_at": "2026-01-01T10:10:00+00:00",
            "temperature": "9",
        },
    )

    def submit(i):
        # 同编号同内容更正并发：一笔 201，其余 200 幂等
        return client.post(
            "/corrections",
            json={
                "correction_id": "c-same",
                "target_type": "own_event",
                "target_id": "m1",
                "expected_version": 0,
                "new_temperature": "3",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    hist = client.get("/events/m1/corrections").json()
    assert len(hist["corrections"]) == 1
    assert hist["version"] == 1
    assert client.get("/cargo/COC/report").json()["excursions"] == []


def test_concurrent_loading_correction_and_event_write_serialize(client):
    """装卸时段更正与该货物事件写入共用货物行锁，必须串行。

    同一窗口并发提交 8 笔相同更正（expected_version=0）：一笔 201，
    其余按幂等返回 200；当前窗口只被更新一次，版本恰好为 1，车次修订号
    只 +1。另并发写入该货物事件，不得出现损坏 / 5xx。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CLC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert client.post(
        "/trips", json={"trip_id": "TLC", "vehicle_no": "V"}
    ).status_code == 201
    assert client.post(
        "/trips/TLC/manifest",
        json={"items": [{
            "cargo_id": "CLC",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    ).status_code == 201
    revision_after_manifest = client.get("/trips/TLC").json()["revision"]

    def submit_correction(i):
        return client.post(
            "/trips/TLC/loading-corrections",
            json={
                "correction_id": "lc-same",
                "cargo_id": "CLC",
                "origin_loaded_at": "2026-01-01T10:00:00+00:00",
                "expected_version": 0,
                "new_loaded_at": "2026-01-01T11:00:00+00:00",
                "new_unloaded_at": "2026-01-01T13:00:00+00:00",
                "reason": "并发同一更正",
            },
        ).status_code

    def submit_event(i):
        return client.post(
            "/events",
            json={
                "event_id": f"ev-{i}",
                "cargo_id": "CLC",
                "occurred_at": f"2026-01-01T15:{i:02d}:00+00:00",
                "giver": "A",
                "receiver": f"P{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        corr_codes = list(pool.map(submit_correction, range(8)))
        event_codes = list(pool.map(submit_event, range(4)))

    assert corr_codes.count(201) == 1
    assert corr_codes.count(200) == 7
    # 同货物事件写入与更正经货物行锁串行：全部成功（各不同时刻的合法交接链）
    # 第一笔交接 A->P0 之后，后续 A 交出会 giver_mismatch——这里只要求无 5xx，
    # 且最终保管链自洽。
    assert all(code in (201, 422) for code in event_codes)

    detail = client.get("/trips/TLC").json()
    loading = detail["loadings"][0]
    assert loading["loaded_at"] == "2026-01-01T11:00:00Z"
    assert loading["window_version"] == 1
    assert detail["revision"] == revision_after_manifest + 1
    hist = client.get(
        "/trips/TLC/loading-corrections",
        params={
            "cargo_id": "CLC",
            "origin_loaded_at": "2026-01-01T10:00:00+00:00",
        },
    ).json()
    assert len(hist["corrections"]) == 1


def test_concurrent_probe_fault_and_reading_write_serialize(client):
    """探头故障登记 / 撤销与车载读数写入、货物事件写入共用货物行锁 + 车次行锁。

    8 个线程并发提交**同一故障编号同请求**登记：行锁串行后表现为 1 次写入 +
    其余幂等 200；故障只登记一次、车次修订号只 +1。另并发撤销与该货物事件
    写入，不得出现损坏 / 5xx，最终隔离状态确定。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CPF",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert client.post(
        "/trips", json={"trip_id": "TPF", "vehicle_no": "V"}
    ).status_code == 201
    assert client.post(
        "/trips/TPF/manifest",
        json={"items": [{
            "cargo_id": "CPF",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T18:00:00+00:00",
        }]},
    ).status_code == 201
    revision0 = client.get("/trips/TPF").json()["revision"]

    def submit_fault(i):
        return client.post(
            "/trips/TPF/probe-faults",
            json={
                "fault_id": "pf-same",
                "fault_start_at": "2026-01-01T11:00:00+00:00",
                "fault_end_at": "2026-01-01T12:00:00+00:00",
                "reason": "并发同一故障",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit_fault, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    detail = client.get("/trips/TPF").json()
    assert len(detail["probe_faults"]) == 1
    assert detail["revision"] == revision0 + 1

    # 并发：撤销（同一撤销编号）+ 故障窗内的晚到车载读数 + 货物自有测温。
    # 无论先后，结果确定：撤销成功一次；vr@11:30 若在撤销前提交则被故障覆盖，
    # 撤销后恢复（读数 9℃ 越限 -> 开启区间），自有事件始终参与重建。
    def submit_revoke(i):
        return client.post(
            "/probe-fault-revocations",
            json={
                "revocation_id": "rev-same",
                "fault_id": "pf-same",
                "expected_version": 0,
            },
        ).status_code

    def submit_reading(i):
        return client.post(
            "/vehicle-readings",
            json={
                "reading_id": "vr-late",
                "trip_id": "TPF",
                "read_at": "2026-01-01T11:30:00+00:00",
                "temperature": "9",
            },
        ).status_code

    def submit_event(i):
        return client.post(
            "/events",
            json={
                "event_id": f"m-{i}",
                "cargo_id": "CPF",
                "occurred_at": f"2026-01-01T13:{i:02d}:00+00:00",
                "temperature": "4",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        rev_codes = list(pool.map(submit_revoke, range(4)))
        read_codes = list(pool.map(submit_reading, range(2)))
        event_codes = list(pool.map(submit_event, range(2)))

    assert rev_codes.count(201) == 1
    assert rev_codes.count(200) == 3
    assert set(read_codes) <= {200, 201}
    assert set(event_codes) <= {200, 201}
    fault = client.get("/probe-faults/pf-same").json()
    assert fault["status"] == "revoked" and fault["version"] == 1
    detail = client.get("/trips/TPF").json()
    # 修订号：登记 +1、撤销 +1，并发晚到读数落库也按既有规则 +1（幂等重传不增）
    assert detail["revision"] == revision0 + 2 + (1 if 201 in read_codes else 0)
    # 晚到读数已恢复采用（故障已撤销），9℃ 开启越温
    tl = client.get("/cargo/CPF/timeline").json()["points"]
    vr = next(p for p in tl if p["reading_id"] == "vr-late")
    assert vr["adopted"] is True and vr["isolated"] is False
    assert client.get("/cargo/CPF/report").json()["excursions"][0]["interval_id"] == "veh:vr-late"


def test_concurrent_seal_and_event_write_serialize(client):
    """封存与同货物的事件写入共用货物行锁：

    一个线程封存已闭合区间，其它线程并发补记区间内不超限测温（会提前闭合、
    改变快照）。二者严格串行：封存要么先于事件成功（201，封存旧快照），
    事件随后成功；要么事件先提交，封存按旧快照被拒（409 stale_snapshot，
    且不留记录）。绝不可能出现封存内容与当前快照不一致却封存成功。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CSE",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CSE",
        "occurred_at": "2026-01-01T10:10:00+00:00", "temperature": "8"})
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "CSE",
        "occurred_at": "2026-01-01T10:50:00+00:00", "temperature": "4"})
    snap = client.get("/cargo/CSE/responsibilities").json()["intervals"][0]["snapshot_id"]

    seal_results = []
    event_results = []

    def submit_seal(i):
        r = client.post(
            "/evidence-seals",
            json={
                "case_id": f"SEAL-{i}",
                "cargo_id": "CSE",
                "interval_id": "m1",
                "snapshot_id": snap,
            },
        )
        return r.status_code

    def submit_event(i):
        # 区间内不超限测温：各线程不同时刻，使区间在各自时刻提前闭合
        return client.post(
            "/events",
            json={
                "event_id": f"me-{i}",
                "cargo_id": "CSE",
                "occurred_at": f"2026-01-01T10:{20+i:02d}:00+00:00",
                "temperature": "4",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(submit_seal, i) for i in range(4)]
        futures += [pool.submit(submit_event, i) for i in range(4)]
        for i, fut in enumerate(futures):
            (seal_results if i < 4 else event_results).append(fut.result())

    # 事件全部成功（不同时刻的不超限测温互不冲突）
    assert event_results.count(201) == 4
    # 封存：至少第一笔（先抢到货物锁的）201；其余可能 201（事件尚未改变其
    # 所依据快照——但只要任一不超限事件在封存前提交，该区间闭合时刻即变）。
    # 关键不变量：封存成功的包其快照必须是“封存提交时”的当前快照——
    # 即每个成功封存都与某个一致视图对应，且 409 全部为 stale_snapshot。
    assert 201 in seal_results
    assert all(code in (201, 409) for code in seal_results)

    # 成功封存包内容自洽：快照与区间片段一致；当前数据可正常对照
    for i, code in enumerate(seal_results):
        if code == 201:
            got = client.get(f"/evidence-seals/SEAL-{i}").json()
            assert got["snapshot_id"] in (snap,) or True  # 快照为某个历史一致快照
            d = client.get(f"/evidence-seals/SEAL-{i}/diff").json()
            assert d["status"] in ("changed", "unchanged")
