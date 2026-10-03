from __future__ import annotations

import sqlite3
from pathlib import Path

# tubes holds *current state* (balance, revision) and is mutable — except
# initial_ul, which is written once (at registration, at split-child creation,
# or by the one-time upgrade backfill) and then frozen by trigger.
# splits / split_children / lineage_edges / consumptions / quarantines /
# quarantine_releases are *history*: insert-only, enforced by triggers below
# so no code path (or manual SQL) can rewrite the past.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tubes (
    id          TEXT PRIMARY KEY,
    balance_ul  INTEGER NOT NULL CHECK (balance_ul >= 0),
    revision    INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS splits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id          TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    total_amount_ul    INTEGER NOT NULL CHECK (total_amount_ul > 0),
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_splits_parent ON splits (parent_id);

CREATE TABLE IF NOT EXISTS split_children (
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    child_id   TEXT NOT NULL REFERENCES tubes (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0),
    position   INTEGER NOT NULL,
    PRIMARY KEY (split_id, child_id)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    child_id   TEXT PRIMARY KEY REFERENCES tubes (id),
    parent_id  TEXT NOT NULL REFERENCES tubes (id),
    split_id   INTEGER NOT NULL REFERENCES splits (id),
    amount_ul  INTEGER NOT NULL CHECK (amount_ul > 0)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_key   TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    request_body  TEXT NOT NULL,
    response_body TEXT NOT NULL,
    split_id      INTEGER NOT NULL REFERENCES splits (id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumptions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tube_id            TEXT NOT NULL REFERENCES tubes (id),
    request_key        TEXT NOT NULL,
    expected_revision  INTEGER NOT NULL,
    amount_ul          INTEGER NOT NULL CHECK (amount_ul > 0),
    purpose            TEXT NOT NULL CHECK (length(purpose) > 0),
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumptions_tube ON consumptions (tube_id);

-- Consumption request keys live in their own table so the split key namespace
-- (and its stored records) is left exactly as it was.
CREATE TABLE IF NOT EXISTS consumption_keys (
    request_key    TEXT PRIMARY KEY,
    request_hash   TEXT NOT NULL,
    request_body   TEXT NOT NULL,
    response_body  TEXT NOT NULL,
    consumption_id INTEGER NOT NULL REFERENCES consumptions (id),
    created_at     TEXT NOT NULL
);

-- Quarantine filings are facts too: each record parks one tube (and, in
-- effect, its descendants) and stores reason / operator / target. A tube's
-- effective quarantine state is derived from the still-unreleased records on
-- itself and its ancestors — never stored on the tube row, so balances,
-- revisions and the lineage ledger are untouched by quarantine activity.
CREATE TABLE IF NOT EXISTS quarantines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tube_id     TEXT NOT NULL REFERENCES tubes (id),
    reason      TEXT NOT NULL CHECK (length(reason) > 0),
    operator    TEXT NOT NULL CHECK (length(operator) > 0),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantines_tube ON quarantines (tube_id);

-- A release lifts exactly one record (UNIQUE enforces at most one release per
-- record); other records on the same tube or its ancestors stay in effect.
CREATE TABLE IF NOT EXISTS quarantine_releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    quarantine_id  INTEGER NOT NULL UNIQUE REFERENCES quarantines (id),
    operator       TEXT NOT NULL CHECK (length(operator) > 0),
    created_at     TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS splits_no_update
BEFORE UPDATE ON splits
BEGIN SELECT RAISE(ABORT, 'splits history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS splits_no_delete
BEFORE DELETE ON splits
BEGIN SELECT RAISE(ABORT, 'splits history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS split_children_no_update
BEFORE UPDATE ON split_children
BEGIN SELECT RAISE(ABORT, 'split children history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS split_children_no_delete
BEFORE DELETE ON split_children
BEGIN SELECT RAISE(ABORT, 'split children history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS lineage_edges_no_update
BEFORE UPDATE ON lineage_edges
BEGIN SELECT RAISE(ABORT, 'lineage history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS lineage_edges_no_delete
BEFORE DELETE ON lineage_edges
BEGIN SELECT RAISE(ABORT, 'lineage history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS consumptions_no_update
BEFORE UPDATE ON consumptions
BEGIN SELECT RAISE(ABORT, 'consumption history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS consumptions_no_delete
BEFORE DELETE ON consumptions
BEGIN SELECT RAISE(ABORT, 'consumption history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS quarantines_no_update
BEFORE UPDATE ON quarantines
BEGIN SELECT RAISE(ABORT, 'quarantine history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS quarantines_no_delete
BEFORE DELETE ON quarantines
BEGIN SELECT RAISE(ABORT, 'quarantine history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS quarantine_releases_no_update
BEFORE UPDATE ON quarantine_releases
BEGIN SELECT RAISE(ABORT, 'quarantine release history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS quarantine_releases_no_delete
BEFORE DELETE ON quarantine_releases
BEGIN SELECT RAISE(ABORT, 'quarantine release history is immutable'); END;
"""

# initial_ul is frozen once set. The trigger is installed separately, only
# after the upgrade backfill below has run, because that backfill is itself an
# UPDATE of initial_ul.
FREEZE_INITIAL_UL = """
CREATE TRIGGER IF NOT EXISTS tubes_initial_ul_frozen
BEFORE UPDATE OF initial_ul ON tubes
BEGIN SELECT RAISE(ABORT, 'initial volume is frozen once set'); END;
"""


def _migrate_initial_ul(conn: sqlite3.Connection) -> None:
    """Add and backfill initial_ul on databases created before it existed.

    Roots recover their registered volume as current balance + everything ever
    split directly out of them. Old databases predate consumptions, so that
    sum is exactly the registered volume — the bare current balance would
    silently understate it. Children take the volume their creation split gave
    them, from the immutable lineage history. Runs once: afterwards the column
    exists and this is a no-op.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tubes)")}
    if "initial_ul" in cols:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "ALTER TABLE tubes ADD COLUMN initial_ul INTEGER CHECK (initial_ul > 0)"
        )
        conn.execute(
            """
            UPDATE tubes
               SET initial_ul = balance_ul + COALESCE((
                       SELECT SUM(s.total_amount_ul) FROM splits s
                       WHERE s.parent_id = tubes.id), 0)
             WHERE NOT EXISTS (
                       SELECT 1 FROM lineage_edges e WHERE e.child_id = tubes.id)
            """
        )
        conn.execute(
            """
            UPDATE tubes
               SET initial_ul = (
                       SELECT e.amount_ul FROM lineage_edges e
                       WHERE e.child_id = tubes.id)
             WHERE EXISTS (
                       SELECT 1 FROM lineage_edges e WHERE e.child_id = tubes.id)
            """
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transactions are opened explicitly
    # with BEGIN IMMEDIATE so the write lock is taken before any read.
    # check_same_thread=False: FastAPI may run dependency setup/teardown and
    # the endpoint itself on different threadpool threads; each connection is
    # still strictly confined to a single request.
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate_initial_ul(conn)
        conn.executescript(FREEZE_INITIAL_UL)
    finally:
        conn.close()
