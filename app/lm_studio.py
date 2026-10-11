"""Local LM Studio API integration and process RSS observations."""
import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx


class LMError(Exception):
    pass


def explain(response):
    try:
        detail = response.json()
        detail = detail.get('error', detail.get('message', detail)) if isinstance(detail, dict) else detail
    except ValueError:
        detail = response.text[:300]
    return f'LM Studio HTTP {response.status_code}: {detail}'


class LMStudio:
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.http = httpx.AsyncClient(base_url=self.url, timeout=httpx.Timeout(300, connect=4), trust_env=False)

    async def close(self):
        await self.http.aclose()

    async def models(self):
        try:
            response = await self.http.get('/api/v1/models')
            if response.status_code == 404:
                raise LMError('Требуется LM Studio 0.4.0+ с API /api/v1/models.')
            if response.is_error:
                raise LMError(explain(response))
            models = response.json()['models']
            return [m for m in models if m.get('type') == 'llm']
        except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
            raise LMError(f'LM Studio недоступен или вернул неверный список моделей: {exc}') from exc

    async def prepare(self, settings):
        models = await self.models()
        model = next((m for m in models if m['key'] == settings['model']), None)
        if not model:
            raise LMError('Выбранная модель не установлена. Скачайте её в LM Studio.')
        variant = settings['variant']
        variants = model.get('variants') or []
        if variant not in (variants or [model['key']]):
            raise LMError('Выбранный вариант квантования не установлен. Скачайте его в LM Studio.')
        limit = model.get('max_context_length')
        if limit and settings['context_length'] > limit:
            raise LMError(f'Контекстное окно превышает предел модели: {limit}.')
        if variants and model.get('selected_variant') != variant:
            raise LMError(
                f'В LM Studio выбран вариант {model.get("selected_variant") or "неизвестен"}, '
                f'а профиль требует {variant}. Выберите этот вариант в LM Studio и повторите запрос: '
                'его REST API не позволяет переключать квантования автоматически.')
        instances = model.get('loaded_instances') or []
        # Loaded instances have no variant metadata. Reload multi-variant models
        # rather than mislabeling a previously loaded variant after GUI selection.
        matching = next((x for x in instances if x.get('config', {}).get('context_length') == settings['context_length']), None) if len(variants) <= 1 else None
        load_seconds = 0.0
        if not matching:
            try:
                for instance in instances:
                    response = await self.http.post('/api/v1/models/unload', json={'instance_id': instance['id']})
                    if response.is_error:
                        raise LMError(explain(response))
                start = time.monotonic()
                response = await self.http.post('/api/v1/models/load', json={
                    'model': model['key'], 'context_length': settings['context_length'], 'echo_load_config': True})
                load_seconds = time.monotonic() - start
                if response.status_code == 404:
                    try:
                        error = response.json().get('error') or {}
                    except ValueError:
                        error = {}
                    if error.get('type') != 'model_not_found':
                        raise LMError('Требуется LM Studio 0.4.0+ с API загрузки моделей.')
                if response.is_error:
                    raise LMError('Не удалось загрузить модель (контекст или память): ' + explain(response))
                data = response.json()
                if data.get('status') != 'loaded' or data.get('load_config', {}).get('context_length') != settings['context_length']:
                    raise LMError('LM Studio не подтвердил запрошенное контекстное окно.')
                instance_id = data['instance_id']
            except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
                raise LMError(f'Ошибка загрузки модели LM Studio: {exc}') from exc
        else:
            instance_id = matching['id']
        refreshed = await self.models()
        active = next((m for m in refreshed if m['key'] == settings['model']), None)
        instance = next((x for x in (active or {}).get('loaded_instances', []) if x['id'] == instance_id), None)
        if not instance or instance.get('config', {}).get('context_length') != settings['context_length']:
            raise LMError('LM Studio не подтвердил фактически загруженное контекстное окно.')
        if variants and active.get('selected_variant') != variant:
            raise LMError('LM Studio не подтвердил выбранный вариант квантования.')
        actual = dict(settings)
        actual.update(context_length=instance['config']['context_length'], quantization=(active.get('quantization') or {}).get('name'),
                      model_size_bytes=active.get('size_bytes'), instance_id=instance_id)
        return actual, load_seconds

    async def stream(self, settings, messages):
        """Yield text fragments, usage, then finish reason; broken streams raise LMError."""
        payload = {'model': settings['instance_id'], 'messages': messages,
                   'temperature': settings['temperature'], 'max_tokens': settings['max_tokens'],
                   'stream': True, 'stream_options': {'include_usage': True}}
        try:
            async with self.http.stream('POST', '/v1/chat/completions', json=payload) as response:
                if response.is_error:
                    body = await response.aread()
                    raise LMError('Генерация LM Studio: ' + explain(response))
                usage = None
                done = False
                finish_reason = None
                async for line in response.aiter_lines():
                    if not line.startswith('data: '):
                        continue
                    raw = line[6:].strip()
                    if raw == '[DONE]':
                        done = True
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError as exc:
                        raise LMError('Повреждён поток LM Studio.') from exc
                    if chunk.get('error'):
                        raise LMError(f"Генерация LM Studio: {chunk['error']}")
                    if chunk.get('usage'):
                        usage = chunk['usage']
                    for choice in chunk.get('choices') or []:
                        delta = choice.get('delta') or {}
                        if isinstance(delta.get('content'), str) and delta['content']:
                            yield ('delta', delta['content'])
                        if choice.get('finish_reason') is not None:
                            finish_reason = choice['finish_reason']
                if not done:
                    raise LMError('Поток LM Studio оборвался до завершения.')
                yield ('usage', usage or {})
                yield ('finish', finish_reason)
        except httpx.RequestError as exc:
            raise LMError(f'LM Studio недоступен или поток прерван: {exc}') from exc


class RSSMonitor:
    """Sum RSS of LM Studio process tree, not system memory or model-exclusive RAM."""
    def __init__(self, url):
        self.reason = 'Не удалось определить процессы LM Studio (psutil или локальный процесс недоступен).'
        self.processes = []
        address = urlsplit(url)
        if address.hostname not in ('localhost', '127.0.0.1', '::1'):
            self.reason = 'LM Studio не на этом компьютере: локальный RSS нельзя отнести к удалённой модели.'
            return
        try:
            import psutil
            roots = [p for p in psutil.process_iter(['name']) if 'lm studio' in (p.info['name'] or '').lower() or 'lmstudio' in (p.info['name'] or '').lower()]
            found = {p.pid: p for root in roots for p in [root, *root.children(recursive=True)]}
            port = address.port or (443 if address.scheme == 'https' else 80)
            addresses = {'127.0.0.1', '0.0.0.0'} if address.hostname == '127.0.0.1' else {'127.0.0.1', '0.0.0.0', '::1', '::'}
            for p in found.values():
                try:
                    if any(c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.laddr.ip in addresses for c in p.net_connections(kind='inet') if c.laddr):
                        self.processes = list(found.values())
                        break
                except psutil.Error:
                    continue
            if self.processes:
                self.reason = 'RSS psutil; PID/имена: ' + ', '.join(f'{p.pid}:{p.name()}' for p in self.processes)
            else:
                self.reason = f'Не найден процесс LM Studio, слушающий порт {port}; RSS недоступен для этого сервера.'
        except Exception as exc:
            self.reason = f'Не удалось прочитать процессы LM Studio через psutil: {exc}'

    def sample(self):
        if not self.processes:
            return None
        try:
            import psutil
            values = [p.memory_info().rss for p in self.processes if p.is_running()]
            if not values:
                self.reason = 'Процессы LM Studio завершились до замера RSS.'
                return None
            return sum(values)
        except (psutil.Error, OSError) as exc:
            self.reason = f'Не удалось прочитать RSS процессов LM Studio: {exc}'
            return None

    async def peak_during(self, stop, samples):
        while not stop.is_set():
            value = self.sample()
            if value is not None:
                samples.append(value)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
