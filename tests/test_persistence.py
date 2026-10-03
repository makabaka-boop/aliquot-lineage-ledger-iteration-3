import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import make_split, total_balance


def test_restart_preserves_state_and_idempotency(db_path):
    split_body = make_split("root", 0, "k1", [("a", 300), ("b", 100)])
    with TestClient(create_app(db_path)) as c1:
        c1.post("/tubes", json={"id": "root", "balance_ul": 900})
        first = c1.post("/splits", json=split_body)
        assert first.status_code == 201

    # "restart": a brand-new app instance over the same SQLite file
    with TestClient(create_app(db_path)) as c2:
        root = c2.get("/tubes/root").json()
        assert root["balance_ul"] == 500 and root["revision"] == 1
        assert c2.get("/tubes/a").json()["balance_ul"] == 300

        # a retry after the restart returns the original recorded result
        replay = c2.post("/splits", json=split_body)
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert c2.get("/tubes/root").json()["balance_ul"] == 500  # not deducted twice

        # stale revision is still rejected, the bumped revision works
        assert c2.post("/splits", json=make_split("root", 0, "k2", [("c", 10)])).status_code == 412
        assert c2.post("/splits", json=make_split("root", 1, "k3", [("c", 50)])).status_code == 201
        assert total_balance(c2) == 900

        # lineage survives the restart
        chain = c2.get("/tubes/c/ancestry").json()
        assert [hop["tube"]["id"] for hop in chain["chain"]] == ["root", "c"]


def test_history_tables_reject_updates_and_deletes(db_path):
    with TestClient(create_app(db_path)) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        client.post("/splits", json=make_split("root", 0, "k1", [("a", 10)]))

    conn = sqlite3.connect(db_path)
    for table in ("splits", "split_children", "lineage_edges"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {table} SET rowid = rowid")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {table}")
    conn.close()
