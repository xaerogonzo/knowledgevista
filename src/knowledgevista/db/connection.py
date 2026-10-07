"""How every connection to the catalog is opened. One place, so a pragma cannot be forgotten on one path.

Why each setting:

  foreign_keys=ON   SQLite does NOT enforce foreign keys unless asked, per connection. A catalog that quietly
                    accepts orphan rows looks fine until a document points at an artifact that is not there.
  journal_mode=WAL  readers (GUI, MCP, CLI) coexist with one writer. The cost is that committed data can sit in
                    the `-wal` file, which is why a backup must never be a plain file copy (see migrations.py).
  busy_timeout      a second process waits briefly for the writer instead of failing at once. Writes are still
                    serialised: there is one logical writer, however many processes exist.
  isolation_level   None: the code issues BEGIN/COMMIT itself, so a transaction's boundaries are visible in the
                    source and never decided by the sqlite3 module's implicit rules.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the catalog. A read-only connection can never write, whatever the caller does."""
    target = Path(path)
    if read_only:
        connection = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection
