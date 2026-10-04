"""车次、装卸时段与车载测温读数的写入 / 查询。

加锁顺序（全系统统一，避免死锁）
--------------------------------
**先按 cargo_id 升序锁定受影响货物行，再锁定车次行**；自有事件写入与责任表态
只锁单个货物行。因此：

* 车载读数写入与“受影响货物”的事件写入、责任表态严格串行——读数持锁期间，
  受影响货物的越温时间线不会被别的事务改动；
* 装卸登记与车载读数写入对同一车次/货物也经货物行锁 + 车次行锁串行。

装卸时段登记（manifest）为批量原子操作：任一货物不存在、任一时段非法
（起点不早于终点）或与该货物既有/同批时段重叠（跨车次重叠即“同货物同时
属于两个车次”），整批拒绝，不留部分记录。

车载读数允许晚到；reading_id 同内容重传幂等、异内容冲突；同车次同刻至多
一笔读数。读数本身不改变任何货物的保管方（交接仍只由货物自有事件产生）。

车次修订号与装卸前预检
----------------------
车次行维护单调递增的 ``revision``（初始 0）：该车次的**装卸登记**、
**车载读数写入**、**车载读数更正**与**装卸时段更正**在持车次行锁的
同一事务内使其 +1。
登记装卸前可先预检（``precheck_manifest``，只读）：按“既有装卸 ∪ 本批
拟登记”输出各重叠时段的货物集合、温度上限差异、既有车载读数覆盖与
潜在越温风险，并给出绑定车次修订号、涉及货物温度上限与读数版本的
**预检摘要**；确认登记（``confirm_manifest``）在持锁事务内重算摘要，
摘要过期（车次 / 货物 / 读数版本变化）即拒绝，通过后沿用同一套批量
原子校验（任一时段冲突整批不落库）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import chain, overlap, precheck, timeline
from app.crud import ApiError
from app.models import Cargo, Event, Loading, Trip, VehicleReading
from app.schemas import (
    ManifestConfirmCreate,
    ManifestCreate,
    ManifestPrecheckCreate,
    TripCreate,
    VehicleReadingCreate,
)
from app.timeutil import ensure_utc


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


def _not_found(message: str) -> ApiError:
    return ApiError(404, "trip_not_found", message)


# ----------------------------------------------------------- 车次


def create_trip(db, payload: TripCreate) -> Trip:
    trip = Trip(
        trip_id=payload.trip_id,
        vehicle_no=payload.vehicle_no,
        note=payload.note,
    )
    db.add(trip)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict(f"车次编号 {payload.trip_id} 已存在", code="trip_exists")
    return trip


def list_trips(db) -> list[Trip]:
    return list(db.scalars(select(Trip).order_by(Trip.trip_id)).all())


def get_trip(db, trip_id: str) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise _not_found(f"车次 {trip_id} 不存在")
    return trip


def _load_trip_locked(db, trip_id: str) -> Trip:
    trip = db.scalars(
        select(Trip).where(Trip.trip_id == trip_id).with_for_update()
    ).first()
    if trip is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return trip


def _lock_cargo_rows(db, cargo_ids):
    """按 cargo_id 升序锁定货物行；任一不存在返回 404。"""
    from app.models import Cargo

    rows = {}
    missing = []
    for cargo_id in sorted(set(cargo_ids)):
        cargo = db.scalars(
            select(Cargo).where(Cargo.cargo_id == cargo_id).with_for_update()
        ).first()
        if cargo is None:
            missing.append(cargo_id)
        else:
            rows[cargo_id] = cargo
    if missing:
        raise ApiError(
            404,
            "cargo_not_found",
            f"货物 {', '.join(sorted(missing))} 不存在",
        )
    return rows


def _windows_overlap(lo1, un1, lo2, un2) -> bool:
    """两个左闭右开时段是否重叠（首尾相接不算重叠）。"""
    return lo1 < un2 and lo2 < un1


# ----------------------------------------------------------- 装卸登记


@dataclass
class ManifestResult:
    trip_id: str
    items: list[Loading]


def register_manifest(db, payload: ManifestCreate):
    try:
        return _register_manifest_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _register_manifest_tx(db, payload)


def confirm_manifest(db, payload: ManifestConfirmCreate):
    """携预检摘要确认登记装卸时段（批量原子，与直接登记同一套校验）。

    摘要在**持锁事务内**按当前数据重算：车次修订号、涉及货物温度上限或
    车次读数版本任一变化（摘要过期）即拒绝，不留任何记录。
    """
    try:
        return _register_manifest_tx(db, payload, expected_digest=payload.digest)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _register_manifest_tx(db, payload, expected_digest=payload.digest)


def _register_manifest_tx(
    db,
    payload: ManifestCreate | ManifestConfirmCreate,
    expected_digest: str | None = None,
) -> ManifestResult:
    # 1) 时段形状：左闭右开且起点严格早于终点（先做无锁校验，整批不落库）
    norm = []
    for it in payload.items:
        loaded_at = ensure_utc(it.loaded_at)
        unloaded_at = ensure_utc(it.unloaded_at)
        if loaded_at >= unloaded_at:
            raise _unprocessable(
                f"车次 {payload.trip_id} 货物 {it.cargo_id} 的装卸时段 "
                f"[{loaded_at.isoformat()}, {unloaded_at.isoformat()}) 无效："
                "装载时段必须左闭右开且起点严格早于终点",
                code="invalid_loading_window",
            )
        norm.append((it.cargo_id, loaded_at, unloaded_at))

    try:
        # 2) 车次须存在（先读；真正互斥在拿到车次行锁后）
        if db.get(Trip, payload.trip_id) is None:
            raise ApiError(
                404, "trip_not_found", f"车次 {payload.trip_id} 不存在"
            )

        # 3) 先按 cargo_id 升序锁定本批涉及的全部货物行
        _lock_cargo_rows(db, [c for c, _, _ in norm])

        # 4) 再锁定车次行
        trip = _load_trip_locked(db, payload.trip_id)

        # 4.1) 确认登记：持锁期间按当前数据重算预检摘要，过期即整批拒绝
        if expected_digest is not None:
            current_digest = _current_manifest_digest(db, trip, norm)
            if current_digest != expected_digest:
                raise _conflict(
                    f"预检摘要已过期：车次 {payload.trip_id} 的车次修订号、"
                    "涉及货物或车载读数版本在预检后发生变化，"
                    "请重新预检后再确认登记",
                    code="stale_precheck_digest",
                )

        # 5) 同批内同一货物时段不得互相重叠
        by_cargo: dict[str, list] = {}
        for cargo_id, lo, un in norm:
            for lo2, un2 in by_cargo.setdefault(cargo_id, []):
                if _windows_overlap(lo, un, lo2, un2):
                    raise _conflict(
                        f"货物 {cargo_id} 在本批登记中存在重叠装卸时段 "
                        f"[{lo.isoformat()}, {un.isoformat()}) 与 "
                        f"[{lo2.isoformat()}, {un2.isoformat()})",
                        code="loading_overlap",
                    )
            by_cargo[cargo_id].append((lo, un))

        # 6) 与该货物既有装卸时段（任意车次）比对：跨车次重叠即同货物同时属于两个车次
        existing = list(
            db.scalars(
                select(Loading).where(
                    Loading.cargo_id.in_(sorted({c for c, _, _ in norm}))
                )
            ).all()
        )
        for l in existing:
            l.loaded_at = ensure_utc(l.loaded_at)
            l.unloaded_at = ensure_utc(l.unloaded_at)
        for cargo_id, lo, un in norm:
            for l in existing:
                if l.cargo_id != cargo_id:
                    continue
                if _windows_overlap(lo, un, l.loaded_at, l.unloaded_at):
                    raise _conflict(
                        f"货物 {cargo_id} 的装卸时段 "
                        f"[{lo.isoformat()}, {un.isoformat()}) 与车次 {l.trip_id} "
                        f"既有时段 [{l.loaded_at.isoformat()}, "
                        f"{l.unloaded_at.isoformat()}) 重叠；"
                        "同一货物不得同时属于两个车次",
                        code="loading_overlap",
                    )

        rows = [
            Loading(
                trip_id=payload.trip_id,
                cargo_id=cargo_id,
                origin_loaded_at=lo,
                loaded_at=lo,
                unloaded_at=un,
                origin_unloaded_at=un,
                window_version=0,
            )
            for cargo_id, lo, un in norm
        ]
        db.add_all(rows)
        # 装卸登记落库 -> 车次修订号 +1（同事务，校验失败回滚则不递增）
        trip.revision = (trip.revision or 0) + 1
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"车次 {payload.trip_id} 的装卸登记与既有数据冲突"
            "（重复时段 / 车次或货物缺失）",
            code="integrity_conflict",
        )

    for l in rows:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)
    return ManifestResult(trip_id=payload.trip_id, items=rows)


# ----------------------------------------------------------- 装卸前预检


@dataclass
class PrecheckResult:
    trip_id: str
    trip_revision: int
    digest: str
    analyses: list  # list[precheck.SegmentAnalysis]


def _manifest_context(db, trip_id: str, norm_items):
    """预检 / 确认共用的只读上下文。

    返回 ``(existing_loadings, cargos, reading_infos)``：

    * ``existing_loadings``：该车次全部既有装卸时段（UTC 归一化）；
    * ``cargos``：本批货物 ∪ 既有装卸货物 的编号 -> Cargo 行（温度上限即
      “货物版本”指纹：上限变化会使预检摘要过期）；
    * ``reading_infos``：该车次全部既有车载读数的
      :class:`app.precheck.ReadingInfo`（有效温度 + 更正版本）。
    """
    loadings = get_loadings(db, trip_id)
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)

    cargo_ids = {c for c, _, _ in norm_items} | {l.cargo_id for l in loadings}
    cargos = {
        c.cargo_id: c
        for c in db.scalars(
            select(Cargo).where(Cargo.cargo_id.in_(sorted(cargo_ids) or [""]))
        ).all()
    }

    readings = get_readings(db, trip_id)
    _, reading_eff = timeline.load_effective_maps(
        db, reading_ids={r.reading_id for r in readings}
    )
    reading_infos = []
    for r in readings:
        eff = reading_eff.get(r.reading_id)
        if eff is not None:
            eff_temp, version = Decimal(eff[0]), eff[1]
        else:
            eff_temp, version = Decimal(r.temperature), 0
        reading_infos.append(
            precheck.ReadingInfo(
                reading_id=r.reading_id,
                read_at=ensure_utc(r.read_at),
                effective_temperature=eff_temp,
                version=version,
            )
        )
    return loadings, cargos, reading_infos


def _current_manifest_digest(db, trip: Trip, norm_items) -> str:
    """按当前数据重算本批拟登记的预检摘要（确认时在持锁事务内调用）。"""
    _, cargos, reading_infos = _manifest_context(db, trip.trip_id, norm_items)
    return precheck.manifest_digest(
        trip.trip_id,
        trip.revision,
        norm_items,
        {cid: Decimal(c.temp_upper_limit) for cid, c in cargos.items()},
        reading_infos,
    )


def _own_measure_times(db, cargo_ids, read_ats):
    """各涉及货物在车次读数时刻的自有测温时刻（同刻自有优先的取代标记用）。"""
    own_at: dict[str, set] = {}
    if not cargo_ids or not read_ats:
        return own_at
    rows = db.scalars(
        select(Event).where(
            Event.cargo_id.in_(sorted(cargo_ids)),
            Event.occurred_at.in_(list(read_ats)),
        )
    ).all()
    for e in rows:
        if getattr(e.event_type, "name", e.event_type) == "measurement":
            own_at.setdefault(e.cargo_id, set()).add(ensure_utc(e.occurred_at))
    return own_at


def precheck_manifest(
    db, trip_id: str, payload: ManifestPrecheckCreate
) -> PrecheckResult:
    """装卸前温控兼容性预检（只读，不写入任何装卸记录）。

    对“既有装卸 ∪ 本批拟登记”做扫描线分段，输出各时段的货物集合、
    温度上限差异、既有车载读数覆盖与潜在越温风险，并给出预检摘要。
    整次预检基于一个一致的已提交数据视图（PostgreSQL 下 REPEATABLE READ
    快照，与质控同时越温查询相同）。
    """
    # 1) 时段形状：左闭右开且起点严格早于终点（与登记同一校验，明确报错）
    norm = []
    for it in payload.items:
        loaded_at = ensure_utc(it.loaded_at)
        unloaded_at = ensure_utc(it.unloaded_at)
        if loaded_at >= unloaded_at:
            raise _unprocessable(
                f"车次 {trip_id} 货物 {it.cargo_id} 的装卸时段 "
                f"[{loaded_at.isoformat()}, {unloaded_at.isoformat()}) 无效："
                "装载时段必须左闭右开且起点严格早于终点",
                code="invalid_loading_window",
            )
        norm.append((it.cargo_id, loaded_at, unloaded_at))

    # 一致快照：PG 下 REPEATABLE READ（首条 SQL 前指定）；SQLite 单连接天然一致
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    # 2) 车次须存在
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

    # 3) 本批货物须全部存在
    missing = [
        cargo_id
        for cargo_id in sorted({c for c, _, _ in norm})
        if db.get(Cargo, cargo_id) is None
    ]
    if missing:
        raise ApiError(
            404,
            "cargo_not_found",
            f"货物 {', '.join(sorted(missing))} 不存在",
        )

    # 4) 预期车次修订号须与当前一致（乐观并发校验）
    if payload.expected_trip_revision != trip.revision:
        raise _conflict(
            f"预期车次修订号过期：车次 {trip_id} 当前修订号为 "
            f"{trip.revision}，请求预期为 {payload.expected_trip_revision}，"
            "请重新查询车次后再预检",
            code="stale_trip_revision",
        )

    # 5) 汇总上下文并计算分段分析 + 预检摘要
    loadings, cargos, reading_infos = _manifest_context(db, trip_id, norm)
    windows = [
        precheck.BoardWindow(l.cargo_id, l.loaded_at, l.unloaded_at, False)
        for l in loadings
    ] + [
        precheck.BoardWindow(cargo_id, lo, un, True)
        for cargo_id, lo, un in norm
    ]
    segments = precheck.boarding_segments(windows)
    own_at = _own_measure_times(
        db, set(cargos), {r.read_at for r in reading_infos}
    )
    analyses = precheck.analyze_segments(
        segments,
        reading_infos,
        {cid: Decimal(c.temp_upper_limit) for cid, c in cargos.items()},
        own_at,
    )
    digest = precheck.manifest_digest(
        trip_id,
        trip.revision,
        norm,
        {cid: Decimal(c.temp_upper_limit) for cid, c in cargos.items()},
        reading_infos,
    )
    return PrecheckResult(
        trip_id=trip_id,
        trip_revision=trip.revision,
        digest=digest,
        analyses=analyses,
    )


# ----------------------------------------------------------- 车载读数


@dataclass
class ReadingResult:
    reading: VehicleReading
    replayed: bool
    affected_cargo_ids: list[str]


def create_vehicle_reading(db, payload: VehicleReadingCreate) -> ReadingResult:
    try:
        return _create_reading_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _create_reading_tx(db, payload)


def _same_reading_content(existing: VehicleReading, payload, read_at) -> bool:
    return (
        existing.trip_id == payload.trip_id
        and ensure_utc(existing.read_at) == read_at
        and existing.temperature == payload.temperature
    )


def _create_reading_tx(db, payload: VehicleReadingCreate) -> ReadingResult:
    read_at = ensure_utc(payload.read_at)
    try:
        # 车次须存在
        if db.get(Trip, payload.trip_id) is None:
            raise ApiError(
                404, "trip_not_found", f"车次 {payload.trip_id} 不存在"
            )

        # 幂等先判定（按主键读，无需持锁）
        existing = db.get(VehicleReading, payload.reading_id)
        if existing is not None:
            if _same_reading_content(existing, payload, read_at):
                db.rollback()
                affected = _affected_cargo_ids(db, payload.trip_id, read_at)
                return ReadingResult(existing, True, affected)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )

        # 预估受影响货物（此刻在车上的货物）
        affected = _affected_cargo_ids(db, payload.trip_id, read_at)

        # 1) 先锁定全部受影响货物行（升序），与这些货物的事件/表态写入串行
        _lock_cargo_rows(db, affected)
        # 2) 再锁定车次行，与装卸登记、同车次读数写入串行
        trip = _load_trip_locked(db, payload.trip_id)

        # 拿到车次锁后复核幂等（并发重传）
        winner = db.get(VehicleReading, payload.reading_id)
        if winner is not None:
            if _same_reading_content(winner, payload, read_at):
                db.rollback()
                affected2 = _affected_cargo_ids(db, payload.trip_id, read_at)
                return ReadingResult(winner, True, affected2)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )

        # 同车次同刻至多一笔读数
        same_slot = db.scalars(
            select(VehicleReading).where(
                VehicleReading.trip_id == payload.trip_id,
                VehicleReading.read_at == read_at,
            )
        ).first()
        if same_slot is not None:
            raise _conflict(
                f"车次 {payload.trip_id} 在 {read_at.isoformat()} "
                f"已存在车载读数 {same_slot.reading_id}，同一时刻不允许重复",
                code="same_timestamp_conflict",
            )

        reading = VehicleReading(
            reading_id=payload.reading_id,
            trip_id=payload.trip_id,
            read_at=read_at,
            temperature=payload.temperature,
        )
        db.add(reading)
        # 新读数落库 -> 车次修订号 +1（同事务，冲突回滚则不递增）
        trip.revision = (trip.revision or 0) + 1
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner = db.get(VehicleReading, payload.reading_id)
        if winner is not None:
            if _same_reading_content(winner, payload, read_at):
                affected2 = _affected_cargo_ids(db, payload.trip_id, read_at)
                return ReadingResult(winner, True, affected2)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )
        raise _conflict(
            f"车载读数 {payload.reading_id} 与既有数据冲突"
            "（同车次同刻重复 / 车次缺失 / 字段约束）",
            code="integrity_conflict",
        )

    reading.read_at = ensure_utc(reading.read_at)
    return ReadingResult(reading, False, affected)


def _affected_cargo_ids(db, trip_id: str, read_at) -> list[str]:
    """读数时刻仍在该车次车上的货物（左闭右开）。"""
    rows = db.scalars(
        select(Loading.cargo_id).where(
            Loading.trip_id == trip_id,
            Loading.loaded_at <= read_at,
            Loading.unloaded_at > read_at,
        )
    ).all()
    return sorted(set(rows))


# ----------------------------------------------------------- 查询


def get_loadings(db, trip_id: str) -> list[Loading]:
    return list(
        db.scalars(
            select(Loading)
            .where(Loading.trip_id == trip_id)
            .order_by(Loading.loaded_at, Loading.cargo_id)
        ).all()
    )


def get_readings(db, trip_id: str) -> list[VehicleReading]:
    return list(
        db.scalars(
            select(VehicleReading)
            .where(VehicleReading.trip_id == trip_id)
            .order_by(VehicleReading.read_at, VehicleReading.reading_id)
        ).all()
    )


# ----------------------------------------------------------- 质控：同时越温查询


def query_simultaneous_excursions(db, trip_id: str, start_at, end_at, min_cargo: int):
    """同一车次上多票货物同时越温时段查询。

    只读查询，整次查询基于一个**一致的已提交数据视图**：PostgreSQL 下以
    REPEATABLE READ 快照事务执行（SQLite 单连接读本身串行，无需提升级别）。
    晚到读数 / 事件在本次查询事务开始前已提交即会被反映，提交后才能被
    下一次查询看到；查询期间其他事务的中途写入不会被混入。

    每票货物都按其**现有合并时间线**重建越温区间（同刻自有测温优先、
    车载读数仅对在车货物生效），再与该车次装卸时段（左闭右开）及查询窗
    相交；未闭合区间裁剪至查询终点。
    """
    if min_cargo < 2:
        raise ApiError(
            422,
            "invalid_min_cargo",
            f"数量门槛 min_cargo={min_cargo} 非法：必须为不小于 2 的整数",
        )
    if end_at <= start_at:
        raise ApiError(
            422,
            "invalid_query_window",
            f"查询窗非法：终点 {end_at.isoformat()} 必须严格晚于起点 "
            f"{start_at.isoformat()}",
        )

    # 一致快照：在该 Session 的第一条 SQL 前指定事务隔离级别。
    # PG REPEATABLE READ 的快照在首条查询时建立，之后整个事务看到同一已提交版本；
    # SQLite 全库单连接（内存库 StaticPool），读期间视图天然一致。
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    if db.get(Trip, trip_id) is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

    loadings = get_loadings(db, trip_id)
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)

    cargo_ids = sorted({l.cargo_id for l in loadings})
    cargos = {
        c.cargo_id: c
        for c in db.scalars(
            select(Cargo).where(Cargo.cargo_id.in_(cargo_ids or [""]))
        ).all()
    }

    cargo_windows: dict[str, list[tuple]] = {}
    for cargo_id in cargo_ids:
        cargo = cargos.get(cargo_id)
        if cargo is None:
            # 外键约束下不应发生；防御性跳过，避免单票脏数据导致整查询失败
            continue
        # 与货物报告同一套重建：必须含该货物**全部车次**的上下文——货物可能
        # 从上一车次带着未闭合越温区间装到本车次，此时自本车次装车时刻起即越温。
        events, all_loadings, ctx_readings, effective, active_faults = (
            timeline.load_cargo_context(db, cargo_id)
        )
        event_eff, reading_eff = effective
        points = timeline.adopted_timeline(
            events,
            ctx_readings,
            all_loadings,
            event_eff,
            reading_eff,
            active_faults,
        )
        result = chain.rebuild_chain(
            cargo.initial_holder, Decimal(cargo.temp_upper_limit), points
        )
        own_loadings = [l for l in all_loadings if l.trip_id == trip_id]
        windows = overlap.cargo_excursion_windows(
            result.excursions, own_loadings, start_at, end_at
        )
        if windows:
            cargo_windows[cargo_id] = windows

    return overlap.simultaneous_segments(cargo_windows, min_cargo=min_cargo)
