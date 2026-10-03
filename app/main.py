from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db as dbmod
from .errors import ApiError
from .schemas import ConsumeRequest, RegisterTubeRequest, SplitRequest


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _tube_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "balance_ul": row["balance_ul"],
        "revision": row["revision"],
        "initial_ul": row["initial_ul"],
        "created_at": row["created_at"],
    }


def _consumption_record(row: sqlite3.Row) -> dict:
    return {
        "consumption_id": row["id"],
        "tube_id": row["tube_id"],
        "request_key": row["request_key"],
        "expected_revision": row["expected_revision"],
        "amount_ul": row["amount_ul"],
        "purpose": row["purpose"],
        "created_at": row["created_at"],
    }


def _split_record(conn: sqlite3.Connection, split_row: sqlite3.Row) -> dict:
    children = conn.execute(
        "SELECT child_id AS id, amount_ul, position FROM split_children "
        "WHERE split_id = ? ORDER BY position",
        (split_row["id"],),
    ).fetchall()
    return {
        "split_id": split_row["id"],
        "parent_id": split_row["parent_id"],
        "request_key": split_row["request_key"],
        "expected_revision": split_row["expected_revision"],
        "total_amount_ul": split_row["total_amount_ul"],
        "created_at": split_row["created_at"],
        "children": [dict(c) for c in children],
    }


def get_db(request: Request):
    conn = dbmod.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def create_app(db_path: str) -> FastAPI:
    dbmod.init_db(db_path)
    app = FastAPI(title="Sample Split Service", version="1.0.0")
    app.state.db_path = db_path

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "request failed validation",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # ---------------------------------------------------------------- tubes

    @app.post("/tubes", status_code=201)
    def register_tube(req: RegisterTubeRequest, conn: sqlite3.Connection = Depends(get_db)) -> dict:
        now = _utcnow()
        try:
            # initial_ul is frozen at registration: the volume this tube
            # started with, never rewritten afterwards (enforced by trigger).
            conn.execute(
                "INSERT INTO tubes (id, balance_ul, revision, created_at, initial_ul) "
                "VALUES (?, ?, 0, ?, ?)",
                (req.id, req.balance_ul, now, req.balance_ul),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "TUBE_ALREADY_EXISTS", f"tube {req.id!r} already exists")
        # The receipt is the state this insert committed — revision 0 and the
        # full registered volume. It must not be re-read from the database:
        # a split from another request could commit between the INSERT and a
        # re-SELECT, and the response would then describe a state this
        # creation never produced.
        return {
            "id": req.id,
            "balance_ul": req.balance_ul,
            "revision": 0,
            "initial_ul": req.balance_ul,
            "created_at": now,
        }

    @app.get("/tubes")
    def list_tubes(conn: sqlite3.Connection = Depends(get_db)) -> dict:
        rows = conn.execute("SELECT * FROM tubes ORDER BY rowid").fetchall()
        return {"tubes": [_tube_view(r) for r in rows]}

    @app.get("/tubes/{tube_id}")
    def get_tube(tube_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
        row = conn.execute("SELECT * FROM tubes WHERE id = ?", (tube_id,)).fetchone()
        if row is None:
            raise ApiError(404, "TUBE_NOT_FOUND", f"tube {tube_id!r} does not exist")
        view = _tube_view(row)
        edge = conn.execute(
            "SELECT parent_id FROM lineage_edges WHERE child_id = ?", (tube_id,)
        ).fetchone()
        view["parent_id"] = edge["parent_id"] if edge else None
        return view

    @app.get("/tubes/{tube_id}/ancestry")
    def get_ancestry(tube_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
        # The whole walk runs inside one explicit read transaction, so every
        # level of the chain is read from the same snapshot. Without it each
        # SELECT is its own snapshot (autocommit) and concurrent splits of
        # the descendant and its ancestors could be stitched into a balance
        # combination that never existed at any single moment. In WAL mode a
        # read transaction does not block writers. Each hop's split record
        # carries expected_revision, pinning the exact parent revision (and
        # thus the parent balance version) the split consumed.
        conn.execute("BEGIN")
        try:
            # Walk parent pointers up to the root, then reverse -> root-first chain.
            chain = []
            current_id = tube_id
            while True:
                row = conn.execute("SELECT * FROM tubes WHERE id = ?", (current_id,)).fetchone()
                if row is None:
                    raise ApiError(404, "TUBE_NOT_FOUND", f"tube {current_id!r} does not exist")
                edge = conn.execute(
                    "SELECT parent_id, split_id, amount_ul FROM lineage_edges WHERE child_id = ?",
                    (current_id,),
                ).fetchone()
                via = None
                if edge is not None:
                    split_row = conn.execute(
                        "SELECT * FROM splits WHERE id = ?", (edge["split_id"],)
                    ).fetchone()
                    via = {
                        "parent_id": edge["parent_id"],
                        "amount_ul": edge["amount_ul"],
                        "split": _split_record(conn, split_row),
                    }
                chain.append({"tube": _tube_view(row), "via": via})
                if edge is None:
                    break
                current_id = edge["parent_id"]
            chain.reverse()
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        conn.execute("COMMIT")
        return {"tube_id": tube_id, "depth": len(chain) - 1, "chain": chain}

    @app.get("/tubes/{tube_id}/splits")
    def get_tube_splits(tube_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
        row = conn.execute("SELECT id FROM tubes WHERE id = ?", (tube_id,)).fetchone()
        if row is None:
            raise ApiError(404, "TUBE_NOT_FOUND", f"tube {tube_id!r} does not exist")
        splits = conn.execute(
            "SELECT * FROM splits WHERE parent_id = ? ORDER BY id", (tube_id,)
        ).fetchall()
        return {"tube_id": tube_id, "splits": [_split_record(conn, s) for s in splits]}

    @app.get("/tubes/{tube_id}/conservation")
    def get_conservation(tube_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
        # Conservation audit over the whole subtree of a root tube: current
        # balances of the root and every descendant, all consumption vouchers
        # issued anywhere in the subtree, and the root's frozen initial
        # volume — all read inside one explicit transaction, so the audit
        # verifies a combination that really existed at a single moment.
        conn.execute("BEGIN")
        try:
            root = conn.execute("SELECT * FROM tubes WHERE id = ?", (tube_id,)).fetchone()
            if root is None:
                raise ApiError(404, "TUBE_NOT_FOUND", f"tube {tube_id!r} does not exist")
            edge = conn.execute(
                "SELECT 1 FROM lineage_edges WHERE child_id = ?", (tube_id,)
            ).fetchone()
            if edge is not None:
                raise ApiError(
                    422,
                    "NOT_A_ROOT",
                    f"tube {tube_id!r} is not a root tube; audit its root instead",
                )
            tubes = conn.execute(
                """
                WITH RECURSIVE subtree(id) AS (
                    SELECT ?
                    UNION
                    SELECT e.child_id
                      FROM lineage_edges e
                      JOIN subtree s ON e.parent_id = s.id
                )
                SELECT t.* FROM tubes t
                JOIN subtree s ON t.id = s.id
                ORDER BY t.rowid
                """,
                (tube_id,),
            ).fetchall()
            tube_ids = [t["id"] for t in tubes]
            placeholders = ", ".join("?" for _ in tube_ids)
            consumptions = conn.execute(
                f"SELECT * FROM consumptions WHERE tube_id IN ({placeholders}) ORDER BY id",
                tube_ids,
            ).fetchall()
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        conn.execute("COMMIT")
        total_balance = sum(t["balance_ul"] for t in tubes)
        total_consumed = sum(c["amount_ul"] for c in consumptions)
        return {
            "root_id": tube_id,
            "initial_ul": root["initial_ul"],
            "tubes": [_tube_view(t) for t in tubes],
            "consumptions": [_consumption_record(c) for c in consumptions],
            "total_balance_ul": total_balance,
            "total_consumed_ul": total_consumed,
            "conserved": total_balance + total_consumed == root["initial_ul"],
        }

    # ---------------------------------------------------------------- split

    @app.post("/splits", status_code=201)
    def split_tube(req: SplitRequest, conn: sqlite3.Connection = Depends(get_db)):
        body = req.model_dump(mode="json")
        canonical = _canonical(body)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            # BEGIN IMMEDIATE takes the database write lock up front, so the
            # check-then-act sequence below is serialised against every other
            # split: a concurrent request on the same parent either waits and
            # then sees the bumped revision (412), or replays the stored
            # idempotent response.
            conn.execute("BEGIN IMMEDIATE")

            replay = conn.execute(
                "SELECT request_hash, response_body FROM idempotency_keys WHERE request_key = ?",
                (req.request_key,),
            ).fetchone()
            if replay is not None:
                if replay["request_hash"] != digest:
                    raise ApiError(
                        409,
                        "REQUEST_KEY_CONFLICT",
                        f"request_key {req.request_key!r} was already used with a different body",
                    )
                conn.execute("COMMIT")
                return JSONResponse(status_code=201, content=json.loads(replay["response_body"]))

            parent = conn.execute(
                "SELECT * FROM tubes WHERE id = ?", (req.parent_id,)
            ).fetchone()
            if parent is None:
                raise ApiError(404, "PARENT_NOT_FOUND", f"parent tube {req.parent_id!r} does not exist")
            if parent["revision"] != req.expected_revision:
                raise ApiError(
                    412,
                    "REVISION_CONFLICT",
                    f"parent {req.parent_id!r} is at revision {parent['revision']}, "
                    f"not {req.expected_revision}",
                )

            total = sum(c.amount_ul for c in req.children)
            if total > parent["balance_ul"]:
                raise ApiError(
                    422,
                    "INSUFFICIENT_BALANCE",
                    f"children total {total} uL exceeds parent balance "
                    f"{parent['balance_ul']} uL",
                )

            placeholders = ", ".join("?" for _ in req.children)
            clashes = conn.execute(
                f"SELECT id FROM tubes WHERE id IN ({placeholders})",
                [c.id for c in req.children],
            ).fetchall()
            if clashes:
                taken = ", ".join(sorted(r["id"] for r in clashes))
                raise ApiError(409, "CHILD_ID_EXISTS", f"child id(s) already exist: {taken}")

            now = _utcnow()
            new_balance = parent["balance_ul"] - total
            cur = conn.execute(
                "UPDATE tubes SET balance_ul = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (new_balance, req.parent_id, req.expected_revision),
            )
            if cur.rowcount != 1:  # unreachable under the write lock; defence in depth
                raise ApiError(412, "REVISION_CONFLICT", f"parent {req.parent_id!r} changed concurrently")

            cur = conn.execute(
                "INSERT INTO splits (parent_id, request_key, expected_revision, "
                "total_amount_ul, created_at) VALUES (?, ?, ?, ?, ?)",
                (req.parent_id, req.request_key, req.expected_revision, total, now),
            )
            split_id = cur.lastrowid
            for position, child in enumerate(req.children):
                conn.execute(
                    "INSERT INTO tubes (id, balance_ul, revision, created_at, initial_ul) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (child.id, child.amount_ul, now, child.amount_ul),
                )
                conn.execute(
                    "INSERT INTO split_children (split_id, child_id, amount_ul, position) "
                    "VALUES (?, ?, ?, ?)",
                    (split_id, child.id, child.amount_ul, position),
                )
                conn.execute(
                    "INSERT INTO lineage_edges (child_id, parent_id, split_id, amount_ul) "
                    "VALUES (?, ?, ?, ?)",
                    (child.id, req.parent_id, split_id, child.amount_ul),
                )

            response = {
                "split_id": split_id,
                "request_key": req.request_key,
                "parent": {
                    "id": parent["id"],
                    "balance_ul": new_balance,
                    "revision": req.expected_revision + 1,
                },
                "children": [
                    {"id": c.id, "balance_ul": c.amount_ul, "revision": 0} for c in req.children
                ],
                "total_amount_ul": total,
                "created_at": now,
            }
            conn.execute(
                "INSERT INTO idempotency_keys (request_key, request_hash, request_body, "
                "response_body, split_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (req.request_key, digest, canonical, json.dumps(response, ensure_ascii=False),
                 split_id, now),
            )
            conn.execute("COMMIT")
            return response
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # ------------------------------------------------------------ consume

    @app.post("/consumptions", status_code=201)
    def consume_tube(req: ConsumeRequest, conn: sqlite3.Connection = Depends(get_db)):
        body = req.model_dump(mode="json")
        canonical = _canonical(body)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            # Same serialisation discipline as splits: BEGIN IMMEDIATE takes
            # the write lock before any read, so a consumption and a split (or
            # two consumptions) contending for the same revision are ordered —
            # the loser sees the bumped revision and gets 412.
            conn.execute("BEGIN IMMEDIATE")

            replay = conn.execute(
                "SELECT request_hash, response_body FROM consumption_keys "
                "WHERE request_key = ?",
                (req.request_key,),
            ).fetchone()
            if replay is not None:
                if replay["request_hash"] != digest:
                    raise ApiError(
                        409,
                        "REQUEST_KEY_CONFLICT",
                        f"request_key {req.request_key!r} was already used with a different body",
                    )
                conn.execute("COMMIT")
                return JSONResponse(status_code=201, content=json.loads(replay["response_body"]))

            tube = conn.execute(
                "SELECT * FROM tubes WHERE id = ?", (req.tube_id,)
            ).fetchone()
            if tube is None:
                raise ApiError(404, "TUBE_NOT_FOUND", f"tube {req.tube_id!r} does not exist")
            if tube["revision"] != req.expected_revision:
                raise ApiError(
                    412,
                    "REVISION_CONFLICT",
                    f"tube {req.tube_id!r} is at revision {tube['revision']}, "
                    f"not {req.expected_revision}",
                )
            if req.amount_ul > tube["balance_ul"]:
                raise ApiError(
                    422,
                    "INSUFFICIENT_BALANCE",
                    f"amount {req.amount_ul} uL exceeds tube balance "
                    f"{tube['balance_ul']} uL",
                )

            now = _utcnow()
            new_balance = tube["balance_ul"] - req.amount_ul
            cur = conn.execute(
                "UPDATE tubes SET balance_ul = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (new_balance, req.tube_id, req.expected_revision),
            )
            if cur.rowcount != 1:  # unreachable under the write lock; defence in depth
                raise ApiError(412, "REVISION_CONFLICT", f"tube {req.tube_id!r} changed concurrently")

            # The deduction, the revision bump and the immutable consumption
            # fact commit in this one transaction — a failure anywhere rolls
            # all of it back, so no half-written voucher can survive.
            cur = conn.execute(
                "INSERT INTO consumptions (tube_id, request_key, expected_revision, "
                "amount_ul, purpose, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (req.tube_id, req.request_key, req.expected_revision,
                 req.amount_ul, req.purpose, now),
            )
            consumption_id = cur.lastrowid

            response = {
                "consumption_id": consumption_id,
                "request_key": req.request_key,
                "tube": {
                    "id": tube["id"],
                    "balance_ul": new_balance,
                    "revision": req.expected_revision + 1,
                },
                "amount_ul": req.amount_ul,
                "purpose": req.purpose,
                "created_at": now,
            }
            conn.execute(
                "INSERT INTO consumption_keys (request_key, request_hash, request_body, "
                "response_body, consumption_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (req.request_key, digest, canonical, json.dumps(response, ensure_ascii=False),
                 consumption_id, now),
            )
            conn.execute("COMMIT")
            return response
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    return app


app = create_app(os.environ.get("DATABASE_PATH", "data/lab.db"))
