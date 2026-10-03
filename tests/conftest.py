from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app


def make_split(parent: str, revision: int, key: str, children: list[tuple[str, int]]) -> dict:
    return {
        "parent_id": parent,
        "expected_revision": revision,
        "request_key": key,
        "children": [{"id": cid, "amount_ul": amt} for cid, amt in children],
    }


def make_consumption(tube: str, revision: int, key: str, amount: int, purpose: str = "QC 检测") -> dict:
    return {
        "tube_id": tube,
        "expected_revision": revision,
        "request_key": key,
        "amount_ul": amount,
        "purpose": purpose,
    }


def total_balance(client: TestClient) -> int:
    return sum(t["balance_ul"] for t in client.get("/tubes").json()["tubes"])


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "lab.db")


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    with TestClient(app) as test_client:
        yield test_client


def _start_server(app):
    """Bind a real uvicorn instance to an ephemeral port and wait for it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn failed to start")
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server, thread


@pytest.fixture()
def server(db_path):
    """A real uvicorn instance in a thread: concurrent requests get genuinely
    independent SQLite connections, like two terminals hitting the API."""
    app = create_app(db_path)
    url, server, thread = _start_server(app)
    yield url
    server.should_exit = True
    thread.join(timeout=15)


class StatementGate:
    """Parks one chosen SQL statement inside the request executing it, after
    the statement ran but before the request continues, until the test
    releases it. Lets a test interleave a second request deterministically
    into the middle of a first one — against the real database file."""

    def __init__(self):
        self._lock = threading.Lock()
        self._predicate = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def arm_once(self, predicate):
        """Park the next statement for which predicate(conn, sql, params) is true."""
        with self._lock:
            self._predicate = predicate

    def after_execute(self, conn, sql, params):
        with self._lock:
            if self._predicate is None or not self._predicate(conn, sql, params):
                return
            self._predicate = None
        self.entered.set()
        if not self.release.wait(timeout=15):
            raise RuntimeError("statement gate was not released in time")


class _GatedConnection:
    """sqlite3.Connection proxy that reports every executed statement to the gate."""

    def __init__(self, real, gate):
        self._real = real
        self._gate = gate

    def execute(self, sql, parameters=()):
        cur = self._real.execute(sql, parameters)
        self._gate.after_execute(self._real, sql, parameters)
        return cur

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def gated_server(db_path, monkeypatch):
    """Like `server`, but every connection the app opens is wrapped so tests
    can park a chosen statement mid-request. Yields (base_url, gate)."""
    gate = StatementGate()
    real_connect = dbmod.connect
    monkeypatch.setattr(
        dbmod, "connect", lambda path: _GatedConnection(real_connect(path), gate)
    )
    app = create_app(db_path)
    url, server, thread = _start_server(app)
    yield url, gate
    server.should_exit = True
    thread.join(timeout=15)
