from __future__ import annotations

import json
import threading

import pytest

from app.db import write_db
from app.routers.bags import BagAddIn, add_to_bag
from app.routers.scans import route_scan


def test_pending_scan_admission_fails_whole_and_leaves_all_read_models_unchanged(
    client, pending_scan, open_bag
):
    before_bag = client.get("/api/封袋").json()[0]
    response = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": pending_scan["id"], "准入键": "pending-must-fail"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "仅已路由件可入袋"
    assert body["允许"] is False
    assert body["原因码"] == "SCAN_NOT_ROUTED"

    scan = client.get("/api/扫描").json()
    current = next(x for x in scan if x["id"] == pending_scan["id"])
    assert current["status"] == "待路由"
    assert current["bag_id"] is None
    assert current["chute_id"] is None

    bags = client.get("/api/封袋").json()
    bag = next(x for x in bags if x["id"] == open_bag["id"])
    assert bag["件数"] == 0
    assert bag["件数"] == before_bag["件数"]
    assert bag["一致"] is True

    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 0
    assert book["件"] == []

    overview = client.get("/api/总览").json()
    assert overview["分布"]["待路由"] == 1
    assert overview["分布"]["已入袋"] == 0
    assert overview["袋件总数"] == 0
    assert overview["对账"]["一致"] is True
    assert overview["允许待路由先入袋"] is False

    audit = client.get("/api/审计").json()
    rejected = next(e for e in audit if e.get("admission_id") == body["准入编号"])
    assert rejected["action"] == "入袋"
    assert rejected["result"] == "拒绝"

    replayed = client.get(f"/api/封袋/准入/{body['准入编号']}").json()
    assert replayed["allowed"] == 0
    assert replayed["响应"]["detail"] == "仅已路由件可入袋"

    same_key = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": pending_scan["id"], "准入键": "pending-must-fail"},
    )
    assert same_key.status_code == 400
    assert same_key.json()["重放"] is True
    assert client.get(f"/api/封袋/{open_bag['id']}/册").json()["件"] == []


def test_routed_admission_atomically_updates_scan_bag_book_overview_and_audit(
    client, routed_scan, open_bag
):
    response = client.post(
        f"/api/封袋/{open_bag['id']}/入袋",
        json={"扫描编号": routed_scan["id"], "准入键": "ok-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    admission_id = body["准入编号"]
    assert body["status"] == "已入袋"
    assert body["bag_id"] == open_bag["id"]
    assert body["袋件数"] == 1
    assert body["口门一致"] is True
    assert body["回执"]["条码"] == "PB-PENDING-001"

    current = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert current["status"] == "已入袋"
    assert current["袋码"] == open_bag["code"]

    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 1
    assert bag["一致"] is True

    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 1
    assert book["件"][0]["barcode"] == "PB-PENDING-001"
    assert any(e["action"] == "入袋" for e in book["事件"])

    overview = client.get("/api/总览").json()
    assert overview["分布"]["已入袋"] == 1
    assert overview["袋件总数"] == 1
    assert overview["对账"]["一致"] is True

    audit = client.get("/api/审计").json()
    assert any(e.get("admission_id") == admission_id and e["result"] == "允许" for e in audit)

    replay = client.post(f"/api/封袋/准入/{admission_id}/重放")
    assert replay.status_code == 200
    assert replay.json()["响应"]["准入编号"] == admission_id

    duplicate = client.post(
        f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["原因码"] == "ALREADY_IN_BAG"
    assert next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])["件数"] == 1


def test_chute_mismatch_is_rejected_without_bag_count_or_scan_change(
    client, routed_scan, other_chute_id
):
    bag = client.post("/api/封袋/开袋", json={"口门编号": other_chute_id}).json()
    response = client.post(
        f"/api/封袋/{bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "扫描口门与袋口门不一致"
    current = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert current["status"] == "已路由"
    assert current["bag_id"] is None
    assert next(x for x in client.get("/api/封袋").json() if x["id"] == bag["id"])["件数"] == 0


def test_half_failure_after_scan_write_rolls_back_bag_and_scan(
    client, routed_scan, open_bag, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated audit half-write failure")

    monkeypatch.setattr("app.routers.bags.audit", boom)

    with pytest.raises(RuntimeError, match="simulated"):
        client.post(
            f"/api/封袋/{open_bag['id']}/入袋",
            json={"扫描编号": routed_scan["id"], "准入键": "half-failure"},
        )

    scan = next(x for x in client.get("/api/扫描").json() if x["id"] == routed_scan["id"])
    assert scan["status"] == "已路由"
    assert scan["bag_id"] is None
    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 0
    assert client.get(f"/api/封袋/{open_bag['id']}/册").json()["册页件数"] == 0
    consistency = client.get("/api/封袋/一致性").json()
    assert consistency["一致"] is True
    assert consistency["问题总数"] == 0


def test_concurrent_route_and_admission_never_create_double_terminal_or_count_drift(client):
    chute = next(x for x in client.get("/api/口门").json() if x["code"] == "A01")

    for round_no in range(5):
        scan = client.post("/api/扫描", json={"条码": f"CONC-{round_no}", "目的地码": "110"}).json()
        bag = client.post("/api/封袋/开袋", json={"口门编号": chute["id"], "备注": f"c{round_no}"}).json()
        sid = scan["id"]
        bid = bag["id"]
        barrier = threading.Barrier(3)
        outcomes: list[tuple[str, int, dict | None]] = []
        errors: list[Exception] = []

        def route_worker():
            barrier.wait()
            try:
                result = route_scan(sid)
                outcomes.append(("route", 200, {"status": result["status"]}))
            except Exception as exc:  # direct call surfaces HTTPException
                errors.append(exc)

        def bag_worker():
            barrier.wait()
            try:
                response = add_to_bag(bid, BagAddIn(扫描编号=sid), None, None, None)
                payload = json.loads(response.body)
                outcomes.append(("bag", response.status_code, payload))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=route_worker), threading.Thread(target=bag_worker), threading.Thread(target=bag_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        assert not errors, errors
        final = next(x for x in client.get("/api/扫描").json() if x["id"] == sid)
        bag_row = next(x for x in client.get("/api/封袋").json() if x["id"] == bid)
        book = client.get(f"/api/封袋/{bid}/册").json()

        assert final["status"] in ("已路由", "已入袋")
        if final["status"] == "已路由":
            assert final["bag_id"] is None
            assert bag_row["件数"] == 0
            assert book["册页件数"] == 0
        else:
            assert final["status"] == "已入袋"
            assert final["bag_id"] == bid
            assert bag_row["件数"] == 1
            assert book["册页件数"] == 1

        allowed_bag = [
            p for kind, code, p in outcomes
            if kind == "bag" and code == 200 and p and p.get("允许") and not p.get("重放")
        ]
        assert len(allowed_bag) <= 1
        assert bag_row["件数"] == len(book["件"])


def test_divert_from_bag_atomically_releases_bag_count_and_keeps_ledger(client, routed_scan, open_bag):
    admit = client.post(
        f"/api/封袋/{open_bag['id']}/入袋", json={"扫描编号": routed_scan["id"]}
    )
    assert admit.status_code == 200

    diverted = client.post("/api/回流批次", json={"扫描编号": routed_scan["id"]})
    assert diverted.status_code == 200, diverted.text
    scan = diverted.json()["扫描"]
    assert scan["status"] == "错分回流"
    assert scan["bag_id"] is None

    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 0
    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 0
    assert any(e["action"] == "移出回流" for e in book["事件"])
    assert client.get("/api/封袋/一致性").json()["一致"] is True
    overview = client.get("/api/总览").json()
    assert overview["分布"]["错分回流"] == 1
    assert overview["分布"]["已入袋"] == 0


def test_manual_half_state_is_visible_in_consistency_and_overview(client, pending_scan, open_bag):
    # Simulate the field incident: bag association/count exists while scan remains pending.
    with write_db() as conn:
        conn.execute(
            "UPDATE scans SET bag_id=?, chute_id=? WHERE id=?",
            (open_bag["id"], open_bag["chute_id"], pending_scan["id"]),
        )

    report = client.get("/api/封袋/一致性").json()
    assert report["一致"] is False
    assert report["问题总数"] >= 1
    assert any(p["类型"] == "袋册状态拧花" and p["scan_id"] == pending_scan["id"] for p in report["问题"])

    bag = next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])
    assert bag["件数"] == 1
    assert bag["一致"] is False
    assert bag["状态不一致数"] == 1

    overview = client.get("/api/总览").json()
    assert overview["对账"]["一致"] is False
    assert overview["对账"]["问题总数"] >= 1
    assert overview["口门一致性"]["异常袋件数"] == 1

    # The raw book deliberately includes the corrupted row so it can be worked, not hidden.
    book = client.get(f"/api/封袋/{open_bag['id']}/册").json()
    assert book["册页件数"] == 1
    assert book["件"][0]["status"] == "待路由"

    rolled_back = client.post(f"/api/封袋/{open_bag['id']}/回滚异常")
    assert rolled_back.status_code == 200
    assert len(rolled_back.json()["已回滚"]) == 1
    restored = next(x for x in client.get("/api/扫描").json() if x["id"] == pending_scan["id"])
    assert restored["status"] == "待路由"
    assert restored["bag_id"] is None
    assert next(x for x in client.get("/api/封袋").json() if x["id"] == open_bag["id"])["件数"] == 0
    assert client.get("/api/封袋/一致性").json()["一致"] is True
