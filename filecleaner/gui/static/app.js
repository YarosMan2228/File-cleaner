'use strict';
/* File Cleaner — окно программы. Данные — с локального сервера (filecleaner/gui/app.py). */

// ================================================================ язык
// Фразы в коде — русские; для английского берётся перевод из i18n.js. Язык сервер ставит в <html lang>.
const LANG = document.documentElement.lang === 'en' ? 'en' : 'ru';
const LOCALE = LANG === 'en' ? 'en-GB' : 'ru-RU';
const DICT = LANG === 'en' ? (window.I18N_EN || {}) : {};
const PLURALS_EN = window.I18N_PLURALS_EN || {};

/** Фраза на языке окна; {0}, {1}… заменяются аргументами. */
function t(text, ...args) {
  const template = DICT[text] || text;
  return args.length ? template.replace(/\{(\d+)\}/g, (match, i) => String(args[i] ?? '')) : template;
}

/** Перевод неподвижной разметки index.html: текстовые узлы и подписи для экранного диктора. */
function translateStatic() {
  if (LANG !== 'en') return;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const key = node.nodeValue.replace(/\s+/g, ' ').trim();
    if (key && DICT[key]) node.nodeValue = node.nodeValue.replace(/\S[\s\S]*\S|\S/, DICT[key]);
  }
  for (const el of document.querySelectorAll('[placeholder], [aria-label], [title]')) {
    for (const attr of ['placeholder', 'aria-label', 'title']) {
      const value = el.getAttribute(attr);
      if (value && DICT[value]) el.setAttribute(attr, DICT[value]);
    }
  }
}

// ================================================================ доступ к серверу
const HASH = new URLSearchParams(location.hash.slice(1));  // #token=…&view=review

const TOKEN = (() => {
  const token = HASH.get('token');
  if (token) {
    try { sessionStorage.setItem('fc-token', token); } catch { /* хранилище недоступно — ничего */ }
    history.replaceState(null, '', location.pathname);  // токен не остаётся в адресе
    return token;
  }
  try { return sessionStorage.getItem('fc-token') || ''; } catch { return ''; }
})();

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'X-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await response.json(); } catch { /* пустой ответ */ }
  if (!response.ok) throw new Error((data && data.error) || t('Ошибка {0}', response.status));
  return data;
}

// ================================================================ помощники
const $ = (selector) => document.querySelector(selector);

/** Элемент из тега, свойств и детей. Текст всегда вставляется как текст — имена файлов не «исполняются». */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === false || value == null) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const number = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });

function size(bytes) {
  const units = [t('Б'), t('КБ'), t('МБ'), t('ГБ'), t('ТБ')];
  let value = bytes || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit === 0 ? value : number.format(value)} ${units[unit]}`;
}

function plural(n, one, few, many) {
  const en = LANG === 'en' && PLURALS_EN[one];
  if (en) return `${n.toLocaleString(LOCALE)} ${n === 1 ? en[0] : en[1]}`;
  const n10 = n % 10;
  const n100 = n % 100;
  const word = n10 === 1 && n100 !== 11 ? one
    : n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14) ? few : many;
  return `${n.toLocaleString(LOCALE)} ${word}`;
}
const objects = (n) => plural(n, 'объект', 'объекта', 'объектов');  // русские формы — ключ для английских
const total = (items) => items.reduce((sum, item) => sum + (item.size || 0), 0);

function when(value) {
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return date.toLocaleString(LOCALE, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

let toastTimer = 0;
function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 9000 : 6000);
}

function confirmDialog({ title, text, ok, danger = true }) {
  const dialog = $('#confirm');
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  const okButton = $('#confirm-ok');
  okButton.textContent = ok;
  okButton.className = danger ? 'btn danger' : 'btn primary';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    dialog.returnValue = '';
    dialog.showModal();
    $('#confirm-cancel').focus();  // по умолчанию — безопасный выбор
  });
}

function stat(value, label) {
  return h('div', { class: 'stat' }, h('b', {}, value), h('span', {}, label));
}

/** Таблица «первые N из списка» внутри раскрывающегося блока. */
function listBlock(title, group, columns, cells) {
  if (!group.count) return null;
  const rows = group.items.map((item) => h('tr', {}, cells(item).map((cell, i) =>
    h('td', { class: columns[i].num ? 'num' : false }, cell))));
  const more = group.count > group.items.length
    ? h('p', { class: 'more' }, t('…и ещё {0} — все есть в отчёте.', objects(group.count - group.items.length))) : null;
  return h('details', {},
    h('summary', {}, title, h('small', {}, `${objects(group.count)} · ${size(group.bytes)}`)),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c.num ? 'num' : false }, c.title)))),
      h('tbody', {}, rows))),
    more);
}

// ================================================================ состояние
const state = {
  view: 'home',
  status: null,
  task: null,
  plan: null,
  result: null,
  review: [],
  deselected: new Set(),   // что ты снял; всё остальное в «На решение» выбрано
  filter: '',
  busy: false,
  settings: null,          // как сохранено
  draft: null,             // что сейчас в форме
  aiSetup: null,           // установлена ли Ollama, запущена ли, есть ли модель
};

// ================================================================ вкладки
const VIEWS = ['home', 'review', 'history', 'settings'];

function showView(name, focusTab = false) {
  for (const view of VIEWS) {
    const active = view === name;
    const tab = $(`#tab-${view}`);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    $(`#view-${view}`).hidden = !active;
    if (active && focusTab) tab.focus();
  }
  state.view = name;
  if (name === 'review') loadReview();
  if (name === 'history') loadHistory();
  if (name === 'settings' && !settingsDirty()) loadSettings();  // несохранённое не затираем
}

function bindTabs() {
  VIEWS.forEach((view, index) => {
    const tab = $(`#tab-${view}`);
    tab.addEventListener('click', () => showView(view));
    tab.addEventListener('keydown', (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      showView(VIEWS[(index + step + VIEWS.length) % VIEWS.length], true);
    });
  });
}

// ================================================================ статус и главная
async function refreshStatus(first = false) {
  try {
    state.status = await api('/api/status');
  } catch (error) {
    $('#ai-text').textContent = TOKEN ? t('Программа закрыта — запусти её снова') : t('Открой окно через File Cleaner');
    setBusy(true);
    return;
  }
  renderStatus();
  const ai = state.status.ai;
  if (ai.enabled && !(ai.local && ai.available)) refreshAiSetup();
  else $('#ai-panel').hidden = true;
  const task = state.status.task;
  if (task && task.running && !taskTimer) {
    state.task = task;
    renderTask();
    watchTask();
  } else if (first && task && !task.running) {
    state.task = task;
    onTaskFinished(task, true);
  }
}

const openUpdate = () => api('/api/open', { what: 'update' }).catch((error) => toast(error.message, true));

function renderStatus() {
  const { ai, review, last, update } = state.status;
  const fresh = $('#update-chip');
  fresh.hidden = !update;
  fresh.textContent = update ? t('Вышла версия {0}', update.version) : '';
  fresh.title = update ? update.notes : '';
  const chip = $('#ai-status');
  chip.classList.toggle('ok', ai.enabled && ai.local && ai.available);
  chip.classList.toggle('warn', ai.enabled && !(ai.local && ai.available));
  $('#ai-text').textContent = !ai.enabled ? t('ИИ выключен в правилах')
    : !ai.local ? t('ИИ выключен: модель не на этом компьютере')
      : ai.available ? t('ИИ: {0} готов', ai.model) : t('ИИ: запусти Ollama');

  const count = $('#review-count');
  count.hidden = !review.count;
  count.textContent = review.count ? review.count.toLocaleString(LOCALE) : '';

  const line = $('#last-run');
  line.replaceChildren();
  if (last) {
    line.append(t('Прошлый запуск: {0} — освобождено {1}, разложено {2}.', when(last.finished), size(last.freed), objects(last.sorted)));
    if (last.report) {
      line.append(' ', h('button', { type: 'button', class: 'link', onclick: () => openReport(last.report) }, t('Отчёт')));
    }
  }
}

// ---------------------------------------------------------------- первый запуск ИИ
async function refreshAiSetup() {
  try { state.aiSetup = await api('/api/ai-setup'); } catch { return; }
  renderAiSetup();
}

function setupStep(done, title, hint, action) {
  return h('li', { class: done ? 'done' : 'todo' },
    h('span', { class: 'mark', 'aria-hidden': 'true' }, done ? '✓' : '·'),
    h('div', {}, h('strong', {}, title), h('span', { class: 'sr-only' }, done ? t(' — готово') : t(' — нужно сделать')),
      hint ? h('span', { class: 'sub' }, hint) : null),
    action);
}

function renderAiSetup() {
  const setup = state.aiSetup;
  const panel = $('#ai-panel');
  panel.hidden = !setup || !setup.enabled || setup.ready;
  if (panel.hidden) return;
  const pulling = Boolean(state.task && state.task.running && state.task.kind === 'pull');
  const button = (label, onclick, disabled = false) => h('button', {
    type: 'button', class: 'btn small primary', onclick, disabled,
  }, label);
  let steps;
  if (!setup.local) {
    steps = [setupStep(false, t('Адрес модели не на этом компьютере'),
      t('Ради приватности ИИ выключен: файлы не должны уходить в сеть. Верни адрес http://localhost:11434 в файле правил.'))];
  } else {
    steps = [
      setupStep(setup.installed, t('Ollama установлена'),
        setup.installed ? null : t('Скачай и установи её с официального сайта, потом вернись сюда — я проверю сам.'),
        setup.installed ? null : button(t('Открыть сайт Ollama'), () => api('/api/open', { what: 'ollama-site' })
          .catch((e) => toast(e.message, true)))),
      setupStep(setup.running, t('Ollama запущена'),
        setup.running ? null : t('Она работает в фоне, значок — возле часов.'),
        !setup.running && setup.installed ? button(t('Запустить Ollama'), startOllama) : null),
      setupStep(setup.has_model, t('Модель {0} скачана', setup.model),
        setup.has_model ? null
          : setup.running ? t('Около 4,7 ГБ, скачивается один раз.') : t('Проверю, когда Ollama запустится.'),
        !setup.has_model && setup.running
          ? button(pulling ? t('Скачиваю…') : t('Скачать модель'), () => startTask('/api/ai/pull', {}), pulling) : null),
    ];
  }
  $('#ai-steps').replaceChildren(...steps);
}

async function startOllama() {
  try {
    const result = await api('/api/ai/start', {});
    if (!result.started) { toast(t('Не нашёл Ollama — установи её с сайта.'), true); return; }
    toast(t('Запускаю Ollama — это займёт несколько секунд.'));
    for (let i = 0; i < 10; i += 1) {  // проверяем, поднялась ли
      await new Promise((resolve) => setTimeout(resolve, 3000));
      await refreshAiSetup();
      if (state.aiSetup && state.aiSetup.running) break;
    }
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function bindAiSetup() {
  $('#btn-ai-recheck').addEventListener('click', async () => { await refreshAiSetup(); refreshStatus(); });
  $('#btn-ai-off').addEventListener('click', async () => {
    try {
      await api('/api/ai/disable', {});
      toast(t('ИИ выключен: раскладываю только по правилам. Включить можно в «Настройках».'));
      state.settings = null;
      refreshStatus();
    } catch (error) {
      toast(error.message, true);
    }
  });
  $('#update-chip').addEventListener('click', openUpdate);
  $('#ai-status').addEventListener('click', () => {
    const ai = state.status && state.status.ai;
    if (ai && !ai.enabled) { showView('settings'); return; }
    showView('home');
    if (!$('#ai-panel').hidden) $('#ai-panel').focus();
  });
}

function setBusy(busy) {
  state.busy = busy;
  $('#btn-preview').disabled = busy;
  $('#btn-run').disabled = busy || !state.plan;
  updateSelection();
}

async function openReport(path) {
  try { await api('/api/open', { what: 'report', path }); } catch (error) { toast(error.message, true); }
}

// ---------------------------------------------------------------- долгая операция
let taskTimer = 0;

function watchTask() {
  if (taskTimer) return;
  taskTimer = setInterval(async () => {
    let task;
    try { task = await api('/api/task'); } catch { return; }
    state.task = task;
    renderTask();
    if (!task || !task.running) {
      clearInterval(taskTimer);
      taskTimer = 0;
      onTaskFinished(task);
    }
  }, 1000);
}

function renderTask() {
  const task = state.task;
  const panel = $('#task-panel');
  if (!task || !task.running) {
    panel.hidden = true;
    setBusy(false);
    return;
  }
  panel.hidden = false;
  setBusy(true);
  $('#task-title').textContent = task.title;
  $('#task-progress').textContent = task.progress || t('Работаю…');
  const bar = $('#task-bar');
  const known = typeof task.fraction === 'number';
  bar.classList.toggle('determinate', known);
  bar.firstElementChild.style.width = known ? `${Math.round(task.fraction * 100)}%` : '';
  if (known) bar.setAttribute('aria-valuenow', String(Math.round(task.fraction * 100)));
  else bar.removeAttribute('aria-valuenow');
  const log = $('#task-log');
  log.replaceChildren(...task.log.slice(-14).map((line) => h('li', {}, line)));
  log.scrollTop = log.scrollHeight;
  $('#btn-stay').hidden = typeof task.countdown !== 'number';
}

function onTaskFinished(task, quiet = false) {
  if (!task) return;
  if (task.error && !quiet) toast(t('Не получилось: {0}', task.error), true);
  if (task.kind === 'preview' && task.result) {
    state.plan = task.result;
    state.result = null;
    renderPlan();
  }
  if (task.kind === 'pull' && !task.error && !quiet) {
    toast(t('Модель скачана — ИИ готов раскладывать.'));
  }
  if (task.kind === 'night' && task.result) {
    state.result = task.result;
    state.plan = null;
    renderResult();
    if (!quiet) toast(t('Готово: освобождено {0}, разложено {1}.', size(task.result.freed), objects(task.result.sorted)));
  }
  setBusy(false);
  if (!quiet) {
    refreshStatus();
    if (state.view === 'review') loadReview();
  }
}

async function startTask(path, body) {
  try {
    state.task = await api(path, body);
    renderTask();
    watchTask();
  } catch (error) {
    toast(error.message, true);
  }
}

// ---------------------------------------------------------------- «что будет» и «готово»
function renderPlan() {
  const plan = state.plan;
  $('#result-panel').hidden = true;
  $('#plan-panel').hidden = !plan;
  if (!plan) return;
  $('#plan-stats').replaceChildren(
    stat(size(plan.junk.bytes), t('кэши и временное, {0} — удалю сразу', objects(plan.junk.count))),
    stat(size(plan.auto.bytes), t('лишние копии, {0} — удалю, сверив с оригиналом', objects(plan.auto.count))),
    stat(size(plan.waiting.bytes), t('спорное, {0} — отложу на решение', objects(plan.waiting.count))),
    stat(size(plan.report.bytes), t('только покажу в отчёте — не трону')),
  );
  const sortLine = plan.sort.length ? h('p', {}, t('Разложу: '),
    plan.sort.map((f) => `${f.path}${f.subfolders ? '' : t(' (только файлы)')}`).join(', '), '.') : null;
  $('#plan-details').replaceChildren(...[
    listBlock(t('Удалю сразу — копии'), plan.auto,
      [{ title: t('Файл') }, { title: t('Размер'), num: true }, { title: t('Что останется') }],
      (item) => [item.path, size(item.size), item.keep || '—']),
    listBlock(t('Отложу на решение'), plan.waiting,
      [{ title: t('Файл') }, { title: t('Размер'), num: true }, { title: t('Почему') }],
      (item) => [item.path, size(item.size), item.reason]),
    sortLine,
    plan.notes.length ? h('ul', { class: 'notes' }, plan.notes.map((note) => h('li', {}, note))) : null,
  ].filter(Boolean));
  $('#btn-run').disabled = state.busy;
  $('#btn-run').classList.add('primary');
  $('#btn-preview').classList.remove('primary');
  $('#run-hint').textContent = t('Можно приступать. Разложилось не так — «История» → «Отменить».');
}

function renderResult() {
  const result = state.result;
  $('#plan-panel').hidden = true;
  $('#result-panel').hidden = !result;
  if (!result) return;
  $('#result-stats').replaceChildren(
    stat(size(result.freed), t('освобождено: кэши {0}, копии {1}', size(result.junk_freed), size(result.auto_freed))),
    stat(result.sorted.toLocaleString(LOCALE), t('разложено по папкам')),
    stat(size(result.waiting_bytes), t('ждёт решения, {0}', objects(result.waiting))),
  );
  const details = [];
  if (result.notes.length) details.push(h('ul', { class: 'notes' }, result.notes.map((n) => h('li', {}, n))));
  if (result.errors_count) {
    details.push(h('details', {},
      h('summary', { class: 'errors' }, t('Не получилось: {0}', objects(result.errors_count))),
      h('ul', { class: 'notes' }, result.errors.map((e) => h('li', {}, e)))));
  }
  $('#result-details').replaceChildren(...details);
  $('#btn-result-report').hidden = !result.report;
  $('#btn-result-review').hidden = !result.waiting;
  $('#btn-run').disabled = true;
  $('#btn-run').classList.remove('primary');
  $('#btn-preview').classList.add('primary');
  $('#run-hint').textContent = t('Чтобы запустить ещё раз, снова посмотри, что будет.');
}

function bindHome() {
  $('#btn-preview').addEventListener('click', () => startTask('/api/preview', {}));
  $('#btn-run').addEventListener('click', async () => {
    const sleep = $('#opt-sleep').checked;
    const ok = await confirmDialog({
      title: t('Приступить?'),
      text: t('Удалю кэши и проверенные копии, спорное отложу на решение, разложу папки.{0} Можно уйти — всё сделается само.', sleep ? t(' В конце усыплю компьютер.') : ''),
      ok: t('Приступить'),
      danger: false,
    });
    if (ok) startTask('/api/night', { after: sleep ? 'sleep' : 'nothing' });
  });
  $('#btn-stay').addEventListener('click', async () => {
    try { await api('/api/stay-awake', {}); toast(t('Хорошо, компьютер не усыплю.')); } catch (e) { toast(e.message, true); }
  });
  $('#btn-result-report').addEventListener('click', () => state.result && openReport(state.result.report));
  $('#btn-result-review').addEventListener('click', () => showView('review'));
}

// ================================================================ на решение
function allItems() {
  return state.review.flatMap((batch) => batch.items.map((item) => ({
    ...item, batch: batch.batch, key: `${batch.batch}\u0000${item.id}`,
  })));
}

function visibleItems() {
  const filter = state.filter.trim().toLowerCase();
  const items = allItems();
  if (!filter) return items;
  return items.filter((i) => `${i.name} ${i.from} ${i.reason} ${i.group}`.toLowerCase().includes(filter));
}

const isSelected = (item) => !state.deselected.has(item.key);

function setSelected(item, selected) {
  if (selected) state.deselected.delete(item.key); else state.deselected.add(item.key);
}

function selectedItems() {
  return visibleItems().filter(isSelected);  // действуем только на то, что видно
}

function updateSelection() {
  const chosen = selectedItems();
  const label = $('#review-selected');
  if (!label) return;
  label.textContent = chosen.length ? t('Выбрано {0} · {1}', objects(chosen.length), size(total(chosen))) : t('Ничего не выбрано');
  $('#btn-delete').disabled = state.busy || !chosen.length;
  $('#btn-restore').disabled = state.busy || !chosen.length;
}

async function loadReview() {
  try {
    state.review = await api('/api/review');
  } catch (error) {
    $('#review-list').replaceChildren(h('p', { class: 'errors' }, t('Не удалось загрузить: {0}', error.message)));
    return;
  }
  renderReview();
}

function renderReview() {
  const items = allItems();
  const list = $('#review-list');
  $('#review-meta').textContent = items.length ? `${objects(items.length)} · ${size(total(items))}` : '';
  $('#review-toolbar').hidden = !items.length;
  $('#review-actions').hidden = !items.length;
  if (!items.length) {
    list.replaceChildren(h('div', { class: 'panel empty' },
      h('h2', {}, t('Ничего не ждёт решения')),
      h('p', {}, t('Когда программа отложит спорные файлы, они появятся здесь.'))));
    return;
  }
  const groups = new Map();
  for (const item of visibleItems()) {
    if (!groups.has(item.group)) groups.set(item.group, []);
    groups.get(item.group).push(item);
  }
  const sections = [...groups.entries()]
    .sort((a, b) => total(b[1]) - total(a[1]))
    .map(([name, rows]) => groupSection(name, rows));
  list.replaceChildren(...(sections.length ? sections : [h('p', { class: 'empty' }, t('По такому запросу ничего нет.'))]));
  updateSelection();
}

function groupSection(name, rows) {
  rows.sort((a, b) => b.size - a.size);
  const groupBox = h('input', { type: 'checkbox', 'aria-label': t('Выбрать всю группу «{0}»', name) });
  const boxes = [];
  const syncGroup = () => {
    const chosen = rows.filter(isSelected).length;
    groupBox.checked = chosen === rows.length;
    groupBox.indeterminate = chosen > 0 && chosen < rows.length;
  };
  groupBox.addEventListener('change', () => {
    rows.forEach((row, i) => { setSelected(row, groupBox.checked); boxes[i].checked = groupBox.checked; });
    syncGroup();
    updateSelection();
  });
  const body = rows.map((item) => {
    const box = h('input', { type: 'checkbox', 'aria-label': t('Выбрать «{0}»', item.name) });
    box.checked = isSelected(item);
    box.addEventListener('change', () => { setSelected(item, box.checked); syncGroup(); updateSelection(); });
    boxes.push(box);
    const reveal = h('button', {
      type: 'button', class: 'link',
      onclick: () => api('/api/reveal', { batch: item.batch, id: item.id }).catch((e) => toast(e.message, true)),
    }, t('Показать'));
    return h('tr', {},
      h('td', { class: 'pick' }, box),
      h('td', { class: 'name' }, h('strong', {}, item.name), h('span', { class: 'sub' }, t('из {0}', item.from))),
      h('td', { class: 'num' }, size(item.size)),
      h('td', {}, item.reason),
      h('td', {}, item.keep || '—'),
      h('td', { class: 'act' }, reveal));
  });
  syncGroup();
  return h('section', { class: 'group', 'aria-label': name },
    h('div', { class: 'group-head' }, groupBox, h('h2', {}, name),
      h('span', { class: 'meta' }, `${objects(rows.length)} · ${size(total(rows))}`)),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {},
        h('th', { class: 'pick' }, h('span', { class: 'sr-only' }, t('Выбор'))),
        h('th', {}, t('Файл')), h('th', { class: 'num' }, t('Размер')), h('th', {}, t('Почему здесь')),
        h('th', {}, t('Что останется')), h('th', {}, h('span', { class: 'sr-only' }, t('Показать в Проводнике'))))),
      h('tbody', {}, body))));
}

async function resolveItems(action, chosen) {
  setBusy(true);
  try {
    const result = await api('/api/resolve', {
      action, items: chosen.map((item) => ({ batch: item.batch, id: item.id })),
    });
    const message = action === 'delete'
      ? t('Удалено {0}, освобождено {1}', objects(result.deleted), size(result.freed))
      : t('Вернул на место {0}', objects(result.restored));
    const failed = result.errors.length ? t(' · не получилось: {0} ({1})', result.errors.length, result.errors[0]) : '';
    toast(message + failed, Boolean(failed));
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
    await loadReview();
    refreshStatus();
  }
}

function bindReview() {
  $('#review-filter').addEventListener('input', (event) => { state.filter = event.target.value; renderReview(); });
  $('#btn-select-all').addEventListener('click', () => {
    visibleItems().forEach((item) => setSelected(item, true));
    renderReview();
  });
  $('#btn-select-none').addEventListener('click', () => {
    visibleItems().forEach((item) => setSelected(item, false));
    renderReview();
  });
  $('#btn-delete').addEventListener('click', async () => {
    const chosen = selectedItems();
    if (!chosen.length) return;
    const ok = await confirmDialog({
      title: t('Удалить {0}?', objects(chosen.length)),
      text: t('Освободится {0}. Файлы удалятся насовсем — это не отменить.', size(total(chosen))),
      ok: t('Удалить'),
    });
    if (ok) resolveItems('delete', chosen);
  });
  $('#btn-restore').addEventListener('click', () => {
    const chosen = selectedItems();
    if (chosen.length) resolveItems('restore', chosen);
  });
}

// ================================================================ история
async function loadHistory() {
  const list = $('#history-list');
  let sessions;
  try {
    sessions = await api('/api/history');
  } catch (error) {
    list.replaceChildren(h('p', { class: 'errors' }, t('Не удалось загрузить: {0}', error.message)));
    return;
  }
  if (!sessions.length) {
    list.replaceChildren(h('div', { class: 'panel empty' }, h('h2', {}, t('Пока пусто')),
      h('p', {}, t('Здесь появится всё, что сделает программа.'))));
    return;
  }
  list.replaceChildren(h('div', { class: 'panel' }, sessions.map(historyRow)));
}

function historyRow(session) {
  let action;
  if (session.undone) action = h('span', { class: 'tag' }, t('Отменено'));
  else if (session.undoable) {
    action = h('button', { type: 'button', class: 'btn small', onclick: () => undo(session) }, t('Отменить'));
  } else action = h('span', { class: 'tag' }, session.deleted ? t('Удалённое не вернуть') : t('Нечего возвращать'));
  return h('div', { class: 'history-row' },
    h('time', { datetime: session.created }, when(session.created)),
    h('div', {}, h('strong', {}, session.title), h('span', { class: 'sub' }, session.summary)),
    action);
}

async function undo(session) {
  const ok = await confirmDialog({
    title: t('Отменить?'),
    text: t('«{0}»: {1}. Перенесённое вернётся на места.', session.title, session.summary),
    ok: t('Отменить'),
    danger: false,
  });
  if (!ok) return;
  try {
    const result = await api('/api/undo', { id: session.id });
    const problems = result.errors.length + result.missing.length;
    toast(t('Вернул на место {0}{1}', objects(result.restored), problems ? t(' · не получилось: {0}', problems) : ''), problems > 0);
  } catch (error) {
    toast(error.message, true);
  }
  loadHistory();
  refreshStatus();
}

// ================================================================ настройки
const splitList = (text) => text.split(',').map((s) => s.trim()).filter(Boolean);
const splitLines = (text) => text.split('\n').map((s) => s.trim()).filter(Boolean);
const settingsDirty = () => Boolean(state.draft && state.settings)
  && JSON.stringify(state.draft) !== JSON.stringify(state.settings);

function markDirty() {
  $('#settings-actions').hidden = !settingsDirty();
}

/** Поле: подпись, элемент ввода и подсказка; подпись связана с полем, как у обычной формы. */
function field(label, control, hint) {
  return h('label', { class: 'field' }, h('span', { class: 'field-label' }, label), control,
    hint ? h('span', { class: 'hint' }, hint) : null);
}

function textInput(value, onChange, attrs = {}) {
  const input = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', ...attrs });
  input.value = value ?? '';
  input.addEventListener('input', () => { onChange(input.value); markDirty(); });
  return input;
}

function textArea(value, onChange, rows = 3) {
  const area = h('textarea', { rows: String(rows), spellcheck: 'false' });
  area.value = value ?? '';
  area.addEventListener('input', () => { onChange(area.value); markDirty(); });
  return area;
}

function timeInput(value, onChange) {
  const input = h('input', { type: 'time', required: true });
  input.value = value || '03:00';
  input.addEventListener('input', () => { if (input.value) { onChange(input.value); markDirty(); } });
  return input;
}

function toggle(checked, label, onChange) {
  const box = h('input', { type: 'checkbox' });
  box.checked = Boolean(checked);
  box.addEventListener('change', () => { onChange(box.checked); markDirty(); });
  return h('label', { class: 'check' }, box, label);
}

function sectorCard(sector, index, types) {
  const typeBoxes = types.map((type) => toggle(sector.types.includes(type.id), type.name, (on) => {
    sector.types = on ? [...sector.types, type.id] : sector.types.filter((id) => id !== type.id);
  }));
  const remove = h('button', {
    type: 'button', class: 'btn small',
    onclick: () => { state.draft.sectors.splice(index, 1); renderSettings(); markDirty(); },
  }, t('Удалить сектор'));
  return h('fieldset', { class: 'sector' },
    h('legend', { class: 'sr-only' }, t('Сектор «{0}»', sector.name || t('без названия'))),
    h('div', { class: 'sector-head' },
      field(t('Название — это имя папки'), textInput(sector.name, (v) => { sector.name = v; }, { maxlength: '60' })),
      remove),
    field(t('Описание для ИИ'), textArea(sector.description, (v) => { sector.description = v; }),
      t('Чем подробнее, тем точнее ИИ раскладывает: чем ты тут занимаешься, какие проекты, какие слова встречаются.')),
    h('div', { class: 'grid-2' },
      field(t('Ключевые слова в имени файла'), textInput(sector.keywords.join(', '), (v) => { sector.keywords = splitList(v); }),
        t('Через запятую: lab, лекция, домашка')),
      field(t('Сайты, откуда скачано'), textInput(sector.sources.join(', '), (v) => { sector.sources = splitList(v); }),
        t('Через запятую: coursera.org, github.com'))),
    h('div', { class: 'field' }, h('span', { class: 'field-label' }, t('Все файлы этих типов — сюда')),
      h('div', { class: 'types' }, typeBoxes)),
    field(t('Где хранить (необязательно)'), textInput(sector.target, (v) => { sector.target = v; },
      { placeholder: t('например B:/{0}', sector.name || t('Сектор')) }),
      t('Пусто — папка сектора рядом с разбираемой папкой.')));
}

function renderSettings() {
  const draft = state.draft;
  const body = $('#settings-body');
  if (!draft) return;
  const addSector = h('button', {
    type: 'button', class: 'btn',
    onclick: () => {
      draft.sectors.push({ name: '', description: '', keywords: [], sources: [], types: [], target: '' });
      renderSettings();
      markDirty();
      body.querySelector('.sector:last-of-type input')?.focus();
    },
  }, t('Добавить сектор'));

  const confidence = h('input', { type: 'range', min: '50', max: '100', step: '5', 'aria-describedby': 'confidence-hint' });
  confidence.value = String(draft.ai.min_confidence);
  const confidenceValue = h('output', {}, `${draft.ai.min_confidence}`);
  confidence.addEventListener('input', () => {
    draft.ai.min_confidence = Number(confidence.value);
    confidenceValue.textContent = confidence.value;
    markDirty();
  });

  const afterChoices = [['nothing', t('ничего')], ['sleep', t('усыпить компьютер')], ['shutdown', t('выключить компьютер')]];
  const after = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': t('Когда «Приступай» закончит') },
    afterChoices.map(([value, label]) => {
      const radio = h('input', { type: 'radio', name: 'night-after', value });
      radio.checked = draft.night.after === value;
      radio.addEventListener('change', () => { draft.night.after = value; markDirty(); });
      return h('label', { class: 'check' }, radio, label);
    }));

  const languages = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': t('Язык программы') },
    Object.entries(state.settings.languages).map(([value, label]) => {
      const radio = h('input', { type: 'radio', name: 'ui-language', value });
      radio.checked = draft.ui.language === value;
      radio.addEventListener('change', () => { draft.ui.language = value; markDirty(); });
      return h('label', { class: 'check' }, radio, label);
    }));

  const updateNote = h('span', { class: 'hint', 'aria-live': 'polite' });
  const checkNow = h('button', {
    type: 'button', class: 'btn small',
    onclick: async () => {
      checkNow.disabled = true;
      updateNote.replaceChildren(t('Проверяю…'));
      try {
        const result = await api('/api/update', {});
        state.status.update = result.update;
        renderStatus();
        updateNote.replaceChildren(...(result.update
          ? [t('Вышла версия {0}.', result.update.version), ' ',
            h('button', { type: 'button', class: 'link', onclick: openUpdate }, t('Открыть страницу загрузки'))]
          : [t('У тебя последняя версия.')]));
      } catch (error) {
        updateNote.replaceChildren(error.message);
      } finally {
        checkNow.disabled = false;
      }
    },
  }, t('Проверить сейчас'));

  body.replaceChildren(
    h('div', { class: 'panel' }, h('h2', {}, t('Язык программы')), languages),
    h('div', { class: 'panel' },
      h('h2', {}, t('Секторы — куда раскладывать')),
      h('p', { class: 'hint' }, t('Файл попадает в сектор, если скачан с сайта из списка, в имени есть ключевое слово, '
        + 'подходит тип или его узнал ИИ по описанию. Внутри сектора файлы раскладываются по типам.')),
      draft.sectors.map((sector, i) => sectorCard(sector, i, state.settings.types)),
      h('div', { class: 'actions' }, addSector)),
    h('div', { class: 'panel' },
      h('h2', {}, t('ИИ')),
      toggle(draft.ai.enabled, t('Раскладывать с помощью локальной модели (Ollama) — файлы не уходят в интернет'),
        (on) => { draft.ai.enabled = on; }),
      h('div', { class: 'grid-2' },
        field(t('Модель'), textInput(draft.ai.model, (v) => { draft.ai.model = v.trim(); }), t('Например qwen2.5:7b')),
        h('label', { class: 'field' },
          h('span', { class: 'field-label' }, t('Уверенность, с которой файл уходит в сектор: '), confidenceValue),
          confidence,
          h('span', { class: 'hint', id: 'confidence-hint' }, t('При 70 модель часто угадывает, 80 — проверенный порог.')))),
      field(t('О тебе — для ИИ'), textArea(draft.ai.about, (v) => { draft.ai.about = v; }),
        t('Пара фраз: где учишься, кем работаешь, над какими проектами. После правки описаний ИИ заново '
        + 'посмотрит файлы при следующем запуске.'))),
    h('div', { class: 'panel' },
      h('h2', {}, t('Что не трогать')),
      field(t('Не трогать совсем — слова в имени'), textInput(draft.protect.keep_keywords.join(', '),
        (v) => { draft.protect.keep_keywords = splitList(v); }), t('Такие файлы не удаляются и не раскладываются. Например: thesis_final')),
      field(t('Не трогать совсем — папки и файлы'), textArea(draft.protect.paths.join('\n'),
        (v) => { draft.protect.paths = splitLines(v); }, 3), t('Каждый путь с новой строки, например D:/Проекты/База')),
      field(t('Не удалять, но раскладывать можно — слова в имени'), textInput(draft.protect.name_keywords.join(', '),
        (v) => { draft.protect.name_keywords = splitList(v); }), t('Паспорта, договоры, сертификаты — только в отчёт, не на удаление.'))),
    h('div', { class: 'panel' },
      h('h2', {}, t('«Приступай»')),
      toggle(draft.schedule.enabled, t('Запускать сам каждую ночь'), (on) => { draft.schedule.enabled = on; }),
      h('div', { class: 'grid-2' },
        field(t('Во сколько'), timeInput(draft.schedule.time, (v) => { draft.schedule.time = v; })),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, ' '),
          toggle(draft.schedule.wake, t('Будить компьютер, если он спит'), (on) => { draft.schedule.wake = on; }))),
      h('p', { class: 'hint' }, t('Задача в Планировщике Windows: запускается, только когда ноутбук на зарядке, '
        + 'и не догоняет пропущенный запуск днём. Чтобы компьютер просыпался, в электропитании должны быть '
        + 'разрешены таймеры пробуждения.')),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, t('Когда закончит')), after),
      toggle(draft.night.auto_delete, t('Проверенные копии в Загрузках и на Рабочем столе удалять сразу (сверив с оригиналом)'),
        (on) => { draft.night.auto_delete = on; }),
      toggle(draft.night.drives, t('Искать мусор и копии на всех дисках, а не только в личных папках'),
        (on) => { draft.night.drives = on; }),
      field(t('Какие папки раскладывать'), textArea(draft.night.sort_folders.join('\n'),
        (v) => { draft.night.sort_folders = splitLines(v); }, 3),
        t('downloads, desktop, documents или путь — каждая с новой строки. Подпапки переносятся только в Загрузках и на Рабочем столе.'))),
    h('div', { class: 'panel' },
      h('h2', {}, t('Обновления')),
      toggle(draft.update.check, t('Раз в день проверять, вышла ли новая версия'), (on) => { draft.update.check = on; }),
      h('p', { class: 'hint' }, t('Программа спрашивает у GitHub только номер последней версии — о тебе и твоих файлах '
        + 'ничего не отправляется. Сама ничего не скачивает и не ставит: покажет ссылку на страницу загрузки.')),
      h('div', { class: 'actions' },
        h('span', {}, t('Установлена версия {0}.', state.status ? state.status.version : '')), checkNow, updateNote)),
  );
  markDirty();
}

async function loadSettings() {
  try {
    state.settings = await api('/api/settings');
  } catch (error) {
    $('#settings-body').replaceChildren(h('p', { class: 'errors' }, t('Не удалось загрузить: {0}', error.message)));
    return;
  }
  state.draft = structuredClone(state.settings);
  renderSettings();
}

function bindSettings() {
  $('#settings-form').addEventListener('submit', (event) => event.preventDefault());
  $('#btn-settings-save').addEventListener('click', async () => {
    const button = $('#btn-settings-save');
    button.disabled = true;
    const languageChanged = state.draft.ui.language !== state.settings.ui.language;
    try {
      state.settings = await api('/api/settings', state.draft);
      if (languageChanged) { location.reload(); return; }  // окно — сразу на новом языке
      state.draft = structuredClone(state.settings);
      renderSettings();
      toast(t('Сохранено.'));
      refreshStatus();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $('#btn-settings-reset').addEventListener('click', () => {
    state.draft = structuredClone(state.settings);
    renderSettings();
  });
  $('#btn-open-rules').addEventListener('click', () => {
    api('/api/open', { what: 'rules' }).catch((error) => toast(error.message, true));
  });
}

// ================================================================ старт
translateStatic();
bindTabs();
bindHome();
bindReview();
bindSettings();
bindAiSetup();
if (VIEWS.includes(HASH.get('view'))) showView(HASH.get('view'));
refreshStatus(true);
setInterval(refreshStatus, 5000);  // заодно сигнал серверу, что окно открыто
