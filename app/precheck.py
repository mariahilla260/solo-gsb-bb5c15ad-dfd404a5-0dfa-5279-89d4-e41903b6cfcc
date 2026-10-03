"""装卸登记前的温控兼容性预检：分段分析与预检摘要指纹（纯函数，便于单测）。

预检针对“既有装卸时段 ∪ 本批拟登记时段”做扫描线叠加，输出车上货物集合
恒定的各个时段（非空集合才成段），并对每段给出：

* 货物集合（含本批拟登记的货物子集）；
* 温度上限差异：集合内各票货物温度上限的最小 / 最大 / 极差；
* 已有车载读数覆盖：落在该时段内的车次既有车载读数（有效温度与更正版本）；
* 潜在越温风险：时段内既有读数的有效温度高于集合中某票货物自身上限的
  （货物 × 读数）组合；若该货物在读数时刻有自有测温（同刻自有优先），
  标记 ``supplanted_by_own``——车载读数对该货物实际会被取代，仅供参考。

预检摘要（digest）
-----------------
预检摘要把“本次分析所依据的全部输入”绑定为一个指纹：

    digest = "sha256:" + hex(sha256(规范化 JSON))

规范化 JSON 为单行、无多余空白，内容按固定顺序排列：

    [trip_id, trip_revision,
     [[cargo_id, loaded_at, unloaded_at], ...],            -- 本批时段（排序）
     [[cargo_id, temp_upper_limit], ...],                  -- 涉及货物（排序）
     [[reading_id, read_at, version, effective_temp], ...]]-- 车次读数（排序）

其中“涉及货物”为本批货物与该车次既有装卸货物之并集（货物版本指纹＝其
温度上限）；“车次读数”为该车次全部既有读数及其更正版本与有效温度
（读数版本指纹）。确认时服务端按当前数据重算指纹：车次修订号、货物
温度上限或读数版本任一变化都会使指纹改变，过期摘要即被拒绝。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from app.timeutil import ensure_utc

# 温度指纹统一量化到 NUMERIC(10,3) 的精度，避免存储层标度差异影响指纹
_TEMP_SCALE = Decimal("0.001")


def _canon_dt(dt) -> str:
    """UTC ISO 8601（保留微秒），供指纹使用。"""
    return ensure_utc(dt).isoformat()


def _canon_temp(value) -> str:
    """温度指纹：量化到千分位后的十进制字符串。"""
    return str(Decimal(value).quantize(_TEMP_SCALE))


@dataclass(frozen=True)
class BoardWindow:
    """一段“在车”窗口：既有时段或本批拟登记时段。"""

    cargo_id: str
    start: object
    end: object
    proposed: bool  # True=本批拟登记；False=车次既有时段


@dataclass(frozen=True)
class BoardSegment:
    """车上货物集合恒定的一段时段 [start, end)（左闭右开，正长度）。"""

    start: object
    end: object
    cargo_ids: tuple[str, ...]           # 该时段车上全部货物（排序）
    proposed_ids: tuple[str, ...]        # 其中来自本批拟登记的货物（排序）


def boarding_segments(windows: list[BoardWindow]) -> list[BoardSegment]:
    """扫描线叠加全部在车窗口，输出货物集合非空且恒定的分段。

    同一时刻先处理“下车”再处理“上车”，保证左闭右开语义：恰在 t 卸货的
    货物不计入 t 起的新段。同一货物可能有多段窗口（甚至与本批/既有窗口
    重叠——重叠冲突由确认时的批量原子校验拒绝），此处按引用计数判定
    “是否在车”，保证预检对任意输入都有确定结果。相邻且集合相同的段合并。
    """
    events: list[tuple] = []
    for w in windows:
        events.append((w.start, 1, w.cargo_id, w.proposed))
        events.append((w.end, 0, w.cargo_id, w.proposed))
    # 排序键：时刻；同刻下车(0)先于上车(1)；再按货物编号稳定排序
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    onboard: dict[str, int] = {}          # cargo_id -> 在车窗口计数
    proposed_onboard: dict[str, int] = {}  # 其中拟登记窗口的计数
    raw: list[BoardSegment] = []
    seg_start = None
    for at, kind, cargo_id, proposed in events:
        if seg_start is not None and seg_start < at:
            cargos = tuple(sorted(c for c, n in onboard.items() if n > 0))
            if cargos:
                raw.append(
                    BoardSegment(
                        seg_start,
                        at,
                        cargos,
                        tuple(
                            c
                            for c in cargos
                            if proposed_onboard.get(c, 0) > 0
                        ),
                    )
                )
        delta = 1 if kind == 1 else -1
        onboard[cargo_id] = onboard.get(cargo_id, 0) + delta
        if proposed:
            proposed_onboard[cargo_id] = (
                proposed_onboard.get(cargo_id, 0) + delta
            )
        seg_start = at

    # 相邻且货物集合（含拟登记子集）相同的段合并
    segments: list[BoardSegment] = []
    for seg in raw:
        if (
            segments
            and segments[-1].end == seg.start
            and segments[-1].cargo_ids == seg.cargo_ids
            and segments[-1].proposed_ids == seg.proposed_ids
        ):
            prev = segments[-1]
            segments[-1] = BoardSegment(
                prev.start, seg.end, prev.cargo_ids, prev.proposed_ids
            )
        else:
            segments.append(seg)
    return segments


@dataclass(frozen=True)
class ReadingInfo:
    """一笔车载读数的预检视图：有效温度（含更正）与更正版本。"""

    reading_id: str
    read_at: object
    effective_temperature: Decimal
    version: int


@dataclass(frozen=True)
class RiskEntry:
    """一个潜在越温组合：时段内既有读数的有效温度高于该货物自身上限。"""

    cargo_id: str
    reading_id: str
    read_at: object
    effective_temperature: Decimal
    temp_upper_limit: Decimal
    supplanted_by_own: bool  # 该货物在读数时刻有自有测温（车载读数被取代）


@dataclass(frozen=True)
class SegmentAnalysis:
    """一个分段的完整预检分析。"""

    segment: BoardSegment
    temp_limit_min: Decimal
    temp_limit_max: Decimal
    temp_limit_spread: Decimal
    readings: tuple[ReadingInfo, ...]   # 落在该时段内的既有车载读数
    risks: tuple[RiskEntry, ...]


def analyze_segments(
    segments: list[BoardSegment],
    readings: list[ReadingInfo],
    temp_limits: dict[str, Decimal],
    own_measure_at: dict[str, set] | None = None,
) -> list[SegmentAnalysis]:
    """对每个分段计算温度上限差异、读数覆盖与潜在越温风险。

    :param segments: :func:`boarding_segments` 的输出。
    :param readings: 该车次的全部既有车载读数（有效温度 + 版本）。
    :param temp_limits: 货物编号 -> 温度上限（须覆盖段内全部货物）。
    :param own_measure_at: 货物编号 -> 该货物自有测温时刻集合
        （用于标记同刻自有测温取代车载读数的情形）。
    """
    own_measure_at = own_measure_at or {}
    analyses: list[SegmentAnalysis] = []
    for seg in segments:
        limits = [Decimal(temp_limits[c]) for c in seg.cargo_ids]
        lo, hi = min(limits), max(limits)
        covered = tuple(
            r for r in readings if seg.start <= r.read_at < seg.end
        )
        risks: list[RiskEntry] = []
        for r in covered:
            for cargo_id in seg.cargo_ids:
                limit = Decimal(temp_limits[cargo_id])
                if Decimal(r.effective_temperature) > limit:
                    risks.append(
                        RiskEntry(
                            cargo_id=cargo_id,
                            reading_id=r.reading_id,
                            read_at=r.read_at,
                            effective_temperature=Decimal(
                                r.effective_temperature
                            ),
                            temp_upper_limit=limit,
                            supplanted_by_own=(
                                r.read_at in own_measure_at.get(cargo_id, set())
                            ),
                        )
                    )
        analyses.append(
            SegmentAnalysis(
                segment=seg,
                temp_limit_min=lo,
                temp_limit_max=hi,
                temp_limit_spread=hi - lo,
                readings=covered,
                risks=tuple(risks),
            )
        )
    return analyses


def manifest_digest(
    trip_id: str,
    trip_revision: int,
    items,
    cargos: dict[str, Decimal],
    readings: list[ReadingInfo],
) -> str:
    """计算预检摘要指纹（``sha256:`` 前缀）。

    :param items: 本批拟登记时段 ``(cargo_id, loaded_at, unloaded_at)``
        （已归一化到 UTC）。
    :param cargos: 涉及货物（本批 ∪ 车次既有装卸）编号 -> 温度上限。
    :param readings: 该车次全部既有车载读数（有效温度 + 更正版本）。
    """
    payload = [
        trip_id,
        int(trip_revision),
        sorted(
            [cargo_id, _canon_dt(loaded_at), _canon_dt(unloaded_at)]
            for cargo_id, loaded_at, unloaded_at in items
        ),
        sorted(
            [cargo_id, _canon_temp(limit)]
            for cargo_id, limit in cargos.items()
        ),
        sorted(
            [
                r.reading_id,
                _canon_dt(r.read_at),
                int(r.version),
                _canon_temp(r.effective_temperature),
            ]
            for r in readings
        ),
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
