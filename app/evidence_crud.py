"""责任证据封存的写入与查询事务。

封存于单事务内完成，并与同货物的事件写入、车载读数写入、责任表态、测温
更正、装卸登记 / 更正共用同一把**货物行锁**（``SELECT ... FOR UPDATE``），
因此封存组装所用的重建视图一定是持锁期间的最新已提交状态——“一致的已
提交视图”。封存失败（区间消失 / 重开 / 快照已变 / 编号冲突）一律回滚，
不留任何封存记录。

案件编号幂等 / 冲突
-------------------
* 同 ``case_id`` + 同请求（货物编号、区间标识、当前快照三元组一致）重试：
  **即使现况已变**（区间消失 / 重开 / 快照变化）也返回**原封存包**
  （``replayed=true``）——封存成功后其裁决不再依赖当前数据；
* 同 ``case_id`` + 异请求 → ``409 case_id_conflict``，原封存不变。

封存行 append-only：任何接口都不修改 / 不删除已封存内容；晚到记录与更正
只影响 :mod:`app.sealing` 的现况对照结果。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, OperationalError

from app import sealing
from app.crud import ApiError, _load_cargo_locked
from app.models import EvidenceSeal
from app.schemas import EvidenceSealCreate
from app.timeutil import ensure_utc


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _not_found(message: str) -> ApiError:
    return ApiError(404, "seal_not_found", message)


@dataclass
class SealWriteResult:
    row: EvidenceSeal
    content: dict
    replayed: bool


def _row_content(row: EvidenceSeal) -> dict:
    return json.loads(row.content)


def _same_request(row: EvidenceSeal, cargo_id: str, interval_key: str, snapshot_id: str) -> bool:
    return row.request_hash == sealing.request_fingerprint(
        cargo_id, interval_key, snapshot_id
    )


def create_seal(db, payload: EvidenceSealCreate) -> SealWriteResult:
    try:
        return _create_seal_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _create_seal_tx(db, payload)


def _replay(row: EvidenceSeal) -> SealWriteResult:
    return SealWriteResult(row=row, content=_row_content(row), replayed=True)


def _create_seal_tx(db, payload: EvidenceSealCreate) -> SealWriteResult:
    case_id = payload.case_id
    cargo_id = payload.cargo_id
    interval_key = payload.interval_id
    snapshot_id = payload.snapshot_id
    request_hash = sealing.request_fingerprint(cargo_id, interval_key, snapshot_id)

    # 幂等 / 冲突快路径（先读，无需持锁；货物不存在仍按 404 裁决）。
    # 注意：封存成功后重试即使现况已变也必须返回原包，故此处不校验现况。
    existing = db.get(EvidenceSeal, case_id)
    if existing is not None:
        if _same_request(existing, cargo_id, interval_key, snapshot_id):
            db.rollback()  # 只读裁决，结束事务
            return _replay(existing)
        raise _conflict(
            f"案件编号 {case_id} 已封存不同请求的证据包"
            f"（既存：货物 {existing.cargo_id} / 区间 {existing.interval_key} / "
            f"快照 {existing.snapshot_id}），拒绝覆盖",
            code="case_id_conflict",
        )

    try:
        # 与事件 / 读数 / 表态 / 更正 / 装卸写入串行化：锁货物行
        cargo = _load_cargo_locked(db, cargo_id)

        # 持锁后复核幂等（并发首传竞争在此分出唯一赢家）
        winner = db.get(EvidenceSeal, case_id)
        if winner is not None:
            if _same_request(winner, cargo_id, interval_key, snapshot_id):
                db.rollback()
                return _replay(winner)
            raise _conflict(
                f"案件编号 {case_id} 已封存不同请求的证据包，拒绝覆盖",
                code="case_id_conflict",
            )

        # 在持锁期间的最新视图上定位区间（不存在 404 / 开放 422，复用表态语义）
        from app.stance_crud import _resolve_interval
        from app import stance_crud

        _, ex = _resolve_interval(db, cargo, interval_key)

        # 快照校验：必须封存当前快照；快照已变即拒绝（不留记录）
        current_snapshot = sealing.resp.snapshot_id(ex)
        if snapshot_id != current_snapshot:
            raise _conflict(
                f"快照已变化：请求快照 {snapshot_id} 与区间 {interval_key} "
                f"当前快照 {current_snapshot} 不一致（区间边界或责任片段可能已"
                "因晚到记录 / 更正变化），拒绝封存；请按当前快照重新发起",
                code="stale_snapshot",
            )

        # 一致视图上组装证据（context 与定位区间时同源；同事务同锁，无并发写入）
        from app.timeline import load_cargo_context
        from app import responsibility as resp_mod

        events, loadings, readings, effective = load_cargo_context(
            db, cargo_id
        )
        rows = stance_crud.load_interval_stances(db, cargo_id, interval_key)
        view = resp_mod.build_interval_view(cargo_id, ex, rows)

        sealed_at = datetime.now(timezone.utc)
        payload_seal = sealing.build_seal_payload(
            case_id,
            cargo,
            ex,
            view,
            events,
            readings,
            loadings,
            effective,
            sealed_at,
        )

        row = EvidenceSeal(
            case_id=case_id,
            cargo_id=cargo_id,
            interval_key=interval_key,
            snapshot_id=current_snapshot,
            request_hash=request_hash,
            content=payload_seal.canonical,
            content_digest=payload_seal.content_digest,
            sealed_at=ensure_utc(sealed_at),
        )
        db.add(row)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        # SQLite 下 FOR UPDATE 为空操作：并发同 case_id 首传可能以唯一约束
        # 分出赢家——回滚后重查，同请求按幂等返回，异请求按冲突拒绝。
        winner2 = db.get(EvidenceSeal, case_id)
        if winner2 is not None:
            if _same_request(winner2, cargo_id, interval_key, snapshot_id):
                return _replay(winner2)
            raise _conflict(
                f"案件编号 {case_id} 已封存不同请求的证据包，拒绝覆盖",
                code="case_id_conflict",
            )
        raise _conflict(
            f"封存 {case_id} 与既有数据冲突（案件编号冲突 / 字段约束）",
            code="integrity_conflict",
        )

    return SealWriteResult(row=row, content=_row_content(row), replayed=False)


# ----------------------------------------------------------- 查询


def get_seal(db, case_id: str) -> SealWriteResult:
    row = db.get(EvidenceSeal, case_id)
    if row is None:
        raise _not_found(f"案件编号 {case_id} 的封存证据包不存在")
    return SealWriteResult(row=row, content=_row_content(row), replayed=False)


def build_seal_diff(db, case_id: str, now: datetime | None = None) -> dict:
    """读取封存包并按当前数据做现况对照（只读，不改写任何内容）。"""
    result = get_seal(db, case_id)
    content = result.content

    from app.models import Cargo

    cargo = db.get(Cargo, content["cargo"]["cargo_id"])
    if cargo is None:
        # 外键 RESTRICT 下不会发生（货物不可在有封存时删除）；防御性处理
        raise ApiError(
            404, "cargo_not_found",
            f"封存货物 {content['cargo']['cargo_id']} 不存在",
        )
    cargo.started_at = ensure_utc(cargo.started_at)

    interval_key = content["interval"]["interval_id"]
    state = sealing.rebuild_current(db, cargo, interval_key)

    now_ub = ensure_utc(now or datetime.now(timezone.utc))
    return sealing.build_diff(content, state, now_ub)
