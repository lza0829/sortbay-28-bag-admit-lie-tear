from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import db, row_to_dict, rows_to_list, write_db
from app.services.admission import (
    POLICY,
    POLICY_CODE,
    admit_scan,
    bag_consistency,
    consistency_report,
    get_admission,
    policy_info,
    replay_admission,
)
from app.services.common import audit, current_shift, now_str, require_unsealed

router = APIRouter(tags=["封袋"])


class BagOpenIn(BaseModel):
    口门编号: int
    备注: str = ""


class BagAddIn(BaseModel):
    扫描编号: int
    准入键: str | None = None
    幂等键: str | None = None
    idempotency_key: str | None = None


@router.get("/封袋/政策")
def bag_policy():
    return policy_info()


@router.get("/封袋/一致性")
def bags_consistency(shift_id: int | None = None):
    with db() as conn:
        return consistency_report(conn, shift_id)


@router.get("/封袋/准入/{admission_id}")
def admission_outcome(admission_id: int):
    with db() as conn:
        result = get_admission(conn, admission_id)
        if not result:
            raise HTTPException(404, "准入结局不存在")
        return result


@router.post("/封袋/准入/{admission_id}/重放")
def replay_admission_outcome(admission_id: int):
    with write_db() as conn:
        result = replay_admission(conn, admission_id, increment=True)
        if not result:
            raise HTTPException(404, "准入结局不存在")
        return result


@router.get("/admissions/{admission_id}/replay")
def replay_admission_alias(admission_id: int):
    with db() as conn:
        result = replay_admission(conn, admission_id, increment=False)
        if not result:
            raise HTTPException(404, "准入结局不存在")
        return result


@router.get("/袋册")
def bag_books():
    with db() as conn:
        bags = rows_to_list(conn.execute(
            """
            SELECT b.*, c.code AS 口门码,
              (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            ORDER BY b.id DESC LIMIT 100
            """
        ).fetchall())
        for bag in bags:
            bag["一致性"] = bag_consistency(conn, bag["id"])
            bag["册页"] = rows_to_list(conn.execute(
                "SELECT id, barcode, dest_code, status, chute_id, bag_id FROM scans WHERE bag_id=? ORDER BY id",
                (bag["id"],),
            ).fetchall())
        return bags


@router.get("/封袋/{bag_id}")
def get_bag(bag_id: int):
    with db() as conn:
        bag = conn.execute(
            """
            SELECT b.*, c.code AS 口门码,
              (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            WHERE b.id=?
            """,
            (bag_id,),
        ).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        result = row_to_dict(bag)
        result["一致性"] = bag_consistency(conn, bag_id)
        result["一致"] = result["一致性"]["一致"]
        return result


@router.post("/封袋/{bag_id}/校验")
def check_bag(bag_id: int):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM bags WHERE id=?", (bag_id,)).fetchone():
            raise HTTPException(404, "袋不存在")
        return bag_consistency(conn, bag_id)


@router.post("/封袋/{bag_id}/回滚异常")
def rollback_abnormal_bag_links(bag_id: int):
    with write_db() as conn:
        require_unsealed(conn)
        bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        if bag["status"] != "集包中":
            raise HTTPException(409, "已封袋异常属于单袋封确认边界，不在入袋准入回滚口处理")

        scans = conn.execute("SELECT * FROM scans WHERE bag_id=? ORDER BY id", (bag_id,)).fetchall()
        repaired: list[dict] = []
        blocked: list[dict] = []
        for scan in scans:
            chute_bad = scan["chute_id"] is None or scan["chute_id"] != bag["chute_id"]
            status_bad = scan["status"] != "已入袋"
            if not chute_bad and not status_bad:
                continue
            if scan["status"] == "已封袋":
                blocked.append({"scan_id": scan["id"], "reason": "扫描已封袋，需走封袋确认/解封流程"})
                continue

            # 口门不一致但已到“已入袋”：撤回到已路由，保留裁决口门，便于重新入正确袋。
            new_status = scan["status"]
            if scan["status"] == "已入袋" and chute_bad:
                new_status = "已路由"
            conn.execute(
                "UPDATE scans SET bag_id=NULL, status=?, version=COALESCE(version,0)+1 WHERE id=?",
                (new_status, scan["id"]),
            )
            conn.execute(
                """
                INSERT INTO bag_item_events(bag_id, scan_id, action, created_at, note)
                VALUES (?, ?, '异常回滚出袋', ?, ?)
                """,
                (bag_id, scan["id"], now_str(), "入袋准入一致性回滚"),
            )
            audit(
                conn, "封袋", bag_id, "入袋异常回滚",
                f"扫描{scan['id']} {scan['status']}->{new_status}",
                result="回滚",
            )
            repaired.append({"scan_id": scan["id"], "from_status": scan["status"], "to_status": new_status})

        report = bag_consistency(conn, bag_id)
        return {"袋编号": bag_id, "已回滚": repaired, "阻断": blocked, "一致性": report}


@router.get("/封袋/{bag_id}/册")
def bag_book(bag_id: int):
    with db() as conn:
        bag = conn.execute(
            """
            SELECT b.*, c.code AS 口门码
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            WHERE b.id=?
            """,
            (bag_id,),
        ).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        rows = rows_to_list(conn.execute(
            """
            SELECT s.*, c.code AS 口门码
            FROM scans s LEFT JOIN chutes c ON c.id=s.chute_id
            WHERE s.bag_id=?
            ORDER BY s.id
            """,
            (bag_id,),
        ).fetchall())
        events = rows_to_list(conn.execute(
            "SELECT * FROM bag_item_events WHERE bag_id=? ORDER BY id DESC LIMIT 200",
            (bag_id,),
        ).fetchall())
        consistency = bag_consistency(conn, bag_id)
        return {
            **row_to_dict(bag),
            "件数": len(rows),
            "册页件数": len(rows),
            "件": rows,
            "册页": rows,
            "事件": events,
            "一致性": consistency,
        }


@router.get("/封袋")
def list_bags():
    with db() as conn:
        bags = rows_to_list(conn.execute(
            """
            SELECT b.*, c.code AS 口门码,
              (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            ORDER BY b.id DESC LIMIT 100
            """
        ).fetchall())
        for bag in bags:
            report = bag_consistency(conn, bag["id"])
            bag["一致"] = report["一致"]
            bag["口门不一致数"] = report["口门不一致数"]
            bag["状态不一致数"] = report["状态不一致数"]
            bag["册页件数"] = report["册页件数"]
        return bags


@router.post("/封袋/开袋")
def open_bag(body: BagOpenIn):
    with write_db() as conn:
        require_unsealed(conn)
        shift = current_shift(conn)
        chute = conn.execute("SELECT * FROM chutes WHERE id=?", (body.口门编号,)).fetchone()
        if not chute:
            raise HTTPException(404, "口门不存在")
        stamp = now_str().replace("-", "").replace(":", "").replace(" ", "")
        code = f"袋-{chute['code']}-{shift['id']}-{stamp[-8:]}-{uuid.uuid4().hex[:6].upper()}"
        conn.execute(
            "INSERT INTO bags(shift_id, chute_id, code, status, note) VALUES (?, ?, ?, '集包中', ?)",
            (shift["id"], body.口门编号, code, body.备注),
        )
        row = conn.execute("SELECT * FROM bags WHERE code=?", (code,)).fetchone()
        audit(conn, "封袋", row["id"], "开袋", code)
        return row_to_dict(row)


@router.post("/封袋/{bag_id}/入袋")
def add_to_bag(
    bag_id: int,
    body: BagAddIn,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    x_admission_key: str | None = Header(default=None, alias="X-Admission-Key"),
):
    idem_key = (
        body.准入键
        or body.幂等键
        or body.idempotency_key
        or idempotency_key_header
        or x_idempotency_key
        or x_admission_key
    )
    with write_db() as conn:
        # 先做班次写口闸门；后续所有状态、袋册、审计和结局在同一 IMMEDIATE 事务。
        require_unsealed(conn)
        shift = current_shift(conn)
        result = admit_scan(
            conn,
            bag_id=bag_id,
            scan_id=body.扫描编号,
            idem_key=idem_key,
            request_data=body.model_dump(),
            audit_func=audit,
            expected_shift_id=shift["id"],
        )
        response = result["response"]
        response.setdefault("政策", POLICY)
        response.setdefault("政策代码", POLICY_CODE)
        json_response = JSONResponse(status_code=result["http_status"], content=response)
        if response.get("准入编号") is not None:
            json_response.headers["X-Admission-Id"] = str(response["准入编号"])
        if response.get("重放"):
            json_response.headers["X-Admission-Replay"] = "1"
        return json_response


@router.post("/封袋/{bag_id}/封口")
def seal_bag(bag_id: int):
    with write_db() as conn:
        require_unsealed(conn)
        bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        if bag["status"] != "集包中":
            raise HTTPException(400, "袋状态不可封口")
        consistency = bag_consistency(conn, bag_id)
        if not consistency["一致"]:
            raise HTTPException(409, {
                "detail": "袋册与扫描/口门不一致，禁止封口",
                "一致性": consistency,
            })
        n = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE bag_id=?", (bag_id,)
        ).fetchone()["c"]
        if n == 0:
            raise HTTPException(400, "空袋不可封口")
        cur = conn.execute(
            "UPDATE bags SET status='已封袋', sealed_at=? WHERE id=? AND status='集包中'",
            (now_str(), bag_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "袋状态已被并发事务改变")
        conn.execute(
            "UPDATE scans SET status='已封袋', version=COALESCE(version, 0)+1 WHERE bag_id=? AND status='已入袋'",
            (bag_id,),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE bag_id=? AND status!='已封袋'",
            (bag_id,),
        ).fetchone()["c"]
        if remaining:

            raise HTTPException(409, "封口半截：仍有袋内扫描未转为已封袋，已回滚")
        audit(conn, "封袋", bag_id, "封口", f"件数{n}")
        return row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())

def _surface_router_bags() -> dict:
    return {"panel": "bags", "ready": True}

