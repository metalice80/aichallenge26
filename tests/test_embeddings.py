import pytest

from src.embeddings import LMStudioClient


def test_unavailable_local_model_server_has_clear_error():
    with pytest.raises(ConnectionError, match="LM Studio недоступен"):
        LMStudioClient("http://127.0.0.1:0/v1", timeout=0.2).list_models()


def test_embedding_rejects_invalid_vectors(monkeypatch):
    client = LMStudioClient()
    monkeypatch.setattr(client, "_json", lambda *args, **kwargs: {"data": [
        {"index": 0, "embedding": [float("nan"), 1.0]}
    ]})
    with pytest.raises(ValueError, match="вектор"):
        client.embed(["пример"], "embedding-model")
