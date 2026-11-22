"""In-process memory and session management for the voice-agent bridge.

Stores caller sessions, long-term memory (facts) and daily notes in one
SQLite database (``DATA_DIR/bridge.db``) in WAL mode, using only the standard
library ``sqlite3`` module so the bridge runs without an external database.

Data at rest is encrypted with Fernet when ``DATA_ENCRYPTION_KEY`` is set.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TypedDict

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATA_DIR: Path = Path(os.getenv("DATA_DIR", "./data"))
_DATA_ENCRYPTION_KEY: str | None = os.getenv("DATA_ENCRYPTION_KEY")
_fernet: Fernet | None = Fernet(_DATA_ENCRYPTION_KEY.encode()) if _DATA_ENCRYPTION_KEY else None

DB_FILENAME = "bridge.db"
_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    phone_hash TEXT PRIMARY KEY,
    record     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_hash TEXT NOT NULL,
    fact       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_phone_hash ON memories (phone_hash, id);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL NOT NULL,
    note       TEXT NOT NULL,
    phone_hash TEXT
);
CREATE INDEX IF NOT EXISTS notes_phone_hash ON notes (phone_hash, id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ── Type definitions ────────────────────────────────────────────────────────


class Session(TypedDict, total=False):
    """Represents a single caller session."""

    session_id: str
    phone_hash: str
    caller_name: str
    call_count: int
    first_seen: float
    last_seen: float
    last_call_sid: str
    active: bool


class MemoryStore(TypedDict, total=False):
    """Long-term facts stored per caller."""

    phone_hash: str
    facts: list[str]


class Note(TypedDict):
    """A daily / global context note."""

    timestamp: float
    note: str
    phone_hash: str | None


class StorageError(RuntimeError):
    """The database could not be read or written, so the change was not saved."""


# ── Database connection ─────────────────────────────────────────────────────


def db_path() -> Path:
    """Return the path to the SQLite database."""
    return DATA_DIR / DB_FILENAME


_conn_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def _open_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the file owner-only; SQLite gives the -wal and -shm files the same mode.
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)
    conn = sqlite3.connect(
        path,
        timeout=_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StorageError(f"{path} could not be switched to WAL mode (got {mode!r})")
        conn.execute("PRAGMA synchronous = FULL")
        # Overwrite freed space so replaced plaintext doesn't linger in the file.
        conn.execute("PRAGMA secure_delete = ON")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    except BaseException:
        conn.close()
        raise
    return conn


def _connection() -> sqlite3.Connection:
    """Return the shared connection for the current ``DATA_DIR``, opening it if needed."""
    global _conn, _conn_path  # noqa: PLW0603
    path = db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    close_db()
    _conn = _open_connection(path)
    _conn_path = path
    return _conn


def close_db() -> None:
    """Close the shared connection, if one is open."""
    global _conn, _conn_path  # noqa: PLW0603
    with _conn_lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _conn_path = None


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Run a write transaction on the shared connection.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so a read-modify-write
    never races another process; the thread lock serializes this process's
    callers on the one connection. Any SQLite failure is raised as
    ``StorageError`` and the transaction is rolled back.
    """
    with _conn_lock:
        try:
            conn = _connection()
            conn.execute("BEGIN IMMEDIATE")
        except (sqlite3.Error, OSError) as exc:
            raise StorageError(f"Failed to open {db_path()}: {exc}") from exc
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise StorageError(f"Failed to write {db_path()}: {exc}") from exc
            if isinstance(exc, (AttributeError, KeyError, TypeError, ValueError)):
                raise StorageError(f"{db_path()} has a malformed record: {exc!r}") from exc
            raise


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    with _conn_lock:
        try:
            return _connection().execute(sql, params).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise StorageError(f"Failed to read {db_path()}: {exc}") from exc


# ── Encryption helpers ──────────────────────────────────────────────────────


def _encrypt(data: str) -> str:
    """Encrypt *data* with Fernet if a key is configured."""
    if not _fernet:
        return data
    return _fernet.encrypt(data.encode("utf-8")).decode("utf-8")


_FERNET_TOKEN_PREFIX = "gAAAAA"


def _decrypt(data: str) -> str:
    """Decrypt *data* with Fernet if a key is configured."""
    if not _fernet:
        return data
    if not data.startswith(_FERNET_TOKEN_PREFIX):
        # Written before encryption was enabled; Fernet tokens always carry this prefix.
        return data
    try:
        return _fernet.decrypt(data.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.error("Failed to decrypt data — key mismatch or corruption")
        return "[DECRYPTION_FAILED]"


def _seal_session(session: dict) -> str:
    """Serialize a session record, encrypting it whole when a key is configured."""
    if not _fernet:
        return json.dumps(session)
    return json.dumps({"enc": _encrypt(json.dumps(session))})


def _is_sealed(value) -> bool:
    return isinstance(value, dict) and set(value) == {"enc"}


def _open_session(record: str) -> dict | None:
    """Return the session stored in *record*, or ``None`` if it can't be read."""
    try:
        value = json.loads(record)
        if _is_sealed(value):
            value = json.loads(_decrypt(value["enc"]))
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to decrypt a session record — key mismatch or corruption")
        return None
    return value if isinstance(value, dict) else None


# ── Startup ─────────────────────────────────────────────────────────────────


def ensure_data_dir() -> None:
    """Create the data directory if it does not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Set restrictive permissions: owner read/write/execute only
    os.chmod(DATA_DIR, 0o700)
    logger.info("Data directory ready: %s", DATA_DIR.resolve())


def init_db() -> int:
    """Open the database, check its integrity and import any v1.2 JSON files.

    Runs at startup. Raises ``RuntimeError`` if the database fails
    ``PRAGMA integrity_check`` or a legacy JSON file can't be parsed, so the
    bridge doesn't start on damaged data. Returns the number of records
    imported from JSON.
    """
    with _conn_lock:
        try:
            result = _connection().execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"{db_path()} is not a readable SQLite database ({exc}). "
                "Restore it from a backup or move it out of DATA_DIR, then restart."
            ) from exc
    problems = [row[0] for row in result if row[0] != "ok"]
    if problems:
        raise RuntimeError(
            f"{db_path()} failed PRAGMA integrity_check: {'; '.join(problems[:5])}. "
            "Restore it from a backup or move it out of DATA_DIR, then restart."
        )
    return _import_legacy_json()


_LEGACY_FILES = ("sessions.json", "memories.json", "notes.json")


def _load_legacy(path: Path, expected: type) -> dict | list | None:
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        return expected()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{path} is not valid JSON, so it cannot be imported into {DB_FILENAME}. "
            "Restore it from a backup or move it out of DATA_DIR, then restart."
        ) from exc
    if data == {}:
        data = expected()
    if not isinstance(data, expected):
        raise RuntimeError(
            f"{path} holds a JSON {type(data).__name__}, expected {expected.__name__}. "
            "Restore it from a backup or move it out of DATA_DIR, then restart."
        )
    return data


def _import_legacy_json() -> int:
    """Move sessions.json, memories.json and notes.json into the database.

    All three files are imported in one transaction, then each is renamed to
    ``<name>.migrated`` so it is kept as a backup and never imported twice.
    """
    paths = {name: DATA_DIR / name for name in _LEGACY_FILES}
    if not any(p.exists() for p in paths.values()):
        return 0
    sessions = _load_legacy(paths["sessions.json"], dict) or {}
    memories = _load_legacy(paths["memories.json"], dict) or {}
    notes = _load_legacy(paths["notes.json"], list) or []

    try:
        session_rows = [(phone_hash, json.dumps(value)) for phone_hash, value in sessions.items()]
        memory_rows = [
            (phone_hash, str(fact))
            for phone_hash, store in memories.items()
            for fact in (store or {}).get("facts", [])
        ]
        note_rows = [
            (float(entry.get("timestamp", 0)), str(entry.get("note", "")), entry.get("phone_hash"))
            for entry in notes
        ]
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"A JSON file in {DATA_DIR} has a malformed record ({exc!r}), so nothing was "
            "imported. Fix it or move it out of DATA_DIR, then restart."
        ) from exc

    with _transaction() as conn:
        if conn.execute("SELECT 1 FROM meta WHERE key = 'legacy_json_imported'").fetchone():
            # A crash after the commit left the files behind; don't import them twice.
            logger.warning("JSON files in %s were already imported; renaming them", DATA_DIR)
            session_rows, memory_rows, note_rows = [], [], []
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_json_imported', ?)",
            (str(time.time()),),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO sessions (phone_hash, record) VALUES (?, ?)", session_rows
        )
        conn.executemany("INSERT INTO memories (phone_hash, fact) VALUES (?, ?)", memory_rows)
        conn.executemany(
            "INSERT INTO notes (timestamp, note, phone_hash) VALUES (?, ?, ?)", note_rows
        )
    imported = len(session_rows) + len(memory_rows) + len(note_rows)

    for path in paths.values():
        if path.exists():
            path.replace(path.with_name(path.name + ".migrated"))
        lock = path.with_name(path.name + ".lock")
        if lock.exists():
            lock.unlink()
    logger.info("Imported %d records from JSON files into %s", imported, db_path())
    return imported


def encrypt_plaintext_entries() -> int:
    """Encrypt sessions, facts and notes written before encryption was enabled.

    Runs at startup when a key is configured. Values already carrying the Fernet
    prefix are left alone. Returns the number of entries encrypted; raises if
    the database can't be read or written.
    """
    if not _fernet:
        return 0
    migrated = 0
    with _transaction() as conn:
        for phone_hash, record in conn.execute("SELECT phone_hash, record FROM sessions").fetchall():
            try:
                value = json.loads(record)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"The session record for {phone_hash[:8]} is not valid JSON, so it "
                    "cannot be encrypted."
                ) from exc
            if not _is_sealed(value):
                conn.execute(
                    "UPDATE sessions SET record = ? WHERE phone_hash = ?",
                    (_seal_session(value), phone_hash),
                )
                migrated += 1
        for table, column in (("memories", "fact"), ("notes", "note")):
            rows = conn.execute(
                f"SELECT id, {column} FROM {table} WHERE substr({column}, 1, ?) != ?",
                (len(_FERNET_TOKEN_PREFIX), _FERNET_TOKEN_PREFIX),
            ).fetchall()
            for row_id, value in rows:
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?", (_encrypt(value), row_id)
                )
            migrated += len(rows)
    if migrated:
        _checkpoint()
    return migrated


def _checkpoint() -> None:
    """Copy the WAL into the database and truncate it, dropping old page versions."""
    with _conn_lock:
        try:
            _connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to checkpoint {db_path()}: {exc}") from exc


# ── Public API ──────────────────────────────────────────────────────────────


def load_session(phone_hash: str) -> Session:
    """Load an existing session or create a new one for *phone_hash*.

    Returns a ``Session`` dict.  The ``call_count`` is incremented each time
    this function is called (i.e., per inbound call).
    """
    try:
        with _transaction() as conn:
            row = conn.execute(
                "SELECT record FROM sessions WHERE phone_hash = ?", (phone_hash,)
            ).fetchone()
            session, unreadable = _touch_session(row[0] if row else None, phone_hash)
            if not unreadable:
                # Leave an undecryptable record alone so restoring the right key recovers it.
                conn.execute(
                    "INSERT OR REPLACE INTO sessions (phone_hash, record) VALUES (?, ?)",
                    (phone_hash, _seal_session(session)),
                )
            return session
    except StorageError as exc:
        # The call still goes ahead, without persisted history.
        logger.error("%s; using a temporary session", exc)
        return _touch_session(None, phone_hash)[0]


def _touch_session(record: str | None, phone_hash: str) -> tuple[Session, bool]:
    now = time.time()
    session = _open_session(record) if record is not None else None
    unreadable = record is not None and session is None
    if unreadable:
        logger.warning(
            "Session record for %s can't be decrypted (wrong or missing key); "
            "keeping it and using a temporary session",
            phone_hash[:8],
        )

    if session is not None:
        session["call_count"] = session.get("call_count", 0) + 1
        session["last_seen"] = now
        session["active"] = True
        logger.info(
            "Returning caller %s — call #%d",
            phone_hash[:8],
            session["call_count"],
        )
    else:
        session = Session(
            session_id=f"{phone_hash[:12]}_{int(now)}",
            phone_hash=phone_hash,
            caller_name="",
            call_count=1,
            first_seen=now,
            last_seen=now,
            last_call_sid="",
            active=True,
        )
        logger.info("New caller %s", phone_hash[:8])
    return session, unreadable  # type: ignore[return-value]


def end_session(phone_hash: str, call_sid: str) -> None:
    """Mark the session for *phone_hash* as inactive after a call ends."""
    try:
        with _transaction() as conn:
            row = conn.execute(
                "SELECT record FROM sessions WHERE phone_hash = ?", (phone_hash,)
            ).fetchone()
            session = _open_session(row[0]) if row else None
            if session is not None:
                session["active"] = False
                session["last_call_sid"] = call_sid
                conn.execute(
                    "UPDATE sessions SET record = ? WHERE phone_hash = ?",
                    (_seal_session(session), phone_hash),
                )
    except StorageError as exc:
        logger.error("%s; session end not recorded", exc)


def get_memories(phone_hash: str) -> list[str]:
    """Return the stored facts for *phone_hash*, oldest first."""
    try:
        rows = _query("SELECT fact FROM memories WHERE phone_hash = ? ORDER BY id", (phone_hash,))
    except StorageError as exc:
        logger.warning("%s", exc)
        return []
    return [_decrypt(fact) for (fact,) in rows]


def add_memory(phone_hash: str, fact: str) -> list[str]:
    """Append *fact* to long-term memory for *phone_hash*.

    Returns the caller's facts as stored (encrypted when a key is configured).
    """
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO memories (phone_hash, fact) VALUES (?, ?)", (phone_hash, _encrypt(fact))
        )
        facts = [
            stored
            for (stored,) in conn.execute(
                "SELECT fact FROM memories WHERE phone_hash = ? ORDER BY id", (phone_hash,)
            )
        ]
    logger.info("Stored fact for %s (total: %d)", phone_hash[:8], len(facts))
    return facts


def get_notes(phone_hash: str | None = None) -> list[Note]:
    """Return notes, optionally filtered by *phone_hash*, oldest first.

    Global notes (``phone_hash is None``) are always included.
    """
    try:
        rows = _query(
            "SELECT timestamp, note, phone_hash FROM notes "
            "WHERE phone_hash IS NULL OR phone_hash = ? ORDER BY id",
            (phone_hash,),
        )
    except StorageError as exc:
        logger.warning("%s", exc)
        return []
    return [Note(timestamp=ts, note=_decrypt(note), phone_hash=ph) for ts, note, ph in rows]


def add_note(note: str, phone_hash: str | None = None) -> Note:
    """Create a new note and persist it.

    If *phone_hash* is provided the note is scoped to that caller; otherwise
    it is a global/daily note visible to all callers.
    """
    entry = Note(timestamp=time.time(), note=note, phone_hash=phone_hash)
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO notes (timestamp, note, phone_hash) VALUES (?, ?, ?)",
            (entry["timestamp"], _encrypt(note), phone_hash),
        )
    logger.info("Added note (global=%s)", phone_hash is None)
    return entry


def backup(dest: str | Path) -> Path:
    """Write a consistent copy of the live database to *dest* with ``VACUUM INTO``.

    Safe while the bridge is serving calls, unlike copying ``bridge.db`` and
    its ``-wal`` file. *dest* must not exist yet.
    """
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    with _conn_lock:
        try:
            _connection().execute("VACUUM INTO ?", (str(dest),))
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to back up {db_path()} to {dest}: {exc}") from exc
    os.chmod(dest, 0o600)
    return dest


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "backup":
        sys.exit("usage: python memory.py backup <dest.db>")
    logging.basicConfig(level=logging.INFO)
    print(backup(sys.argv[2]))
