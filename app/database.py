import sqlite3
import time
import threading
from contextlib import closing
from .config import DB_PATH, MAX_ROWS, QUERY_SECONDS, MAX_CELL_CHARS

lock = threading.Lock()


def get_database_schema():
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        tables = ["customers", "orders", "refunds"]
        schema = []

        for table in tables:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = cursor.fetchall()

            column_descriptions = [
                f"{column[1]} {column[2]}"
                for column in columns
            ]

            schema.append(
                f"Table: {table}\n"
                + "\n".join(column_descriptions)
            )

        return "\n\n".join(schema)

def query(sql: str, params: tuple | list = ()) -> dict:
    """Execute one bounded, read-only SQLite statement and return JSON-safe evidence."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 12000:
        return {"error": "SQL must be a nonempty string of at most 12000 characters."}
    started = time.monotonic()
    try:
        uri = DB_PATH.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=QUERY_SECONDS)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1000000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
            conn.execute("PRAGMA query_only=ON")

            def authorizer(action, arg1, arg2, database, source):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                    if action == sqlite3.SQLITE_READ and (database != "main" or arg1 not in {"customers", "orders", "refunds"}):
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            conn.set_authorizer(authorizer)
            conn.set_progress_handler(lambda: int(time.monotonic() - started > QUERY_SECONDS), 1000)
            cursor = conn.execute(sql, params)
            if cursor.description is None:
                raise ValueError("Only queries returning rows are allowed")
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS + 1)
            truncated = len(rows) > MAX_ROWS
            cell_truncated = False
            remaining_chars = 100000
            safe_rows = []
            for row in rows[:MAX_ROWS]:
                safe_row = []
                for value in row:
                    if isinstance(value, bytes):
                        value = "[binary data omitted]"
                        cell_truncated = True
                    elif isinstance(value, str) and len(value) > MAX_CELL_CHARS:
                        value = value[:MAX_CELL_CHARS] + "..."
                        cell_truncated = True
                    if isinstance(value, str):
                        if len(value) > remaining_chars:
                            value = value[:remaining_chars]
                            cell_truncated = True
                        remaining_chars -= len(value)
                    safe_row.append(value)
                safe_rows.append(safe_row)
            return {"columns": columns, "rows": safe_rows, "truncated": truncated, "cells_truncated": cell_truncated}
    except (sqlite3.Error, ValueError, OSError) as exc:
        return {"error": str(exc)}