"""车次探头故障：故障 / 撤销的读取与隔离判定。

探头被证实失准后，质控人员可对车次登记**左闭右开**的故障时段并可凭期望
版本撤销（写入事务见 :mod:`app.probe_fault_crud`）。登记与撤销均 append-only
留痕（``probe_fault`` / ``probe_fault_revocation`` 两张表），本模块只负责
读取“生效故障”与隔离判定等纯逻辑。

隔离规则
--------
* 故障与**车次**绑定、与装卸时段无关：对某票货物而言，一笔车载读数被隔离
  当且仅当该读数所属车次存在一条**生效故障**（未撤销），且读数时刻落在
  故障时段 ``[fault_start, fault_end)`` 内，并且读数时刻该货物仍在车上
  （左闭右开，合并时间线已保证只有适用读数进入判定）；
* 任一生效故障覆盖即隔离（多条重叠故障时覆盖故障编号全部列出）；
* 重叠故障撤销一条后，另一条仍覆盖的读数**继续隔离**；撤销最后一条覆盖
  故障后，读数恢复按**当前有效温度**参与重建（同刻自有测温仍优先）；
* 晚到读数同样隔离：隔离在重建 / 查询时按当时的生效故障即时判定，与读数
  写入先后无关——故障登记之后补录的旧时刻读数，只要落在生效故障时段内
  即不参与重建。
"""
from __future__ import annotations

from app.timeutil import ensure_utc


def fault_is_active(row) -> bool:
    """故障是否生效（未撤销；撤销行 append-only，存在撤销即不生效）。"""
    return getattr(row, "revocation", None) is None


def fault_window(row):
    """故障行的左闭右开窗口（UTC）：``[fault_start, fault_end)``。"""
    return ensure_utc(row.fault_start), ensure_utc(row.fault_end)


def faults_covering(active_faults, at) -> tuple[str, ...]:
    """时刻 at 被哪些生效故障覆盖（左闭右开），按故障编号升序。"""
    at = ensure_utc(at)
    return tuple(
        sorted(
            f.fault_id
            for f in active_faults
            if fault_window(f)[0] <= at < fault_window(f)[1]
        )
    )


def reading_quarantine_map(readings, active_faults_by_trip: dict) -> dict:
    """车载读数ID -> 覆盖该读数时刻的生效故障编号元组（仅含被覆盖的读数）。

    :param readings: 车载读数 ORM 行（可为多车次）。
    :param active_faults_by_trip: 车次编号 -> 该车次的生效故障行列表。
    """
    mapping: dict[str, tuple[str, ...]] = {}
    for r in readings:
        faults = active_faults_by_trip.get(r.trip_id) or []
        covers = faults_covering(faults, ensure_utc(r.read_at))
        if covers:
            mapping[r.reading_id] = covers
    return mapping


def load_active_faults(db, trip_ids) -> dict:
    """车次编号集合 -> 该车次的**生效**（未撤销）故障行列表。

    撤销行与故障行为一对一，经 outerjoin 后在内存中过滤；故障窗口在返回前
    统一归一化到 UTC。
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import ProbeFault

    trip_ids = set(trip_ids)
    result: dict[str, list] = {tid: [] for tid in trip_ids}
    if not trip_ids:
        return result
    rows = list(
        db.scalars(
            select(ProbeFault)
            .where(ProbeFault.trip_id.in_(sorted(trip_ids)))
            .options(selectinload(ProbeFault.revocation))
            .order_by(ProbeFault.fault_start, ProbeFault.fault_id)
        ).all()
    )
    for row in rows:
        row.fault_start = ensure_utc(row.fault_start)
        row.fault_end = ensure_utc(row.fault_end)
        if fault_is_active(row):
            result.setdefault(row.trip_id, []).append(row)
    return result


def load_trip_faults(db, trip_id: str) -> list:
    """一个车次的全部故障（含已撤销），按登记时刻 / 编号升序。"""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import ProbeFault

    rows = list(
        db.scalars(
            select(ProbeFault)
            .where(ProbeFault.trip_id == trip_id)
            .options(selectinload(ProbeFault.revocation))
            .order_by(ProbeFault.fault_start, ProbeFault.fault_id)
        ).all()
    )
    for row in rows:
        row.fault_start = ensure_utc(row.fault_start)
        row.fault_end = ensure_utc(row.fault_end)
    return rows


def load_fault(db, fault_id: str):
    """按主键取故障行（含撤销关系）。"""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import ProbeFault

    return db.scalars(
        select(ProbeFault)
        .where(ProbeFault.fault_id == fault_id)
        .options(selectinload(ProbeFault.revocation))
    ).first()


def load_revocation(db, revocation_id: str):
    """按主键取撤销行。"""
    from app.models import ProbeFaultRevocation

    return db.get(ProbeFaultRevocation, revocation_id)
