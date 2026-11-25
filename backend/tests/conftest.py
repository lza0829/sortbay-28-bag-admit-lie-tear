from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("httpx")

# Must be set before importing app.config.
_DB = Path(tempfile.gettempdir()) / f"sortbay-pytest-{os.getpid()}.db"
os.environ["SORTBAY_DB"] = str(_DB)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    if _DB.exists():
        _DB.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(_DB) + suffix)
        if side.exists():
            side.unlink()
    seed_if_empty()
    with db() as conn:
        # Remove the smoke scan/events but keep line, shift, chutes and routes.
        conn.execute("DELETE FROM admission_requests")
        conn.execute("DELETE FROM bag_item_events")
        conn.execute("DELETE FROM audit_events")
        conn.execute("DELETE FROM route_events")
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM bags")
        conn.execute("DELETE FROM divert_batches")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('scans','bags','audit_events','admission_requests','bag_item_events','route_events','divert_batches')")
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def chute_id(client):
    rows = client.get("/api/口门").json()
    return next(row["id"] for row in rows if row["code"] == "A01")


@pytest.fixture
def other_chute_id(client):
    rows = client.get("/api/口门").json()
    return next(row["id"] for row in rows if row["code"] == "A02")


@pytest.fixture
def open_bag(client, chute_id):
    response = client.post("/api/封袋/开袋", json={"口门编号": chute_id})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def pending_scan(client):
    response = client.post("/api/扫描", json={"条码": "PB-PENDING-001", "目的地码": "110"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def routed_scan(client, pending_scan):
    response = client.post(f"/api/扫描/{pending_scan['id']}/路由")
    assert response.status_code == 200, response.text
    return response.json()
