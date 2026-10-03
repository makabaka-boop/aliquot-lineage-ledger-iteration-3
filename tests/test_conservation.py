"""Conservation audit by root tube, old-database upgrade, and restart review.

The audit reads a root's whole subtree in one snapshot and verifies
sum(balances) + sum(consumed) == initial volume. Old databases (created
before initial_ul and consumptions existed) are upgraded on open: the root's
initial volume is recovered once from its current balance plus everything
directly split out of it — never the bare current balance — and frozen.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import make_consumption, make_split

# The schema as it existed before consumptions and initial_ul were introduced.
OLD_SCHEMA = """
CREATE TABLE tubes (
    id          TEXT PRIMARY KEY,
    balance_ul  INTEGER NOT NULL CHECK (balance_ul >= 0),
    revision    INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at  TEXT NOT NULL
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
"""

OLD_SPLIT_BODY = {
    "parent_id": "root",
    "expected_revision": 0,
    "request_key": "old-key",
    "children": [{"id": "a", "amount_ul": 300}, {"id": "b", "amount_ul": 100}],
}
OLD_SPLIT_RESPONSE = {
    "split_id": 1,
    "request_key": "old-key",
    "parent": {"id": "root", "balance_ul": 600, "revision": 1},
    "children": [
        {"id": "a", "balance_ul": 300, "revision": 0},
        {"id": "b", "balance_ul": 100, "revision": 0},
    ],
    "total_amount_ul": 400,
    "created_at": "2026-01-01T00:00:00.000+00:00",
}

HISTORY_TABLES = ("splits", "split_children", "lineage_edges", "idempotency_keys")


def _dump_history(path):
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in HISTORY_TABLES
        }
    finally:
        conn.close()


def _build_old_db(path):
    """Create a pre-upgrade database: root 1000, one old split root->a(300),b(100)."""
    conn = sqlite3.connect(path)
    now = OLD_SPLIT_RESPONSE["created_at"]
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO tubes VALUES ('root', 600, 1, ?)", (now,))
    conn.execute("INSERT INTO tubes VALUES ('a', 300, 0, ?)", (now,))
    conn.execute("INSERT INTO tubes VALUES ('b', 100, 0, ?)", (now,))
    conn.execute(
        "INSERT INTO splits (id, parent_id, request_key, expected_revision, "
        "total_amount_ul, created_at) VALUES (1, 'root', 'old-key', 0, 400, ?)",
        (now,),
    )
    conn.execute("INSERT INTO split_children VALUES (1, 'a', 300, 0)")
    conn.execute("INSERT INTO split_children VALUES (1, 'b', 100, 1)")
    conn.execute("INSERT INTO lineage_edges VALUES ('a', 'root', 1, 300)")
    conn.execute("INSERT INTO lineage_edges VALUES ('b', 'root', 1, 100)")
    canonical = json.dumps(OLD_SPLIT_BODY, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO idempotency_keys VALUES ('old-key', ?, ?, ?, 1, ?)",
        (digest, canonical, json.dumps(OLD_SPLIT_RESPONSE, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# audit over a multi-level tree
# ---------------------------------------------------------------------------


def test_conservation_audit_after_layered_splits_and_consumptions(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/splits", json=make_split("root", 0, "s1", [("mid", 400), ("sib", 100)])).status_code == 201
    assert client.post("/splits", json=make_split("mid", 0, "s2", [("leaf", 150)])).status_code == 201
    # lab work consumes from two different levels of the tree
    c1 = client.post("/consumptions", json=make_consumption("mid", 1, "c1", 50, "质控"))
    assert c1.status_code == 201
    c2 = client.post("/consumptions", json=make_consumption("leaf", 0, "c2", 20, "复检"))
    assert c2.status_code == 201

    audit = client.get("/tubes/root/conservation").json()
    assert audit["root_id"] == "root"
    assert audit["initial_ul"] == 1000
    assert [t["id"] for t in audit["tubes"]] == ["root", "mid", "sib", "leaf"]
    balances = {t["id"]: t["balance_ul"] for t in audit["tubes"]}
    assert balances == {"root": 500, "mid": 200, "sib": 100, "leaf": 130}
    # every voucher in the subtree is listed with its tube and purpose
    assert [(c["tube_id"], c["amount_ul"], c["purpose"]) for c in audit["consumptions"]] == [
        ("mid", 50, "质控"),
        ("leaf", 20, "复检"),
    ]
    # sum of remaining balances + cumulative consumed == registered initial
    assert audit["total_balance_ul"] == 930
    assert audit["total_consumed_ul"] == 70
    assert audit["conserved"] is True

    # a second root is an independent ledger
    client.post("/tubes", json={"id": "other", "balance_ul": 40})
    assert client.post("/consumptions", json=make_consumption("other", 0, "c3", 5)).status_code == 201
    other = client.get("/tubes/other/conservation").json()
    assert other["initial_ul"] == 40
    assert other["total_balance_ul"] == 35 and other["total_consumed_ul"] == 5
    assert other["conserved"] is True
    # and the first audit is untouched by it
    again = client.get("/tubes/root/conservation").json()
    assert again["total_balance_ul"] == 930 and again["conserved"] is True


def test_conservation_audit_rejects_non_root_and_unknown_tube(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    client.post("/splits", json=make_split("root", 0, "s1", [("child", 40)]))

    resp = client.get("/tubes/child/conservation")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NOT_A_ROOT"

    resp = client.get("/tubes/ghost/conservation")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TUBE_NOT_FOUND"


# ---------------------------------------------------------------------------
# upgrade of a pre-consumption database
# ---------------------------------------------------------------------------


def test_old_database_upgrade_recovers_and_freezes_initial_volume(db_path):
    _build_old_db(db_path)
    history_before = _dump_history(db_path)

    with TestClient(create_app(db_path)) as client:
        # old split requests and lineage edges were not rewritten by the upgrade
        assert _dump_history(db_path) == history_before

        # root initial volume recovered as balance 600 + direct split-out 400,
        # not the bare current balance 600
        root = client.get("/tubes/root").json()
        assert root["balance_ul"] == 600
        assert root["initial_ul"] == 1000
        # children recovered from their creation edges in the lineage history
        assert client.get("/tubes/a").json()["initial_ul"] == 300
        assert client.get("/tubes/b").json()["initial_ul"] == 100

        # the old split request still replays its original receipt
        replay = client.post("/splits", json=OLD_SPLIT_BODY)
        assert replay.status_code == 201
        assert replay.json() == OLD_SPLIT_RESPONSE
        conflict = client.post("/splits", json=make_split("root", 0, "old-key", [("z", 1)]))
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "REQUEST_KEY_CONFLICT"

        # conservation audit works on the upgraded database
        audit = client.get("/tubes/root/conservation").json()
        assert audit["initial_ul"] == 1000
        assert audit["total_balance_ul"] == 1000
        assert audit["total_consumed_ul"] == 0
        assert audit["conserved"] is True

        # new consumptions and splits work on upgraded tubes
        c = client.post("/consumptions", json=make_consumption("root", 1, "c1", 50, "旧库检测"))
        assert c.status_code == 201
        s = client.post("/splits", json=make_split("a", 0, "s-new", [("a1", 120)]))
        assert s.status_code == 201
        assert client.get("/tubes/a1").json()["initial_ul"] == 120

        audit = client.get("/tubes/root/conservation").json()
        assert audit["total_balance_ul"] == 950
        assert audit["total_consumed_ul"] == 50
        assert audit["conserved"] is True


def test_upgrade_is_idempotent_across_restarts(db_path):
    _build_old_db(db_path)
    with TestClient(create_app(db_path)) as client:
        client.post("/consumptions", json=make_consumption("root", 1, "c1", 70))

    # second open of the same file: migration must not "recover" again —
    # the initial volume stays frozen at 1000 even though balances moved
    with TestClient(create_app(db_path)) as client:
        root = client.get("/tubes/root").json()
        assert root["balance_ul"] == 530
        assert root["initial_ul"] == 1000
        audit = client.get("/tubes/root/conservation").json()
        assert audit["total_balance_ul"] == 930
        assert audit["total_consumed_ul"] == 70
        assert audit["conserved"] is True


# ---------------------------------------------------------------------------
# restart review
# ---------------------------------------------------------------------------


def test_restart_preserves_consumptions_and_conservation(db_path):
    consume_body = make_consumption("mid", 0, "c1", 30, "留样")
    with TestClient(create_app(db_path)) as c1:
        c1.post("/tubes", json={"id": "root", "balance_ul": 900})
        c1.post("/splits", json=make_split("root", 0, "s1", [("mid", 400), ("sib", 100)]))
        first = c1.post("/consumptions", json=consume_body)
        assert first.status_code == 201

    # "restart": a brand-new app instance over the same SQLite file
    with TestClient(create_app(db_path)) as c2:
        # a retry after the restart returns the original recorded receipt
        replay = c2.post("/consumptions", json=consume_body)
        assert replay.status_code == 201
        assert replay.json() == first.json()
        # and was not deducted twice
        assert c2.get("/tubes/mid").json()["balance_ul"] == 370

        audit = c2.get("/tubes/root/conservation").json()
        assert audit["initial_ul"] == 900
        assert audit["total_balance_ul"] == 870
        assert audit["total_consumed_ul"] == 30
        assert audit["conserved"] is True
        assert [c["request_key"] for c in audit["consumptions"]] == ["c1"]

        # stale revision still rejected, the bumped revision works
        assert c2.post("/consumptions", json=make_consumption("mid", 0, "c2", 10)).status_code == 412
        assert c2.post("/consumptions", json=make_consumption("mid", 1, "c2", 10)).status_code == 201
        assert c2.get("/tubes/root/conservation").json()["conserved"] is True
