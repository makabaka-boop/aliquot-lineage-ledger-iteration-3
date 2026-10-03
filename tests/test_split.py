from conftest import make_split, total_balance


def test_basic_split(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    resp = client.post("/splits", json=make_split("root", 0, "k1", [("a", 300), ("b", 200)]))
    assert resp.status_code == 201
    body = resp.json()
    assert body["parent"] == {"id": "root", "balance_ul": 500, "revision": 1}
    assert body["total_amount_ul"] == 500
    assert [(c["id"], c["balance_ul"]) for c in body["children"]] == [("a", 300), ("b", 200)]

    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 500 and root["revision"] == 1
    assert client.get("/tubes/a").json()["parent_id"] == "root"
    assert client.get("/tubes/b").json()["balance_ul"] == 200
    assert total_balance(client) == 1000


def test_split_may_consume_entire_balance(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = client.post("/splits", json=make_split("root", 0, "k1", [("a", 100)]))
    assert resp.status_code == 201
    assert client.get("/tubes/root").json()["balance_ul"] == 0


def test_insufficient_balance_422_and_state_unchanged(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    resp = client.post("/splits", json=make_split("root", 0, "k1", [("a", 60), ("b", 50)]))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 100 and root["revision"] == 0
    assert client.get("/tubes/a").status_code == 404
    assert client.get("/tubes/root/splits").json()["splits"] == []


def test_stale_revision_412(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    client.post("/splits", json=make_split("root", 0, "k1", [("a", 10)]))
    resp = client.post("/splits", json=make_split("root", 0, "k2", [("b", 10)]))
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"


def test_unknown_parent_404(client):
    resp = client.post("/splits", json=make_split("ghost", 0, "k1", [("a", 10)]))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PARENT_NOT_FOUND"


def test_child_id_collision_409(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 100})
    client.post("/tubes", json={"id": "existing", "balance_ul": 5})
    resp = client.post("/splits", json=make_split("root", 0, "k1", [("existing", 10)]))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHILD_ID_EXISTS"
    # parent id itself is also an existing tube id
    resp = client.post("/splits", json=make_split("root", 0, "k2", [("root", 10)]))
    assert resp.status_code == 409


def test_split_validation_errors(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    base = make_split("root", 0, "k1", [("a", 10)])

    cases = []
    cases.append(dict(base, children=[]))                                   # 0 children
    cases.append(make_split("root", 0, "k1", [(f"c{i}", 1) for i in range(21)]))  # 21 children
    cases.append(make_split("root", 0, "k1", [("a", 10), ("a", 5)]))        # duplicate ids
    cases.append(make_split("root", 0, "k1", [("a", 0)]))                   # zero amount
    cases.append(make_split("root", 0, "k1", [("a", -3)]))                  # negative amount
    cases.append(make_split("root", 0, "k1", [("a", 2.5)]))                 # fractional amount
    cases.append(make_split("root", 0, "k1", [("a", "10")]))                # string amount
    cases.append(dict(base, unexpected=1))                                  # unknown top-level field
    bad_child = make_split("root", 0, "k1", [("a", 10)])
    bad_child["children"][0]["volume_note"] = "x"                           # unknown child field
    cases.append(bad_child)
    missing_key = dict(base)
    del missing_key["request_key"]                                          # missing request key
    cases.append(missing_key)
    cases.append(make_split("root", -1, "k1", [("a", 10)]))                 # negative revision

    for payload in cases:
        resp = client.post("/splits", json=payload)
        assert resp.status_code == 422, payload
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # nothing was applied
    root = client.get("/tubes/root").json()
    assert root["balance_ul"] == 1000 and root["revision"] == 0


def test_multi_level_split_conservation_and_ancestry(client):
    client.post("/tubes", json={"id": "root", "balance_ul": 1000})
    assert client.post("/splits", json=make_split("root", 0, "s1", [("mid", 400), ("sib", 100)])).status_code == 201
    assert client.post("/splits", json=make_split("mid", 0, "s2", [("leaf", 150), ("leaf2", 50)])).status_code == 201
    assert client.post("/splits", json=make_split("leaf", 0, "s3", [("tiny", 25)])).status_code == 201

    # conservation: every microlitre is still accounted for across all tubes
    assert total_balance(client) == 1000
    balances = {t["id"]: t["balance_ul"] for t in client.get("/tubes").json()["tubes"]}
    assert balances == {"root": 500, "mid": 200, "sib": 100, "leaf": 125, "leaf2": 50, "tiny": 25}

    # ancestry: root-first chain with the original split record at every hop
    payload = client.get("/tubes/tiny/ancestry").json()
    assert payload["depth"] == 3
    assert [hop["tube"]["id"] for hop in payload["chain"]] == ["root", "mid", "leaf", "tiny"]
    assert payload["chain"][0]["via"] is None
    hop_mid = payload["chain"][1]
    assert hop_mid["via"]["amount_ul"] == 400
    assert hop_mid["via"]["split"]["request_key"] == "s1"
    assert [c["id"] for c in hop_mid["via"]["split"]["children"]] == ["mid", "sib"]
    hop_tiny = payload["chain"][3]
    assert hop_tiny["via"]["parent_id"] == "leaf"
    assert hop_tiny["via"]["split"]["total_amount_ul"] == 25

    # per-tube split history
    history = client.get("/tubes/root/splits").json()
    assert len(history["splits"]) == 1
    assert history["splits"][0]["expected_revision"] == 0
    assert history["splits"][0]["children"][0] == {"id": "mid", "amount_ul": 400, "position": 0}
    assert client.get("/tubes/sib/splits").json()["splits"] == []
    assert client.get("/tubes/ghost/ancestry").status_code == 404
