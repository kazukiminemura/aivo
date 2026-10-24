import sqlite3
from contextlib import closing
import shutil
import uuid
from pathlib import Path

import pytest

from aivo.history import HistoryStore


@pytest.fixture
def tmp_path():
    # Use the writable workspace: the Windows sandbox's system temp is restricted.
    root = (Path.cwd() / ".pytest_cache" / "history-tests").resolve()
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        assert path.resolve().parent == root
        shutil.rmtree(path)


def test_no_storage_is_created_before_explicit_save(tmp_path):
    path = tmp_path / "new" / "history.sqlite3"
    store = HistoryStore(path)
    assert store.list_conversations() == []
    assert store.get_conversation("session") == []
    store.delete_conversation("session")
    store.clear()
    assert not path.exists() and not path.parent.exists()


def test_complete_turns_are_ordered_and_metadata_is_updated(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    store.append_turn("one", "こんにちは", "こんにちは！")
    store.append_turn("two", "別の会話", "はい")
    store.append_turn("one", "猫の名前はあずき", "覚えました")
    listed = store.list_conversations()
    assert [item["id"] for item in listed] == ["one", "two"]
    assert listed[0]["turns"] == 2
    assert listed[0]["preview"] == "猫の名前はあずき"
    assert listed[0]["updated_at"].endswith("+00:00")
    assert store.get_conversation("one") == [
        {"role": "user", "content": "こんにちは"}, {"role": "assistant", "content": "こんにちは！"},
        {"role": "user", "content": "猫の名前はあずき"}, {"role": "assistant", "content": "覚えました"},
    ]


def test_deletion_is_scoped_and_sql_parameters_are_not_executed(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    injection = "x'); DROP TABLE turns; --"
    store.append_turn(injection, "one", "answer")
    store.append_turn("keep", "two", "answer")
    assert len(store.get_conversation(injection)) == 2
    store.delete_conversation(injection)
    assert store.get_conversation(injection) == []
    assert len(store.get_conversation("keep")) == 2
    store.clear()
    assert store.list_conversations() == []


@pytest.mark.parametrize("session,user,assistant", [
    ("", "user", "reply"), ("x" * 129, "user", "reply"),
    ("ok", "", "reply"), ("ok", "user", "  "), ("ok", "user", "x" * 12001),
], ids=["empty-id", "long-id", "empty-user", "empty-assistant", "long-assistant"])
def test_invalid_turn_cannot_create_database(tmp_path, session, user, assistant):
    path = tmp_path / "history.sqlite3"
    with pytest.raises(ValueError):
        HistoryStore(path).append_turn(session, user, assistant)
    assert not path.exists()


def test_reads_are_bounded_and_keep_complete_exchanges(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    store.append_turn("long", "u0", "a0")
    with closing(sqlite3.connect(store.path)) as db, db:
        db.executemany("INSERT INTO turns(session_id,updated_at,user_text,assistant_text) VALUES (?,?,?,?)",
                       [("long", "now", f"u{i}", f"a{i}") for i in range(1, 503)])
    messages = store.get_conversation("long")
    assert len(messages) == 1000
    assert messages[0] == {"role": "user", "content": "u3"}
    assert messages[-1] == {"role": "assistant", "content": "a502"}
