'use strict';
/* File Cleaner — окно программы. Данные — с локального сервера (filecleaner/gui/app.py). */

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
  if (!response.ok) throw new Error((data && data.error) || `Ошибка ${response.status}`);
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

const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });

function size(bytes) {
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let value = bytes || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit === 0 ? value : number.format(value)} ${units[unit]}`;
}

function plural(n, one, few, many) {
  const n10 = n % 10;
  const n100 = n % 100;
  const word = n10 === 1 && n100 !== 11 ? one
    : n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14) ? few : many;
  return `${n.toLocaleString('ru-RU')} ${word}`;
}
const objects = (n) => plural(n, 'объект', 'объекта', 'объектов');
const total = (items) => items.reduce((sum, item) => sum + (item.size || 0), 0);

function when(value) {
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return date.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
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
    ? h('p', { class: 'more' }, `…и ещё ${objects(group.count - group.items.length)} — все есть в отчёте.`) : null;
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
};

// ================================================================ вкладки
const VIEWS = ['home', 'review', 'history'];

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
    $('#ai-text').textContent = TOKEN ? 'Программа закрыта — запусти её снова' : 'Открой окно через File Cleaner';
    setBusy(true);
    return;
  }
  renderStatus();
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

function renderStatus() {
  const { ai, review, last } = state.status;
  const chip = $('#ai-status');
  chip.classList.toggle('ok', ai.enabled && ai.local && ai.available);
  chip.classList.toggle('warn', ai.enabled && !(ai.local && ai.available));
  $('#ai-text').textContent = !ai.enabled ? 'ИИ выключен в правилах'
    : !ai.local ? 'ИИ выключен: модель не на этом компьютере'
      : ai.available ? `ИИ: ${ai.model} готов` : 'ИИ: запусти Ollama';

  const count = $('#review-count');
  count.hidden = !review.count;
  count.textContent = review.count ? review.count.toLocaleString('ru-RU') : '';

  const line = $('#last-run');
  line.replaceChildren();
  if (last) {
    line.append(`Прошлый запуск: ${when(last.finished)} — освобождено ${size(last.freed)}, разложено ${objects(last.sorted)}.`);
    if (last.report) {
      line.append(' ', h('button', { type: 'button', class: 'link', onclick: () => openReport(last.report) }, 'Отчёт'));
    }
  }
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
  $('#task-progress').textContent = task.progress || 'Работаю…';
  const log = $('#task-log');
  log.replaceChildren(...task.log.slice(-14).map((line) => h('li', {}, line)));
  log.scrollTop = log.scrollHeight;
  $('#btn-stay').hidden = !/^Через \d+ с/.test(task.progress || '');
}

function onTaskFinished(task, quiet = false) {
  if (!task) return;
  if (task.error && !quiet) toast(`Не получилось: ${task.error}`, true);
  if (task.kind === 'preview' && task.result) {
    state.plan = task.result;
    state.result = null;
    renderPlan();
  }
  if (task.kind === 'night' && task.result) {
    state.result = task.result;
    state.plan = null;
    renderResult();
    if (!quiet) toast(`Готово: освобождено ${size(task.result.freed)}, разложено ${objects(task.result.sorted)}.`);
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
    stat(size(plan.junk.bytes), `кэши и временное, ${objects(plan.junk.count)} — удалю сразу`),
    stat(size(plan.auto.bytes), `лишние копии, ${objects(plan.auto.count)} — удалю, сверив с оригиналом`),
    stat(size(plan.waiting.bytes), `спорное, ${objects(plan.waiting.count)} — отложу на решение`),
    stat(size(plan.report.bytes), 'только покажу в отчёте — не трону'),
  );
  const sortLine = plan.sort.length ? h('p', {}, 'Разложу: ',
    plan.sort.map((f) => `${f.path}${f.subfolders ? '' : ' (только файлы)'}`).join(', '), '.') : null;
  $('#plan-details').replaceChildren(...[
    listBlock('Удалю сразу — копии', plan.auto,
      [{ title: 'Файл' }, { title: 'Размер', num: true }, { title: 'Что останется' }],
      (item) => [item.path, size(item.size), item.keep || '—']),
    listBlock('Отложу на решение', plan.waiting,
      [{ title: 'Файл' }, { title: 'Размер', num: true }, { title: 'Почему' }],
      (item) => [item.path, size(item.size), item.reason]),
    sortLine,
    plan.notes.length ? h('ul', { class: 'notes' }, plan.notes.map((note) => h('li', {}, note))) : null,
  ].filter(Boolean));
  $('#btn-run').disabled = state.busy;
  $('#btn-run').classList.add('primary');
  $('#btn-preview').classList.remove('primary');
  $('#run-hint').textContent = 'Можно приступать. Разложилось не так — «История» → «Отменить».';
}

function renderResult() {
  const result = state.result;
  $('#plan-panel').hidden = true;
  $('#result-panel').hidden = !result;
  if (!result) return;
  $('#result-stats').replaceChildren(
    stat(size(result.freed), `освобождено: кэши ${size(result.junk_freed)}, копии ${size(result.auto_freed)}`),
    stat(result.sorted.toLocaleString('ru-RU'), 'разложено по папкам'),
    stat(size(result.waiting_bytes), `ждёт решения, ${objects(result.waiting)}`),
  );
  const details = [];
  if (result.notes.length) details.push(h('ul', { class: 'notes' }, result.notes.map((n) => h('li', {}, n))));
  if (result.errors_count) {
    details.push(h('details', {},
      h('summary', { class: 'errors' }, `Не получилось: ${objects(result.errors_count)}`),
      h('ul', { class: 'notes' }, result.errors.map((e) => h('li', {}, e)))));
  }
  $('#result-details').replaceChildren(...details);
  $('#btn-result-report').hidden = !result.report;
  $('#btn-result-review').hidden = !result.waiting;
  $('#btn-run').disabled = true;
  $('#btn-run').classList.remove('primary');
  $('#btn-preview').classList.add('primary');
  $('#run-hint').textContent = 'Чтобы запустить ещё раз, снова посмотри, что будет.';
}

function bindHome() {
  $('#btn-preview').addEventListener('click', () => startTask('/api/preview', {}));
  $('#btn-run').addEventListener('click', async () => {
    const sleep = $('#opt-sleep').checked;
    const ok = await confirmDialog({
      title: 'Приступить?',
      text: `Удалю кэши и проверенные копии, спорное отложу на решение, разложу папки.${sleep ? ' В конце усыплю компьютер.' : ''} Можно уйти — всё сделается само.`,
      ok: 'Приступить',
      danger: false,
    });
    if (ok) startTask('/api/night', { after: sleep ? 'sleep' : 'nothing' });
  });
  $('#btn-stay').addEventListener('click', async () => {
    try { await api('/api/stay-awake', {}); toast('Хорошо, компьютер не усыплю.'); } catch (e) { toast(e.message, true); }
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
  label.textContent = chosen.length ? `Выбрано ${objects(chosen.length)} · ${size(total(chosen))}` : 'Ничего не выбрано';
  $('#btn-delete').disabled = state.busy || !chosen.length;
  $('#btn-restore').disabled = state.busy || !chosen.length;
}

async function loadReview() {
  try {
    state.review = await api('/api/review');
  } catch (error) {
    $('#review-list').replaceChildren(h('p', { class: 'errors' }, `Не удалось загрузить: ${error.message}`));
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
      h('h2', {}, 'Ничего не ждёт решения'),
      h('p', {}, 'Когда программа отложит спорные файлы, они появятся здесь.')));
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
  list.replaceChildren(...(sections.length ? sections : [h('p', { class: 'empty' }, 'По такому запросу ничего нет.')]));
  updateSelection();
}

function groupSection(name, rows) {
  rows.sort((a, b) => b.size - a.size);
  const groupBox = h('input', { type: 'checkbox', 'aria-label': `Выбрать всю группу «${name}»` });
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
    const box = h('input', { type: 'checkbox', 'aria-label': `Выбрать «${item.name}»` });
    box.checked = isSelected(item);
    box.addEventListener('change', () => { setSelected(item, box.checked); syncGroup(); updateSelection(); });
    boxes.push(box);
    const reveal = h('button', {
      type: 'button', class: 'link',
      onclick: () => api('/api/reveal', { batch: item.batch, id: item.id }).catch((e) => toast(e.message, true)),
    }, 'Показать');
    return h('tr', {},
      h('td', { class: 'pick' }, box),
      h('td', { class: 'name' }, h('strong', {}, item.name), h('span', { class: 'sub' }, `из ${item.from}`)),
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
        h('th', { class: 'pick' }, h('span', { class: 'sr-only' }, 'Выбор')),
        h('th', {}, 'Файл'), h('th', { class: 'num' }, 'Размер'), h('th', {}, 'Почему здесь'),
        h('th', {}, 'Что останется'), h('th', {}, h('span', { class: 'sr-only' }, 'Показать в Проводнике')))),
      h('tbody', {}, body))));
}

async function resolveItems(action, chosen) {
  setBusy(true);
  try {
    const result = await api('/api/resolve', {
      action, items: chosen.map((item) => ({ batch: item.batch, id: item.id })),
    });
    const message = action === 'delete'
      ? `Удалено ${objects(result.deleted)}, освобождено ${size(result.freed)}`
      : `Вернул на место ${objects(result.restored)}`;
    const failed = result.errors.length ? ` · не получилось: ${result.errors.length} (${result.errors[0]})` : '';
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
      title: `Удалить ${objects(chosen.length)}?`,
      text: `Освободится ${size(total(chosen))}. Файлы удалятся насовсем — это не отменить.`,
      ok: 'Удалить',
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
    list.replaceChildren(h('p', { class: 'errors' }, `Не удалось загрузить: ${error.message}`));
    return;
  }
  if (!sessions.length) {
    list.replaceChildren(h('div', { class: 'panel empty' }, h('h2', {}, 'Пока пусто'),
      h('p', {}, 'Здесь появится всё, что сделает программа.')));
    return;
  }
  list.replaceChildren(h('div', { class: 'panel' }, sessions.map(historyRow)));
}

function historyRow(session) {
  let action;
  if (session.undone) action = h('span', { class: 'tag' }, 'Отменено');
  else if (session.undoable) {
    action = h('button', { type: 'button', class: 'btn small', onclick: () => undo(session) }, 'Отменить');
  } else action = h('span', { class: 'tag' }, session.deleted ? 'Удалённое не вернуть' : 'Нечего возвращать');
  return h('div', { class: 'history-row' },
    h('time', { datetime: session.created }, when(session.created)),
    h('div', {}, h('strong', {}, session.title), h('span', { class: 'sub' }, session.summary)),
    action);
}

async function undo(session) {
  const ok = await confirmDialog({
    title: 'Отменить?',
    text: `«${session.title}»: ${session.summary}. Перенесённое вернётся на места.`,
    ok: 'Отменить',
    danger: false,
  });
  if (!ok) return;
  try {
    const result = await api('/api/undo', { id: session.id });
    const problems = result.errors.length + result.missing.length;
    toast(`Вернул на место ${objects(result.restored)}${problems ? ` · не получилось: ${problems}` : ''}`, problems > 0);
  } catch (error) {
    toast(error.message, true);
  }
  loadHistory();
  refreshStatus();
}

// ================================================================ старт
bindTabs();
bindHome();
bindReview();
if (VIEWS.includes(HASH.get('view'))) showView(HASH.get('view'));
refreshStatus(true);
setInterval(refreshStatus, 5000);  // заодно сигнал серверу, что окно открыто
