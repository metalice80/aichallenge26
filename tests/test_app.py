"""Exercise the public HTTP API against a local fake LM Studio server."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeLMStudio(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        payload = json.dumps({"data": [{"id": "local-chat", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        if "ошибка сервера" in body["messages"][-1]["content"]:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            def event(value):
                self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
                self.wfile.flush()

            answer = "Помню: кедр" if len(body["messages"]) > 1 else "Запомнил кедр"
            event({"choices": [{"delta": {"content": answer[:7]}}]})
            time.sleep(0.02)
            event({"choices": [{"delta": {"content": answer[7:]}}]})
            if "сломай поток" in body["messages"][-1]["content"]:
                return
            event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            if "без токенов" not in body["messages"][-1]["content"]:
                event({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_app(tmp_path):
    FakeLMStudio.requests = []
    lm = ThreadingHTTPServer(("127.0.0.1", 0), FakeLMStudio)
    thread = Thread(target=lm.serve_forever, daemon=True)
    thread.start()
    port = free_port()
    env = dict(os.environ, CHAT_DB_PATH=str(tmp_path / "history.sqlite3"),
               LM_STUDIO_BASE_URL=f"http://127.0.0.1:{lm.server_port}/v1")

    def launch():
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if proc.poll() is not None:
                raise AssertionError(f"App exited during startup: {proc.returncode}")
            try:
                httpx.get(f"http://127.0.0.1:{port}/api/chats", timeout=0.2)
                return proc
            except httpx.ConnectError:
                time.sleep(0.05)
        proc.terminate()
        raise AssertionError("App did not start")

    proc = launch()
    try:
        yield {"base": f"http://127.0.0.1:{port}", "lm": lm, "process": proc, "launch": launch}
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        lm.shutdown()
        lm.server_close()
        thread.join(timeout=5)


def stream(base, chat_id, content, model_id=None):
    payload = {"content": content}
    if model_id is not None:
        payload["model_id"] = model_id
    with httpx.Client(timeout=15) as client:
        with client.stream("POST", f"{base}/api/chats/{chat_id}/messages/stream", json=payload) as response:
            assert response.status_code == 200, response.read()
            return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def test_chat_routes_and_saved_history_survive_restart(running_app):
    base = running_app["base"]
    with httpx.Client(base_url=base) as client:
        assert client.get("/api/health").json()["lm_studio"] == "online"
        assert {model["id"] for model in client.get("/api/models").json()["models"]} == {"local-chat"}
        chat = client.post("/api/chats", json={"title": "Память", "model_id": "local-chat"}).json()
        chat_id = chat["id"]
        assert client.patch(f"/api/chats/{chat_id}", json={"title": "Новый заголовок"}).json()["title"] == "Новый заголовок"
        assert any(row["id"] == chat_id for row in client.get("/api/chats").json())

    first = stream(base, chat_id, "Запомни: кедр")
    with httpx.Client(base_url=base) as client:
        assert client.get(f"/api/chats/{chat_id}").json()["chat"]["title"] == "Запомни: кедр"
        assert any(row["id"] == chat_id and row["title"] == "Запомни: кедр"
                   for row in client.get("/api/chats").json())
    second = stream(base, chat_id, "Какое слово?")
    assert "".join(e["text"] for e in first if e["type"] == "delta") == "Запомнил кедр"
    assert first[-1]["type"] == second[-1]["type"] == "done"
    assert second[-1]["message"]["content"] == "Помню: кедр"
    assert second[-1]["message"]["status"] == "completed"
    assert second[-1]["message"]["model_id"] == "local-chat"
    assert second[-1]["message"]["prompt_tokens"] == 9
    assert second[-1]["message"]["completion_tokens"] == 4
    assert second[-1]["message"]["duration_ms"] >= second[-1]["message"]["time_to_first_token_ms"] >= 0
    assert len(FakeLMStudio.requests) == 2
    assert FakeLMStudio.requests[0]["stream"] is True
    assert FakeLMStudio.requests[0]["stream_options"] == {"include_usage": True}
    assert [m["role"] for m in FakeLMStudio.requests[1]["messages"]] == ["user", "assistant", "user"]
    assert FakeLMStudio.requests[1]["messages"][1]["content"] == "Запомнил кедр"

    running_app["process"].terminate()
    running_app["process"].wait(timeout=5)
    running_app["process"] = running_app["launch"]()
    with httpx.Client(base_url=base) as client:
        detail = client.get(f"/api/chats/{chat_id}").json()
        assert detail["chat"]["title"] == "Запомни: кедр"
        assert [message["role"] for message in detail["messages"]] == ["user", "assistant", "user", "assistant"]
        assert detail["messages"][-1]["prompt_tokens"] == 9
        assert client.delete(f"/api/chats/{chat_id}").status_code == 204
        assert client.get(f"/api/chats/{chat_id}").status_code == 404


def test_broken_generation_and_offline_keep_saved_chats(running_app):
    base = running_app["base"]
    with httpx.Client(base_url=base) as client:
        chat_id = client.post("/api/chats", json={"title": "Ошибки", "model_id": "local-chat"}).json()["id"]
    broken = stream(base, chat_id, "сломай поток")
    with httpx.Client(base_url=base) as client:
        assert client.get(f"/api/chats/{chat_id}").json()["chat"]["title"] == "сломай поток"
    assert broken[-1]["type"] == "error"
    assert broken[-1]["status"] == "interrupted"
    assert broken[-1]["assistant"]["content"]
    assert broken[-1]["assistant"]["status"] == "interrupted"
    running_app["lm"].shutdown()
    running_app["lm"].server_close()
    with httpx.Client(base_url=base) as client:
        assert client.get("/api/health").json()["lm_studio"] == "offline"
        assert client.get(f"/api/chats/{chat_id}").json()["messages"][-1]["status"] == "interrupted"
        offline = stream(base, chat_id, "Ты ещё тут?")
        assert offline[-1]["type"] == "error"
        assert client.get(f"/api/chats/{chat_id}").status_code == 200


def test_input_limit_does_not_corrupt_chat(running_app):
    base = running_app["base"]
    with httpx.Client(base_url=base) as client:
        chat_id = client.post("/api/chats", json={"model_id": "local-chat"}).json()["id"]
        response = client.post(f"/api/chats/{chat_id}/messages/stream", json={"content": "x" * 8001})
        assert response.status_code in (400, 413, 422)
        assert client.get(f"/api/chats/{chat_id}").json()["messages"] == []

def test_usage_missing_and_upstream_error_are_not_reported_as_completed(running_app):
    base = running_app["base"]
    with httpx.Client(base_url=base) as client:
        chat_id = client.post("/api/chats", json={"model_id": "local-chat"}).json()["id"]
    without_usage = stream(base, chat_id, "без токенов")
    assert without_usage[-1]["type"] == "done"
    assert without_usage[-1]["message"]["prompt_tokens"] is None
    assert without_usage[-1]["message"]["completion_tokens"] is None
    failure = stream(base, chat_id, "ошибка сервера")
    assert failure[-1]["type"] == "error"
    assert failure[-1]["assistant"]["status"] == "failed"
    with httpx.Client(base_url=base) as client:
        assert client.get(f"/api/chats/{chat_id}").json()["messages"][-1]["status"] == "failed"
