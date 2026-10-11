(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { models: [], modelsAvailable: false, profiles: [], chats: [], suites: [], runs: [], chatId: null, profileId: null, runId: null, sending: false, savingProfile: false, savingSuite: false, startingRun: false, activeRunId: null, pollTimer: null, chatRevision: 0, runRevision: 0 };
  const na = 'н/д';
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  };
  const option = (value, label) => { const item = node('option', '', label); item.value = String(value); return item; };
  const value = (item) => item === null || item === undefined || item === '' ? na : String(item);
  const numeric = (item, digits = 2) => item === null || item === undefined || item === '' || !Number.isFinite(Number(item)) ? na : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(Number(item));
  const seconds = (item) => numeric(item) === na ? na : `${numeric(item)} с`;
  const bytes = (item) => { if (item === null || item === undefined || !Number.isFinite(Number(item))) return na; const amount = Number(item); const unit = amount >= 1024 ** 3 ? 'ГиБ' : amount >= 1024 ** 2 ? 'МиБ' : 'КиБ'; return `${numeric(amount / (unit === 'ГиБ' ? 1024 ** 3 : unit === 'МиБ' ? 1024 ** 2 : 1024))} ${unit}`; };
  const status = (item) => ({ completed: 'Завершён', complete: 'Завершён', done: 'Завершён', truncated: 'Неполный ответ', success: 'Готово', running: 'Выполняется', pending: 'В очереди', queued: 'В очереди', failed: 'Ошибка', error: 'Ошибка', interrupted: 'Прерван' })[item] || value(item);
  const isActive = (item) => ['running', 'pending', 'queued'].includes(item);
  const date = (item) => { if (!item) return na; const parsed = new Date(item); return Number.isNaN(parsed.getTime()) ? String(item) : parsed.toLocaleString('ru-RU'); };
  const idUrl = (id) => encodeURIComponent(String(id));
  function detailOf(payload) { if (typeof payload === 'string') return payload; if (Array.isArray(payload)) return payload.map(detailOf).join('; '); if (payload && typeof payload === 'object') return payload.msg || payload.message || JSON.stringify(payload); return ''; }
  function errorText(error) { return error instanceof TypeError ? 'Нет соединения с локальным приложением. Проверьте, что сервер запущен.' : (error.message || String(error)); }
  async function request(path, options = {}) {
    let response;
    try { response = await fetch(path, { ...options, headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } }); }
    catch (error) { throw new Error('Нет соединения с локальным приложением. Проверьте, что сервер запущен.'); }
    if (!response.ok) {
      let payload;
      try { payload = await response.json(); } catch { payload = null; }
      throw new Error(detailOf(payload?.detail || payload?.message) || `Ошибка сервера (HTTP ${response.status})`);
    }
    if (response.status === 204) return null;
    try { return await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ.'); }
  }
  const json = (method, body) => ({ method, body: JSON.stringify(body) });
  function notify(message) { $('notice').textContent = message; $('notice').hidden = !message; if (message) $('notice').scrollIntoView({ block: 'nearest' }); }
  async function action(work) { try { await work(); notify(''); } catch (error) { notify(errorText(error)); } }
  function empty(container, message) { container.replaceChildren(node('p', 'empty', message)); }
  function fillSelect(select, entries, placeholder, selected) {
    select.replaceChildren(option('', placeholder), ...entries.map(([key, label]) => option(key, label)));
    if (selected !== undefined && selected !== null) select.value = String(selected);
    if (select.selectedIndex < 0) select.value = '';
  }
  function setSection(section) {
    for (const tab of document.querySelectorAll('.tab')) {
      const active = tab.dataset.section === section;
      tab.classList.toggle('active', active);
      if (active) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current');
    }
    for (const pane of document.querySelectorAll('main > section')) {
      pane.hidden = pane.id !== section;
      pane.classList.toggle('active', pane.id === section);
    }
    notify('');
  }
  const modelKey = (model) => String(model.key ?? model.id ?? '');
  const variantKey = (variant) => typeof variant === 'string' ? variant : String(variant.key ?? variant.id ?? variant.path ?? variant.name ?? '');
  const variantName = (variant) => typeof variant === 'string' ? variant : String(variant.display_name ?? variant.name ?? variant.quantization?.name ?? variantKey(variant));
  function variantList(model) { return Array.isArray(model?.variants) ? model.variants : []; }
  function selectedModel() { return state.models.find((model) => modelKey(model) === $('profile-model').value); }
  function selectedVariant() { return variantList(selectedModel()).find((variant) => variantKey(variant) === $('profile-variant').value); }
  function maxContext() { return Number(selectedVariant()?.max_context_length ?? selectedModel()?.max_context_length) || null; }
  function updateVariantDetails() {
    const model = selectedModel();
    const limit = maxContext();
    const installed = !variantList(model).length || variantList(model).some((variant) => variantKey(variant) === $('profile-variant').value);
    const metadataMatches = installed && (!variantList(model).length || model.selected_variant === $('profile-variant').value);
    $('profile-context').max = limit ? String(limit) : '';
    $('context-help').textContent = limit ? `Поддерживаемый предел: ${numeric(limit, 0)} токенов.` : 'Предел контекста неизвестен; сервер дополнительно проверит настройки.';
    $('model-help').textContent = model ? `Квантование: ${metadataMatches ? value(model.quantization?.name) : na} · размер: ${metadataMatches ? bytes(model.size_bytes) : na} · предел контекста: ${limit ? numeric(limit, 0) : na}` : (state.modelsAvailable ? 'Выберите установленную модель.' : 'Список моделей недоступен. Сохранённые профили и результаты остаются доступны.');
    $('variant-help').textContent = !installed ? 'Этот вариант не установлен: сначала скачайте его в LM Studio.' : !metadataMatches ? 'Перед отправкой выберите этот вариант в LM Studio. REST API не переключает квантования; размер появится после загрузки.' : variantList(model).length > 1 ? 'Показаны метаданные выбранного в LM Studio варианта. Для автоматического сравнения разных квантований импортируйте их под разными ключами моделей.' : 'Показаны метаданные установленного варианта из LM Studio.';
  }
  function refreshVariant(preferred) {
    const model = selectedModel();
    const variants = variantList(model);
    const options = variants.map((variant) => [variantKey(variant), `${variantName(variant)}${variant.size_bytes != null ? ` · ${bytes(variant.size_bytes)}` : ''}`]);
    const currentProfile = state.profiles.find((profile) => String(profile.id) === String(state.profileId));
    if (preferred && !options.some(([key]) => key === String(preferred)) && (!model || currentProfile?.model === modelKey(model))) options.push([String(preferred), `${preferred} · сохранённый вариант`]);
    if (model && !variants.length && !options.length) options.push([modelKey(model), `${model.display_name || modelKey(model)} · единственный вариант`]);
    const selected = preferred ?? (model?.selected_variant ? variantKey(model.selected_variant) : null) ?? options[0]?.[0] ?? '';
    fillSelect($('profile-variant'), options, variants.length ? 'Выберите установленный вариант' : 'Варианты не указаны', selected);
    $('profile-variant').disabled = !variants.length;
    updateVariantDetails();
  }
  function refreshModelSelect(preferred) {
    const models = state.models.map((model) => [modelKey(model), model.display_name || modelKey(model)]).filter(([key]) => key);
    if (preferred && !models.some(([key]) => key === String(preferred))) models.push([String(preferred), `${preferred} · сохранённая модель (сейчас недоступна)`]);
    fillSelect($('profile-model'), models, state.modelsAvailable ? 'Выберите модель' : 'Список моделей недоступен', preferred);
    refreshVariant(state.profiles.find((profile) => String(profile.id) === String(state.profileId))?.variant);
  }
  async function loadModels() {
    try {
      const payload = await request('/api/models');
      state.models = Array.isArray(payload?.models) ? payload.models : [];
      state.modelsAvailable = true;
      $('model-notice').hidden = true;
      if (!state.profiles.length) await loadProfiles();
    } catch (error) {
      state.models = [];
      state.modelsAvailable = false;
      $('model-notice-text').textContent = `Модели LM Studio недоступны: ${errorText(error)} Сохранённые данные можно просматривать.`;
      $('model-notice').hidden = false;
    }
    refreshModelSelect($('profile-model').value || state.profiles.find((profile) => String(profile.id) === String(state.profileId))?.model);
  }
  async function loadProfiles() {
    state.profiles = await request('/api/profiles');
    if (!Array.isArray(state.profiles)) state.profiles = [];
    renderProfiles(); renderProfileChoices();
    if (state.profileId != null) {
      const profile = state.profiles.find((entry) => String(entry.id) === String(state.profileId));
      if (profile) editProfile(profile); else newProfile();
    } else refreshModelSelect($('profile-model').value);
  }
  function renderProfiles() {
    $('profile-count').textContent = state.profiles.length;
    const container = $('profiles-list'); container.replaceChildren();
    if (!state.profiles.length) { empty(container, 'Профилей пока нет. Создайте первый профиль.'); return; }
    for (const profile of state.profiles) {
      const button = node('button', `item-button${String(state.profileId) === String(profile.id) ? ' selected' : ''}`, profile.name);
      button.type = 'button'; button.append(node('small', '', `${profile.model || na}${profile.variant ? ` · ${profile.variant}` : ''}`));
      button.addEventListener('click', () => editProfile(profile)); container.append(button);
    }
  }
  function renderProfileChoices() {
    const entries = state.profiles.map((profile) => [profile.id, profile.name]);
    for (const id of ['chat-profile', 'run-baseline', 'run-optimized']) {
      const select = $(id); const previous = select.value;
      fillSelect(select, entries, 'Выберите профиль', previous);
      if (!select.value && entries.length) select.value = String(entries[id === 'run-optimized' && entries.length > 1 ? 1 : 0][0]);
    }
    updateBusy();
  }
  function newProfile() {
    state.profileId = null; $('profile-heading').textContent = 'Новый профиль'; $('copy-profile').disabled = true;
    $('profile-form').reset(); $('profile-name').value = ''; $('profile-temperature').value = '0.7'; $('profile-tokens').value = '512'; $('profile-context').value = '4096'; $('profile-system').value = '';
    refreshModelSelect(''); renderProfiles();
  }
  function editProfile(profile) {
    state.profileId = profile.id; $('profile-heading').textContent = 'Редактировать профиль'; $('copy-profile').disabled = false;
    $('profile-name').value = profile.name ?? ''; $('profile-temperature').value = profile.temperature ?? 0.7;
    $('profile-tokens').value = profile.max_tokens ?? 512; $('profile-context').value = profile.context_length ?? 4096;
    $('profile-system').value = profile.system_prompt ?? '';
    refreshModelSelect(profile.model); refreshVariant(profile.variant);
    renderProfiles();
  }
  function profileBody() {
    const max = maxContext(); const context = Number($('profile-context').value);
    if (max && context > max) { $('profile-context').setCustomValidity(`Максимум для выбранной модели: ${max}`); $('profile-context').reportValidity(); throw new Error(`Окно контекста не может превышать ${max} токенов.`); }
    $('profile-context').setCustomValidity('');
    return { name: $('profile-name').value.trim(), model: $('profile-model').value, variant: $('profile-variant').value || null, temperature: Number($('profile-temperature').value), max_tokens: Number($('profile-tokens').value), context_length: context, system_prompt: $('profile-system').value };
  }
  async function saveProfile(event) {
    event.preventDefault(); if (state.savingProfile) return;
    state.savingProfile = true; $('save-profile').disabled = true;
    await action(async () => {
      const body = profileBody();
      if (!body.name || !body.model) throw new Error('Укажите название и установленную модель.');
      const saved = await request(state.profileId == null ? '/api/profiles' : `/api/profiles/${idUrl(state.profileId)}`, json(state.profileId == null ? 'POST' : 'PUT', body));
      state.profileId = saved?.id ?? state.profileId;
      await loadProfiles();
    });
    state.savingProfile = false; $('save-profile').disabled = false;
  }
  async function copyProfile() {
    if (state.profileId == null) return;
    await action(async () => { const copy = await request(`/api/profiles/${idUrl(state.profileId)}/copy`, { method: 'POST' }); state.profileId = copy.id; await loadProfiles(); });
  }

  async function loadChats() {
    state.chats = await request('/api/chats'); if (!Array.isArray(state.chats)) state.chats = [];
    if (state.chatId != null && !state.chats.some((chat) => String(chat.id) === String(state.chatId))) state.chatId = null;
    renderChats();
    if (state.chatId == null && state.chats.length) await openChat(state.chats[0].id);
    else if (state.chatId == null) showNoChat();
  }
  function renderChats() {
    $('chat-count').textContent = state.chats.length;
    const list = $('chats-list'); list.replaceChildren();
    if (!state.chats.length) { empty(list, 'Диалогов пока нет. Создайте новый чат.'); return; }
    for (const chat of state.chats) {
      const button = node('button', `item-button${String(state.chatId) === String(chat.id) ? ' selected' : ''}`, chat.title || 'Без названия');
      button.type = 'button'; button.addEventListener('click', () => action(() => openChat(chat.id))); list.append(button);
    }
  }
  function showNoChat() { $('chat-title').textContent = 'Выберите или создайте чат'; empty($('messages'), 'Новый диалог начнётся здесь.'); updateBusy(); }
  async function openChat(id) {
    state.chatId = id; const revision = ++state.chatRevision;
    const chat = state.chats.find((entry) => String(entry.id) === String(id));
    $('chat-title').textContent = chat?.title || 'Без названия'; renderChats(); updateBusy();
    try { const messages = await request(`/api/chats/${idUrl(id)}/messages`); if (revision === state.chatRevision) renderMessages(Array.isArray(messages) ? messages : []); }
    catch (error) { if (revision === state.chatRevision) { empty($('messages'), `Не удалось загрузить сообщения: ${errorText(error)}`); throw error; } }
  }
  async function createChat() {
    const title = window.prompt('Название нового чата:', 'Новый диалог');
    if (title === null) return;
    if (!title.trim()) { notify('Введите название чата.'); return; }
    await action(async () => {
      const chat = await request('/api/chats', json('POST', { title: title.trim() }));
      state.chats = await request('/api/chats'); await openChat(chat.id);
      setSection('chat');
    });
  }
  async function renameChat() {
    const chat = state.chats.find((item) => String(item.id) === String(state.chatId)); if (!chat) return;
    const title = window.prompt('Новое название чата:', chat.title || ''); if (title === null) return;
    if (!title.trim()) { notify('Введите название чата.'); return; }
    await action(async () => { await request(`/api/chats/${idUrl(chat.id)}`, json('PUT', { title: title.trim() })); state.chats = await request('/api/chats'); renderChats(); $('chat-title').textContent = title.trim(); });
  }
  async function deleteChat() {
    if (state.chatId == null || !window.confirm('Удалить чат и все его сообщения?')) return;
    await action(async () => { await request(`/api/chats/${idUrl(state.chatId)}`, { method: 'DELETE' }); state.chatId = null; ++state.chatRevision; await loadChats(); });
  }
  function snapshotText(settings) {
    if (!settings || typeof settings !== 'object') return 'Снимок настроек: н/д';
    const fields = [['Модель', settings.model ?? settings.model_key], ['Вариант', settings.variant], ['Квантование', settings.quantization?.name ?? settings.quantization], ['Размер модели', settings.model_size_bytes == null ? null : bytes(settings.model_size_bytes)], ['Экземпляр', settings.instance_id], ['Контекст', settings.actual_context_length ?? settings.context_length], ['Температура', settings.temperature], ['Макс. токенов', settings.max_tokens], ['System prompt', settings.system_prompt]];
    return fields.map(([label, item]) => `${label}: ${value(item)}`).join(' · ');
  }
  function metricsText(row) {
    return `Статус: ${status(row.status)} · Время: ${seconds(row.total_seconds)} · Первый фрагмент: ${seconds(row.first_token_seconds)} · Токены: ${numeric(row.prompt_tokens, 0)} → ${numeric(row.completion_tokens, 0)} · Загрузка: ${seconds(row.load_seconds)} · RAM после/пик: ${bytes(row.ram_after_bytes)} / ${bytes(row.ram_peak_bytes)}${row.tokens_per_second == null ? '' : ` · Скорость: ${numeric(row.tokens_per_second)} ток/с`}${row.ram_reason ? ` · RAM: ${row.ram_reason}` : ''}`;
  }
  function renderMessage(row) {
    const article = node('article', `message ${row.role === 'user' ? 'user' : 'assistant'}`);
    article.append(node('div', 'message-label', row.role === 'user' ? 'Вы' : 'Модель'));
    article.append(node('div', 'message-body', row.content || (row.role === 'assistant' ? 'Ожидание ответа…' : '')));
    if (row.role !== 'user') {
      article.append(node('div', 'message-meta', snapshotText(row.settings)));
      article.append(node('div', 'message-meta', metricsText(row)));
      if (row.error) article.append(node('div', 'message-meta error', row.error));
    }
    return article;
  }
  function renderMessages(messages) {
    const container = $('messages'); container.replaceChildren();
    if (!messages.length) empty(container, 'Пока нет сообщений. Выберите профиль и задайте вопрос.');
    else container.append(...messages.map(renderMessage));
    container.scrollTop = container.scrollHeight;
  }
  function updateBusy() {
    const busy = state.sending || !!state.activeRunId;
    $('send-button').disabled = busy || state.chatId == null || !state.profiles.length;
    $('chat-input').disabled = busy || state.chatId == null;
    $('chat-profile').disabled = busy || !state.profiles.length;
    $('rename-chat').disabled = state.chatId == null || state.sending;
    $('delete-chat').disabled = state.chatId == null || state.sending;
    $('new-chat').disabled = state.sending;
    $('start-run').disabled = busy || !state.suites.length || state.profiles.length < 2 || state.startingRun;
    $('run-form').querySelectorAll('select,input').forEach((input) => { input.disabled = !!state.activeRunId || state.startingRun; });
    $('send-button').textContent = state.sending ? 'Ответ формируется…' : state.activeRunId ? 'Идёт сравнение…' : 'Отправить ↗';
  }
  async function sendMessage(event) {
    event.preventDefault();
    if (state.sending || state.activeRunId || state.chatId == null) return;
    const content = $('chat-input').value.trim(); const profileId = $('chat-profile').value;
    if (!content || !profileId) return;
    const chatId = state.chatId;
    state.sending = true; updateBusy();
    const messages = $('messages');
    if (messages.querySelector('.empty')) messages.replaceChildren();
    messages.append(renderMessage({ role: 'user', content }));
    const pending = renderMessage({ role: 'assistant', content: '' });
    const answer = pending.querySelector('.message-body'); const meta = pending.querySelector('.message-meta');
    meta.textContent = 'Ответ формируется…'; messages.append(pending); messages.scrollTop = messages.scrollHeight;
    let done = false; let streamError = '';
    try {
      let response;
      try { response = await fetch(`/api/chats/${idUrl(chatId)}/send`, { ...json('POST', { profile_id: Number(profileId), content }), headers: { 'Content-Type': 'application/json' } }); }
      catch { throw new Error('Нет соединения с локальным приложением. Проверьте, что сервер запущен.'); }
      if (!response.ok) {
        let payload; try { payload = await response.json(); } catch { payload = null; }
        throw new Error(detailOf(payload?.detail || payload?.message) || `Ошибка отправки (HTTP ${response.status})`);
      }
      if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ.');
      $('chat-input').value = '';
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      const processLine = (line) => {
        if (!line.trim()) return;
        let item; try { item = JSON.parse(line); } catch { throw new Error('Сервер вернул некорректный поток ответа.'); }
        if (item.type === 'delta') { answer.textContent += item.text ?? ''; messages.scrollTop = messages.scrollHeight; }
        else if (item.type === 'start') { meta.textContent = 'Генерация ответа…'; }
        else if (item.type === 'done') { done = true; if (item.message) pending.replaceWith(renderMessage(item.message)); }
        else if (item.type === 'error') { streamError = detailOf(item.detail) || 'Ошибка генерации ответа.'; if (item.message) pending.replaceWith(renderMessage(item.message)); }
      };
      while (true) {
        const { value: chunk, done: ended } = await reader.read();
        buffer += decoder.decode(chunk || new Uint8Array(), { stream: !ended });
        let pos; while ((pos = buffer.indexOf('\n')) !== -1) { processLine(buffer.slice(0, pos)); buffer = buffer.slice(pos + 1); }
        if (ended) { if (buffer.trim()) processLine(buffer); break; }
      }
      if (streamError) throw new Error(streamError);
      if (!done) throw new Error('Поток ответа завершился без подтверждения сохранения.');
      notify('');
    } catch (error) {
      streamError = errorText(error); notify(streamError);
      if (pending.isConnected) meta.textContent = `Ошибка: ${streamError}`;
    } finally {
      state.sending = false; updateBusy();
      if (String(state.chatId) === String(chatId)) {
        try { await openChat(chatId); } catch { /* Stream error already shown; keep available content. */ }
      }
    }
  }

  async function loadSuites() {
    state.suites = await request('/api/suites'); if (!Array.isArray(state.suites)) state.suites = [];
    fillSelect($('run-suite'), state.suites.map((suite) => [suite.id, suite.name]), 'Выберите набор', $('run-suite').value);
    if (!$('run-suite').value && state.suites.length) $('run-suite').value = String(state.suites[0].id);
    await previewSuite(); updateBusy();
  }
  async function previewSuite() {
    const id = $('run-suite').value;
    if (!id) { $('suite-preview').textContent = 'Создайте набор вопросов для запуска.'; return; }
    $('suite-preview').textContent = 'Загрузка вопросов…';
    try {
      const suite = await request(`/api/suites/${idUrl(id)}`);
      if ($('run-suite').value !== id) return;
      const cases = Array.isArray(suite.cases) ? suite.cases : [];
      $('suite-preview').textContent = `${cases.length} вопрос(ов): ${cases.map((item) => item.question).join(' · ')}`;
    } catch (error) { $('suite-preview').textContent = `Не удалось загрузить вопросы: ${errorText(error)}`; }
  }
  function addCase(question = '', criteria = '') {
    const row = node('div', 'case-row');
    const q = node('textarea'); q.rows = 2; q.required = true; q.maxLength = 10000; q.placeholder = 'Текст вопроса'; q.setAttribute('aria-label', 'Текст вопроса'); q.value = question;
    const c = node('textarea'); c.rows = 2; c.placeholder = 'Эталонные факты или критерии (необязательно)'; c.setAttribute('aria-label', 'Критерии ответа'); c.value = criteria;
    const remove = node('button', 'button subtle', 'Удалить вопрос'); remove.type = 'button'; remove.addEventListener('click', () => { if ($('case-fields').children.length > 1) row.remove(); else { q.value = ''; c.value = ''; q.focus(); } });
    row.append(q, c, remove); $('case-fields').append(row);
  }
  async function saveSuite(event) {
    event.preventDefault(); if (state.savingSuite) return;
    const cases = Array.from($('case-fields').children, (row) => ({ question: row.querySelectorAll('textarea')[0].value.trim(), criteria: row.querySelectorAll('textarea')[1].value.trim() }));
    if (!cases.length || cases.some((item) => !item.question)) { notify('Заполните все вопросы набора.'); return; }
    state.savingSuite = true; $('save-suite').disabled = true;
    await action(async () => {
      const suite = await request('/api/suites', json('POST', { name: $('suite-name').value.trim(), cases }));
      $('suite-form').reset(); $('case-fields').replaceChildren(); addCase(); await loadSuites();
      if (suite?.id != null) { $('run-suite').value = String(suite.id); await previewSuite(); }
    });
    state.savingSuite = false; $('save-suite').disabled = false;
  }
  async function loadRuns() {
    state.runs = await request('/api/runs'); if (!Array.isArray(state.runs)) state.runs = [];
    const active = state.runs.find((run) => isActive(run.status));
    state.activeRunId = active?.id ?? null;
    renderRuns(); updateBusy(); schedulePoll();
    if (state.runId != null) await openRun(state.runId);
    else if (state.runs.length) await openRun(state.runs[0].id);
  }
  function renderRuns() {
    $('run-count').textContent = state.runs.length; const list = $('runs-list'); list.replaceChildren();
    if (!state.runs.length) { empty(list, 'Запусков пока нет. Создайте набор вопросов и выберите два профиля.'); return; }
    for (const run of state.runs) {
      const button = node('button', `item-button${String(run.id) === String(state.runId) ? ' selected' : ''}`, `#${run.id} · ${status(run.status)}`);
      button.type = 'button'; button.append(node('small', '', date(run.created_at)));
      button.addEventListener('click', () => action(() => openRun(run.id))); list.append(button);
    }
  }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (state.activeRunId != null) state.pollTimer = setTimeout(() => action(pollRun), 2000);
  }
  async function pollRun() {
    const activeId = state.activeRunId; if (activeId == null) return;
    try {
      const [run, runs] = await Promise.all([request(`/api/runs/${idUrl(activeId)}`), request('/api/runs')]);
      state.runs = Array.isArray(runs) ? runs : state.runs;
      state.activeRunId = state.runs.find((item) => isActive(item.status))?.id ?? (isActive(run.status) ? activeId : null);
      if (String(state.runId) === String(activeId)) renderRun(run);
      renderRuns(); updateBusy();
    } finally { schedulePoll(); }
  }
  async function startRun(event) {
    event.preventDefault(); if (state.activeRunId || state.sending || state.startingRun) return;
    const baseline = $('run-baseline').value, optimized = $('run-optimized').value;
    if (!baseline || !optimized || baseline === optimized) { notify('Выберите два разных профиля для сравнения.'); return; }
    state.startingRun = true; updateBusy();
    await action(async () => {
      const run = await request('/api/runs', json('POST', { suite_id: Number($('run-suite').value), baseline_profile_id: Number(baseline), optimized_profile_id: Number(optimized), repeats: Number($('run-repeats').value) }));
      state.runId = run.id; state.activeRunId = run.id; await loadRuns();
      if (state.activeRunId == null) { await openRun(run.id); }
    });
    state.startingRun = false; updateBusy();
  }
  async function openRun(id) {
    state.runId = id; const revision = ++state.runRevision;
    renderRuns(); $('run-detail').hidden = false; $('run-detail').replaceChildren(node('p', 'muted', 'Загрузка результатов…'));
    try {
      const run = await request(`/api/runs/${idUrl(id)}`);
      if (revision === state.runRevision) {
        if (state.activeRunId == null && isActive(run.status)) { state.activeRunId = id; updateBusy(); schedulePoll(); }
        renderRun(run);
      }
    }
    catch (error) { if (revision === state.runRevision) $('run-detail').replaceChildren(node('p', 'notice', `Не удалось загрузить запуск: ${errorText(error)}`)); throw error; }
  }
  const table = (headers, rows) => {
    const wrap = node('div', 'table-wrap'); const element = node('table', 'metrics-table'); const head = node('thead');
    const headRow = node('tr'); headers.forEach((label) => headRow.append(node('th', '', label))); head.append(headRow);
    const body = node('tbody'); rows.forEach((row) => { const tr = node('tr'); row.forEach((item) => tr.append(node('td', '', item))); body.append(tr); });
    element.append(head, body); wrap.append(element); return wrap;
  };
  function renderSummary(run, container) {
    const panel = node('div', 'panel summary-panel'); panel.append(node('h3', '', 'Итоги · медианы и средние оценки'));
    const base = run.summary?.baseline || {}, opt = run.summary?.optimized || {};
    const rows = [
      ['Ответов завершено', 'completed', (s) => numeric(s.completed, 0)],
      ['Оценено ответов', 'rated', (s) => numeric(s.rated, 0)],
      ['Полное время · медиана', 'total_seconds', (s) => seconds(s.total_seconds)],
      ['Первый фрагмент · медиана', 'first_token_seconds', (s) => seconds(s.first_token_seconds)],
      ['Входные токены · медиана', 'prompt_tokens', (s) => numeric(s.prompt_tokens, 0)],
      ['Выходные токены · медиана', 'completion_tokens', (s) => numeric(s.completion_tokens, 0)],
      ['Скорость · медиана', 'tokens_per_second', (s) => s.tokens_per_second == null ? na : `${numeric(s.tokens_per_second)} ток/с`],
      ['RAM после · медиана', 'ram_after_bytes', (s) => bytes(s.ram_after_bytes)],
      ['RAM пик · медиана', 'ram_peak_bytes', (s) => bytes(s.ram_peak_bytes)],
      ['Точность · среднее', 'accuracy', (s) => numeric(s.accuracy)],
      ['Полнота · среднее', 'completeness', (s) => numeric(s.completeness)],
      ['Формат · среднее', 'format_score', (s) => numeric(s.format_score)]
    ];
    panel.append(table(['Метрика', 'Базовый', 'Оптимизированный', 'Разница (после − до)'], rows.map(([label, key, format]) => [label, format(base), format(opt), base[key] == null || opt[key] == null ? na : format({ [key]: opt[key] - base[key] })])));
    panel.append(node('p', 'hint', 'Медианы по сохранённым измерениям; оценки выставляет пользователь. «н/д» означает, что измерение отсутствует.'));
    container.append(panel);
  }
  function ratingSelect(label, key, current) {
    const wrapper = node('label', '', label); const select = node('select'); select.name = key; select.required = true;
    select.append(option('', 'Оценка…'));
    for (let score = 1; score <= 5; score++) select.append(option(score, String(score)));
    select.value = current == null ? '' : String(current);
    wrapper.append(select); return wrapper;
  }
  function renderResult(row, label, running) {
    const box = node('div', 'result-side'); box.append(node('h4', '', label));
    box.append(node('span', `pill${['failed', 'error', 'truncated'].includes(row.status) ? ' failed' : ''}`, status(row.status)));
    box.append(node('div', 'answer', row.answer || (row.error ? 'Ответ не получен.' : 'Пустой ответ.')));
    if (row.error) box.append(node('p', 'result-error', row.error));
    box.append(node('div', 'snapshot', snapshotText(row.settings)));
    box.append(node('p', 'message-meta', metricsText(row)));
    const form = node('form', 'rating-fields');
    form.append(ratingSelect('Точность', 'accuracy', row.accuracy), ratingSelect('Полнота', 'completeness', row.completeness), ratingSelect('Формат', 'format_score', row.format_score));
    const button = node('button', 'button subtle', 'Сохранить оценку'); button.type = 'submit'; form.append(button);
    if (running || row.status !== 'complete') { form.querySelectorAll('select').forEach((select) => { select.disabled = true; }); button.disabled = true; }
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); button.disabled = true;
      await action(async () => {
        const payload = Object.fromEntries(['accuracy', 'completeness', 'format_score'].map((key) => [key, Number(form.elements[key].value)]));
        await request(`/api/results/${idUrl(row.id)}/rating`, json('POST', payload));
        await openRun(state.runId);
      });
      button.disabled = false;
    });
    box.append(form); return box;
  }
  function renderRun(run) {
    const container = $('run-detail'); container.hidden = false; container.replaceChildren();
    const heading = node('div', 'detail-heading'); const title = node('div');
    title.append(node('p', 'eyebrow', `ЗАПУСК #${run.id} / ${date(run.created_at)}`), node('h2', '', run.suite_name || 'Результаты сравнения'), node('span', `pill${['failed', 'error'].includes(run.status) ? ' failed' : ''}`, status(run.status)));
    heading.append(title);
    const report = node('a', 'button secondary', '↓ Скачать Markdown'); report.href = `/api/runs/${idUrl(run.id)}/report`; report.setAttribute('download', ''); heading.append(report);
    container.append(heading);
    if (run.error) container.append(node('p', 'notice', `Ошибка запуска: ${run.error}`));
    if (isActive(run.status)) container.append(node('p', 'hint', 'Выполнение продолжается. Результаты обновляются автоматически каждые 2 секунды; оценка откроется по завершении.'));
    const settings = node('div', 'panel summary-panel'); settings.append(node('h3', '', 'Снимки настроек'), table(['Базовый профиль', 'Оптимизированный профиль'], [[snapshotText(run.baseline_settings), snapshotText(run.optimized_settings)]])); container.append(settings);
    if (run.summary) renderSummary(run, container);
    const results = Array.isArray(run.results) ? run.results : [];
    if (!results.length) { container.append(node('p', 'empty', isActive(run.status) ? 'Ожидание первых результатов…' : 'Ответов пока нет.')); return; }
    const groups = new Map();
    for (const result of results) {
      const key = `${result.case_id ?? result.question ?? ''}\u0000${result.repeat_index ?? 0}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(result);
    }
    const label = node('h2', '', `Ответы и оценки · ${results.length}`); label.style.marginTop = '1.5rem'; container.append(label);
    for (const group of groups.values()) {
      const first = group[0]; const card = node('article', 'panel result-card');
      card.append(node('h3', 'result-question', `${first.question || 'Вопрос'} · повтор ${Number(first.repeat_index ?? 0)}`));
      if (first.criteria) card.append(node('p', 'hint', `Критерии: ${first.criteria}`));
      const pair = node('div', 'pair');
      for (const side of ['baseline', 'optimized']) {
        const row = group.find((entry) => entry.side === side);
        pair.append(row ? renderResult(row, side === 'baseline' ? 'Базовый профиль' : 'Оптимизированный профиль', isActive(run.status)) : node('div', 'result-side muted', `${side === 'baseline' ? 'Базовый' : 'Оптимизированный'}: ожидается ответ…`));
      }
      card.append(pair); container.append(card);
    }
  }
  function bind() {
    document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => setSection(tab.dataset.section)));
    $('retry-models').addEventListener('click', () => action(loadModels));
    $('new-profile').addEventListener('click', newProfile);
    $('copy-profile').addEventListener('click', copyProfile);
    $('profile-form').addEventListener('submit', saveProfile);
    $('profile-model').addEventListener('change', () => refreshVariant());
    $('profile-variant').addEventListener('change', updateVariantDetails);
    $('profile-context').addEventListener('input', () => $('profile-context').setCustomValidity(''));
    $('new-chat').addEventListener('click', createChat);
    $('rename-chat').addEventListener('click', renameChat);
    $('delete-chat').addEventListener('click', deleteChat);
    $('send-form').addEventListener('submit', sendMessage);
    $('chat-input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('send-form').requestSubmit(); } });
    $('run-suite').addEventListener('change', previewSuite);
    $('add-case').addEventListener('click', () => addCase());
    $('suite-form').addEventListener('submit', saveSuite);
    $('run-form').addEventListener('submit', startRun);
  }
  bind(); addCase(); showNoChat(); newProfile();
  Promise.allSettled([loadProfiles(), loadChats(), loadSuites(), loadRuns(), loadModels()]).then((results) => {
    const names = ['профили', 'чаты', 'наборы вопросов', 'запуски'];
    const failures = results.slice(0, 4).flatMap((result, index) => result.status === 'rejected' ? [`${names[index]}: ${errorText(result.reason)}`] : []);
    if (failures.length) notify(`Не удалось загрузить данные (${failures.join('; ')}). Обновите страницу для повтора.`);
  });
})();
