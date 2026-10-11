"""Single-user local chat and repeatable LM Studio profile comparisons."""
import asyncio
import json
import os
import statistics
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from . import storage as store
from .lm_studio import LMError, LMStudio, RSSMonitor

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    env_file = ROOT / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


class Profile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    temperature: float = Field(ge=0, le=2, allow_inf_nan=False)
    max_tokens: int = Field(ge=1, le=32768)
    context_length: int = Field(ge=256, le=1048576)
    system_prompt: str = Field(max_length=20000)


class Title(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class Send(BaseModel):
    profile_id: int
    content: str = Field(min_length=1, max_length=20000)


class Case(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    criteria: str = Field(default='', max_length=10000)


class Suite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cases: list[Case] = Field(min_length=1, max_length=100)


class Run(BaseModel):
    suite_id: int
    baseline_profile_id: int
    optimized_profile_id: int
    repeats: int = Field(default=3, ge=1, le=10)


class Rating(BaseModel):
    accuracy: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    format_score: int = Field(ge=1, le=5)


def missing(row, label):
    if row is None:
        raise HTTPException(404, f'{label} не найден')
    return row


def snapshot(profile):
    return {k: profile[k] for k in ('model', 'variant', 'temperature', 'max_tokens', 'context_length', 'system_prompt')}


def media(values):
    numbers = [value for value in values if value is not None]
    return statistics.median(numbers) if numbers else None


def summarize(results, side):
    rows = [r for r in results if r['side'] == side and r['status'] == 'complete']
    rated = [r for r in rows if all(r[k] is not None for k in ('accuracy', 'completeness', 'format_score'))]
    result = {'completed': len(rows), 'rated': len(rated)}
    for field in ('total_seconds', 'first_token_seconds', 'prompt_tokens', 'completion_tokens', 'tokens_per_second', 'ram_after_bytes', 'ram_peak_bytes'):
        result[field] = media([r[field] for r in rows])
    for field in ('accuracy', 'completeness', 'format_score'):
        result[field] = statistics.mean(r[field] for r in rated) if rated else None
    return result


def create_app(db_path=None, lm_url=None):
    load_env()
    path = str(db_path or os.getenv('DATABASE_PATH', str(ROOT / 'data' / 'app.db')))
    url = lm_url or os.getenv('LM_STUDIO_URL', 'http://127.0.0.1:1234')
    store.initialize(path)
    client = LMStudio(url)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await client.close()

    app = FastAPI(title='Оптимизация локальной LLM', lifespan=lifespan)
    app.state.db_path = path
    app.state.lm = client

    def db():
        return store.connect(path)

    def get_profile(profile_id):
        with db() as conn:
            return missing(store.one(conn, 'SELECT * FROM profiles WHERE id=?', (profile_id,)), 'Профиль')

    async def available(model, variant, context):
        models = await client.models()
        selected = next((m for m in models if m['key'] == model), None)
        if not selected:
            raise HTTPException(422, 'Модель не установлена в LM Studio. Сначала скачайте её там.')
        if variant not in (selected.get('variants') or [model]):
            raise HTTPException(422, 'Вариант квантования не установлен в LM Studio; сначала скачайте его там.')
        limit = selected.get('max_context_length')
        if limit and context > limit:
            raise HTTPException(422, f'Предел контекста этой модели: {limit}.')

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'static' / 'index.html')

    @app.get('/static/{filename}')
    async def static_file(filename: str):
        if filename not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(ROOT / 'static' / filename)

    @app.get('/api/models')
    async def models():
        try:
            items = await client.models()
            with db() as conn:
                if not store.one(conn, 'SELECT id FROM profiles LIMIT 1') and items:
                    model = items[0]
                    variant = model.get('selected_variant') or (model.get('variants') or [model['key']])[0]
                    context = min(4096, model.get('max_context_length') or 4096)
                    for name, temperature, max_tokens, prompt in (
                        ('Python · базовый', 0.8, 1024, 'Ты полезный помощник по Python.'),
                        ('Python · кандидат оптимизации', 0.2, 512, 'Ты помощник по Python. Отвечай точно, кратко, с рабочим примером и пояснением ограничений.')):
                        conn.execute('INSERT INTO profiles(name,model,variant,temperature,max_tokens,context_length,system_prompt) VALUES(?,?,?,?,?,?,?)',
                                     (name, model['key'], variant, temperature, max_tokens, context, prompt))
            return {'models': items}
        except LMError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/api/profiles')
    def profiles():
        with db() as conn:
            return store.many(conn, 'SELECT * FROM profiles ORDER BY id')

    @app.post('/api/profiles')
    async def add_profile(body: Profile):
        try:
            await available(body.model, body.variant, body.context_length)
        except LMError as exc:
            raise HTTPException(503, str(exc)) from exc
        with db() as conn:
            cursor = conn.execute('INSERT INTO profiles(name,model,variant,temperature,max_tokens,context_length,system_prompt) VALUES(?,?,?,?,?,?,?)', tuple(body.model_dump().values()))
            return store.one(conn, 'SELECT * FROM profiles WHERE id=?', (cursor.lastrowid,))

    @app.put('/api/profiles/{profile_id}')
    async def edit_profile(profile_id: int, body: Profile):
        get_profile(profile_id)
        try:
            await available(body.model, body.variant, body.context_length)
        except LMError as exc:
            raise HTTPException(503, str(exc)) from exc
        with db() as conn:
            conn.execute('UPDATE profiles SET name=?,model=?,variant=?,temperature=?,max_tokens=?,context_length=?,system_prompt=? WHERE id=?', (*body.model_dump().values(), profile_id))
            return store.one(conn, 'SELECT * FROM profiles WHERE id=?', (profile_id,))

    @app.post('/api/profiles/{profile_id}/copy')
    def copy_profile(profile_id: int):
        profile = get_profile(profile_id)
        with db() as conn:
            cursor = conn.execute('INSERT INTO profiles(name,model,variant,temperature,max_tokens,context_length,system_prompt) VALUES(?,?,?,?,?,?,?)',
                                  (profile['name'] + ' · копия', *[profile[k] for k in ('model', 'variant', 'temperature', 'max_tokens', 'context_length', 'system_prompt')]))
            return store.one(conn, 'SELECT * FROM profiles WHERE id=?', (cursor.lastrowid,))

    @app.get('/api/chats')
    def chats():
        with db() as conn:
            return store.many(conn, 'SELECT * FROM chats ORDER BY id DESC')

    @app.post('/api/chats')
    def add_chat(body: Title):
        with db() as conn:
            cursor = conn.execute('INSERT INTO chats(title) VALUES(?)', (body.title,))
            return store.one(conn, 'SELECT * FROM chats WHERE id=?', (cursor.lastrowid,))

    @app.put('/api/chats/{chat_id}')
    def edit_chat(chat_id: int, body: Title):
        with db() as conn:
            missing(store.one(conn, 'SELECT id FROM chats WHERE id=?', (chat_id,)), 'Чат')
            conn.execute('UPDATE chats SET title=? WHERE id=?', (body.title, chat_id))
            return store.one(conn, 'SELECT * FROM chats WHERE id=?', (chat_id,))

    @app.delete('/api/chats/{chat_id}')
    def delete_chat(chat_id: int):
        with db() as conn:
            missing(store.one(conn, 'SELECT id FROM chats WHERE id=?', (chat_id,)), 'Чат')
            conn.execute('DELETE FROM chats WHERE id=?', (chat_id,))
        return {'ok': True}

    @app.get('/api/chats/{chat_id}/messages')
    def chat_messages(chat_id: int):
        with db() as conn:
            missing(store.one(conn, 'SELECT id FROM chats WHERE id=?', (chat_id,)), 'Чат')
            return [store.decode(row, 'settings') for row in store.many(conn, 'SELECT * FROM messages WHERE chat_id=? ORDER BY id', (chat_id,))]

    async def generate(settings, messages, delta=None):
        monitor = RSSMonitor(client.url)
        after = monitor.sample()
        samples = []
        stop = asyncio.Event()
        watcher = asyncio.create_task(monitor.peak_during(stop, samples))
        start = time.monotonic()
        first = None
        chunks = []
        usage = {}
        finish_reason = None
        try:
            async for kind, value in client.stream(settings, messages):
                if kind == 'delta':
                    if first is None:
                        first = time.monotonic() - start
                    chunks.append(value)
                    if delta:
                        await delta(value)
                elif kind == 'usage':
                    usage = value
                else:
                    finish_reason = value
            total = time.monotonic() - start
            truncated = finish_reason == 'length'
            if truncated and usage.get('completion_tokens') is not None and usage['completion_tokens'] >= settings['max_tokens']:
                reason = f'Достигнут предел ответа max_tokens={settings["max_tokens"]}. Увеличьте его в профиле и повторите запрос; этот ответ неполный.'
            elif truncated:
                reason = (f'LM Studio остановил генерацию по ограничению длины (finish_reason=length). '
                          f'Проверьте max_tokens={settings["max_tokens"]} и окно контекста {settings["context_length"]}; этот ответ неполный.')
            else:
                reason = None
            return {'answer': ''.join(chunks), 'status': 'truncated' if truncated else 'complete', 'error': reason,
                    'total_seconds': total, 'first_token_seconds': first,
                    'prompt_tokens': usage.get('prompt_tokens'), 'completion_tokens': usage.get('completion_tokens'),
                    'tokens_per_second': usage['completion_tokens'] / total if usage.get('completion_tokens') is not None and total > 0 else None,
                    'ram_after_bytes': after, 'ram_peak_bytes': max(samples) if samples else after, 'ram_reason': monitor.reason}
        except LMError as exc:
            return {'answer': ''.join(chunks), 'status': 'error', 'error': str(exc),
                    'total_seconds': time.monotonic() - start, 'first_token_seconds': first,
                    'prompt_tokens': None, 'completion_tokens': None, 'tokens_per_second': None,
                    'ram_after_bytes': after, 'ram_peak_bytes': max(samples) if samples else after, 'ram_reason': monitor.reason}
        finally:
            stop.set()
            await watcher

    def save_message(chat_id, role, text, status, settings=None, metrics=None):
        metrics = metrics or {}
        with db() as conn:
            cursor = conn.execute('''INSERT INTO messages(chat_id,role,content,status,error,settings,total_seconds,first_token_seconds,prompt_tokens,completion_tokens,load_seconds,ram_after_bytes,ram_peak_bytes,ram_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (chat_id, role, text, status, metrics.get('error'), json.dumps(settings, ensure_ascii=False) if settings else None,
                  *[metrics.get(key) for key in ('total_seconds', 'first_token_seconds', 'prompt_tokens', 'completion_tokens', 'load_seconds', 'ram_after_bytes', 'ram_peak_bytes', 'ram_reason')]))
            return store.decode(store.one(conn, 'SELECT * FROM messages WHERE id=?', (cursor.lastrowid,)), 'settings')

    @app.post('/api/chats/{chat_id}/send')
    async def send(chat_id: int, body: Send):
        with db() as conn:
            missing(store.one(conn, 'SELECT id FROM chats WHERE id=?', (chat_id,)), 'Чат')
        profile = get_profile(body.profile_id)
        settings = snapshot(profile)

        async def events():
            # Lock covers load and stream: context cannot change mid-request.
            async with lock:
                user = save_message(chat_id, 'user', body.content, 'complete')
                yield json.dumps({'type': 'start', 'message': user}, ensure_ascii=False) + '\n'
                actual = settings
                load_seconds = None
                task = None
                streamed = []
                try:
                    actual, load_seconds = await client.prepare(settings)
                    with db() as conn:
                        history = store.many(conn, "SELECT role,content FROM messages WHERE chat_id=? AND id<=? AND status IN ('complete','legacy') ORDER BY id", (chat_id, user['id']))
                    queue = asyncio.Queue()
                    async def delta(text):
                        streamed.append(text)
                        await queue.put(text)
                    async def work():
                        return await generate(actual, ([{'role': 'system', 'content': actual['system_prompt']}] if actual['system_prompt'] else []) + history, delta)
                    task = asyncio.create_task(work())
                    while not task.done() or not queue.empty():
                        try:
                            text = await asyncio.wait_for(queue.get(), timeout=0.05)
                            yield json.dumps({'type': 'delta', 'text': text}, ensure_ascii=False) + '\n'
                        except asyncio.TimeoutError:
                            pass
                    result = await task
                except asyncio.CancelledError:
                    if task is not None:
                        task.cancel()
                    save_message(chat_id, 'assistant', ''.join(streamed), 'interrupted', actual,
                                 {'load_seconds': load_seconds, 'error': 'Клиент прервал поток.', 'ram_reason': 'Клиент прервал поток.'})
                    raise
                except LMError as exc:
                    result = {'answer': '', 'status': 'error', 'error': str(exc)}
                result['load_seconds'] = load_seconds
                saved = save_message(chat_id, 'assistant', result['answer'], result['status'], actual, result)
                if result['status'] == 'complete':
                    yield json.dumps({'type': 'done', 'message': saved}, ensure_ascii=False) + '\n'
                else:
                    yield json.dumps({'type': 'error', 'detail': result['error'], 'message': saved}, ensure_ascii=False) + '\n'
        return StreamingResponse(events(), media_type='application/x-ndjson')

    @app.get('/api/suites')
    def suites():
        with db() as conn:
            return store.many(conn, 'SELECT * FROM test_suites ORDER BY id DESC')

    @app.post('/api/suites')
    def add_suite(body: Suite):
        with db() as conn:
            cursor = conn.execute('INSERT INTO test_suites(name) VALUES(?)', (body.name,))
            suite_id = cursor.lastrowid
            conn.executemany('INSERT INTO test_cases(suite_id,question,criteria) VALUES(?,?,?)', [(suite_id, c.question, c.criteria) for c in body.cases])
            return {'id': suite_id, 'name': body.name, 'cases': store.many(conn, 'SELECT * FROM test_cases WHERE suite_id=?', (suite_id,))}

    @app.get('/api/suites/{suite_id}')
    def suite(suite_id: int):
        with db() as conn:
            row = missing(store.one(conn, 'SELECT * FROM test_suites WHERE id=?', (suite_id,)), 'Набор вопросов')
            row['cases'] = store.many(conn, 'SELECT * FROM test_cases WHERE suite_id=? ORDER BY id', (suite_id,))
            return row

    @app.get('/api/runs')
    def runs():
        with db() as conn:
            return store.many(conn, 'SELECT id,suite_id,status,error,created_at FROM experiment_runs ORDER BY id DESC')

    async def perform(run_id, cases, baseline, optimized, repeats):
        try:
            async with lock:
                for side, profile in (('baseline', baseline), ('optimized', optimized)):
                    settings = snapshot(profile)
                    try:
                        actual, load_seconds = await client.prepare(settings)
                    except LMError as exc:
                        with db() as conn:
                            conn.execute('UPDATE experiment_runs SET status=?,error=? WHERE id=?', ('error', str(exc), run_id))
                        return
                    with db() as conn:
                        conn.execute(f'UPDATE experiment_runs SET {side}_settings=? WHERE id=?', (json.dumps(actual, ensure_ascii=False), run_id))
                    # One untimed warm-up per profile, same question, clean conversation.
                    warm = [{'role': 'system', 'content': actual['system_prompt']}] if actual['system_prompt'] else []
                    warm.append({'role': 'user', 'content': cases[0]['question']})
                    warmed = await generate(actual, warm)
                    if warmed['status'] != 'complete':
                        with db() as conn:
                            conn.execute('UPDATE experiment_runs SET status=?,error=? WHERE id=?', ('error', 'Прогрев: ' + warmed['error'], run_id))
                        return
                    for case in cases:
                        for index in range(1, repeats + 1):
                            messages = ([{'role': 'system', 'content': actual['system_prompt']}] if actual['system_prompt'] else []) + [{'role': 'user', 'content': case['question']}]
                            result = await generate(actual, messages)
                            with db() as conn:
                                conn.execute('''INSERT INTO experiment_results(run_id,case_id,side,repeat_index,question,criteria,answer,status,error,settings,total_seconds,first_token_seconds,prompt_tokens,completion_tokens,tokens_per_second,load_seconds,ram_after_bytes,ram_peak_bytes,ram_reason)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                (run_id, case['id'], side, index, case['question'], case['criteria'], result['answer'], result['status'], result['error'], json.dumps(actual, ensure_ascii=False),
                                 *[result.get(k) for k in ('total_seconds', 'first_token_seconds', 'prompt_tokens', 'completion_tokens', 'tokens_per_second')], load_seconds if index == 1 and case == cases[0] else 0,
                                 result['ram_after_bytes'], result['ram_peak_bytes'], result['ram_reason']))
                            if result['status'] != 'complete':
                                with db() as conn:
                                    conn.execute('UPDATE experiment_runs SET status=?,error=? WHERE id=?', ('error', result['error'], run_id))
                                return
                with db() as conn:
                    conn.execute('UPDATE experiment_runs SET status=? WHERE id=?', ('complete', run_id))
        except Exception as exc:
            with db() as conn:
                conn.execute('UPDATE experiment_runs SET status=?,error=? WHERE id=?', ('error', str(exc), run_id))

    @app.post('/api/runs')
    async def add_run(body: Run):
        if body.baseline_profile_id == body.optimized_profile_id:
            raise HTTPException(422, 'Выберите два разных профиля.')
        baseline = get_profile(body.baseline_profile_id)
        optimized = get_profile(body.optimized_profile_id)
        if baseline['model'] == optimized['model'] and baseline['variant'] != optimized['variant']:
            raise HTTPException(
                422, 'LM Studio не позволяет приложению переключать варианты квантования одной модели '
                'через REST API. Чтобы сравнить Q4 и Q8 автоматически, импортируйте установленные файлы '
                'как две модели с разными ключами в LM Studio и выберите эти ключи в профилях.')
        with db() as conn:
            missing(store.one(conn, 'SELECT id FROM test_suites WHERE id=?', (body.suite_id,)), 'Набор вопросов')
            cases = store.many(conn, 'SELECT * FROM test_cases WHERE suite_id=? ORDER BY id', (body.suite_id,))
            cursor = conn.execute('INSERT INTO experiment_runs(suite_id,baseline_settings,optimized_settings,repeats,status) VALUES(?,?,?,?,?)',
                                  (body.suite_id, json.dumps(snapshot(baseline), ensure_ascii=False), json.dumps(snapshot(optimized), ensure_ascii=False), body.repeats, 'running'))
            run_id = cursor.lastrowid
        asyncio.create_task(perform(run_id, cases, baseline, optimized, body.repeats))
        return {'id': run_id}

    def run_detail(run_id):
        with db() as conn:
            row = missing(store.one(conn, 'SELECT r.*,s.name suite_name FROM experiment_runs r JOIN test_suites s ON s.id=r.suite_id WHERE r.id=?', (run_id,)), 'Опыт')
            store.decode(row, 'baseline_settings', 'optimized_settings')
            row['results'] = [store.decode(r, 'settings') for r in store.many(conn, 'SELECT * FROM experiment_results WHERE run_id=? ORDER BY case_id,repeat_index,side', (run_id,))]
            row['summary'] = {side: summarize(row['results'], side) for side in ('baseline', 'optimized')}
            return row

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: int):
        return run_detail(run_id)

    @app.post('/api/results/{result_id}/rating')
    def rate(result_id: int, body: Rating):
        with db() as conn:
            row = missing(store.one(conn, 'SELECT id,status FROM experiment_results WHERE id=?', (result_id,)), 'Ответ')
            if row['status'] != 'complete':
                raise HTTPException(422, 'Оценивать можно только завершённые ответы.')
            conn.execute('UPDATE experiment_results SET accuracy=?,completeness=?,format_score=? WHERE id=?',
                         (body.accuracy, body.completeness, body.format_score, result_id))
            return store.one(conn, 'SELECT * FROM experiment_results WHERE id=?', (result_id,))

    @app.get('/api/runs/{run_id}/report')
    def report(run_id: int):
        r = run_detail(run_id)
        def fmt(value):
            return 'н/д' if value is None else str(value)
        lines = [f"# Сравнение профилей · опыт {run_id}", '', f"Набор: {r['suite_name']}; дата: {r['created_at']}; статус: {r['status']}; повторы: {r['repeats']}.",
                 f"Ошибка: {fmt(r['error'])}", '', '## Методика', '',
                 'Стороны выполнялись последовательно, с отдельной чистой сессией (system prompt + один и тот же вопрос). Один прогрев на профиль исключён из агрегата. Время генерации: monotonic от начала HTTP запроса до завершения потока; время до первого текстового фрагмента отдельно. Холодная загрузка показана отдельно. Медианы вычислены по успешным измеряемым повторам. Скорость = выходные токены usage / длительность генерации в секундах; при отсутствии usage — н/д. RSS — сумма RSS процессов LM Studio и дочерних процессов, psutil, не память отдельной модели; GPU/VRAM не измеряются. Оценки: ручные средние только по полностью оценённым ответам.', '',
                 '## Снимки настроек', '']
        for side in ('baseline', 'optimized'):
            lines += [f"### {side}", '```json', json.dumps(r[side + '_settings'], indent=2, ensure_ascii=False), '```', '']
        lines += ['## Медианы и оценки', '', '| Показатель | Базовый | Оптимизированный | Разница (после − до) |', '| --- | ---: | ---: | ---: |']
        for key in ('completed', 'rated', 'total_seconds', 'first_token_seconds', 'prompt_tokens', 'completion_tokens', 'tokens_per_second', 'ram_after_bytes', 'ram_peak_bytes', 'accuracy', 'completeness', 'format_score'):
            a, b = r['summary']['baseline'][key], r['summary']['optimized'][key]
            lines.append(f'| {key} | {fmt(a)} | {fmt(b)} | {fmt(b-a) if a is not None and b is not None else "н/д"} |')
        lines += ['', '## Ответы, измерения и оценки', '']
        for x in r['results']:
            lines += [f"### {x['side']} · вопрос {x['case_id']} · повтор {x['repeat_index']}", '',
                      f"Вопрос: {x['question']}", '', f"Критерии: {x['criteria']}", '',
                      f"Статус: {x['status']}; ошибка: {fmt(x['error'])}", '', 'Снимок:','```json',json.dumps(x['settings'], ensure_ascii=False, indent=2),'```','',
                      'Ответ:', '', '```text', x['answer'].replace('```', '` ` `'), '```', '',
                      'Метрики: ' + '; '.join(f'{key}={fmt(x[key])}' for key in ('total_seconds','first_token_seconds','prompt_tokens','completion_tokens','tokens_per_second','load_seconds','ram_after_bytes','ram_peak_bytes','ram_reason')), '',
                      'Оценки: ' + '; '.join(f'{key}={fmt(x[key])}' for key in ('accuracy','completeness','format_score')), '']
        return PlainTextResponse('\n'.join(lines), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="experiment-{run_id}.md"'})

    return app


app = create_app()
