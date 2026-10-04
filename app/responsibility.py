"""已闭合越温区间的责任确认：快照标识、表态有效性与汇总（纯逻辑）。

区间的开启测温可来自**货物自有测温事件**或**车载测温读数**（后者仅对读数
时刻仍在车上的货物生效，同刻有自有测温时以自有为准）。二者统一在合并读数
时间线上重建（见 app/timeline.py），区间稳定标识见 app/chain.interval_id。

快照标识
--------
区间的“当前快照”由**区间起止**与**责任片段序列**决定：

    snapshot_id = "sha256:" + hex(sha256(规范化 JSON))

规范化 JSON 为单行、无多余空白、键固定按
``[start, end, segments: [[start, end, responsible_party], ...]]``
排列，时间统一为 UTC ISO 8601（``YYYY-MM-DDTHH:MM:SSZ``）。

表态有效性（晚到事件 / 晚到车载读数重建后）
-------------------------------------------
表态行只追加、从不修改或删除。晚到的测温 / 交接事件或车载读数使区间重建后：

* 区间消失（开启读数不再开启区间）或重新开放 → 该区间全部旧表态失效；
* 区间仍在且已闭合、但边界或责任片段变化 → 受影响片段上的旧表态失效，
  未变化片段上的表态继续有效（逐片段判定）；
* 快照完全不变 → 全部继续有效。

失效表态的历史仍可经表态历史接口查询。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.chain import ExcursionResult, interval_id
from app.models import Stance, StanceKind
from app.timeutil import ensure_utc

# 汇总状态
ALL_CONFIRMED = "all_confirmed"  # 全部责任方均已确认
HAS_DISPUTE = "has_dispute"      # 有效表态中存在异议
PENDING = "pending"              # 尚有责任方未表态（且无异议）


def _ts(dt: datetime) -> str:
    """UTC ISO 8601 秒级，Z 后缀（与对外时间格式一致）。"""
    dt = ensure_utc(dt).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _segment_triples(segments) -> list:
    return [
        [
            _ts(seg.start),
            _ts(seg.end) if seg.end is not None else None,
            seg.party,
        ]
        for seg in segments
    ]


def attested_segments_json(excursion: ExcursionResult, party: str) -> str:
    """责任方表态时承担的片段坐标（规范化 JSON 字符串）。"""
    triples = _segment_triples(
        [seg for seg in excursion.segments if seg.party == party]
    )
    return json.dumps(triples, ensure_ascii=False, separators=(",", ":"))


def snapshot_id(excursion: ExcursionResult) -> str:
    """由区间起止与责任片段序列计算当前快照标识。"""
    payload = [
        _ts(excursion.start),
        _ts(excursion.end) if excursion.end is not None else None,
        _segment_triples(excursion.segments),
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class SegmentView:
    start: datetime
    end: datetime
    responsible_party: str


@dataclass(frozen=True)
class StanceView:
    """一条“当前有效”的最新表态（每个责任方至多一条）。"""

    stance_id: str
    responsible_party: str
    kind: StanceKind
    reason: str | None
    created_at: datetime
    segments: tuple[SegmentView, ...]   # 该责任方在当前区间内的全部片段
    snapshot_id: str | None = None      # 表态提交时校验通过的快照标识


@dataclass(frozen=True)
class IntervalView:
    cargo_id: str
    interval_id: str
    start_event_id: str | None
    end_event_id: str | None
    start_source: str
    end_source: str
    start_reading_id: str | None
    end_reading_id: str | None
    start_trip_id: str | None
    end_trip_id: str | None
    start: datetime
    end: datetime
    snapshot_id: str
    segments: tuple[SegmentView, ...]
    parties: tuple[str, ...]
    stances: tuple[StanceView, ...]
    summary: str


def _segments_of_party(ex: ExcursionResult, party: str) -> tuple[SegmentView, ...]:
    return tuple(
        SegmentView(start=seg.start, end=seg.end, responsible_party=seg.party)
        for seg in ex.segments
        if seg.party == party
    )


def is_stance_current(stance: Stance, ex: ExcursionResult | None) -> bool:
    """表态是否对当前重建结果仍然有效（逐片段判定）。

    * 区间不存在 / 未闭合 / 区间标识变化（开启读数来源或坐标变化）→ False；
    * 表态时该责任方的片段坐标与当前不一致（边界或责任片段变化）→ False；
    * 区间标识与片段坐标完全一致（未受晚到记录影响）→ True。
    """
    if ex is None or ex.is_open:
        return False
    if stance.interval_key != interval_id(ex):
        return False
    return (
        stance.attested_segments
        == attested_segments_json(ex, stance.responsible_party)
    )


def build_interval_view(
    cargo_id: str,
    ex: ExcursionResult,
    stances: list[Stance],
) -> IntervalView:
    """组装单个已闭合区间的责任确认视图。

    ``stances`` 为该区间（按 interval_key 过滤）的全部表态行
    （含已被改口、已失效的历史行）。
    """
    for s in stances:
        s.created_at = ensure_utc(s.created_at)

    current_rows = [s for s in stances if is_stance_current(s, ex)]
    # 每个责任方只取最新一条有效表态作为“最新有效表态”（显式取最大，不依赖行序）；
    # 更早的有效表态为改口历史，可在表态历史接口中查到
    latest_by_party: dict[str, Stance] = {}
    for s in current_rows:
        prev = latest_by_party.get(s.responsible_party)
        if prev is None or (s.created_at, s.stance_id) > (
            prev.created_at,
            prev.stance_id,
        ):
            latest_by_party[s.responsible_party] = s
    parties = tuple(dict.fromkeys(seg.party for seg in ex.segments))
    stance_views: list[StanceView] = []
    # 按责任方在片段中首次出现的顺序输出，稳定可读
    for party in parties:
        s = latest_by_party.get(party)
        if s is None:
            continue
        stance_views.append(
            StanceView(
                stance_id=s.stance_id,
                responsible_party=party,
                kind=StanceKind(s.kind),
                reason=s.reason,
                created_at=s.created_at,
                segments=_segments_of_party(ex, party),
                snapshot_id=s.snapshot_id,
            )
        )

    responded = {sv.responsible_party for sv in stance_views}
    if any(sv.kind is StanceKind.dispute for sv in stance_views):
        summary = HAS_DISPUTE
    elif set(parties) <= responded and parties:
        summary = ALL_CONFIRMED
    else:
        summary = PENDING

    return IntervalView(
        cargo_id=cargo_id,
        interval_id=interval_id(ex),
        start_event_id=ex.start_event_id,
        end_event_id=ex.end_event_id,
        start_source=ex.start_source,
        end_source=ex.end_source,
        start_reading_id=ex.start_reading_id,
        end_reading_id=ex.end_reading_id,
        start_trip_id=ex.start_trip_id,
        end_trip_id=ex.end_trip_id,
        start=ex.start,
        end=ex.end,
        snapshot_id=snapshot_id(ex),
        segments=tuple(
            SegmentView(
                start=seg.start, end=seg.end, responsible_party=seg.party
            )
            for seg in ex.segments
        ),
        parties=parties,
        stances=tuple(stance_views),
        summary=summary,
    )


def rebuild(
    cargo,
    own_events,
    loadings=None,
    vehicle_readings=None,
    effective=None,
    quarantine=None,
) -> tuple[object, dict[str, ExcursionResult]]:
    """在合并读数时间线上重建保管链，返回（重建结果, 区间标识 -> 闭合区间）。

    ``own_events`` 为货物自有事件；``loadings`` / ``vehicle_readings``
    为该货物的装卸时段与相关车载读数（缺省时退化为仅自有事件，兼容旧调用）。
    ``effective`` 为 ``(event_effective, reading_effective)`` 更正有效值映射；
    ``quarantine`` 为车载读数ID -> 生效探头故障编号元组的隔离映射（被覆盖
    的车载读数不参与重建）。
    """
    from app import chain, timeline

    if loadings is None or vehicle_readings is None:
        items = list(own_events)
    else:
        event_effective, reading_effective = effective or ({}, {})
        items = timeline.adopted_timeline(
            own_events,
            vehicle_readings,
            loadings,
            event_effective,
            reading_effective,
            quarantine or {},
        )
    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), items
    )
    closed = {
        interval_id(ex): ex for ex in result.excursions if not ex.is_open
    }
    return result, closed
