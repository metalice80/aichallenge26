# Локальная LLM как приватный сервис

Проект запускает локальную языковую модель через `llama.cpp` и предоставляет:

- веб-интерфейс чата;
- OpenAI-совместимый HTTP API;
- ограничение контекста и числа параллельных запросов;
- rate limit через Nginx.

## Состав

- `compose.yml` — запуск `llama-server` и Nginx;
- `nginx.conf` — проксирование запросов и rate limit;
- `.env` — параметры модели и сервера.

## Настройка

Создайте рядом с `compose.yml` файл `.env`:

```dotenv
MODEL=Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M
CPU_THREADS=4
MAX_CONTEXT=1024
PARALLEL_REQUESTS=1
PORT=8081
```

Для Orange Pi 3 LTS с 2 ГБ RAM рекомендуется не увеличивать `PARALLEL_REQUESTS` выше `1`. Модель и кэш сохраняются в `/mnt/sd/llama-cache`.

## Запуск

Для старой версии Docker Compose:

```bash
docker-compose -f compose.yml up -d
```

Для Compose v2:

```bash
docker compose -f compose.yml up -d
```

Проверка состояния и просмотр логов:

```bash
docker-compose -f compose.yml ps
docker-compose -f compose.yml logs -f
```

После загрузки модели веб-чат доступен по адресу:

```text
http://<IP-адрес Orange Pi>:8081
```

## Проверка API

Проверка состояния:

```bash
curl http://127.0.0.1:8081/health
```

Тест чата:

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M",
    "messages": [
      {"role": "user", "content": "Что такое Orange Pi?"}
    ],
    "max_tokens": 64
  }'
```

## Ограничения

- максимальный контекст: `1024` токена на запрос;
- параллельная генерация: `1` запрос;
- rate limit: `5` запросов в минуту с одного IP;
- burst: ещё `2` запроса без задержки;
- при превышении rate limit сервер возвращает `HTTP 429`;
- при превышении контекста чат-запрос отклоняется с `HTTP 400`.

Для ручной проверки rate limit несколько раз подряд отправьте один и тот же запрос к `/v1/chat/completions`. После исчерпания лимита ответ изменится с `HTTP 200` на `HTTP 429`.

## Остановка

```bash
docker-compose -f compose.yml down
```

Кэш модели в `/mnt/sd/llama-cache` при этом сохраняется.

> Сервис рассчитан на использование в доверенной локальной сети. Перед публикацией в интернете необходимо добавить API-ключ, HTTPS и сетевые ограничения.
