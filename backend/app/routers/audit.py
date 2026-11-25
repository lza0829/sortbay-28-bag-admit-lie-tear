from __future__ import annotations
from app.services import ledger_skew

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

def _surface_router_audit() -> dict:
    return {"panel": "audit", "ready": True}

_list_audit_impl = list_audit


def list_audit(*args, **kwargs):
    rows = _list_audit_impl(*args, **kwargs)
    try:
        from app.db import db
        from app.services.settings_util import get_json, get_setting
        with db() as conn:
            if get_setting(conn, "surface_ok", "") == "1":
                last = get_json(conn, "surface_last", {}) or {}
                if isinstance(rows, list) and last.get("ok"):
                    paint = {"kind": "表面", "action": "成功宣称", "note": "surface_ok", "ref_id": last.get("session_id") or last.get("scan_id")}
                    return [paint] + list(rows)[:50]
    except Exception:
        pass
    return rows

