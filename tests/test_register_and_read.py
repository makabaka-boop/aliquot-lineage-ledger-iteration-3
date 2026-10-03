import pytest


def test_register_and_get(client):
    resp = client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "root"
    assert body["balance_ul"] == 1000
    assert body["revision"] == 0

    got = client.get("/tubes/root")
    assert got.status_code == 200
    assert got.json()["balance_ul"] == 1000
    assert got.json()["parent_id"] is None


def test_register_duplicate_conflict(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    resp = client.post("/tubes", json={"id": "root", "balance_ul": 500})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TUBE_ALREADY_EXISTS"
    # original untouched
    assert client.get("/tubes/root").json()["balance_ul"] == 1000


def test_register_unknown_field_rejected(client):
    resp = client.post("/tubes", json={"id": "a", "balance_ul": 10, "operator": "bob"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad", [0, -5, 3.5, "10", True, None])
def test_register_invalid_balance_rejected(client, bad):
    resp = client.post("/tubes", json={"id": "a", "balance_ul": bad})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_unknown_tube_404(client):
    resp = client.get("/tubes/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TUBE_NOT_FOUND"


def test_history_endpoints_are_read_only(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    # no PUT/PATCH/DELETE anywhere: history cannot be rewritten via the API
    assert client.put("/splits", json={}).status_code == 405
    assert client.delete("/splits").status_code == 405
    assert client.patch("/tubes/root", json={}).status_code == 405
    assert client.delete("/tubes/root").status_code == 405
