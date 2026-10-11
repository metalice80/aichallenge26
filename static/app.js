"use strict";

const $ = (id) => document.getElementById(id);
const ui = {
  health: $("health"), list: $("chat-list"), listEmpty: $("list-empty"),
  newChat: $("new-chat"), title: $("chat-title"), subtitle: $("chat-subtitle"),
  rename: $("rename-chat"), remove: $("delete-chat"),
  select: $("model-select"), refresh: $("refresh-models"), modelState: $("model-state"),
  notice: $("notice"), messages: $("messages"), composer: $("composer"),
  input: $("message-input"), send: $("send"), sendHint: $("send-hint"),
};
const state = {
  chats: [], chat: null, models: [], draftModel: null, online: false,
  loadingModels: false, operation: false, generation: null, viewVersion: 0,
};

function errorText(payload, fallback) {
  const detail = payload?.detail ?? payload?.message;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join("; ");
  return fallback;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorText(payload, `Ошибка HTTP ${response.status}`));
  return payload;
}

function showError(message) {
  ui.notice.textContent = message;
  ui.notice.hidden = false;
}
function clearError() {
  ui.notice.textContent = "";
  ui.notice.hidden = true;
}
function selectedModel() { return ui.select.value || null; }
function modelAvailable(id) { return Boolean(id && state.models.some((model) => model.id === id)); }
function isBusy() { return Boolean(state.generation || state.operation); }

function updateControls() {
  const busy = isBusy();
  const modelId = selectedModel();
  ui.newChat.disabled = busy;
  ui.rename.disabled = !state.chat || busy;
  ui.remove.disabled = !state.chat || busy;
  ui.select.disabled = busy || state.loadingModels;
  ui.refresh.disabled = busy || state.loadingModels;
  ui.input.disabled = !state.chat || busy;
  ui.send.disabled = !state.chat || busy || !state.online || !modelAvailable(modelId) || !ui.input.value.trim();
  if (state.generation) ui.sendHint.textContent = "Генерация ответа… Для перехода к другому чату поток будет прерван.";
  else if (!state.chat) ui.sendHint.textContent = "Выберите чат или создайте новый.";
  else if (!state.online) ui.sendHint.textContent = "LM Studio недоступен. Запустите локальный сервер.";
  else if (!state.models.length) ui.sendHint.textContent = "Нет доступных моделей. Загрузите модель в LM Studio.";
  else if (!modelAvailable(modelId)) ui.sendHint.textContent = "Выберите доступную модель для отправки.";
  else ui.sendHint.textContent = "До 8000 символов. История и метрики сохраняются локально.";
}

function renderChats() {
  ui.list.replaceChildren();
  ui.listEmpty.hidden = state.chats.length !== 0;
  for (const chat of state.chats) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = chat.title;
    button.title = chat.title;
    if (state.chat?.id === chat.id) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => openChat(chat.id));
    ui.list.append(button);
  }
}

function renderModelPicker() {
  const value = state.chat ? state.chat.model_id : state.draftModel;
  ui.select.replaceChildren(new Option("Модель не выбрана", ""));
  for (const model of state.models) ui.select.add(new Option(model.id, model.id));
  if (value && !modelAvailable(value)) ui.select.add(new Option(`${value} (недоступна)`, value));
  ui.select.value = value || "";
  if (state.loadingModels) ui.modelState.textContent = "Получаем список моделей…";
  else if (!state.online) ui.modelState.textContent = "LM Studio недоступен. Чаты доступны без сервера модели.";
  else if (!state.models.length) ui.modelState.textContent = "Нет загруженных моделей. Загрузите модель в LM Studio и обновите список.";
  else if (value && !modelAvailable(value)) ui.modelState.textContent = "Выбранная модель недоступна; выберите другую.";
  else if (!value) ui.modelState.textContent = "Модель не выбрана.";
  else ui.modelState.textContent = "Модель этого чата.";
  updateControls();
}

function timeLabel(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return "н/д";
  return `${(Number(ms) / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} с`;
}
function tokensLabel(count) { return count == null ? "н/д" : String(count); }
function scrollToLatest() { ui.messages.scrollTop = ui.messages.scrollHeight; }

function renderMessage(message, existing = null) {
  const row = existing || document.createElement("article");
  const assistant = message.role === "assistant";
  row.className = `message ${assistant ? "assistant" : "user"}`;
  if (assistant && message.status && message.status !== "completed") row.classList.add(message.status);
  row.replaceChildren();
  const role = document.createElement("span");
  role.className = "role";
  role.textContent = assistant ? "Модель" : "Вы";
  const content = document.createElement("div");
  content.className = "content";
  content.textContent = message.content || (assistant ? "(Ответ без текста)" : "");
  row.append(role, content);
  if (assistant) {
    const meta = document.createElement("div");
    meta.className = "meta";
    const status = { completed: "Завершён", failed: "Ошибка генерации", interrupted: "Поток прерван", streaming: "Идёт генерация" }[message.status] || "Статус неизвестен";
    meta.textContent = `${status} · Модель: ${message.model_id || "не указана"} · Время: ${timeLabel(message.duration_ms)} · Токены: вход ${tokensLabel(message.prompt_tokens)}, выход ${tokensLabel(message.completion_tokens)}`;
    if (message.time_to_first_token_ms != null) meta.textContent += ` · Первый фрагмент: ${timeLabel(message.time_to_first_token_ms)}`;
    row.append(meta);
  }
  if (message.id != null) row.dataset.messageId = String(message.id);
  return row;
}

function displayHistory(messages) {
  ui.messages.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "empty-message";
    empty.textContent = "В этом чате пока нет сообщений.";
    ui.messages.append(empty);
  } else {
    for (const message of messages) ui.messages.append(renderMessage(message));
  }
  scrollToLatest();
}

async function loadChats() {
  state.chats = await api("/api/chats");
  if (state.chat) {
    const current = state.chats.find((chat) => chat.id === state.chat.id);
    if (current) {
      state.chat.title = current.title;
      ui.title.textContent = current.title;
    }
  }
  renderChats();
}

function interruptGeneration() {
  const current = state.generation;
  if (!current) return false;
  current.cancelled = true;
  current.controller.abort();
  state.generation = null;
  updateControls();
  return true;
}

async function openChat(id, allowWhileOperating = false) {
  if ((state.operation && !allowWhileOperating) || (state.chat?.id === id && !state.generation)) return;
  const interrupted = interruptGeneration();
  const version = ++state.viewVersion;
  state.chat = null;
  ui.title.textContent = "Загружаем чат…";
  ui.subtitle.textContent = "";
  ui.messages.replaceChildren();
  renderChats();
  renderModelPicker();
  clearError();
  try {
    const data = await api(`/api/chats/${encodeURIComponent(id)}`);
    if (version !== state.viewVersion) return;
    state.chat = data.chat;
    ui.title.textContent = data.chat.title;
    ui.subtitle.textContent = "История сохраняется на этом компьютере";
    displayHistory(data.messages);
    renderChats();
    renderModelPicker();
    if (interrupted) showError("Генерация в предыдущем чате прервана. При следующем открытии чата история обновится.");
  } catch (error) {
    if (version === state.viewVersion) {
      ui.title.textContent = "Не удалось открыть чат";
      showError(`Не удалось загрузить историю: ${error.message}`);
      renderModelPicker();
    }
  }
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    state.online = health.lm_studio === "online";
    ui.health.className = `connection ${state.online ? "online" : "offline"}`;
    ui.health.textContent = state.online ? "LM Studio подключён" : `LM Studio недоступен${health.detail ? `: ${health.detail}` : ". Запустите локальный сервер."}`;
  } catch (error) {
    state.online = false;
    ui.health.className = "connection offline";
    ui.health.textContent = `Нет связи с приложением: ${error.message}`;
  }
  renderModelPicker();
}

async function refreshModels() {
  if (state.loadingModels || isBusy()) return;
  state.loadingModels = true;
  renderModelPicker();
  try {
    const result = await api("/api/models");
    state.models = Array.isArray(result.models) ? result.models.filter((model) => typeof model.id === "string" && model.id) : [];
  } catch (error) {
    state.models = [];
    showError(`Не удалось получить модели: ${error.message} — сохранённые чаты по-прежнему доступны.`);
  } finally {
    state.loadingModels = false;
    renderModelPicker();
  }
}

async function createChat() {
  if (isBusy()) return;
  state.operation = true;
  updateControls();
  try {
    const chat = await api("/api/chats", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: selectedModel() }),
    });
    await loadChats();
    await openChat(chat.id, true);
    ui.input.focus();
  } catch (error) { showError(`Не удалось создать чат: ${error.message}`); }
  finally { state.operation = false; updateControls(); }
}

async function renameChat() {
  if (!state.chat || isBusy()) return;
  const title = window.prompt("Новое название чата", state.chat.title);
  if (title === null) return;
  if (!title.trim()) { showError("Название чата не может быть пустым."); return; }
  state.operation = true;
  updateControls();
  try {
    const chat = await api(`/api/chats/${encodeURIComponent(state.chat.id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: title.trim() }),
    });
    state.chat = chat;
    ui.title.textContent = chat.title;
    await loadChats();
    clearError();
  } catch (error) { showError(`Не удалось переименовать чат: ${error.message}`); }
  finally { state.operation = false; updateControls(); }
}

async function deleteChat() {
  if (!state.chat || isBusy() || !window.confirm(`Удалить чат «${state.chat.title}» и всю переписку?`)) return;
  state.operation = true;
  updateControls();
  try {
    await api(`/api/chats/${encodeURIComponent(state.chat.id)}`, { method: "DELETE" });
    ++state.viewVersion;
    state.chat = null;
    ui.title.textContent = "Выберите чат";
    ui.subtitle.textContent = "История сохраняется на этом компьютере";
    ui.messages.replaceChildren();
    displayHistory([]);
    await loadChats();
    renderModelPicker();
    clearError();
  } catch (error) { showError(`Не удалось удалить чат: ${error.message}`); }
  finally { state.operation = false; updateControls(); }
}

async function changeModel() {
  const modelId = selectedModel();
  if (!state.chat) {
    state.draftModel = modelId;
    renderModelPicker();
    return;
  }
  state.operation = true;
  updateControls();
  try {
    state.chat = await api(`/api/chats/${encodeURIComponent(state.chat.id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model_id: modelId }),
    });
    state.draftModel = modelId;
    await loadChats();
    clearError();
  } catch (error) {
    showError(`Не удалось выбрать модель: ${error.message}`);
  } finally {
    state.operation = false;
    renderModelPicker();
  }
}

// TextDecoder(stream: true) and the persistent buffer handle both split UTF-8 characters
// and SSE records divided arbitrarily across network reads.
async function consumeSSE(response, onEvent) {
  if (!response.body) throw new Error("Сервер не предоставил поток ответа.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let fields = [];
  let terminal = false;
  const processLine = (line) => {
    if (line === "") {
      if (!fields.length) return;
      const value = fields.join("\n");
      fields = [];
      const event = JSON.parse(value);
      onEvent(event);
      if (event.type === "done" || event.type === "error") terminal = true;
    } else if (line.startsWith("data:")) {
      fields.push(line.slice(5).replace(/^ /, ""));
    }
  };
  const takeLines = () => {
    let index;
    while ((index = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      processLine(line);
    }
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      takeLines();
    }
    buffer += decoder.decode();
    takeLines();
    if (buffer) processLine(buffer.replace(/\r$/, ""));
    if (fields.length) processLine("");
    if (!terminal) throw new Error("Поток оборвался до подтверждения сохранения ответа.");
  } finally { reader.releaseLock(); }
}

async function sendMessage(event) {
  event.preventDefault();
  if (!state.chat || isBusy()) return;
  const content = ui.input.value.trim();
  const modelId = selectedModel();
  if (!content) { showError("Введите сообщение."); return; }
  if (!modelAvailable(modelId)) { showError("Выберите доступную модель перед отправкой."); return; }
  if (!state.online) { showError("LM Studio недоступен. Запустите сервер и обновите модели."); return; }
  const session = { controller: new AbortController(), chatId: state.chat.id, cancelled: false, userSaved: false, row: null, text: "", terminal: false };
  state.generation = session;
  clearError();
  updateControls();
  const ensureRow = () => {
    if (session.row) return session.row;
    const row = renderMessage({ role: "assistant", status: "streaming", model_id: modelId, content: "Генерация ответа…" });
    ui.messages.append(row);
    session.row = row;
    return row;
  };
  try {
    const response = await fetch(`/api/chats/${encodeURIComponent(session.chatId)}/messages/stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, model_id: modelId }), signal: session.controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(errorText(payload, `Ошибка HTTP ${response.status}`));
    }
    await consumeSSE(response, (part) => {
      if (session.cancelled || state.chat?.id !== session.chatId) return;
      if (part.type === "user" && part.message) {
        if (!session.userSaved) {
          const firstMessage = !ui.messages.querySelector(".message");
          const empty = ui.messages.querySelector(".empty-message");
          if (empty) empty.remove();
          ui.messages.append(renderMessage(part.message));
          session.userSaved = true;
          ui.input.value = "";
          if (firstMessage) {
            const title = part.message.content.trim();
            state.chat.title = title;
            const listed = state.chats.find((chat) => chat.id === session.chatId);
            if (listed) listed.title = title;
            ui.title.textContent = title;
            renderChats();
          }
        }
      } else if (part.type === "delta") {
        session.text += typeof part.text === "string" ? part.text : "";
        const row = ensureRow();
        row.querySelector(".content").textContent = session.text || "Генерация ответа…";
      } else if (part.type === "done" && part.message) {
        renderMessage(part.message, ensureRow());
        session.terminal = true;
      } else if (part.type === "error") {
        const row = ensureRow();
        renderMessage(part.assistant || { role: "assistant", status: part.status === "interrupted" ? "interrupted" : "failed", model_id: modelId, content: session.text }, row);
        showError(`Генерация не завершена: ${part.message || "Ошибка потока."}`);
        session.terminal = true;
      }
      scrollToLatest();
    });
    if (!session.terminal && !session.cancelled) throw new Error("Поток завершился без сохранённого ответа.");
  } catch (error) {
    if (!session.cancelled && state.chat?.id === session.chatId) {
      if (session.userSaved || session.row) {
        renderMessage({ role: "assistant", status: "interrupted", model_id: modelId, content: session.text }, ensureRow());
        showError(`Поток прерван: ${error.message}. Откройте чат снова для обновления истории.`);
      } else {
        showError(`Не удалось отправить сообщение: ${error.message}`);
      }
    }
  } finally {
    if (state.generation === session) {
      state.generation = null;
      updateControls();
      if (session.userSaved) loadChats().catch((error) => showError(`Не удалось обновить список чатов: ${error.message}`));
    }
  }
}

ui.newChat.addEventListener("click", createChat);
ui.rename.addEventListener("click", renameChat);
ui.remove.addEventListener("click", deleteChat);
ui.select.addEventListener("change", changeModel);
ui.refresh.addEventListener("click", async () => { clearError(); await checkHealth(); await refreshModels(); });
ui.input.addEventListener("input", updateControls);
ui.composer.addEventListener("submit", sendMessage);
window.addEventListener("pagehide", interruptGeneration);

async function start() {
  updateControls();
  const requests = [checkHealth(), refreshModels()];
  try {
    await loadChats();
    if (state.chats.length) await openChat(state.chats[0].id);
  } catch (error) { showError(`Не удалось загрузить чаты: ${error.message}`); }
  await Promise.all(requests);
  window.setInterval(() => { if (!state.generation) checkHealth(); }, 15000);
}
start();
