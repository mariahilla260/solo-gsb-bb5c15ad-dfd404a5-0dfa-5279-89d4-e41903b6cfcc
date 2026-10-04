"""责任确认表态的写入与查询。

写入在单事务内完成，与事件写入、车载读数写入共用同一把“货物行锁”
（``SELECT ... FOR UPDATE``），因此新表态提交与同货物的事件 / 读数写入严格
串行化：校验所用快照一定来自持锁期间在合并读数时间线上重建出的最新结果。

区间稳定标识（interval_id）：货物自有测温开启=测温事件ID；
车载读数开启=``veh:<reading_id>``（见 app/chain.py）。

拒绝场景（一律不落库）：
* 表态编号已存在：同内容返回原记录（幂等，即使其快照已失效）；异内容 409；
* 表态形状非法（confirm 带理由 / dispute 无理由 / 未知取值）→ 422；
* 区间不存在 → 404；区间仍开放 → 422；
* 提交的快照标识与当前快照不一致（过期）→ 409；
* 责任方不在当前责任片段中 → 422。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import responsibility as resp
from app.chain import SOURCE_VEHICLE, interval_id
from app.crud import ApiError, _load_cargo_locked
from app.models import IntervalSource, Stance, StanceKind
from app.schemas import StanceCreate
from app.timeline import load_cargo_context

# 车载区间标识前缀（与 app/chain.VEHICLE_INTERVAL_PREFIX 保持一致）
_VEH_PREFIX = "veh:"


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


# ----------------------------------------------------------- 读取辅助


def _rebuild_closed(db, cargo):
    """持锁/读事务内在合并读数时间线上重建，返回 (chain结果, 闭合区间映射)。"""
    events, loadings, readings, effective, active_faults = load_cargo_context(
        db, cargo.cargo_id
    )
    return resp.rebuild(
        cargo, events, loadings, readings, effective, active_faults
    )


def _resolve_interval(db, cargo, interval_key: str, require_closed: bool = True):
    """按区间稳定标识（事件ID / veh:<读数ID>）定位当前区间。"""
    result, closed = _rebuild_closed(db, cargo)
    ex = closed.get(interval_key)
    if ex is not None:
        return result, ex
    # 可能是开放区间
    if require_closed and any(
        interval_id(e) == interval_key for e in result.excursions
    ):
        raise _unprocessable(
            f"区间 {interval_key} 尚未闭合，开放区间不接受责任表态",
            code="interval_open",
        )
    raise ApiError(
        404,
        "interval_not_found",
        f"货物 {cargo.cargo_id} 不存在标识为 {interval_key} 的已闭合越温区间",
    )


# ----------------------------------------------------------- 提交


@dataclass
class StanceWriteResult:
    stance: Stance
    replayed: bool


def _parse_kind(payload: StanceCreate) -> StanceKind:
    raw = (payload.stance or "").strip().lower()
    reason = (payload.reason or "").strip() if payload.reason is not None else None
    if raw == "confirm":
        if reason:
            raise _unprocessable(
                "确认（confirm）表态不得附带理由",
                code="bad_stance_shape",
            )
        payload.stance = "confirm"
        payload.reason = None
        return StanceKind.confirm
    if raw == "dispute":
        if not reason:
            raise _unprocessable(
                "异议（dispute）表态必须提供非空理由 reason",
                code="dispute_reason_required",
            )
        payload.stance = "dispute"
        payload.reason = reason
        return StanceKind.dispute
    raise _unprocessable(
        f"未知表态 {payload.stance!r}，只允许 confirm 或 dispute",
        code="bad_stance_shape",
    )


def _same_stance_content(
    existing: Stance,
    cargo_id: str,
    interval_key: str,
    payload: StanceCreate,
    kind: StanceKind,
) -> bool:
    """逐字段比对（stance_id 已相同）。"""
    return (
        existing.cargo_id == cargo_id
        and existing.interval_key == interval_key
        and existing.responsible_party == payload.responsible_party
        and StanceKind(existing.kind) is kind
        and (existing.reason or None)
        == ((payload.reason or None) if kind is StanceKind.dispute else None)
        and existing.snapshot_id == payload.snapshot_id
    )


def submit_stance(db, cargo_id: str, interval_key: str, payload: StanceCreate) -> StanceWriteResult:
    kind = _parse_kind(payload)
    # SQLite 文件库无行级锁，极端并发下 busy timeout 耗尽会抛 OperationalError，
    # 整体重试一次即可（行锁/快照校验全部重来，语义不变）；PG 不会触发。
    try:
        return _submit_stance_tx(db, cargo_id, interval_key, payload, kind)
    except OperationalError:
        db.rollback()
        return _submit_stance_tx(db, cargo_id, interval_key, payload, kind)


def _submit_stance_tx(db, cargo_id: str, interval_key: str, payload: StanceCreate, kind) -> StanceWriteResult:
    try:
        # 与事件 / 车载读数写入串行化：先锁货物行
        cargo = _load_cargo_locked(db, cargo_id)

        # 幂等优先：已存表态编号，同内容返回原记录（即使快照已失效）
        existing = db.get(Stance, payload.stance_id)
        if existing is not None:
            if _same_stance_content(existing, cargo_id, interval_key, payload, kind):
                db.rollback()  # 仅释放行锁，未做修改
                return StanceWriteResult(stance=existing, replayed=True)
            raise _conflict(
                f"表态编号 {payload.stance_id} 已存在且内容不同，拒绝覆盖；"
                "改口请使用新的表态编号",
                code="stance_id_conflict",
            )

        # 以当前合并时间线定位区间（不存在 / 开放 一律拒绝）
        _, ex = _resolve_interval(db, cargo, interval_key)

        # 快照校验：必须针对当前快照表态
        current_snapshot = resp.snapshot_id(ex)
        if payload.snapshot_id != current_snapshot:
            raise _conflict(
                f"快照已过期：提交的快照 {payload.snapshot_id} 与当前快照 "
                f"{current_snapshot} 不一致（区间边界或责任片段可能已因晚到事件 / "
                "车载读数变化），请重新查询后表态",
                code="stale_snapshot",
            )

        # 责任方必须出现在当前片段中
        parties = list(dict.fromkeys(seg.party for seg in ex.segments))
        if payload.responsible_party not in parties:
            raise _unprocessable(
                f"责任方 {payload.responsible_party} 不在区间 {interval_key} "
                f"的当前责任片段中（当前责任方：{', '.join(parties)}）",
                code="party_not_in_segment",
            )

        is_vehicle = ex.start_source == SOURCE_VEHICLE
        stance = Stance(
            stance_id=payload.stance_id,
            cargo_id=cargo_id,
            interval_key=interval_key,
            opener_source=(
                IntervalSource.vehicle if is_vehicle else IntervalSource.own
            ),
            start_event_id=(None if is_vehicle else ex.start_event_id),
            vehicle_reading_id=(
                ex.start_reading_id if is_vehicle else None
            ),
            responsible_party=payload.responsible_party,
            kind=kind,
            reason=payload.reason if kind is StanceKind.dispute else None,
            snapshot_id=current_snapshot,
            attested_segments=resp.attested_segments_json(
                ex, payload.responsible_party
            ),
        )
        db.add(stance)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        # PostgreSQL 下货物行锁已把同货物写入串行，不会走到这里；
        # SQLite 的 FOR UPDATE 为空操作，并发同主键插入可能直接触发唯一约束。
        # 回滚后重查：同内容按幂等返回，异内容按冲突拒绝。
        db.rollback()
        winner = db.get(Stance, payload.stance_id)
        if winner is not None and _same_stance_content(
            winner, cargo_id, interval_key, payload, kind
        ):
            return StanceWriteResult(stance=winner, replayed=True)
        if winner is not None:
            raise _conflict(
                f"表态编号 {payload.stance_id} 已存在且内容不同，拒绝覆盖；"
                "改口请使用新的表态编号",
                code="stance_id_conflict",
            )
        raise _conflict(
            f"表态 {payload.stance_id} 与既有数据冲突"
            "（表态编号冲突 / 字段约束）",
            code="integrity_conflict",
        )

    return StanceWriteResult(stance=stance, replayed=False)


def load_interval_stances(db, cargo_id: str, interval_key: str) -> list[Stance]:
    """按稳定区间标识读取该区间全部表态行（含历史、已失效）。"""
    return list(
        db.scalars(
            select(Stance)
            .where(
                Stance.cargo_id == cargo_id,
                Stance.interval_key == interval_key,
            )
            .order_by(Stance.created_at, Stance.stance_id)
        ).all()
    )
