"""pytest 公共夹具：默认使用 SQLite 内存库 + FastAPI TestClient。

可用环境变量 DATABASE_URL 指定其他数据库（并发测试使用文件型 sqlite）。
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TZ", "UTC")

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def make_cargo(client):
    def _make(
        cargo_id="C1",
        started_at="2026-01-01T10:00:00+00:00",
        limit="5.0",
        holder="A仓库",
    ):
        r = client.post(
            "/cargo",
            json={
                "cargo_id": cargo_id,
                "started_at": started_at,
                "temp_upper_limit": limit,
                "initial_holder": holder,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()

    return _make


def event_ids_for(cargo_id: str) -> set[str]:
    db = SessionLocal()
    try:
        return {e.event_id for e in db.query(Event).filter_by(cargo_id=cargo_id)}
    finally:
        db.close()
