"""Acceptance: determinate creation receipts and lineage snapshots under
interleaved traffic, plus stable rejection of out-of-range volumes.

The interleaving tests run against a real uvicorn server over a real SQLite
file. A statement gate (see conftest.gated_server) parks a chosen SQL
statement mid-request so a second request lands exactly between two steps of
the first — no sleeps, no probabilistic races.
"""

from __future__ import annotations

import threading

import httpx
from conftest import make_split

INT64_MAX = 2**63 - 1


def _post_tube(url: str, tube_id: str, balance_ul: int) -> httpx.Response:
    return httpx.post(f"{url}/tubes", json={"id": tube_id, "balance_ul": balance_ul}, timeout=30)


def _post_split(url: str, parent: str, revision: int, key: str, children: list) -> httpx.Response:
    return httpx.post(f"{url}/splits", json=make_split(parent, revision, key, children), timeout=30)


def _total_balance(url: str) -> int:
    return sum(t["balance_ul"] for t in httpx.get(f"{url}/tubes", timeout=30).json()["tubes"])


def _run_in_thread(fn):
    """Run fn() in a thread, capturing its return value; returns (thread, box)."""
    box = {}

    def wrapper():
        box["result"] = fn()

    thread = threading.Thread(target=wrapper)
    thread.start()
    return thread, box


# ---------------------------------------------------------------------------
# Registration receipt: a determinate record of the state this request created
# ---------------------------------------------------------------------------


def test_register_receipt_is_creation_state_even_when_split_lands_mid_request(gated_server):
    url, gate = gated_server

    # Park the registration after its INSERT has committed (autocommit -> the
    # connection is not inside a transaction) but before the response is built.
    gate.arm_once(
        lambda conn, sql, params: sql.startswith("INSERT INTO tubes")
        and not conn.in_transaction
    )

    thread, box = _run_in_thread(lambda: _post_tube(url, "root", 1000))
    try:
        assert gate.entered.wait(timeout=15), "registration INSERT never happened"
        # Another operator immediately splits the brand-new tube. This commits
        # while the registration request is still parked.
        split = _post_split(url, "root", 0, "s1", [("a", 400)])
        assert split.status_code == 201
        assert split.json()["parent"] == {"id": "root", "balance_ul": 600, "revision": 1}
    finally:
        gate.release.set()
    thread.join(timeout=15)

    resp = box["result"]
    assert resp.status_code == 201
    receipt = resp.json()
    # The receipt is the initial credential of this creation: revision 0 and
    # the full registered volume — not the post-split state a re-read would see.
    assert receipt["id"] == "root"
    assert receipt["balance_ul"] == 1000
    assert receipt["revision"] == 0
    assert receipt["created_at"]

    # The split did happen; both views are each internally consistent.
    root = httpx.get(f"{url}/tubes/root", timeout=30).json()
    assert root["balance_ul"] == 600 and root["revision"] == 1
    assert _total_balance(url) == 1000


# ---------------------------------------------------------------------------
# Lineage query: one snapshot for the whole chain
# ---------------------------------------------------------------------------


def test_ancestry_chain_is_one_snapshot_under_interleaved_two_level_splits(gated_server):
    url, gate = gated_server

    assert _post_tube(url, "root", 1000).status_code == 201
    s1 = _post_split(url, "root", 0, "s1", [("mid", 400), ("sib", 100)])
    assert s1.status_code == 201 and s1.json()["parent"]["revision"] == 1
    s2 = _post_split(url, "mid", 0, "s2", [("leaf", 150)])
    assert s2.status_code == 201 and s2.json()["parent"]["revision"] == 1
    # State now: root(500, rev1) -> mid(250, rev1) -> leaf(150, rev0); sib(100).

    # Park the ancestry query right after it has read the descendant ("leaf")
    # but before it walks up to the ancestors.
    gate.arm_once(
        lambda conn, sql, params: sql.startswith("SELECT * FROM tubes")
        and tuple(params) == ("leaf",)
    )

    thread, box = _run_in_thread(
        lambda: httpx.get(f"{url}/tubes/leaf/ancestry", timeout=30)
    )
    try:
        assert gate.entered.wait(timeout=15), "ancestry query never read the descendant"
        # Interleave: split the descendant, then each ancestor in turn.
        s3 = _post_split(url, "leaf", 0, "s3", [("tiny", 50)])
        assert s3.status_code == 201
        assert s3.json()["parent"] == {"id": "leaf", "balance_ul": 100, "revision": 1}
        s4 = _post_split(url, "mid", 1, "s4", [("mid2", 100)])
        assert s4.status_code == 201
        assert s4.json()["parent"] == {"id": "mid", "balance_ul": 150, "revision": 2}
        s5 = _post_split(url, "root", 1, "s5", [("r2", 200)])
        assert s5.status_code == 201
        assert s5.json()["parent"] == {"id": "root", "balance_ul": 300, "revision": 2}
    finally:
        gate.release.set()
    thread.join(timeout=15)

    resp = box["result"]
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["depth"] == 2
    chain = payload["chain"]
    assert [hop["tube"]["id"] for hop in chain] == ["root", "mid", "leaf"]
    # Every level comes from the snapshot taken when the query started — the
    # interleaved splits are invisible, so the balances are a combination that
    # really existed at one moment, not a stitch of different times.
    assert [(h["tube"]["balance_ul"], h["tube"]["revision"]) for h in chain] == [
        (500, 1),
        (250, 1),
        (150, 0),
    ]

    # Each hop's split evidence pins the parent revision it consumed, so an
    # auditor can tell exactly which parent-balance version it refers to.
    assert chain[0]["via"] is None
    via_mid, via_leaf = chain[1]["via"], chain[2]["via"]
    assert via_mid["parent_id"] == "root" and via_mid["amount_ul"] == 400
    assert via_mid["split"]["expected_revision"] == 0  # root was at revision 0 then
    assert via_leaf["parent_id"] == "mid" and via_leaf["amount_ul"] == 150
    assert via_leaf["split"]["expected_revision"] == 0  # mid was at revision 0 then

    # Conservation is recomputable from this single response: at the snapshot,
    # each tube's balance plus what its creation split distributed adds up.
    root, mid, leaf = (hop["tube"] for hop in chain)
    assert root["balance_ul"] + sum(c["amount_ul"] for c in via_mid["split"]["children"]) == 1000
    assert mid["balance_ul"] + sum(c["amount_ul"] for c in via_leaf["split"]["children"]) == 400
    assert leaf["balance_ul"] == via_leaf["amount_ul"]  # leaf had not split yet

    # Replaying one of the interleaved requests returns the original result
    # and does not deduct twice.
    replay = _post_split(url, "leaf", 0, "s3", [("tiny", 50)])
    assert replay.status_code == 201
    assert replay.json() == s3.json()
    assert _total_balance(url) == 1000

    # A fresh query sees the new committed state — again a self-consistent
    # combination, proving the first answer was a snapshot, not staleness.
    fresh = httpx.get(f"{url}/tubes/leaf/ancestry", timeout=30).json()
    assert [(h["tube"]["balance_ul"], h["tube"]["revision"]) for h in fresh["chain"]] == [
        (300, 2),
        (150, 2),
        (100, 1),
    ]

    # Full audit recompute over split evidence: for every tube on the chain,
    # current balance + everything it ever dispensed == its initial volume.
    for tube_id, initial in (("root", 1000), ("mid", 400), ("leaf", 150)):
        tube = httpx.get(f"{url}/tubes/{tube_id}", timeout=30).json()
        splits = httpx.get(f"{url}/tubes/{tube_id}/splits", timeout=30).json()["splits"]
        assert tube["revision"] == len(splits)
        assert tube["balance_ul"] + sum(s["total_amount_ul"] for s in splits) == initial
    assert _total_balance(url) == 1000


# ---------------------------------------------------------------------------
# Out-of-range volumes: stable input failure, nothing persisted
# ---------------------------------------------------------------------------


def test_register_balance_above_int64_is_a_stable_input_error(client):
    resp = client.post("/tubes", json={"id": "big", "balance_ul": INT64_MAX + 1})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # nothing polluted the persistent record
    assert client.get("/tubes/big").status_code == 404
    assert client.get("/tubes").json()["tubes"] == []


def test_register_balance_at_int64_max_is_still_legal(client):
    resp = client.post("/tubes", json={"id": "max", "balance_ul": INT64_MAX})
    assert resp.status_code == 201
    assert resp.json()["balance_ul"] == INT64_MAX
    assert resp.json()["revision"] == 0
    # and such a tube can be split normally
    split = client.post("/splits", json=make_split("max", 0, "k1", [("a", INT64_MAX - 1)]))
    assert split.status_code == 201
    assert split.json()["parent"]["balance_ul"] == 1


def test_split_child_amount_above_int64_rejected_and_key_not_burned(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = client.post("/splits", json=make_split("root", 0, "k1", [("a", INT64_MAX + 1)]))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # the failed request neither changed state nor consumed the request key
    fixed = client.post("/splits", json=make_split("root", 0, "k1", [("a", 40)]))
    assert fixed.status_code == 201
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 60 and root["revision"] == 1
