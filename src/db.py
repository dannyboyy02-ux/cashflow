"""SQLite connection helper."""
import sqlite3

from src.config import SQLITE_PATH


def get_connection() -> sqlite3.Connection:
    """Open a connection to the project's SQLite database.

    Creates the parent directory if it doesn't exist. Configures row_factory
    so query results can be accessed by column name.
    """
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn