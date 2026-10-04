"""Pydantic 请求 / 响应模型。"""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import CorrectionTarget, EventType, StanceKind


class CargoCreate(BaseModel):
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    started_at: datetime = Field(..., description="起始时间（ISO 8601，建议带时区）")
    temp_upper_limit: Decimal = Field(..., description="温度上限（摄氏度）")
    initial_holder: str = Field(
        ..., min_length=1, max_length=128, description="起始保管方"
    )


class EventCreate(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="事件ID")
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    occurred_at: datetime = Field(..., description="发生时间（ISO 8601，建议带时区）")

    # 测温
    temperature: Decimal | None = Field(
        default=None, description="测温温度；测温事件必填且事件只能含此字段"
    )
    # 交接
    giver: str | None = Field(default=None, max_length=128, description="交出方")
    receiver: str | None = Field(default=None, max_length=128, description="接收方")

    def event_type(self) -> EventType:
        is_measurement = self.temperature is not None
        is_handover = self.giver is not None or self.receiver is not None
        if is_measurement and is_handover:
            raise ValueError("事件不能同时包含温度与交接双方")
        if not is_measurement and not is_handover:
            raise ValueError("事件必须包含 temperature，或同时包含 giver 与 receiver")
        if is_handover and not (self.giver and self.receiver):
            raise ValueError("交接事件必须同时提供交出方与接收方")
        if is_handover and self.giver == self.receiver:
            raise ValueError("交出方与接收方不能相同")
        return EventType.measurement if is_measurement else EventType.handover


class CargoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cargo_id: str
    started_at: datetime
    temp_upper_limit: Decimal
    initial_holder: str
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    cargo_id: str
    event_type: EventType
    occurred_at: datetime
    temperature: Decimal | None = Field(
        ..., description="原始录入温度（测温）；更正不改此字段"
    )
    effective_temperature: Decimal | None = Field(
        ...,
        description="当前有效温度（测温）：无更正时等于 temperature，"
        "有更正时为最新更正值；交接恒为 null",
    )
    version: int = Field(
        ...,
        description="测温更正版本：0=原始值，n>=1 表示已更正 n 次；交接恒为 0",
    )
    giver: str | None
    receiver: str | None


class ResponsibilitySegment(BaseModel):
    """越温区间内按交接时刻切分出的责任片段。"""

    start: datetime = Field(..., description="片段起点（含）")
    end: datetime | None = Field(
        ..., description="片段终点（不含）；null 表示区间未闭合（未结束）"
    )
    responsible_party: str = Field(..., description="该片段责任方（保管方）")


class Excursion(BaseModel):
    """一次越温区间及其责任拆分。"""

    start: datetime = Field(..., description="越温开始（首次超限测温时刻，含）")
    end: datetime | None = Field(
        ..., description="越温结束（首次不超限测温时刻，不含）；null=未闭合"
    )
    is_open: bool = Field(..., description="是否尚未闭合（未结束）")
    segments: list[ResponsibilitySegment] = Field(..., description="责任方片段")
    interval_id: str = Field(
        ...,
        description="区间稳定标识：货物自有测温开启=事件ID；车载读数开启=veh:<读数ID>",
    )
    start_source: str = Field(
        ..., description="开启读数来源：own=货物自有测温；vehicle=车载读数"
    )
    end_source: str | None = Field(
        ..., description="闭合读数来源；未闭合时为 null"
    )
    start_reading_id: str | None = Field(
        ..., description="车载开启时的读数ID（自有开启为 null）"
    )
    end_reading_id: str | None = Field(
        ..., description="车载闭合时的读数ID（自有闭合 / 未闭合为 null）"
    )
    start_trip_id: str | None = Field(
        ..., description="车载开启时所属车次ID"
    )
    end_trip_id: str | None = Field(
        ..., description="车载闭合时所属车次ID"
    )


class ChainReport(BaseModel):
    cargo_id: str
    temp_upper_limit: Decimal
    current_holder: str = Field(..., description="按事件重建出的当前保管方")
    excursions: list[Excursion]


class IdempotentReplay(BaseModel):
    """同 ID 同内容重传时的响应：不重复写入。"""

    replayed: bool = True
    event_id: str
    message: str


# ----------------------------------------------------- 责任确认


class StanceCreate(BaseModel):
    """责任方表态提交。

    区间由路径上的区间标识（开启测温事件ID）定位；
    货物编号同样在路径上，请求体不再重复。
    """

    snapshot_id: str = Field(
        ...,
        min_length=8,
        max_length=71,
        description="提交前查询到的当前快照标识；与服务端当前快照不一致则拒绝（过期）",
    )
    responsible_party: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="表态责任方，必须出现在当前区间责任片段中",
    )
    stance: str = Field(
        ...,
        description="表态：confirm=确认担责；dispute=异议（必须同时提供 reason）",
    )
    reason: str | None = Field(
        default=None,
        max_length=512,
        description="异议理由；stance=dispute 时必填且非空，confirm 时必须为空",
    )
    stance_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="表态编号，客户端指定；同编号同内容重传返回原记录",
    )


class StanceSegment(BaseModel):
    """责任方在区间内承担的片段（一个责任方可能跨交接承担多段）。"""

    start: datetime
    end: datetime
    responsible_party: str


class LatestStance(BaseModel):
    """某责任方的最新一条有效表态。"""

    stance_id: str
    responsible_party: str
    stance: StanceKind
    reason: str | None
    created_at: datetime
    segments: list[StanceSegment] = Field(
        ..., description="该责任方在当前区间内承担的全部片段"
    )


class ResponsibilityInterval(BaseModel):
    """一个已闭合越温区间的责任确认视图。"""

    cargo_id: str
    interval_id: str = Field(
        ...,
        description="区间稳定标识：自有开启=测温事件ID；车载开启=veh:<读数ID>",
    )
    start_event_id: str | None = Field(
        ..., description="货物自有测温开启/闭合时的事件ID；车载来源为 null"
    )
    end_event_id: str | None
    start_source: str = Field(..., description="开启读数来源：own / vehicle")
    end_source: str = Field(..., description="闭合读数来源：own / vehicle")
    start_reading_id: str | None = Field(..., description="车载开启读数ID")
    end_reading_id: str | None = Field(..., description="车载闭合读数ID")
    start_trip_id: str | None
    end_trip_id: str | None
    start: datetime
    end: datetime
    snapshot_id: str = Field(..., description="当前快照标识（区间起止 + 责任片段）")
    segments: list[StanceSegment] = Field(..., description="当前责任片段（按时间）")
    latest_stances: list[LatestStance] = Field(
        ..., description="各责任方最新一条有效表态（按片段顺序）"
    )
    summary: str = Field(
        ...,
        description="汇总状态：all_confirmed 全确认 / has_dispute 有异议 / pending 待确认",
    )


class ResponsibilityReport(BaseModel):
    """货物下全部已闭合越温区间的责任确认汇总（开放区间不参与确认）。"""

    cargo_id: str
    intervals: list[ResponsibilityInterval]


class StanceOut(BaseModel):
    """表态记录。"""

    model_config = ConfigDict(from_attributes=True)

    stance_id: str
    cargo_id: str
    interval_id: str = Field(
        ...,
        description="区间稳定标识：自有开启=测温事件ID；车载开启=veh:<读数ID>",
    )
    opener_source: str = Field(..., description="区间开启读数来源：own / vehicle")
    start_event_id: str | None = Field(
        ..., description="自有开启的事件ID（车载开启为 null）"
    )
    vehicle_reading_id: str | None = Field(
        ..., description="车载开启的读数ID（自有开启为 null）"
    )
    responsible_party: str
    stance: StanceKind
    reason: str | None
    snapshot_id: str
    created_at: datetime


class StanceReplay(BaseModel):
    """同编号同内容重传时的响应：返回原记录（即使其快照已失效）。"""

    replayed: bool = True
    stance: StanceOut
    message: str


class StanceHistoryItem(StanceOut):
    """表态历史中的一条：附加该表态对当前快照是否仍有效。"""

    is_current: bool = Field(
        ...,
        description="对当前快照是否仍有效：区间/片段变化后为 false，但历史仍保留可查",
    )
    superseded: bool = Field(
        ..., description="是否已被同责任方更新的表态改口（新编号）"
    )


class StanceHistory(BaseModel):
    cargo_id: str
    interval_id: str
    interval_exists: bool = Field(..., description="区间当前是否存在")
    is_open: bool = Field(..., description="区间当前是否仍开放")
    current_snapshot_id: str | None = Field(
        ..., description="当前快照标识；区间不存在/开放时为 null"
    )
    history: list[StanceHistoryItem] = Field(
        ..., description="历次表态（含已改口、已失效），按提交时间升序"
    )


# ----------------------------------------------------- 车次 / 装卸 / 车载读数


class TripCreate(BaseModel):
    trip_id: str = Field(..., min_length=1, max_length=64, description="车次编号")
    vehicle_no: str = Field(
        ..., min_length=1, max_length=64, description="车牌号 / 车辆编号"
    )
    note: str | None = Field(default=None, max_length=256, description="备注")


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    vehicle_no: str
    note: str | None
    revision: int = Field(
        ...,
        description="车次修订号（初始 0）：该车次的装卸登记、车载读数写入、"
        "车载读数更正使其 +1；装卸前预检凭预期修订号做乐观并发校验",
    )
    created_at: datetime


class LoadingItem(BaseModel):
    """登记一批中的一条：某票货物在该车次上的装卸时段。"""

    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    loaded_at: datetime = Field(..., description="装载时刻（区间起点，含）")
    unloaded_at: datetime = Field(..., description="卸载时刻（区间终点，不含）")


class ManifestCreate(BaseModel):
    """整批登记一个车次承载的多票货物装卸时段（原子：任一非法整批拒绝）。

    车次编号取自路径参数，请求体无需也不应提供 trip_id。
    """

    trip_id: str | None = Field(default=None, max_length=64)
    items: list[LoadingItem] = Field(
        ...,
        min_length=1,
        description="本批装卸时段登记（至少一条）；同货物时段不得与既有/同批重叠",
    )


class LoadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    cargo_id: str
    origin_loaded_at: datetime = Field(
        ...,
        description="首次登记装车时刻：该条装卸窗口的稳定身份（更正不改），"
        "更正时以此与车次、货物共同定位旧窗口",
    )
    loaded_at: datetime = Field(
        ..., description="当前窗口起点（含；装卸时段更正后为修正值）"
    )
    unloaded_at: datetime = Field(
        ..., description="当前窗口终点（不含；装卸时段更正后为修正值）"
    )
    window_version: int = Field(
        ...,
        description="该条装卸窗口的更正版本：0=原始登记，n>=1 表示已更正 n 次；"
        "更正请求的 expected_version 取此值",
    )


class ManifestOut(BaseModel):
    trip_id: str
    items: list[LoadingOut]


# ----------------------------------------------------- 装卸前预检与确认


class ManifestPrecheckCreate(BaseModel):
    """装卸前温控兼容性预检请求（只读，不写入装卸记录）。

    车次编号取自路径参数；``expected_trip_revision`` 为调用方预期的车次
    修订号（先查询车次详情获得），与当前修订号不一致即拒绝。
    """

    expected_trip_revision: int = Field(
        ...,
        ge=0,
        description="预期车次修订号：与车次当前修订号不一致返回 409 stale_trip_revision",
    )
    items: list[LoadingItem] = Field(
        ...,
        min_length=1,
        description="本批拟登记的装卸时段（至少一条）；预检不写入任何记录",
    )


class PrecheckReadingCoverage(BaseModel):
    """落在某预检时段内的一笔既有车载读数。"""

    reading_id: str
    read_at: datetime
    effective_temperature: Decimal = Field(
        ..., description="当前有效温度（含测温更正）"
    )
    version: int = Field(..., description="该读数的当前更正版本（0=原始值）")
    quarantined: bool = Field(
        ...,
        description="该读数是否被本车次生效探头故障覆盖（被覆盖则不参与实际"
        "越温重建，列此仅供参考）",
    )
    covering_fault_ids: list[str] = Field(
        ..., description="覆盖该读数的生效故障编号（未覆盖为空列表）"
    )


class PrecheckRisk(BaseModel):
    """一个潜在越温组合：时段内既有读数的有效温度高于该货物自身上限。"""

    cargo_id: str
    reading_id: str
    read_at: datetime
    effective_temperature: Decimal
    temp_upper_limit: Decimal = Field(..., description="该货物自身温度上限")
    supplanted_by_own: bool = Field(
        ...,
        description="该货物在读数时刻是否有自有测温（同刻自有优先，"
        "为 true 时车载读数对该货物实际被取代，风险仅供参考）",
    )
    quarantined: bool = Field(
        ...,
        description="该读数是否被生效探头故障覆盖（被覆盖则不参与实际重建，"
        "风险仅供参考）",
    )
    covering_fault_ids: list[str] = Field(
        ..., description="覆盖该读数的生效故障编号（未覆盖为空列表）"
    )


class PrecheckSegment(BaseModel):
    """一个预检时段：车上货物集合恒定的一段左闭右开时段。"""

    start: datetime = Field(..., description="时段起点（含，UTC）")
    end: datetime = Field(..., description="时段终点（不含，UTC）")
    cargo_ids: list[str] = Field(..., description="该时段车上货物集合（按编号排序）")
    proposed_cargo_ids: list[str] = Field(
        ..., description="其中来自本批拟登记的货物（按编号排序）"
    )
    cargo_count: int
    temp_limit_min: Decimal = Field(..., description="集合内最低温度上限")
    temp_limit_max: Decimal = Field(..., description="集合内最高温度上限")
    temp_limit_spread: Decimal = Field(
        ..., description="温度上限极差（max - min）：同车货物温控要求差异"
    )
    readings: list[PrecheckReadingCoverage] = Field(
        ..., description="落在该时段内的车次既有车载读数（按时刻排序）"
    )
    reading_count: int
    risks: list[PrecheckRisk] = Field(
        ..., description="潜在越温风险（货物 × 读数组合，按读数时刻与货物编号排序）"
    )


class ManifestPrecheckOut(BaseModel):
    """预检结果：各重叠时段分析 + 预检摘要（确认时须原样携带）。"""

    trip_id: str
    trip_revision: int = Field(..., description="预检时的车次当前修订号")
    digest: str = Field(
        ...,
        description="预检摘要（sha256: 前缀）：绑定本批时段、车次修订号、"
        "涉及货物温度上限与车次读数版本；确认时服务端按当前数据重算，"
        "任一变化即拒绝过期摘要",
    )
    segments: list[PrecheckSegment] = Field(
        ...,
        description="既有装卸 ∪ 本批拟登记叠加出的各时段分析"
        "（车上货物集合非空才成段，按时间升序）",
    )


class ManifestConfirmCreate(BaseModel):
    """装卸登记确认请求：携带预检摘要，摘要过期即拒绝。

    车次编号取自路径参数。摘要校验通过后沿用与 ``POST /trips/{id}/manifest``
    相同的批量原子校验：任一时段冲突整批拒绝、不留部分记录。
    """

    trip_id: str | None = Field(default=None, max_length=64)
    digest: str = Field(
        ...,
        min_length=8,
        max_length=71,
        description="预检接口返回的预检摘要；车次、货物或读数版本变化后即过期",
    )
    items: list[LoadingItem] = Field(
        ...,
        min_length=1,
        description="与预检一致的本批装卸时段（摘要对时段内容敏感）",
    )


class VehicleReadingCreate(BaseModel):
    reading_id: str = Field(
        ..., min_length=1, max_length=64, description="车载读数编号（客户端指定）"
    )
    trip_id: str = Field(..., min_length=1, max_length=64, description="所属车次编号")
    read_at: datetime = Field(..., description="读数时刻（ISO 8601，建议带时区）")
    temperature: Decimal = Field(..., description="车载探头温度（摄氏度）")


class VehicleReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reading_id: str
    trip_id: str
    read_at: datetime
    temperature: Decimal = Field(..., description="原始读数温度；更正不改此字段")
    effective_temperature: Decimal = Field(
        ...,
        description="当前有效温度：无更正时等于 temperature，有更正时为最新更正值",
    )
    version: int = Field(
        ..., description="更正版本：0=原始值，n>=1 表示已更正 n 次"
    )
    created_at: datetime


class VehicleReadingReplay(BaseModel):
    """同编号同内容重传：返回原读数，并给出本读数适用/影响的货物。"""

    replayed: bool = True
    reading: VehicleReadingOut
    affected_cargo_ids: list[str] = Field(
        ...,
        description="读数时刻仍在车上的货物（按各自温度上限判断越温；可能为空）",
    )
    message: str


class VehicleReadingAck(BaseModel):
    """车载读数新写入回执：含本次受影响货物。"""

    reading: VehicleReadingOut
    affected_cargo_ids: list[str] = Field(
        ..., description="读数时刻仍在车上、按各自温度上限受本读数影响的货物"
    )


class ReadingApplication(BaseModel):
    """一笔车载读数对一票货物的适用性/采用情况（车次查询用）。"""

    cargo_id: str
    on_board: bool = Field(..., description="读数时刻该货物是否仍在车上（左闭右开）")
    adopted: bool = Field(
        ...,
        description="是否被该货物越温时间线采用：在车上且同刻无自有测温、"
        "且未被生效探头故障覆盖时为 true",
    )
    over_limit: bool | None = Field(
        ...,
        description="按该货物自身温度上限是否越温（按当前有效温度判定，"
        "仅供查阅）；不在车上时为 null。被故障隔离的读数仍给出其温度判定",
    )
    quarantined: bool = Field(
        ...,
        description="是否被生效探头故障覆盖而隔离（不参与越温重建）；"
        "不在车上时恒为 false",
    )
    covering_fault_ids: list[str] = Field(
        ...,
        description="覆盖该读数时刻的生效故障编号（在车且被覆盖时非空；按编号排序）",
    )
    reason: str = Field(
        ...,
        description="未采用原因：adopted=已采用 / not_on_board=不在车上 / "
        "supplanted_by_own=同刻有货物自有测温 / quarantined=被生效探头故障覆盖"
        "（原始读数仍可查）。同时被故障覆盖且同刻有自有测温时优先标记 "
        "supplanted_by_own（covering_fault_ids 仍给出覆盖故障）",
    )


class ProbeFaultRevocationOut(BaseModel):
    """一条故障撤销记录（append-only 留痕）。"""

    model_config = ConfigDict(from_attributes=True)

    revocation_id: str
    fault_id: str
    trip_id: str
    expected_version: int
    version: int = Field(..., description="撤销后的故障版本（= expected_version + 1）")
    revoked_at: datetime = Field(..., description="撤销发生时刻（UTC）")
    created_at: datetime


class ProbeFaultOut(BaseModel):
    """一条探头故障登记及其撤销状态（登记与撤销均留痕）。"""

    model_config = ConfigDict(from_attributes=True)

    fault_id: str
    trip_id: str
    fault_start: datetime = Field(..., description="故障时段起点（含，UTC）")
    fault_end: datetime = Field(..., description="故障时段终点（不含，UTC）")
    reason: str
    version: int = Field(
        ...,
        description="故障版本：0=已登记未撤销；1=已撤销（撤销的乐观锁以此为准）",
    )
    active: bool = Field(..., description="是否生效（未撤销）")
    created_at: datetime
    revocation: ProbeFaultRevocationOut | None = Field(
        ..., description="撤销记录；未撤销时为 null"
    )


class TripReadingOut(VehicleReadingOut):
    quarantined: bool = Field(
        ...,
        description="该读数是否被本车次的生效探头故障覆盖（读数时刻落在未撤销"
        "故障时段内）：被覆盖即不参与任何在车货物的越温重建，原始读数仍可查",
    )
    covering_fault_ids: list[str] = Field(
        ..., description="覆盖该读数的本车次生效故障编号（未覆盖为空列表）"
    )
    applications: list[ReadingApplication] = Field(
        ..., description="对车次上各票已登记货物的适用性，按货物编号排序"
    )


class TripDetail(TripOut):
    """车次详情：装卸时段 + 车载原始读数（含对各货物的采用情况）。"""

    loadings: list[LoadingOut]
    readings: list[TripReadingOut]
    faults: list[ProbeFaultOut] = Field(
        ...,
        description="本车次的探头故障登记（含已撤销，按故障开始时刻升序）；"
        "生效故障覆盖的车载读数不参与越温重建",
    )


class TimelinePointOut(BaseModel):
    """货物合并读数时间线上的一个点（查询各货物采用的读数来源）。"""

    occurred_at: datetime
    point_type: str = Field(..., description="measurement=测温；handover=交接")
    source: str = Field(
        ...,
        description="读数来源：own=货物自有事件；vehicle=车载读数；交接恒为 own",
    )
    event_id: str | None = Field(..., description="自有事件ID（车载点为 null）")
    reading_id: str | None = Field(..., description="车载读数ID（自有为 null）")
    trip_id: str | None = Field(..., description="车载读数所属车次")
    temperature: Decimal | None = Field(
        ...,
        description="测温点的**当前有效温度**（含更正）；交接为 null",
    )
    version: int = Field(
        ...,
        description="测温点有效温度的更正版本：0=原始值，n>=1 为最新更正版本",
    )
    giver: str | None
    receiver: str | None
    adopted: bool = Field(
        ...,
        description="车载读数是否被采用：同刻有自有测温或被生效探头故障覆盖时"
        "为 false（原始读数仍可查）",
    )
    quarantined: bool = Field(
        ...,
        description="车载读数是否因被生效探头故障覆盖而隔离"
        "（不参与越温重建；自有 / 交接点恒为 false）",
    )
    covering_fault_ids: list[str] = Field(
        ...,
        description="覆盖该车载读数的生效故障编号（仅车载点；未隔离为空列表）",
    )
    changes_holder: bool = Field(
        ..., description="是否为改变保管方的交接（仅自有交接为 true）"
    )


class CargoTimeline(BaseModel):
    cargo_id: str
    points: list[TimelinePointOut] = Field(
        ..., description="合并读数时间线（含被自有测温取代的车载原始读数），按时间"
    )


# ----------------------------------------------------- 质控：同时越温查询


class SimultaneousExcursionSegment(BaseModel):
    """同时越温片段：窗口内越温货物集合恒定，且数量达到门槛。"""

    start: datetime = Field(..., description="片段起点（含，UTC）")
    end: datetime = Field(..., description="片段终点（不含，UTC；未闭合区间已裁至查询终点）")
    cargo_count: int = Field(..., description="该片段同时越温的货物数量")
    cargo_ids: list[str] = Field(
        ..., description="该片段同时越温的货物编号（按编号排序）"
    )


class SimultaneousExcursionReport(BaseModel):
    """同一车次同时越温时段查询结果（按时间排序的不重叠片段）。"""

    trip_id: str
    start_at: datetime = Field(..., description="查询窗起点（含）")
    end_at: datetime = Field(..., description="查询窗终点（不含）")
    min_cargo: int = Field(..., description="生效的数量门槛")
    segments: list[SimultaneousExcursionSegment] = Field(
        ...,
        description="同时越温货物数达到门槛的正长度片段；集合变化即分段，"
        "相邻同集合片段已合并；无匹配时为空列表",
    )


# ----------------------------------------------------- 测温值更正


class CorrectionCreate(BaseModel):
    """测温值更正提交：只改温度，时间 / 归属 / 原编号不变；交接不可更正。"""

    correction_id: str = Field(
        ..., min_length=1, max_length=64, description="更正编号（客户端指定）"
    )
    target_type: str = Field(
        ...,
        description="目标类型：own_event=货物自有测温事件；"
        "vehicle_reading=车载测温读数",
    )
    target_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="目标编号：自有测温事件ID 或 车载读数ID",
    )
    expected_version: int = Field(
        ...,
        ge=0,
        description="预期的目标当前更正版本（首次更正为 0，随后为最新版本号）；"
        "与当前版本不一致则拒绝（版本过期）",
    )
    new_temperature: Decimal = Field(..., description="更正后的新温度（摄氏度）")

    def target_type_enum(self) -> CorrectionTarget:
        raw = (self.target_type or "").strip().lower()
        if raw == "own_event":
            return CorrectionTarget.own_event
        if raw == "vehicle_reading":
            return CorrectionTarget.vehicle_reading
        raise ValueError(
            "target_type 只允许 own_event 或 vehicle_reading"
        )


class CorrectionOut(BaseModel):
    """一条测温更正记录。"""

    correction_id: str
    target_type: str = Field(..., description="own_event / vehicle_reading")
    target_id: str = Field(..., description="目标测温编号（事件ID 或 读数ID）")
    cargo_id: str | None = Field(..., description="自有测温更正时所属货物")
    trip_id: str | None = Field(..., description="车载读数更正时所属车次")
    expected_version: int
    version: int = Field(..., description="本次更正后的版本（= expected_version + 1）")
    new_temperature: Decimal
    created_at: datetime


class CorrectionAck(BaseModel):
    """更正新写入回执：更正记录 + 更正后目标的有效温度/版本 + 受影响货物。"""

    correction: CorrectionOut
    effective_temperature: Decimal = Field(
        ..., description="更正后目标的当前有效温度"
    )
    version: int = Field(..., description="更正后目标的当前版本")
    affected_cargo_ids: list[str] = Field(
        ...,
        description="受影响货物：自有测温更正为该测温所属货物；"
        "车载读数更正为读数时刻仍在车上的全部货物（左闭右开，可能为空）",
    )


class CorrectionReplay(CorrectionAck):
    """同更正ID同内容重传：返回原结果（幂等）。"""

    replayed: bool = True
    message: str


class CorrectionHistoryItem(CorrectionOut):
    """更正历史中的一条：附目标原始温度与本次更正后的有效温度。"""

    original_temperature: Decimal | None = Field(
        ..., description="目标测温的原始温度；目标已不存在时为 null"
    )


class CorrectionHistory(BaseModel):
    """一笔目标测温的原始记录信息与历次更正（按版本升序）。"""

    target_type: str
    target_id: str
    target_exists: bool = Field(..., description="目标测温当前是否存在")
    original_temperature: Decimal | None = Field(
        ..., description="原始温度；目标不存在时为 null"
    )
    effective_temperature: Decimal | None = Field(
        ...,
        description="当前有效温度（最新更正值；无更正时等于原始温度）；"
        "目标不存在时为 null",
    )
    version: int = Field(..., description="当前更正版本（无更正 / 目标不存在为 0）")
    corrections: list[CorrectionHistoryItem] = Field(
        ..., description="历次更正（append-only），按版本升序"
    )


# ----------------------------------------------------- 装卸时段更正


class LoadingWindowCorrectionCreate(BaseModel):
    """装卸时段更正提交：只改一条装卸窗口的时间，车次 / 货物归属不变。

    被更正窗口由 ``cargo_id`` + ``origin_loaded_at``（首次登记装车时刻，车次
    取自路径）按稳定身份定位；``expected_version`` 为该窗口当前更正版本
    （首次更正为 0，取车次详情中该窗口的 ``window_version``）。三个时刻均须
    携带时区偏移；新窗口左闭右开且起点严格早于终点。
    """

    correction_id: str = Field(
        ..., min_length=1, max_length=64, description="更正编号（客户端指定）"
    )
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    origin_loaded_at: datetime = Field(
        ...,
        description="原车次上该条窗口**首次登记的装车时刻**（稳定身份，须带时区）",
    )
    expected_version: int = Field(
        ...,
        ge=0,
        description="预期的该窗口当前更正版本（首次更正为 0，"
        "随后取车次详情中的 window_version）；不一致则拒绝（版本过期）",
    )
    new_loaded_at: datetime = Field(
        ..., description="修正后的装车时刻（新窗口起点，含；须带时区）"
    )
    new_unloaded_at: datetime = Field(
        ..., description="修正后的卸车时刻（新窗口终点，不含；须带时区）"
    )
    reason: str = Field(
        ..., min_length=1, max_length=512, description="更正原因（必填非空）"
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("更正原因 reason 不能为空白")
        return value.strip()


class LoadingCorrectionOut(BaseModel):
    """一条装卸时段更正记录（append-only）。"""

    correction_id: str
    trip_id: str
    cargo_id: str
    origin_loaded_at: datetime = Field(
        ..., description="被更正窗口的稳定身份：首次登记装车时刻（不可变）"
    )
    origin_unloaded_at: datetime = Field(
        ..., description="首次登记卸车时刻（原始窗口，留痕）"
    )
    previous_loaded_at: datetime = Field(
        ..., description="更正前一刻的窗口起点（本次所改的旧窗口起点）"
    )
    previous_unloaded_at: datetime = Field(
        ..., description="更正前一刻的窗口终点（本次所改的旧窗口终点）"
    )
    new_loaded_at: datetime = Field(..., description="更正后的窗口起点（含）")
    new_unloaded_at: datetime = Field(..., description="更正后的窗口终点（不含）")
    expected_version: int
    version: int = Field(..., description="本次更正后的版本（= expected_version + 1）")
    reason: str
    created_at: datetime


class LoadingCorrectionAck(BaseModel):
    """装卸时段更正回执：更正记录 + 更正后该窗口的当前窗口 / 版本。"""

    correction: LoadingCorrectionOut
    loaded_at: datetime = Field(..., description="更正后该窗口的当前起点（含）")
    unloaded_at: datetime = Field(..., description="更正后该窗口的当前终点（不含）")
    origin_loaded_at: datetime
    origin_unloaded_at: datetime
    version: int = Field(..., description="更正后该窗口的当前版本")
    affected_cargo_ids: list[str] = Field(
        ...,
        description="受影响货物：装卸时段更正只作用于该窗口所属货物，恒为单元素列表",
    )


class LoadingCorrectionReplay(LoadingCorrectionAck):
    """同更正编号同请求重传：返回原结果（幂等）。"""

    replayed: bool = True
    message: str


class LoadingCorrectionHistory(BaseModel):
    """一条装卸窗口的当前/原始窗口与历次更正（按版本升序）。"""

    trip_id: str
    cargo_id: str
    origin_loaded_at: datetime = Field(
        ..., description="被查询窗口的稳定身份：首次登记装车时刻"
    )
    window_exists: bool = Field(..., description="该装卸窗口当前是否存在")
    origin_unloaded_at: datetime | None = Field(
        ..., description="首次登记卸车时刻（原始窗口）；窗口不存在时为 null"
    )
    loaded_at: datetime | None = Field(
        ..., description="当前窗口起点（含）；窗口不存在时为 null"
    )
    unloaded_at: datetime | None = Field(
        ..., description="当前窗口终点（不含）；窗口不存在时为 null"
    )
    version: int = Field(..., description="当前更正版本（无更正 / 窗口不存在为 0）")
    corrections: list[LoadingCorrectionOut] = Field(
        ..., description="历次更正（append-only），按版本升序"
    )


# ----------------------------------------------------- 车次探头故障（请求 / 回执）


class ProbeFaultCreate(BaseModel):
    """车次探头故障登记：故障编号 + 左闭右开带时区时段 + 原因（车次取自路径）。"""

    fault_id: str = Field(
        ..., min_length=1, max_length=64, description="故障编号（客户端指定）"
    )
    trip_id: str | None = Field(default=None, max_length=64)
    fault_start: datetime = Field(
        ...,
        description="故障时段起点（含），ISO 8601 且必须显式携带时区偏移",
    )
    fault_end: datetime = Field(
        ...,
        description="故障时段终点（不含，左闭右开），必须严格晚于起点且带时区",
    )
    reason: str = Field(
        ..., min_length=1, max_length=512, description="故障原因（必填非空）"
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("故障原因 reason 不能为空白")
        return value.strip()


class ProbeFaultAck(BaseModel):
    """故障登记回执：故障记录 + 本次隔离的车载读数 / 受影响货物。"""

    fault: ProbeFaultOut
    quarantined_reading_ids: list[str] = Field(
        ...,
        description="故障时段内的车次车载读数：任一生效故障覆盖即隔离，"
        "不参与货物越温重建（晚到读数同样隔离，按重建时生效故障判定）",
    )
    affected_cargo_ids: list[str] = Field(
        ...,
        description="故障时段内在车上的货物（装卸窗口与故障时段相交，按编号排序）",
    )


class ProbeFaultReplay(ProbeFaultAck):
    """同故障编号同请求重传：返回原结果（幂等）。"""

    replayed: bool = True
    message: str


class ProbeFaultRevokeCreate(BaseModel):
    """故障撤销：撤销编号 + 故障编号 + 期望版本（车次取自路径）。"""

    revocation_id: str = Field(
        ..., min_length=1, max_length=64, description="撤销编号（客户端指定）"
    )
    fault_id: str = Field(..., min_length=1, max_length=64, description="被撤销故障编号")
    trip_id: str | None = Field(default=None, max_length=64)
    expected_version: int = Field(
        ...,
        ge=0,
        description="预期的故障当前版本（登记后为 0）；不一致或故障已撤销"
        "则拒绝（stale_fault_version）",
    )


class ProbeFaultRevokeAck(BaseModel):
    """故障撤销回执：撤销记录 + 撤销后恢复采用的读数。"""

    revocation: ProbeFaultRevocationOut
    fault: ProbeFaultOut
    restored_reading_ids: list[str] = Field(
        ...,
        description="撤销后恢复参与重建的车载读数：故障时段内且不再被任何"
        "其它生效故障覆盖的读数（按当前有效温度恢复，同刻自有测温仍优先）",
    )
    affected_cargo_ids: list[str] = Field(
        ..., description="故障时段内在车上的货物（按编号排序）"
    )


class ProbeFaultRevokeReplay(ProbeFaultRevokeAck):
    """同撤销编号同请求重传：返回原结果（幂等）。"""

    replayed: bool = True
    message: str


class ProbeFaultList(BaseModel):
    """车次的故障登记列表（含已撤销，按故障开始时刻升序）。"""

    trip_id: str
    faults: list[ProbeFaultOut]


# ----------------------------------------------------- 责任证据封存


class EvidenceSealCreate(BaseModel):
    """责任证据封存请求：案件编号 + 货物 + 已闭合区间标识 + 当前快照标识。"""

    case_id: str = Field(
        ..., min_length=1, max_length=128,
        description="案件编号（客户端指定，封存包主键；同案件编号同请求重试"
        "返回原包，异请求冲突）",
    )
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    interval_id: str = Field(
        ..., min_length=1, max_length=68,
        description="已闭合越温区间的稳定标识：自有开启=测温事件ID；"
        "车载开启=veh:<读数ID>",
    )
    snapshot_id: str = Field(
        ..., min_length=8, max_length=71,
        description="封存前查询到的区间当前快照标识；与当前快照不一致"
        "（快照已变）即拒绝，不留封存记录",
    )


class SealCargo(BaseModel):
    """封存时的货物温控参数。"""

    cargo_id: str
    started_at: datetime
    temp_upper_limit: Decimal
    initial_holder: str


class SealSegment(BaseModel):
    """封存 / 现况对照中的责任片段（开放区间末段 end 可为 null）。"""

    start: datetime
    end: datetime | None = None
    responsible_party: str


class SealStance(BaseModel):
    """封存时一条当时有效的责任方表态（各责任方最新一条有效表态）。"""

    stance_id: str
    responsible_party: str
    stance: StanceKind
    reason: str | None = None
    created_at: datetime
    snapshot_id: str | None = Field(
        ..., description="该表态提交时校验通过的快照标识"
    )
    segments: list[SealSegment] = Field(
        ..., description="该责任方在封存区间内承担的全部片段"
    )


class SealOwnEvent(BaseModel):
    """封存的一条货物自有事件（测温附当前有效温度与更正版本）。"""

    event_id: str
    event_type: str
    occurred_at: datetime
    temperature: Decimal | None = Field(
        ..., description="原始测温温度；交接为 null"
    )
    effective_temperature: Decimal | None = Field(
        ..., description="封存时当前有效温度（含更正）；交接为 null"
    )
    version: int = Field(..., description="封存时测温更正版本（0=原始值）")
    giver: str | None = None
    receiver: str | None = None


class SealVehicleReading(BaseModel):
    """封存的一笔车载读数：当前有效温度、更正版本与封存时是否被采用。"""

    reading_id: str
    trip_id: str
    read_at: datetime
    temperature: Decimal = Field(..., description="原始读数温度")
    effective_temperature: Decimal = Field(
        ..., description="封存时当前有效温度（含更正）"
    )
    version: int = Field(..., description="封存时更正版本（0=原始值）")
    adopted: bool = Field(
        ...,
        description="封存时该读数是否被本货物采用：同刻有自有测温时为 false"
        "（原始读数仍随包封存）",
    )


class SealLoadingWindow(BaseModel):
    """封存的一条当前装卸窗口及窗口版本。"""

    trip_id: str
    cargo_id: str
    origin_loaded_at: datetime = Field(
        ..., description="窗口稳定身份：首次登记装车时刻（更正不改）"
    )
    loaded_at: datetime = Field(..., description="封存时当前窗口起点（含）")
    unloaded_at: datetime = Field(..., description="封存时当前窗口终点（不含）")
    window_version: int = Field(
        ..., description="封存时窗口更正版本（0=原始登记）"
    )


class SealInterval(BaseModel):
    """封存的区间责任片段视图。"""

    interval_id: str
    start: datetime
    end: datetime | None
    is_open: bool = False
    start_source: str
    end_source: str | None
    start_event_id: str | None
    end_event_id: str | None
    start_reading_id: str | None
    end_reading_id: str | None
    start_trip_id: str | None
    end_trip_id: str | None
    snapshot_id: str | None
    segments: list[SealSegment]


class EvidenceSealContent(BaseModel):
    """封存包内容（此后不可改；查询封存包原样返回）。"""

    case_id: str
    sealed_at: datetime
    cargo: SealCargo
    interval: SealInterval
    current_valid_stances: list[SealStance] = Field(
        ..., description="封存时各责任方最新一条有效表态"
    )
    summary: str = Field(..., description="封存时的责任确认汇总状态")
    own_events: list[SealOwnEvent] = Field(
        ..., description="重建该区间所用的货物自有事件（不晚于区间闭合时刻）"
    )
    vehicle_readings: list[SealVehicleReading] = Field(
        ...,
        description="重建该区间所用的车载读数（区间内在车适用，含有效温度/"
        "版本/是否被同刻自有测温取代）",
    )
    loading_windows: list[SealLoadingWindow] = Field(
        ..., description="重建该区间所用的当前装卸窗口及窗口版本"
    )
    content_digest: str = Field(
        ..., description="封存内容摘要：sha256:<hex>（对规范化封存内容取摘要）"
    )


class EvidenceSealOut(BaseModel):
    """封存回执 / 封存包查询结果。"""

    case_id: str
    cargo_id: str
    interval_id: str
    snapshot_id: str = Field(..., description="封存时绑定的区间当前快照标识")
    content_digest: str
    sealed_at: datetime
    content: EvidenceSealContent = Field(..., description="封存内容原文（不可改）")


class EvidenceSealReplay(BaseModel):
    """同案件编号同请求重传：即使现况已变也返回原封存包。"""

    replayed: bool = True
    seal: EvidenceSealOut
    message: str


# 现况对照


class SealDiffItem(BaseModel):
    """一个证据项的增 / 删 / 变更。"""

    key: str = Field(
        ...,
        description="证据项稳定键：自有事件=event_id；车载读数=reading_id；"
        "装卸窗口=trip_id|origin_loaded_at",
    )
    change: str = Field(..., description="added / removed / changed")
    fields: list[str] | None = Field(
        default=None,
        description="changed 时发生变化的字段名（added/removed 为 null）",
    )
    sealed: dict | None = Field(..., description="封存时该项内容；added 时为 null")
    current: dict | None = Field(..., description="当前该项内容；removed 时为 null")


class SealSegmentChange(BaseModel):
    """一条责任片段的增 / 删 / 变更（以片段起点为稳定键）。"""

    start: datetime
    change: str = Field(..., description="added / removed / changed")
    sealed: list | None = Field(..., description="封存片段 [start,end,party]；added 为 null")
    current: list | None = Field(..., description="当前片段 [start,end,party]；removed 为 null")


class SealDiffReport(BaseModel):
    """封存包 vs 当前现况的对照结果（不改写封存原包）。"""

    case_id: str
    cargo_id: str
    interval_id: str
    status: str = Field(
        ...,
        description="unchanged 仍闭合快照相同 / changed 仍闭合快照已变 / "
        "reopened 区间重开 / disappeared 区间消失",
    )
    sealed_snapshot_id: str
    current_snapshot_id: str | None
    sealed_at: datetime
    compared_at: datetime
    current_interval: SealInterval | None = Field(
        ...,
        description="当前区间视图：仍闭合 / 重开时给出（重开 is_open=true、"
        "end/snapshot 为 null）；区间消失时为 null",
    )
    segment_changes: list[SealSegmentChange] = Field(
        ..., description="责任片段的增 / 删 / 变更（按片段起点排序）"
    )
    evidence_changes: dict = Field(
        ...,
        description="证据项变更：{own_events, vehicle_readings, loading_windows} "
        "各为 SealDiffItem 列表（按键排序）",
    )
