"""28 集包准入撕裂夹具（架构 A：ROUTED_ONLY 全局唯一口径）。

成功灯必须能由“准入谓词 ∪ 袋册成员 ∪ 扫描状态机”在同一事务里同命运重放：
- 待路由静默进袋、已路由被拒却进袋成功 -> 整笔回滚；
- 半截成员写入与状态翻页必须一起回滚；
- 口门占用与袋占用不允许叠成双终态；
- 任何绕开写口的“表面层”成功宣称都不得存在。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.db import db, write_db
from app.routers.bags import BagAddIn, add_to_bag
from app.routers.scans import route_scan


# ---------------------------------------------------------------------------
# 夹具一：灯亮而状态机仍停在待路由 —— 成功回执与准入证据不得撕裂
# ---------------------------------------------------------------------------

def test_successful_receipt_must_replay_from_predicate_ledger_and_state_machine(
    client, routed_scan, open_bag
):
    response = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": routed_scan["id"], "准入键": "receipt-truth-1"},
    )
    assert response.status_code == 200
    body = response.json()
    admission_id = body["准入编号"]
    assert body["允许"] is True
    assert body["回执"]["扫描状态"] == "已入袋"

    # 1) 状态机翻页
    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert scan["status"] == "已入袋"
    assert scan["bag_id"] == open_bag["id"]

    # 2) 袋册多且只多这一行成员
    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 1
    assert book["件"][0]["id"] == routed_scan["id"]
    assert book["件"][0]["status"] == "已入袋"

    # 3) 总览已入袋格按 scans 实情计数，不存在独立“表面”计数
    overview = client.get("/api/总览").json()
    assert overview["分布"]["已入袋"] == 1
    assert "surface_ok" not in overview
    assert "surface_probe" not in overview
    assert "surface_待路由" not in overview

    # 4) 准入结局与审计可按编号重放，且与现场同命运
    outcome = client.get(f"/api/封袋/准入/{admission_id}").json()
    assert outcome["allowed"] == 1
    assert outcome["policy_code"] == "ROUTED_ONLY"
    assert outcome["响应"]["回执"]["扫描状态"] == "已入袋"
    audit_trail = client.get(f"/api/审计/准入/{admission_id}").json()
    assert any(e["result"] == "允许" for e in audit_trail["审计事件"])

    # 5) 任何成功灯旁路的设置键/审计伪事件都不得存在
    with db() as conn:
        surface_keys = [
            r["key"]
            for r in conn.execute("SELECT key FROM settings WHERE key LIKE 'surface%' OR key LIKE 'admit_surface:%'")
        ]
        fake_audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE kind='表面' AND action='成功宣称'"
        ).fetchone()["c"]
    assert surface_keys == []
    assert fake_audit == 0


def test_pending_scan_never_silently_enters_bag_even_under_replay(client, pending_scan, open_bag):
    first = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": pending_scan["id"], "准入键": "no-silent-pending"},
    )
    assert first.status_code == 400
    assert first.json()["原因码"] == "SCAN_NOT_ROUTED"
    # 同键重放也必须维持拒绝，不允许第二次换口径放进去
    again = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": pending_scan["id"], "准入键": "no-silent-pending"},
    )
    assert again.status_code == 400
    assert again.json()["重放"] is True

    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == pending_scan["id"])
    assert scan["status"] == "待路由"
    assert scan["bag_id"] is None
    assert next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])["件数"] == 0
    assert client.get(f"/api/封袋/{open_bag['id']}/册").json()["册页件数"] == 0
    # 拒绝路径同样不许留下任何表面层脏设置
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM settings WHERE key LIKE 'surface%'").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# 夹具二：已路由进袋失败却袋有成员 —— 幽灵成员必须被打出来并可回滚
# ---------------------------------------------------------------------------

def test_ghost_member_pending_row_inside_bag_is_flagged_and_rolls_back_whole(
    client, pending_scan, open_bag
):
    # 现场拧花：袋册挂了成员，状态机却还停在待路由
    with write_db() as conn:
        conn.execute(
            "UPDATE scans SET bag_id=?, chute_id=? WHERE id=?",
            (open_bag["id"], open_bag["chute_id"], pending_scan["id"]),
        )

    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 1
    assert bag["一致"] is False
    assert bag["状态不一致数"] == 1

    report = client.get("/api/封袋/一致性").json()
    assert report["一致"] is False
    assert any(
        p["类型"] == "袋册状态拧花" and p["scan_id"] == pending_scan["id"]
        for p in report["问题"]
    )
    overview = client.get("/api/总览").json()
    assert overview["对账"]["一致"] is False
    assert overview["口门一致性"]["异常袋件数"] == 1

    # 封袋闸必须挡住幽灵成员袋，不准假装可封
    blocked = client.post(f"/api/封袋/{open_bag['id']}/封口")
    assert blocked.status_code == 409

    rolled = client.post(f"/api/封袋/{open_bag['id']}/回滚异常")
    assert rolled.status_code == 200
    assert len(rolled.json()["已回滚"]) == 1
    restored = next(x for x in client.get("/api/扫描").json() if x["id"] == pending_scan["id"])
    assert restored["status"] == "待路由"
    assert restored["bag_id"] is None
    assert next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])["件数"] == 0
    assert client.get("/api/封袋/一致性").json()["一致"] is True


def test_routed_scan_rejected_by_predicate_never_leaves_bag_member(client, routed_scan, other_chute_id):
    wrong_bag = client.post("/api/封袋/开袋", json={"口门编号": other_chute_id}).json()
    response = client.post(
        f"/api/封袋/{wrong_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert response.status_code == 400
    assert response.json()["原因码"] == "CHUTE_MISMATCH"

    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert scan["status"] == "已路由"
    assert scan["bag_id"] is None
    book = client.get(f"/api/封袋/{wrong_bag['id']}/册").json()
    assert book["册页件数"] == 0
    assert book["事件"] == []
    # 被拒准入本身要留痕且可重放，但不产生袋册事件
    audit_rows = client.get("/api/审计").json()
    assert any(
        e.get("admission_id") == response.json()["准入编号"] and e["result"] == "拒绝"
        for e in audit_rows
    )


# ---------------------------------------------------------------------------
# 夹具三：半截成员写入而状态未翻 —— 事务边界必须一起回滚
# ---------------------------------------------------------------------------

def test_half_write_failure_rolls_back_member_event_state_and_admission_claim(
    client, routed_scan, open_bag, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after scan page flip")

    # 炸在审计写入点：扫描翻页与袋册事件已在同一事务内，必须整体回滚
    monkeypatch.setattr("app.routers.bags.audit", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        client.post(
            f"/api/封袋/{open_bag['id']}/入袋",
            json={"扫描编号": routed_scan["id"], "准入键": "half-boom"},
        )

    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert scan["status"] == "已路由"
    assert scan["bag_id"] is None
    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 0
    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 0
    assert book["事件"] == []
    # 回滚的事务连准入结局也不许留下“成功”宣称
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM admission_requests WHERE allowed=1").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM bag_item_events").fetchone()["c"] == 0
    assert client.get("/api/封袋/一致性").json()["一致"] is True

    # 故障恢复后同一扫描仍可正常入袋（状态机没有被半截写口卡死）
    monkeypatch.undo()
    retry = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": routed_scan["id"], "准入键": "half-boom-retry"},
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "已入袋"


def test_half_written_bag_item_event_without_scan_flip_is_visible_and_repairable(
    client, routed_scan, open_bag
):
    # 模拟“成员行写进去了、状态机没翻页”的半截现场（只造事件，不动 scans）
    with write_db() as conn:
        conn.execute(
            "INSERT INTO bag_item_events(bag_id, scan_id, action, created_at, note) VALUES (?, ?, '入袋', ?, ?)",
            (open_bag["id"], routed_scan["id"], "2026-11-25 00:00:00", "半截写入演练"),
        )

    # 袋件数以 scans.bag_id 当前集合为准：幽灵事件不得虚增袋件数
    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 0
    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 0
    assert any(e["action"] == "入袋" for e in book["事件"])
    # 扫描仍可正常走完裁决后入袋，不被半截事件挡住
    ok = client.post(
        f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert ok.status_code == 200
    assert next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])["status"] == "已入袋"


# ---------------------------------------------------------------------------
# 夹具四：袋占用与口门占用叠罗汉 —— 并发写口不得产生双终态
# ---------------------------------------------------------------------------

def test_concurrent_admission_and_routing_never_stack_bag_and_chute_occupancy(client):
    chute = next(x for x in client.get("/api/口门").json() if x["code"] == "A01")
    for round_no in range(6):
        scan = client.post("/api/扫描", json={"条码": f"STACK-{round_no}", "目的地码": "110"}).json()
        bag = client.post("/api/封袋/开袋", json={"口门编号": chute["id"]}).json()
        sid, bid = scan["id"], bag["id"]
        barrier = threading.Barrier(3)
        outcomes: list[tuple[str, int]] = []
        errors: list[Exception] = []

        def route_worker():
            barrier.wait()
            try:
                route_scan(sid)
                outcomes.append(("route", 200))
            except Exception as exc:
                errors.append(exc)

        def bag_worker():
            barrier.wait()
            try:
                resp = add_to_bag(bid, BagAddIn(扫描编号=sid), None, None, None)
                outcomes.append(("bag", resp.status_code))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=route_worker),
            threading.Thread(target=bag_worker),
            threading.Thread(target=bag_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()
        assert not errors, errors

        final = next(x for x in client.get("/api/扫描").json() if x["id"] == sid)
        bag_row = next(x for x in client.get("/api/封袋").json() if x["id"] == bid)
        book = client.get(f"/api/封袋/{bid}/册").json()

        # 双终态禁止：要么待路由/已路由且无袋，要么已入袋且挂这只袋
        assert final["status"] in ("已路由", "已入袋")
        if final["status"] == "已路由":
            assert final["bag_id"] is None
            assert bag_row["件数"] == 0
        else:
            assert final["bag_id"] == bid
            assert bag_row["件数"] == 1
        assert bag_row["件数"] == book["册页件数"]
        allowed = [code for kind, code in outcomes if kind == "bag" and code == 200]
        assert len(allowed) <= 1
        assert client.get("/api/封袋/一致性").json()["一致"] is True


def test_concurrent_double_admission_same_scan_succeeds_at_most_once(client, routed_scan, open_bag):
    sid, bid = routed_scan["id"], open_bag["id"]
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def worker():
        barrier.wait()
        try:
            resp = add_to_bag(bid, BagAddIn(扫描编号=sid), None, None, None)
            results.append(resp.status_code)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()
    assert not errors, errors
    assert sorted(results) == [200, 400]
    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == bid)
    assert bag["件数"] == 1
    assert client.get(f"/api/封袋/{bid}/册").json()["册页件数"] == 1
    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == sid)
    assert scan["status"] == "已入袋"


# ---------------------------------------------------------------------------
# 封袋闸 / 班次闸 / 回流移袋 / 重放对账 同命运链
# ---------------------------------------------------------------------------

def test_seal_gate_blocks_until_state_machine_ledger_and_predicate_align(
    client, routed_scan, open_bag
):
    # 空袋不可封
    assert client.post(f"/api/封袋/{open_bag['id']}/封口").status_code == 400
    client.post(f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]})

    sealed = client.post(f"/api/封袋/{open_bag['id']}/封口")
    assert sealed.status_code == 200
    bag = sealed.json()
    assert bag["status"] == "已封袋"
    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert scan["status"] == "已封袋"
    # 已封袋禁止再入
    again = client.post(
        f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert again.status_code == 400
    assert again.json()["原因码"] == "BAG_NOT_OPEN"


def test_divert_moves_member_out_of_bag_atomically_and_chain_stays_consistent(
    client, routed_scan, open_bag
):
    client.post(f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]})
    diverted = client.post("/api/回流批次", json={"扫描编号": routed_scan["id"]})
    assert diverted.status_code == 200
    scan = diverted.json()["扫描"]
    assert scan["status"] == "错分回流"
    assert scan["bag_id"] is None
    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 0
    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 0
    assert any(e["action"] == "移出回流" for e in book["事件"])
    assert client.get("/api/封袋/一致性").json()["一致"] is True
    # 回流后可重新裁决、重新入袋
    rerouted = client.post(f"/api/扫描/{routed_scan['id']}/路由")
    assert rerouted.status_code == 200
    readmit = client.post(
        f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert readmit.status_code == 200


def test_replay_endpoints_return_identical_outcome_without_rewriting_state(
    client, routed_scan, open_bag
):
    ok = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": routed_scan["id"], "准入键": "replay-chain"},
    )
    admission_id = ok.json()["准入编号"]
    before = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])

    replayed = client.post(f"/api/封袋/准入/{admission_id}/重放").json()
    assert replayed["allowed"] == 1
    assert replayed["replay_count"] >= 1
    via_audit = client.get(f"/api/审计/准入/{admission_id}").json()
    assert via_audit["准入"]["id"] == admission_id
    alias = client.get(f"/api/admissions/{admission_id}/replay").json()
    assert alias["id"] == admission_id

    after = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert after["status"] == before["status"] == "已入袋"
    assert after["bag_id"] == before["bag_id"] == open_bag["id"]
    # 重放只增审计，不改袋件数
    assert next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])["件数"] == 1


def test_barcode_destination_line_shift_write_gates_share_one_routed_only_policy(client):
    # 条码目的地 -> 路由落入 -> 口门在口 -> 入袋 -> 封袋 全链一条口径
    chute_rows = client.get("/api/口门").json()
    chute = next(c for c in chute_rows if c["code"] == "A02")
    line = client.get("/api/分拣线").json()[0]
    scan = client.post(
        "/api/扫描",
        json={"条码": "CHAIN-001", "目的地码": "210", "分拣线编号": line["id"]},
    ).json()
    assert scan["status"] == "待路由"
    # 闭门时裁决必须被挡，件落不进口门
    client.post(f"/api/口门/{chute['id']}/开闭?状态=口门闭")
    assert client.post(f"/api/扫描/{scan['id']}/路由").status_code == 400
    client.post(f"/api/口门/{chute['id']}/开闭?状态=口门开")
    routed = client.post(f"/api/扫描/{scan['id']}/路由").json()
    assert routed["status"] == "已路由"
    assert routed["chute_id"] == chute["id"]

    bag = client.post("/api/封袋/开袋", json={"口门编号": chute["id"]}).json()
    admitted = client.post(
        f"/api/封袋/{bag['id']}/入袋", json={"扫描编号": scan["id"]}
    ).json()
    assert admitted["允许"] is True
    assert admitted["袋"]["chute_id"] == chute["id"]
    assert client.post(f"/api/封袋/{bag['id']}/封口").status_code == 200

    overview = client.get("/api/总览").json()
    assert overview["允许待路由先入袋"] is False
    assert overview["architecture"] == "routed_only"
    assert overview["对账"]["一致"] is True
    shift_report = client.get("/api/班次/对账").json()
    assert shift_report["一致"] is True


def test_settings_cannot_switch_global_policy_to_pending_first(client):
    rejected = client.post("/api/设置", json={"键": "入袋准入", "值": "待路由先入袋"})
    assert rejected.status_code == 409
    policy = client.get("/api/封袋/政策").json()
    assert policy["政策代码"] == "ROUTED_ONLY"
    assert policy["允许待路由先入袋"] is False


def test_surface_modules_are_gone_and_no_read_path_emits_fake_lamp_fields(client, routed_scan, open_bag):
    # 模块层面：旁路实现已物理移除，import 必须失败
    import importlib

    for module_name in (
        "app.services.admit_surface",
        "app.services.ledger_skew",
        "app.services.surface_probe",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)

    client.post(f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]})
    # 所有读页面都不得再出现 surface_* 假灯字段
    for path in ("/api/总览", "/api/扫描", "/api/审计", "/api/封袋", "/api/口门", "/api/回流批次", "/api/班次"):
        payload = client.get(path).json()
        text = json.dumps(payload, ensure_ascii=False)
        assert "surface_ok" not in text
        assert "surface_probe" not in text
        assert "surface_status" not in text
        assert "成功宣称" not in text
