"""车次探头故障时段登记 / 撤销的写入与查询事务。

质控人员确认车次探头失准后，可凭**故障编号**、车次、带时区起止时刻与原因
登记左闭右开故障时段 ``[fault_start_at, fault_end_at)``；可凭**撤销编号**、
故障编号与**期望版本**撤销。登记与撤销均 append-only 留痕。

隔离语义
--------
任一生效故障覆盖的车载读数（读数时刻落在故障时段内，且对该货物在车适用）
**不参与货物越温重建**（晚到读数亦同）：原始读数与温度更正历史照常可查，
时间线 / 车次详情标明隔离状态与覆盖故障。重叠故障允许登记；撤销其中一条后，
另一条仍覆盖的读数继续隔离；撤销最后一条覆盖故障后，读数按当前有效温度
恢复采用（同刻自有测温仍优先，交接不受影响）。

加锁与串行（全系统统一锁顺序：先按 cargo_id 升序锁货物行、再锁车次行，
见 app/vehicle_crud.py）
--------------------------------------------------------------------------------
登记 / 撤销都先锁定与故障时段重叠的全部装卸货物行，再锁定车次行，故与这些
货物的事件写入、责任表态、车载读数写入 / 测温更正、装卸登记 / 更正严格串行；
成功落库时同事务把车次修订号 +1（旧装卸预检摘要随之失效）。

乐观锁 / 幂等
-------------
* 登记：``fault_id`` 同请求（车次 / 起止 / 原因全相同）重传返回原结果
  （``200 replayed``）；异内容 ``409 probe_fault_id_conflict``；
* 撤销：``revocation_id`` 同请求（故障编号 + 期望版本相同）重传返回原结果；
  异请求 ``409 revocation_id_conflict``；故障不存在 ``404 probe_fault_not_found``；
  ``expected_version`` 与故障当前版本不一致（含已撤销故障）→
  ``409 stale_fault_version``。
* 非法时段（起点不严格早于终点）/ 缺时区 / 原因为空 / 未知车次一律拒绝，
  不写任何部分结果。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.crud import ApiError
from app.models import (
    Loading,
    ProbeFault,
    ProbeFaultRevocation,
    Trip,
)
from app.schemas import ProbeFaultCreate, ProbeFaultRevokeCreate
from app.timeutil import ensure_utc
from app.vehicle_crud import _load_trip_locked, _lock_cargo_rows

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


# ----------------------------------------------------------- 请求指纹


def _register_request_hash(trip_id, start_at, end_at, reason: str) -> str:
    payload = [
        trip_id,
        ensure_utc(start_at).isoformat(),
        ensure_utc(end_at).isoformat(),
        reason,
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _revoke_request_hash(fault_id: str, expected_version: int) -> str:
    payload = [fault_id, int(expected_version)]
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


# ----------------------------------------------------------- 读取辅助


def get_probe_fault(db, fault_id: str) -> ProbeFault | None:
    return db.get(ProbeFault, fault_id)


def list_probe_faults(db, trip_id: str) -> list[ProbeFault]:
    """车次的全部探头故障（含已撤销，按开始时刻 / 故障编号排序，留痕可查）。"""
    return list(
        db.scalars(
            select(ProbeFault)
            .where(ProbeFault.trip_id == trip_id)
            .order_by(ProbeFault.fault_start_at, ProbeFault.fault_id)
        ).all()
    )


def active_fault_rows(db, trip_id: str) -> list[ProbeFault]:
    """车次当前生效（未撤销）的故障行。"""
    return list(
        db.scalars(
            select(ProbeFault).where(
                ProbeFault.trip_id == trip_id,
                ProbeFault.status == STATUS_ACTIVE,
            )
        ).all()
    )


def affected_cargo_ids(db, trip_id: str, start_at, end_at) -> list[str]:
    """与故障时段 [start_at, end_at) 重叠的车次装卸货物（左闭右开求交）。

    即故障期间曾在该车次车上的全部货物：
    ``loaded_at < end_at AND unloaded_at > start_at``（首尾相接不算重叠）。
    """
    rows = db.scalars(
        select(Loading.cargo_id).where(
            Loading.trip_id == trip_id,
            Loading.loaded_at < end_at,
            Loading.unloaded_at > start_at,
        )
    ).all()
    return sorted(set(rows))


# ----------------------------------------------------------- 登记


@dataclass
class ProbeFaultResult:
    fault: ProbeFault
    replayed: bool
    affected_cargo_ids: list[str]


def _same_register_request(row: ProbeFault, request_hash: str) -> bool:
    return row.request_hash == request_hash


def register_probe_fault(db, trip_id: str, payload: ProbeFaultCreate) -> ProbeFaultResult:
    try:
        return _register_tx(db, trip_id, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _register_tx(db, trip_id, payload)


def _register_tx(db, trip_id: str, payload: ProbeFaultCreate) -> ProbeFaultResult:
    start_at = ensure_utc(payload.fault_start_at)
    end_at = ensure_utc(payload.fault_end_at)
    reason = payload.reason.strip()
    fault_id = payload.fault_id
    request_hash = _register_request_hash(trip_id, start_at, end_at, reason)

    # 幂等快路径（按主键读，无需持锁）
    existing = db.get(ProbeFault, fault_id)
    if existing is not None:
        if _same_register_request(existing, request_hash):
            db.rollback()
            affected = affected_cargo_ids(db, existing.trip_id, start_at, end_at)
            return ProbeFaultResult(existing, True, affected)
        raise _conflict(
            f"探头故障编号 {fault_id} 已存在且请求内容不同，拒绝覆盖",
            code="probe_fault_id_conflict",
        )

    try:
        # 车次须存在（先读；真正互斥在拿到车次行锁后）
        if db.get(Trip, trip_id) is None:
            raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

        # 故障期间在车的全部货物：先锁货物行（升序），再锁车次行
        affected = affected_cargo_ids(db, trip_id, start_at, end_at)
        _lock_cargo_rows(db, affected)
        trip = _load_trip_locked(db, trip_id)

        # 持锁后复核幂等（并发首传竞争在此分出唯一赢家）
        winner = db.get(ProbeFault, fault_id)
        if winner is not None:
            if _same_register_request(winner, request_hash):
                db.rollback()
                affected2 = affected_cargo_ids(db, trip_id, start_at, end_at)
                return ProbeFaultResult(winner, True, affected2)
            raise _conflict(
                f"探头故障编号 {fault_id} 已存在且请求内容不同，拒绝覆盖",
                code="probe_fault_id_conflict",
            )

        row = ProbeFault(
            fault_id=fault_id,
            trip_id=trip_id,
            fault_start_at=start_at,
            fault_end_at=end_at,
            reason=reason,
            status=STATUS_ACTIVE,
            version=0,
            request_hash=request_hash,
        )
        db.add(row)
        # 故障登记落库 -> 车次修订号 +1（同事务，冲突回滚则不递增）
        trip.revision = (trip.revision or 0) + 1
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner2 = db.get(ProbeFault, fault_id)
        if winner2 is not None:
            if _same_register_request(winner2, request_hash):
                affected2 = affected_cargo_ids(
                    db, winner2.trip_id,
                    ensure_utc(winner2.fault_start_at),
                    ensure_utc(winner2.fault_end_at),
                )
                return ProbeFaultResult(winner2, True, affected2)
            raise _conflict(
                f"探头故障编号 {fault_id} 已存在且请求内容不同，拒绝覆盖",
                code="probe_fault_id_conflict",
            )
        raise _conflict(
            f"探头故障 {fault_id} 与既有数据冲突（车次缺失 / 字段约束）",
            code="integrity_conflict",
        )

    row.fault_start_at = ensure_utc(row.fault_start_at)
    row.fault_end_at = ensure_utc(row.fault_end_at)
    return ProbeFaultResult(row, False, affected)


# ----------------------------------------------------------- 撤销


@dataclass
class ProbeFaultRevokeResult:
    revocation: ProbeFaultRevocation
    fault: ProbeFault
    replayed: bool
    affected_cargo_ids: list[str]


def _same_revoke_request(row: ProbeFaultRevocation, request_hash: str) -> bool:
    return row.request_hash == request_hash


def revoke_probe_fault(db, payload: ProbeFaultRevokeCreate) -> ProbeFaultRevokeResult:
    try:
        return _revoke_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _revoke_tx(db, payload)


def _revoke_tx(db, payload: ProbeFaultRevokeCreate) -> ProbeFaultRevokeResult:
    revocation_id = payload.revocation_id
    fault_id = payload.fault_id
    expected_version = payload.expected_version
    request_hash = _revoke_request_hash(fault_id, expected_version)

    # 幂等快路径：撤销编号已存在时（通常是串行重传）无需加锁即可裁决
    existing_rev = db.get(ProbeFaultRevocation, revocation_id)
    if existing_rev is not None:
        if _same_revoke_request(existing_rev, request_hash):
            db.rollback()
            fault = db.get(ProbeFault, existing_rev.fault_id)
            affected = affected_cargo_ids(
                db, fault.trip_id,
                ensure_utc(fault.fault_start_at),
                ensure_utc(fault.fault_end_at),
            )
            return ProbeFaultRevokeResult(existing_rev, fault, True, affected)
        raise _conflict(
            f"撤销编号 {revocation_id} 已存在且请求内容不同，拒绝覆盖",
            code="revocation_id_conflict",
        )

    try:
        fault = db.get(ProbeFault, fault_id)
        if fault is None:
            raise ApiError(
                404, "probe_fault_not_found", f"探头故障 {fault_id} 不存在"
            )
        trip_id = fault.trip_id

        # 与登记同一锁顺序：先故障期间在车货物行（升序），再车次行
        affected = affected_cargo_ids(
            db,
            trip_id,
            ensure_utc(fault.fault_start_at),
            ensure_utc(fault.fault_end_at),
        )
        _lock_cargo_rows(db, affected)
        trip = _load_trip_locked(db, trip_id)

        # 持锁后复核幂等（并发首传竞争）
        winner_rev = db.get(ProbeFaultRevocation, revocation_id)
        if winner_rev is not None:
            if _same_revoke_request(winner_rev, request_hash):
                db.rollback()
                fault2 = db.get(ProbeFault, fault_id)
                affected2 = affected_cargo_ids(
                    db, trip_id,
                    ensure_utc(fault2.fault_start_at),
                    ensure_utc(fault2.fault_end_at),
                )
                return ProbeFaultRevokeResult(winner_rev, fault2, True, affected2)
            raise _conflict(
                f"撤销编号 {revocation_id} 已存在且请求内容不同，拒绝覆盖",
                code="revocation_id_conflict",
            )

        # 持锁后重读故障状态（锁前可能已被并发撤销）
        fault = db.get(ProbeFault, fault_id)
        if fault is None:
            raise ApiError(
                404, "probe_fault_not_found", f"探头故障 {fault_id} 不存在"
            )
        if fault.version != expected_version or fault.status != STATUS_ACTIVE:
            raise _conflict(
                f"版本过期：探头故障 {fault_id} 当前版本为 "
                f"{fault.version}（状态 {fault.status}），请求期望版本为 "
                f"{expected_version}，请重新查询故障详情后再撤销",
                code="stale_fault_version",
            )

        revoked_at = datetime.now(timezone.utc)
        revocation = ProbeFaultRevocation(
            revocation_id=revocation_id,
            fault_id=fault_id,
            trip_id=trip_id,
            expected_version=expected_version,
            version=1,
            request_hash=request_hash,
        )
        db.add(revocation)

        # 故障行就地标记撤销（登记留痕字段不动）
        fault.status = STATUS_REVOKED
        fault.version = 1
        fault.revocation_id = revocation_id
        fault.revoked_at = revoked_at

        # 撤销落库 -> 车次修订号 +1（同事务，版本过期回滚则不递增）
        trip.revision = (trip.revision or 0) + 1
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner2 = db.get(ProbeFaultRevocation, revocation_id)
        if winner2 is not None:
            if _same_revoke_request(winner2, request_hash):
                fault2 = db.get(ProbeFault, winner2.fault_id)
                affected2 = affected_cargo_ids(
                    db, fault2.trip_id,
                    ensure_utc(fault2.fault_start_at),
                    ensure_utc(fault2.fault_end_at),
                )
                return ProbeFaultRevokeResult(winner2, fault2, True, affected2)
            raise _conflict(
                f"撤销编号 {revocation_id} 已存在且请求内容不同，拒绝覆盖",
                code="revocation_id_conflict",
            )
        raise _conflict(
            f"故障撤销 {revocation_id} 与既有数据冲突（撤销编号竞争 / 字段约束）",
            code="integrity_conflict",
        )

    fault = db.get(ProbeFault, fault_id)
    return ProbeFaultRevokeResult(revocation, fault, False, affected)
