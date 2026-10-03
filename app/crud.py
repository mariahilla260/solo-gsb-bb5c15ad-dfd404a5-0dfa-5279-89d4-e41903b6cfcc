"""写入逻辑与一致性校验。

所有事件写入在单事务内完成：
1. 锁定货物行（PostgreSQL SELECT ... FOR UPDATE），串行化同一货物的并发写入；
2. 幂等：event_id 已存在则逐字段比对，相同内容直接返回（不重复写入），
   不同内容拒绝；
3. 把候选事件并入既有事件做“整链校验”：
   - 事件时间不得早于货物起始时间；
   - 同一货物同一时刻至多一笔测温、至多一笔交接；
   - 沿事件流模拟保管链，每笔交接的交出方必须等于交接时刻的当前保管方
     （晚到的插入若使既有交接失效，同样整体拒绝）；
4. 校验失败即回滚——既有结果保持不变；
5. 提交时由唯一索引 / CHECK 约束兜底并发竞争。
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import chain
from app.models import Cargo, Event, EventType
from app.schemas import CargoCreate, EventCreate
from app.timeutil import ensure_utc

# 车载读数开启区间的标识前缀（veh:<reading_id>）被保留，
# 货物自有事件ID不得此前缀，以免与车载区间标识冲突
_RESERVED_EVENT_PREFIX = "veh:"


class ApiError(Exception):
    """业务校验错误，由路由层映射为 HTTP 状态码。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _conflict(message: str, code: str = "conflict") -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str = "validation_failed") -> ApiError:
    return ApiError(422, code, message)


@dataclass
class WriteResult:
    event: Event
    replayed: bool


def create_cargo(db, payload: CargoCreate) -> Cargo:
    existing = db.get(Cargo, payload.cargo_id)
    if existing is not None:
        raise _conflict(
            f"货物编号 {payload.cargo_id} 已存在", code="cargo_exists"
        )
    cargo = Cargo(
        cargo_id=payload.cargo_id,
        started_at=payload.started_at,
        temp_upper_limit=payload.temp_upper_limit,
        initial_holder=payload.initial_holder,
    )
    db.add(cargo)
    try:
        # expire_on_commit=False：提交后直接返回内存对象，无需额外查询
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"货物编号 {payload.cargo_id} 已存在", code="cargo_exists"
        )
    return cargo


def _load_cargo_locked(db, cargo_id: str) -> Cargo:
    stmt = select(Cargo).where(Cargo.cargo_id == cargo_id).with_for_update()
    cargo = db.scalars(stmt).first()
    if cargo is None:
        raise ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    return cargo


def _load_events(db, cargo_id: str) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    return events


def _same_content(existing: Event, candidate: Event) -> bool:
    """逐字段比对（event_id 已相同）。Decimal 按数值比较，时间归一化到 UTC。"""
    return (
        existing.cargo_id == candidate.cargo_id
        and existing.event_type == candidate.event_type
        and ensure_utc(existing.occurred_at) == candidate.occurred_at
        and existing.temperature == candidate.temperature
        and existing.giver == candidate.giver
        and existing.receiver == candidate.receiver
    )


def _validate_prospective(cargo: Cargo, events: list[Event]) -> None:
    """对“既有事件 + 候选事件”做整链校验，不合法即抛 ApiError。"""
    holder = cargo.initial_holder
    points = chain._sorted_events(events)

    # 1) 沿事件流模拟保管链：交出方必须等于交接时刻的当前保管方
    #    （晚到的插入若使既有交接失效，同样整体拒绝）
    for p in points:
        if p.is_handover:
            if p.giver != holder:
                raise _unprocessable(
                    f"交接 {p.source_id} 的交出方 {p.giver} 不是 "
                    f"{p.occurred_at.isoformat()} 时刻的保管方 {holder}，"
                    "写入被整体拒绝",
                    code="giver_mismatch",
                )
            holder = p.receiver

    # 2) 同一货物同一时刻同类型事件冲突（测温/交接互不冲突）
    seen_measure_at: set = set()
    seen_handover_at: set = set()
    for p in points:
        bucket = seen_measure_at if p.is_measurement else seen_handover_at
        if p.occurred_at in bucket:
            raise _conflict(
                f"货物 {cargo.cargo_id} 在 {p.occurred_at.isoformat()} "
                f"已存在一笔{'测温' if p.is_measurement else '交接'}，"
                "同一时刻不允许重复",
                code="same_timestamp_conflict",
            )
        bucket.add(p.occurred_at)


def create_event(db, payload: EventCreate) -> WriteResult:
    try:
        etype = payload.event_type()
    except ValueError as exc:
        raise _unprocessable(str(exc), code="bad_event_shape")

    if payload.event_id.startswith(_RESERVED_EVENT_PREFIX):
        raise _unprocessable(
            f"事件ID {payload.event_id!r} 以保留前缀 "
            f"{_RESERVED_EVENT_PREFIX!r} 开头（用于车载读数区间标识），请更换编号",
            code="reserved_event_id",
        )

    occurred_at = ensure_utc(payload.occurred_at)
    candidate = Event(
        event_id=payload.event_id,
        cargo_id=payload.cargo_id,
        event_type=etype,
        occurred_at=occurred_at,
        temperature=payload.temperature if etype is EventType.measurement else None,
        giver=payload.giver if etype is EventType.handover else None,
        receiver=payload.receiver if etype is EventType.handover else None,
    )

    try:
        # SELECT ... FOR UPDATE 锁定货物行，串行化同货物并发写入
        cargo = _load_cargo_locked(db, payload.cargo_id)

        # 早于起始时间的事件拒绝
        if candidate.occurred_at < cargo.started_at:
            raise _unprocessable(
                f"事件发生时间 {candidate.occurred_at.isoformat()} 早于货物起始时间 "
                f"{cargo.started_at.isoformat()}",
                code="before_start",
            )

        # 幂等：同 ID 同内容重传不重复，异内容拒绝
        existing = db.get(Event, payload.event_id)
        if existing is not None:
            if _same_content(existing, candidate):
                db.rollback()  # 仅释放货物行锁，未做任何修改
                return WriteResult(event=existing, replayed=True)
            raise _conflict(
                f"事件ID {payload.event_id} 已存在且内容不同，拒绝覆盖",
                code="event_id_conflict",
            )

        events = _load_events(db, payload.cargo_id)
        events.append(candidate)
        _validate_prospective(cargo, events)

        db.add(candidate)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"事件 {payload.event_id} 与既有数据冲突"
            "（同一时刻重复事件 / 事件ID冲突 / 字段约束）",
            code="integrity_conflict",
        )

    return WriteResult(event=candidate, replayed=False)
