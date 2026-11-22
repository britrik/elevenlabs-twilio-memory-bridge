"""Tests for the SQLite storage layer: WAL setup, JSON import, integrity and backups."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
import stat

import pytest
from cryptography.fernet import Fernet

import app as app_module
import memory as memory_module


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(memory_module, "_fernet", None)
    yield tmp_path
    memory_module.close_db()


def test_database_uses_wal_busy_timeout_and_owner_only_permissions(data_dir):
    memory_module.add_memory("hash1", "likes tea")
    conn = memory_module._connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == memory_module._BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA user_version").fetchone()[0] == memory_module._SCHEMA_VERSION
    for path in data_dir.glob("bridge.db*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def _legacy_files(data_dir):
    (data_dir / "sessions.json").write_text(json.dumps({
        "hash1": {"session_id": "s1", "phone_hash": "hash1", "caller_name": "Ann", "call_count": 3},
    }))
    (data_dir / "memories.json").write_text(json.dumps({
        "hash1": {"phone_hash": "hash1", "facts": ["likes jazz", "prefers mornings"]},
    }))
    (data_dir / "notes.json").write_text(json.dumps([
        {"timestamp": 1.0, "note": "global note", "phone_hash": None},
        {"timestamp": 2.0, "note": "caller note", "phone_hash": "hash1"},
        {"timestamp": 3.0, "note": "other caller", "phone_hash": "hash2"},
    ]))
    (data_dir / "sessions.json.lock").write_text("")


def test_init_db_imports_v1_2_json_once_and_keeps_the_files(data_dir):
    _legacy_files(data_dir)
    assert memory_module.init_db() == 6
    assert memory_module.init_db() == 0

    assert memory_module.get_memories("hash1") == ["likes jazz", "prefers mornings"]
    assert [n["note"] for n in memory_module.get_notes("hash1")] == ["global note", "caller note"]
    session = memory_module.load_session("hash1")
    assert session["caller_name"] == "Ann" and session["call_count"] == 4
    for name in ("sessions.json", "memories.json", "notes.json"):
        assert not (data_dir / name).exists()
        assert (data_dir / f"{name}.migrated").exists()
    assert not (data_dir / "sessions.json.lock").exists()


def test_files_left_behind_after_an_import_are_not_imported_twice(data_dir):
    _legacy_files(data_dir)
    memory_module.init_db()
    _legacy_files(data_dir)
    assert memory_module.init_db() == 0
    assert memory_module.get_memories("hash1") == ["likes jazz", "prefers mornings"]
    assert (data_dir / "memories.json.migrated").exists()


def test_malformed_json_record_imports_nothing(data_dir):
    _legacy_files(data_dir)
    (data_dir / "notes.json").write_text(json.dumps([{"timestamp": "soon", "note": "x"}]))
    with pytest.raises(RuntimeError, match="malformed record"):
        memory_module.init_db()
    assert memory_module.get_memories("hash1") == []
    assert (data_dir / "memories.json").exists()


def test_imported_plaintext_is_encrypted_at_startup(data_dir, monkeypatch):
    _legacy_files(data_dir)
    monkeypatch.setattr(memory_module, "_fernet", Fernet(Fernet.generate_key()))
    memory_module.init_db()
    assert memory_module.encrypt_plaintext_entries() == 6
    raw = b"".join(p.read_bytes() for p in data_dir.glob("bridge.db*"))
    for text in (b"likes jazz", b"caller note", b"Ann"):
        assert text not in raw
    assert memory_module.get_memories("hash1") == ["likes jazz", "prefers mornings"]
    assert memory_module.load_session("hash1")["caller_name"] == "Ann"


def test_integrity_check_failure_stops_startup(data_dir):
    (data_dir / "bridge.db").write_bytes(b"not a database" * 100)
    with pytest.raises(RuntimeError, match="not a readable SQLite database"):
        memory_module.init_db()


def test_backup_writes_a_consistent_copy(data_dir, tmp_path_factory):
    memory_module.add_memory("hash1", "likes tea")
    dest = tmp_path_factory.mktemp("backup") / "bridge-backup.db"
    memory_module.backup(dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    with sqlite3.connect(dest) as copy:
        assert copy.execute("SELECT fact FROM memories").fetchall() == [("likes tea",)]
    with pytest.raises(FileExistsError):
        memory_module.backup(dest)


def _add_facts(data_dir, worker, count):
    memory_module.DATA_DIR = data_dir
    memory_module._fernet = None
    for i in range(count):
        memory_module.add_memory("hash1", f"w{worker}-{i}")
        memory_module.load_session("hash1")


def test_concurrent_processes_do_not_lose_writes(data_dir):
    memory_module.init_db()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_add_facts, args=(data_dir, w, 20)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert len(memory_module.get_memories("hash1")) == 80
    assert memory_module.load_session("hash1")["call_count"] == 81


def test_lifespan_imports_json_and_closes_the_database(data_dir, monkeypatch):
    monkeypatch.setattr(app_module, "PHONE_HASH_SALT", "salt")
    monkeypatch.setattr(app_module, "WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(app_module, "DATA_ENCRYPTION_KEY", "")
    monkeypatch.setattr(app_module, "ALLOW_PLAINTEXT_STORAGE", True)
    _legacy_files(data_dir)

    async def run():
        ctx = app_module.lifespan(app_module.app)
        await ctx.__aenter__()
        assert memory_module.get_memories("hash1") == ["likes jazz", "prefers mornings"]
        await ctx.__aexit__(None, None, None)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
    assert memory_module._conn is None
