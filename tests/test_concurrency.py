import threading

import httpx

from conftest import make_split


def _fire(url, payload, barrier, outcomes):
    barrier.wait(timeout=10)
    resp = httpx.post(f"{url}/splits", json=payload, timeout=30)
    outcomes.append((resp.status_code, resp.json()))


def _race(url, payloads):
    barrier = threading.Barrier(len(payloads))
    outcomes = []
    threads = [
        threading.Thread(target=_fire, args=(url, p, barrier, outcomes)) for p in payloads
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return outcomes


def test_two_connections_race_same_parent_same_revision(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 1000}).raise_for_status()
    # each split is well within the balance, so a 412 can only come from the
    # revision check: at most one writer may win with the stale revision
    outcomes = _race(
        server,
        [
            make_split("root", 0, "race-a", [("child-a", 400)]),
            make_split("root", 0, "race-b", [("child-b", 400)]),
        ],
    )

    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 412]
    loser = next(body for status, body in outcomes if status == 412)
    assert loser["error"]["code"] == "REVISION_CONFLICT"

    root = httpx.get(f"{server}/tubes/root").json()
    assert root["revision"] == 1 and root["balance_ul"] == 600
    tubes = httpx.get(f"{server}/tubes").json()["tubes"]
    assert sum(t["balance_ul"] for t in tubes) == 1000  # conservation
    assert len(httpx.get(f"{server}/tubes/root/splits").json()["splits"]) == 1


def test_many_connections_race_exactly_one_wins(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 5000}).raise_for_status()
    outcomes = _race(
        server,
        [make_split("root", 0, f"racer-{i}", [(f"kid-{i}", 100)]) for i in range(8)],
    )
    statuses = [status for status, _ in outcomes]
    assert statuses.count(201) == 1
    assert statuses.count(412) == 7
    root = httpx.get(f"{server}/tubes/root").json()
    assert root["revision"] == 1 and root["balance_ul"] == 4900
    tubes = httpx.get(f"{server}/tubes").json()["tubes"]
    assert sum(t["balance_ul"] for t in tubes) == 5000


def test_concurrent_identical_retry_is_applied_once(server):
    httpx.post(f"{server}/tubes", json={"id": "root", "balance_ul": 500}).raise_for_status()
    payload = make_split("root", 0, "dup-key", [("only-child", 100)])
    outcomes = _race(server, [payload, dict(payload)])

    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 201]
    bodies = [body for _, body in outcomes]
    assert bodies[0] == bodies[1]  # identical original result for both callers

    root = httpx.get(f"{server}/tubes/root").json()
    assert root["revision"] == 1 and root["balance_ul"] == 400
    assert len(httpx.get(f"{server}/tubes/root/splits").json()["splits"]) == 1
    tubes = httpx.get(f"{server}/tubes").json()["tubes"]
    assert sum(t["balance_ul"] for t in tubes) == 500
