import asyncio
import sqlite3

import httpx

from app.main import create_app


def test_legacy_history_migrates_once_and_can_be_deleted(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE conversations(id INTEGER PRIMARY KEY,title TEXT,created_at TEXT);'
                         'CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id INTEGER,role TEXT,content TEXT,created_at TEXT);'
                         "INSERT INTO conversations VALUES(7,'Старый чат','2025-01-01');"
                         "INSERT INTO messages VALUES(11,7,'user','Привет','2025-01-01');"
                         "INSERT INTO messages VALUES(12,7,'assistant','Здравствуйте','2025-01-02');")
    app = create_app(db_path=path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id,title FROM chats').fetchall() == [(7, 'Старый чат')]
        assert db.execute('SELECT content,status FROM messages ORDER BY id').fetchall() == [('Привет', 'legacy'), ('Здравствуйте', 'legacy')]
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('DELETE FROM chats WHERE id=7')
        db.commit()
    second = create_app(db_path=path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM chats').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM legacy_messages').fetchone()[0] == 2
    async def close():
        await app.state.lm.close()
        await second.state.lm.close()
    asyncio.run(close())


def test_offline_models_and_failed_send_preserve_existing_chat(tmp_path):
    async def scenario():
        app = create_app(db_path=tmp_path / 'offline.db')
        def offline(request):
            raise httpx.ConnectError('connection refused')
        await app.state.lm.http.aclose()
        app.state.lm.http = httpx.AsyncClient(base_url='http://127.0.0.1:1234', transport=httpx.MockTransport(offline))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
            chat = (await api.post('/api/chats', json={'title': 'Сохранённый'})).json()
            assert (await api.get('/api/models')).status_code == 503
            assert (await api.get('/api/chats')).json()[0]['title'] == 'Сохранённый'
            with sqlite3.connect(app.state.db_path) as conn:
                conn.execute('INSERT INTO profiles(name,model,variant,temperature,max_tokens,context_length,system_prompt) VALUES(?,?,?,?,?,?,?)',
                             ('offline','sample/python','sample/python@q4',0.2,100,2048,''))
                conn.commit()
            result = await api.post(f"/api/chats/{chat['id']}/send", json={'profile_id': 1, 'content': 'Тест'})
            assert '"type": "error"' in result.text
            saved = (await api.get(f"/api/chats/{chat['id']}/messages")).json()
            assert len(saved) == 2 and saved[-1]['status'] == 'error'
        await app.state.lm.close()
    asyncio.run(scenario())
