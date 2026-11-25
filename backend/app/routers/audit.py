from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.db import db, rows_to_list
from app.services.admission import get_admission, replay_admission

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit(limit: int = 200):
    limit = max(1, min(limit, 1000))
    with db() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall())


@router.get("/审计/准入/{admission_id}")
def audit_admission(admission_id: int):
    with db() as conn:
        result = get_admission(conn, admission_id)
        if not result:
            raise HTTPException(404, "准入结局不存在")
        events = rows_to_list(conn.execute(
            "SELECT * FROM audit_events WHERE admission_id=? ORDER BY id",
            (admission_id,),
        ).fetchall())
        return {"准入": result, "审计事件": events}


@router.post("/审计/重放/{admission_id}")
def audit_replay(admission_id: int):
    with db() as conn:
        result = replay_admission(conn, admission_id, increment=False)
        if not result:
            raise HTTPException(404, "准入结局不存在")
        return result

