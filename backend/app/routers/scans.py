from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import db, row_to_dict, rows_to_list, write_db
from app.services.admission import POLICY, POLICY_CODE, admission_predicate, bag_consistency
from app.services.common import audit, current_shift, now_str, require_unsealed

router = APIRouter(tags=["过机扫描"])


class ScanIn(BaseModel):
    条码: str = Field(min_length=1)
    目的地码: str = ""
    分拣线编号: int | None = None
    备注: str = ""


class AdjudicateIn(BaseModel):
    扫描编号: int


@router.get("/扫描")
def list_scans(状态: str | None = None):
    sql = """
        SELECT s.*, c.code AS 口门码, b.code AS 袋码
        FROM scans s
        LEFT JOIN chutes c ON c.id = s.chute_id
        LEFT JOIN bags b ON b.id = s.bag_id
    """
    with db() as conn:
        if 状态:
            rows = conn.execute(
                sql + " WHERE s.status=? ORDER BY s.id DESC LIMIT 200", (状态,)
            ).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY s.id DESC LIMIT 200").fetchall()
        return rows_to_list(rows)


@router.get("/扫描/{scan_id}/一致性")
def scan_consistency(scan_id: int, bag_id: int | None = None):
    with db() as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        target_bag_id = bag_id if bag_id is not None else scan["bag_id"]
        bag = conn.execute("SELECT * FROM bags WHERE id=?", (target_bag_id,)).fetchone() if target_bag_id else None
        predicate = admission_predicate(scan, bag) if bag else (400, "BAG_REQUIRED", "请指定集包袋")
        return {
            "扫描编号": scan_id,
            "bag_id": scan["bag_id"],
            "目标袋编号": target_bag_id,
            "chute_id": scan["chute_id"],
            "status": scan["status"],
            "扫描状态": scan["status"],
            "可入袋": predicate is None,
            "入袋谓词通过": predicate is None,
            "谓词拒绝": {"http_status": predicate[0], "原因码": predicate[1], "message": predicate[2]} if predicate else None,
            "袋一致性": bag_consistency(conn, scan["bag_id"]) if scan["bag_id"] else None,
            "政策": POLICY,
            "政策代码": POLICY_CODE,
        }


@router.post("/扫描")
def create_scan(body: ScanIn):
    with write_db() as conn:
        require_unsealed(conn)
        shift = current_shift(conn)
        line = None
        if body.分拣线编号:
            line = conn.execute("SELECT * FROM lines WHERE id=?", (body.分拣线编号,)).fetchone()
        else:
            line = conn.execute("SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not line:
            raise HTTPException(400, "无可用分拣线")
        if not line["active"]:
            raise HTTPException(400, "分拣线已停用")
        dup = conn.execute(
            "SELECT id FROM scans WHERE barcode=? AND shift_id=?",
            (body.条码, shift["id"]),
        ).fetchone()
        if dup:
            raise HTTPException(400, "本班次条码已过机")
        conn.execute(
            """
            INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)
            VALUES (?, ?, ?, ?, '待路由', ?, ?)
            """,
            (body.条码, body.目的地码, shift["id"], line["id"], now_str(), body.备注),
        )
        row = conn.execute(
            """
            SELECT s.*, c.code AS 口门码, b.code AS 袋码
            FROM scans s
            LEFT JOIN chutes c ON c.id=s.chute_id
            LEFT JOIN bags b ON b.id=s.bag_id
            WHERE s.barcode=? AND s.shift_id=?
            """,
            (body.条码, shift["id"]),
        ).fetchone()
        audit(conn, "扫描", row["id"], "过机装载", body.条码)
        result = row_to_dict(row)
        result["入袋政策"] = POLICY
        result["政策代码"] = POLICY_CODE
        return result


@router.post("/扫描/{scan_id}/路由")
@router.post("/扫描/{scan_id}/裁决")
@router.post("/扫描/{scan_id}/后补裁决")
def route_scan(scan_id: int):
    with write_db() as conn:
        require_unsealed(conn)
        shift = current_shift(conn)
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        if scan["shift_id"] != shift["id"]:
            raise HTTPException(400, "扫描不属于当前未封班次")
        if scan["bag_id"] is not None and scan["status"] == "待路由":
            raise HTTPException(409, "扫描状态与袋册拧花：待路由却已挂袋，请先执行入袋一致性回滚/隔离")
        if scan["status"] not in ("待路由", "错分回流"):
            raise HTTPException(400, f"当前状态不可路由: {scan['status']}")
        if not scan["dest_code"]:
            raise HTTPException(400, "缺少目的地码")
        rule = conn.execute(
            "SELECT * FROM route_rules WHERE dest_code=?", (scan["dest_code"],)
        ).fetchone()
        if not rule:
            raise HTTPException(400, "无匹配路由")
        chute = conn.execute("SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)).fetchone()
        if not chute:
            raise HTTPException(400, "口门不存在")
        if chute["status"] != "口门开":
            raise HTTPException(400, "口门已闭，禁止落入")

        # Conditional predicate prevents a concurrent 入袋/裁决 from producing two terminal states.
        cur = conn.execute(
            """
            UPDATE scans
            SET status='已路由', chute_id=?, version=COALESCE(version, 0)+1
            WHERE id=?
              AND status IN ('待路由', '错分回流')
              AND bag_id IS NULL
            """,
            (chute["id"], scan_id),
        )
        if cur.rowcount != 1:
            fresh = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            raise HTTPException(409, f"路由与入袋并发冲突，请刷新；当前状态: {fresh['status'] if fresh else '不存在'}")
        conn.execute(
            "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
            (scan_id, chute["id"], "路由落入", now_str(), chute["code"]),
        )
        audit(conn, "扫描", scan_id, "路由裁决", chute["code"], result="已路由")
        row = conn.execute(
            """
            SELECT s.*, c.code AS 口门码, b.code AS 袋码
            FROM scans s
            LEFT JOIN chutes c ON c.id=s.chute_id
            LEFT JOIN bags b ON b.id=s.bag_id
            WHERE s.id=?
            """,
            (scan_id,),
        ).fetchone()
        return row_to_dict(row)


@router.post("/后补裁决")
def adjudicate_by_body(body: AdjudicateIn):
    return route_scan(body.扫描编号)


@router.post("/裁决")
def adjudicate_by_body_alias(body: AdjudicateIn):
    return route_scan(body.扫描编号)

