from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=0.25,
    )
    connection.execute("PRAGMA busy_timeout=250")
    connection.execute("PRAGMA query_only=ON")
    return connection
