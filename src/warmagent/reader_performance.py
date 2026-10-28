"""In-memory performance wrappers for the bundled WeChat reader process."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import runpy
import sys
import threading
import time
from functools import wraps


class DerivedKeyCache:
    """Cache derived SQLCipher keys in memory, scoped to the exact DB identity."""

    def __init__(self, max_entries: int = 256):
        self._entries = {}
        self._max_entries = max_entries
        self._lock = threading.RLock()

    @staticmethod
    def _identity(path: str) -> tuple[str, str]:
        resolved = os.path.normcase(os.path.realpath(os.fspath(path)))
        database = Path(resolved)
        account = next((parent.parent for parent in database.parents
                        if parent.name.casefold() == "db_storage"), database.parent)
        return os.path.normcase(str(account)), resolved

    def wrap(self, derive):
        @wraps(derive)
        def cached(path, fallback_key, fallback_salt, passphrase=""):
            if not passphrase:
                identity = self._identity(path)
                with self._lock:
                    for old_key in tuple(self._entries):
                        if old_key[:2] == identity:
                            del self._entries[old_key]
                return derive(path, fallback_key, fallback_salt, passphrase)
            account_path, database_path = self._identity(path)
            try:
                with open(database_path, "rb") as handle:
                    header = handle.read(16)
            except OSError:
                return derive(path, fallback_key, fallback_salt, passphrase)
            if len(header) != 16:
                return derive(path, fallback_key, fallback_salt, passphrase)
            secret = passphrase if isinstance(passphrase, bytes) else str(passphrase).encode("utf-8")
            credential_fingerprint = hashlib.sha256(secret).digest()
            identity = (account_path, database_path)
            cache_key = (*identity, header, credential_fingerprint, fallback_key, fallback_salt)
            with self._lock:
                # A changed header, credential, or fallback invalidates the prior key for this DB.
                for old_key in tuple(self._entries):
                    if old_key[:2] == identity and old_key != cache_key:
                        del self._entries[old_key]
                if cache_key in self._entries:
                    return self._entries[cache_key]
                value = derive(path, fallback_key, fallback_salt, passphrase)
                if value != (fallback_key, fallback_salt):
                    self._entries[cache_key] = value
                    while len(self._entries) > self._max_entries:
                        del self._entries[next(iter(self._entries))]
                return value

        return cached


class _SharedReadConnection:
    """Forward SQL calls while deferring per-message close to the poll boundary."""

    __slots__ = ("_connection",)

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None

    def close_now(self):
        return self._connection.close()


class _PollTracker:
    def __init__(self):
        self._local = threading.local()
        self._last_log = 0.

    def _round(self, reader):
        if getattr(self._local, "reader", None) is not reader:
            return None
        return getattr(self._local, "poll_round", None)

    def _begin_round(self, reader):
        poll_round = {
            "started": time.perf_counter(),
            "started_wall": time.time(),
            "last_poll_before": float(getattr(reader, "last_poll", 0) or 0),
            "refresh_seconds": 0.0,
            "selection_seconds": 0.0,
            "message_seconds": 0.0,
            "message_calls": 0,
            "talker_count": 0,
            "selection_recorded": False,
            "sticker_seconds": 0.0,
            "connections": None,
        }
        self._local.poll_round = poll_round
        return poll_round

    def _finish_round(self, reader, *, log_metrics: bool):
        poll_round = self._round(reader)
        if poll_round is None:
            return
        self._local.poll_round = None
        close_error = ""
        for connection in poll_round["connections"] or ():
            try:
                connection.close_now()
            except Exception as error:
                close_error = type(error).__name__
        if not log_metrics:
            return
        completed = float(getattr(reader, "last_poll", 0) or 0) != poll_round["last_poll_before"]
        total_ms = int((time.perf_counter() - poll_round["started"]) * 1000)
        now = time.monotonic()
        if completed and total_ms < 1000 and now - self._last_log < 60:
            return
        self._last_log = now
        last_poll_delay = poll_round["started_wall"] - poll_round["last_poll_before"]
        delay_value = str(max(0, int(last_poll_delay * 1000))) if poll_round["last_poll_before"] else "na"
        parts = [
            "Reader poll metrics",
            "result=ok" if completed else "result=error",
            f"total_ms={total_ms}",
            f"refresh_ms={int(poll_round['refresh_seconds'] * 1000)}",
            f"selection_ms={int(poll_round['selection_seconds'] * 1000)}",
            f"talkers={poll_round['talker_count']}",
            f"message_queries={poll_round['message_calls']}",
            f"messages_ms={int(poll_round['message_seconds'] * 1000)}",
            f"sticker_ms={int(poll_round['sticker_seconds'] * 1000)}",
            f"lastPollDelay_ms={delay_value}",
        ]
        if not completed:
            parts.append(f"error={getattr(reader, 'last_error', '') or 'unknown'}")
        if close_error:
            parts.append(f"connectionCloseError={close_error}")
        print("; ".join(parts), flush=True)

    def install(self, reader_class):
        original_poll = reader_class.poll
        original_open_messages = reader_class.open_messages
        original_refresh_names = reader_class.refresh_names
        original_selected_talkers = reader_class.selected_talkers
        original_messages = reader_class.messages

        @wraps(original_poll)
        def poll(reader, *args, **kwargs):
            previous_reader = getattr(self._local, "reader", None)
            previous_round = getattr(self._local, "poll_round", None)
            self._local.reader = reader
            self._local.poll_round = None
            event = reader.stop
            stop_wait = event.wait

            @wraps(stop_wait)
            def wait(*wait_args, **wait_kwargs):
                self._finish_round(reader, log_metrics=True)
                return stop_wait(*wait_args, **wait_kwargs)

            event.wait = wait
            try:
                return original_poll(reader, *args, **kwargs)
            finally:
                self._finish_round(reader, log_metrics=False)
                event.wait = stop_wait
                if previous_reader is None:
                    self._local.__dict__.pop("reader", None)
                    self._local.__dict__.pop("poll_round", None)
                else:
                    self._local.reader = previous_reader
                    self._local.poll_round = previous_round

        @wraps(original_open_messages)
        def open_messages(reader, *args, **kwargs):
            poll_round = self._round(reader)
            if poll_round is None:
                return original_open_messages(reader, *args, **kwargs)
            if poll_round["connections"] is None:
                poll_round["connections"] = [
                    _SharedReadConnection(connection)
                    for connection in original_open_messages(reader, *args, **kwargs)
                ]
            return poll_round["connections"]

        @wraps(original_refresh_names)
        def refresh_names(reader, *args, **kwargs):
            if getattr(self._local, "reader", None) is not reader:
                return original_refresh_names(reader, *args, **kwargs)
            poll_round = self._round(reader)
            if poll_round is None:
                poll_round = self._begin_round(reader)
            started = time.perf_counter()
            try:
                return original_refresh_names(reader, *args, **kwargs)
            finally:
                poll_round["refresh_seconds"] += time.perf_counter() - started

        @wraps(original_selected_talkers)
        def selected_talkers(reader, *args, **kwargs):
            poll_round = self._round(reader)
            started = time.perf_counter()
            result = original_selected_talkers(reader, *args, **kwargs)
            if poll_round is not None and not poll_round["selection_recorded"]:
                poll_round["selection_seconds"] += time.perf_counter() - started
                poll_round["talker_count"] = len(result)
                poll_round["selection_recorded"] = True
            return result

        @wraps(original_messages)
        def messages(reader, *args, **kwargs):
            poll_round = self._round(reader)
            started = time.perf_counter()
            try:
                return original_messages(reader, *args, **kwargs)
            finally:
                if poll_round is not None:
                    poll_round["message_seconds"] += time.perf_counter() - started
                    poll_round["message_calls"] += 1

        def time_sticker_method(original):
            @wraps(original)
            def timed(reader, *args, **kwargs):
                poll_round = self._round(reader)
                started = time.perf_counter()
                try:
                    return original(reader, *args, **kwargs)
                finally:
                    if poll_round is not None:
                        poll_round["sticker_seconds"] += time.perf_counter() - started
            return timed

        reader_class.poll = poll
        reader_class.open_messages = open_messages
        reader_class.refresh_names = refresh_names
        reader_class.selected_talkers = selected_talkers
        reader_class.messages = messages
        for name in ("pump_sticker_collection", "pump_sticker_pool_review"):
            original = getattr(reader_class, name, None)
            if original is not None:
                setattr(reader_class, name, time_sticker_method(original))


def install_reader_performance(reader_class, nt_common, nt_decrypt):
    cache = DerivedKeyCache()
    cached_derive = cache.wrap(nt_decrypt.derive_database_key)
    nt_common.derive_database_key = cached_derive
    nt_decrypt.derive_database_key = cached_derive
    tracker = _PollTracker()
    tracker.install(reader_class)
    return cache


def run_reader(api_path: str | Path, arguments=()) -> int:
    """Load the local reader source, patch it in memory, then run its normal API."""
    api_path = Path(api_path).resolve()
    workspace = api_path.parent.parent
    original_argv = sys.argv
    sys.argv = [str(api_path), *arguments]
    try:
        api = runpy.run_path(str(api_path), run_name="_warmagent_reader_api")
        nt_common = importlib.import_module("nt_common")
        install_reader_performance(api["Reader"], nt_common, api["nt"])
        reader = api["Reader"]()
        if "--check" in arguments:
            print(json.dumps({
                "databaseReady": True,
                "contactNamesLoaded": bool(reader.names),
                "selectedConversations": len(reader.selected_talkers()),
            }))
        else:
            config = api["read_json"](workspace / "config" / "wechatagent.json")
            token = api["read_json"](workspace / "state" / "wechat-io-config.json")["reader_token"]
            api["serve"](reader, config["ports"]["weflow"], token)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("Reader initialization failed: " + message, file=sys.stderr)
        return 1
    finally:
        sys.argv = original_argv
