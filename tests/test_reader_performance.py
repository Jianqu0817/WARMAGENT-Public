import hashlib
from pathlib import Path
import sqlite3
import sys
from types import ModuleType
from types import SimpleNamespace
import threading
import time

import pytest

from warmagent.reader_performance import DerivedKeyCache, install_reader_performance


def test_derived_key_cache_invalidates_on_database_or_credential_change(tmp_path):
    database = tmp_path / "account" / "db_storage" / "message" / "message_0.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"a" * 16 + b"body")
    calls = []

    def derive(path, fallback_key, fallback_salt, passphrase=""):
        header = Path(path).read_bytes()[:16]
        calls.append((str(path), passphrase, header))
        return hashlib.sha256(passphrase.encode() + header).hexdigest(), header.hex()

    cached_derive = DerivedKeyCache().wrap(derive)
    first = cached_derive(str(database), "fallback", "salt", "credential-a")
    assert cached_derive(str(database), "fallback", "salt", "credential-a") == first
    assert len(calls) == 1

    database.write_bytes(b"b" * 16 + b"body")
    changed_header = cached_derive(str(database), "fallback", "salt", "credential-a")
    assert changed_header != first
    assert len(calls) == 2

    changed_credential = cached_derive(str(database), "fallback", "salt", "credential-b")
    assert changed_credential != changed_header
    assert len(calls) == 3

    cached_derive(str(database), "fallback", "salt", "credential-b")
    assert len(calls) == 3

    cached_derive(str(database), "fallback", "salt", "")
    cached_derive(str(database), "fallback", "salt", "credential-b")
    assert len(calls) == 5


def test_poll_reuses_read_only_connection_and_keeps_http_thread_isolated(tmp_path, capsys):
    database = tmp_path / "messages.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")

    class Reader:
        def __init__(self):
            self.stop = threading.Event()
            self.last_poll = 0
            self.last_error = ""
            self.raw_connections = []
            self.used_connections = {}
            self.first_message_read = threading.Event()
            self.continue_poll = threading.Event()

        def refresh_names(self, max_age=0):
            return None

        def selected_talkers(self):
            return ["first", "second"]

        def open_messages(self):
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA query_only=ON")
            self.raw_connections.append(connection)
            return [connection]

        def messages(self, talker):
            connections = self.open_messages()
            try:
                connection = connections[0]
                connection.execute("SELECT value FROM sample").fetchall()
                with pytest.raises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE blocked(value INTEGER)")
                self.used_connections[(threading.current_thread().name, talker)] = connection
                if talker == "first":
                    self.first_message_read.set()
                    self.continue_poll.wait(timeout=5)
            finally:
                for connection in connections:
                    connection.close()

        def pump_sticker_collection(self):
            return None

        def pump_sticker_pool_review(self):
            return None

        def poll(self):
            while not self.stop.is_set():
                self.refresh_names(max_age=30)
                for talker in self.selected_talkers():
                    self.messages(talker)
                self.last_poll = time.time()
                self.last_error = ""
                self.stop.set()
                self.stop.wait(1)

    def derive(path, key, salt, passphrase=""):
        return key, salt

    common = SimpleNamespace(derive_database_key=derive)
    decrypt = SimpleNamespace(derive_database_key=derive)
    install_reader_performance(Reader, common, decrypt)
    reader = Reader()
    poll_thread = threading.Thread(target=reader.poll, name="reader-poll")
    poll_thread.start()
    assert reader.first_message_read.wait(timeout=3)

    reader.messages("http")
    http_connection = reader.used_connections[(threading.current_thread().name, "http")]
    reader.continue_poll.set()
    poll_thread.join(timeout=3)
    assert not poll_thread.is_alive()

    first = reader.used_connections[("reader-poll", "first")]
    second = reader.used_connections[("reader-poll", "second")]
    assert first is second
    assert http_connection is not first
    assert len(reader.raw_connections) == 2
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        http_connection.execute("SELECT 1")

    output = capsys.readouterr().out
    assert "Reader poll metrics" in output
    assert "message_queries=2" in output
    assert "lastPollDelay_ms=na" in output
    assert "first" not in output and "second" not in output and "http" not in output


def test_run_reader_bootstrap_loads_without_starting_the_server(tmp_path, monkeypatch, capsys):
    from warmagent.reader_performance import run_reader

    api_path = tmp_path / "workspace" / "scripts" / "reader-api.py"
    api_path.parent.mkdir(parents=True)
    api_path.write_text(
        """
from types import SimpleNamespace

def derive_database_key(path, key, salt, passphrase=''):
    return key, salt

nt = SimpleNamespace(derive_database_key=derive_database_key)

class Reader:
    def __init__(self):
        self.stop = SimpleNamespace(wait=lambda *args: None)
        self.last_poll = 0
        self.last_error = ''
        self.names = {'test': 'Test'}

    def poll(self): pass
    def open_messages(self): return []
    def refresh_names(self, max_age=0): pass
    def selected_talkers(self): return ['test']
    def messages(self, talker): return []

if __name__ == '__main__':
    raise AssertionError('reader main block must not run')
""",
        encoding="utf-8",
    )
    nt_common = ModuleType("nt_common")

    def original_derive(path, key, salt, passphrase=""):
        return key, salt

    nt_common.derive_database_key = original_derive
    monkeypatch.setitem(sys.modules, "nt_common", nt_common)
    original_argv = sys.argv

    assert run_reader(api_path, ["--check"]) == 0
    assert sys.argv is original_argv
    assert nt_common.derive_database_key is not original_derive
    assert '"databaseReady": true' in capsys.readouterr().out
