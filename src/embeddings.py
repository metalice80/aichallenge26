"""Small OpenAI-compatible LM Studio client, including streamed completions."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np


class LMStudioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, endpoint: str, payload: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + endpoint, data=body,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise ConnectionError(f"LM Studio: HTTP {exc.code} ({detail}).") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"LM Studio недоступен по адресу {self.base_url}: {exc}") from exc

    def _json(self, endpoint: str, payload: dict | None = None) -> dict:
        try:
            with self._request(endpoint, payload) as response:
                return json.load(response)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConnectionError("LM Studio вернул некорректный JSON.") from exc

    def list_models(self) -> list[str]:
        result = self._json("/models")
        if not isinstance(result.get("data"), list):
            raise ConnectionError("Ответ /models не содержит список моделей.")
        return [item["id"] for item in result["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)]

    def ensure_model(self, model_id: str) -> None:
        if not model_id or model_id not in self.list_models():
            raise ValueError(f"Модель '{model_id}' не найдена в LM Studio (/models).")

    def embed(self, texts: Sequence[str], model_id: str) -> np.ndarray:
        """Return a dense matrix, checking API cardinality, dimension and finiteness."""
        if not texts:
            raise ValueError("Нет текста для вычисления эмбеддингов.")
        result = self._json("/embeddings", {"model": model_id, "input": list(texts)})
        entries = result.get("data")
        if not isinstance(entries, list) or len(entries) != len(texts):
            raise ValueError("LM Studio вернул неверное число эмбеддингов.")
        ordered: list[list[float] | None] = [None] * len(texts)
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("embedding"), list):
                raise ValueError("LM Studio вернул неверный формат эмбеддинга.")
            index = entry.get("index", position)
            if not isinstance(index, int) or not 0 <= index < len(texts) or ordered[index] is not None:
                raise ValueError("LM Studio вернул повторяющийся/неверный индекс эмбеддинга.")
            ordered[index] = entry["embedding"]
        try:
            matrix = np.asarray(ordered, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise ValueError("Размерность эмбеддингов LM Studio не совпадает.") from exc
        if matrix.ndim != 2 or matrix.shape[0] != len(texts) or matrix.shape[1] < 1:
            raise ValueError("LM Studio вернул пустой или неверной размерности эмбеддинг.")
        if not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) <= 0):
            raise ValueError("LM Studio вернул нечисловой, бесконечный или нулевой вектор.")
        return matrix

    def stream_chat(self, messages: list[dict[str, str]], model_id: str) -> Iterator[str]:
        """Decode OpenAI SSE data events; missing [DONE] is an incomplete response."""
        payload = {"model": model_id, "messages": messages, "stream": True}
        try:
            with self._request("/chat/completions", payload) as response:
                data_lines: list[str] = []
                done = False
                for raw in response:
                    line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        event = "\n".join(data_lines)
                        data_lines.clear()
                        if event == "[DONE]":
                            done = True
                            break
                        try:
                            item = json.loads(event)
                            if "error" in item:
                                raise ConnectionError(f"LM Studio: {item['error']}")
                            delta = item["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if isinstance(content, str) and content:
                                yield content
                        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                            raise ConnectionError("LM Studio вернул некорректный поток ответа.") from exc
                if not done and data_lines and "\n".join(data_lines) == "[DONE]":
                    done = True
                if not done:
                    raise ConnectionError("Поток ответа LM Studio оборвался до завершения.")
        except (URLError, TimeoutError, OSError, UnicodeError) as exc:
            raise ConnectionError(f"Соединение с LM Studio прервалось: {exc}") from exc
