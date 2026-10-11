import asyncio
import json
import sqlite3

import httpx
import pytest

from app.main import create_app
from app.lm_studio import LMError


class FakeLM:
    def __init__(self):
        self.loaded = None
        self.selected = 'sample/python@q4'
        self.usage = True
        self.break_stream = False
        self.finish_reason = None
        self.truncate_at = None
        self.output_tokens = 2
        self.context_error = False
        self.calls = []
        self.loads = []
        self.unloads = 0

    def __call__(self, request):
        path = request.url.path
        if path == '/api/v1/models':
            return httpx.Response(200, json={'models': [{'type': 'llm', 'key': 'sample/python', 'display_name': 'Python',
                'variants': ['sample/python@q4', 'sample/python@q8'], 'selected_variant': self.selected,
                'quantization': {'name': self.selected.split('@')[-1].upper()}, 'size_bytes': 4200 if self.selected.endswith('q4') else 8400,
                'max_context_length': 8192, 'loaded_instances': [self.loaded] if self.loaded else []}]})
        if path == '/api/v1/models/unload':
            self.unloads += 1
            self.loaded = None
            return httpx.Response(200, json={'instance_id': 'loaded'})
        if path == '/api/v1/models/load':
            body = json.loads(request.content)
            if body['model'] != 'sample/python':
                return httpx.Response(404, json={'error': {'type': 'model_not_found', 'message': 'Variant keys are not loadable'}})
            self.loads.append(body)
            self.loaded = {'id': 'loaded', 'config': {'context_length': body['context_length']}}
            return httpx.Response(200, json={'status': 'loaded', 'instance_id': 'loaded', 'load_config': self.loaded['config']})
        if path == '/v1/chat/completions':
            body = json.loads(request.content)
            self.calls.append(body)
            if self.context_error:
                return httpx.Response(400, json={'error': {'type': 'context_length_exceeded', 'message': 'Prompt exceeds context window'}})
            chunks = ['data: ' + json.dumps({'choices': [{'delta': {'content': 'Ответ'}, 'finish_reason': None}]}) + '\n\n']
            if not self.break_stream:
                reason = 'length' if self.truncate_at is not None and len(self.calls) >= self.truncate_at else self.finish_reason
                if reason:
                    chunks.append('data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': reason}]}) + '\n\n')
                if self.usage:
                    chunks.append('data: ' + json.dumps({'choices': [], 'usage': {'prompt_tokens': 13, 'completion_tokens': self.output_tokens}}) + '\n\n')
                chunks.append('data: [DONE]\n\n')
            return httpx.Response(200, text=''.join(chunks), headers={'content-type': 'text/event-stream'})
        return httpx.Response(404)


async def setup(tmp_path):
    app = create_app(db_path=tmp_path / 'test.db')
    fake = FakeLM()
    await app.state.lm.http.aclose()
    app.state.lm.http = httpx.AsyncClient(base_url='http://127.0.0.1:1234', transport=httpx.MockTransport(fake))
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return app, fake, api


def profile(name='custom', variant='sample/python@q4', context=2048):
    return {'name': name, 'model': 'sample/python', 'variant': variant, 'temperature': 0.3,
            'max_tokens': 256, 'context_length': context, 'system_prompt': 'Помогай с Python'}


async def close(app, api):
    await api.aclose()
    await app.state.lm.close()


def test_chat_stream_history_errors_and_context(tmp_path):
    async def scenario():
        app, fake, api = await setup(tmp_path)
        try:
            assert (await api.get('/api/models')).status_code == 200
            assert len((await api.get('/api/profiles')).json()) == 2
            assert (await api.post('/api/profiles', json=profile(context=9000))).status_code == 422
            assert (await api.post('/api/profiles', json=profile(variant='not-installed'))).status_code == 422
            p = (await api.post('/api/profiles', json=profile())).json()
            chat = (await api.post('/api/chats', json={'title': 'Беседа'})).json()
            first = await api.post(f"/api/chats/{chat['id']}/send", json={'profile_id': p['id'], 'content': 'Первый?'})
            events = [json.loads(line) for line in first.text.splitlines()]
            assert [event['type'] for event in events] == ['start', 'delta', 'done']
            assert events[-1]['message']['settings']['context_length'] == 2048
            assert events[-1]['message']['completion_tokens'] == 2
            fake.usage = False
            second = await api.post(f"/api/chats/{chat['id']}/send", json={'profile_id': p['id'], 'content': 'Второй?'})
            assert json.loads(second.text.splitlines()[-1])['message']['completion_tokens'] is None
            assert fake.calls[-1]['messages'] == [{'role': 'system', 'content': 'Помогай с Python'},
                {'role': 'user', 'content': 'Первый?'}, {'role': 'assistant', 'content': 'Ответ'}, {'role': 'user', 'content': 'Второй?'}]
            assert fake.calls[-1]['stream_options'] == {'include_usage': True}
            assert len(fake.loads) == 2 and fake.unloads == 1  # multiple variants require verified reload
            assert (await api.put(f"/api/profiles/{p['id']}", json=profile(context=3072))).status_code == 200
            switched = await api.post(f"/api/chats/{chat['id']}/send", json={'profile_id': p['id'], 'content': 'Контекст?'})
            assert json.loads(switched.text.splitlines()[-1])['message']['settings']['context_length'] == 3072
            assert fake.loads[-1]['context_length'] == 3072 and fake.unloads == 2
            updated = profile(context=4096, variant='sample/python@q8')
            assert (await api.put(f"/api/profiles/{p['id']}", json=updated)).status_code == 200
            with pytest.raises(LMError, match='Выберите этот вариант в LM Studio'):
                await app.state.lm.prepare(updated)
            assert fake.unloads == 2  # mismatch must not unload the working variant
            fake.selected = 'sample/python@q8'  # user changes selection in LM Studio GUI
            fake.break_stream = True
            third = await api.post(f"/api/chats/{chat['id']}/send", json={'profile_id': p['id'], 'content': 'Третий?'})
            assert json.loads(third.text.splitlines()[-1])['type'] == 'error'
            saved = (await api.get(f"/api/chats/{chat['id']}/messages")).json()
            assert saved[-1]['status'] == 'error' and saved[-1]['settings']['variant'] == 'sample/python@q8'
            assert 'оборвался' in saved[-1]['error']
            assert saved[1]['settings']['variant'] == 'sample/python@q4'
            assert fake.loads[-1]['context_length'] == 4096
            assert fake.loads[-1]['model'] == 'sample/python'
            assert saved[-1]['settings']['quantization'] == 'Q8'
            # A fresh app reading the same database sees completed and failed history.
            other = create_app(db_path=tmp_path / 'test.db')
            assert len((await api.get(f"/api/chats/{chat['id']}/messages")).json()) == 8
            with sqlite3.connect(other.state.db_path) as conn:
                assert conn.execute('SELECT count(*) FROM messages WHERE chat_id=?', (chat['id'],)).fetchone()[0] == 8
            await other.state.lm.close()
        finally:
            await close(app, api)
    asyncio.run(scenario())


def test_comparison_ratings_report_clean_sessions(tmp_path):
    async def scenario():
        app, fake, api = await setup(tmp_path)
        try:
            p1 = (await api.post('/api/profiles', json=profile('base'))).json()
            p2 = (await api.post('/api/profiles', json=profile('tuned', 'sample/python@q4', 4096))).json()
            suite = (await api.post('/api/suites', json={'name': 'Python facts', 'cases': [{'question': 'Что такое list?', 'criteria': 'Упомянуть изменяемость'}, {'question': 'Что такое tuple?', 'criteria': 'Неизменяемость'}]})).json()
            q8 = (await api.post('/api/profiles', json=profile('q8', 'sample/python@q8'))).json()
            unavailable = await api.post('/api/runs', json={'suite_id': suite['id'], 'baseline_profile_id': p1['id'], 'optimized_profile_id': q8['id']})
            assert unavailable.status_code == 422 and 'переключать варианты квантования' in unavailable.json()['detail']
            assert (await api.get('/api/runs')).json() == [] and fake.calls == []
            run = (await api.post('/api/runs', json={'suite_id': suite['id'], 'baseline_profile_id': p1['id'], 'optimized_profile_id': p2['id'], 'repeats': 2})).json()
            for _ in range(100):
                detail = (await api.get(f"/api/runs/{run['id']}")).json()
                if detail['status'] != 'running':
                    break
                await asyncio.sleep(.01)
            assert detail['status'] == 'complete', detail['error']
            assert len(detail['results']) == 8
            assert len(fake.calls) == 10  # two warmups plus eight measured answers
            assert all(len(call['messages']) == 2 for call in fake.calls)
            assert detail['summary']['baseline']['completed'] == 4
            assert detail['summary']['baseline']['rated'] == 0
            result = detail['results'][0]
            assert (await api.post(f"/api/results/{result['id']}/rating", json={'accuracy': 4, 'completeness': 5, 'format_score': 3})).status_code == 200
            detail = (await api.get(f"/api/runs/{run['id']}")).json()
            assert detail['summary'][result['side']]['rated'] == 1
            assert detail['summary'][result['side']]['accuracy'] == 4
            assert {r['settings']['quantization'] for r in detail['results']} == {'Q4'}
            assert {r['settings']['model_size_bytes'] for r in detail['results']} == {4200}
            report = await api.get(f"/api/runs/{run['id']}/report")
            assert report.status_code == 200 and 'Упомянуть изменяемость' in report.text and 'Скорость = ' in report.text
            assert (await api.put(f"/api/profiles/{p1['id']}", json=profile('changed', context=3072))).status_code == 200
            assert (await api.get(f"/api/runs/{run['id']}")).json()['baseline_settings']['context_length'] == 2048
        finally:
            await close(app, api)
    asyncio.run(scenario())


def test_token_cap_preserves_incomplete_answer_and_usage(tmp_path):
    async def scenario():
        app, fake, api = await setup(tmp_path)
        try:
            limited = profile()
            limited['max_tokens'] = 2
            p = (await api.post('/api/profiles', json=limited)).json()
            chat = (await api.post('/api/chats', json={'title': 'Лимиты'})).json()
            fake.finish_reason = 'length'
            url = f"/api/chats/{chat['id']}/send"
            first = await api.post(url, json={'profile_id': p['id'], 'content': 'Расскажи подробно'})
            events = [json.loads(line) for line in first.text.splitlines()]
            assert [event['type'] for event in events] == ['start', 'delta', 'error']
            truncated = events[-1]['message']
            assert truncated['status'] == 'truncated' and truncated['content'] == 'Ответ'
            assert truncated['completion_tokens'] == 2 and truncated['prompt_tokens'] == 13
            assert 'max_tokens=2' in truncated['error'] and 'Увеличьте' in truncated['error']
            assert truncated['total_seconds'] > 0
            limited['max_tokens'] = 32
            assert (await api.put(f"/api/profiles/{p['id']}", json=limited)).status_code == 200
            fake.finish_reason = None
            second = await api.post(url, json={'profile_id': p['id'], 'content': 'Ответь короче'})
            assert json.loads(second.text.splitlines()[-1])['message']['status'] == 'complete'
            assert fake.calls[-1]['messages'] == [
                {'role': 'system', 'content': 'Помогай с Python'},
                {'role': 'user', 'content': 'Расскажи подробно'},
                {'role': 'user', 'content': 'Ответь короче'}]
            fake.finish_reason = 'length'
            fake.usage = False
            unknown = await api.post(url, json={'profile_id': p['id'], 'content': 'Ещё'})
            message = json.loads(unknown.text.splitlines()[-1])['message']
            assert message['status'] == 'truncated' and message['completion_tokens'] is None
            assert 'max_tokens=32' in message['error'] and 'окно контекста' in message['error']
            fake.context_error = True
            overflow = await api.post(url, json={'profile_id': p['id'], 'content': 'Очень длинный ввод'})
            error = json.loads(overflow.text.splitlines()[-1])['message']
            assert error['status'] == 'error' and 'context_length_exceeded' in error['error']
        finally:
            await close(app, api)
    asyncio.run(scenario())


def test_truncated_comparison_excludes_incomplete_measurement(tmp_path):
    async def scenario():
        app, fake, api = await setup(tmp_path)
        try:
            p1 = (await api.post('/api/profiles', json=profile('base'))).json()
            p2 = (await api.post('/api/profiles', json=profile('tuned'))).json()
            suite = (await api.post('/api/suites', json={'name': 'Факты', 'cases': [{'question': 'Что такое list?', 'criteria': 'Изменяемый'}]})).json()
            fake.truncate_at = 2  # warm-up succeeds, first measured answer reaches the cap
            run = (await api.post('/api/runs', json={'suite_id': suite['id'], 'baseline_profile_id': p1['id'], 'optimized_profile_id': p2['id'], 'repeats': 2})).json()
            for _ in range(100):
                detail = (await api.get(f"/api/runs/{run['id']}")).json()
                if detail['status'] != 'running':
                    break
                await asyncio.sleep(.01)
            assert detail['status'] == 'error' and len(detail['results']) == 1
            row = detail['results'][0]
            assert row['status'] == 'truncated' and row['answer'] == 'Ответ'
            assert row['completion_tokens'] == 2 and row['error']
            assert detail['summary']['baseline']['completed'] == 0
            assert (await api.post(f"/api/results/{row['id']}/rating", json={'accuracy': 5, 'completeness': 5, 'format_score': 5})).status_code == 422
            report = (await api.get(f"/api/runs/{run['id']}/report")).text
            assert 'truncated' in report and 'Ответ' in report and 'max_tokens=256' in report
        finally:
            await close(app, api)
    asyncio.run(scenario())
