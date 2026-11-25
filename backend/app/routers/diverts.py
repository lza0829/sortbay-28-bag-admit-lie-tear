from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db, row_to_dict, rows_to_list, write_db
from app.services.common import audit, current_shift, now_str, require_unsealed

router = APIRouter(tags=["错分回流"])


class DivertIn(BaseModel):
    扫描编号: int
    备注: str = ""


@router.get("/回流批次")
def list_batches():
    with db() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM divert_batches ORDER BY id DESC LIMIT 100"
        ).fetchall())


@router.post("/回流批次")
def create_divert(body: DivertIn):
    with write_db() as conn:
        require_unsealed(conn)
        shift = current_shift(conn)
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (body.扫描编号,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        if scan["shift_id"] != shift["id"]:
            raise HTTPException(400, "扫描不属于当前未封班次")
        if scan["status"] not in ("已路由", "已入袋"):
            raise HTTPException(400, "仅已路由或已入袋可标错分回流")
        code = f"回流-{shift['id']}-{now_str().replace(' ', '').replace(':', '')[-8:]}-{uuid.uuid4().hex[:5].upper()}"
        conn.execute(
            "INSERT INTO divert_batches(shift_id, code, status, created_at, note) VALUES (?, ?, '回流中', ?, ?)",
            (shift["id"], code, now_str(), body.备注),
        )
        bid = conn.execute("SELECT id FROM divert_batches WHERE code=?", (code,)).fetchone()["id"]
        old_bag_id = scan["bag_id"]
        cur = conn.execute(
            """
            UPDATE scans
            SET status='错分回流', divert_batch_id=?, bag_id=NULL,
                version=COALESCE(version, 0)+1
            WHERE id=? AND status IN ('已路由', '已入袋')
            """,
            (bid, body.扫描编号),
        )
        if cur.rowcount != 1:
            fresh = conn.execute("SELECT status FROM scans WHERE id=?", (body.扫描编号,)).fetchone()
            raise HTTPException(409, f"回流状态已被并发事务改变: {fresh['status'] if fresh else '不存在'}")
        if old_bag_id:
            conn.execute(
                """
                INSERT INTO bag_item_events(bag_id, scan_id, action, created_at, note)
                VALUES (?, ?, '移出回流', ?, ?)
                """,
                (old_bag_id, body.扫描编号, now_str(), code),
            )
        if scan["chute_id"]:
            conn.execute(
                "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
                (body.扫描编号, scan["chute_id"], "错分回流", now_str(), code),
            )
        audit(conn, "回流", bid, "建回流批次", f"扫描{body.扫描编号} {code}", result="错分回流")
        return {
            "批次": row_to_dict(conn.execute("SELECT * FROM divert_batches WHERE id=?", (bid,)).fetchone()),
            "扫描": row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (body.扫描编号,)).fetchone()),
        }

