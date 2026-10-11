"""Local FastAPI chat endpoints and static web entry point."""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dialogues import ChatBusy, Dialogues, MAX_INPUT_CHARS
from .lm_studio import LMStudioClient, LMStudioError
from .storage import ChatNotFound, Storage

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "chats.sqlite3"
DEFAULT_LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"


class DialogueStreamingResponse(StreamingResponse):
    """Close the generator on transport errors as well as clean disconnects."""

    def __init__(self, *args, dialogues: Dialogues, chat_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.dialogues = dialogues
        self.chat_id = chat_id

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                await self.dialogues.release(self.chat_id)


class ChatCreate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    model_id: str | None = Field(default=None, max_length=256)


class ChatPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    model_id: str | None = Field(default=None, max_length=256)


class MessageCreate(BaseModel):
    content: str = Field(max_length=MAX_INPUT_CHARS)
    model_id: str | None = Field(default=None, max_length=256)


def _title(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(422, "Название чата не может быть пустым")
    return value.strip()


def _model(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise HTTPException(422, "Идентификатор модели не может быть пустым")
    return value


def _not_found(exc: ChatNotFound) -> HTTPException:
    return HTTPException(404, "Чат не найден")


def create_app(
    db_path: str | Path | None = None,
    lm_studio_base_url: str | None = None,
    lm_client: LMStudioClient | None = None,
) -> FastAPI:
    storage = Storage(db_path if db_path is not None else os.getenv("CHAT_DB_PATH", str(DEFAULT_DB_PATH)))
    owns_client = lm_client is None
    client = lm_client or LMStudioClient(lm_studio_base_url or os.getenv("LM_STUDIO_BASE_URL", DEFAULT_LM_STUDIO_BASE_URL))
    dialogues = Dialogues(storage, client)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        storage.initialize()
        try:
            yield
        finally:
            if owns_client:
                await client.close()

    application = FastAPI(lifespan=lifespan)
    application.state.storage = storage
    application.state.lm_studio = client
    application.state.dialogues = dialogues
    static_dir = ROOT / "static"

    @application.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @application.get("/api/health")
    async def health():
        try:
            await client.models()
        except LMStudioError as exc:
            return {"app": "ok", "lm_studio": "offline", "detail": str(exc)}
        return {"app": "ok", "lm_studio": "online"}

    @application.get("/api/models")
    async def models():
        try:
            return {"models": await client.models()}
        except LMStudioError as exc:
            raise HTTPException(503, str(exc)) from exc

    @application.get("/api/chats")
    async def list_chats():
        return storage.list_chats()

    @application.post("/api/chats", status_code=201)
    async def create_chat(body: ChatCreate):
        return storage.create_chat(_title(body.title) if body.title is not None else "Новый чат", _model(body.model_id))

    @application.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: int):
        try:
            return {"chat": storage.get_chat(chat_id), "messages": storage.list_messages(chat_id)}
        except ChatNotFound as exc:
            raise _not_found(exc) from exc

    @application.patch("/api/chats/{chat_id}")
    async def update_chat(chat_id: int, body: ChatPatch):
        changes = body.model_dump(exclude_unset=True)
        if "title" in changes:
            changes["title"] = _title(changes["title"])
        if "model_id" in changes:
            changes["model_id"] = _model(changes["model_id"])
        try:
            return storage.update_chat(chat_id, changes)
        except ChatNotFound as exc:
            raise _not_found(exc) from exc

    @application.delete("/api/chats/{chat_id}", status_code=204)
    async def delete_chat(chat_id: int):
        try:
            await dialogues.ensure_idle(chat_id)
            storage.delete_chat(chat_id)
        except ChatBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except ChatNotFound as exc:
            raise _not_found(exc) from exc
        return Response(status_code=204)

    @application.post("/api/chats/{chat_id}/messages/stream")
    async def stream_message(chat_id: int, body: MessageCreate):
        if not body.content.strip():
            raise HTTPException(422, "Сообщение не может быть пустым")
        if "model_id" in body.model_fields_set:
            chosen_model = _model(body.model_id)
        else:
            try:
                chosen_model = storage.get_chat(chat_id)["model_id"]
            except ChatNotFound as exc:
                raise _not_found(exc) from exc
        try:
            await dialogues.reserve(chat_id)
        except ChatBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            user, assistant, history = dialogues.begin(chat_id, body.content, chosen_model)
        except ChatNotFound as exc:
            await dialogues.release(chat_id)
            raise _not_found(exc) from exc
        except Exception:
            await dialogues.release(chat_id)
            raise
        return DialogueStreamingResponse(
            dialogues.stream(chat_id, user, assistant, history),
            dialogues=dialogues,
            chat_id=chat_id,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    application.mount("/static", StaticFiles(directory=static_dir, check_dir=False), name="static")
    return application


app = create_app()
