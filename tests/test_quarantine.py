"""Tube quarantine: cold-chain review can park a contaminated tube and its
existing descendants without rewriting the sample ledger.

A quarantine record stores reason / operator / target tube and is insert-only
history. A tube is effectively quarantined while any record on itself or an
ancestor remains unreleased; releasing one record never releases the others.
Splits and consumptions adjudicate quarantine state inside the same write
transaction as the revision check — idempotent replays of already-committed
requests still return their original receipts, while genuinely new requests
are subject to the current quarantine state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app
from conftest import StatementGate, _start_server, make_consumption, make_split


def make_quarantine(reason: str = "冷链复核污染疑点", operator: str = "qa-1") -> dict:
    return {"reason": reason, "operator": operator}


def _build_chain(client):
    """root(1000) -> mid(400) -> leaf(150), plus sib(100) under root."""
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/splits", json=make_split("root", 0, "s1", [("mid", 400), ("sib", 100)])).status_code == 201
    assert client.post("/splits", json=make_split("mid", 0, "s2", [("leaf", 150)])).status_code == 201


def _run_in_thread(fn):
    """Run fn() in a thread, capturing its return value; returns (thread, box)."""
    box = {}

    def wrapper():
        box["result"] = fn()

    thread = threading.Thread(target=wrapper)
    thread.start()
    return thread, box


# ---------------------------------------------------------------------------
# basic flow: park, block, release, resume
# ---------------------------------------------------------------------------


def test_quarantine_blocks_split_and_consume_until_released(client):
    _build_chain(client)

    resp = client.post("/tubes/mid/quarantine", json=make_quarantine())
    assert resp.status_code == 201
    record = resp.json()
    assert record["tube_id"] == "mid"
    assert record["reason"] == "冷链复核污染疑点"
    assert record["operator"] == "qa-1"
    assert record["created_at"]
    qid = record["quarantine_id"]

    # quarantine moved nothing: balance and revision are as before
    mid = client.get("/tubes/mid").json()
    assert mid["balance_ul"] == 250 and mid["revision"] == 1
    assert mid["quarantine"]["is_quarantined"] is True

    # the tube itself and its descendants are parked
    blocked_split = client.post("/splits", json=make_split("mid", 1, "s3", [("x", 10)]))
    assert blocked_split.status_code == 409
    assert blocked_split.json()["error"]["code"] == "TUBE_QUARANTINED"
    blocked_consume = client.post("/consumptions", json=make_consumption("leaf", 0, "c1", 10))
    assert blocked_consume.status_code == 409
    assert blocked_consume.json()["error"]["code"] == "TUBE_QUARANTINED"

    # an unrelated branch keeps working
    assert client.post("/consumptions", json=make_consumption("sib", 0, "c2", 10)).status_code == 201

    # nothing else moved: conservation still audits clean
    audit = client.get("/tubes/root/conservation").json()
    assert audit["total_balance_ul"] == 990 and audit["total_consumed_ul"] == 10
    assert audit["conserved"] is True

    # release the record -> the flow resumes; blocked keys were not burned
    rel = client.post(f"/quarantines/{qid}/release", json={"operator": "qa-lead"})
    assert rel.status_code == 201
    assert rel.json()["quarantine_id"] == qid
    assert rel.json()["tube_id"] == "mid"
    assert client.get("/tubes/mid").json()["quarantine"]["is_quarantined"] is False
    assert client.post("/splits", json=make_split("mid", 1, "s3", [("x", 10)])).status_code == 201
    assert client.post("/consumptions", json=make_consumption("leaf", 0, "c1", 10)).status_code == 201
    assert client.get("/tubes/root/conservation").json()["conserved"] is True


def test_unquarantined_tube_shows_empty_quarantine_view(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    view = client.get("/tubes/root").json()["quarantine"]
    assert view == {"is_quarantined": False, "direct": [], "effective": []}
    chain = client.get("/tubes/root/ancestry").json()["chain"]
    assert chain[0]["tube"]["quarantine"] == {"is_quarantined": False, "direct": [], "effective": []}


# ---------------------------------------------------------------------------
# nested quarantine: records on ancestor and descendant stack
# ---------------------------------------------------------------------------


def test_nested_quarantine_shows_effective_reasons_root_to_leaf(client):
    _build_chain(client)
    q_root = client.post("/tubes/root/quarantine", json=make_quarantine("root 疑点", "qa-1")).json()
    q_mid = client.post("/tubes/mid/quarantine", json=make_quarantine("mid 疑点", "qa-2")).json()

    # leaf has no record of its own but is parked by both ancestors
    leaf = client.get("/tubes/leaf").json()["quarantine"]
    assert leaf["is_quarantined"] is True
    assert leaf["direct"] == []
    assert [r["quarantine_id"] for r in leaf["effective"]] == [
        q_root["quarantine_id"],
        q_mid["quarantine_id"],
    ]
    assert [(r["tube_id"], r["reason"], r["operator"]) for r in leaf["effective"]] == [
        ("root", "root 疑点", "qa-1"),
        ("mid", "mid 疑点", "qa-2"),
    ]

    mid = client.get("/tubes/mid").json()["quarantine"]
    assert [r["quarantine_id"] for r in mid["direct"]] == [q_mid["quarantine_id"]]
    assert [r["quarantine_id"] for r in mid["effective"]] == [
        q_root["quarantine_id"],
        q_mid["quarantine_id"],
    ]

    root = client.get("/tubes/root").json()["quarantine"]
    assert [r["quarantine_id"] for r in root["direct"]] == [q_root["quarantine_id"]]
    assert [r["quarantine_id"] for r in root["effective"]] == [q_root["quarantine_id"]]

    # the ancestry query carries the same per-hop picture, root to leaf
    chain = client.get("/tubes/leaf/ancestry").json()["chain"]
    assert [hop["tube"]["id"] for hop in chain] == ["root", "mid", "leaf"]
    assert [r["quarantine_id"] for r in chain[0]["tube"]["quarantine"]["effective"]] == [
        q_root["quarantine_id"]
    ]
    for hop in chain[1:]:
        assert [r["quarantine_id"] for r in hop["tube"]["quarantine"]["effective"]] == [
            q_root["quarantine_id"],
            q_mid["quarantine_id"],
        ]
    assert chain[2]["tube"]["quarantine"]["direct"] == []

    # every descendant is blocked, siblings of mid included (via root's record)
    assert client.post("/splits", json=make_split("leaf", 0, "sx", [("t", 1)])).status_code == 409
    assert client.post("/consumptions", json=make_consumption("sib", 0, "cx", 1)).status_code == 409

    # the quarantine itself rewrote no history
    assert [s["request_key"] for s in client.get("/tubes/root/splits").json()["splits"]] == ["s1"]
    root_tube = client.get("/tubes/root").json()
    assert root_tube["initial_ul"] == 1000 and root_tube["balance_ul"] == 500


# ---------------------------------------------------------------------------
# release: one record at a time
# ---------------------------------------------------------------------------


def test_ancestor_release_leaves_descendant_records_in_effect(client):
    _build_chain(client)
    q_root = client.post("/tubes/root/quarantine", json=make_quarantine("root 疑点", "qa-1")).json()
    q_mid = client.post("/tubes/mid/quarantine", json=make_quarantine("mid 疑点", "qa-2")).json()

    # lifting the ancestor's record frees the root but not mid's own filing
    rel = client.post(f"/quarantines/{q_root['quarantine_id']}/release", json={"operator": "qa-lead"})
    assert rel.status_code == 201

    assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is False
    assert client.get("/tubes/sib").json()["quarantine"]["is_quarantined"] is False
    mid = client.get("/tubes/mid").json()["quarantine"]
    assert mid["is_quarantined"] is True
    assert [r["quarantine_id"] for r in mid["effective"]] == [q_mid["quarantine_id"]]
    leaf = client.get("/tubes/leaf").json()["quarantine"]
    assert [r["quarantine_id"] for r in leaf["effective"]] == [q_mid["quarantine_id"]]

    # root works again, mid and leaf stay parked
    assert client.post("/consumptions", json=make_consumption("root", 1, "c1", 5)).status_code == 201
    assert client.post("/consumptions", json=make_consumption("leaf", 0, "c2", 5)).status_code == 409
    assert client.post("/splits", json=make_split("mid", 1, "s3", [("x", 10)])).status_code == 409


def test_same_tube_records_are_released_one_by_one(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    q1 = client.post("/tubes/root/quarantine", json=make_quarantine("疑点A", "qa-1")).json()
    q2 = client.post("/tubes/root/quarantine", json=make_quarantine("疑点B", "qa-2")).json()

    tube = client.get("/tubes/root").json()["quarantine"]
    assert [r["quarantine_id"] for r in tube["direct"]] == [
        q1["quarantine_id"],
        q2["quarantine_id"],
    ]

    # releasing one record never releases the other filed on the same tube
    client.post(f"/quarantines/{q1['quarantine_id']}/release", json={"operator": "qa-lead"})
    tube = client.get("/tubes/root").json()["quarantine"]
    assert tube["is_quarantined"] is True
    assert [r["quarantine_id"] for r in tube["direct"]] == [q2["quarantine_id"]]
    assert client.post("/consumptions", json=make_consumption("root", 0, "c1", 5)).status_code == 409

    client.post(f"/quarantines/{q2['quarantine_id']}/release", json={"operator": "qa-lead"})
    assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is False
    assert client.post("/consumptions", json=make_consumption("root", 0, "c1", 5)).status_code == 201


# ---------------------------------------------------------------------------
# idempotency under quarantine
# ---------------------------------------------------------------------------


def test_committed_requests_replay_under_quarantine_new_ones_blocked(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 500})
    split_body = make_split("root", 0, "s1", [("a", 100)])
    first_split = client.post("/splits", json=split_body)
    consume_body = make_consumption("a", 0, "c1", 40)
    first_consume = client.post("/consumptions", json=consume_body)
    assert first_split.status_code == first_consume.status_code == 201

    client.post("/tubes/root/quarantine", json=make_quarantine())

    # replays of already-committed requests return the original receipts
    replay_split = client.post("/splits", json=split_body)
    assert replay_split.status_code == 201
    assert replay_split.json() == first_split.json()
    replay_consume = client.post("/consumptions", json=consume_body)
    assert replay_consume.status_code == 201
    assert replay_consume.json() == first_consume.json()

    # and were not applied twice
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 400 and root["revision"] == 1
    assert client.get("/tubes/a").json()["balance_ul"] == 60

    # genuinely new requests are blocked, on the tube and its descendants
    assert client.post("/splits", json=make_split("root", 1, "s2", [("b", 10)])).status_code == 409
    assert client.post("/consumptions", json=make_consumption("a", 1, "c2", 10)).status_code == 409


# ---------------------------------------------------------------------------
# quarantine never rewrites the ledger
# ---------------------------------------------------------------------------


def test_quarantine_and_release_leave_history_and_conservation_untouched(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 800})
    client.post("/splits", json=make_split("root", 0, "s1", [("a", 300)]))
    client.post("/consumptions", json=make_consumption("a", 0, "c1", 50))

    audit_before = client.get("/tubes/root/conservation").json()
    splits_before = client.get("/tubes/root/splits").json()
    ancestry_before = client.get("/tubes/a/ancestry").json()

    q = client.post("/tubes/root/quarantine", json=make_quarantine()).json()
    client.post(f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"})

    # conservation audit, split history and lineage edges are byte-identical
    assert client.get("/tubes/root/conservation").json() == audit_before
    assert client.get("/tubes/root/splits").json() == splits_before
    ancestry_after = client.get("/tubes/a/ancestry").json()
    assert [hop["via"] for hop in ancestry_after["chain"]] == [
        hop["via"] for hop in ancestry_before["chain"]
    ]
    root = client.get("/tubes/root").json()
    assert root["initial_ul"] == 800 and root["balance_ul"] == 500 and root["revision"] == 1


def test_quarantine_tables_are_immutable_and_read_only_over_http(db_path):
    with TestClient(create_app(db_path)) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        q = client.post("/tubes/root/quarantine", json=make_quarantine()).json()
        client.post(f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"})
        # no API path rewrites or removes quarantine history
        qid = q["quarantine_id"]
        assert client.put(f"/quarantines/{qid}/release", json={}).status_code == 405
        assert client.delete(f"/quarantines/{qid}/release").status_code == 405
        assert client.delete("/tubes/root/quarantine").status_code == 405

    conn = sqlite3.connect(db_path)
    for table in ("quarantines", "quarantine_releases"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {table} SET rowid = rowid")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {table}")
    conn.close()


# ---------------------------------------------------------------------------
# validation and failure atomicity
# ---------------------------------------------------------------------------


def test_quarantine_validation_errors_and_unknown_tube(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    bad_bodies = [
        {"reason": "", "operator": "qa"},            # empty reason
        {"reason": "   ", "operator": "qa"},         # blank reason
        {"reason": "r", "operator": ""},             # empty operator
        {"reason": "r", "operator": "  "},           # blank operator
        {"reason": "r"},                             # missing operator
        {"operator": "qa"},                          # missing reason
        {"reason": "r", "operator": "qa", "note": "x"},  # unknown field
    ]
    for body in bad_bodies:
        resp = client.post("/tubes/root/quarantine", json=body)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = client.post("/tubes/ghost/quarantine", json=make_quarantine())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TUBE_NOT_FOUND"

    # nothing was recorded; the tube is fully usable
    assert client.get("/tubes/root").json()["quarantine"] == {
        "is_quarantined": False,
        "direct": [],
        "effective": [],
    }
    assert client.post("/consumptions", json=make_consumption("root", 0, "c1", 10)).status_code == 201


def test_release_unknown_or_already_released_leaves_state_untouched(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})

    resp = client.post("/quarantines/999/release", json={"operator": "qa-lead"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "QUARANTINE_NOT_FOUND"

    resp = client.post("/quarantines/1/release", json={"operator": "  "})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    q = client.post("/tubes/root/quarantine", json=make_quarantine()).json()
    assert client.post(
        f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"}
    ).status_code == 201
    again = client.post(f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "QUARANTINE_ALREADY_RELEASED"

    # the failed second release changed nothing: the tube stays free
    assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is False
    assert client.post("/consumptions", json=make_consumption("root", 0, "c1", 10)).status_code == 201


class _WriteFailure:
    """While armed, the wrapped connection fails the next statement starting
    with `statement`, simulating a write exception mid-transaction."""

    def __init__(self, statement: str):
        self.statement = statement
        self.armed = True


class _FailingConnection:
    def __init__(self, real, failure):
        self._real = real
        self._failure = failure

    def execute(self, sql, parameters=()):
        if self._failure.armed and sql.startswith(self._failure.statement):
            self._failure.armed = False
            raise sqlite3.OperationalError("injected write failure")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_quarantine_write_failure_rolls_back_without_partial_record(db_path, monkeypatch):
    failure = _WriteFailure("INSERT INTO quarantines")
    real_connect = dbmod.connect
    monkeypatch.setattr(
        dbmod, "connect", lambda path: _FailingConnection(real_connect(path), failure)
    )
    app = create_app(db_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        resp = client.post("/tubes/root/quarantine", json=make_quarantine())
        assert resp.status_code == 500

        # no half-written record; balance, revision and conservation untouched
        root = client.get("/tubes/root").json()
        assert root["balance_ul"] == 100 and root["revision"] == 0
        assert root["quarantine"]["is_quarantined"] is False
        assert client.get("/tubes/root/conservation").json()["conserved"] is True

        # failure disarmed: the same filing now succeeds
        retry = client.post("/tubes/root/quarantine", json=make_quarantine())
        assert retry.status_code == 201
        assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is True


def test_release_write_failure_rolls_back_and_record_stays_active(db_path, monkeypatch):
    failure = _WriteFailure("INSERT INTO quarantine_releases")
    real_connect = dbmod.connect
    monkeypatch.setattr(
        dbmod, "connect", lambda path: _FailingConnection(real_connect(path), failure)
    )
    app = create_app(db_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        q = client.post("/tubes/root/quarantine", json=make_quarantine()).json()

        resp = client.post(f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"})
        assert resp.status_code == 500

        # the failed release did not lift the record
        assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is True
        assert client.post("/consumptions", json=make_consumption("root", 0, "c1", 5)).status_code == 409

        # failure disarmed: the release now succeeds
        retry = client.post(f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"})
        assert retry.status_code == 201
        assert client.get("/tubes/root").json()["quarantine"]["is_quarantined"] is False


# ---------------------------------------------------------------------------
# restart recovery
# ---------------------------------------------------------------------------


def test_quarantine_and_release_survive_restart(db_path):
    consume_body = make_consumption("mid", 0, "c1", 30, "留样")
    with TestClient(create_app(db_path)) as c1:
        c1.post("/tubes", json={"id": "root", "balance_ul": 900})
        c1.post("/splits", json=make_split("root", 0, "s1", [("mid", 400)]))
        first = c1.post("/consumptions", json=consume_body)
        assert first.status_code == 201
        q_mid = c1.post("/tubes/mid/quarantine", json=make_quarantine("mid 疑点", "qa-1")).json()
        q_root = c1.post("/tubes/root/quarantine", json=make_quarantine("root 疑点", "qa-2")).json()
        # mid's own record is lifted before the restart; root's stays active
        rel = c1.post(f"/quarantines/{q_mid['quarantine_id']}/release", json={"operator": "qa-lead"})
        assert rel.status_code == 201

    # "restart": a brand-new app instance over the same SQLite file
    with TestClient(create_app(db_path)) as c2:
        # the released record stays released, the surviving one still blocks
        mid = c2.get("/tubes/mid").json()["quarantine"]
        assert mid["is_quarantined"] is True
        assert mid["direct"] == []
        assert [r["quarantine_id"] for r in mid["effective"]] == [q_root["quarantine_id"]]
        assert c2.post("/splits", json=make_split("mid", 1, "s2", [("x", 10)])).status_code == 409

        # the pre-quarantine consumption still replays its original receipt
        replay = c2.post("/consumptions", json=consume_body)
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert c2.get("/tubes/mid").json()["balance_ul"] == 370  # not deducted twice

        # lifting the surviving record resumes the flow
        c2.post(f"/quarantines/{q_root['quarantine_id']}/release", json={"operator": "qa-lead"})
        assert c2.post("/splits", json=make_split("mid", 1, "s2", [("x", 10)])).status_code == 201
        assert c2.get("/tubes/root/conservation").json()["conserved"] is True


# ---------------------------------------------------------------------------
# upgrade of a pre-quarantine database
# ---------------------------------------------------------------------------

# The schema as it existed before quarantine was introduced (tubes already
# carry initial_ul; consumptions and both key namespaces exist).
PRE_QUARANTINE_SCHEMA = """
CREATE TABLE tubes (
    id          TEXT PRIMARY KEY,
    balance_ul  INTEGER NOT NULL CHECK (balance_ul >= 0),
    revision    INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at  TEXT NOT NULL,
    initial_ul  INTEGER CHECK (initial_ul > 0)
);
CREATE TABLE splits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id          TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    total_amount_ul    INTEGER NOT NULL CHECK (total_amount_ul > 0),
    created_at         TEXT NOT NULL
);
CREATE TABLE split_children (
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    child_id   TEXT NOT NULL REFERENCES tubes (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0),
    position   INTEGER NOT NULL,
    PRIMARY KEY (split_id, child_id)
);
CREATE TABLE lineage_edges (
    child_id   TEXT PRIMARY KEY REFERENCES tubes (id),
    parent_id  TEXT NOT NULL REFERENCES tubes (id),
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0)
);
CREATE TABLE idempotency_keys (
    request_key   TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    request_body  TEXT NOT NULL,
    response_body TEXT NOT NULL,
    split_id      INTEGER NOT NULL REFERENCES splits (id),
    created_at    TEXT NOT NULL
);
CREATE TABLE consumptions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tube_id            TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    amount_ul          INTEGER NOT NULL CHECK (amount_ul > 0),
    purpose            TEXT NOT NULL CHECK (length(purpose) > 0),
    created_at         TEXT NOT NULL
);
CREATE TABLE consumption_keys (
    request_key    TEXT PRIMARY KEY,
    request_hash   TEXT NOT NULL,
    request_body   TEXT NOT NULL,
    response_body  TEXT NOT NULL,
    consumption_id INTEGER NOT NULL REFERENCES consumptions (id),
    created_at     TEXT NOT NULL
);
"""

OLD_SPLIT_BODY = {
    "parent_id": "root",
    "expected_revision": 0,
    "request_key": "old-split",
    "children": [{"id": "a", "amount_ul": 400}],
}
OLD_SPLIT_RESPONSE = {
    "split_id": 1,
    "request_key": "old-split",
    "parent": {"id": "root", "balance_ul": 600, "revision": 1},
    "children": [{"id": "a", "balance_ul": 400, "revision": 0}],
    "total_amount_ul": 400,
    "created_at": "2026-01-01T00:00:00.000+00:00",
}
OLD_CONSUME_BODY = {
    "tube_id": "a",
    "expected_revision": 0,
    "request_key": "old-consume",
    "amount_ul": 50,
    "purpose": "旧库检测",
}
OLD_CONSUME_RESPONSE = {
    "consumption_id": 1,
    "request_key": "old-consume",
    "tube": {"id": "a", "balance_ul": 350, "revision": 1},
    "amount_ul": 50,
    "purpose": "旧库检测",
    "created_at": "2026-01-01T00:00:00.000+00:00",
}


def _store_key(conn, table, body, response, ref_id, now):
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn.execute(
        f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)",
        (body["request_key"], digest, canonical,
         json.dumps(response, ensure_ascii=False), ref_id, now),
    )


def _build_pre_quarantine_db(path):
    """root 1000 -> split a(400); a consumed 50. No quarantine tables at all."""
    conn = sqlite3.connect(path)
    now = OLD_SPLIT_RESPONSE["created_at"]
    conn.executescript(PRE_QUARANTINE_SCHEMA)
    conn.execute("INSERT INTO tubes VALUES ('root', 600, 1, ?, 1000)", (now,))
    conn.execute("INSERT INTO tubes VALUES ('a', 350, 1, ?, 400)", (now,))
    conn.execute(
        "INSERT INTO splits (id, parent_id, request_key, expected_revision, "
        "total_amount_ul, created_at) VALUES (1, 'root', 'old-split', 0, 400, ?)",
        (now,),
    )
    conn.execute("INSERT INTO split_children VALUES (1, 'a', 400, 0)")
    conn.execute("INSERT INTO lineage_edges VALUES ('a', 'root', 1, 400)")
    _store_key(conn, "idempotency_keys", OLD_SPLIT_BODY, OLD_SPLIT_RESPONSE, 1, now)
    conn.execute(
        "INSERT INTO consumptions (id, tube_id, request_key, expected_revision, "
        "amount_ul, purpose, created_at) VALUES (1, 'a', 'old-consume', 0, 50, '旧库检测', ?)",
        (now,),
    )
    _store_key(conn, "consumption_keys", OLD_CONSUME_BODY, OLD_CONSUME_RESPONSE, 1, now)
    conn.commit()
    conn.close()


def test_pre_quarantine_database_upgrades_losslessly(db_path):
    _build_pre_quarantine_db(db_path)

    with TestClient(create_app(db_path)) as client:
        # old request keys still replay their original receipts
        replay = client.post("/splits", json=OLD_SPLIT_BODY)
        assert replay.status_code == 201
        assert replay.json() == OLD_SPLIT_RESPONSE
        replay = client.post("/consumptions", json=OLD_CONSUME_BODY)
        assert replay.status_code == 201
        assert replay.json() == OLD_CONSUME_RESPONSE

        # conservation audits clean on the upgraded database
        audit = client.get("/tubes/root/conservation").json()
        assert audit["initial_ul"] == 1000
        assert audit["total_balance_ul"] == 950
        assert audit["total_consumed_ul"] == 50
        assert audit["conserved"] is True

        # quarantine works on tubes that predate the feature
        q = client.post("/tubes/a/quarantine", json=make_quarantine()).json()
        assert client.post("/consumptions", json=make_consumption("a", 1, "c-new", 10)).status_code == 409
        assert client.post("/splits", json=make_split("a", 1, "s-new", [("a1", 10)])).status_code == 409
        # ...while the old replay still returns its receipt under quarantine
        replay = client.post("/consumptions", json=OLD_CONSUME_BODY)
        assert replay.status_code == 201
        assert replay.json() == OLD_CONSUME_RESPONSE

        client.post(f"/quarantines/{q['quarantine_id']}/release", json={"operator": "qa-lead"})
        assert client.post("/consumptions", json=make_consumption("a", 1, "c-new", 10)).status_code == 201

    # the upgrade is stable across another restart
    with TestClient(create_app(db_path)) as client:
        audit = client.get("/tubes/root/conservation").json()
        assert audit["total_balance_ul"] == 940
        assert audit["total_consumed_ul"] == 60
        assert audit["conserved"] is True


# ---------------------------------------------------------------------------
# commit-order contention: quarantine vs consumption on the same tube
# ---------------------------------------------------------------------------


class _MultiGateConnection:
    """sqlite3.Connection proxy reporting every statement to several gates, so
    a test can park two different requests mid-flight."""

    def __init__(self, real, gates):
        self._real = real
        self._gates = gates

    def execute(self, sql, parameters=()):
        cur = self._real.execute(sql, parameters)
        for gate in self._gates:
            gate.after_execute(self._real, sql, parameters)
        return cur

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def multi_gated_server(db_path, monkeypatch):
    """Like gated_server, but with two independent statement gates."""
    gates = (StatementGate(), StatementGate())
    real_connect = dbmod.connect
    monkeypatch.setattr(
        dbmod, "connect", lambda path: _MultiGateConnection(real_connect(path), gates)
    )
    app = create_app(db_path)
    url, server, thread = _start_server(app)
    try:
        yield url, gates
    finally:
        for gate in gates:
            gate.release.set()
        server.should_exit = True
        thread.join(timeout=15)


def test_consumption_commits_first_then_quarantine(multi_gated_server):
    url, (gate_consume, gate_quarantine) = multi_gated_server
    httpx.post(f"{url}/tubes", json={"id": "root", "balance_ul": 1000}, timeout=30)

    gate_consume.arm_once(lambda conn, sql, params: sql.startswith("INSERT INTO consumptions"))
    gate_quarantine.arm_once(lambda conn, sql, params: sql.startswith("INSERT INTO quarantines"))

    consume_thread, consume_box = _run_in_thread(
        lambda: httpx.post(
            f"{url}/consumptions", json=make_consumption("root", 0, "c1", 300), timeout=30
        )
    )
    try:
        # the consumption holds the write lock mid-transaction; the quarantine
        # queues behind it at BEGIN IMMEDIATE
        assert gate_consume.entered.wait(timeout=15), "consumption never reached its insert"
        quarantine_thread, quarantine_box = _run_in_thread(
            lambda: httpx.post(f"{url}/tubes/root/quarantine", json=make_quarantine(), timeout=30)
        )
        gate_consume.release.set()
        # the quarantine's insert only runs after the consumption committed
        assert gate_quarantine.entered.wait(timeout=15), "quarantine never reached its insert"
        gate_quarantine.release.set()
        consume_thread.join(timeout=15)
        quarantine_thread.join(timeout=15)
    finally:
        gate_consume.release.set()
        gate_quarantine.release.set()

    consume_resp = consume_box["result"]
    assert consume_resp.status_code == 201
    assert consume_resp.json()["tube"] == {"id": "root", "balance_ul": 700, "revision": 1}
    assert quarantine_box["result"].status_code == 201

    # both committed, in that order: the deduction stands and the tube is parked
    root = httpx.get(f"{url}/tubes/root", timeout=30).json()
    assert root["balance_ul"] == 700 and root["revision"] == 1
    assert root["quarantine"]["is_quarantined"] is True
    audit = httpx.get(f"{url}/tubes/root/conservation", timeout=30).json()
    assert audit["total_balance_ul"] == 700 and audit["total_consumed_ul"] == 300
    assert audit["conserved"] is True


def test_quarantine_commits_first_then_consumption_is_rejected(multi_gated_server):
    url, (gate_quarantine, gate_consume) = multi_gated_server
    httpx.post(f"{url}/tubes", json={"id": "root", "balance_ul": 1000}, timeout=30)

    gate_quarantine.arm_once(lambda conn, sql, params: sql.startswith("INSERT INTO quarantines"))
    quarantine_thread, quarantine_box = _run_in_thread(
        lambda: httpx.post(f"{url}/tubes/root/quarantine", json=make_quarantine(), timeout=30)
    )
    try:
        assert gate_quarantine.entered.wait(timeout=15), "quarantine never reached its insert"
        # the consumption queues at BEGIN IMMEDIATE behind the parked
        # quarantine; gate it once it holds the lock and reads the tube
        gate_consume.arm_once(
            lambda conn, sql, params: sql.startswith("SELECT * FROM tubes")
            and tuple(params) == ("root",)
        )
        consume_thread, consume_box = _run_in_thread(
            lambda: httpx.post(
                f"{url}/consumptions", json=make_consumption("root", 0, "c1", 300), timeout=30
            )
        )
        gate_quarantine.release.set()
        # the consumption's tube read only happens after the quarantine
        # committed — its quarantine adjudication must see that record
        assert gate_consume.entered.wait(timeout=15), "consumption never read the tube"
        gate_consume.release.set()
        quarantine_thread.join(timeout=15)
        consume_thread.join(timeout=15)
    finally:
        gate_quarantine.release.set()
        gate_consume.release.set()

    assert quarantine_box["result"].status_code == 201
    consume_resp = consume_box["result"]
    assert consume_resp.status_code == 409
    assert consume_resp.json()["error"]["code"] == "TUBE_QUARANTINED"

    # the rejected consumption moved nothing and did not burn its key
    root = httpx.get(f"{url}/tubes/root", timeout=30).json()
    assert root["balance_ul"] == 1000 and root["revision"] == 0
    audit = httpx.get(f"{url}/tubes/root/conservation", timeout=30).json()
    assert audit["consumptions"] == [] and audit["conserved"] is True

    # after release, the same key succeeds — the blocked attempt left no trace
    qid = quarantine_box["result"].json()["quarantine_id"]
    httpx.post(f"{url}/quarantines/{qid}/release", json={"operator": "qa-lead"}, timeout=30)
    retry = httpx.post(
        f"{url}/consumptions", json=make_consumption("root", 0, "c1", 300), timeout=30
    )
    assert retry.status_code == 201
    assert retry.json()["tube"] == {"id": "root", "balance_ul": 700, "revision": 1}


def test_quarantine_and_consumption_race_is_consistent_in_either_order(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 1000}).raise_for_status()
    barrier = threading.Barrier(2)
    outcomes = []

    def fire(path, payload):
        barrier.wait(timeout=10)
        resp = httpx.post(f"{server}{path}", json=payload, timeout=30)
        outcomes.append((path, resp.status_code, resp.json()))

    threads = [
        threading.Thread(target=fire, args=("/consumptions", make_consumption("root", 0, "c1", 300))),
        threading.Thread(target=fire, args=("/tubes/root/quarantine", make_quarantine())),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    by_path = {path: (status, body) for path, status, body in outcomes}
    # the quarantine always commits; the consumption either beat it (201) or
    # was adjudicated against it inside the write transaction (409)
    assert by_path["/tubes/root/quarantine"][0] == 201
    consume_status, consume_body = by_path["/consumptions"]
    assert consume_status in (201, 409)

    root = httpx.get(f"{server}/tubes/root", timeout=30).json()
    assert root["quarantine"]["is_quarantined"] is True
    if consume_status == 201:
        assert root["balance_ul"] == 700 and root["revision"] == 1
    else:
        assert consume_body["error"]["code"] == "TUBE_QUARANTINED"
        assert root["balance_ul"] == 1000 and root["revision"] == 0
    audit = httpx.get(f"{server}/tubes/root/conservation", timeout=30).json()
    assert audit["conserved"] is True
    assert audit["total_balance_ul"] + audit["total_consumed_ul"] == 1000
