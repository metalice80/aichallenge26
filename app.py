"""Local document search and LM Studio chat. Run: streamlit run app.py."""
from __future__ import annotations

import json
import time
from pathlib import Path

import streamlit as st

from src.chat import ChatService
from src.embeddings import LMStudioClient
from src.evaluation import compare, hit_at_5
from src.extract import ExtractionError, load_corpus, load_document
from src.index_store import Store

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CORPUS = ROOT / "corpus"
MAX_BYTES = 10 * 1024 * 1024

st.set_page_config(page_title="Локальный RAG", layout="wide")
st.title("Локальный RAG · документы и диалог")

with st.sidebar:
    st.header("LM Studio")
    url = st.text_input("Адрес API", value="http://127.0.0.1:1234/v1")
    client = LMStudioClient(base_url=url)
    if st.button("Получить список моделей"):
        try:
            st.session_state["models"] = client.list_models()
        except (ConnectionError, ValueError, OSError) as exc:
            st.error(f"LM Studio недоступен: {exc}")
    models = st.session_state.get("models", [])
    if models:
        st.caption("Доступные модели: " + ", ".join(models))
    chat_suggestion = st.selectbox("Chat-модель из списка", ["—"] + models, key="chat_choice")
    embedding_suggestion = st.selectbox("Embedding-модель из списка", ["—"] + models, key="embedding_choice")
    chat_manual = st.text_input("Или точный chat_model_id", value="")
    embedding_manual = st.text_input("Или точный embedding_model_id", value="")
    chat_model = chat_manual.strip() or (chat_suggestion if chat_suggestion != "—" else "")
    embedding_model = embedding_manual.strip() or (embedding_suggestion if embedding_suggestion != "—" else "")
    st.caption("Модели не определяются по имени автоматически. Выберите или введите точный ID.")

store = Store(db_path=DATA / "app.db", data_dir=DATA)
tab_docs, tab_compare, tab_chat = st.tabs(["Документы и индексация", "Сравнение стратегий", "Чат"])

with tab_docs:
    st.subheader("Исходные документы")
    origin = st.radio("Источник", ["Демонстрационный корпус", "Загруженные файлы"], horizontal=True)
    uploads = st.file_uploader(
        "Файлы (до 10 МБ каждый)",
        type=["md", "markdown", "txt", "py", "pdf", "rst", "json", "yaml", "yml", "toml", "js", "ts", "css", "html", "csv"],
        accept_multiple_files=True,
        disabled=origin != "Загруженные файлы",
    )
    extraction_started = time.perf_counter()
    error_pages: dict[str, int] = {}
    if origin == "Демонстрационный корпус":
        documents, errors = load_corpus(CORPUS)
    else:
        documents, errors = [], {}
        for position, upload in enumerate(uploads or []):
            if upload.size > MAX_BYTES:
                errors[f"{position + 1}: {upload.name}"] = "Файл больше 10 МБ"
                continue
            try:
                # Identifiers are labels, never filesystem paths.
                documents.append(load_document(upload.name, upload.getvalue(), source=f"upload:{position + 1}:{upload.name}"))
            except ExtractionError as exc:
                label = f"{position + 1}: {upload.name}"
                errors[label] = str(exc)
                error_pages[label] = exc.pdf_page_count
            except OSError as exc:
                errors[f"{position + 1}: {upload.name}"] = str(exc)
    st.caption(f"Этап: извлечение текста · {time.perf_counter() - extraction_started:.3f} с")
    if not documents:
        st.info("Нет пригодных документов: выберите корпус или загрузите файлы.")
    stats = {
        "Файлов": len(documents) + len(errors),
        "Страниц PDF": sum(doc.pdf_page_count for doc in documents) + sum(error_pages.values()),
        "Слов в текстовых файлах": sum(doc.word_count for doc in documents if doc.extension != ".pdf"),
        "Оценка страниц текста (500 слов/стр.)": round(sum(doc.word_count for doc in documents if doc.extension != ".pdf") / 500, 1),
        "Извлечено символов": sum(doc.char_count for doc in documents),
    }
    st.dataframe([stats], hide_index=True, use_container_width=True)
    st.dataframe([
        {"Файл": d.title, "Страниц PDF": d.pdf_page_count, "Слов": d.word_count,
         "Оценка страниц": d.estimated_pages if d.extension != ".pdf" else d.pdf_page_count,
         "Символов": d.char_count, "Ошибка": "; ".join(d.warnings)}
        for d in documents
    ] + [{"Файл": name, "Страниц PDF": error_pages.get(name, 0), "Ошибка": message}
         for name, message in errors.items()], hide_index=True, use_container_width=True)
    if errors:
        for name, message in errors.items():
            st.warning(f"{name}: {message}")
    for document in documents:
        for warning in document.warnings:
            st.warning(f"{document.title}: {warning}")
    with st.expander("Примеры извлечённого текста"):
        for document in documents[:3]:
            st.markdown(f"**{document.title}**")
            st.text(document.text[:900])
    size = st.number_input("Целевой размер чанка (символы)", min_value=200, max_value=4000, value=1000, step=100)
    overlap = st.number_input("Перекрытие длинных частей (символы)", min_value=0, max_value=int(size) - 1, value=150, step=10)
    st.caption("Обе стратегии используют один извлечённый корпус и одну embedding-модель.")
    if st.button("Индексировать обеими стратегиями", disabled=not documents):
        if not embedding_model:
            st.error("Выберите embedding-модель перед индексацией.")
        else:
            progress = st.progress(0)
            stage = st.empty()
            started = time.perf_counter()
            def on_progress(label: str, fraction: float) -> None:
                stage.info(f"Этап: {label} · {fraction:.0%} · прошло {time.perf_counter() - started:.1f} с")
                progress.progress(max(0.0, min(1.0, fraction)))
            try:
                with st.spinner("Проверка модели, разбиение и построение двух индексов…"):
                    result = store.build(documents, client, embedding_model, callback=on_progress,
                                         size=int(size), overlap=int(overlap))
                progress.progress(1.0)
                st.success(f"Индексы сохранены: {result}")
            except (ValueError, ConnectionError, OSError, RuntimeError) as exc:
                st.error(f"Индексация не завершена, предыдущая версия сохранена: {exc}")
    info = store.stats()
    if not info:
        st.info("Индекс не создан. Сначала проиндексируйте документы.")
    else:
        st.subheader("Активный индекс")
        st.dataframe([{"Стратегия": strategy, **values} for strategy, values in info.items()], hide_index=True, use_container_width=True)
        selected_strategy = st.selectbox("Чанки стратегии", ["fixed", "structural"], key="chunks_strategy")
        rows = store.list_chunks(selected_strategy, limit=100)
        if rows:
            st.dataframe([{"ID": c.chunk_id, "Источник": c.source, "Раздел": c.section,
                           "Страница": c.page, "Порядок": c.ordinal, "Символов": len(c.text),
                           "Текст": c.text} for c in rows], hide_index=True, use_container_width=True)
            chosen = st.selectbox("Полный текст чанка", range(len(rows)),
                                  format_func=lambda i: f"{rows[i].source} · {rows[i].section} · {rows[i].ordinal}")
            st.text(rows[chosen].text)

with tab_compare:
    st.subheader("Один вопрос — два индекса")
    info = store.stats()
    if len(info) < 2:
        st.info("Сначала создайте оба индекса на вкладке документов.")
    else:
        st.dataframe([{"Стратегия": key, **value} for key, value in info.items()], hide_index=True, use_container_width=True)
        question = st.text_input("Вопрос для поиска", key="comparison_question")
        if st.button("Сравнить поиск"):
            if not embedding_model:
                st.error("Выберите embedding-модель индекса.")
            elif not question.strip():
                st.warning("Введите вопрос.")
            else:
                try:
                    report = compare(question.strip(), store, client, embedding_model)
                    for column, strategy in zip(st.columns(2), ("fixed", "structural")):
                        with column:
                            st.markdown(f"### {strategy}")
                            st.write(f"Время поиска: {report[strategy]['search_seconds']:.3f} с")
                            for hit in report[strategy]["hits"]:
                                with st.container(border=True):
                                    st.markdown(f"**{hit.title} · {hit.section}** · {hit.score:.3f}")
                                    st.caption(f"{hit.source} · страница {hit.page or '—'}")
                                    st.text(hit.text[:850])
                    if not any(report[s]["hits"] for s in ("fixed", "structural")):
                        st.info("Поиск ничего не нашёл.")
                except (ValueError, ConnectionError, OSError, RuntimeError) as exc:
                    st.error(f"Поиск недоступен: {exc}")
        questions_path = CORPUS / "questions.json"
        if questions_path.exists():
            cases = json.loads(questions_path.read_text(encoding="utf-8"))
            with st.expander("Контрольные вопросы и ожидаемые источники"):
                st.dataframe(cases, hide_index=True, use_container_width=True)
            if st.button("Оценить Hit@5 на контрольных вопросах"):
                if not embedding_model:
                    st.error("Выберите embedding-модель индекса.")
                else:
                    try:
                        scores = hit_at_5(cases, store, client, embedding_model)
                        st.dataframe([{"Стратегия": key, "Hit@5": value} for key, value in scores.items()],
                                     hide_index=True, use_container_width=True)
                        st.caption("Hit@5 измеряет попадание эталонного источника в топ-5; это не оценка ответов LLM.")
                    except (ValueError, ConnectionError, OSError, RuntimeError) as exc:
                        st.error(f"Оценка недоступна: {exc}")

with tab_chat:
    st.subheader("Диалоги")
    service = ChatService(store, client, chat_model, embedding_model)
    conversations = service.list_conversations()
    if st.button("Новый диалог"):
        st.session_state["conversation_id"] = service.create_conversation()
        st.rerun()
    choices = {f"#{item['id']} · {item.get('title') or 'Новый диалог'}": item["id"] for item in conversations}
    current = st.session_state.get("conversation_id")
    selected = st.selectbox("Сохранённый диалог", ["—"] + list(choices),
                            index=next((i for i, label in enumerate(["—"] + list(choices)) if choices.get(label) == current), 0))
    if selected != "—":
        current = choices[selected]
        st.session_state["conversation_id"] = current
    mode = st.radio("Режим", ["С RAG", "Без RAG"], horizontal=True)
    strategy = st.selectbox("Стратегия поиска", ["fixed", "structural"], disabled=mode == "Без RAG")
    top_k = st.slider("top_k", min_value=1, max_value=15, value=5, disabled=mode == "Без RAG")
    if current:
        for message in service.history(current):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                for source in message.get("hits", []):
                    with st.expander(f"{source.get('source')} · {source.get('section')} · {source.get('score', 0):.3f}"):
                        st.text(source.get("text", ""))
    else:
        st.info("Создайте новый диалог или откройте сохранённый.")
    prompt = st.chat_input("Спросите о документах…", disabled=not current)
    if prompt:
        if not chat_model or (mode == "С RAG" and not embedding_model):
            st.error("Выберите chat-модель и, для RAG, embedding-модель.")
        elif mode == "С RAG" and not store.stats():
            st.error("Индекс не создан: сначала проиндексируйте документы или переключитесь на «Без RAG».")
        else:
            try:
                hits, stream = service.reply(current, prompt, rag=mode == "С RAG", strategy=strategy, top_k=top_k)
                with st.chat_message("user"):
                    st.markdown(prompt)
                with st.chat_message("assistant"):
                    st.write_stream(stream)
                    if mode == "С RAG":
                        if not hits:
                            st.info("Поиск ничего не нашёл.")
                        for hit in hits:
                            with st.expander(f"{hit.source} · {hit.section} · {hit.score:.3f}"):
                                st.caption(f"Страница: {hit.page or '—'} · {hit.chunk_id}")
                                st.text(hit.text)
                st.rerun()  # Refresh the conversation list after the streamed turn is committed.
            except (ValueError, ConnectionError, OSError, RuntimeError) as exc:
                st.error(f"Ответ LM Studio недоступен: {exc}")
