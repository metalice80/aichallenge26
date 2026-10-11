"""OpenAI-compatible local LM Studio client and strict streaming SSE decoder."""

from collections.abc import AsyncIterator
import json

import httpx


class LMStudioError(Exception):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


class LMStudioClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10),
            trust_env=False,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def models(self) -> list[dict[str, str]]:
        try:
            response = await self.http.get(f"{self.base_url}/models")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError("Invalid models response")
            return [{"id": item["id"]} for item in data["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise _connection_error(exc) from exc

    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[tuple[str, object]]:
        try:
            async with self.http.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ) as response:
                if response.is_error:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    raise LMStudioError(f"LM Studio вернул HTTP {response.status_code}: {body or response.reason_phrase}")
                data_lines: list[str] = []
                done = False
                async for line in response.aiter_lines():
                    if line == "":
                        if not data_lines:
                            continue
                        event = "\n".join(data_lines)
                        data_lines.clear()
                        if event == "[DONE]":
                            done = True
                            break
                        try:
                            chunk = json.loads(event)
                            if not isinstance(chunk, dict):
                                raise ValueError("Expected an object")
                            if "error" in chunk:
                                error = chunk["error"]
                                detail = error.get("message") if isinstance(error, dict) else str(error)
                                raise LMStudioError(f"Ошибка генерации LM Studio: {detail}")
                            if "choices" not in chunk and "usage" not in chunk:
                                raise ValueError("Missing choices and usage")
                            usage = chunk.get("usage")
                            if usage is not None:
                                if not isinstance(usage, dict):
                                    raise ValueError("Invalid usage")
                                yield "usage", (_token_count(usage.get("prompt_tokens")), _token_count(usage.get("completion_tokens")))
                            choices = chunk.get("choices", [])
                            if not isinstance(choices, list):
                                raise ValueError("Invalid choices")
                            for choice in choices[:1]:
                                if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                                    raise ValueError("Invalid delta")
                                text = choice.get("delta", {}).get("content")
                                if text is not None:
                                    if not isinstance(text, str):
                                        raise ValueError("Invalid text")
                                    if text:
                                        yield "delta", text
                        except (ValueError, TypeError) as exc:
                            raise LMStudioError("LM Studio прислал некорректный потоковый ответ") from exc
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
                        continue
                    else:
                        raise LMStudioError("LM Studio прислал некорректный SSE-фрагмент")
                if not done:
                    raise LMStudioError("Поток LM Studio оборвался до завершения ответа", status="interrupted")
        except httpx.TimeoutException as exc:
            raise LMStudioError("Истекло время ожидания ответа LM Studio", status="interrupted") from exc
        except httpx.RequestError as exc:
            raise _connection_error(exc, status="interrupted") from exc


def _token_count(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Invalid token count")
    return value


def _connection_error(exc: Exception, status: str = "failed") -> LMStudioError:
    if isinstance(exc, httpx.ConnectError):
        return LMStudioError("Нет соединения с LM Studio. Запустите локальный сервер и загрузите чат-модель.", status)
    return LMStudioError(f"LM Studio недоступен: {exc}", status)
