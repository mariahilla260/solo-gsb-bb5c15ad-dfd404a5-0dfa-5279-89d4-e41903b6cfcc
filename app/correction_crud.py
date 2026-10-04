"""测温值更正的写入 / 查询事务。

更正只改**温度**：目标测温（货物自有测温事件 或 车载读数）的时间、归属、
原始编号全部不变，交接事件不可更正。更正只追加（append-only），原始测温 /
读数永不修改；每个目标维护单调递增的更正版本，当前有效温度取最新更正。

加锁与串行（全系统统一的锁顺序，见 app/vehicle_crud.py）
---------------------------------------------------------
* 自有测温更正：锁定该测温所属货物行——与该货物的自有事件写入、责任表态、
  车载读数写入、装卸登记严格串行；
* 车载读数更正：**先按 cargo_id 升序锁定读数时刻仍在车的全部货物行，再锁定
  车次行**——“车载更正须同时影响读数时刻在车的全部货物”，并与这些货物的
  事件 / 表态、车次的装卸登记与读数写入串行；成功落库时同事务将车次
  修订号 +1（装卸前预检摘要随之过期）。

乐观锁 / 幂等
-------------
* ``expected_version`` 必须等于目标当前更正版本（首次为 0），否则
  ``409 stale_version``；目标不存在按类型返回 404；目标为交接
  ``422 handover_not_correctable``。全部失败一律不落库（无部分结果）。
* ``correction_id`` 同内容（含目标、期望版本、新温度）重传返回原结果
  （``200 replayed``）；异内容 ``409 correction_id_conflict``。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.crud import ApiError, _load_cargo_locked
from app.models import (
    CorrectionTarget,
    Event,
    EventType,
    TemperatureCorrection,
    Trip,
    VehicleReading,
)
from app.schemas import CorrectionCreate
from app.timeutil import ensure_utc
from app.vehicle_crud import (
    _affected_cargo_ids,
    _bump_trip_revision,
    _load_trip_locked,
    _lock_cargo_rows,
)


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


# ----------------------------------------------------------- 读取辅助


def load_corrections_for_event(db, event_id: str) -> list[TemperatureCorrection]:
    """一笔自有测温事件的全部更正（按版本升序）。"""
    return list(
        db.scalars(
            select(TemperatureCorrection)
            .where(
                TemperatureCorrection.target_type
                == CorrectionTarget.own_event,
                TemperatureCorrection.target_event_id == event_id,
            )
            .order_by(
                TemperatureCorrection.version, TemperatureCorrection.correction_id
            )
        ).all()
    )


def load_corrections_for_reading(db, reading_id: str) -> list[TemperatureCorrection]:
    """一笔车载读数的全部更正（按版本升序）。"""
    return list(
        db.scalars(
            select(TemperatureCorrection)
            .where(
                TemperatureCorrection.target_type
                == CorrectionTarget.vehicle_reading,
                TemperatureCorrection.target_reading_id == reading_id,
            )
            .order_by(
                TemperatureCorrection.version, TemperatureCorrection.correction_id
            )
        ).all()
    )


def _latest_rows(db, target_type: CorrectionTarget, target_ids: set[str]):
    """目标ID集合 -> 每个目标最新一条更正行（version 最大）。"""
    if not target_ids:
        return {}
    col = (
        TemperatureCorrection.target_event_id
        if target_type is CorrectionTarget.own_event
        else TemperatureCorrection.target_reading_id
    )
    rows = db.scalars(
        select(TemperatureCorrection)
        .where(
            TemperatureCorrection.target_type == target_type,
            col.in_(target_ids),
        )
    ).all()
    latest = {}
    for row in rows:
        key = row.target_event_id if target_type is CorrectionTarget.own_event else row.target_reading_id
        prev = latest.get(key)
        if prev is None or row.version > prev.version:
            latest[key] = row
    return latest


def _current_version(db, target_type: CorrectionTarget, target_id: str) -> int:
    rows = _latest_rows(db, target_type, {target_id})
    row = rows.get(target_id)
    return 0 if row is None else row.version


# ----------------------------------------------------------- 提交


@dataclass
class CorrectionResult:
    correction: TemperatureCorrection
    replayed: bool
    # 自有测温更正：所属货物；车载读数更正：读数时刻在车的全部货物（可能为空）
    cargo_ids: list[str]
    # 更正后目标的有效温度与版本
    effective_temperature: Decimal
    version: int
    # 车载读数更正时的读数所属车次（自有更正为 None）
    trip_id: str | None


def _same_content(
    existing: TemperatureCorrection,
    target_type: CorrectionTarget,
    target_id: str,
    expected_version: int,
    new_temperature: Decimal,
) -> bool:
    """更正ID相同前提下逐字段比对内容（含目标编号）。"""
    existing_target = (
        existing.target_event_id
        if target_type is CorrectionTarget.own_event
        else existing.target_reading_id
    )
    return (
        existing.target_type == target_type
        and existing_target == target_id
        and existing.expected_version == expected_version
        and Decimal(existing.new_temperature) == Decimal(new_temperature)
    )


def _replay_view(
    row: TemperatureCorrection, cargo_ids: list[str]
) -> CorrectionResult:
    trip_id = row.trip_id if row.target_type is CorrectionTarget.vehicle_reading else None
    return CorrectionResult(
        correction=row,
        replayed=True,
        cargo_ids=sorted(set(cargo_ids)),
        effective_temperature=Decimal(row.new_temperature),
        version=row.version,
        trip_id=trip_id,
    )


def submit_correction(db, payload: CorrectionCreate) -> CorrectionResult:
    try:
        return _submit_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变。
        # OperationalError 也可能在 commit 已落库后抛出——重放前先按编号幂等
        # 检测（车载更正含车次修订号 +1，重复执行会多递增），已落库则返回原结果。
        db.rollback()
        target_type = payload.target_type_enum()
        winner = db.get(TemperatureCorrection, payload.correction_id)
        if winner is not None and _same_content(
            winner,
            target_type,
            payload.target_id,
            payload.expected_version,
            Decimal(payload.new_temperature),
        ):
            if winner.target_type is CorrectionTarget.own_event:
                cargo_ids = [winner.cargo_id]
            else:
                cargo_ids = _affected_cargo_ids(
                    db, winner.trip_id, _reading_at(db, winner.target_reading_id)
                )
            return _replay_view(winner, cargo_ids)
        return _submit_tx(db, payload)


def _replay_if_winner(
    db,
    correction_id,
    target_type,
    target_id,
    expected_version,
    new_temperature,
) -> CorrectionResult | None:
    """并发竞争后按已落库的“赢家”行判定：同内容返回原结果，异内容返回 None。"""
    winner = db.get(TemperatureCorrection, correction_id)
    if winner is None:
        return None
    if not _same_content(
        winner, target_type, target_id, expected_version, new_temperature
    ):
        raise _conflict(
            f"更正ID {correction_id} 已存在且内容不同，拒绝覆盖",
            code="correction_id_conflict",
        )
    if winner.target_type is CorrectionTarget.own_event:
        cargo_ids = [winner.cargo_id]
    else:
        cargo_ids = _affected_cargo_ids(
            db, winner.trip_id, _reading_at(db, winner.target_reading_id)
        )
    return _replay_view(winner, cargo_ids)


def _submit_tx(db, payload: CorrectionCreate) -> CorrectionResult:
    target_type = payload.target_type_enum()
    expected_version = payload.expected_version
    new_temperature = Decimal(payload.new_temperature).quantize(Decimal("0.001"))
    correction_id = payload.correction_id
    target_id = payload.target_id

    # 幂等快路径：更正ID已存在时（通常是串行重传），无需加锁即可裁决——
    # 同内容直接返回原结果，异内容直接冲突。并发首传竞争由持锁后的复查兜底。
    existing = db.get(TemperatureCorrection, correction_id)
    if existing is not None:
        if not _same_content(
            existing, target_type, target_id, expected_version, new_temperature
        ):
            raise _conflict(
                f"更正ID {correction_id} 已存在且内容不同，拒绝覆盖",
                code="correction_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        if existing.target_type is CorrectionTarget.own_event:
            cargo_ids = [existing.cargo_id]
        else:
            cargo_ids = _affected_cargo_ids(
                db, existing.trip_id,
                _reading_at(db, existing.target_reading_id),
            )
        return _replay_view(existing, cargo_ids)

    try:
        if target_type is CorrectionTarget.own_event:
            result = _submit_own_event(
                db, correction_id, target_id, expected_version, new_temperature
            )
        else:
            result = _submit_vehicle_reading(
                db, correction_id, target_id, expected_version, new_temperature
            )
        db.commit()
    except ApiError:
        db.rollback()
        # 并发竞争（SQLite 文件库下 FOR UPDATE 为空操作）：版本过期可能只因
        # 同内容并发更正已先提交——此时按幂等返回原结果。
        replay = _concurrent_replay_after_conflict(
            db, target_type, target_id, correction_id, expected_version,
            new_temperature,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        db.rollback()
        replay = _replay_if_winner(
            db, correction_id, target_type, target_id, expected_version,
            new_temperature,
        )
        if replay is not None:
            return replay
        # 版本唯一约束兜底（锁串行下不应触发）
        raise _conflict(
            f"更正 {correction_id} 与既有数据冲突（目标版本号竞争）",
            code="stale_version",
        )

    result.correction.created_at = ensure_utc(result.correction.created_at)
    return result


def _concurrent_replay_after_conflict(
    db, target_type, target_id, correction_id, expected_version,
    new_temperature,
) -> CorrectionResult | None:
    """stale_version 回滚后：若赢家是同更正ID同内容，按幂等返回；否则 None。"""
    winner = db.get(TemperatureCorrection, correction_id)
    if winner is None:
        return None
    if _same_content(
        winner, target_type, target_id, expected_version, new_temperature
    ):
        if winner.target_type is CorrectionTarget.own_event:
            cargo_ids = [winner.cargo_id]
        else:
            cargo_ids = _affected_cargo_ids(
                db, winner.trip_id, _reading_at(db, winner.target_reading_id)
            )
        return _replay_view(winner, cargo_ids)
    return None


def _reading_at(db, reading_id: str):
    reading = db.get(VehicleReading, reading_id)
    return ensure_utc(reading.read_at) if reading is not None else None


def _submit_own_event(
    db, correction_id, event_id, expected_version, new_temperature
) -> CorrectionResult:
    # 目标先定位（用于确定锁哪一票货物）；不存在 -> 404
    event = db.get(Event, event_id)
    if event is None:
        raise ApiError(404, "event_not_found", f"测温事件 {event_id} 不存在")
    if event.event_type is not EventType.measurement:
        raise _unprocessable(
            f"事件 {event_id} 是交接事件，交接不可更正",
            code="handover_not_correctable",
        )

    # 锁货物行，与该货物的事件 / 表态 / 读数 / 装卸登记串行
    cargo = _load_cargo_locked(db, event.cargo_id)

    # 持锁复核：目标行在加锁期间不会被删（无删除路径），但保持与读数路径对称
    event = db.get(Event, event_id)
    if event is None:
        raise ApiError(404, "event_not_found", f"测温事件 {event_id} 不存在")
    if event.event_type is not EventType.measurement:
        raise _unprocessable(
            f"事件 {event_id} 是交接事件，交接不可更正",
            code="handover_not_correctable",
        )

    # 幂等在持锁后判定（并发重传在此串行分出唯一赢家）
    replay = _locked_replay(
        db, correction_id, CorrectionTarget.own_event, event_id,
        expected_version, new_temperature,
    )
    if replay is not None:
        return replay

    current_version = _current_version(
        db, CorrectionTarget.own_event, event_id
    )
    if expected_version != current_version:
        raise _conflict(
            f"版本过期：事件 {event_id} 当前版本为 {current_version}，"
            f"请求期望版本为 {expected_version}，请重新查询有效温度后更正",
            code="stale_version",
        )

    row = TemperatureCorrection(
        correction_id=correction_id,
        target_type=CorrectionTarget.own_event,
        cargo_id=cargo.cargo_id,
        target_event_id=event_id,
        target_reading_id=None,
        trip_id=None,
        expected_version=expected_version,
        version=expected_version + 1,
        new_temperature=new_temperature,
    )
    db.add(row)
    db.flush()
    return CorrectionResult(
        correction=row,
        replayed=False,
        cargo_ids=[cargo.cargo_id],
        effective_temperature=new_temperature,
        version=row.version,
        trip_id=None,
    )


def _submit_vehicle_reading(
    db, correction_id, reading_id, expected_version, new_temperature
) -> CorrectionResult:
    reading = db.get(VehicleReading, reading_id)
    if reading is None:
        raise ApiError(404, "reading_not_found", f"车载读数 {reading_id} 不存在")
    trip_id = reading.trip_id
    read_at = ensure_utc(reading.read_at)

    # 车次须存在（外键正常情况下恒成立，防御性校验）
    if db.get(Trip, trip_id) is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

    # 车载更正须同时影响读数时刻在车的全部货物：先锁货物行（升序），再锁车次行
    affected = _affected_cargo_ids(db, trip_id, read_at)
    _lock_cargo_rows(db, affected)
    trip = _load_trip_locked(db, trip_id)

    reading = db.get(VehicleReading, reading_id)
    if reading is None:
        raise ApiError(
            404, "reading_not_found", f"车载读数 {reading_id} 不存在"
        )

    # 幂等在持锁后判定
    replay = _locked_replay(
        db, correction_id, CorrectionTarget.vehicle_reading, reading_id,
        expected_version, new_temperature,
    )
    if replay is not None:
        replay.cargo_ids = affected
        return replay

    current_version = _current_version(
        db, CorrectionTarget.vehicle_reading, reading_id
    )
    if expected_version != current_version:
        raise _conflict(
            f"版本过期：车载读数 {reading_id} 当前版本为 {current_version}，"
            f"请求期望版本为 {expected_version}，请重新查询有效温度后更正",
            code="stale_version",
        )

    row = TemperatureCorrection(
        correction_id=correction_id,
        target_type=CorrectionTarget.vehicle_reading,
        cargo_id=None,
        target_event_id=None,
        target_reading_id=reading_id,
        trip_id=trip_id,
        expected_version=expected_version,
        version=expected_version + 1,
        new_temperature=new_temperature,
    )
    db.add(row)
    # 车载读数更正在该车次落库 -> 车次修订号 +1（同事务，版本过期回滚则不递增）
    _bump_trip_revision(db, trip)
    db.flush()
    return CorrectionResult(
        correction=row,
        replayed=False,
        cargo_ids=affected,
        effective_temperature=new_temperature,
        version=row.version,
        trip_id=trip_id,
    )


def _locked_replay(
    db, correction_id, target_type, target_id, expected_version,
    new_temperature,
) -> CorrectionResult | None:
    """持锁后判定更正ID幂等：已存在则同内容返回原结果（replayed 标记），
    异内容抛 409；不存在返回 None 继续正常写入。

    与事件 / 表态的“先按主键查再持锁”不同，更正的版本竞争必须在目标锁内
    裁决，故幂等也在持锁后判定。
    """
    existing = db.get(TemperatureCorrection, correction_id)
    if existing is None:
        return None
    if not _same_content(
        existing, target_type, target_id, expected_version, new_temperature
    ):
        raise _conflict(
            f"更正ID {correction_id} 已存在且内容不同，拒绝覆盖",
            code="correction_id_conflict",
        )
    if existing.target_type is CorrectionTarget.own_event:
        cargo_ids = [existing.cargo_id]
    else:
        cargo_ids = _affected_cargo_ids(
            db, existing.trip_id, _reading_at(db, existing.target_reading_id)
        )
    return _replay_view(existing, cargo_ids)
