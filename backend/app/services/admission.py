from __future__ import annotations
from app.services import admit_surface

import json
from typing import Any

from app.db import row_to_dict, rows_to_list
from app.services.common import audit, now_str

# 全局唯一准入架构：仅已路由件可入袋。
# 不实现“待路由先入袋 + 待补裁决”分支，避免两种口径混用。
POLICY_CODE = "ROUTED_ONLY"
POLICY = "仅已路由可入袋"
POLICY_DETAIL = "非已路由则入袋整笔失败；袋件数、扫描态与口门在口均不变；失败准入不进入袋册读路径。"

REASON_NOT_ROUTED = "SCAN_NOT_ROUTED"
REASON_CHUTE_MISMATCH = "CHUTE_MISMATCH"
REASON_BAG_NOT_OPEN = "BAG_NOT_OPEN"
REASON_SHIFT_MISMATCH = "SHIFT_MISMATCH"
REASON_CONCURRENT = "ADMISSION_STATE_CHANGED"


def policy_info() -> dict[str, Any]:
    return {
        "选定架构": "A-仅已路由可入袋",
        "architecture": "routed_only",
        "政策代码": POLICY_CODE,
        "入袋政策": POLICY,
        "口径": POLICY,
        "说明": POLICY_DETAIL,
        "允许待路由先入袋": False,
        "待路由可先入袋": False,
        "待补裁决件数": 0,
        "隔离件数": 0,
    }


def admission_predicate(scan: Any, bag: Any) -> tuple[int, str, str] | None:
    """The single scan-state predicate used by every admission write path."""
    if scan is None:
        return 404, "SCAN_NOT_FOUND", "扫描不存在"
    if scan["status"] != "已路由":
        # 保持既有错误文案，便于现场脚本和旧夹具识别。
        return 400, REASON_NOT_ROUTED, "仅已路由件可入袋"
    if scan["chute_id"] is None or scan["chute_id"] != bag["chute_id"]:
        return 400, REASON_CHUTE_MISMATCH, "扫描口门与袋口门不一致"
    return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _store_outcome(
    conn,
    *,
    idem_key: str | None,
    bag_id: int | None,
    scan_id: int | None,
    allowed: bool,
    http_status: int,
    reason_code: str,
    reason: str,
    request_data: dict,
    response_data: dict,
) -> int:
    now = now_str()
    cur = conn.execute(
        """
        INSERT INTO admission_requests(
          idem_key, policy_code, policy, bag_id, scan_id, allowed,
          reason_code, reason, http_status, request_json, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            idem_key,
            POLICY_CODE,
            POLICY,
            bag_id,
            scan_id,
            1 if allowed else 0,
            reason_code,
            reason,
            http_status,
            json.dumps(request_data, ensure_ascii=False, default=str),
            json.dumps(response_data, ensure_ascii=False, default=str),
            now,
        ),
    )
    admission_id = cur.lastrowid
    conn.execute(
        "UPDATE admission_requests SET response_json=? WHERE id=?",
        (json.dumps(response_data | {"准入编号": admission_id}, ensure_ascii=False, default=str), admission_id),
    )
    return admission_id


def get_admission(conn, admission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM admission_requests WHERE id=?", (admission_id,)).fetchone()
    if not row:
        return None
    result = row_to_dict(row)
    try:
        result["请求"] = json.loads(result.pop("request_json") or "{}")
    except json.JSONDecodeError:
        result["请求"] = {}
    try:
        result["响应"] = json.loads(result.pop("response_json") or "{}")
    except json.JSONDecodeError:
        result["响应"] = {}
    return result


def replay_admission(conn, admission_id: int, *, increment: bool = False) -> dict | None:
    row = conn.execute("SELECT * FROM admission_requests WHERE id=?", (admission_id,)).fetchone()
    if not row:
        return None
    if increment:
        conn.execute("UPDATE admission_requests SET replay_count=replay_count+1 WHERE id=?", (admission_id,))
        audit(conn, "封袋", row["bag_id"], "准入结局重放", f"准入#{admission_id}", result="重放", admission_id=admission_id)
        row = conn.execute("SELECT * FROM admission_requests WHERE id=?", (admission_id,)).fetchone()
    return get_admission(conn, admission_id)


def bag_consistency(conn, bag_id: int) -> dict[str, Any]:
    bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
    if not bag:
        return {
            "bag_id": bag_id,
            "存在": False,
            "一致": False,
            "袋件数": 0,
            "册页件数": 0,
            "口门不一致数": 0,
            "状态不一致数": 0,
            "问题": [{"类型": "袋不存在", "bag_id": bag_id}],
        }

    counts = conn.execute(
        """
        SELECT
          COUNT(s.id) AS 袋件数,
          SUM(CASE
                WHEN (s.chute_id IS NOT NULL AND s.chute_id = b.chute_id)
                 AND (
                   (b.status = '集包中' AND s.status = '已入袋')
                   OR (b.status = '已封袋' AND s.status = '已封袋')
                 )
                THEN 1 ELSE 0
              END) AS 对齐件数,
          SUM(CASE WHEN s.id IS NOT NULL AND (s.chute_id IS NULL OR s.chute_id != b.chute_id) THEN 1 ELSE 0 END) AS 口门不一致数,
          SUM(CASE
                WHEN s.id IS NOT NULL AND b.status = '集包中' AND s.status != '已入袋' THEN 1
                WHEN s.id IS NOT NULL AND b.status = '已封袋' AND s.status != '已封袋' THEN 1
                ELSE 0
              END) AS 状态不一致数
        FROM bags b
        LEFT JOIN scans s ON s.bag_id = b.id
        WHERE b.id = ?
        GROUP BY b.id
        """,
        (bag_id,),
    ).fetchone()
    count_data = dict(counts) if counts else {}
    piece_count = int(count_data.get("袋件数") or 0)
    aligned_count = int(count_data.get("对齐件数") or 0)
    chute_bad = int(count_data.get("口门不一致数") or 0)
    status_bad = int(count_data.get("状态不一致数") or 0)

    items = rows_to_list(conn.execute(
        """
        SELECT s.*, c.code AS 口门码, b.code AS 袋码
        FROM scans s
        LEFT JOIN chutes c ON c.id = s.chute_id
        LEFT JOIN bags b ON b.id = s.bag_id
        WHERE s.bag_id = ?
        ORDER BY s.id
        """,
        (bag_id,),
    ).fetchall())
    issues: list[dict[str, Any]] = []
    for item in items:
        if item.get("chute_id") != bag["chute_id"]:
            issues.append({
                "类型": "口门不一致",
                "bag_id": bag_id,
                "scan_id": item["id"],
                "条码": item["barcode"],
                "扫描口门": item.get("口门码"),
                "袋口门编号": bag["chute_id"],
            })
        expected_status = "已封袋" if bag["status"] == "已封袋" else "已入袋"
        if item["status"] != expected_status:
            issues.append({
                "类型": "袋册状态拧花",
                "bag_id": bag_id,
                "scan_id": item["id"],
                "条码": item["barcode"],
                "扫描状态": item["status"],
                "应有状态": expected_status,
            })

    ledger = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN action='入袋' THEN 1 ELSE 0 END), 0) AS 入袋事件数,
          COALESCE(SUM(CASE WHEN action='移出回流' THEN 1 ELSE 0 END), 0) AS 移出事件数
        FROM bag_item_events WHERE bag_id=?
        """,
        (bag_id,),
    ).fetchone()
    ledger_dict = dict(ledger) if ledger else {}
    return {
        "bag_id": bag_id,
        "袋码": bag["code"],
        "存在": True,
        "袋状态": bag["status"],
        "袋件数": piece_count,
        "对齐件数": aligned_count,
        "异常件数": piece_count - aligned_count,
        # 册页不加状态过滤，确保“册页”和袋件数永远对同一批 bag_id 行计数。
        "册页件数": len(items),
        "口门不一致数": chute_bad,
        "状态不一致数": status_bad,
        "袋册事件": ledger_dict,
        "一致": chute_bad == 0 and status_bad == 0 and piece_count == len(items),
        "问题": issues,
    }


def consistency_report(conn, shift_id: int | None = None) -> dict[str, Any]:
    where = ""
    params: tuple[Any, ...] = ()
    if shift_id is not None:
        where = "WHERE b.shift_id=?"
        params = (shift_id,)
    bags = rows_to_list(conn.execute(f"SELECT id FROM bags b {where} ORDER BY id", params).fetchall())
    bag_reports = [bag_consistency(conn, row["id"]) for row in bags]

    orphan_sql = """
        SELECT s.id AS scan_id, s.barcode, s.status, s.bag_id
        FROM scans s
        LEFT JOIN bags b ON b.id = s.bag_id
        WHERE s.bag_id IS NOT NULL AND b.id IS NULL
    """
    orphan_params: tuple[Any, ...] = ()
    if shift_id is not None:
        orphan_sql += " AND s.shift_id=?"
        orphan_params = (shift_id,)
    orphan_refs = rows_to_list(conn.execute(orphan_sql, orphan_params).fetchall())

    issues: list[dict[str, Any]] = []
    for report in bag_reports:
        issues.extend(report.get("问题", []))
    for orphan in orphan_refs:
        issues.append({"类型": "悬挂袋引用", **orphan})

    if shift_id is None:
        total_pieces = conn.execute(
            "SELECT COUNT(*) c FROM scans s WHERE s.bag_id IS NOT NULL"
        ).fetchone()["c"]
    else:
        total_pieces = conn.execute(
            """
            SELECT COUNT(*) c
            FROM scans s
            JOIN bags b ON b.id = s.bag_id
            WHERE b.shift_id=?
            """,
            (shift_id,),
        ).fetchone()["c"]

    aligned_total = sum(int(report.get("对齐件数") or 0) for report in bag_reports)
    abnormal_total = sum(int(report.get("异常件数") or 0) for report in bag_reports)

    admission_counts = conn.execute(
        """
        SELECT
          COALESCE(SUM(allowed), 0) AS 允许数,
          COALESCE(SUM(CASE WHEN allowed=0 THEN 1 ELSE 0 END), 0) AS 拒绝数,
          COUNT(*) AS 结局数
        FROM admission_requests
        """
    ).fetchone()
    return {
        **policy_info(),
        "一致": not issues,
        "问题总数": len(issues),
        "袋件总数": total_pieces,
        "对齐袋件数": aligned_total,
        "异常袋件数": abnormal_total,
        "悬挂引用数": len(orphan_refs),
        "问题": issues[:200],
        "袋": [{k: v for k, v in report.items() if k != "问题"} for report in bag_reports],
        "准入统计": dict(admission_counts) if admission_counts else {},
    }


def admit_scan(
    conn,
    *,
    bag_id: int,
    scan_id: int,
    idem_key: str | None = None,
    request_data: dict | None = None,
    audit_func=audit,
    expected_shift_id: int | None = None,
) -> dict[str, Any]:
    """Execute one strict admission. Caller owns the IMMEDIATE transaction."""
    request_data = request_data or {}
    fingerprint = json.dumps({"bag_id": bag_id, "scan_id": scan_id}, sort_keys=True, ensure_ascii=False)
    request_data = {**request_data, "指纹": fingerprint}

    if idem_key:
        existing = conn.execute(
            "SELECT * FROM admission_requests WHERE idem_key=?", (idem_key,)
        ).fetchone()
        if existing:
            if existing["bag_id"] != bag_id or existing["scan_id"] != scan_id:
                return {
                    "http_status": 409,
                    "response": {
                        "detail": "准入键已用于其他袋/扫描",
                        "允许": False,
                        "原因码": "IDEMPOTENCY_KEY_CONFLICT",
                        "准入编号": existing["id"],
                    },
                }
            stored = json.loads(existing["response_json"] or "{}")
            conn.execute(
                "UPDATE admission_requests SET replay_count=replay_count+1 WHERE id=?",
                (existing["id"],),
            )
            audit_func(
                conn, "封袋", bag_id, "入袋",
                f"幂等重放 准入#{existing['id']} 扫描{scan_id}",
                result="重放", admission_id=existing["id"],
            )
            stored["重放"] = True
            stored["重放次数"] = int(existing["replay_count"]) + 1
            return {"http_status": existing["http_status"], "response": stored}

    bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()

    def deny(status: int, code: str, message: str) -> dict[str, Any]:
        response = {
            "detail": message,
            "允许": False,
            "重放": False,
            "政策": POLICY,
            "入袋政策": POLICY,
            "原因码": code,
            "原因": message,
            "袋编号": bag_id,
            "扫描编号": scan_id,
        }
        if scan is not None:
            response["status"] = scan["status"]
            response["扫描状态"] = scan["status"]
            response["扫描"] = row_to_dict(scan)
        if bag is not None:
            response["袋"] = row_to_dict(bag)
        aid = _store_outcome(
            conn, idem_key=idem_key, bag_id=bag_id, scan_id=scan_id,
            allowed=False, http_status=status, reason_code=code, reason=message,
            request_data=request_data, response_data=response,
        )
        response["准入编号"] = aid
        response["审计可重放"] = True
        audit_func(
            conn, "封袋", bag_id if bag is not None else None, "入袋",
            f"拒绝 准入#{aid} 扫描{scan_id}：{message}",
            result="拒绝", admission_id=aid,
        )
        # Keep response_json aligned with the generated admission id.
        conn.execute(
            "UPDATE admission_requests SET response_json=? WHERE id=?",
            (json.dumps(response, ensure_ascii=False, default=str), aid),
        )
        return {"http_status": status, "response": response}

    if not bag:
        return deny(404, "BAG_NOT_FOUND", "袋不存在")
    if expected_shift_id is not None and bag["shift_id"] != expected_shift_id:
        return deny(400, REASON_SHIFT_MISMATCH, "袋不属于当前未封班次")
    if bag["status"] != "集包中":
        return deny(400, REASON_BAG_NOT_OPEN, "袋已封，禁止再入")
    if not scan:
        return deny(404, "SCAN_NOT_FOUND", "扫描不存在")
    if expected_shift_id is not None and scan["shift_id"] != expected_shift_id:
        return deny(400, REASON_SHIFT_MISMATCH, "扫描不属于当前未封班次")

    # Without an idempotency key, repeated POST keeps the legacy predicate result
    # (已入袋 is not 已路由 -> 400). Replay of the stored outcome is available via
    # the returned 准入编号; same-key retries are handled above.
    predicate_error = admission_predicate(scan, bag)
    if predicate_error:
        status, code, message = predicate_error
        if scan["status"] == "已入袋" and scan["bag_id"] == bag_id:
            code = "ALREADY_IN_BAG"
        return deny(status, code, message)

    cur = conn.execute(
        """
        UPDATE scans
        SET status='已入袋', bag_id=?, version=COALESCE(version, 0)+1
        WHERE id=?
          AND status='已路由'
          AND chute_id=?
        """,
        (bag_id, scan_id, bag["chute_id"]),
    )
    if cur.rowcount != 1:
        fresh = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        message = "入袋状态已被并发事务改变，请刷新袋册后重试"
        response = {
            "detail": message,
            "允许": False,
            "原因码": REASON_CONCURRENT,
            "原因": message,
            "扫描编号": scan_id,
            "status": fresh["status"] if fresh else None,
            "扫描状态": fresh["status"] if fresh else None,
        }
        aid = _store_outcome(
            conn, idem_key=idem_key, bag_id=bag_id, scan_id=scan_id,
            allowed=False, http_status=409, reason_code=REASON_CONCURRENT, reason=message,
            request_data=request_data, response_data=response,
        )
        audit_func(conn, "封袋", bag_id, "入袋", f"并发拒绝 准入#{aid} 扫描{scan_id}", result="拒绝", admission_id=aid)
        response["准入编号"] = aid
        return {"http_status": 409, "response": response}

    conn.execute(
        """
        INSERT INTO bag_item_events(bag_id, scan_id, action, created_at, note)
        VALUES (?, ?, '入袋', ?, ?)
        """,
        (bag_id, scan_id, now_str(), f"扫描{scan_id}"),
    )

    fresh_scan = row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())
    fresh_bag = row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())
    consistency = bag_consistency(conn, bag_id)
    piece_count = consistency["袋件数"]
    response: dict[str, Any] = {
        **fresh_scan,
        "允许": True,
        "重放": False,
        "政策": POLICY,
        "入袋政策": POLICY,
        "原因码": "",
        "袋编号": bag_id,
        "袋件数": piece_count,
        "扫描状态": fresh_scan["status"],
        "口门一致": consistency["一致"],
        "口门一致性": consistency,
        "袋": {**fresh_bag, "件数": piece_count},
        "审计可重放": True,
    }
    aid = _store_outcome(
        conn, idem_key=idem_key, bag_id=bag_id, scan_id=scan_id,
        allowed=True, http_status=200, reason_code="", reason="",
        request_data=request_data, response_data=response,
    )
    response["准入编号"] = aid
    response["回执"] = {
        "准入编号": aid,
        "袋编号": bag_id,
        "扫描编号": scan_id,
        "条码": fresh_scan["barcode"],
        "扫描状态": "已入袋",
        "袋件数": piece_count,
        "政策": POLICY,
        "口门一致": True,
    }
    conn.execute(
        "UPDATE admission_requests SET response_json=? WHERE id=?",
        (json.dumps(response, ensure_ascii=False, default=str), aid),
    )
    audit_func(
        conn, "封袋", bag_id, "入袋",
        f"准入#{aid} 扫描{scan_id} {fresh_scan['barcode']} 件数{piece_count}",
        result="允许", admission_id=aid,
    )
    return {"http_status": 200, "response": response}

_admit_scan_impl = admit_scan


def admit_scan(*args, **kwargs):
    out = _admit_scan_impl(*args, **kwargs)
    return admit_surface.after_admit(out, *args, **kwargs)

