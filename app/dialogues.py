"""Conversation assembly, stream lifetime, and per-chat generation exclusion."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
import json
import time

from .lm_studio import LMStudioClient, LMStudioError
from .storage import Storage

# Limits count Python Unicode code points. Oldest complete messages are removed
# from the request context until it fits, without deleting persisted history.
MAX_INPUT_CHARS = 8_000
MAX_HISTORY_CHARS = 32_000


class ChatBusy(Exception):
    pass


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _context(rows: list[dict]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    remaining = MAX_HISTORY_CHARS
    for row in reversed(rows):
        if row["status"] != "completed":
            continue
        text = row["content"]
        if len(text) > remaining:
            break
        selected.append({"role": row["role"], "content": text})
        remaining -= len(text)
    selected.reverse()
    return selected


class Dialogues:
    def __init__(self, storage: Storage, client: LMStudioClient):
        self.storage = storage
        self.client = client
        self._active: set[int] = set()
        self._mutex = asyncio.Lock()

    async def reserve(self, chat_id: int) -> None:
        async with self._mutex:
            if chat_id in self._active:
                raise ChatBusy("Ответ в этом чате уже генерируется")
            self._active.add(chat_id)

    async def release(self, chat_id: int) -> None:
        async with self._mutex:
            self._active.discard(chat_id)

    async def ensure_idle(self, chat_id: int) -> None:
        async with self._mutex:
            if chat_id in self._active:
                raise ChatBusy("Дождитесь завершения ответа, прежде чем удалить чат")

    def begin(self, chat_id: int, content: str, model_id: str | None) -> tuple[dict, dict, list[dict]]:
        return self.storage.begin_turn(chat_id, content, model_id)

    async def stream(
        self,
        chat_id: int,
        user: dict,
        assistant: dict,
        history: list[dict],
    ) -> AsyncIterator[str]:
        fragments: list[str] = []
        first_token_ms: int | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        started: int | None = None
        finished = False

        def persist(status: str) -> dict:
            nonlocal finished
            record = self.storage.finish_turn(
                assistant["id"], "".join(fragments), status,
                round((time.perf_counter_ns() - started) / 1_000_000) if started is not None else None,
                first_token_ms, prompt_tokens, completion_tokens,
            )
            finished = True
            return record

        try:
            yield _sse({"type": "user", "message": user})
            model = assistant["model_id"]
            if not model:
                raise LMStudioError("Выберите чат-модель перед отправкой сообщения")
            started = time.perf_counter_ns()
            async with aclosing(self.client.stream(model, _context(history))) as chunks:
                async for kind, data in chunks:
                    if kind == "delta":
                        text = data
                        if first_token_ms is None:
                            first_token_ms = round((time.perf_counter_ns() - started) / 1_000_000)
                        fragments.append(text)
                        yield _sse({"type": "delta", "text": text})
                    elif kind == "usage":
                        prompt_tokens, completion_tokens = data
            yield _sse({"type": "done", "message": persist("completed")})
        except LMStudioError as exc:
            record = persist(exc.status)
            yield _sse({"type": "error", "message": str(exc), "status": exc.status, "assistant": record})
        except (asyncio.CancelledError, GeneratorExit):
            if not finished:
                persist("interrupted")
            raise
        except Exception:
            if not finished:
                record = persist("failed")
                yield _sse({"type": "error", "message": "Неожиданная ошибка при генерации ответа", "status": "failed", "assistant": record})
        finally:
            await self.release(chat_id)
