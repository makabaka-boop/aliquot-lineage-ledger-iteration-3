import json

from conftest import make_split, total_balance


def test_retry_same_key_same_body_returns_original(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    payload = make_split("root", 0, "retry-1", [("a", 300), ("b", 200)])

    first = client.post("/splits", json=payload)
    second = client.post("/splits", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    # the split happened exactly once
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 500 and root["revision"] == 1
    assert len(client.get("/tubes/root/splits").json()["splits"]) == 1
    assert total_balance(client) == 1000


def test_retry_with_reordered_json_keys_is_still_a_replay(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    first = client.post(
        "/splits",
        content=json.dumps(
            {"parent_id": "root", "expected_revision": 0, "request_key": "rk",
             "children": [{"id": "a", "amount_ul": 40}]}
        ),
        headers={"content-type": "application/json"},
    )
    # same semantics, different key order and child field order
    second = client.post(
        "/splits",
        content=json.dumps(
            {"children": [{"amount_ul": 40, "id": "a"}], "request_key": "rk",
             "expected_revision": 0, "parent_id": "root"}
        ),
        headers={"content-type": "application/json"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert client.get("/tubes/root").json()["balance_ul"] == 60


def test_same_key_different_body_409(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/splits", json=make_split("root", 0, "dup", [("a", 100)])).status_code == 201

    mutated = make_split("root", 0, "dup", [("a", 101)])
    resp = client.post("/splits", json=mutated)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REQUEST_KEY_CONFLICT"

    resp = client.post("/splits", json=make_split("root", 0, "dup", [("other", 100)]))
    assert resp.status_code == 409

    # nothing extra was applied
    assert client.get("/tubes/root").json()["balance_ul"] == 900
    assert len(client.get("/tubes/root/splits").json()["splits"]) == 1


def test_failed_request_does_not_burn_the_key(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    # stale revision -> 412, key must not be recorded
    resp = client.post("/splits", json=make_split("root", 5, "fixme", [("a", 10)]))
    assert resp.status_code == 412
    # same key with the corrected revision goes through
    resp = client.post("/splits", json=make_split("root", 0, "fixme", [("a", 10)]))
    assert resp.status_code == 201


def test_different_key_cannot_recreate_children(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    assert client.post("/splits", json=make_split("root", 0, "k1", [("a", 10)])).status_code == 201
    resp = client.post("/splits", json=make_split("root", 1, "k2", [("a", 10)]))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHILD_ID_EXISTS"
