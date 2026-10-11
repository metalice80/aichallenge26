from app.storage import ChatNotFound, Storage
import pytest


def test_sqlite_turns_metrics_and_cascade_across_connections(tmp_path):
    path = tmp_path / "nested" / "chat.sqlite3"
    db = Storage(path)
    db.initialize()
    chat = db.create_chat("Беседа", "local-chat")
    user, pending, context = db.begin_turn(chat["id"], "Первый вопрос", "local-chat")
    assert db.get_chat(chat["id"])["title"] == "Первый вопрос"
    assert [entry["id"] for entry in context] == [user["id"]]
    assert pending["status"] == "interrupted"
    saved = db.finish_turn(pending["id"], "Первый ответ", "completed", 150, 25, 13, 5)
    assert saved["duration_ms"] == 150
    assert saved["time_to_first_token_ms"] == 25
    assert (saved["prompt_tokens"], saved["completion_tokens"]) == (13, 5)
    _, interrupted, context = db.begin_turn(chat["id"], "Следующий вопрос", "local-chat")
    assert db.get_chat(chat["id"])["title"] == "Первый вопрос"
    assert [(entry["role"], entry["content"]) for entry in context] == [
        ("user", "Первый вопрос"), ("assistant", "Первый ответ"), ("user", "Следующий вопрос")
    ]
    db.finish_turn(interrupted["id"], "Неполный", "interrupted", 90, 10, None, None)
    reopened = Storage(path)
    assert reopened.get_chat(chat["id"])["title"] == "Первый вопрос"
    assert [entry["status"] for entry in reopened.list_messages(chat["id"])] == [
        "completed", "completed", "completed", "interrupted"
    ]
    assert reopened.list_messages(chat["id"])[-1]["prompt_tokens"] is None
    reopened.delete_chat(chat["id"])
    with pytest.raises(ChatNotFound):
        db.list_messages(chat["id"])
