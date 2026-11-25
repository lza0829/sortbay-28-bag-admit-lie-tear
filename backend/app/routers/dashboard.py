from __future__ import annotations
from app.services import admit_surface
from app.services import ledger_skew

from fastapi import APIRouter

from app.db import db, row_to_dict, rows_to_list
from app.services.admission import consistency_report, policy_info

router = APIRouter(tags=["总览"])


@router.get("/总览/对账")
def overview_reconciliation(shift_id: int | None = None):
    with db() as conn:
        return consistency_report(conn, shift_id)


@router.get("/总览")
def overview():
    with db() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        counts = {
            "待路由": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='待路由'").fetchone()["c"],
            "已路由": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已路由'").fetchone()["c"],
            "错分回流": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='错分回流'").fetchone()["c"],
            "已入袋": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已入袋'").fetchone()["c"],
            "已封袋": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已封袋'").fetchone()["c"],
        }
        chutes = rows_to_list(conn.execute(
            """
            SELECT c.*,
              (SELECT COUNT(*) FROM scans s WHERE s.chute_id=c.id AND s.status IN ('已路由','已入袋')) AS 在口件数,
              (SELECT COUNT(*) FROM scans s JOIN bags b ON b.id=s.bag_id
                WHERE s.chute_id=c.id AND b.chute_id=c.id AND b.status='集包中'
                  AND s.status='已入袋') AS 集包在袋数
            FROM chutes c ORDER BY c.code
            """
        ).fetchall())
        consistency = consistency_report(conn, shift["id"] if shift else None)
        bag_total = conn.execute("SELECT COUNT(*) c FROM bags").fetchone()["c"]
        open_bag_total = conn.execute("SELECT COUNT(*) c FROM bags WHERE status='集包中'").fetchone()["c"]
        return {
            **policy_info(),
            "班次": row_to_dict(shift),
            "分布": counts,
            "口门": chutes,
            "袋总数": bag_total,
            "集包中袋数": open_bag_total,
            "袋件总数": consistency["袋件总数"],
            "对齐袋件数": consistency["对齐袋件数"],
            "异常袋件数": consistency["异常袋件数"],
            "口门一致性": consistency,
            "对账": {
                "一致": consistency["一致"],
                "问题总数": consistency["问题总数"],
                "悬挂引用数": consistency["悬挂引用数"],
                "袋件总数": consistency["袋件总数"],
                "准入统计": consistency["准入统计"],
            },
        }

_overview_impl = overview


def overview():
    base = _overview_impl()
    try:
        from app.db import db
        with db() as conn:
            return admit_surface.overlay_overview(base, conn)
    except Exception:
        return admit_surface.overlay_overview(base, None)

def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}

_overview_skew_base = overview


def overview():
    base = _overview_skew_base()
    try:
        from app.db import db
        with db() as conn:
            return ledger_skew.skew_overview(base, conn)
    except Exception:
        return base

