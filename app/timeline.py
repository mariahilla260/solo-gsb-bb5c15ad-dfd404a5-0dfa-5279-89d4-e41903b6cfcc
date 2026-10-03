"""把货物自有事件与车载测温读数合并为统一读数时间线。

合并规则
--------
* 车载读数仅对“读数时刻仍在车上”的货物生效——该货物在对应车次登记了装卸时段
  ``[loaded_at, unloaded_at)`` 且 ``loaded_at <= 读数时刻 < unloaded_at``（左闭右开）。
* 同一时刻该货物有自有测温时，**优先采用自有测温**；车载读数在该时刻对该货物
  标记 ``adopted=False``（被取代），不参与越温重建，但原始读数仍可查询。
* 交接事件只来自货物自有事件，车载读数从不改变保管方。
* **测温更正生效于有效温度**：测温点的温度取该自有测温 / 车载读数最新一次
  更正后的有效温度（无更正时为原始温度）。同刻自有测温优先仍只取决于“该时刻
  是否存在自有测温”，与温度取值无关；被取代的车载点同样携带更正后的有效温度
  与版本（仅用于展示，不参与重建）。
"""
from __future__ import annotations

from decimal import Decimal

from app.chain import (
    SOURCE_OWN,
    SOURCE_VEHICLE,
    TimelinePoint,
    point_from_event,
)


def is_on_board(loading, at) -> bool:
    """读数时刻是否仍在车上：左闭右开 [loaded_at, unloaded_at)。"""
    return loading.loaded_at <= at < loading.unloaded_at


def is_reading_applicable(loading, reading) -> bool:
    """车载读数是否对某票货物生效：同一车次且读数时刻仍在车上（左闭右开）。

    必须同时匹配车次与时刻：同一货物可在首尾相接的不同车次上各有装卸时段，
    不能因另一车次同刻开始的窗口而把本车次已下车时刻的读数误判为适用。
    """
    return loading.trip_id == reading.trip_id and is_on_board(
        loading, reading.read_at
    )


def onboard_loadings(loadings, at) -> list:
    """返回 at 时刻车上的装卸登记（同货物同刻至多一条，应用侧已校验不重叠）。"""
    return [l for l in loadings if is_on_board(l, at)]


def merged_timeline(
    own_events,
    vehicle_readings,
    loadings,
    event_effective: dict[str, tuple] | None = None,
    reading_effective: dict[str, tuple] | None = None,
) -> list[TimelinePoint]:
    """生成货物的完整读数时间线（含被取代的车载点，按时间升序）。

    :param own_events: 该货物的全部自有事件（测温 / 交接）。
    :param vehicle_readings: 该货物所有车次的车载原始读数（可含与其无关车次的，
        由 loadings 决定是否适用）。
    :param loadings: 该货物的全部装卸时段登记。
    :param event_effective: 自有测温事件ID -> ``(有效温度, 版本)``，
        仅含发生过更正的测温；缺失即原始温度、版本 0。
    :param reading_effective: 车载读数ID -> ``(有效温度, 版本)``，语义同上。
    """
    event_effective = event_effective or {}
    reading_effective = reading_effective or {}

    points: list[TimelinePoint] = []
    own_measure_at = set()
    for e in own_events:
        p = point_from_event(e)
        if p.is_measurement:
            own_measure_at.add(e.occurred_at)
            eff = event_effective.get(e.event_id)
            if eff is not None:
                p = TimelinePoint(
                    occurred_at=p.occurred_at,
                    is_measurement=True,
                    temperature=Decimal(eff[0]),
                    source=SOURCE_OWN,
                    source_id=e.event_id,
                    adopted=True,
                    temp_version=eff[1],
                )
        points.append(p)

    for r in vehicle_readings:
        # 必须在“同一车次”的装卸时段内且读数时刻仍在车上
        applicable = any(is_reading_applicable(l, r) for l in loadings)
        if not applicable:
            # 与该货物无关的车次读数（不在车 / 别的车次）完全不出现在其时间线上
            continue
        eff = reading_effective.get(r.reading_id)
        if eff is not None:
            temperature, r_version = Decimal(eff[0]), eff[1]
        else:
            temperature, r_version = Decimal(r.temperature), 0
        points.append(
            TimelinePoint(
                occurred_at=r.read_at,
                is_measurement=True,
                temperature=temperature,
                source=SOURCE_VEHICLE,
                source_id=r.reading_id,
                trip_id=r.trip_id,
                adopted=r.read_at not in own_measure_at,
                temp_version=r_version,
            )
        )
    points.sort(
        key=lambda p: (
            p.occurred_at,
            0 if p.is_measurement else 1,
            {SOURCE_OWN: 0, SOURCE_VEHICLE: 1}.get(p.source, 9),
            p.source_id or "",
        )
    )
    return points


def adopted_timeline(
    own_events,
    vehicle_readings,
    loadings,
    event_effective: dict[str, tuple] | None = None,
    reading_effective: dict[str, tuple] | None = None,
) -> list[TimelinePoint]:
    """只含被采用点的时间线，供保管链重建使用。"""
    return [
        p
        for p in merged_timeline(
            own_events,
            vehicle_readings,
            loadings,
            event_effective,
            reading_effective,
        )
        if p.adopted
    ]


def load_cargo_context(db, cargo_id: str):
    """一次读齐货物的自有事件、装卸时段、相关车次车载读数与测温更正有效值。

    返回 ``(events, loadings, readings, effective)``；``effective`` 为
    ``(event_effective, reading_effective)`` 两个映射，键分别为自有测温事件ID
    与车载读数ID，值为 ``(有效温度, 当前版本)``，只含发生过更正的目标。
    """
    from sqlalchemy import select

    from app.models import Event, Loading, TemperatureCorrection, Trip, VehicleReading
    from app.timeutil import ensure_utc

    events = list(
        db.scalars(
            select(Event)
            .where(Event.cargo_id == cargo_id)
            .order_by(Event.occurred_at, Event.event_id)
        ).all()
    )
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)

    loadings = list(
        db.scalars(
            select(Loading)
            .where(Loading.cargo_id == cargo_id)
            .order_by(Loading.loaded_at, Loading.trip_id)
        ).all()
    )
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)

    readings: list = []
    if loadings:
        trip_ids = {l.trip_id for l in loadings}
        rows = list(
            db.scalars(
                select(VehicleReading)
                .where(VehicleReading.trip_id.in_(trip_ids))
                .order_by(VehicleReading.read_at, VehicleReading.reading_id)
            ).all()
        )
        for r in rows:
            r.read_at = ensure_utc(r.read_at)
        # 仅按关系惰性引用，trip 行由车次查询单独获取
        readings = rows

    effective = load_effective_maps(
        db,
        event_ids={
            e.event_id for e in events
            if getattr(e.event_type, "name", e.event_type) == "measurement"
        },
        reading_ids={r.reading_id for r in readings},
    )
    return events, loadings, readings, effective


def load_effective_maps(db, event_ids=None, reading_ids=None):
    """读取目标测温的当前有效值：``(event_effective, reading_effective)``。

    每个映射的值为 ``(Decimal 有效温度, int 当前版本)``，只含存在更正的目标。
    同一目标取 ``version`` 最大（= 最新一次更正）的一条。
    """
    from sqlalchemy import select

    from app.models import CorrectionTarget, TemperatureCorrection

    event_effective: dict[str, tuple] = {}
    reading_effective: dict[str, tuple] = {}

    if event_ids:
        rows = db.scalars(
            select(TemperatureCorrection)
            .where(
                TemperatureCorrection.target_type
                == CorrectionTarget.own_event,
                TemperatureCorrection.target_event_id.in_(event_ids),
            )
        ).all()
        latest: dict[str, TemperatureCorrection] = {}
        for row in rows:
            prev = latest.get(row.target_event_id)
            if prev is None or row.version > prev.version:
                latest[row.target_event_id] = row
        event_effective = {
            eid: (Decimal(row.new_temperature), row.version)
            for eid, row in latest.items()
        }

    if reading_ids:
        rows = db.scalars(
            select(TemperatureCorrection)
            .where(
                TemperatureCorrection.target_type
                == CorrectionTarget.vehicle_reading,
                TemperatureCorrection.target_reading_id.in_(reading_ids),
            )
        ).all()
        latest = {}
        for row in rows:
            prev = latest.get(row.target_reading_id)
            if prev is None or row.version > prev.version:
                latest[row.target_reading_id] = row
        reading_effective = {
            rid: (Decimal(row.new_temperature), row.version)
            for rid, row in latest.items()
        }

    return event_effective, reading_effective


def resolve_trip_for_reading(db, trip_id: str):
    """取车次行（供读数适用性判断等场景）。"""
    from app.models import Trip

    return db.get(Trip, trip_id)
