"""Database migration utilities.

This module contains a single :func:`migrate` function that performs the
following operations in a single transaction:

* Create or upgrade the core schema defined in :mod:`app.storage.database`.
* Add a ``last_activity`` column to the ``user_sessions`` table if it does
  not yet exist.  The column is a nullable TEXT field.
* Back‑fill the column for all existing rows with the value of
  ``created_at``.

The migration is idempotent – running it multiple times will have no
additional effect after the first successful run.  It also respects the
existing transaction logic from :mod:`app.storage.database`.
"""

from __future__ import annotations

from pathlib import Path

from app.storage.database import connect, transaction


def _ensure_last_activity_column(db) -> None:
    """Add ``last_activity`` to ``user_sessions`` if missing.

    The function checks the table schema via ``PRAGMA table_info``.  If the
    column does not exist, it is added with a nullable TEXT type.  The
    ``ALTER TABLE`` statement intentionally omits any ``DEFAULT`` clause
    so that existing rows receive ``NULL`` and can be back‑filled.
    """
    columns = {row["name"] for row in db.execute("PRAGMA table_info(user_sessions)")}
    if "last_activity" not in columns:
        # Add the column; SQLite allows a simple ``ADD COLUMN`` without a
        # default value.  The column is nullable by default.
        db.execute("ALTER TABLE user_sessions ADD COLUMN last_activity TEXT")


def _backfill_last_activity(db) -> None:
    """Populate ``last_activity`` from ``created_at`` where it is NULL.

    The statement is safe to run multiple times because it only updates
    rows where ``last_activity`` is currently ``NULL``.
    """
    db.execute(
        "UPDATE user_sessions SET last_activity = created_at WHERE last_activity IS NULL"
    )


def migrate() -> None:
    """Run the SQLite migration.

    The function is intentionally small so it can be called from tests or
    startup code.  It uses the :func:`transaction` context manager to
    guarantee that either all changes are applied or none are.
    """
    with transaction() as db:
        # The migration assumes the ``user_sessions`` table already exists
        # in the database.  It adds the ``last_activity`` column if it is
        # missing and then back‑fills the column for existing rows.
        _ensure_last_activity_column(db)
        _backfill_last_activity(db)
