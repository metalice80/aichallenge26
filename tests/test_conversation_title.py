from src.chat import ChatService
from src.index_store import Store


class ChatClient:
    def ensure_model(self, model_id):
        assert model_id == "chat-model"

    def stream_chat(self, messages, model_id):
        yield "Ответ"


def test_first_saved_user_message_names_only_untitled_conversation(tmp_path):
    store = Store(db_path=tmp_path / "app.db", data_dir=tmp_path)
    chat = ChatService(store, ChatClient(), "chat-model", "unused")
    automatic = chat.create_conversation()
    explicit = chat.create_conversation("Название автора")
    assert next(c for c in chat.list_conversations() if c["id"] == automatic)["title"] == "Новый диалог"

    _, response = chat.reply(automatic, "  Первый вопрос о документе?  ", rag=False)
    assert "".join(response) == "Ответ"
    reloaded = ChatService(Store(db_path=tmp_path / "app.db", data_dir=tmp_path), ChatClient(),
                           "chat-model", "unused")
    titles = {conversation["id"]: conversation["title"] for conversation in reloaded.list_conversations()}
    assert titles[automatic] == "Первый вопрос о документе?"
    assert titles[explicit] == "Название автора"

    _, response = reloaded.reply(automatic, "Второй вопрос?", rag=False)
    assert "".join(response) == "Ответ"
    _, response = reloaded.reply(explicit, "Первый вопрос в названном диалоге?", rag=False)
    assert "".join(response) == "Ответ"
    titles = {conversation["id"]: conversation["title"] for conversation in reloaded.list_conversations()}
    assert titles[automatic] == "Первый вопрос о документе?"
    assert titles[explicit] == "Название автора"
