"""Tube-level quarantine (cold-chain review hold).

A quarantine record saves a reason, an operator id and a target tube. A tube's
*effective* quarantine is decided by active records on itself and on every one
of its ancestors — descendants of a held mother tube are held too. Releasing
one record releases nothing else. Holds are adjudicated inside the same SQLite
write transaction that performs a split/consumption, so a committed hold can
never be slipped past; successful requests still replay their original receipt.
"""

from __future__ import annotations

import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import make_consumption, make_split


def quarantine(client, tube_id: str, reason: str = "冷链复核污染疑点", operator: str = "alice"):
    return client.post(
        "/quarantines", json={"tube_id": tube_id, "reason": reason, "operator_id": operator}
    )


def release(client, record_id: int, operator: str = "bob", note=None):
    body = {"operator_id": operator}
    if note is not None:
        body["note"] = note
    return client.post(f"/quarantine-records/{record_id}/release", json=body)


def _build_tree(client):
    """root(1000) -> mid(400) -> leaf(150); root left with balance 600."""
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    client.post("/splits", json=make_split("root", 0, "s1", [("mid", 400), ("sib", 100)]))
    client.post("/splits", json=make_split("mid", 0, "s2", [("leaf", 150)]))


# ---------------------------------------------------------------------------
# basic hold: direct record blocks splitting and consuming, changes nothing
# ---------------------------------------------------------------------------


def test_quarantine_record_shape_and_tube_view(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    resp = quarantine(client, "root", reason="管外冷凝异常", operator="op-1")
    assert resp.status_code == 201
    rec = resp.json()
    assert rec["quarantine_id"] == 1
    assert rec["tube_id"] == "root"
    assert rec["reason"] == "管外冷凝异常"
    assert rec["operator_id"] == "op-1"
    assert rec["created_at"]
    assert rec["released_at"] is None
    assert rec["released_by"] is None
    assert rec["release_note"] is None
    assert rec["is_quarantined"] is True
    assert [q["quarantine_id"] for q in rec["effective_quarantine"]] == [1]

    view = client.get("/tubes/root").json()["quarantine"]
    assert view["is_quarantined"] is True
    assert [q["quarantine_id"] for q in view["direct"]] == [1]
    assert [q["quarantine_id"] for q in view["effective"]] == [1]

    # a clean tube reports an empty hold, and the list endpoint is untouched
    client.post("/tubes", json={"id": "clean", "balance_ul": 5})
    clean = client.get("/tubes/clean").json()["quarantine"]
    assert clean == {"is_quarantined": False, "direct": [], "effective": []}
    listed = client.get("/tubes").json()["tubes"]
    assert all("quarantine" not in t for t in listed)  # list view behaviour unchanged


def test_quarantine_blocks_split_and_consumption_without_side_effects(client):
    _build_tree(client)
    assert quarantine(client, "mid").status_code == 201

    blocked_split = client.post(
        "/splits", json=make_split("mid", 0, "x", [("kid", 50)])
    )
    assert blocked_split.status_code == 423
    err = blocked_split.json()["error"]
    assert err["code"] == "TUBE_QUARANTINED"
    assert [q["tube_id"] for q in err["effective_quarantine"]] == ["mid"]

    blocked_consume = client.post(
        "/consumptions", json=make_consumption("mid", 0, "x", 10)
    )
    assert blocked_consume.status_code == 423
    assert blocked_consume.json()["error"]["code"] == "TUBE_QUARANTINED"

    # the blocked requests changed nothing: balances, revision, edges, vouchers
    mid = client.get("/tubes/mid").json()
    assert mid["balance_ul"] == 250 and mid["revision"] == 1
    assert client.get("/tubes/kid").status_code == 404
    audit = client.get("/tubes/root/conservation").json()
    assert audit["conserved"] is True
    assert audit["total_consumed_ul"] == 0
    # and they did not burn their request keys
    assert release(client, 1).status_code == 200
    assert client.post(
        "/splits", json=make_split("mid", 1, "x", [("kid", 50)])
    ).status_code == 201
    assert client.post(
        "/consumptions", json=make_consumption("mid", 2, "x", 10)
    ).status_code == 201


def test_quarantine_unknown_tube_is_404_and_persists_nothing(client):
    resp = quarantine(client, "ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TUBE_NOT_FOUND"
    assert client.get("/tubes").json()["tubes"] == []


def test_quarantine_validation_errors(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    base = {"tube_id": "root", "reason": "r", "operator_id": "o"}
    for payload in (
        {"tube_id": "root", "reason": "", "operator_id": "o"},
        {"tube_id": "root", "reason": "   ", "operator_id": "o"},
        {"tube_id": "root", "reason": "r", "operator_id": ""},
        {"tube_id": "root", "reason": "r"},                       # missing operator
        {"tube_id": "", "reason": "r", "operator_id": "o"},       # empty tube id
        dict(base, unexpected=1),                                 # unknown field
    ):
        resp = client.post("/quarantines", json=payload)
        assert resp.status_code == 422, payload
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    resp = release(client, 1, operator=" ")  # blank operator body
    assert resp.status_code == 422
    assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is False


# ---------------------------------------------------------------------------
# nested quarantine: a held ancestor holds the whole existing descendant tree,
# direct holds on descendants are reported separately
# ---------------------------------------------------------------------------


def test_nested_ancestor_quarantine_holds_whole_tree(client):
    _build_tree(client)
    assert quarantine(client, "root", reason="母管污染疑点", operator="alice").status_code == 201

    # every existing descendant is effectively held by the root's record
    for tid in ("root", "mid", "sib", "leaf"):
        view = client.get(f"/tubes/{tid}").json()["quarantine"]
        assert view["is_quarantined"] is True
        assert view["direct"] == ([] if tid != "root" else view["direct"])
        effective_tubes = [q["tube_id"] for q in view["effective"]]
        assert effective_tubes == ["root"]

    # splitting the root, an inner tube and a leaf are all refused — with the
    # ancestor's reason, not a missing-tube or revision error
    for parent, rev in (("root", 1), ("mid", 1), ("leaf", 0)):
        resp = client.post(
            "/splits", json=make_split(parent, rev, f"k-{parent}", [("k", 1)])
        )
        assert resp.status_code == 423, parent
        assert resp.json()["error"]["effective_quarantine"][0]["reason"] == "母管污染疑点"
    resp = client.post("/consumptions", json=make_consumption("leaf", 0, "c", 1))
    assert resp.status_code == 423

    # conservation facts are exactly as before the hold
    audit = client.get("/tubes/root/conservation").json()
    assert audit["conserved"] is True
    assert audit["total_balance_ul"] == 1000 and audit["total_consumed_ul"] == 0


def test_effective_reasons_are_ordered_root_to_leaf(client):
    _build_tree(client)
    r_root = quarantine(client, "root", reason="根因 R").json()["quarantine_id"]
    r_mid = quarantine(client, "mid", reason="中层 M").json()["quarantine_id"]
    r_leaf = quarantine(client, "leaf", reason="叶 L").json()["quarantine_id"]

    view = client.get("/tubes/leaf").json()["quarantine"]
    # direct = only the leaf's own record; effective = root -> mid -> leaf
    assert [q["quarantine_id"] for q in view["direct"]] == [r_leaf]
    assert [(q["tube_id"], q["reason"]) for q in view["effective"]] == [
        ("root", "根因 R"),
        ("mid", "中层 M"),
        ("leaf", "叶 L"),
    ]

    # ancestry query presents the same root-first effective list and per-hop
    # direct records, without altering the historical edges in any way
    anc = client.get("/tubes/leaf/ancestry").json()
    assert anc["is_quarantined"] is True
    assert [q["tube_id"] for q in anc["effective_quarantine"]] == ["root", "mid", "leaf"]
    direct_at = {
        hop["tube"]["id"]: [q["quarantine_id"] for q in hop["direct_quarantine"]]
        for hop in anc["chain"]
    }
    assert direct_at == {"root": [r_root], "mid": [r_mid], "leaf": [r_leaf]}
    # the original split evidence on every hop is untouched
    assert [hop["via"] for hop in anc["chain"]][0] is None
    assert anc["chain"][1]["via"]["amount_ul"] == 400
    assert anc["chain"][2]["via"]["amount_ul"] == 150


def test_hold_on_descendant_does_not_hold_ancestors_or_siblings(client):
    _build_tree(client)
    assert quarantine(client, "leaf").status_code == 201
    assert client.get("/tubes/mid").json()["quarantine"]["is_quarantined"] is False
    assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is False
    assert client.get("/tubes/sib").json()["quarantine"]["is_quarantined"] is False
    # mid can still dispense to a *new* child; the hold only covers leaf
    resp = client.post("/splits", json=make_split("mid", 1, "s3", [("newkid", 20)]))
    assert resp.status_code == 201
    # the new sibling is born clean — leaf's record does not propagate sideways
    assert client.get("/tubes/newkid").json()["quarantine"]["is_quarantined"] is False


# ---------------------------------------------------------------------------
# releasing: one record at a time; other records (same tube or ancestors) hold
# ---------------------------------------------------------------------------


def test_releasing_one_record_does_not_release_ancestor_or_other_records(client):
    _build_tree(client)
    r_root = quarantine(client, "root", reason="祖先隔离").json()["quarantine_id"]
    r_mid = quarantine(client, "mid", reason="本管隔离").json()["quarantine_id"]

    # releasing the mid record leaves the leaf held by the ancestor record
    resp = release(client, r_mid, operator="bob", note="中层复核通过")
    assert resp.status_code == 200
    body = resp.json()
    assert body["released_at"] and body["released_by"] == "bob"
    assert body["release_note"] == "中层复核通过"
    assert body["is_quarantined"] is True  # mid itself is still held via root
    assert [q["quarantine_id"] for q in body["effective_quarantine"]] == [r_root]

    mid_view = client.get("/tubes/mid").json()["quarantine"]
    assert [q["quarantine_id"] for q in mid_view["direct"]] == []  # own record released
    assert [q["quarantine_id"] for q in mid_view["effective"]] == [r_root]
    leaf_view = client.get("/tubes/leaf").json()["quarantine"]
    assert [q["quarantine_id"] for q in leaf_view["effective"]] == [r_root]

    # leaf still cannot be consumed; the reason carried is the ancestor's
    resp = client.post("/consumptions", json=make_consumption("leaf", 0, "c", 1))
    assert resp.status_code == 423
    assert resp.json()["error"]["effective_quarantine"][0]["reason"] == "祖先隔离"

    # releasing the root record lifts the whole tree
    assert release(client, r_root).json()["is_quarantined"] is False
    for tid in ("root", "mid", "leaf", "sib"):
        assert client.get(f"/tubes/{tid}").json()["quarantine"]["is_quarantined"] is False
    resp = client.post("/consumptions", json=make_consumption("leaf", 0, "c", 1))
    assert resp.status_code == 201


def test_two_records_on_one_tube_releasing_one_keeps_the_hold(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    first = quarantine(client, "root", reason="疑点一").json()["quarantine_id"]
    second = quarantine(client, "root", reason="疑点二").json()["quarantine_id"]
    assert [q["quarantine_id"] for q in client.get("/tubes/root").json()["quarantine"]["direct"]] == [
        first,
        second,
    ]

    resp = release(client, first)
    assert resp.status_code == 200
    assert resp.json()["is_quarantined"] is True  # the sibling record still holds
    remaining = client.get("/tubes/root").json()["quarantine"]
    assert [q["quarantine_id"] for q in remaining["direct"]] == [second]
    blocked = client.post("/consumptions", json=make_consumption("root", 0, "c", 1))
    assert blocked.status_code == 423
    assert blocked.json()["error"]["effective_quarantine"][0]["reason"] == "疑点二"

    assert release(client, second).json()["is_quarantined"] is False
    assert client.post(
        "/consumptions", json=make_consumption("root", 0, "c", 1)
    ).status_code == 201


def test_release_errors_unknown_and_double(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    rid = quarantine(client, "root").json()["quarantine_id"]

    missing = client.post("/quarantine-records/999/release", json={"operator_id": "bob"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "QUARANTINE_RECORD_NOT_FOUND"

    assert release(client, rid).status_code == 200
    again = release(client, rid, note="再次解除")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "QUARANTINE_ALREADY_RELEASED"

    # the failed double-release changed nothing: still released once, still free
    view = client.get("/tubes/root").json()["quarantine"]
    assert view["is_quarantined"] is False
    assert view["direct"] == []
    audit = client.get("/tubes/root/conservation").json()
    assert audit["conserved"] is True


def test_quarantine_does_not_rewrite_history_or_initial_volume(db_path):
    with TestClient(create_app(db_path)) as client:
        _build_tree(client)
        client.post("/consumptions", json=make_consumption("mid", 1, "c0", 20, "留样"))
        quarantine(client, "mid")
        release(client, 1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # initial volumes still frozen at creation values
    assert dict(conn.execute("SELECT * FROM tubes WHERE id = 'mid'").fetchone())["initial_ul"] == 400
    # lineage edges unchanged
    edges = conn.execute("SELECT child_id, parent_id, amount_ul FROM lineage_edges ORDER BY child_id").fetchall()
    assert [tuple(e) for e in edges] == [("leaf", "mid", 150), ("mid", "root", 400), ("sib", "root", 100)]
    # consumption voucher untouched
    c = conn.execute("SELECT tube_id, amount_ul FROM consumptions").fetchone()
    assert tuple(c) == ("mid", 20)
    conn.close()


def test_quarantine_records_are_immutable_except_release(db_path):
    with TestClient(create_app(db_path)) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        rid = quarantine(client, "root").json()["quarantine_id"]

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM quarantine_records")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE quarantine_records SET reason = '改写原因'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE quarantine_records SET tube_id = 'other'")
    # the legitimate release transition is allowed by the trigger
    conn.execute(
        "UPDATE quarantine_records SET released_at = '2026-01-01T00:00:00+00:00', "
        "released_by = 'sql' WHERE id = ?",
        (rid,),
    )
    conn.commit()
    # a released row cannot be re-opened
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE quarantine_records SET released_at = NULL WHERE id = ?", (rid,))
    conn.close()


# ---------------------------------------------------------------------------
# idempotent requests: an already-successful request replays even under hold
# ---------------------------------------------------------------------------


def test_successful_request_replays_while_quarantined(client):
    _build_tree(client)
    # a consumption commits before the hold exists
    payload = make_consumption("mid", 1, "done", 30, "检测")
    first = client.post("/consumptions", json=payload)
    assert first.status_code == 201

    quarantine(client, "mid")
    # a *new* request is blocked by the current hold...
    fresh = client.post("/consumptions", json=make_consumption("mid", 2, "new", 5))
    assert fresh.status_code == 423
    # ...but the retry of the already-committed request returns its original
    # receipt verbatim and does not deduct a second time
    replay = client.post("/consumptions", json=payload)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    mid = client.get("/tubes/mid").json()
    assert mid["balance_ul"] == 220 and mid["revision"] == 2
    audit = client.get("/tubes/root/conservation").json()
    assert audit["total_consumed_ul"] == 30


# ---------------------------------------------------------------------------
# restart recovery: active holds, releases and ordering survive a restart
# ---------------------------------------------------------------------------


def test_quarantine_state_survives_restart(db_path):
    with TestClient(create_app(db_path)) as c1:
        _build_tree(c1)
        r_root = quarantine(c1, "root", reason="重启后仍生效").json()["quarantine_id"]
        r_mid = quarantine(c1, "mid").json()["quarantine_id"]
        release(c1, r_mid)

    with TestClient(create_app(db_path)) as c2:
        leaf = c2.get("/tubes/leaf").json()["quarantine"]
        assert leaf["is_quarantined"] is True
        assert [q["quarantine_id"] for q in leaf["effective"]] == [r_root]
        assert leaf["effective"][0]["reason"] == "重启后仍生效"

        mid = c2.get("/tubes/mid").json()["quarantine"]
        assert mid["is_quarantined"] is True
        assert [q["quarantine_id"] for q in mid["direct"]] == []  # release persisted

        blocked = c2.post("/consumptions", json=make_consumption("leaf", 0, "c", 1))
        assert blocked.status_code == 423

        assert release(c2, r_root).status_code == 200
        assert c2.get("/tubes/mid").json()["quarantine"]["is_quarantined"] is False
        ok = c2.post("/consumptions", json=make_consumption("leaf", 0, "c", 1))
        assert ok.status_code == 201
        audit = c2.get("/tubes/root/conservation").json()
        assert audit["conserved"] is True


# ---------------------------------------------------------------------------
# old-database upgrade: quarantine tables appear without touching old facts
# ---------------------------------------------------------------------------


def test_old_database_upgrades_losslessly_with_quarantine(db_path):
    from test_conservation import _build_old_db, OLD_SPLIT_BODY, OLD_SPLIT_RESPONSE, _dump_history

    _build_old_db(db_path)
    history_before = _dump_history(db_path)

    with TestClient(create_app(db_path)) as client:
        # old history untouched, old request still replays its original receipt
        assert _dump_history(db_path) == history_before
        replay = client.post("/splits", json=OLD_SPLIT_BODY)
        assert replay.status_code == 201 and replay.json() == OLD_SPLIT_RESPONSE

        # quarantine works on upgraded tubes and covers old descendants
        assert quarantine(client, "root").status_code == 201
        assert client.get("/tubes/a").json()["quarantine"]["is_quarantined"] is True
        blocked = client.post("/splits", json=make_split("a", 0, "ns", [("a1", 1)]))
        assert blocked.status_code == 423
        # still lossless: the blocked split added no history at all
        assert _dump_history(db_path) == history_before

        assert release(client, 1).status_code == 200
        new_split = client.post("/splits", json=make_split("a", 0, "ns", [("a1", 1)]))
        assert new_split.status_code == 201

        # the original old facts are still byte-for-byte present; only the one
        # genuinely-new split (allowed after release) was appended
        history_after = _dump_history(db_path)
        for table, rows in history_before.items():
            assert history_after[table][: len(rows)] == rows
        assert len(history_after["splits"]) == len(history_before["splits"]) + 1
        audit = client.get("/tubes/root/conservation").json()
        assert audit["conserved"] is True


# ---------------------------------------------------------------------------
# commit ordering: quarantine/release vs consumption contending for one tube
# against a real uvicorn server + real SQLite file, interleaved with a gate
# ---------------------------------------------------------------------------


def _post_in_thread(url, method, path, payload):
    box = {}

    def run():
        resp = httpx.request(method, f"{url}{path}", json=payload, timeout=30)
        box["status"] = resp.status_code
        box["body"] = resp.json()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_quarantine_committed_first_blocks_waiting_consumption(gated_server):
    """Quarantine parks after its record INSERT (still uncommitted). The
    consumption thread's BEGIN IMMEDIATE then blocks on the write lock. When the
    quarantine commits first, the consumption wakes, adjudicates inside its own
    transaction against the now-visible hold, and is refused 423."""
    url, gate = gated_server
    httpx.post(f"{url}/tubes", json={"id": "root", "balance_ul": 100}).raise_for_status()

    gate.arm_once(
        lambda conn, sql, params: sql.startswith("INSERT INTO quarantine_records")
        and conn.in_transaction
    )
    qthread, qbox = _post_in_thread(
        url, "POST", "/quarantines", {"tube_id": "root", "reason": "争用隔离", "operator_id": "a"}
    )
    try:
        assert gate.entered.wait(timeout=15), "quarantine insert never ran"
        # the consumption can only queue for the write lock while quarantine holds it
        cthread, cbox = _post_in_thread(
            url, "POST", "/consumptions", make_consumption("root", 0, "race-c", 5)
        )
        # give the writer time to park on the lock, then let the quarantine commit
        threading.Event().wait(0.5)
        assert cthread.is_alive()  # still queued: it has not decided yet
    finally:
        gate.release.set()
    qthread.join(timeout=15)
    cthread.join(timeout=15)

    assert qbox["status"] == 201
    assert cbox["status"] == 423
    assert cbox["body"]["error"]["code"] == "TUBE_QUARANTINED"
    assert cbox["body"]["error"]["effective_quarantine"][0]["reason"] == "争用隔离"
    # the refused consumption left no voucher, no balance change, key unburned
    root = httpx.get(f"{url}/tubes/root").json()
    assert root["balance_ul"] == 100 and root["revision"] == 0
    audit = httpx.get(f"{url}/tubes/root/conservation").json()
    assert audit["consumptions"] == []

    # releasing reopens the tube and the same request key now succeeds
    httpx.post(f"{url}/quarantine-records/1/release", json={"operator_id": "b"}).raise_for_status()
    ok = httpx.post(f"{url}/consumptions", json=make_consumption("root", 0, "race-c", 5))
    assert ok.status_code == 201
    assert httpx.get(f"{url}/tubes/root").json()["balance_ul"] == 95


def test_consumption_committed_first_then_quarantine_governs_only_new_requests(gated_server):
    """The release parks holding the write lock before its UPDATE commits. A
    consumption issued then queues on the lock; once the release commits first
    it wakes, adjudicates inside its own transaction against the now-lifted
    hold, and succeeds. A quarantine placed afterwards blocks only subsequent
    new requests — the already-committed request still replays its receipt."""
    url, gate = gated_server
    httpx.post(f"{url}/tubes", json={"id": "root", "balance_ul": 100}).raise_for_status()
    httpx.post(
        f"{url}/quarantines",
        json={"tube_id": "root", "reason": "临时隔离", "operator_id": "a"},
    ).raise_for_status()

    # park the release with the write lock held, before the UPDATE runs
    gate.arm_once(
        lambda conn, sql, params: sql.startswith("UPDATE quarantine_records")
        and conn.in_transaction
    )
    rthread, rbox = _post_in_thread(
        url, "POST", "/quarantine-records/1/release", {"operator_id": "b", "note": "放行"}
    )
    try:
        assert gate.entered.wait(timeout=15), "release never reached its UPDATE"
        # while release still holds the lock, the consumption queues
        cthread, cbox = _post_in_thread(
            url, "POST", "/consumptions", make_consumption("root", 0, "race-c", 40)
        )
        threading.Event().wait(0.5)
        assert cthread.is_alive()  # ordered behind the release, not yet decided
    finally:
        gate.release.set()
    rthread.join(timeout=15)
    cthread.join(timeout=15)

    assert rbox["status"] == 200 and rbox["body"]["is_quarantined"] is False
    assert cbox["status"] == 201
    root = httpx.get(f"{url}/tubes/root").json()
    assert root["balance_ul"] == 60 and root["revision"] == 1

    # a hold placed after governs subsequent new requests — the old receipt
    # still replays, the fresh request is blocked at the new revision too
    httpx.post(
        f"{url}/quarantines",
        json={"tube_id": "root", "reason": "再次隔离", "operator_id": "a"},
    ).raise_for_status()
    replay = httpx.post(
        f"{url}/consumptions", json=make_consumption("root", 0, "race-c", 40)
    )
    assert replay.status_code == 201  # original receipt, no second deduction
    fresh = httpx.post(
        f"{url}/consumptions", json=make_consumption("root", 1, "fresh-c", 5)
    )
    assert fresh.status_code == 423
    assert httpx.get(f"{url}/tubes/root").json()["balance_ul"] == 60
