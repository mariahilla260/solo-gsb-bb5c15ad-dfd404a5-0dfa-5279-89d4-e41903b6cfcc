"""装卸时段更正的写入 / 查询事务。

装卸时段录错后，调度员可凭**更正编号**、**原车次 + 货物 + 装车时刻**（被更正
窗口的稳定身份）、**期望版本**、**修正后的带时区装卸时刻**与**原因**更正已有
登记。更正**只改这一条装卸窗口的当前时间**——车次 / 货物归属与稳定身份
（首次登记装车时刻 ``origin_loaded_at``）永不改变，也不影响其它窗口。

存储模型
--------
* ``loading`` 行就地保存**当前有效窗口**（``loaded_at`` / ``unloaded_at``）与
  更正版本 ``window_version``（0=原始），稳定身份列 ``origin_loaded_at`` /
  ``origin_unloaded_at`` 永不改变——因此既有“按当前窗口”的全部读路径
  （车载适用性、时间线、越温报告、同时越温、车次详情）自动反映更正；
* 每次更正只向 ``loading_correction`` 追加一行（append-only），记录原始窗口、
  更正前窗口、新窗口、期望 / 实际版本与原因，原窗口和历次更正均可追溯。

校验（全部在持锁事务内，失败整次回滚、不留部分变更）
----------------------------------------------------
* 新窗口仍左闭右开且起点严格早于终点 → 否则 ``422 invalid_loading_window``；
* 被更正窗口须存在（按稳定身份定位当前 loading 行）→ 否则
  ``404 loading_window_not_found``；
* ``expected_version`` 须等于该窗口当前更正版本 → 否则
  ``409 stale_window_version``；
* 按“同一货物在**任意车次**的当前时段不得重叠”校验，**排除该条旧窗口本身**
  （首尾相接允许），冲突 → ``409 loading_overlap``，整次拒绝。

加锁与串行（全系统统一锁顺序：先货物行升序、再车次行，见 app/vehicle_crud.py）
--------------------------------------------------------------------------------
锁定该窗口所属货物行与车次行，故更正与该货物的事件写入、责任表态、车载读数
写入 / 测温更正、车次装卸登记严格串行；校验所见必为持锁期间的最新窗口与时间线。
成功落库时同事务把车次修订号 +1（旧装卸预检摘要随之失效）。

幂等 / 冲突
-----------
``correction_id`` 同请求（原身份、期望版本、新窗口、原因全相同）重传返回原结果
（``200 replayed``）；同编号异请求 → ``409 correction_id_conflict``。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.crud import ApiError
from app.models import Loading, LoadingCorrection, Trip
from app.schemas import LoadingWindowCorrectionCreate
from app.timeutil import ensure_utc
from app.vehicle_crud import (
    _bump_trip_revision,
    _load_trip_locked,
    _lock_cargo_rows,
    _windows_overlap,
)


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


# ----------------------------------------------------------- 读取辅助


def load_window_corrections(
    db, trip_id: str, cargo_id: str, origin_loaded_at
) -> list[LoadingCorrection]:
    """一条装卸窗口的全部更正（按版本升序）。"""
    return list(
        db.scalars(
            select(LoadingCorrection)
            .where(
                LoadingCorrection.trip_id == trip_id,
                LoadingCorrection.cargo_id == cargo_id,
                LoadingCorrection.origin_loaded_at == origin_loaded_at,
            )
            .order_by(
                LoadingCorrection.version, LoadingCorrection.correction_id
            )
        ).all()
    )


def get_current_loading(
    db, trip_id: str, cargo_id: str, origin_loaded_at
) -> Loading | None:
    """按稳定身份（车次 + 货物 + 首次登记装车时刻）定位当前 loading 行。"""
    return db.get(Loading, (trip_id, cargo_id, origin_loaded_at))


# ----------------------------------------------------------- 提交


@dataclass
class LoadingCorrectionResult:
    correction: LoadingCorrection
    replayed: bool
    # 更正后该窗口的当前窗口与版本
    loaded_at: object
    unloaded_at: object
    origin_loaded_at: object
    origin_unloaded_at: object
    version: int
    cargo_id: str
    trip_id: str


def _same_content(
    existing: LoadingCorrection,
    cargo_id: str,
    origin_loaded_at,
    expected_version: int,
    new_loaded_at,
    new_unloaded_at,
    reason: str,
) -> bool:
    """更正编号相同前提下逐字段比对请求内容（身份、期望版本、新窗口、原因）。"""
    return (
        existing.cargo_id == cargo_id
        and ensure_utc(existing.origin_loaded_at) == origin_loaded_at
        and existing.expected_version == expected_version
        and ensure_utc(existing.new_loaded_at) == new_loaded_at
        and ensure_utc(existing.new_unloaded_at) == new_unloaded_at
        and existing.reason == reason
    )


def _replay_view(row: LoadingCorrection) -> LoadingCorrectionResult:
    return LoadingCorrectionResult(
        correction=row,
        replayed=True,
        loaded_at=ensure_utc(row.new_loaded_at),
        unloaded_at=ensure_utc(row.new_unloaded_at),
        origin_loaded_at=ensure_utc(row.origin_loaded_at),
        origin_unloaded_at=ensure_utc(row.origin_unloaded_at),
        version=row.version,
        cargo_id=row.cargo_id,
        trip_id=row.trip_id,
    )


def submit_loading_correction(
    db, trip_id: str, payload: LoadingWindowCorrectionCreate
) -> LoadingCorrectionResult:
    try:
        return _submit_tx(db, trip_id, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变。
        # OperationalError 也可能在 commit 已落库后抛出（含车次修订号 +1），
        # 重放前先按编号幂等检测，已落库则返回原结果，避免修订号多递增。
        db.rollback()
        winner = db.get(LoadingCorrection, payload.correction_id)
        if (
            winner is not None
            and winner.trip_id == trip_id
            and _same_content(
                winner,
                payload.cargo_id,
                ensure_utc(payload.origin_loaded_at),
                payload.expected_version,
                ensure_utc(payload.new_loaded_at),
                ensure_utc(payload.new_unloaded_at),
                payload.reason,
            )
        ):
            return _replay_view(winner)
        return _submit_tx(db, trip_id, payload)


def _submit_tx(
    db, trip_id: str, payload: LoadingWindowCorrectionCreate
) -> LoadingCorrectionResult:
    cargo_id = payload.cargo_id
    origin_loaded_at = ensure_utc(payload.origin_loaded_at)
    new_loaded_at = ensure_utc(payload.new_loaded_at)
    new_unloaded_at = ensure_utc(payload.new_unloaded_at)
    expected_version = payload.expected_version
    reason = payload.reason
    correction_id = payload.correction_id

    # 幂等快路径：更正编号已存在（通常是串行重传）时无需加锁即可裁决。
    existing = db.get(LoadingCorrection, correction_id)
    if existing is not None:
        if not _same_content(
            existing,
            cargo_id,
            origin_loaded_at,
            expected_version,
            new_loaded_at,
            new_unloaded_at,
            reason,
        ) or existing.trip_id != trip_id:
            raise _conflict(
                f"更正编号 {correction_id} 已存在且请求内容不同，拒绝覆盖",
                code="correction_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        return _replay_view(existing)

    try:
        # 车次须存在（先读；真正互斥在拿到车次行锁后）
        if db.get(Trip, trip_id) is None:
            raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

        # 锁顺序与全系统一致：先货物行（升序），再车次行
        _lock_cargo_rows(db, [cargo_id])
        trip = _load_trip_locked(db, trip_id)

        # 持锁后定位被更正窗口（稳定身份）
        loading = get_current_loading(db, trip_id, cargo_id, origin_loaded_at)
        if loading is None:
            raise ApiError(
                404,
                "loading_window_not_found",
                f"车次 {trip_id} 货物 {cargo_id} 在 "
                f"{origin_loaded_at.isoformat()} 装车的装卸窗口不存在",
            )

        # 幂等在持锁后判定（并发首传竞争在此串行分出唯一赢家）
        winner = db.get(LoadingCorrection, correction_id)
        if winner is not None:
            if not _same_content(
                winner,
                cargo_id,
                origin_loaded_at,
                expected_version,
                new_loaded_at,
                new_unloaded_at,
                reason,
            ) or winner.trip_id != trip_id:
                raise _conflict(
                    f"更正编号 {correction_id} 已存在且请求内容不同，拒绝覆盖",
                    code="correction_id_conflict",
                )
            db.rollback()
            return _replay_view(winner)

        # 乐观锁：期望版本须等于当前窗口版本
        current_version = loading.window_version or 0
        if expected_version != current_version:
            raise _conflict(
                f"版本过期：车次 {trip_id} 货物 {cargo_id} 装车时刻 "
                f"{origin_loaded_at.isoformat()} 的装卸窗口当前版本为 "
                f"{current_version}，请求期望版本为 {expected_version}，"
                "请重新查询车次详情（window_version）后再更正",
                code="stale_window_version",
            )

        previous_loaded_at = ensure_utc(loading.loaded_at)
        previous_unloaded_at = ensure_utc(loading.unloaded_at)
        origin_unloaded_at = ensure_utc(loading.origin_unloaded_at)

        # 重叠校验：同一货物任意车次的“当前窗口”不得与新窗口重叠，
        # 但排除该条旧窗口本身（首尾相接允许）。
        others = list(
            db.scalars(
                select(Loading).where(Loading.cargo_id == cargo_id)
            ).all()
        )
        for other in others:
            if (
                other.trip_id == trip_id
                and ensure_utc(other.origin_loaded_at) == origin_loaded_at
            ):
                continue  # 被更正的这条旧窗口自身，排除
            o_lo = ensure_utc(other.loaded_at)
            o_un = ensure_utc(other.unloaded_at)
            if _windows_overlap(new_loaded_at, new_unloaded_at, o_lo, o_un):
                raise _conflict(
                    f"货物 {cargo_id} 更正后的装卸时段 "
                    f"[{new_loaded_at.isoformat()}, {new_unloaded_at.isoformat()}) "
                    f"与车次 {other.trip_id} 的当前窗口 "
                    f"[{o_lo.isoformat()}, {o_un.isoformat()}) 重叠；"
                    "同一货物不得同时属于两个车次",
                    code="loading_overlap",
                )

        new_version = current_version + 1
        row = LoadingCorrection(
            correction_id=correction_id,
            trip_id=trip_id,
            cargo_id=cargo_id,
            origin_loaded_at=origin_loaded_at,
            origin_unloaded_at=origin_unloaded_at,
            previous_loaded_at=previous_loaded_at,
            previous_unloaded_at=previous_unloaded_at,
            new_loaded_at=new_loaded_at,
            new_unloaded_at=new_unloaded_at,
            expected_version=expected_version,
            version=new_version,
            reason=reason,
        )
        db.add(row)

        # 当前窗口就地更新（稳定身份列与归属不动）
        loading.loaded_at = new_loaded_at
        loading.unloaded_at = new_unloaded_at
        loading.window_version = new_version

        # 装卸时段更正在该车次落库 -> 车次修订号 +1（失败回滚则不递增）
        _bump_trip_revision(db, trip)
        db.commit()
    except ApiError:
        db.rollback()
        # SQLite 文件库下 FOR UPDATE 为空操作：stale/冲突可能只因同内容并发更正
        # 已先提交——此时按幂等返回原结果。
        replay = _replay_after_conflict(
            db,
            correction_id,
            trip_id,
            cargo_id,
            origin_loaded_at,
            expected_version,
            new_loaded_at,
            new_unloaded_at,
            reason,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        db.rollback()
        replay = _replay_after_conflict(
            db,
            correction_id,
            trip_id,
            cargo_id,
            origin_loaded_at,
            expected_version,
            new_loaded_at,
            new_unloaded_at,
            reason,
        )
        if replay is not None:
            return replay
        raise _conflict(
            f"装卸时段更正 {correction_id} 与既有数据冲突（窗口版本号竞争）",
            code="stale_window_version",
        )

    return LoadingCorrectionResult(
        correction=row,
        replayed=False,
        loaded_at=new_loaded_at,
        unloaded_at=new_unloaded_at,
        origin_loaded_at=origin_loaded_at,
        origin_unloaded_at=origin_unloaded_at,
        version=new_version,
        cargo_id=cargo_id,
        trip_id=trip_id,
    )


def _replay_after_conflict(
    db,
    correction_id,
    trip_id,
    cargo_id,
    origin_loaded_at,
    expected_version,
    new_loaded_at,
    new_unloaded_at,
    reason,
) -> LoadingCorrectionResult | None:
    """回滚后：若已落库的“赢家”是同编号同请求，按幂等返回原结果；否则 None。"""
    winner = db.get(LoadingCorrection, correction_id)
    if winner is None:
        return None
    if winner.trip_id != trip_id or not _same_content(
        winner,
        cargo_id,
        origin_loaded_at,
        expected_version,
        new_loaded_at,
        new_unloaded_at,
        reason,
    ):
        return None
    return _replay_view(winner)
