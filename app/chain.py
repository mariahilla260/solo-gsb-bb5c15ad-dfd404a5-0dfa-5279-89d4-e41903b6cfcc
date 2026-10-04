"""保管链重建与越温责任拆分（纯函数，便于单测）。

规则
----
* 时间线点按 occurred_at 排序；同一时刻测温先于交接处理
  （“交接时刻起归接收方”——交接在该时刻已生效）。
* 测温点有两个来源：
  - 货物自有测温事件（source=``own``）；
  - 车载测温读数（source=``vehicle``），仅对读数时刻仍在车上的货物生效，
    且同一时刻货物有自有测温时车载读数被取代（在合并时间线阶段已剔除，
    见 app/timeline.py），本模块只处理“已采用”的测温点。
* 高于上限的测温开启越温区间；越温期间首次不超限（<= 上限）的测温关闭区间。
* 区间跨交接时按交接时刻拆分：[区间起, 交接时刻) 归交出方，
  [交接时刻, ...) 归接收方；交接与区间起点同刻时直接由接收方担责。
* 区间内重复超限测温不影响区间；区间外不超限测温无影响。
* 不产生零长度片段。

区间稳定标识：自有测温开启的区间以事件ID为标识（保持历史兼容）；
车载读数开启的区间以 ``veh:<reading_id>`` 为标识（见 :func:`interval_id`）。
"""
from dataclasses import dataclass
from decimal import Decimal

from app.models import Event, EventType

# 测温点来源
SOURCE_OWN = "own"
SOURCE_VEHICLE = "vehicle"

# 车载读数开启区间的标识前缀（自有事件ID不得此前缀，写入时拒绝）
VEHICLE_INTERVAL_PREFIX = "veh:"


@dataclass(frozen=True)
class TimelinePoint:
    """合并后时间线上的一个点：测温或交接。

    自有事件与车载读数在重建阶段统一表示为本结构，链逻辑不感知存储差异。
    ``adopted`` 仅对车载测温有意义：同刻存在货物自有测温时为 False
    （原始读数仍可查，但不参与重建）；被**生效探头故障**覆盖的车载读数同样
    为 False（隔离，不参与重建）；其余点恒为 True。
    ``temperature`` 对测温点为**当前有效温度**（应用测温更正后的最新温度，
    无更正时即原始温度）；``temp_version`` 为该有效温度的更正版本
    （0=原始值，n>=1 表示取第 n 次更正值）。``quarantine_faults`` 为覆盖该
    车载读数的生效探头故障编号（空元组=未隔离；自有 / 交接点恒为空）。
    交接点二者无意义。
    """

    occurred_at: object
    is_measurement: bool
    temperature: Decimal | None = None
    giver: str | None = None
    receiver: str | None = None
    source: str = SOURCE_OWN
    source_id: str | None = None      # 自有=event_id；车载=reading_id
    trip_id: str | None = None
    adopted: bool = True
    temp_version: int = 0
    quarantine_faults: tuple[str, ...] = ()

    @property
    def is_handover(self) -> bool:
        return not self.is_measurement


def point_from_event(e: Event) -> TimelinePoint:
    """自有事件 ORM 对象 -> 时间线点。"""
    if e.event_type is EventType.measurement:
        return TimelinePoint(
            occurred_at=e.occurred_at,
            is_measurement=True,
            temperature=Decimal(e.temperature),
            source=SOURCE_OWN,
            source_id=e.event_id,
        )
    return TimelinePoint(
        occurred_at=e.occurred_at,
        is_measurement=False,
        giver=e.giver,
        receiver=e.receiver,
        source=SOURCE_OWN,
        source_id=e.event_id,
    )


def measurement_point(
    occurred_at,
    temperature,
    source: str = SOURCE_OWN,
    source_id: str | None = None,
    trip_id: str | None = None,
    adopted: bool = True,
) -> TimelinePoint:
    return TimelinePoint(
        occurred_at=occurred_at,
        is_measurement=True,
        temperature=Decimal(temperature),
        source=source,
        source_id=source_id,
        trip_id=trip_id,
        adopted=adopted,
    )


@dataclass(frozen=True)
class Segment:
    start: object          # datetime，含
    end: object | None     # datetime，不含；None 表示未闭合
    party: str


@dataclass(frozen=True)
class ExcursionResult:
    start: object
    end: object | None
    segments: tuple[Segment, ...]
    # 自有测温开启 / 闭合时的事件ID（车载来源时为 None）
    start_event_id: str | None = None
    end_event_id: str | None = None
    # 开启 / 闭合测温的来源与来源坐标
    start_source: str = SOURCE_OWN
    start_reading_id: str | None = None
    start_trip_id: str | None = None
    end_source: str = SOURCE_OWN
    end_reading_id: str | None = None
    end_trip_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.end is None


def interval_id(ex: ExcursionResult) -> str:
    """区间的稳定标识：自有开启=事件ID；车载开启=``veh:<reading_id>``。"""
    if ex.start_source == SOURCE_VEHICLE:
        return f"{VEHICLE_INTERVAL_PREFIX}{ex.start_reading_id}"
    return ex.start_event_id


@dataclass(frozen=True)
class ChainResult:
    current_holder: str
    excursions: tuple[ExcursionResult, ...]


def _as_point(item) -> TimelinePoint:
    """统一 Event（自有）与 TimelinePoint 两种输入。"""
    if isinstance(item, TimelinePoint):
        return item
    return point_from_event(item)


def _sorted_events(items) -> list[TimelinePoint]:
    # 同一时刻：measurement 在 handover 之前（交接在测温“之后”生效）；
    # 同为测温时自有在车载之前（被采用的车载点不会与自有测温同刻，此处仅作稳定兜底）
    kind_order = {True: 0, False: 1}
    source_order = {SOURCE_OWN: 0, SOURCE_VEHICLE: 1}
    points = [_as_point(i) for i in items]
    return sorted(
        points,
        key=lambda p: (
            p.occurred_at,
            kind_order[p.is_measurement],
            source_order.get(p.source, 9),
            p.source_id or "",
        ),
    )


def rebuild_chain(
    initial_holder: str,
    temp_upper_limit: Decimal,
    items: list,
) -> ChainResult:
    """按时间线重建当前保管方与全部越温区间（含责任片段）。

    ``items`` 可为自有 :class:`Event` 列表、:class:`TimelinePoint` 列表或二者混合。
    未被采用（``adopted=False``）的车载测温点不参与重建。
    """
    limit = Decimal(temp_upper_limit)
    holder = initial_holder

    open_start = None                  # 当前越温区间起点
    opener = None                      # 开启测温的来源坐标 (source, id, trip_id)
    segments: list[Segment] = []       # 当前区间已切分出的片段（最后一段开放）
    excursions: list[ExcursionResult] = []

    for p in _sorted_events(items):
        if not p.adopted:
            continue
        if p.is_measurement:
            over = Decimal(p.temperature) > limit
            if open_start is None and over:
                # 开启越温：当前保管方自测温时刻起担责
                open_start = p.occurred_at
                opener = (p.source, p.source_id, p.trip_id)
                segments = [Segment(p.occurred_at, None, holder)]
            elif open_start is not None and not over:
                # 首次不超限测温 -> 在该时刻闭合
                last = segments[-1]
                if last.start < p.occurred_at:
                    segments[-1] = Segment(last.start, p.occurred_at, last.party)
                else:
                    segments.pop()  # 理论不会发生（同刻两笔采用测温已拒绝）
                excursions.append(
                    ExcursionResult(
                        open_start,
                        p.occurred_at,
                        tuple(segments),
                        start_event_id=(
                            opener[1] if opener[0] == SOURCE_OWN else None
                        ),
                        end_event_id=(
                            p.source_id if p.source == SOURCE_OWN else None
                        ),
                        start_source=opener[0],
                        start_reading_id=(
                            opener[1] if opener[0] == SOURCE_VEHICLE else None
                        ),
                        start_trip_id=opener[2],
                        end_source=p.source,
                        end_reading_id=(
                            p.source_id if p.source == SOURCE_VEHICLE else None
                        ),
                        end_trip_id=p.trip_id,
                    )
                )
                open_start = None
                opener = None
                segments = []
        else:  # handover（只可能来自货物自有事件）
            if open_start is not None:
                # 跨交接拆分责任片段
                prev = segments[-1]
                if p.occurred_at > prev.start:
                    # 交出方承担 [prev.start, 交接时刻)，接收方自交接时刻起
                    segments[-1] = Segment(prev.start, p.occurred_at, prev.party)
                    segments.append(Segment(p.occurred_at, None, p.receiver))
                else:
                    # 交接与区间起点（同刻的开启测温）同一时刻：
                    # 交出方片段为零长，直接由接收方自该时刻起担责
                    segments[-1] = Segment(p.occurred_at, None, p.receiver)
            holder = p.receiver

    if open_start is not None:
        # 未闭合区间：最后片段保持开放终点，标记“未结束”
        excursions.append(
            ExcursionResult(
                open_start,
                None,
                tuple(segments),
                start_event_id=(
                    opener[1] if opener[0] == SOURCE_OWN else None
                ),
                start_source=opener[0],
                start_reading_id=(
                    opener[1] if opener[0] == SOURCE_VEHICLE else None
                ),
                start_trip_id=opener[2],
            )
        )

    return ChainResult(current_holder=holder, excursions=tuple(excursions))


def holder_at(initial_holder: str, items: list, at) -> str:
    """返回 at 时刻（该时刻的交接已生效后）的保管方。

    用于写入校验：交接的交出方必须等于“交接时刻的保管方”。
    车载测温不改变保管方，故只遍历自有交接点。
    """
    holder = initial_holder
    for p in _sorted_events(items):
        if p.is_handover and p.occurred_at <= at:
            holder = p.receiver
    return holder
