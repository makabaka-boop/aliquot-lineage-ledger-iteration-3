"""Consumption vouchers: irreversible deductions with the same transaction,
idempotency and concurrency discipline as splits."""

from __future__ import annotations

import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app
from conftest import make_consumption, make_split

INT64_MAX = 2**63 - 1


def test_consume_basic_receipt_and_state(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    resp = client.post("/consumptions", json=make_consumption("root", 0, "c1", 250, "测序上机"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["consumption_id"] == 1
    assert body["request_key"] == "c1"
    assert body["tube"] == {"id": "root", "balance_ul": 750, "revision": 1}
    assert body["amount_ul"] == 250
    assert body["purpose"] == "测序上机"
    assert body["created_at"]

    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 750 and root["revision"] == 1
    # consumed volume no longer counts as splittable balance
    assert sum(t["balance_ul"] for t in client.get("/tubes").json()["tubes"]) == 750


def test_consume_may_take_entire_balance(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = client.post("/consumptions", json=make_consumption("root", 0, "c1", 100))
    assert resp.status_code == 201
    assert resp.json()["tube"]["balance_ul"] == 0
    # a further microlitre is now impossible
    resp = client.post("/consumptions", json=make_consumption("root", 1, "c2", 1))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_BALANCE"


def test_consume_insufficient_balance_leaves_no_voucher_and_key_reusable(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = client.post("/consumptions", json=make_consumption("root", 0, "c1", 101))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 100 and root["revision"] == 0
    audit = client.get("/tubes/root/conservation").json()
    assert audit["consumptions"] == [] and audit["total_consumed_ul"] == 0

    # the failed request did not burn the key
    fixed = client.post("/consumptions", json=make_consumption("root", 0, "c1", 60))
    assert fixed.status_code == 201
    assert client.get("/tubes/root").json()["balance_ul"] == 40


def test_consume_unknown_tube_404(client):
    resp = client.post("/consumptions", json=make_consumption("ghost", 0, "c1", 10))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TUBE_NOT_FOUND"


def test_consume_stale_revision_412_and_key_reusable(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))
    resp = client.post("/consumptions", json=make_consumption("root", 0, "c2", 10))
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"
    # key not burned by the failed attempt
    resp = client.post("/consumptions", json=make_consumption("root", 1, "c2", 10))
    assert resp.status_code == 201
    assert client.get("/tubes/root").json()["balance_ul"] == 80


def test_consume_validation_errors(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    base = make_consumption("root", 0, "c1", 10)

    cases = []
    for bad_amount in (0, -5, 2.5, "10", True, INT64_MAX + 1):
        cases.append(make_consumption("root", 0, "c1", bad_amount))
    cases.append(make_consumption("root", 0, "c1", 10, ""))          # empty purpose
    cases.append(make_consumption("root", 0, "c1", 10, "   "))       # blank purpose
    cases.append(make_consumption("root", -1, "c1", 10))             # negative revision
    cases.append(dict(base, operator="bob"))                         # unknown field
    missing = dict(base)
    del missing["purpose"]                                           # missing purpose
    cases.append(missing)

    for payload in cases:
        resp = client.post("/consumptions", json=payload)
        assert resp.status_code == 422, payload
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # nothing applied, no key burned — the same key still works afterwards
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 100 and root["revision"] == 0
    ok = client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))
    assert ok.status_code == 201


def test_consume_retry_same_key_same_body_returns_original(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    payload = make_consumption("root", 0, "retry-1", 300, "留样复测")

    first = client.post("/consumptions", json=payload)
    second = client.post("/consumptions", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    # the consumption happened exactly once
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 700 and root["revision"] == 1
    audit = client.get("/tubes/root/conservation").json()
    assert len(audit["consumptions"]) == 1
    assert audit["total_consumed_ul"] == 300


def test_consume_retry_with_reordered_json_keys_is_still_a_replay(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    first = client.post(
        "/consumptions",
        content=json.dumps(
            {"tube_id": "root", "expected_revision": 0, "request_key": "rk",
             "amount_ul": 40, "purpose": "酶活检测"}
        ),
        headers={"content-type": "application/json"},
    )
    second = client.post(
        "/consumptions",
        content=json.dumps(
            {"purpose": "酶活检测", "amount_ul": 40, "request_key": "rk",
             "expected_revision": 0, "tube_id": "root"}
        ),
        headers={"content-type": "application/json"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert client.get("/tubes/root").json()["balance_ul"] == 60


def test_consume_same_key_different_body_409(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/consumptions", json=make_consumption("root", 0, "dup", 100)).status_code == 201

    resp = client.post("/consumptions", json=make_consumption("root", 0, "dup", 101))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REQUEST_KEY_CONFLICT"

    resp = client.post("/consumptions", json=make_consumption("root", 0, "dup", 100, "别的用途"))
    assert resp.status_code == 409

    # nothing extra was applied
    assert client.get("/tubes/root").json()["balance_ul"] == 900
    audit = client.get("/tubes/root/conservation").json()
    assert len(audit["consumptions"]) == 1


def test_consume_and_split_keys_are_independent_namespaces(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/splits", json=make_split("root", 0, "shared", [("a", 100)])).status_code == 201
    # the same key string in the consumption flow is its own request
    resp = client.post("/consumptions", json=make_consumption("root", 1, "shared", 50))
    assert resp.status_code == 201
    assert client.get("/tubes/root").json()["balance_ul"] == 850


def test_consumption_history_is_immutable(db_path):
    with TestClient(create_app(db_path)) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE consumptions SET amount_ul = 999")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM consumptions")
    conn.close()


def test_initial_volume_is_frozen(db_path):
    with TestClient(create_app(db_path)) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tubes SET initial_ul = 999")
    # ordinary balance/revision updates are unaffected by the freeze trigger
    conn.execute("UPDATE tubes SET balance_ul = 50, revision = 1")
    row = conn.execute("SELECT balance_ul, initial_ul FROM tubes WHERE id = 'root'").fetchone()
    assert row == (50, 100)
    conn.close()


class _WriteFailure:
    """Flag object: while armed, the wrapped connection fails the next
    INSERT INTO consumptions, simulating a write exception mid-transaction."""

    def __init__(self):
        self.armed = True


class _FailingConnection:
    def __init__(self, real, failure):
        self._real = real
        self._failure = failure

    def execute(self, sql, parameters=()):
        if self._failure.armed and sql.startswith("INSERT INTO consumptions"):
            self._failure.armed = False
            raise sqlite3.OperationalError("injected write failure")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_write_failure_rolls_back_without_partial_voucher(db_path, monkeypatch):
    failure = _WriteFailure()
    real_connect = dbmod.connect
    monkeypatch.setattr(
        dbmod, "connect", lambda path: _FailingConnection(real_connect(path), failure)
    )
    app = create_app(db_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/tubes", json={"id": "root", "balance_ul": 100})
        resp = client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))
        assert resp.status_code == 500

        # no half voucher: balance, revision and history are all untouched
        root = client.get("/tubes/root").json()
        assert root["balance_ul"] == 100 and root["revision"] == 0
        audit = client.get("/tubes/root/conservation").json()
        assert audit["consumptions"] == [] and audit["total_consumed_ul"] == 0
        assert audit["conserved"] is True

        # the key was not burned by the failed write (failure now disarmed)
        retry = client.post("/consumptions", json=make_consumption("root", 0, "c1", 10))
        assert retry.status_code == 201
        assert client.get("/tubes/root").json()["balance_ul"] == 90


# ---------------------------------------------------------------------------
# concurrency: consumption vs split (and consumption vs consumption) on the
# same revision — at most one writer may win
# ---------------------------------------------------------------------------


def _race(url, calls):
    """Fire (path, payload) calls simultaneously; return [(status, body)]."""
    barrier = threading.Barrier(len(calls))
    outcomes = []

    def fire(path, payload):
        barrier.wait(timeout=10)
        resp = httpx.post(f"{url}{path}", json=payload, timeout=30)
        outcomes.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=fire, args=c) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return outcomes


def test_consumption_and_split_race_same_revision_one_wins(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 1000}).raise_for_status()
    outcomes = _race(
        server,
        [
            ("/splits", make_split("root", 0, "race-split", [("child", 400)])),
            ("/consumptions", make_consumption("root", 0, "race-consume", 300)),
        ],
    )
    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 412]
    loser = next(body for status, body in outcomes if status == 412)
    assert loser["error"]["code"] == "REVISION_CONFLICT"

    root = httpx.get(f"{server}/tubes/root", timeout=30).json()
    assert root["revision"] == 1
    audit = httpx.get(f"{server}/tubes/root/conservation", timeout=30).json()
    assert audit["conserved"] is True
    assert audit["total_balance_ul"] + audit["total_consumed_ul"] == 1000


def test_many_consumptions_race_same_revision_exactly_one_wins(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 5000}).raise_for_status()
    outcomes = _race(
        server,
        [("/consumptions", make_consumption("root", 0, f"racer-{i}", 100)) for i in range(8)],
    )
    statuses = [status for status, _ in outcomes]
    assert statuses.count(201) == 1
    assert statuses.count(412) == 7
    root = httpx.get(f"{server}/tubes/root", timeout=30).json()
    assert root["revision"] == 1 and root["balance_ul"] == 4900
    audit = httpx.get(f"{server}/tubes/root/conservation", timeout=30).json()
    assert audit["total_consumed_ul"] == 100
    assert audit["conserved"] is True


def test_concurrent_identical_consumption_retry_is_applied_once(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 500}).raise_for_status()
    payload = make_consumption("root", 0, "dup-key", 100)
    outcomes = _race(server, [("/consumptions", payload), ("/consumptions", dict(payload))])

    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 201]
    bodies = [body for _, body in outcomes]
    assert bodies[0] == bodies[1]

    root = httpx.get(f"{server}/tubes/root", timeout=30).json()
    assert root["revision"] == 1 and root["balance_ul"] == 400
    audit = httpx.get(f"{server}/tubes/root/conservation", timeout=30).json()
    assert len(audit["consumptions"]) == 1
