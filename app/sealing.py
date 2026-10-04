"""责任证据封存：证据归集、内容摘要（规范化 JSON + SHA-256）与现况对照（纯逻辑）。

封存内容
--------
调查人员对一个**已闭合**越温区间封存责任证据。封存包（:func:`build_seal_content`）
在一致的已提交视图上保存：

* 货物温控参数（编号 / 起始时间 / 温度上限 / 起始保管方）；
* 区间责任片段、开启 / 闭合读数来源坐标与当前快照标识；
* **当时有效表态**：封存时各责任方最新一条仍对当前快照有效的表态；
* 重建该区间所用的**自有事件**（发生时刻不晚于区间闭合时刻的全部事件，
  测温附当前有效温度与更正版本）；
* **车载读数**：对该货物适用（同一车次且读数时刻在当前装卸窗口内，
  左闭右开）且读数时刻落在区间内的读数，附当前有效温度、更正版本与
  封存时是否被同刻自有测温取代（``adopted``）；
* **当前装卸窗口及版本**：与区间时间范围相交的该货物全部装卸窗口
  （``window_version``，当前窗口时间即其当前版本）。

内容摘要
========
对封存内容（除 ``content_digest`` 自身外）取规范化 JSON 的 SHA-256：

    content_digest = "sha256:" + hex(sha256(canonical_json))

规范化 JSON 为单行、无多余空白（``separators=(",", ":")``），非 ASCII 不转义；
时间统一为 UTC ISO 8601 秒级（``YYYY-MM-DDTHH:MM:SSZ``），温度量化到
千分位十进制字符串。封存包原文与摘要一同落库，此后不可改。

现况对照（diff）
----------------
封存后再次重建：

* ``unchanged``：区间仍存在、已闭合且快照标识与封存时相同——证据项无
  增删变更（封存内容本就是该视图的确定性投影）；
* ``changed``：区间仍闭合但快照已变（边界 / 责任片段变化）；
* ``reopened``：同标识区间当前为开放区间；
* ``disappeared``：同标识区间当前既不闭合也不开放（已消失）。

对照按封存时相同的证据范围逐项比较，列出**证据项**（自有事件 / 车载读数
/ 装卸窗口）与**责任片段**的新增 / 删除 / 变更；晚到记录与测温 / 装卸
更正只影响对照结果，**绝不改写封存原包**。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from app import chain, responsibility as resp, timeline
from app.chain import interval_id
from app.timeutil import ensure_utc

# 封存后现况状态
STATUS_UNCHANGED = "unchanged"        # 区间仍闭合且快照相同
STATUS_CHANGED = "changed"            # 区间仍闭合但快照已变（边界/片段变化）
STATUS_REOPENED = "reopened"          # 同标识区间重新开放
STATUS_DISAPPEARED = "disappeared"    # 同标识区间已消失

# 证据项变更类型
ITEM_ADDED = "added"
ITEM_REMOVED = "removed"
ITEM_CHANGED = "changed"

_TEMP_SCALE = Decimal("0.001")


# ----------------------------------------------------------- 规范化原语


def ts(dt) -> str:
    """UTC ISO 8601 秒级，Z 后缀（封存内容时间的统一表示）。"""
    dt = ensure_utc(dt).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def temp(value) -> str:
    """温度指纹 / 表示：量化到千分位的十进制字符串。"""
    return str(Decimal(value).quantize(_TEMP_SCALE))


def _seg_triples(segments) -> list:
    out = []
    for seg in segments:
        party = getattr(seg, "responsible_party", None) or seg.party
        out.append(
            [
                ts(seg.start),
                ts(seg.end) if seg.end is not None else None,
                party,
            ]
        )
    return out


def canonical_json(obj) -> str:
    """封存内容的规范化 JSON 字符串（与内容摘要一一对应）。"""
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest_of(canonical: str) -> str:
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def request_fingerprint(cargo_id: str, interval_key: str, snapshot_id: str) -> str:
    """同案件编号幂等 / 冲突裁决用的请求指纹（货物 + 区间 + 当前快照）。"""
    payload = [cargo_id, interval_key, snapshot_id]
    return digest_of(canonical_json(payload))


# ----------------------------------------------------------- 证据归集


def _own_event_item(e, event_effective: dict) -> dict:
    is_measurement = getattr(e.event_type, "name", e.event_type) == "measurement"
    eff = event_effective.get(e.event_id) if is_measurement else None
    return {
        "event_id": e.event_id,
        "event_type": "measurement" if is_measurement else "handover",
        "occurred_at": ts(e.occurred_at),
        "temperature": temp(e.temperature) if is_measurement else None,
        "effective_temperature": (
            temp(eff[0]) if eff is not None else (temp(e.temperature) if is_measurement else None)
        ),
        "version": eff[1] if eff is not None else 0,
        "giver": e.giver,
        "receiver": e.receiver,
    }


def _reading_item(r, reading_effective: dict, adopted: bool,
                  isolated: bool = False, covering_fault_ids=None) -> dict:
    eff = reading_effective.get(r.reading_id)
    return {
        "reading_id": r.reading_id,
        "trip_id": r.trip_id,
        "read_at": ts(r.read_at),
        "temperature": temp(r.temperature),
        "effective_temperature": (
            temp(eff[0]) if eff is not None else temp(r.temperature)
        ),
        "version": eff[1] if eff is not None else 0,
        "adopted": adopted,
        "isolated": isolated,
        "covering_fault_ids": sorted(covering_fault_ids or []),
    }


def _loading_item(l) -> dict:
    return {
        "trip_id": l.trip_id,
        "cargo_id": l.cargo_id,
        "origin_loaded_at": ts(l.origin_loaded_at),
        "loaded_at": ts(l.loaded_at),
        "unloaded_at": ts(l.unloaded_at),
        "window_version": l.window_version or 0,
    }


def scoped_evidence(
    events,
    readings,
    loadings,
    effective,
    start_bound,
    span_end,
    apply_window_filter: bool = True,
    active_faults=None,
):
    """按时间范围归集重建证据（封存与现况对照共用同一定义）。

    :param start_bound: 车载读数 / 装卸窗口的范围下界（含）；货物起始时间
        或区间起点。
    :param span_end: 范围上界（含）；封存时为区间闭合时刻，开放区间对照
        传入查询上界；为 None 时视为无上界。
    :param apply_window_filter: 自有事件是否同样施加下界——封存与对照的
        自有事件均取“不晚于闭合时刻”的**全部**事件（含开启前完整保管链，
        下界置 False）；车载读数 / 装卸窗口恒按下界与上界归集。
    :param active_faults: 相关车次的生效探头故障时段（:class:`timeline.ActiveFault`
        列表）；被覆盖的适用车载读数标记 ``isolated=true`` 与覆盖故障编号，
        与时间线的隔离判定一致。
    :returns: ``(own_event_items, reading_items, loading_items)`` 规范化字典，
        分别按 事件ID / (读数时刻, 读数ID) / (车次, 原装车时刻) 升序。
    """
    event_effective, reading_effective = effective
    active_faults = active_faults or []

    lower = ensure_utc(start_bound) if start_bound is not None else None

    # 自有事件：封存与对照均取不晚于上界的全部事件（apply_window_filter 为
    # False 时不施下界）；开启前的测温/交接是重建“开启时刻保管方”所必需。
    own_items = []
    for e in events:
        at = ensure_utc(e.occurred_at)
        if span_end is not None and at > span_end:
            continue
        if apply_window_filter and lower is not None and at < lower:
            continue
        own_items.append(_own_event_item(e, event_effective))
    own_items.sort(key=lambda x: x["event_id"])

    own_measure_at = {
        ensure_utc(e.occurred_at)
        for e in events
        if getattr(e.event_type, "name", e.event_type) == "measurement"
    }

    # 车载读数：对该货物适用（同车次 + 当前窗口左闭右开在车）且读数时刻
    # 落在范围内；被同刻自有测温取代或被生效探头故障隔离的原始读数同样
    # 封存（adopted=false），以完整保留重建上下文（同刻自有优先 + 故障隔离）。
    reading_items = []
    for r in readings:
        read_at = ensure_utc(r.read_at)
        if lower is not None and read_at < lower:
            continue
        if span_end is not None and read_at > span_end:
            continue
        applicable = any(timeline.is_reading_applicable(l, r) for l in loadings)
        if not applicable:
            continue
        faults = timeline.covering_faults(active_faults, r.trip_id, read_at)
        fault_ids = sorted(f.fault_id for f in faults)
        supplanted = read_at in own_measure_at
        reading_items.append(
            _reading_item(
                r,
                reading_effective,
                adopted=not supplanted and not fault_ids,
                isolated=bool(fault_ids),
                covering_fault_ids=fault_ids,
            )
        )
    reading_items.sort(key=lambda x: (x["read_at"], x["reading_id"]))

    # 装卸窗口：与 [lower, span_end] 相交（unload>下界 且 load<=上界）。
    loading_items = []
    for l in loadings:
        lo, un = ensure_utc(l.loaded_at), ensure_utc(l.unloaded_at)
        if lower is not None and un <= lower:
            continue
        if span_end is not None and lo > span_end:
            continue
        loading_items.append(_loading_item(l))
    loading_items.sort(key=lambda x: (x["trip_id"], x["origin_loaded_at"]))

    return own_items, reading_items, loading_items


# ----------------------------------------------------------- 封存包


def _stance_item(view) -> dict:
    """封存时一条“当前有效”的最新表态（取自责任确认视图）。"""
    return {
        "stance_id": view.stance_id,
        "responsible_party": view.responsible_party,
        "stance": view.kind.value,
        "reason": view.reason,
        "created_at": ts(view.created_at),
        "snapshot_id": view.snapshot_id,
        "segments": _seg_triples(view.segments),
    }


def _interval_dict(ex) -> dict:
    return {
        "interval_id": interval_id(ex),
        "start": ts(ex.start),
        "end": ts(ex.end),
        "is_open": False,
        "start_source": ex.start_source,
        "end_source": ex.end_source,
        "start_event_id": ex.start_event_id,
        "end_event_id": ex.end_event_id,
        "start_reading_id": ex.start_reading_id,
        "end_reading_id": ex.end_reading_id,
        "start_trip_id": ex.start_trip_id,
        "end_trip_id": ex.end_trip_id,
        "snapshot_id": resp.snapshot_id(ex),
        "segments": _seg_triples(ex.segments),
    }


@dataclass(frozen=True)
class SealPayload:
    """封存组装结果：规范化封存包（含摘要）与摘要（落库 / 回执用）。"""

    content: dict
    canonical: str
    content_digest: str
    snapshot_id: str
    sealed_at: str


def build_seal_payload(
    case_id: str,
    cargo,
    ex,
    interval_view,
    events,
    readings,
    loadings,
    effective,
    sealed_at,
    active_faults=None,
) -> SealPayload:
    """在一个一致的已提交视图上组装封存包并计算内容摘要。

    :param ex: 已闭合区间 :class:`app.chain.ExcursionResult`。
    :param interval_view: :meth:`responsibility.build_interval_view` 的视图
        （提供当时各责任方最新有效表态）。
    :param sealed_at: 封存时刻（datetime，统一转 UTC 秒级）。
    :param active_faults: 相关车次的生效探头故障时段（车载读数隔离依据）。
    """
    start, end = ensure_utc(ex.start), ensure_utc(ex.end)
    # 封存自有事件取“不晚于闭合时刻”的全部事件（含开启前完整保管链，
    # apply_window_filter=False 不施下界）；车载读数 / 装卸窗口按区间范围归集。
    own_items, reading_items, loading_items = scoped_evidence(
        events,
        readings,
        loadings,
        effective,
        start,
        end,
        apply_window_filter=False,
        active_faults=active_faults,
    )

    sealed_at_s = ts(sealed_at)
    content = {
        "case_id": case_id,
        "sealed_at": sealed_at_s,
        "cargo": {
            "cargo_id": cargo.cargo_id,
            "started_at": ts(cargo.started_at),
            "temp_upper_limit": temp(cargo.temp_upper_limit),
            "initial_holder": cargo.initial_holder,
        },
        "interval": _interval_dict(ex),
        "current_valid_stances": [
            _stance_item(sv) for sv in interval_view.stances
        ],
        "summary": interval_view.summary,
        "own_events": own_items,
        "vehicle_readings": reading_items,
        "loading_windows": loading_items,
    }
    canonical = canonical_json(content)
    content_with_digest = dict(content)
    content_with_digest["content_digest"] = digest_of(canonical)
    return SealPayload(
        content=content_with_digest,
        canonical=canonical,
        content_digest=content_with_digest["content_digest"],
        snapshot_id=resp.snapshot_id(ex),
        sealed_at=sealed_at_s,
    )


# ----------------------------------------------------------- 现况重建


@dataclass(frozen=True)
class CurrentState:
    """封存区间在当前数据下的重建状态。"""

    status: str
    current_ex: object | None        # 仍闭合时的 ExcursionResult
    open_ex: object | None           # 重开时的开放 ExcursionResult
    current_snapshot_id: str | None
    current_view: object | None      # 仍闭合时的 IntervalView
    cargo_start: object              # 货物起始时间（消失对照的证据下界）
    events: list
    readings: list
    loadings: list
    effective: tuple
    active_faults: list = None


def rebuild_current(db, cargo, interval_key: str) -> CurrentState:
    """按当前数据重建目标区间状态（读事务内调用；不抛 404/422）。"""
    events, loadings, readings, effective, active_faults = (
        timeline.load_cargo_context(db, cargo.cargo_id)
    )
    cargo_start = ensure_utc(cargo.started_at)
    chain_result, closed = resp.rebuild(
        cargo, events, loadings, readings, effective, active_faults
    )
    ex = closed.get(interval_key)
    open_ex = next(
        (e for e in chain_result.excursions if interval_id(e) == interval_key),
        None,
    )

    from app import stance_crud

    if ex is not None:
        rows = stance_crud.load_interval_stances(
            db, cargo.cargo_id, interval_key
        )
        view = resp.build_interval_view(cargo.cargo_id, ex, rows)
        return CurrentState(
            status=STATUS_UNCHANGED,
            current_ex=ex,
            open_ex=None,
            current_snapshot_id=resp.snapshot_id(ex),
            current_view=view,
            cargo_start=cargo_start,
            events=events,
            readings=readings,
            loadings=loadings,
            effective=effective,
            active_faults=active_faults,
        )
    if open_ex is not None:
        return CurrentState(
            status=STATUS_REOPENED,
            current_ex=None,
            open_ex=open_ex,
            current_snapshot_id=None,
            current_view=None,
            cargo_start=cargo_start,
            events=events,
            readings=readings,
            loadings=loadings,
            effective=effective,
            active_faults=active_faults,
        )
    return CurrentState(
        status=STATUS_DISAPPEARED,
        current_ex=None,
        open_ex=None,
        current_snapshot_id=None,
        current_view=None,
        cargo_start=cargo_start,
        events=events,
        readings=readings,
        loadings=loadings,
        effective=effective,
        active_faults=active_faults,
    )


# ----------------------------------------------------------- 现况对照


def _evidence_diff(sealed_items: list[dict], current_items: list[dict], key: str):
    """按键归集的证据项逐项比对：added / removed / changed（含变更字段）。"""
    sealed_map = {item[key]: item for item in sealed_items}
    current_map = {item[key]: item for item in current_items}
    diffs = []
    for k in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(k)
        new = current_map.get(k)
        if old is None:
            diffs.append({"key": k, "change": ITEM_ADDED, "sealed": None, "current": new})
        elif new is None:
            diffs.append({"key": k, "change": ITEM_REMOVED, "sealed": old, "current": None})
        elif old != new:
            changed_fields = sorted({f for f in old if old.get(f) != new.get(f)})
            diffs.append(
                {
                    "key": k,
                    "change": ITEM_CHANGED,
                    "fields": changed_fields,
                    "sealed": old,
                    "current": new,
                }
            )
    return diffs


def _segment_diff(sealed_segments: list[list], current_segments: list[list] | None):
    """责任片段增删变更：以片段起点为稳定键，比对终点与责任方。

    current_segments 为 None（区间重开 / 消失）时，封存片段全部为 removed。
    开放区间当前片段也一并给出（其末段终点为 null）。
    """
    current_segments = current_segments or []
    sealed_map = {s[0]: s for s in sealed_segments}
    current_map = {s[0]: s for s in current_segments}
    diffs = []
    for start in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(start)
        new = current_map.get(start)
        if old is None:
            diffs.append({"start": start, "change": ITEM_ADDED,
                          "sealed": None, "current": new})
        elif new is None:
            diffs.append({"start": start, "change": ITEM_REMOVED,
                          "sealed": old, "current": None})
        elif old != new:
            diffs.append({"start": start, "change": ITEM_CHANGED,
                          "sealed": old, "current": new})
    return diffs


def _current_evidence(state: CurrentState, start_bound, span_end):
    """按封存相同的范围定义归集当前证据。

    自有事件与封存一致：不施下界（不晚于上界的全部事件——含开启前保管链），
    ``start_bound`` 只约束车载读数与装卸窗口。
    """
    return scoped_evidence(
        state.events,
        state.readings,
        state.loadings,
        state.effective,
        start_bound,
        span_end,
        apply_window_filter=False,
        active_faults=state.active_faults,
    )


def _normalize_reading_items(items: list[dict]) -> list[dict]:
    """旧封存包（探头故障功能上线前）的车载读数项补齐隔离字段默认值。

    旧包没有 ``isolated`` / ``covering_fault_ids`` 键；对照现况时统一补为
    false / []，避免旧包在现况无变化时被误判为 changed（原包内容与摘要
    不被改写，仅在比较时归一化）。
    """
    normalized = []
    for item in items:
        new_item = dict(item)
        new_item.setdefault("isolated", False)
        new_item.setdefault("covering_fault_ids", [])
        normalized.append(new_item)
    return normalized


def build_diff(content: dict, state: CurrentState, now_ub) -> dict:
    """封存包内容 vs 当前重建状态的对照结果。

    :param content: 封存时的内容字典（含 interval / 各证据项列表）。
    :param now_ub: 区间重开时的证据范围上界（查询时刻，UTC）；
        区间消失时无上界（开放结束的区间可能因晚到记录而消失，
        取当时全部自有事件 / 在车读数 / 相交窗口比对）。
    """
    sealed_interval = content["interval"]
    sealed_segments = sealed_interval["segments"]
    # 旧封存包缺隔离字段：比较前归一化（不改写原包）
    sealed_reading_items = _normalize_reading_items(
        content["vehicle_readings"]
    )

    if state.status == STATUS_UNCHANGED:
        ex = state.current_ex
        cur_start, cur_end = ensure_utc(ex.start), ensure_utc(ex.end)
        own_now, read_now, load_now = _current_evidence(
            state, cur_start, cur_end
        )
        current_interval = _interval_dict(ex)
        current_segments = current_interval["segments"]
        status = (
            STATUS_UNCHANGED
            if state.current_snapshot_id == sealed_interval["snapshot_id"]
            else STATUS_CHANGED
        )
    elif state.status == STATUS_REOPENED:
        ex = state.open_ex
        cur_start = ensure_utc(ex.start)
        own_now, read_now, load_now = _current_evidence(
            state, cur_start, now_ub
        )
        current_interval = _open_interval_dict(ex)
        current_segments = current_interval["segments"]
        status = STATUS_REOPENED
    else:
        # 区间消失：证据范围下界放宽到货物起始时间（新的开启点 / 窗口更正
        # 可能把适用读数带到封存起点之前），上界不限——晚到记录 / 测温更正 /
        # 窗口更正都可能是区间消失的原因，需逐项列出。
        own_now, read_now, load_now = _current_evidence(
            state, state.cargo_start, None
        )
        current_interval = None
        current_segments = None
        status = STATUS_DISAPPEARED

    return {
        "case_id": content["case_id"],
        "cargo_id": content["cargo"]["cargo_id"],
        "interval_id": sealed_interval["interval_id"],
        "status": status,
        "sealed_snapshot_id": sealed_interval["snapshot_id"],
        "current_snapshot_id": state.current_snapshot_id,
        "sealed_at": content["sealed_at"],
        "compared_at": ts(now_ub),
        "current_interval": current_interval,
        "segment_changes": _segment_diff(sealed_segments, current_segments),
        "evidence_changes": {
            "own_events": _evidence_diff(
                content["own_events"], own_now, "event_id"
            ),
            "vehicle_readings": _evidence_diff(
                sealed_reading_items, read_now, "reading_id"
            ),
            "loading_windows": _loading_window_diff(
                content["loading_windows"], load_now
            ),
        },
    }


def _loading_window_diff(sealed_items: list[dict], current_items: list[dict]):
    """装卸窗口以 (trip_id, origin_loaded_at) 为稳定键比对。"""
    def key_of(item):
        return f"{item['trip_id']}|{item['origin_loaded_at']}"

    sealed_pairs = [(key_of(i), i) for i in sealed_items]
    current_pairs = [(key_of(i), i) for i in current_items]
    sealed_map = dict(sealed_pairs)
    current_map = dict(current_pairs)
    diffs = []
    for k in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(k)
        new = current_map.get(k)
        if old is None:
            diffs.append({"key": k, "change": ITEM_ADDED, "sealed": None, "current": new})
        elif new is None:
            diffs.append({"key": k, "change": ITEM_REMOVED, "sealed": old, "current": None})
        elif old != new:
            changed_fields = sorted({f for f in old if old.get(f) != new.get(f)})
            diffs.append(
                {
                    "key": k,
                    "change": ITEM_CHANGED,
                    "fields": changed_fields,
                    "sealed": old,
                    "current": new,
                }
            )
    return diffs


def _open_interval_dict(ex) -> dict:
    return {
        "interval_id": interval_id(ex),
        "start": ts(ex.start),
        "end": None,
        "is_open": True,
        "start_source": ex.start_source,
        "end_source": None,
        "start_event_id": ex.start_event_id,
        "end_event_id": None,
        "start_reading_id": ex.start_reading_id,
        "end_reading_id": None,
        "start_trip_id": ex.start_trip_id,
        "end_trip_id": None,
        "snapshot_id": None,
        "segments": _seg_triples(ex.segments),
    }


def _parse_sealed_ts(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
