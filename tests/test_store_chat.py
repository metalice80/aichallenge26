from __future__ import annotations

import numpy as np
import pytest

from src.chat import ChatService, build_messages
from src.extract import load_document
from src.index_store import Store


class LocalFake:
    def __init__(self):
        self.messages = None
        self.fail = False
        self.models = []

    def ensure_model(self, model_id):
        if not model_id:
            raise ValueError("Модель не указана")
        self.models.append(model_id)

    def embed(self, texts, model_id):
        if self.fail:
            raise ConnectionError("LM Studio выключен")
        return np.asarray([[1.0, 0.0, 0.0] if "альфа" in text.lower() else [0.0, 1.0, 0.0]
                           for text in texts], dtype=np.float32)

    def stream_chat(self, messages, model_id):
        self.messages = messages
        yield "Проверенный "
        yield "ответ."


def docs():
    return [load_document("alpha.md", "# Альфа\nОсобое правило альфа.".encode()),
            load_document("beta.md", "# Бета\nИной случай бета.".encode())]


def test_two_indexes_persist_id_mapping_and_model_guard(tmp_path):
    db = tmp_path / "app.db"
    client = LocalFake()
    first = Store(db_path=db, data_dir=tmp_path)
    first.build(docs(), client, "embedding-1")
    assert set(first.stats()) == {"fixed", "structural"}
    assert first.stats()["fixed"]["count"] == 2
    for strategy in ("fixed", "structural"):
        chunks = first.list_chunks(strategy)
        assert len(chunks) == 2
        hits = Store(db_path=db, data_dir=tmp_path).search("альфа", client, "embedding-1", strategy)
        assert hits[0].source == "alpha.md"
        assert hits[0].chunk_id == next(c.chunk_id for c in chunks if c.source == "alpha.md")
        assert hits[0].vector_id == next(c.vector_id for c in chunks if c.source == "alpha.md")
        assert hits[0].score > hits[1].score
    with pytest.raises(ValueError, match="модел|переиндекс",):
        first.search("альфа", client, "different-model")


def test_failed_rebuild_preserves_complete_previous_version(tmp_path):
    store = Store(db_path=tmp_path / "app.db", data_dir=tmp_path)
    client = LocalFake()
    store.build(docs(), client, "embedding-1")
    version = store.stats()["fixed"]["version"]
    client.fail = True
    with pytest.raises(ConnectionError):
        store.build([load_document("new.txt", "Новый материал".encode())], client, "embedding-1")
    client.fail = False
    reloaded = Store(db_path=tmp_path / "app.db", data_dir=tmp_path)
    assert reloaded.stats()["fixed"]["version"] == version
    assert reloaded.search("альфа", client, "embedding-1")[0].source == "alpha.md"


def test_chat_context_modes_history_and_resume(tmp_path):
    store = Store(db_path=tmp_path / "app.db", data_dir=tmp_path)
    client = LocalFake()
    store.build(docs(), client, "embedding-1")
    chat = ChatService(store, client, "chat-1", "embedding-1")
    conversation = chat.create_conversation("Проверка")
    hits, fragments = chat.reply(conversation, "Что говорит альфа?", rag=True, top_k=1)
    assert hits[0].source == "alpha.md"
    assert "".join(fragments) == "Проверенный ответ."
    assert "альфа" in str(client.messages).lower()
    assert any("alpha.md" in str(message) for message in client.messages)
    assert client.messages[-1]["role"] == "user"
    assert "Особое правило альфа" in client.messages[-1]["content"]
    assert len(chat.history(conversation)) == 2
    assert chat.history(conversation)[1]["hits"][0]["source"] == "alpha.md"

    plain_hits, plain_fragments = chat.reply(conversation, "Второй вопрос?", rag=False)
    assert not plain_hits
    assert "".join(plain_fragments) == "Проверенный ответ."
    assert client.messages[-1] == {"role": "user", "content": "Второй вопрос?"}
    assert "alpha.md" not in str(client.messages)
    resumed = ChatService(Store(db_path=tmp_path / "app.db", data_dir=tmp_path), client, "chat-1", "embedding-1")
    history = resumed.history(conversation)
    assert [message["role"] for message in history] == ["user", "assistant", "user", "assistant"]
    assert history[1]["rag"] is True and history[1]["strategy"] == "fixed"
    assert history[1]["hits"][0]["chunk_id"] == hits[0].chunk_id
    assert history[1]["hits"][0]["score"] == hits[0].score
    assert history[1]["created_at"]
    assert history[3]["rag"] is False and history[3]["strategy"] is None
    assert not history[-1]["hits"]
    assert conversation in [item["id"] for item in resumed.list_conversations()]


def test_chat_limits_prior_turns_without_reusing_old_context():
    prior = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"ход {i}"}
             for i in range(40)]
    rag = build_messages("Новый вопрос", prior, context="Источник: архив.md\nФакт.")
    plain = build_messages("Новый вопрос", prior)
    assert len(rag) == len(plain) == 14  # system + twelve last messages + question
    assert rag[1]["content"] == plain[1]["content"] == "ход 28"
    assert "архив.md" in rag[-1]["content"]
    assert plain[-1] == {"role": "user", "content": "Новый вопрос"}
