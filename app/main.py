"""冷链货物追责服务 —— FastAPI 入口。

启动（开发）: uvicorn app.main:app --reload
一键启动    : docker compose up --build  （见 README）
"""
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    chain,
    correction_crud,
    crud,
    evidence_crud,
    loading_correction_crud,
    responsibility,
    sealing,
    stance_crud,
    timeline,
    vehicle_crud,
)
from app.database import Base, engine, get_db
from app.models import (
    Cargo,
    Event,
    Loading,
    Stance,
    StanceKind,
    Trip,
    VehicleReading,
)
from app.schemas import (
    CargoCreate,
    CargoOut,
    CargoTimeline,
    ChainReport,
    CorrectionAck,
    CorrectionCreate,
    CorrectionHistory,
    CorrectionHistoryItem,
    CorrectionOut,
    CorrectionReplay,
    EvidenceSealContent,
    EvidenceSealCreate,
    EvidenceSealOut,
    EvidenceSealReplay,
    SealCargo,
    SealDiffItem,
    SealDiffReport,
    SealInterval,
    SealLoadingWindow,
    SealOwnEvent,
    SealSegmentChange,
    SealStance,
    SealVehicleReading,
    EventCreate,
    EventOut,
    Excursion,
    IdempotentReplay,
    LatestStance,
    LoadingOut,
    LoadingCorrectionAck,
    LoadingCorrectionHistory,
    LoadingCorrectionOut,
    LoadingCorrectionReplay,
    LoadingWindowCorrectionCreate,
    ManifestConfirmCreate,
    ManifestCreate,
    ManifestOut,
    ManifestPrecheckCreate,
    ManifestPrecheckOut,
    PrecheckReadingCoverage,
    PrecheckRisk,
    PrecheckSegment,
    ReadingApplication,
    ResponsibilityInterval,
    ResponsibilityReport,
    ResponsibilitySegment,
    StanceCreate,
    StanceHistory,
    StanceHistoryItem,
    StanceOut,
    StanceReplay,
    StanceSegment,
    TimelinePointOut,
    TripCreate,
    TripDetail,
    TripOut,
    TripReadingOut,
    VehicleReadingAck,
    VehicleReadingCreate,
    VehicleReadingOut,
    VehicleReadingReplay,
    SimultaneousExcursionReport,
    SimultaneousExcursionSegment,
)
from app.timeutil import ensure_utc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产中由 db/init.sql 建表；此处确保直接 uvicorn / sqlite 测试也可用
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            # 既有数据卷升级（init.sql 只对全新数据卷生效）
            # 1) 为车次表补齐修订号列
            conn.exec_driver_sql(
                "ALTER TABLE trip "
                "ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0"
            )
            # 2) loading 表加入装卸时段更正所需的稳定身份列 / 原始窗口 / 版本。
            #    旧数据卷主键原为 (trip_id, cargo_id, loaded_at)：新列补齐并回填
            #    后，把主键切换到稳定身份 (trip_id, cargo_id, origin_loaded_at)。
            conn.exec_driver_sql(
                "ALTER TABLE loading "
                "ADD COLUMN IF NOT EXISTS origin_loaded_at TIMESTAMPTZ"
            )
            conn.exec_driver_sql(
                "ALTER TABLE loading "
                "ADD COLUMN IF NOT EXISTS origin_unloaded_at TIMESTAMPTZ"
            )
            conn.exec_driver_sql(
                "ALTER TABLE loading "
                "ADD COLUMN IF NOT EXISTS window_version INTEGER NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "UPDATE loading SET origin_loaded_at = loaded_at "
                "WHERE origin_loaded_at IS NULL"
            )
            conn.exec_driver_sql(
                "UPDATE loading SET origin_unloaded_at = unloaded_at "
                "WHERE origin_unloaded_at IS NULL"
            )
            conn.exec_driver_sql(
                """
                DO $do$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'chk_loading_origin_window'
                          AND conrelid = 'loading'::regclass
                    ) THEN
                        ALTER TABLE loading
                            ADD CONSTRAINT chk_loading_origin_window
                            CHECK (origin_loaded_at < origin_unloaded_at);
                    END IF;
                END $do$
                """
            )
            conn.exec_driver_sql(
                "ALTER TABLE loading DROP CONSTRAINT IF EXISTS loading_pkey"
            )
            conn.exec_driver_sql(
                "ALTER TABLE loading "
                "ADD PRIMARY KEY (trip_id, cargo_id, origin_loaded_at)"
            )
            # 3) 装卸时段更正表（新表，IF NOT EXISTS 兜底）
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS loading_correction (
                    correction_id          VARCHAR(64)   PRIMARY KEY,
                    trip_id                VARCHAR(64)   NOT NULL,
                    cargo_id               VARCHAR(64)   NOT NULL,
                    origin_loaded_at       TIMESTAMPTZ   NOT NULL,
                    origin_unloaded_at     TIMESTAMPTZ   NOT NULL,
                    previous_loaded_at     TIMESTAMPTZ   NOT NULL,
                    previous_unloaded_at   TIMESTAMPTZ   NOT NULL,
                    new_loaded_at          TIMESTAMPTZ   NOT NULL,
                    new_unloaded_at        TIMESTAMPTZ   NOT NULL,
                    expected_version       INTEGER       NOT NULL,
                    version                INTEGER       NOT NULL,
                    reason                 VARCHAR(512)  NOT NULL,
                    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
                    CONSTRAINT chk_loading_correction_shape CHECK (
                        expected_version >= 0
                        AND version >= 1
                        AND new_loaded_at < new_unloaded_at
                        AND origin_loaded_at < origin_unloaded_at
                        AND char_length(reason) > 0
                    )
                )
                """
            )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_loading_correction_window_version "
                "ON loading_correction (trip_id, cargo_id, origin_loaded_at, version)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_loading_correction_window "
                "ON loading_correction (trip_id, cargo_id, origin_loaded_at, created_at)"
            )
            # 4) 责任证据封存表（新表，IF NOT EXISTS 兜底；内容 append-only）
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS evidence_seal (
                    case_id        VARCHAR(128) PRIMARY KEY,
                    cargo_id       VARCHAR(64)  NOT NULL
                                   REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
                    interval_key   VARCHAR(68)  NOT NULL,
                    snapshot_id    VARCHAR(71)  NOT NULL,
                    request_hash   VARCHAR(71)  NOT NULL,
                    content        TEXT         NOT NULL,
                    content_digest VARCHAR(71)  NOT NULL,
                    sealed_at      TIMESTAMPTZ  NOT NULL,
                    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
                )
                """
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_evidence_seal_cargo_interval "
                "ON evidence_seal (cargo_id, interval_key)"
            )
    yield


app = FastAPI(
    title="冷链货物追责服务",
    description=(
        "记录冷链货物的自有测温与交接事件、车次装卸时段与车载测温读数，"
        "在合并读数时间线上按发生时间重建保管链，计算越温区间并拆分各保管方责任。"
        "支持对录错的自有测温 / 车载读数提交只改温度的更正（乐观版本、append-only），"
        "追责报告、责任确认、货物时间线与车次同时越温查询一律按更正后的有效值重建。"
        "登记装卸时段前可先对一批待装货物做温控兼容性预检（只读，输出各重叠时段"
        "分析与预检摘要），再凭预检摘要确认登记（车次修订号乐观并发，摘要过期"
        "拒绝，通过后沿用批量原子校验）。"
        "装卸时段录错后可凭原车次/货物/首次登记装车时刻、期望版本、修正后的"
        "带时区时刻与原因更正单条装卸窗口（只改该条窗口，append-only 可追溯，"
        "车载适用性/越温/责任/质控按当前窗口重建，成功推进车次修订号）。"
        "调查人员可对已闭合越温区间封存责任证据（凭案件编号、货物编号、区间"
        "标识与当前快照标识）：在一致视图上保存区间责任片段、当时有效表态与"
        "重建所用自有事件、车载读数有效温度/版本、当前装卸窗口及版本，生成"
        "内容摘要，封存后不可改；区间消失/重开/快照已变拒绝且不留记录，"
        "同案件编号同请求重试即使现况已变仍返回原包，并支持封存包查询与"
        "现况对照（证据项/责任片段增删变更，晚到记录与更正不改写原包）。"
    ),
    version="1.6.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.ApiError)
async def api_error_handler(request: Request, exc: crud.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------- 货物


@app.post("/cargo", response_model=CargoOut, status_code=201, tags=["货物"])
def create_cargo(payload: CargoCreate, db: Session = Depends(get_db)) -> Cargo:
    payload.started_at = ensure_utc(payload.started_at)
    return crud.create_cargo(db, payload)


@app.get("/cargo", response_model=list[CargoOut], tags=["货物"])
def list_cargo(db: Session = Depends(get_db)) -> list[Cargo]:
    return list(db.scalars(select(Cargo).order_by(Cargo.cargo_id)).all())


@app.get("/cargo/{cargo_id}", response_model=CargoOut, tags=["货物"])
def get_cargo(cargo_id: str, db: Session = Depends(get_db)) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    return cargo


# ---------------------------------------------------------------- 事件


def _event_out(
    e: Event,
    effective: tuple | None = None,
) -> EventOut:
    """自有事件响应：测温给出原始温度 + 当前有效温度与更正版本（交接二者为 null/0）。"""
    is_measurement = e.event_type.name == "measurement"
    if is_measurement:
        original = Decimal(e.temperature)
        if effective is not None:
            eff_temp, version = Decimal(effective[0]), effective[1]
        else:
            eff_temp, version = original, 0
    else:
        original = eff_temp = None
        version = 0
    return EventOut(
        event_id=e.event_id,
        cargo_id=e.cargo_id,
        event_type=e.event_type,
        occurred_at=ensure_utc(e.occurred_at),
        temperature=original,
        effective_temperature=eff_temp,
        version=version,
        giver=e.giver,
        receiver=e.receiver,
    )


@app.post(
    "/events",
    response_model=EventOut,
    status_code=201,
    tags=["货物自有事件"],
    responses={
        200: {
            "model": IdempotentReplay,
            "description": "同 ID 同内容重传：不重复写入",
        }
    },
)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    payload.occurred_at = ensure_utc(payload.occurred_at)
    result = crud.create_event(db, payload)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=IdempotentReplay(
                event_id=payload.event_id,
                message="相同内容的事件已存在，重传被忽略（幂等）",
            ).model_dump(),
        )
    # 新写入事件尚无更正：有效温度即原始温度、版本 0
    return _event_out(result.event)


@app.get(
    "/cargo/{cargo_id}/events",
    response_model=list[EventOut],
    tags=["货物自有事件"],
)
def list_events(cargo_id: str, db: Session = Depends(get_db)) -> list[EventOut]:
    """列出货物**自有**事件（测温 / 交接）；车载读数不在此列表。

    测温事件的 ``temperature`` 恒为**原始录入值**（不因更正改变），另给
    ``effective_temperature`` / ``version`` 表示当前有效温度与更正版本。
    """
    if db.get(Cargo, cargo_id) is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    event_eff, _ = timeline.load_effective_maps(
        db,
        event_ids={
            e.event_id for e in events if e.event_type.name == "measurement"
        },
    )
    return [
        _event_out(e, event_eff.get(e.event_id))
        for e in events
    ]


# ---------------------------------------------------------------- 车次 / 装卸 / 车载读数


@app.post("/trips", response_model=TripOut, status_code=201, tags=["车次与车载测温"])
def create_trip(payload: TripCreate, db: Session = Depends(get_db)) -> Trip:
    return vehicle_crud.create_trip(db, payload)


@app.get("/trips", response_model=list[TripOut], tags=["车次与车载测温"])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    return vehicle_crud.list_trips(db)


def _loading_out(l: Loading) -> LoadingOut:
    """一条装卸窗口的响应：当前窗口 + 稳定身份（首次登记装车时刻）+ 更正版本。"""
    return LoadingOut(
        trip_id=l.trip_id,
        cargo_id=l.cargo_id,
        origin_loaded_at=ensure_utc(l.origin_loaded_at),
        loaded_at=ensure_utc(l.loaded_at),
        unloaded_at=ensure_utc(l.unloaded_at),
        window_version=l.window_version or 0,
    )


def _reading_out(
    r: VehicleReading,
    effective: tuple | None = None,
) -> VehicleReadingOut:
    original = Decimal(r.temperature)
    if effective is not None:
        eff_temp, version = Decimal(effective[0]), effective[1]
    else:
        eff_temp, version = original, 0
    return VehicleReadingOut(
        reading_id=r.reading_id,
        trip_id=r.trip_id,
        read_at=ensure_utc(r.read_at),
        temperature=original,
        effective_temperature=eff_temp,
        version=version,
        created_at=ensure_utc(r.created_at),
    )


@app.post(
    "/trips/{trip_id}/manifest",
    response_model=ManifestOut,
    status_code=201,
    tags=["车次与车载测温"],
)
def register_manifest(
    trip_id: str, payload: ManifestCreate, db: Session = Depends(get_db)
) -> ManifestOut:
    """批量登记车次上各票货物的装卸时段（原子：任一非法整批拒绝，不留部分记录）。"""
    payload.trip_id = trip_id
    for it in payload.items:
        it.loaded_at = ensure_utc(it.loaded_at)
        it.unloaded_at = ensure_utc(it.unloaded_at)
    result = vehicle_crud.register_manifest(db, payload)
    return ManifestOut(
        trip_id=result.trip_id,
        items=[_loading_out(l) for l in result.items],
    )


def _precheck_out(result) -> ManifestPrecheckOut:
    return ManifestPrecheckOut(
        trip_id=result.trip_id,
        trip_revision=result.trip_revision,
        digest=result.digest,
        segments=[
            PrecheckSegment(
                start=a.segment.start,
                end=a.segment.end,
                cargo_ids=list(a.segment.cargo_ids),
                proposed_cargo_ids=list(a.segment.proposed_ids),
                cargo_count=len(a.segment.cargo_ids),
                temp_limit_min=a.temp_limit_min,
                temp_limit_max=a.temp_limit_max,
                temp_limit_spread=a.temp_limit_spread,
                readings=[
                    PrecheckReadingCoverage(
                        reading_id=r.reading_id,
                        read_at=r.read_at,
                        effective_temperature=r.effective_temperature,
                        version=r.version,
                    )
                    for r in a.readings
                ],
                reading_count=len(a.readings),
                risks=[
                    PrecheckRisk(
                        cargo_id=risk.cargo_id,
                        reading_id=risk.reading_id,
                        read_at=risk.read_at,
                        effective_temperature=risk.effective_temperature,
                        temp_upper_limit=risk.temp_upper_limit,
                        supplanted_by_own=risk.supplanted_by_own,
                    )
                    for risk in a.risks
                ],
            )
            for a in result.analyses
        ],
    )


@app.post(
    "/trips/{trip_id}/manifest/precheck",
    response_model=ManifestPrecheckOut,
    tags=["车次与车载测温"],
    responses={
        404: {"description": "车次 / 货物不存在"},
        409: {"description": "预期车次修订号过期"},
        422: {"description": "装卸时段非法（起点不早于终点）"},
    },
)
def precheck_manifest(
    trip_id: str, payload: ManifestPrecheckCreate, db: Session = Depends(get_db)
) -> ManifestPrecheckOut:
    """装卸前温控兼容性预检（只读，**不写入装卸记录**）。

    对“车次既有装卸 ∪ 本批拟登记时段”做扫描线分段，逐段输出车上货物集合、
    温度上限差异（min/max/极差）、落在时段内的既有车载读数（有效温度与
    更正版本）与潜在越温风险（读数有效温度高于货物自身上限的组合；同刻
    有自有测温的货物标记 ``supplanted_by_own``）。响应携带**预检摘要**
    （绑定本批时段、车次修订号、涉及货物温度上限与车次读数版本），确认
    登记时须原样携带。``expected_trip_revision`` 与车次当前修订号不一致
    即拒绝（请先查询车次详情获取最新修订号）。
    """
    for it in payload.items:
        it.loaded_at = ensure_utc(it.loaded_at)
        it.unloaded_at = ensure_utc(it.unloaded_at)
    result = vehicle_crud.precheck_manifest(db, trip_id, payload)
    return _precheck_out(result)


@app.post(
    "/trips/{trip_id}/manifest/confirm",
    response_model=ManifestOut,
    status_code=201,
    tags=["车次与车载测温"],
    responses={
        404: {"description": "车次 / 货物不存在"},
        409: {"description": "预检摘要过期 / 装卸时段重叠冲突"},
        422: {"description": "装卸时段非法（起点不早于终点）"},
    },
)
def confirm_manifest(
    trip_id: str, payload: ManifestConfirmCreate, db: Session = Depends(get_db)
) -> ManifestOut:
    """携预检摘要确认登记装卸时段。

    服务端在持锁事务内按当前数据重算摘要：车次修订号、涉及货物温度上限
    或车载读数版本在预检后发生变化（摘要过期）即拒绝且不落任何记录；
    摘要通过后沿用与 ``POST /trips/{trip_id}/manifest`` 相同的批量原子
    校验——任一时段冲突整批拒绝、不留部分记录。
    """
    payload.trip_id = trip_id
    for it in payload.items:
        it.loaded_at = ensure_utc(it.loaded_at)
        it.unloaded_at = ensure_utc(it.unloaded_at)
    result = vehicle_crud.confirm_manifest(db, payload)
    return ManifestOut(
        trip_id=result.trip_id,
        items=[_loading_out(l) for l in result.items],
    )


@app.post(
    "/vehicle-readings",
    response_model=VehicleReadingAck,
    status_code=201,
    tags=["车次与车载测温"],
    responses={
        200: {
            "model": VehicleReadingReplay,
            "description": "同编号同内容重传：返回原读数（幂等）",
        }
    },
)
def create_vehicle_reading(
    payload: VehicleReadingCreate, db: Session = Depends(get_db)
):
    """写入车载原始读数（允许晚到）。

    与读数时刻在车上的全部货物的事件写入 / 责任表态共用货物行锁串行一致。
    回执给出受影响货物（读数时刻仍在车上，按各自温度上限判断越温）。
    """
    payload.read_at = ensure_utc(payload.read_at)
    result = vehicle_crud.create_vehicle_reading(db, payload)
    _, reading_eff = timeline.load_effective_maps(
        db, reading_ids={result.reading.reading_id}
    )
    reading_out = _reading_out(
        result.reading, reading_eff.get(result.reading.reading_id)
    )
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=VehicleReadingReplay(
                replayed=True,
                reading=reading_out,
                affected_cargo_ids=result.affected_cargo_ids,
                message="相同内容的车载读数已存在，重传被忽略（幂等）",
            ).model_dump(mode="json"),
        )
    return VehicleReadingAck(
        reading=reading_out, affected_cargo_ids=result.affected_cargo_ids
    )


def _excursion_schema(ex) -> Excursion:
    return Excursion(
        start=ex.start,
        end=ex.end,
        is_open=ex.is_open,
        interval_id=chain.interval_id(ex),
        start_source=ex.start_source,
        end_source=(None if ex.is_open else ex.end_source),
        start_reading_id=ex.start_reading_id,
        end_reading_id=(None if ex.is_open else ex.end_reading_id),
        start_trip_id=ex.start_trip_id,
        end_trip_id=(None if ex.is_open else ex.end_trip_id),
        segments=[
            ResponsibilitySegment(
                start=seg.start, end=seg.end, responsible_party=seg.party
            )
            for seg in ex.segments
        ],
    )


def _trip_reading_application(
    db, reading: VehicleReading, loadings, cargos, reading_eff
):
    """一笔车载读数对车次上各票货物的适用性/采用情况。"""
    apps = []
    read_at = ensure_utc(reading.read_at)
    cargo_loadings: dict[str, list] = {}
    for l in loadings:
        cargo_loadings.setdefault(l.cargo_id, []).append(l)

    own_at: dict[str, bool] = {}
    if cargo_loadings:
        rows = db.scalars(
            select(Event).where(
                Event.cargo_id.in_(sorted(cargo_loadings)),
                Event.occurred_at == read_at,
            )
        ).all()
        own_at = {
            e.cargo_id: True
            for e in rows
            if e.event_type.name == "measurement"
        }

    # 越温判定按更正后的有效温度（原始读数仍可查）
    eff = reading_eff.get(reading.reading_id)
    eff_temp = Decimal(eff[0]) if eff is not None else Decimal(reading.temperature)

    for cargo_id in sorted(cargo_loadings):
        on_board = any(timeline.is_on_board(l, read_at) for l in cargo_loadings[cargo_id])
        adopted = bool(on_board and not own_at.get(cargo_id))
        over = None
        reason = "adopted"
        if not on_board:
            reason = "not_on_board"
        elif own_at.get(cargo_id):
            reason = "supplanted_by_own"
        if on_board:
            over = eff_temp > Decimal(cargos[cargo_id].temp_upper_limit)
        apps.append(
            ReadingApplication(
                cargo_id=cargo_id,
                on_board=on_board,
                adopted=adopted,
                over_limit=over,
                reason=reason,
            )
        )
    return apps


@app.get(
    "/trips/{trip_id}",
    response_model=TripDetail,
    tags=["车次与车载测温"],
)
def get_trip_detail(trip_id: str, db: Session = Depends(get_db)) -> TripDetail:
    """车次详情：装卸时段、车载原始读数，以及每笔读数对各货物的适用性/采用情况。"""
    trip = vehicle_crud.get_trip(db, trip_id)
    loadings = vehicle_crud.get_loadings(db, trip_id)
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)
    readings = vehicle_crud.get_readings(db, trip_id)

    cargos = {
        c.cargo_id: c
        for c in db.scalars(
            select(Cargo).where(
                Cargo.cargo_id.in_(
                    sorted({l.cargo_id for l in loadings}) or [""]
                )
            )
        ).all()
    }

    _, reading_eff = timeline.load_effective_maps(
        db, reading_ids={r.reading_id for r in readings}
    )

    trip_readings = []
    for r in readings:
        apps = _trip_reading_application(
            db, r, loadings, cargos, reading_eff
        )
        trip_readings.append(
            TripReadingOut(
                reading_id=r.reading_id,
                trip_id=r.trip_id,
                read_at=ensure_utc(r.read_at),
                temperature=Decimal(r.temperature),
                effective_temperature=(
                    Decimal(reading_eff[r.reading_id][0])
                    if r.reading_id in reading_eff
                    else Decimal(r.temperature)
                ),
                version=(
                    reading_eff[r.reading_id][1]
                    if r.reading_id in reading_eff
                    else 0
                ),
                created_at=ensure_utc(r.created_at),
                applications=apps,
            )
        )

    return TripDetail(
        trip_id=trip.trip_id,
        vehicle_no=trip.vehicle_no,
        note=trip.note,
        revision=trip.revision,
        created_at=ensure_utc(trip.created_at),
        loadings=[_loading_out(l) for l in loadings],
        readings=trip_readings,
    )


# ---------------------------------------------------------------- 质控：同时越温查询


def _parse_aware_datetime(raw: str, field: str) -> datetime:
    """解析查询参数中的 ISO 8601 时刻；必须带时区（调用方明确给出带时区起止）。"""
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise crud.ApiError(
            422,
            "invalid_query_window",
            f"参数 {field}={raw!r} 不是合法的 ISO 8601 日期时间",
        )
    if parsed.tzinfo is None:
        raise crud.ApiError(
            422,
            "timezone_required",
            f"参数 {field}={raw!r} 必须携带时区偏移（如 2026-09-30T08:00:00+08:00）",
        )
    return ensure_utc(parsed)


def _parse_min_cargo(raw: str | None) -> int:
    """数量门槛：不小于 2 的正整数（缺省 2）。"""
    if raw is None:
        return 2
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise crud.ApiError(
            422,
            "invalid_min_cargo",
            f"参数 min_cargo={raw!r} 非法：必须为不小于 2 的整数",
        )
    if value < 2:
        raise crud.ApiError(
            422,
            "invalid_min_cargo",
            f"参数 min_cargo={value} 非法：必须为不小于 2 的整数",
        )
    return value


@app.get(
    "/trips/{trip_id}/simultaneous-excursions",
    response_model=SimultaneousExcursionReport,
    tags=["质控查询"],
    responses={
        404: {"description": "车次不存在"},
        422: {"description": "查询窗非法（终点不晚于起点 / 缺时区）或门槛非法"},
    },
)
def trip_simultaneous_excursions(
    trip_id: str,
    start_at: str | None = None,
    end_at: str | None = None,
    min_cargo: str | None = None,
    db: Session = Depends(get_db),
) -> SimultaneousExcursionReport:
    """质控：查找同一车次上**多票货物同时越温**的时段。

    每票货物按其现有合并读数时间线重建越温区间（同刻自有测温优先、车载读数
    仅对在车货物按各自上限生效），与该车次装卸时段（左闭右开）及查询窗
    相交（未闭合区间裁剪至查询终点）后扫描线叠加：货物集合变化即分段、
    相邻同集合片段合并，仅保留越温货物数达到 ``min_cargo``（默认 2）的
    正长度片段，按时间升序返回。晚到读数 / 事件提交后的查询反映新结果，
    同一次查询基于一致的已提交数据视图（PostgreSQL 下 REPEATABLE READ 快照）。
    """
    if start_at is None:
        raise crud.ApiError(
            422,
            "missing_parameter",
            "缺少必填参数 start_at：带时区的查询起点（ISO 8601）",
        )
    if end_at is None:
        raise crud.ApiError(
            422,
            "missing_parameter",
            "缺少必填参数 end_at：带时区的查询终点（ISO 8601，严格晚于起点）",
        )
    start = _parse_aware_datetime(start_at, "start_at")
    end = _parse_aware_datetime(end_at, "end_at")
    threshold = _parse_min_cargo(min_cargo)

    segments = vehicle_crud.query_simultaneous_excursions(
        db, trip_id, start, end, threshold
    )
    return SimultaneousExcursionReport(
        trip_id=trip_id,
        start_at=start,
        end_at=end,
        min_cargo=threshold,
        segments=[
            SimultaneousExcursionSegment(
                start=seg.start,
                end=seg.end,
                cargo_count=seg.cargo_count,
                cargo_ids=list(seg.cargo_ids),
            )
            for seg in segments
        ],
    )


# ---------------------------------------------------------------- 追责查询


def _load_cargo(db, cargo_id: str) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    return cargo


@app.get(
    "/cargo/{cargo_id}/report",
    response_model=ChainReport,
    tags=["追责查询"],
)
def cargo_report(cargo_id: str, db: Session = Depends(get_db)) -> ChainReport:
    """按合并读数时间线返回越温区间（含读数来源）与各责任方片段；未闭合区间标记未结束。"""
    cargo = _load_cargo(db, cargo_id)
    events, loadings, readings, effective = timeline.load_cargo_context(
        db, cargo_id
    )
    points = timeline.adopted_timeline(
        events, readings, loadings, effective[0], effective[1]
    )

    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), points
    )

    excursions = [_excursion_schema(ex) for ex in result.excursions]
    return ChainReport(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        current_holder=result.current_holder,
        excursions=excursions,
    )


@app.get(
    "/cargo/{cargo_id}/timeline",
    response_model=CargoTimeline,
    tags=["追责查询"],
)
def cargo_timeline(cargo_id: str, db: Session = Depends(get_db)) -> CargoTimeline:
    """货物的合并读数时间线：自有测温/交接 + 适用的车载原始读数。

    含被自有测温取代（``adopted=false``）的车载读数，可查每票货物各时刻
    实际采用的读数来源；交接（``changes_holder=true``）只来自货物自有事件。
    """
    _load_cargo(db, cargo_id)
    events, loadings, readings, effective = timeline.load_cargo_context(
        db, cargo_id
    )
    points = timeline.merged_timeline(
        events, readings, loadings, effective[0], effective[1]
    )
    return CargoTimeline(
        cargo_id=cargo_id,
        points=[
            TimelinePointOut(
                occurred_at=p.occurred_at,
                point_type=("measurement" if p.is_measurement else "handover"),
                source=p.source,
                event_id=p.source_id if p.source == "own" else None,
                reading_id=p.source_id if p.source == "vehicle" else None,
                trip_id=p.trip_id,
                temperature=(Decimal(p.temperature) if p.is_measurement else None),
                version=(p.temp_version if p.is_measurement else 0),
                giver=p.giver,
                receiver=p.receiver,
                adopted=p.adopted,
                changes_holder=(not p.is_measurement),
            )
            for p in points
        ],
    )


# ---------------------------------------------------------------- 责任确认


def _interval_schema(view) -> ResponsibilityInterval:
    """责任确认视图 -> 响应模型。"""
    return ResponsibilityInterval(
        cargo_id=view.cargo_id,
        interval_id=view.interval_id,
        start_event_id=view.start_event_id,
        end_event_id=view.end_event_id,
        start_source=view.start_source,
        end_source=view.end_source,
        start_reading_id=view.start_reading_id,
        end_reading_id=view.end_reading_id,
        start_trip_id=view.start_trip_id,
        end_trip_id=view.end_trip_id,
        start=view.start,
        end=view.end,
        snapshot_id=view.snapshot_id,
        segments=[
            StanceSegment(
                start=seg.start, end=seg.end, responsible_party=seg.responsible_party
            )
            for seg in view.segments
        ],
        latest_stances=[
            LatestStance(
                stance_id=sv.stance_id,
                responsible_party=sv.responsible_party,
                stance=sv.kind,
                reason=sv.reason,
                created_at=sv.created_at,
                segments=[
                    StanceSegment(
                        start=s.start,
                        end=s.end,
                        responsible_party=s.responsible_party,
                    )
                    for s in sv.segments
                ],
            )
            for sv in view.stances
        ],
        summary=view.summary,
    )


def _closed_interval_views(db, cargo: Cargo):
    """在合并时间线上重建并为每个已闭合区间装配责任确认视图（按开始时间升序）。"""
    events, loadings, readings, effective = timeline.load_cargo_context(
        db, cargo.cargo_id
    )
    result, closed = responsibility.rebuild(
        cargo, events, loadings, readings, effective
    )
    views = []
    for ex in sorted(closed.values(), key=lambda e: e.start):
        rows = stance_crud.load_interval_stances(
            db, cargo.cargo_id, chain.interval_id(ex)
        )
        views.append(
            responsibility.build_interval_view(cargo.cargo_id, ex, rows)
        )
    return result, views


@app.get(
    "/cargo/{cargo_id}/responsibilities",
    response_model=ResponsibilityReport,
    tags=["责任确认"],
)
def list_responsibilities(cargo_id: str, db: Session = Depends(get_db)) -> ResponsibilityReport:
    """列出货物下所有**已闭合**越温区间的责任确认状态（开放区间不参与）。"""
    cargo = _load_cargo(db, cargo_id)
    _, views = _closed_interval_views(db, cargo)
    return ResponsibilityReport(
        cargo_id=cargo_id, intervals=[_interval_schema(v) for v in views]
    )


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}",
    response_model=ResponsibilityInterval,
    tags=["责任确认"],
    responses={404: {"description": "货物 / 区间不存在"}, 422: {"description": "区间开放"}},
)
def get_responsibility(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> ResponsibilityInterval:
    """按区间稳定标识（自有=开启测温事件ID；车载=veh:<读数ID>）查询单个已闭合区间。"""
    cargo = _load_cargo(db, cargo_id)

    _, ex = stance_crud._resolve_interval(db, cargo, interval_id)
    rows = stance_crud.load_interval_stances(db, cargo_id, interval_id)
    return _interval_schema(
        responsibility.build_interval_view(cargo_id, ex, rows)
    )


def _stance_out(stance: Stance) -> StanceOut:
    return StanceOut(
        stance_id=stance.stance_id,
        cargo_id=stance.cargo_id,
        interval_id=stance.interval_key,
        opener_source=stance.opener_source.value
        if hasattr(stance.opener_source, "value")
        else str(stance.opener_source),
        start_event_id=stance.start_event_id,
        vehicle_reading_id=stance.vehicle_reading_id,
        responsible_party=stance.responsible_party,
        stance=StanceKind(stance.kind),
        reason=stance.reason,
        snapshot_id=stance.snapshot_id,
        created_at=ensure_utc(stance.created_at),
    )


@app.post(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceOut,
    status_code=201,
    tags=["责任确认"],
    responses={
        200: {"model": StanceReplay, "description": "同编号同内容重传：返回原记录"},
    },
)
def submit_stance(
    interval_id: str,
    payload: StanceCreate,
    cargo_id: str,
    db: Session = Depends(get_db),
):
    """提交责任方表态（确认 / 异议）。

    与同货物的自有事件写入、车载读数写入共用货物行锁串行化；以当前快照校验，
    过期快照、区间不存在或开放区间一律拒绝且不落库。
    """
    result = stance_crud.submit_stance(db, cargo_id, interval_id, payload)
    body = _stance_out(result.stance)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=StanceReplay(
                replayed=True,
                stance=body,
                message="相同内容的表态已存在，重传被忽略（幂等；即使该表态快照已失效也返回原记录）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceHistory,
    tags=["责任确认"],
)
def list_stance_history(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> StanceHistory:
    """区间表态历史：历次表态全部保留（含已改口、已失效），按提交时间升序。

    区间当前不存在 / 仍开放时，历史仍可查，头部字段给出当前状态。
    """
    cargo = _load_cargo(db, cargo_id)
    rows = stance_crud.load_interval_stances(db, cargo_id, interval_id)

    # 当前区间状态（不因此报 404/422：历史要始终可查）
    events, loadings, readings, effective = timeline.load_cargo_context(
        db, cargo_id
    )
    chain_result, closed = responsibility.rebuild(
        cargo, events, loadings, readings, effective
    )
    ex = closed.get(interval_id)
    open_ex = next(
        (e for e in chain_result.excursions if chain.interval_id(e) == interval_id),
        None,
    )
    interval_exists = ex is not None or open_ex is not None
    is_open = ex is None and open_ex is not None
    current_snapshot = responsibility.snapshot_id(ex) if ex is not None else None

    # 各责任方“最新有效表态”（行已按 (created_at, stance_id) 升序），
    # 用于把更早的有效表态标记为已改口
    latest_current: dict[str, Stance] = {}
    if ex is not None:
        for s in rows:
            if responsibility.is_stance_current(s, ex):
                latest_current[s.responsible_party] = s

    items = [
        StanceHistoryItem(
            stance_id=s.stance_id,
            cargo_id=s.cargo_id,
            interval_id=s.interval_key,
            opener_source=s.opener_source.value
            if hasattr(s.opener_source, "value")
            else str(s.opener_source),
            start_event_id=s.start_event_id,
            vehicle_reading_id=s.vehicle_reading_id,
            responsible_party=s.responsible_party,
            stance=StanceKind(s.kind),
            reason=s.reason,
            snapshot_id=s.snapshot_id,
            created_at=ensure_utc(s.created_at),
            is_current=(ex is not None and responsibility.is_stance_current(s, ex)),
            # 改口只在“当前仍有效”的行之间比较：已因快照变化失效的行不算被改口
            superseded=(
                ex is not None
                and responsibility.is_stance_current(s, ex)
                and (
                    (latest := latest_current.get(s.responsible_party)) is not None
                    and latest.stance_id != s.stance_id
                )
            ),
        )
        for s in rows
    ]
    return StanceHistory(
        cargo_id=cargo_id,
        interval_id=interval_id,
        interval_exists=interval_exists,
        is_open=is_open,
        current_snapshot_id=current_snapshot,
        history=items,
    )


# ---------------------------------------------------------------- 测温值更正


def _correction_out(row) -> CorrectionOut:
    target_type = (
        row.target_type.value
        if hasattr(row.target_type, "value")
        else str(row.target_type)
    )
    return CorrectionOut(
        correction_id=row.correction_id,
        target_type=target_type,
        target_id=(
            row.target_event_id
            if target_type == "own_event"
            else row.target_reading_id
        ),
        cargo_id=row.cargo_id,
        trip_id=row.trip_id,
        expected_version=row.expected_version,
        version=row.version,
        new_temperature=Decimal(row.new_temperature),
        created_at=ensure_utc(row.created_at),
    )


def _correction_ack(result) -> CorrectionAck:
    return CorrectionAck(
        correction=_correction_out(result.correction),
        effective_temperature=result.effective_temperature,
        version=result.version,
        affected_cargo_ids=list(result.cargo_ids),
    )


@app.post(
    "/corrections",
    response_model=CorrectionAck,
    status_code=201,
    tags=["测温值更正"],
    responses={
        200: {
            "model": CorrectionReplay,
            "description": "同更正ID同内容重传：返回原结果（幂等）",
        },
        404: {"description": "目标测温事件 / 车载读数不存在"},
        409: {"description": "版本过期 / 更正ID异内容冲突"},
        422: {"description": "目标为交接（不可更正）或请求形状非法"},
    },
)
def submit_correction(
    payload: CorrectionCreate, db: Session = Depends(get_db)
):
    """提交测温值更正：只改温度，时间、归属与原编号不变。

    目标由 ``target_type``（``own_event`` 自有测温事件 /
    ``vehicle_reading`` 车载读数）+ ``target_id`` 指定；交接事件不可更正。
    ``expected_version`` 为目标当前更正版本（首次为 0），版本过期、目标
    不存在或目标为交接一律拒绝且不落部分结果。车载读数更正一次影响读数
    时刻仍在车上的全部货物（回执 ``affected_cargo_ids``）。更正与相关
    事件、表态及装卸登记经行锁串行。
    """
    try:
        payload.target_type_enum()
    except ValueError as exc:
        raise crud.ApiError(422, "bad_correction_shape", str(exc))
    result = correction_crud.submit_correction(db, payload)
    ack = _correction_ack(result)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=CorrectionReplay(
                **ack.model_dump(mode="json"),
                message="相同内容的更正已存在，重传返回原结果（幂等）",
            ).model_dump(mode="json"),
        )
    return ack


def _history_response(
    target_type: str,
    target_id: str,
    target_exists: bool,
    original_temperature,
    rows: list,
) -> CorrectionHistory:
    corrections = [
        CorrectionHistoryItem(
            **_correction_out(row).model_dump(mode="json"),
            original_temperature=(
                Decimal(original_temperature)
                if target_exists
                else None
            ),
        )
        for row in rows
    ]
    if not target_exists:
        effective = None
        version = 0
    elif rows:
        latest = rows[-1]
        effective = Decimal(latest.new_temperature)
        version = latest.version
    else:
        effective = Decimal(original_temperature)
        version = 0
    return CorrectionHistory(
        target_type=target_type,
        target_id=target_id,
        target_exists=target_exists,
        original_temperature=(
            Decimal(original_temperature) if target_exists else None
        ),
        effective_temperature=effective,
        version=version,
        corrections=corrections,
    )


@app.get(
    "/events/{event_id}/corrections",
    response_model=CorrectionHistory,
    tags=["测温值更正"],
    responses={404: {"description": "事件不存在"}},
)
def list_event_corrections(
    event_id: str, db: Session = Depends(get_db)
) -> CorrectionHistory:
    """一笔货物自有事件的原始测温与历次更正（按版本升序）。

    目标为交接事件时也可查询：``version=0``、有效温度为 null、更正列表为空。
    """
    event = db.get(Event, event_id)
    if event is None:
        raise crud.ApiError(404, "event_not_found", f"事件 {event_id} 不存在")
    if event.event_type.name != "measurement":
        # 交接不可更正：不存在任何更正行；原始/有效温度均为 null、版本 0
        return CorrectionHistory(
            target_type="own_event",
            target_id=event_id,
            target_exists=True,
            original_temperature=None,
            effective_temperature=None,
            version=0,
            corrections=[],
        )
    rows = correction_crud.load_corrections_for_event(db, event_id)
    return _history_response(
        "own_event", event_id, True, event.temperature, rows
    )


@app.get(
    "/vehicle-readings/{reading_id}/corrections",
    response_model=CorrectionHistory,
    tags=["测温值更正"],
    responses={404: {"description": "车载读数不存在"}},
)
def list_reading_corrections(
    reading_id: str, db: Session = Depends(get_db)
) -> CorrectionHistory:
    """一笔车载读数的原始温度与历次更正（按版本升序）。"""
    reading = db.get(VehicleReading, reading_id)
    if reading is None:
        raise crud.ApiError(
            404, "reading_not_found", f"车载读数 {reading_id} 不存在"
        )
    rows = correction_crud.load_corrections_for_reading(db, reading_id)
    return _history_response(
        "vehicle_reading",
        reading_id,
        True,
        reading.temperature,
        rows,
    )


# ---------------------------------------------------------------- 装卸时段更正


def _loading_correction_out(row) -> LoadingCorrectionOut:
    return LoadingCorrectionOut(
        correction_id=row.correction_id,
        trip_id=row.trip_id,
        cargo_id=row.cargo_id,
        origin_loaded_at=ensure_utc(row.origin_loaded_at),
        origin_unloaded_at=ensure_utc(row.origin_unloaded_at),
        previous_loaded_at=ensure_utc(row.previous_loaded_at),
        previous_unloaded_at=ensure_utc(row.previous_unloaded_at),
        new_loaded_at=ensure_utc(row.new_loaded_at),
        new_unloaded_at=ensure_utc(row.new_unloaded_at),
        expected_version=row.expected_version,
        version=row.version,
        reason=row.reason,
        created_at=ensure_utc(row.created_at),
    )


def _parse_origin_loaded_at(raw: str | None) -> datetime:
    """解析定位旧窗口的“原装车时刻”查询参数：必须是带时区的 ISO 8601 时刻。"""
    if raw is None:
        raise crud.ApiError(
            422,
            "missing_parameter",
            "缺少必填参数 origin_loaded_at：原窗口首次登记的带时区装车时刻",
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise crud.ApiError(
            422,
            "bad_loading_correction_shape",
            f"参数 origin_loaded_at={raw!r} 不是合法的 ISO 8601 日期时间",
        )
    if parsed.tzinfo is None:
        raise crud.ApiError(
            422,
            "timezone_required",
            f"参数 origin_loaded_at={raw!r} 必须携带时区偏移"
            "（如 2026-09-30T09:00:00+08:00）",
        )
    return ensure_utc(parsed)


@app.post(
    "/trips/{trip_id}/loading-corrections",
    response_model=LoadingCorrectionAck,
    status_code=201,
    tags=["装卸时段更正"],
    responses={
        200: {
            "model": LoadingCorrectionReplay,
            "description": "同更正编号同请求重传：返回原结果（幂等）",
        },
        404: {"description": "车次 / 装卸窗口不存在"},
        409: {"description": "版本过期 / 编号异请求冲突 / 更正后窗口重叠"},
        422: {"description": "新时段非法（起点不早于终点 / 缺时区 / 原因为空）"},
    },
)
def submit_loading_correction(
    trip_id: str,
    payload: LoadingWindowCorrectionCreate,
    db: Session = Depends(get_db),
):
    """更正一条已登记的装卸时段（只改该条窗口的时间，不改车次 / 货物归属）。

    被更正窗口由 ``cargo_id`` + ``origin_loaded_at``（首次登记装车时刻，车次
    取自路径）按稳定身份定位；``expected_version`` 取车次详情中该窗口的
    ``window_version``（首次更正为 0）。新窗口仍左闭右开且起点严格早于终点，
    按“同一货物任意车次的当前时段不得重叠”校验时排除该条旧窗口，冲突整次
    拒绝。成功后车载读数适用性（装车时刻纳入、卸车时刻排除）、越温报告、
    责任确认、同时越温查询与车次详情一律按当前窗口重建，车次修订号 +1，
    旧装卸预检摘要失效。同更正编号同请求重传返回原结果，异请求冲突。
    """
    # 三个时刻必须显式携带时区偏移（本轮接口的强约束）
    for field in ("origin_loaded_at", "new_loaded_at", "new_unloaded_at"):
        value = getattr(payload, field)
        if value.tzinfo is None or value.utcoffset() is None:
            raise crud.ApiError(
                422,
                "timezone_required",
                f"字段 {field} 必须携带时区偏移（如 2026-09-30T09:00:00+08:00）",
            )
    payload.origin_loaded_at = ensure_utc(payload.origin_loaded_at)
    payload.new_loaded_at = ensure_utc(payload.new_loaded_at)
    payload.new_unloaded_at = ensure_utc(payload.new_unloaded_at)
    if payload.new_loaded_at >= payload.new_unloaded_at:
        raise crud.ApiError(
            422,
            "invalid_loading_window",
            f"车次 {trip_id} 货物 {payload.cargo_id} 更正后的装卸时段 "
            f"[{payload.new_loaded_at.isoformat()}, "
            f"{payload.new_unloaded_at.isoformat()}) 无效："
            "装载时段必须左闭右开且起点严格早于终点",
        )
    result = loading_correction_crud.submit_loading_correction(db, trip_id, payload)
    ack = LoadingCorrectionAck(
        correction=_loading_correction_out(result.correction),
        loaded_at=result.loaded_at,
        unloaded_at=result.unloaded_at,
        origin_loaded_at=result.origin_loaded_at,
        origin_unloaded_at=result.origin_unloaded_at,
        version=result.version,
        affected_cargo_ids=[result.cargo_id],
    )
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=LoadingCorrectionReplay(
                **ack.model_dump(mode="json"),
                message="相同请求的装卸时段更正已存在，重传返回原结果（幂等）",
            ).model_dump(mode="json"),
        )
    return ack


@app.get(
    "/trips/{trip_id}/loading-corrections",
    response_model=LoadingCorrectionHistory,
    tags=["装卸时段更正"],
    responses={
        404: {"description": "车次不存在"},
        422: {"description": "缺少 origin_loaded_at / 时刻非法或无时区"},
    },
)
def list_loading_corrections(
    trip_id: str,
    cargo_id: str,
    origin_loaded_at: str | None = None,
    db: Session = Depends(get_db),
) -> LoadingCorrectionHistory:
    """一条装卸窗口的原始 / 当前窗口与历次更正（按版本升序，append-only）。

    窗口由车次（路径）+ ``cargo_id`` + ``origin_loaded_at``（首次登记装车
    时刻）按稳定身份定位。窗口不存在（车次存在但无此窗口）时返回
    ``window_exists=false``、空更正列表与版本 0。
    """
    origin = _parse_origin_loaded_at(origin_loaded_at)
    if db.get(Trip, trip_id) is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

    loading = loading_correction_crud.get_current_loading(
        db, trip_id, cargo_id, origin
    )
    rows = loading_correction_crud.load_window_corrections(
        db, trip_id, cargo_id, origin
    )
    corrections = [_loading_correction_out(r) for r in rows]

    if loading is None:
        return LoadingCorrectionHistory(
            trip_id=trip_id,
            cargo_id=cargo_id,
            origin_loaded_at=origin,
            window_exists=False,
            origin_unloaded_at=None,
            loaded_at=None,
            unloaded_at=None,
            version=0,
            corrections=[],
        )

    # 当前版本优先取 loading 行的 window_version；无 loading 行版本列的兜底
    # 取最新一条更正（二者在同一事务内维护，正常恒一致）
    version = loading.window_version or (rows[-1].version if rows else 0)
    return LoadingCorrectionHistory(
        trip_id=trip_id,
        cargo_id=cargo_id,
        origin_loaded_at=origin,
        window_exists=True,
        origin_unloaded_at=ensure_utc(loading.origin_unloaded_at),
        loaded_at=ensure_utc(loading.loaded_at),
        unloaded_at=ensure_utc(loading.unloaded_at),
        version=version,
        corrections=corrections,
    )


# ---------------------------------------------------------------- 责任证据封存


def _seal_content_for_response(content: dict, content_digest: str) -> dict:
    """封存内容（规范化 JSON 形态：片段为三元组、无摘要字段）-> 响应模型形态。"""
    def seg_obj(triple):
        return {
            "start": triple[0],
            "end": triple[1],
            "responsible_party": triple[2],
        }

    body = dict(content)
    interval = dict(body["interval"])
    interval["segments"] = [seg_obj(t) for t in interval["segments"]]
    body["interval"] = interval
    body["current_valid_stances"] = [
        dict(s, segments=[seg_obj(t) for t in s["segments"]])
        for s in body["current_valid_stances"]
    ]
    body["content_digest"] = content_digest
    return body


def _seal_out(row, content: dict) -> EvidenceSealOut:
    return EvidenceSealOut(
        case_id=row.case_id,
        cargo_id=row.cargo_id,
        interval_id=row.interval_key,
        snapshot_id=row.snapshot_id,
        content_digest=row.content_digest,
        sealed_at=ensure_utc(row.sealed_at),
        content=EvidenceSealContent.model_validate(
            _seal_content_for_response(content, row.content_digest)
        ),
    )


@app.post(
    "/evidence-seals",
    response_model=EvidenceSealOut,
    status_code=201,
    tags=["责任证据封存"],
    responses={
        200: {
            "model": EvidenceSealReplay,
            "description": "同案件编号同请求重传：即使现况已变也返回原封存包",
        },
        404: {"description": "货物 / 已闭合区间不存在"},
        409: {"description": "快照已变 / 案件编号异请求冲突"},
        422: {"description": "区间仍开放"},
    },
)
def create_evidence_seal(payload: EvidenceSealCreate, db: Session = Depends(get_db)):
    """封存一个已闭合越温区间的责任证据。

    调查人员凭**案件编号**、货物编号、已闭合区间稳定标识与其**当前快照
    标识**封存。封存于持货物行锁的单事务内、在一致的已提交视图上保存：
    区间责任片段、当时各责任方有效表态，以及重建该区间所用的自有事件、
    车载读数（当前有效温度与版本、是否被自有测温取代）、当前装卸窗口及
    版本，并生成内容摘要（``content_digest``）。封存内容此后不可改。

    * 区间消失 / 重开（``404`` / ``422``）或快照已变（``409 stale_snapshot``）
      一律拒绝，事务回滚、不留封存记录；
    * 同案件编号 + 同请求（货物 / 区间 / 快照一致）重试：**即使现况已变**
      也返回原封存包（``200 replayed=true``）；
    * 同案件编号 + 异请求 → ``409 case_id_conflict``。
    """
    result = evidence_crud.create_seal(db, payload)
    body = _seal_out(result.row, result.content)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=EvidenceSealReplay(
                seal=body,
                message="相同请求的责任证据封存包已存在，重传返回原包"
                "（幂等；即使区间此后已消失 / 重开 / 快照变化，原包不变）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/evidence-seals/{case_id}",
    response_model=EvidenceSealOut,
    tags=["责任证据封存"],
    responses={404: {"description": "封存包不存在"}},
)
def get_evidence_seal(case_id: str, db: Session = Depends(get_db)) -> EvidenceSealOut:
    """按案件编号查询封存包：返回原封存内容与内容摘要（此后不可改）。"""
    result = evidence_crud.get_seal(db, case_id)
    return _seal_out(result.row, result.content)


@app.get(
    "/evidence-seals/{case_id}/diff",
    response_model=SealDiffReport,
    tags=["责任证据封存"],
    responses={404: {"description": "封存包不存在"}},
)
def diff_evidence_seal(case_id: str, db: Session = Depends(get_db)) -> SealDiffReport:
    """封存包对照当前现况：列出证据项与责任片段的增 / 删 / 变更。

    状态 ``status``：``unchanged``（区间仍闭合且快照相同）/ ``changed``
    （仍闭合但边界或责任片段已变）/ ``reopened``（区间重开，明确状态）/
    ``disappeared``（区间消失，明确状态）。晚到记录与测温 / 装卸更正只
    反映在本对照结果中，**绝不改写封存原包**；对照为只读操作。
    """
    report = evidence_crud.build_seal_diff(db, case_id)
    # current_interval.segments 为规范化三元组，转成响应对象
    if report.get("current_interval") is not None:
        ci = dict(report["current_interval"])
        ci["segments"] = [
            {"start": t[0], "end": t[1], "responsible_party": t[2]}
            for t in ci["segments"]
        ]
        report["current_interval"] = ci
    return SealDiffReport.model_validate(report)


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}