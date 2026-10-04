"""车次探头故障登记 / 撤销的写入与查询事务。

质控人员在车次探头被证实失准后，凭**故障编号**、**车次**、**带时区起止
时刻**与**原因**登记左闭右开故障时段 ``[fault_start, fault_end)``；可凭
**撤销编号**与**期望版本**撤销。登记与撤销均 append-only 留痕
（``probe_fault`` / ``probe_fault_revocation``），登记行不删不改，撤销只
追加一条撤销行；故障生效与否、被哪些故障覆盖全部由查询时的重建判定。

隔离语义（见 :mod:`app.faults` / :mod:`app.timeline`）
----------------------------------------------------
任一生效（未撤销）故障覆盖的车载读数（读数时刻落在故障左闭右开时段内）
对读数时刻在车的货物一律隔离，不参与越温重建；晚到读数同样隔离（按重建
时的生效故障即时判定）。重叠故障撤销一条后，另一条覆盖的读数仍隔离；
撤销最后一条覆盖故障后，读数按当前有效温度恢复采用（同刻自有测温仍优先，
交接不受影响）。越温报告、责任确认与车次同时越温查询随之重算；旧表态
历史与已封存证据包保持原样，封存现况对照反映变化。

加锁与串行（全系统统一锁顺序：先货物行升序、再车次行）
------------------------------------------------------
故障写入须与相关装卸、读数、测温更正及表态串行：先按 cargo_id 升序锁定
**故障时段在车的全部货物行**（装卸窗口与故障时段相交的货物），再锁定车次
行——与车载读数写入、车载测温更正同一锁顺序。成功登记 / 撤销在同一事务
内把车次修订号 +1（旧装卸预检摘要随之失效；失败回滚不递增）。

幂等 / 乐观锁 / 拒绝（全部失败不留部分写入）
--------------------------------------------
* 故障编号同请求（车次、起止、原因全相同）重传返回原结果
  （``200 replayed``，回执按当前数据给明细）；异内容
  ``409 fault_id_conflict``；
* 撤销编号同请求重传返回原结果（即使故障此后已撤销——重传的就是这次撤销）；
  异请求 ``409 revocation_id_conflict``；
* 撤销的 ``expected_version`` 须等于故障当前版本（登记后为 0），否则
  ``409 stale_fault_version``；故障已撤销再撤销（版本号已为 1）按版本
  过期拒绝；
* 故障不存在（含故障不属于该车次）→ 404 ``probe_fault_not_found``；
  未知车次 → 404 ``trip_not_found``；
* 时段非左闭右开（起点不严格早于终点）→ 422 ``invalid_fault_window``；
  缺时区 → 422 ``timezone_required``；原因为空 → 422 ``bad_fault_shape``。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import faults as faults_mod
from app.crud import ApiError
from app.models import (
    Loading,
    ProbeFault,
    ProbeFaultRevocation,
    VehicleReading,
)
from app.schemas import ProbeFaultCreate, ProbeFaultRevokeCreate
from app.timeutil import ensure_utc
from app.vehicle_crud import (
    _bump_trip_revision,
    _load_trip_locked,
    _lock_cargo_rows,
)


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


# ----------------------------------------------------------- 读取辅助


def _cargo_onboard_during(db, trip_id: str, start, end) -> list[str]:
    """故障时段内曾在该车上的货物：装卸窗口与 [start, end) 相交（左闭右开）。

    首尾相接不算相交（``unloaded_at == fault_start`` 的窗口在故障开始时已
    卸货；``loaded_at == fault_end`` 的窗口在故障结束时才装车）。
    """
    rows = db.scalars(
        select(Loading.cargo_id).where(
            Loading.trip_id == trip_id,
            Loading.loaded_at < end,
            Loading.unloaded_at > start,
        )
    ).all()
    return sorted(set(rows))


def _readings_in_window(db, trip_id: str, start, end) -> list:
    """车次上读数时刻落在左闭右开 [start, end) 内的车载读数（按时刻 / 编号）。"""
    return list(
        db.scalars(
            select(VehicleReading)
            .where(
                VehicleReading.trip_id == trip_id,
                VehicleReading.read_at >= start,
                VehicleReading.read_at < end,
            )
            .order_by(VehicleReading.read_at, VehicleReading.reading_id)
        ).all()
    )


def _fault_replay_view(db, row: ProbeFault) -> "FaultWriteResult":
    """已存在故障的幂等重放：回执按当前数据给出被隔离读数与在车货物。"""
    start, end = ensure_utc(row.fault_start), ensure_utc(row.fault_end)
    quarantined = [
        r.reading_id for r in _readings_in_window(db, row.trip_id, start, end)
    ]
    affected = _cargo_onboard_during(db, row.trip_id, start, end)
    return FaultWriteResult(
        fault=row,
        replayed=True,
        quarantined_reading_ids=quarantined,
        affected_cargo_ids=affected,
    )


def _revocation_replay_view(
    db, row: ProbeFaultRevocation
) -> "FaultRevokeResult":
    """撤销的幂等重放：恢复读数按当前数据计算（撤销结果本身不变）。"""
    fault = faults_mod.load_fault(db, row.fault_id)
    start, end = ensure_utc(fault.fault_start), ensure_utc(fault.fault_end)
    affected = _cargo_onboard_during(db, fault.trip_id, start, end)
    return FaultRevokeResult(
        revocation=row,
        fault=fault,
        replayed=True,
        restored_reading_ids=_restored_reading_ids(db, fault),
        affected_cargo_ids=affected,
    )


# ----------------------------------------------------------- 登记


@dataclass
class FaultWriteResult:
    fault: ProbeFault
    replayed: bool
    quarantined_reading_ids: list[str]
    affected_cargo_ids: list[str]


def _same_fault_content(existing: ProbeFault, payload, start, end) -> bool:
    return (
        existing.trip_id == payload.trip_id
        and ensure_utc(existing.fault_start) == start
        and ensure_utc(existing.fault_end) == end
        and existing.reason == payload.reason
    )


def register_fault(db, payload: ProbeFaultCreate) -> FaultWriteResult:
    try:
        return _register_fault_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变。
        # OperationalError 也可能在 commit 已落库后抛出——重放前先按编号幂等
        # 检测，避免把已提交事务（含车次修订号 +1）再执行一遍。
        db.rollback()
        winner = faults_mod.load_fault(db, payload.fault_id)
        start, end = ensure_utc(payload.fault_start), ensure_utc(payload.fault_end)
        if winner is not None and _same_fault_content(winner, payload, start, end):
            return _fault_replay_view(db, winner)
        return _register_fault_tx(db, payload)


def _register_fault_tx(db, payload: ProbeFaultCreate) -> FaultWriteResult:
    trip_id = payload.trip_id
    start = ensure_utc(payload.fault_start)
    end = ensure_utc(payload.fault_end)
    fault_id = payload.fault_id

    # 幂等快路径：故障编号已存在（通常是串行重传）时无需加锁即可裁决
    existing = faults_mod.load_fault(db, fault_id)
    if existing is not None:
        if not _same_fault_content(existing, payload, start, end):
            raise _conflict(
                f"故障编号 {fault_id} 已存在且请求内容不同，拒绝覆盖",
                code="fault_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        return _fault_replay_view(db, existing)

    affected: list[str] = []
    try:
        # 1) 锁定故障时段在车的全部货物行（升序），与这些货物的装卸 / 读数 /
        #    更正 / 表态串行；2) 再锁定车次行。
        affected = _cargo_onboard_during(db, trip_id, start, end)
        _lock_cargo_rows(db, affected)
        trip = _load_trip_locked(db, trip_id)

        # 持锁后复核幂等（并发首传竞争在此分出唯一赢家）
        winner = faults_mod.load_fault(db, fault_id)
        if winner is not None:
            if not _same_fault_content(winner, payload, start, end):
                raise _conflict(
                    f"故障编号 {fault_id} 已存在且请求内容不同，拒绝覆盖",
                    code="fault_id_conflict",
                )
            quarantined = [
                r.reading_id
                for r in _readings_in_window(db, trip_id, start, end)
            ]
            db.rollback()
            result = FaultWriteResult(
                fault=winner,
                replayed=True,
                quarantined_reading_ids=quarantined,
                affected_cargo_ids=affected,
            )
            return result

        fault = ProbeFault(
            fault_id=fault_id,
            trip_id=trip_id,
            fault_start=start,
            fault_end=end,
            reason=payload.reason,
            version=0,
        )
        db.add(fault)
        # 故障登记落库 -> 车次修订号 +1（同事务，冲突回滚则不递增）
        _bump_trip_revision(db, trip)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner2 = faults_mod.load_fault(db, fault_id)
        if winner2 is not None:
            if _same_fault_content(winner2, payload, start, end):
                return _fault_replay_view(db, winner2)
            raise _conflict(
                f"故障编号 {fault_id} 已存在且请求内容不同，拒绝覆盖",
                code="fault_id_conflict",
            )
        raise _conflict(
            f"故障登记 {fault_id} 与既有数据冲突（车次缺失 / 字段约束）",
            code="integrity_conflict",
        )

    fault.fault_start = start
    fault.fault_end = end
    return FaultWriteResult(
        fault=fault,
        replayed=False,
        quarantined_reading_ids=[
            r.reading_id for r in _readings_in_window(db, trip_id, start, end)
        ],
        affected_cargo_ids=affected,
    )


# ----------------------------------------------------------- 撤销


@dataclass
class FaultRevokeResult:
    revocation: ProbeFaultRevocation
    fault: ProbeFault
    replayed: bool
    restored_reading_ids: list[str]
    affected_cargo_ids: list[str]


def _same_revoke_content(
    existing: ProbeFaultRevocation,
    fault_id: str,
    trip_id: str,
    expected_version: int,
) -> bool:
    return (
        existing.fault_id == fault_id
        and existing.trip_id == trip_id
        and existing.expected_version == expected_version
    )


def _other_active_faults(db, fault: ProbeFault) -> list:
    """与给定故障同车次、除其自身外的生效（未撤销）故障。

    在撤销事务内调用：本事务已写入给定故障的撤销行，故加载结果中给定故障
    已带撤销关系、不会被计入；其余未撤销故障即“撤销后仍覆盖”的故障。
    """
    rows = faults_mod.load_trip_faults(db, fault.trip_id)
    return [
        f
        for f in rows
        if f.fault_id != fault.fault_id and f.revocation is None
    ]


def _restored_reading_ids(db, fault: ProbeFault) -> list[str]:
    """撤销 fault 后恢复采用的读数：故障时段内、且不被其它生效故障覆盖。"""
    start, end = ensure_utc(fault.fault_start), ensure_utc(fault.fault_end)
    others = _other_active_faults(db, fault)
    restored = []
    for r in _readings_in_window(db, fault.trip_id, start, end):
        if not faults_mod.faults_covering(others, ensure_utc(r.read_at)):
            restored.append(r.reading_id)
    return restored


def revoke_fault(db, payload: ProbeFaultRevokeCreate) -> FaultRevokeResult:
    try:
        return _revoke_fault_tx(db, payload)
    except OperationalError:
        db.rollback()
        # 同样防御 commit 后 OperationalError：本撤销编号已落库且同请求则按
        # 幂等返回原结果；否则重放事务（版本过期 / 冲突均由事务内重新裁决）。
        winner = faults_mod.load_revocation(db, payload.revocation_id)
        if winner is not None and _same_revoke_content(
            winner,
            payload.fault_id,
            payload.trip_id,
            payload.expected_version,
        ):
            return _revocation_replay_view(db, winner)
        return _revoke_fault_tx(db, payload)


def _revoke_fault_tx(db, payload: ProbeFaultRevokeCreate) -> FaultRevokeResult:
    revocation_id = payload.revocation_id
    fault_id = payload.fault_id
    expected_version = payload.expected_version

    # 幂等快路径：撤销编号已存在时无需加锁即可裁决
    existing = faults_mod.load_revocation(db, revocation_id)
    if existing is not None:
        if not _same_revoke_content(
            existing, fault_id, payload.trip_id, expected_version
        ):
            raise _conflict(
                f"撤销编号 {revocation_id} 已存在且请求内容不同，拒绝覆盖",
                code="revocation_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        return _revocation_replay_view(db, existing)

    affected: list[str] = []
    try:
        # 故障须存在且属于该车次（先按主键读，仅用于确定锁集合；真正状态判定
        # 在持锁后重新加载，避免 relationship 快照在加锁期间过期）。
        preloaded = db.get(ProbeFault, fault_id)
        if preloaded is None:
            raise ApiError(
                404,
                "probe_fault_not_found",
                f"探头故障 {fault_id} 不存在",
            )
        if preloaded.trip_id != payload.trip_id:
            raise ApiError(
                404,
                "probe_fault_not_found",
                f"车次 {payload.trip_id} 上不存在故障 {fault_id}",
            )
        start = ensure_utc(preloaded.fault_start)
        end = ensure_utc(preloaded.fault_end)

        # 先锁故障时段在车货物（升序），再锁车次行——锁集合与登记时相同
        affected = _cargo_onboard_during(db, preloaded.trip_id, start, end)
        _lock_cargo_rows(db, affected)
        trip = _load_trip_locked(db, preloaded.trip_id)

        # 持锁后复核撤销编号幂等（并发首传竞争在此分出唯一赢家）
        winner = faults_mod.load_revocation(db, revocation_id)
        if winner is not None:
            if not _same_revoke_content(
                winner, fault_id, preloaded.trip_id, expected_version
            ):
                raise _conflict(
                    f"撤销编号 {revocation_id} 已存在且请求内容不同，拒绝覆盖",
                    code="revocation_id_conflict",
                )
            db.rollback()
            return _revocation_replay_view(db, winner)

        # 持锁后重新加载故障（其撤销状态在加锁期间可能变化）
        fault = faults_mod.load_fault(db, fault_id)
        if fault is None:
            raise ApiError(
                404,
                "probe_fault_not_found",
                f"探头故障 {fault_id} 不存在",
            )

        # 乐观锁：期望版本须等于故障当前版本（登记后为 0，撤销后为 1）
        current_version = fault.version or 0
        if fault.revocation is not None or expected_version != current_version:
            raise _conflict(
                f"版本过期：故障 {fault_id} 当前版本为 {current_version}，"
                f"请求期望版本为 {expected_version}；"
                "已撤销的故障不可再次撤销，请重新查询故障状态",
                code="stale_fault_version",
            )

        revoked_at = datetime.now(timezone.utc)
        revocation = ProbeFaultRevocation(
            revocation_id=revocation_id,
            fault_id=fault_id,
            trip_id=fault.trip_id,
            expected_version=expected_version,
            version=current_version + 1,
            revoked_at=revoked_at,
        )
        db.add(revocation)
        # 当前版本就地推进（登记行不删不改，仅版本随撤销 +1，留痕以撤销行为准）
        fault.version = current_version + 1
        # 撤销落库 -> 车次修订号 +1（失败回滚则不递增）
        _bump_trip_revision(db, trip)

        # 恢复采用的读数：本故障时段内、撤销后无任何其它生效故障覆盖的读数。
        # db.flush() 使撤销行对同事务 relationship/查询可见。
        db.flush()
        restored = _restored_reading_ids(db, fault)
        db.commit()
        # 提交后身份映射中的 fault 仍带着加锁前加载的撤销关系快照，
        # 显式过期全部对象后重新加载，确保回执反映“已撤销”。
        db.expire_all()
        fault = faults_mod.load_fault(db, fault_id)
        revocation = fault.revocation
    except ApiError:
        db.rollback()
        replay = _revoke_replay_after_conflict(
            db, revocation_id, fault_id, payload.trip_id, expected_version
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        db.rollback()
        replay = _revoke_replay_after_conflict(
            db, revocation_id, fault_id, payload.trip_id, expected_version
        )
        if replay is not None:
            return replay
        raise _conflict(
            f"故障撤销 {revocation_id} 与既有数据冲突"
            "（撤销编号冲突 / 故障已撤销）",
            code="stale_fault_version",
        )

    return FaultRevokeResult(
        revocation=revocation,
        fault=fault,
        replayed=False,
        restored_reading_ids=restored,
        affected_cargo_ids=affected,
    )


def _revoke_replay_after_conflict(
    db, revocation_id, fault_id, trip_id, expected_version
) -> FaultRevokeResult | None:
    """回滚后：赢家是同撤销编号同请求则按幂等返回，否则 None。"""
    winner = faults_mod.load_revocation(db, revocation_id)
    if winner is None:
        return None
    if not _same_revoke_content(winner, fault_id, trip_id, expected_version):
        return None
    return _revocation_replay_view(db, winner)


# ----------------------------------------------------------- 查询


def get_trip_faults(db, trip_id: str) -> list[ProbeFault]:
    """车次的全部故障登记（含已撤销），按故障开始时刻 / 编号升序。"""
    return faults_mod.load_trip_faults(db, trip_id)
