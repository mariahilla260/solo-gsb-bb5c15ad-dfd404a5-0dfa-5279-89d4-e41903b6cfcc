"""质控查询：同一车次上多票货物**同时越温**时段（纯逻辑）。

输入为每票货物按现有合并时间线重建出的越温区间（见 app/chain.py），
与该货物在**指定车次**的装卸时段（左闭右开）及查询窗
``[start_at, end_at)`` 求交：

* 越温区间只在货物仍在该车上的时段成立，故与每段装卸时段分别求交；
* 查询窗外的部分一律裁掉；
* 未闭合区间向后视为无限延伸，裁剪至查询终点（或更早的卸货时刻）。

随后把每票货物裁剪后的若干不相交时段做扫描线叠加：
货物集合变化即分段，相邻且集合相同的片段合并，仅保留越温货物数
达到数量门槛（>=2）的正长度片段。
"""
from __future__ import annotations

from dataclasses import dataclass


def _clip(start, end, win_start, win_end):
    """[start, end) 与左闭右开窗口求交；end 为 None 表示开放（延伸到 win_end）。"""
    lo = max(start, win_start)
    hi = min(win_end if end is None else end, win_end)
    return (lo, hi) if lo < hi else None


def cargo_excursion_windows(
    excursions,
    trip_loadings,
    query_start,
    query_end,
) -> list[tuple]:
    """一票货物在查询窗内、且在指定车次车上的越温时段（左闭右开，正长度）。

    :param excursions: 该货物合并时间线重建出的全部越温区间
        （:class:`app.chain.ExcursionResult`，含未闭合）。
    :param trip_loadings: 该货物在**本次查询车次**上的装卸时段（可多段，
        互不可见重叠已由写入侧保证）。
    :param query_start / query_end: 查询窗（左闭右开）。
    """
    windows: list[tuple] = []
    for ex in excursions:
        ex_end = None if ex.is_open else ex.end
        for loading in trip_loadings:
            # 先与装卸时段求交：开放区间以卸货时刻为后沿
            clipped = _clip(
                ex.start, ex_end, loading.loaded_at, loading.unloaded_at
            )
            if clipped is None:
                continue
            # 再与查询窗求交：开放区间（或超出查询窗的闭合区间）裁至查询终点
            clipped = _clip(clipped[0], clipped[1], query_start, query_end)
            if clipped is not None:
                windows.append(clipped)
    # 同货物各装卸时段互不重叠，结果按开始时间排序即可稳定输出
    windows.sort()
    return windows


@dataclass(frozen=True)
class SimultaneousSegment:
    """同时越温片段：[start, end) 内越温货物集合恒为 cargo_ids。"""

    start: object
    end: object
    cargo_ids: tuple[str, ...]

    @property
    def cargo_count(self) -> int:
        return len(self.cargo_ids)


def simultaneous_segments(
    cargo_windows: dict[str, list[tuple]],
    min_cargo: int = 2,
) -> list[SimultaneousSegment]:
    """对各票货物的越温时段做扫描线叠加，输出达门槛的同时越温片段。

    :param cargo_windows: 货物编号 -> 该车次上的越温时段列表（左闭右开）。
    :param min_cargo: 数量门槛（不得小于 2）；片段内越温货物数 >= 门槛才保留。

    同一时刻先处理“结束”再处理“开始”，保证左闭右开语义：
    在车区间恰在 t 结束的货物不计入 t 起的新片段。货物集合变化即分段，
    相邻且集合相同的片段合并；零长度片段不产生。
    """
    if min_cargo < 2:
        raise ValueError("min_cargo 不得小于 2")

    events: list[tuple] = []
    for cargo_id, windows in cargo_windows.items():
        for start, end in windows:
            events.append((start, 1, cargo_id))   # 进入越温
            events.append((end, 0, cargo_id))     # 结束（同刻排在开始之前）
    # 排序键：时刻；同刻结束(0)先于开始(1)；再按货物编号稳定排序
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    active: set[str] = set()
    raw: list[tuple] = []          # (start, end, frozenset(cargo_ids))
    seg_start = None
    for at, kind, cargo_id in events:
        if seg_start is not None and seg_start < at:
            raw.append((seg_start, at, frozenset(active)))
        if kind == 1:
            active.add(cargo_id)
        else:
            active.discard(cargo_id)
        seg_start = at

    segments: list[SimultaneousSegment] = []
    for start, end, cargos in raw:
        if start >= end or len(cargos) < min_cargo:
            continue
        ordered = tuple(sorted(cargos))
        # 相邻且货物集合相同的片段合并（防御性合并：正常扫描线在边界处
        # 集合必变，此处保证任意输入下约定都成立）
        if (
            segments
            and segments[-1].end == start
            and segments[-1].cargo_ids == ordered
        ):
            segments[-1] = SimultaneousSegment(segments[-1].start, end, ordered)
        else:
            segments.append(SimultaneousSegment(start, end, ordered))
    return segments
