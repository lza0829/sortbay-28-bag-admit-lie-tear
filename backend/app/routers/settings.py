from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db, rows_to_list, write_db
from app.services.admission import POLICY
from app.services.common import audit

router = APIRouter(tags=["设置"])


class SettingIn(BaseModel):
    键: str
    值: str


@router.get("/设置")
def list_settings():
    with db() as conn:
        return rows_to_list(conn.execute("SELECT key AS 键, value AS 值 FROM settings").fetchall())


@router.post("/设置")
def set_setting(body: SettingIn):
    if body.键 == "入袋准入" and body.值 != POLICY:
        raise HTTPException(409, f"本系统已全局选定准入架构：{POLICY}，不得切换为待路由先入袋")
    with write_db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.键, body.值),
        )
        audit(conn, "设置", None, "改设置", f"{body.键}={body.值}")
        return {"键": body.键, "值": body.值}

def _surface_router_settings() -> dict:
    return {"panel": "settings", "ready": True}

