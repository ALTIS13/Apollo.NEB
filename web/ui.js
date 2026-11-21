"use strict";

const $ = id => document.getElementById(id);
const all = selector => [...document.querySelectorAll(selector)];
const n = (value, digits = 1) => Number.isFinite(value)
  ? value.toLocaleString("ru-RU", {maximumFractionDigits: digits}) : "—";
const fmt = (value, unit, digits) => Number.isFinite(value) ? `${n(value, digits)} ${unit}` : "—";
const inList = value => Array.isArray(value) ? value : [];
const localTime = (stamp, options) => Number.isFinite(stamp)
  ? new Date(stamp * 1000).toLocaleString("ru-RU", options) : "—";
const clock = stamp => localTime(stamp, {hour: "2-digit", minute: "2-digit"});
const eventTime = stamp => localTime(stamp, {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"});
const text = (el, value) => { if (el.textContent !== String(value)) el.textContent = value; };
const create = (tag, value, cls) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (value !== undefined) el.textContent = value;
  return el;
};

const METRICS = {
  rate: ["Хешрейт", "МХ/с"], watts: ["Потребление GPU", "Вт"],
  edge: ["Ядро", "°C"], hot: ["Hotspot", "°C"], hbm: ["Память", "°C"],
  active: ["Активные воркеры", ""], efficiency: ["Эффективность GPU", "МХ/(с·Вт)"],
};
const TEMPERATURES = ["edge", "hot", "hbm"];
const GROUP_LIMIT = 24;
const NODE_GROUP_LIMIT = 16;
const STATES = new Map([
  ["running", ["Работает", "good"]], ["mining", ["Работает", "good"]],
  ["starting", ["Запускается", "warn"]], ["stopping", ["Останавливается", "warn"]],
  ["stopped", ["Остановлен", "neutral"]], ["idle", ["Остановлен", "neutral"]],
  ["blocked", ["Запуск заблокирован", "warn"]], ["protection", ["Термозащита", "bad"]],
  ["failed", ["Ошибка", "bad"]], ["unavailable", ["Недоступен", "warn"]],
]);
const EVENTS = new Map([
  ["start", "Запуск"], ["stop", "Остановка"], ["thermal", "Термозащита"],
  ["exit", "Завершение"], ["support", "Поддержка автора"],
  ["profile", "Профиль"], ["limits", "Температурные пределы"],
]);
const PAGES = {overview: "Обзор фермы", workers: "Воркеры", nodes: "Ноды", events: "События"};
const isRunning = worker => worker.state === "running" || worker.state === "mining";
const rate = value => !Number.isFinite(value) ? "—" : value >= 1e6
  ? fmt(value / 1e6, "МХ/с", 3) : value >= 1e3 ? fmt(value / 1e3, "кХ/с", 2) : fmt(value, "Х/с", 1);
const sum = (rows, field) => rows.length && rows.every(row => Number.isFinite(row[field]))
  ? rows.reduce((total, row) => total + row[field], 0) : null;
const safeLine = value => typeof value === "string" ? value.slice(0, 350) : "";

const state = {
  mode: "boot", view: "overview", status: null, generation: 0,
  history: null, historyError: "", events: null, eventsError: "",
  eventsKey: "", chartKey: "", incidentsKey: "", poll: null, pollAgain: false,
  timelineSeq: 0, online: false, actions: new Set(),
  authTotp: null, authInfoSeq: 0, authMutation: false, authEpoch: 0,
  selected: new Set(), groupBusy: false,
};
const cards = new Map();
const vault = {seq: 0, busy: false, data: null, exportId: null};
const setup = {seq: 0, busy: false, data: null, selected: new Set()};
const totp = {seq: 0, busy: false, timer: null, expiresAt: 0};
const fleet = {generation: 0, loading: false, busy: false, fresh: false, nodes: null, key: "", selected: new Map(), revokeId: null, inviteTimer: null};
let releaseSeq = 0;

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

async function api(path, data, timeoutMs = 12000) {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), timeoutMs);
  try {
    const options = {credentials: "same-origin", cache: "no-store", signal: timeout.signal};
    if (data !== undefined) {
      options.method = "POST";
      options.headers = {"Content-Type": "application/json"};
      options.body = JSON.stringify(data);
    }
    const response = await fetch(`/api/${path}`, options);
    const json = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(response.status, safeLine(json?.error || json?.message || json?.detail));
    if (!json || typeof json !== "object") throw new ApiError(502, "");
    return json;
  } finally {
    clearTimeout(timer);
  }
}

function errorText(error, fallback) {
  if (error.status === 429) return "Слишком много попыток. Подождите и попробуйте снова.";
  if (error.status >= 500 || !error.status) return "Служба не ответила. Проверьте подключение и текущее состояние.";
  return error.message || fallback;
}

function connection(label, tone) {
  $("connection").dataset.tone = tone;
  text($("connection-label"), label);
}

function notice(message, tone = "good") {
  $("notice").dataset.tone = tone;
  text($("notice"), message);
  $("notice").hidden = !message;
}

function showLogin(expired = false) {
  state.generation++;
  state.mode = "login";
  state.view = "overview";
  state.status = state.history = state.events = null;
  state.historyError = state.eventsError = state.chartKey = state.eventsKey = state.incidentsKey = "";
  state.online = state.pollAgain = false;
  state.actions.clear();
  state.selected.clear();
  state.groupBusy = state.authMutation = false;
  resetAuxiliary();
  cards.clear();
  for (const id of ["worker-grid", "event-list", "chart-svg", "incident-list", "live-thermal"]) $(id).replaceChildren();
  for (const id of ["profile-form", "limits-form", "login-form"]) $(id).reset();
  for (const dialog of all("dialog[open]")) dialog.close();
  $("boot").hidden = $("app").hidden = $("logout").hidden = true;
  $("login").hidden = false;
  text($("login-message"), expired ? "Сеанс завершён. Войдите снова." : "");
  connection("Вход в панель", "neutral");
  notice("");
  refreshAuthInfo();
  $("password").focus();
}

function canStart(worker) {
  return state.online && !state.actions.has(worker.id)
    && !["running", "mining", "starting", "stopping", "unavailable"].includes(worker.state)
    && Boolean(state.status?.profile?.wallet);
}

function workerCards(workers) {
  const ids = new Set(workers.map(worker => worker.id));
  for (const [id, entry] of cards) {
    if (!ids.has(id)) {
      entry.el.remove();
      cards.delete(id);
    }
  }
  workers.forEach(worker => {
    let entry = cards.get(worker.id);
    if (!entry) {
      const el = $("worker-template").content.firstElementChild.cloneNode(true);
      const fields = Object.fromEntries([...el.querySelectorAll("[data-field]")].map(field => [field.dataset.field, field]));
      const start = el.querySelector('[data-worker-action="start"]');
      const stop = el.querySelector('[data-worker-action="stop"]');
      const select = el.querySelector("[data-worker-select]");
      select.addEventListener("change", () => {
        if (select.checked) state.selected.add(worker.id);
        else state.selected.delete(worker.id);
        renderGroup(inList(state.status?.workers));
      });
      start.addEventListener("click", () => workerAction(worker.id, "start"));
      stop.addEventListener("click", () => workerAction(worker.id, "stop"));
      entry = {el, fields, start, stop, select};
      cards.set(worker.id, entry);
      $("worker-grid").append(el);
    }
    const {el, fields, start, stop, select} = entry;
    const [label, tone] = STATES.get(worker.state) || ["Неизвестно", "neutral"];
    el.dataset.tone = tone;
    text(fields.label, worker.label || worker.id);
    select.setAttribute("aria-label", "Выбрать " + (worker.label || worker.id));
    select.checked = state.selected.has(worker.id);
    select.disabled = state.groupBusy || state.actions.has(worker.id);
    text(fields.device, [worker.device, worker.adapter].filter(Boolean).join(" · ") || "Устройство не указано");
    text(fields.state, state.actions.has(worker.id) ? "Выполняется…" : label);
    text(fields.rate, rate(worker.rate_hs));
    text(fields.watts, fmt(worker.watts, "Вт"));
    for (const name of TEMPERATURES) {
      text(fields[name], fmt(worker[name], "°C"));
      fields[name].dataset.tone = thermalTone(worker[name], state.status?.limits?.[`${name}_limit`]);
    }
    text(fields.shares, `Шары: ${n(worker.accepted, 0)} принято · ${n(worker.rejected, 0)} отклонено`);
    const details = safeLine(worker.message);
    fields.message.hidden = !details || !["protection", "failed", "blocked", "unavailable"].includes(worker.state);
    text(fields.message, details);
    start.disabled = !canStart(worker);
    start.title = state.status?.profile?.wallet ? "" : "Сначала задайте профиль пула";
    stop.disabled = !state.online || state.actions.has(worker.id) || !isRunning(worker);
  });
}

function thermalTone(value, limit) {
  if (!Number.isFinite(value) || !Number.isFinite(limit)) return "neutral";
  return value >= limit ? "bad" : value >= limit - 5 ? "warn" : "neutral";
}

function renderStatus() {
  const {workers: rawWorkers, profile, limits, support} = state.status || {};
  const workers = inList(rawWorkers);
  const active = workers.filter(isRunning);
  const devices = workers.filter(worker => worker.device);
  text($("live-workers"), `${active.length} / ${workers.length}`);
  text($("live-rate"), rate(!active.length && workers.length
    && workers.every(worker => !["starting", "stopping", "unavailable"].includes(worker.state)) ? 0 : sum(active, "rate_hs")));
  text($("live-watts"), fmt(sum(devices, "watts"), "Вт"));
  text($("live-shares"), `${n(sum(active, "accepted"), 0)} / ${n(sum(active, "rejected"), 0)}`);
  $("first-run").hidden = Boolean(workers.length && profile?.wallet);
  text($("step-profile-title"), profile?.wallet ? "Профиль пула сохранён" : "Задайте профиль пула");
  text($("step-profile-action"), profile?.wallet ? "Изменить профиль" : "Настроить профиль");
  text($("step-workers-title"), workers.length ? "Воркеры добавлены" : "Добавьте воркеры");
  all("[data-profile]").forEach(button => button.classList.toggle("primary", !profile?.wallet));
  $("first-run").querySelector("[data-discovery]").classList.toggle("primary", Boolean(profile?.wallet && !workers.length));
  const supportPercent = fmt(support?.basis_points / 100, "%", 2);
  const runtimeShare = `Включена оператором: до ${supportPercent} рабочего времени воркера выделяется автору. Это не удержание из выплат.`;
  text($("setup-support"), support?.enabled ? `Поддержка автора. ${runtimeShare}`
    : "Поддержка автора: выключена, 0 % рабочего времени. Включается только явно в конфигурации Linux.");
  $("support").hidden = !support?.enabled || !$("first-run").hidden;
  text($("support-summary"), runtimeShare);
  text($("support-recipient"), support?.enabled && support.wallet ? `Получатель: ${support.wallet}` : "");
  const onSupport = inList(support?.active_workers);
  text($("support-active"), onSupport.length
    ? `Сейчас: ${onSupport.map(id => workers.find(worker => worker.id === id)?.label || id).join(", ")}. Сводка учитывает их работу.`
    : "Сейчас ни один воркер не участвует.");
  const alerts = workers.filter(worker => ["protection", "failed", "blocked", "unavailable"].includes(worker.state));
  const nearby = devices
    .flatMap(worker => TEMPERATURES.map(key => ({worker, key, tone: thermalTone(worker[key], limits?.[`${key}_limit`])})))
    .filter(({tone}) => tone !== "neutral");
  $("attention").hidden = !alerts.length && !nearby.length;
  if (alerts.length) {
    const first = alerts[0];
    text($("attention-detail"), `${first.label || first.id}: ${safeLine(first.message) || STATES.get(first.state)[0]}${alerts.length > 1 ? ` · ещё ${alerts.length - 1}` : ""}`);
  } else if (nearby.length) {
    const {worker, key, tone} = nearby.find(entry => entry.tone === "bad") || nearby[0];
    text($("attention-detail"), `${worker.label || worker.id}: ${METRICS[key][0]} ${fmt(worker[key], "°C")} · ${tone === "bad" ? "достигнут предел" : "приближение к пределу"}`);
  }
  const sensors = TEMPERATURES.map(key => {
    const values = workers.map(row => row[key]).filter(Number.isFinite);
    const value = values.length ? Math.max(...values) : null;
    const limit = limits?.[`${key}_limit`];
    const div = create("div");
    const dd = create("dd", fmt(value, "°C"));
    dd.dataset.tone = thermalTone(value, limit);
    div.append(create("dt", METRICS[key][0]), dd, create("small",
      `Датчики: ${values.length} / ${workers.length} · предел: ${Number.isFinite(limit) ? fmt(limit, "°C") : "не задан"}`));
    return div;
  });
  $("live-thermal").replaceChildren(...sensors);
  text($("worker-summary"), workers.length ? `В работе: ${active.length} из ${workers.length}` : "Нет настроенных воркеров.");
  $("workers-empty").hidden = Boolean(workers.length);
  workerCards(workers);
  renderGroup(workers);
  $("profile-open").disabled = !state.online;
  for (const button of all("[data-limits], [data-profile], [data-wallets], [data-discovery], [data-totp]")) button.disabled = !state.online;
}

function renderEvents() {
  const raw = inList(state.events);
  const filter = $("event-filter").value;
  const entries = raw.filter(event => !filter || event.type === filter)
    .sort((a, b) => (b.time || 0) - (a.time || 0)).slice(0, 300);
  const key = JSON.stringify([filter, entries]);
  if (state.eventsKey !== key) {
    $("event-list").replaceChildren(...entries.map(event => {
      const row = create("li");
      const time = create("time", eventTime(event.time), "event-time");
      if (Number.isFinite(event.time)) time.dateTime = new Date(event.time * 1000).toISOString();
      const type = create("span", EVENTS.get(event.type) || "Событие", "event-kind");
      type.dataset.type = event.type;
      const body = create("div");
      const metadata = [event.worker, STATES.get(event.status)?.[0] || event.status].filter(Boolean).join(" · ");
      body.append(create("p", safeLine(event.detail) || "—", "event-detail"));
      if (metadata) body.append(create("p", safeLine(metadata), "event-device"));
      row.append(time, type, body);
      return row;
    }));
    state.eventsKey = key;
  }
  $("events-empty").hidden = entries.length > 0 && !state.eventsError;
  text($("events-empty"), state.eventsError || (!state.events ? "Загружаем события…" :
    filter ? "Нет событий этого типа." : "Событий пока нет."));
}

function svg(tag, attrs, content) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
  if (content !== undefined) el.textContent = content;
  return el;
}

function drawHistory() {
  if (state.view !== "overview") return;
  const width = $("chart-wrap").clientWidth;
  if (!width) return;
  const h = state.history;
  const current = $("period").value;
  const data = h?.range === current ? h : null;
  const metric = $("metric").value;
  const [label, unit] = METRICS[metric];
  const limit = TEMPERATURES.includes(metric) ? state.status?.limits?.[`${metric}_limit`] : null;
  const status = state.historyError || (data ? "" : "Загружаем историю…");
  const key = JSON.stringify([width, metric, data, status, limit]);
  if (state.chartKey === key) return;
  state.chartKey = key;
  const chart = $("chart-svg");
  chart.setAttribute("viewBox", `0 0 ${width} 230`);
  chart.setAttribute("aria-label", `${label}: ${$("period").selectedOptions[0].textContent}`);
  chart.replaceChildren();
  text($("history-count"), data && Number.isFinite(data.count) ? `Замеров: ${n(data.count, 0)}` : "");
  const help = status && data ? `${status} Показаны последние полученные данные.`
    : "Пропуск означает, что данных не было. Время показано по часам браузера.";
  text($("chart-caption"), `${help}${Number.isFinite(limit) ? ` Пунктир: текущий предел ${fmt(limit, "°C")}.` : ""}`);
  $("chart-empty").hidden = false;
  text($("chart-empty"), status || "Нет значений за этот период.");
  if (!data || !Number.isFinite(data.from) || !Number.isFinite(data.to) || data.to <= data.from) {
    renderIncidentButtons([]);
    return;
  }
  const left = width < 340 ? 46 : 52;
  const right = width - 15, top = 25, bottom = 195;
  const x = stamp => left + (stamp - data.from) / (data.to - data.from) * (right - left);
  const rows = inList(data.series).filter(row => Number.isFinite(row.t) && row.t >= data.from && row.t <= data.to)
    .slice(0, 1500).sort((a, b) => a.t - b.t);
  const values = rows.map(row => row[metric]).filter(Number.isFinite);
  const thermal = TEMPERATURES.includes(metric);
  const scale = Number.isFinite(limit) ? [...values, limit] : values;
  const maximum = scale.length ? Math.max(...scale) : 0;
  const minimum = scale.length ? Math.min(...scale) : 0;
  const lo = thermal ? Math.floor((minimum - 5) / 5) * 5 : 0;
  const hi = thermal ? Math.max(lo + 10, Math.ceil((maximum + 5) / 5) * 5) : maximum > 0 ? maximum * 1.08 : 1;
  const y = value => bottom - (value - lo) / (hi - lo) * (bottom - top);
  for (let i = 0; i <= 4; i++) {
    const value = lo + (hi - lo) * i / 4;
    chart.append(svg("line", {x1: left, x2: right, y1: y(value), y2: y(value), class: "chart-grid"}));
    if (scale.length) chart.append(svg("text", {x: left - 7, y: y(value) + 3, "text-anchor": "end", class: "chart-text"},
      n(value, metric === "efficiency" ? 4 : metric === "rate" ? 3 : 1)));
  }
  if (unit && scale.length) chart.append(svg("text", {x: left, y: 15, class: "chart-text"}, unit));
  const interval = right - left;
  const ticks = width < 510 ? 2 : 4;
  for (let i = 0; i <= ticks; i++) {
    const stamp = data.from + (data.to - data.from) * i / ticks;
    const at = left + interval * i / ticks;
    const tick = current === "7d" || current === "30d"
      ? localTime(stamp, {day: "2-digit", month: "2-digit"}) : clock(stamp);
    chart.append(svg("line", {x1: at, x2: at, y1: top, y2: bottom, class: "chart-grid"}),
      svg("text", {x: at, y: 216, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle", class: "chart-text"}, tick));
  }
  if (Number.isFinite(limit)) chart.append(svg("line", {x1: left, x2: right, y1: y(limit), y2: y(limit), class: "chart-limit"}));
  const incidents = inList(data.incidents).filter(event => event.type === "thermal" && Number.isFinite(event.time)
    && event.time >= data.from && event.time <= data.to).sort((a, b) => a.time - b.time).slice(-150);
  for (const event of incidents) {
    const at = x(event.time);
    const marker = svg("g", {"data-incident": ""});
    marker.append(svg("title", {}, `${eventTime(event.time)} · ${event.worker || "Термозащита"} · ${safeLine(event.detail)}`),
      svg("line", {x1: at, x2: at, y1: top, y2: bottom, class: "chart-marker"}),
      svg("path", {d: `M${at},${top + 6} l-4,-6 h8 z`, class: "chart-alert"}));
    chart.append(marker);
  }
  renderIncidentButtons(incidents);
  if (!values.length) return;
  $("chart-empty").hidden = true;
  const layer = svg("g", {"data-series": ""});
  let line = [];
  let previous = null;
  function flush() {
    if (line.length > 1) layer.append(svg("path", {d: `M${line.join(" L")}`, class: "chart-line", "data-kind": thermal ? "thermal" : metric}));
    line = [];
  }
  for (const row of rows) {
    const value = row[metric];
    if (!Number.isFinite(value)) {
      flush();
      previous = null;
      continue;
    }
    if (previous !== null && Number.isFinite(data.step) && row.t - previous > data.step * 1.1) flush();
    const px = x(row.t), py = y(value);
    line.push(`${px},${py}`);
    const point = svg("circle", {cx: px, cy: py, r: values.length > 180 ? 1.25 : 2.2,
      class: "chart-point", "data-kind": thermal ? "thermal" : metric, "data-point": ""});
    point.append(svg("title", {}, `${eventTime(row.t)} · ${n(value, 3)} ${unit}`));
    layer.append(point);
    previous = row.t;
  }
  flush();
  chart.append(layer);
}

function renderIncidentButtons(incidents) {
  const latest = incidents.slice(-4).reverse();
  const key = JSON.stringify([latest, incidents.length > 4]);
  if (state.incidentsKey === key) return;
  state.incidentsKey = key;
  $("chart-incidents").hidden = !latest.length;
  const buttons = latest.map(event => {
    const button = create("button", `${eventTime(event.time)} · ${event.worker || "Термозащита"}`, "incident-link");
    button.type = "button";
    button.title = safeLine(event.detail);
    button.addEventListener("click", openThermalEvents);
    return button;
  });
  if (incidents.length > 4) {
    const button = create("button", "Все события защиты", "incident-link");
    button.type = "button";
    button.addEventListener("click", openThermalEvents);
    buttons.push(button);
  }
  $("incident-list").replaceChildren(...buttons);
}

function openThermalEvents() {
  $("event-filter").value = "thermal";
  setView("events", true);
}

async function refreshTimeline() {
  if (state.mode !== "app" || !["overview", "events"].includes(state.view)) return;
  const seq = ++state.timelineSeq;
  const generation = state.generation;
  const authEpoch = state.authEpoch;
  const view = state.view;
  const period = $("period").value;
  const current = () => generation === state.generation && authEpoch === state.authEpoch && state.mode === "app" && seq === state.timelineSeq
    && view === state.view && (view !== "overview" || period === $("period").value);
  try {
    if (view === "overview") {
      const data = await api(`history?range=${encodeURIComponent(period)}`);
      if (!current()) return;
      state.history = data;
      state.historyError = "";
    } else {
      const data = await api("events");
      if (!current()) return;
      state.events = data.events;
      state.eventsError = "";
    }
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) { if (!state.authMutation) showLogin(true); return; }
    if (view === "overview") state.historyError = "История временно недоступна.";
    else state.eventsError = state.events ? "Журнал недоступен. Показаны последние полученные данные." : "Журнал временно недоступен.";
  }
  if (view === "overview") drawHistory();
  else renderEvents();
}

function setView(view, focus = false) {
  if (!Object.hasOwn(PAGES, view)) return;
  if (state.view === "nodes" && view !== "nodes") clearFleetInvite();
  state.view = view;
  for (const button of all("[data-view]")) {
    if (button.dataset.view === view) {
      button.setAttribute("aria-current", "page");
      if (focus) button.focus();
    } else button.removeAttribute("aria-current");
  }
  for (const page of Object.keys(PAGES)) $(page).hidden = page !== view;
  text($("page-title"), PAGES[view]);
  drawHistory();
  if (view === "events") renderEvents();
  if (view === "workers") refreshStatus();
  else if (view === "nodes") loadFleet();
  else refreshTimeline();
}

async function refreshStatus({fresh = false} = {}) {
  if (state.mode === "login" || state.authMutation) return;
  if (state.poll) {
    if (!fresh) return state.poll;
    if (state.pollGeneration === state.generation) state.pollAgain = true;
    await state.poll;
    if (state.poll) return state.poll;
  }
  const generation = state.generation;
  const authEpoch = state.authEpoch;
  state.pollGeneration = generation;
  const promise = (async () => {
    try {
      const data = await api("status");
      if (generation !== state.generation || authEpoch !== state.authEpoch) return;
      state.status = data;
      state.online = true;
      connection(`На связи · ${clock(data.time)}`, "good");
      const initial = state.mode !== "app";
      state.mode = "app";
      $("app").hidden = $("logout").hidden = false;
      $("boot").hidden = $("login").hidden = true;
      renderStatus();
      if (initial) setView("overview");
      else if (state.view === "overview") drawHistory();
    } catch (error) {
      if (generation !== state.generation || authEpoch !== state.authEpoch) return;
      if (error.status === 401) { if (!state.authMutation) showLogin(state.mode === "app"); return; }
      state.online = false;
      connection("Нет связи", "warn");
      if (state.mode === "app") renderStatus();
      else {
        text($("boot-message"), error.status === 403 ? "Служба отклонила адрес панели. Проверьте адрес или способ подключения."
          : "Служба не ответила. Проверьте, запущена ли Apollo.NEB на Linux.");
        $("retry").hidden = false;
      }
    }
  })();
  state.poll = promise;
  try { await promise; }
  finally {
    state.poll = null;
    const again = state.pollAgain;
    state.pollAgain = false;
    if (again && generation === state.generation) await refreshStatus();
  }
}

function errorForAction(error, fallback, authEpoch) {
  if (error.status === 401 && !state.authMutation && authEpoch === state.authEpoch) showLogin(true);
  else notice(errorText(error, fallback), "warn");
}

async function workerAction(id, action) {
  const worker = inList(state.status?.workers).find(row => row.id === id);
  if (!worker || state.actions.has(id) || !state.online
    || (action === "start" ? !canStart(worker) : !isRunning(worker))) return;
  const generation = state.generation;
  const authEpoch = state.authEpoch;
  state.actions.add(id);
  notice("");
  renderStatus();
  try {
    await api(action, {id});
    if (generation !== state.generation) return;
    notice(action === "start" ? "Команда запуска принята." : "Команда остановки принята.");
  } catch (error) {
    if (generation === state.generation) errorForAction(error, "Не удалось выполнить команду.", authEpoch);
  } finally {
    if (generation === state.generation) {
      await refreshStatus({fresh: true});
      if (generation === state.generation) {
        state.actions.delete(id);
        renderStatus();
      }
    }
  }
}

function renderGroup(workers) {
  const visibleIds = new Set(workers.map(worker => worker.id));
  for (const id of state.selected) if (!visibleIds.has(id)) state.selected.delete(id);
  $("group-toolbar").hidden = workers.length === 0;
  text($("group-select"), workers.length > GROUP_LIMIT ? `Выбрать первые ${GROUP_LIMIT}` : "Выбрать все");
  $("group-select").disabled = state.groupBusy || (state.selected.size <= GROUP_LIMIT && workers.slice(0, GROUP_LIMIT).every(worker => state.selected.has(worker.id)));
  $("group-clear").disabled = state.groupBusy || !state.selected.size;
  $("group-start").disabled = !state.online || state.groupBusy || state.selected.size > GROUP_LIMIT || !workers.some(worker => state.selected.has(worker.id) && canStart(worker));
  $("group-stop").disabled = !state.online || state.groupBusy || state.selected.size > GROUP_LIMIT
    || !workers.some(worker => state.selected.has(worker.id) && isRunning(worker) && !state.actions.has(worker.id));
  text($("group-count"), `Выбрано: ${state.selected.size} из ${workers.length}${state.selected.size > GROUP_LIMIT ? ` · максимум ${GROUP_LIMIT}` : ""}`);
}

async function groupAction(action) {
  if (!state.online || state.groupBusy || state.selected.size > GROUP_LIMIT) return;
  const rows = inList(state.status?.workers).filter(worker => state.selected.has(worker.id) && !state.actions.has(worker.id)
    && (action === "start" ? canStart(worker) : isRunning(worker)));
  if (!rows.length) return;
  const generation = state.generation;
  const authEpoch = state.authEpoch;
  state.groupBusy = true;
  for (const worker of rows) state.actions.add(worker.id);
  $("group-results").hidden = true;
  text($("group-message"), "Отправляем команды…");
  renderStatus();
  try {
    const data = await api(`group/${action}`, {ids: rows.map(worker => worker.id)}, 60000);
    if (generation !== state.generation) return;
    const results = new Map(inList(data.results).map(result => [result.id, result]));
    let accepted = 0;
    const list = rows.map(worker => {
      const result = results.get(worker.id);
      if (result?.ok) accepted++;
      const li = create("li", `${worker.label || worker.id}: ${result?.ok ? "команда принята." : safeLine(result?.message) || "результат не подтверждён."}`);
      li.dataset.tone = result?.ok ? "good" : "warn";
      return li;
    });
    $("group-results").replaceChildren(...list);
    $("group-results").hidden = false;
    text($("group-message"), `Команда ${action === "start" ? "запуска" : "остановки"}: ${accepted} из ${rows.length} приняты.`);
  } catch (error) {
    if (generation !== state.generation) return;
    if (error.status === 401 && !state.authMutation && authEpoch === state.authEpoch) showLogin(true);
    else text($("group-message"), errorText(error, "Команды не подтверждены. Проверьте статусы воркеров."));
  } finally {
    if (generation === state.generation) {
      await refreshStatus({fresh: true});
      if (generation === state.generation) {
        for (const worker of rows) state.actions.delete(worker.id);
        state.groupBusy = false;
        renderStatus();
      }
    }
  }
}

async function refreshAuthInfo() {
  const seq = ++state.authInfoSeq;
  try {
    const data = await api("auth/info");
    if (seq !== state.authInfoSeq) return;
    state.authTotp = typeof data.totp === "boolean" ? data.totp : null;
  } catch {
    if (seq !== state.authInfoSeq) return;
    state.authTotp = null;
  }
  $("login-code-row").hidden = state.authTotp === false && !$("login-code").value;
  $("login-code").required = state.authTotp === true;
  text($("step-auth-title"), state.authTotp === true ? "Двухфакторная защита включена" : "Защитите вход с помощью 2FA");
}

function setAuxBusy(dialogId, busy) {
  const dialog = $(dialogId);
  dialog.dataset.busy = String(busy);
  dialog.setAttribute("aria-busy", String(busy));
  if (busy) {
    for (const control of dialog.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled)")) {
      control.dataset.busyDisabled = "";
      control.disabled = true;
    }
  } else {
    for (const control of dialog.querySelectorAll("[data-busy-disabled]")) {
      delete control.dataset.busyDisabled;
      control.disabled = false;
    }
  }
}

function renderTotpStep(step) {
  $("totp-begin").hidden = step !== "password";
  $("totp-enroll").hidden = step !== "enroll";
  $("totp-recovery").hidden = step !== "recovery";
  $("totp-disable").hidden = step !== "disable";
}

function clearTotp() {
  totp.seq++;
  clearTimeout(totp.timer);
  totp.expiresAt = 0;
  totp.busy = false;
  setAuxBusy("totp-dialog", false);
  $("totp-begin").reset();
  $("totp-confirm").reset();
  $("totp-disable").reset();
  $("totp-qr").replaceChildren();
  $("totp-qr-wrap").hidden = true;
  $("totp-codes").replaceChildren();
  text($("totp-secret"), "");
  text($("totp-message"), "");
  renderTotpStep("");
}

function drawTotpQr(uri) {
  if (typeof qrcodegen === "undefined" || typeof uri !== "string" || !uri.startsWith("otpauth://totp/") || uri.length > 1000) return;
  try {
    const qr = qrcodegen.QrCode.encodeText(uri, qrcodegen.QrCode.Ecc.MEDIUM);
    const canvas = $("totp-qr");
    const size = qr.size + 8;
    const points = [];
    for (let y = 0; y < qr.size; y++) for (let x = 0; x < qr.size; x++) {
      if (qr.getModule(x, y)) points.push(`M${x + 4} ${y + 4}h1v1h-1z`);
    }
    canvas.setAttribute("viewBox", `0 0 ${size} ${size}`);
    canvas.replaceChildren(svg("path", {d: points.join(""), fill: "currentColor"}));
    $("totp-qr-wrap").hidden = false;
  } catch { /* The operator can always enter the secret manually. */ }
}

async function copySensitive(value, message, stillOpen) {
  try {
    await navigator.clipboard.writeText(value);
    if (stillOpen()) text(message, "Скопировано.");
  } catch {
    if (stillOpen()) text(message, "Не удалось скопировать автоматически. Выделите значение и скопируйте вручную.");
  }
}

function totpInitial() {
  clearTotp();
  text($("totp-status"), state.authTotp === true ? "Двухфакторная защита включена." : "Приложение-аутентификатор добавит второй шаг входа.");
  if (state.authTotp !== true) {
    renderTotpStep("password");
    $("totp-password").focus();
  } else if (location.protocol === "http:" && location.hostname === "127.0.0.1") {
    renderTotpStep("disable");
  } else {
    text($("totp-status"), "Двухфакторная защита включена и обязательна для внешнего HTTPS-доступа.");
  }
}

function openTotp() {
  if (!state.online || $("totp-dialog").open) return;
  $("totp-dialog").showModal();
  totpInitial();
}

async function beginTotp(event) {
  event.preventDefault();
  if (totp.busy) return;
  const seq = ++totp.seq;
  const password = $("totp-password").value;
  $("totp-password").value = "";
  totp.busy = true;
  setAuxBusy("totp-dialog", true);
  text($("totp-message"), "Подготавливаем настройку…");
  try {
    const data = await api("auth/totp/begin", {password});
    if (seq !== totp.seq || !$("totp-dialog").open) return;
    if (typeof data.secret !== "string" || !/^[A-Z2-7=]{8,160}$/.test(data.secret)) throw new Error();
    text($("totp-secret"), data.secret);
    drawTotpQr(data.uri);
    renderTotpStep("enroll");
    text($("totp-message"), "");
    clearTimeout(totp.timer);
    if (Number.isFinite(data.expires_in) && data.expires_in > 0) {
      totp.expiresAt = Date.now() + Math.min(data.expires_in, 3600) * 1000;
      totp.timer = setTimeout(() => {
        if (seq === totp.seq && !totp.busy && $("totp-dialog").open) {
          totpInitial();
          text($("totp-message"), "Срок настройки истёк. Начните заново.");
        }
      }, totp.expiresAt - Date.now());
    }
  } catch (error) {
    if (seq === totp.seq) text($("totp-message"), error.status === 401 || error.status === 403 ? "Пароль панели не подтверждён."
      : errorText(error, "Не удалось подготовить 2FA."));
  } finally {
    if (seq === totp.seq) {
      totp.busy = false;
      setAuxBusy("totp-dialog", false);
      ($("totp-enroll").hidden ? $("totp-password") : $("totp-code")).focus();
    }
  }
}

async function confirmTotp(event) {
  event.preventDefault();
  if (totp.busy || $("totp-enroll").hidden) return;
  const seq = totp.seq;
  const code = $("totp-code").value;
  $("totp-code").value = "";
  totp.busy = state.authMutation = true;
  setAuxBusy("totp-dialog", true);
  text($("totp-message"), "Проверяем код…");
  try {
    const data = await api("auth/totp/confirm", {code});
    if (seq !== totp.seq || !$("totp-dialog").open) return;
    clearTimeout(totp.timer);
    text($("totp-secret"), "");
    $("totp-qr").replaceChildren();
    $("totp-qr-wrap").hidden = true;
    const codes = inList(data.recovery_codes).filter(code => typeof code === "string" && code.length <= 80).slice(0, 20);
    $("totp-codes").replaceChildren(...codes.map(code => create("li", code)));
    $("totp-recovery-copy").hidden = !codes.length;
    renderTotpStep("recovery");
    text($("totp-status"), "Двухфакторная защита включена.");
    text($("totp-message"), codes.length ? "" : "Сервер не вернул список резервных кодов.");
    state.authEpoch++;
    state.authInfoSeq++;
    state.authTotp = true;
    text($("step-auth-title"), "Двухфакторная защита включена");
  } catch (error) {
    if (seq === totp.seq) text($("totp-message"), error.status === 401 || error.status === 403 ? "Код не подтверждён."
      : errorText(error, "Не удалось включить 2FA."));
  } finally {
    if (seq === totp.seq) {
      totp.busy = state.authMutation = false;
      setAuxBusy("totp-dialog", false);
      if (!$("totp-recovery").hidden) {
        ($("totp-recovery-copy").hidden ? $("totp-dialog").querySelector("[data-close]") : $("totp-recovery-copy")).focus();
        await refreshStatus({fresh: true});
      } else if (totp.expiresAt && Date.now() >= totp.expiresAt) {
        totpInitial();
        text($("totp-message"), "Срок настройки истёк. Начните заново.");
      } else $("totp-code").focus();
    }
  }
}

async function disableTotp(event) {
  event.preventDefault();
  if (totp.busy || $("totp-disable").hidden) return;
  const seq = totp.seq;
  const password = $("totp-disable-password").value;
  const code = $("totp-disable-code").value.trim();
  $("totp-disable").reset();
  totp.busy = state.authMutation = true;
  setAuxBusy("totp-dialog", true);
  text($("totp-message"), "Проверяем пароль и код…");
  try {
    await api("auth/totp/disable", {password, code});
    if (seq !== totp.seq) return;
    state.authEpoch++;
    state.authInfoSeq++;
    state.authTotp = false;
    text($("step-auth-title"), "Защитите вход с помощью 2FA");
    state.authMutation = false;
    totpInitial();
    text($("totp-message"), "2FA отключена. Резервные коды больше не действуют.");
    await refreshStatus({fresh: true});
  } catch (error) {
    if (seq === totp.seq) text($("totp-message"), error.status === 401 || error.status === 403 ? "Пароль или код не подтверждён."
      : errorText(error, "Не удалось отключить 2FA."));
  } finally {
    if (seq === totp.seq) {
      totp.busy = state.authMutation = false;
      setAuxBusy("totp-dialog", false);
      $("totp-disable-password").focus();
    }
  }
}

function resetAuxiliary() {
  clearTotp();
  resetWalletForms();
  vault.seq++;
  vault.busy = false;
  vault.data = null;
  setup.seq++;
  setup.busy = false;
  setup.data = null;
  setup.selected.clear();
  for (const id of ["wallets-dialog", "discovery-dialog"]) setAuxBusy(id, false);
  for (const id of ["wallets-list", "miner-list", "device-list", "group-results"]) $(id).replaceChildren();
  for (const id of ["wallets-message", "wallets-status", "discovery-message", "discovery-status", "wallet-export-target", "group-message"]) text($(id), "");
  $("group-results").hidden = true;
  releaseSeq++;
  $("release-check").disabled = false;
  $("release-link").hidden = true;
  $("release-guide").hidden = true;
  $("release-link").removeAttribute("href");
  text($("release-status"), "Проверка выполняется по запросу.");
  clearFleet();
}

function resetWalletForms() {
  vault.exportId = null;
  for (const id of ["wallet-create", "wallet-export"]) {
    $(id).reset();
    $(id).hidden = true;
  }
  text($("wallet-export-target"), "");
}

function actionButton(label, handler, css) {
  const btn = create("button", label, css);
  btn.type = "button";
  btn.addEventListener("click", handler);
  return btn;
}

function renderWallets() {
  const data = vault.data;
  const items = inList(data?.items);
  $("wallets-install").hidden = !data || data.available !== false;
  $("wallet-new").hidden = !data?.available;
  text($("wallets-status"), safeLine(data?.message) || (data?.available ? items.length ? `Кошельков: ${items.length}` : "Кошельков пока нет."
    : data ? "Для создания кошелька требуется установить проверенный модуль. Публичный адрес можно добавить без него."
    : "Запрашиваем списки кошельков…"));
  const list = items.slice(0, 100).map(item => {
    const row = create("li");
    const body = create("div");
    const label = safeLine(item.label) || "Кошелёк NEB";
    const address = safeLine(item.address);
    body.append(create("h3", label), create("code", address));
    const actions = create("div", undefined, "panel-actions");
    if (/^neb1[023456789acdefghjklmnpqrstuvwxyz]{58}$/.test(address)) {
      actions.append(actionButton("В профиль", () => {
        $("wallets-dialog").close();
        openProfile(address);
      }));
    }
    if (data?.available && typeof item.id === "string") {
      actions.append(actionButton("Скачать резервную копию", () => {
        resetWalletForms();
        vault.exportId = item.id;
        text($("wallet-export-target"), label);
        text($("wallets-message"), "");
        $("wallet-export").hidden = false;
        $("wallet-export-password").focus();
      }));
    }
    row.append(body, actions);
    return row;
  });
  $("wallets-list").replaceChildren(...list);
}

async function loadWallets() {
  if (vault.busy || !$("wallets-dialog").open) return;
  const seq = ++vault.seq;
  vault.busy = true;
  setAuxBusy("wallets-dialog", true);
  if (!vault.data) text($("wallets-status"), "Запрашиваем список кошельков…");
  try {
    const data = await api("wallets");
    if (seq !== vault.seq || !$("wallets-dialog").open) return;
    vault.data = data;
    renderWallets();
  } catch (error) {
    if (seq !== vault.seq) return;
    if (error.status === 401) return showLogin(true);
    vault.data = null;
    renderWallets();
    text($("wallets-status"), "Список кошельков недоступен.");
  } finally {
    if (seq === vault.seq) {
      vault.busy = false;
      setAuxBusy("wallets-dialog", false);
    }
  }
}

function openWallets() {
  if (!state.online || $("wallets-dialog").open) return;
  $("wallets-dialog").showModal();
  loadWallets();
}

function saveEncryptedBackup(data) {
  if (typeof data.backup !== "string" || !data.backup || data.backup.length > 8192) throw new Error();
  const blob = new Blob([Uint8Array.from(atob(data.backup), char => char.charCodeAt(0))], {type: "application/octet-stream"});
  const link = create("a");
  link.download = typeof data.filename === "string" && /^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,100}\.neb$/.test(data.filename) ? data.filename : "wallet.neb";
  const url = URL.createObjectURL(blob);
  link.href = url;
  document.body.append(link);
  try { link.click(); }
  finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
}

async function walletMutation(path, body, waitMessage, onSuccess) {
  if (vault.busy || !$("wallets-dialog").open) return;
  const seq = ++vault.seq;
  vault.busy = true;
  setAuxBusy("wallets-dialog", true);
  text($("wallets-message"), waitMessage);
  try {
    const data = await api(path, body, path === "wallets/install" ? 180000 : 45000);
    if (seq === vault.seq) await onSuccess(data);
  } catch (error) {
    if (seq !== vault.seq) return;
    if (error.status === 401) return showLogin(true);
    text($("wallets-message"), errorText(error, "Операция не подтверждена."));
  } finally {
    if (seq === vault.seq) {
      vault.busy = false;
      setAuxBusy("wallets-dialog", false);
    }
  }
}

function renderDiscovery() {
  const data = setup.data;
  const devices = inList(data?.devices);
  const miners = inList(data?.miners);
  const configured = new Set(inList(state.status?.workers).map(worker => worker.device));
  text($("discovery-status"), data ? `Обнаружено GPU: ${devices.length} · настроено воркеров: ${n(data.installed_workers_count, 0)}.`
    : "Проверяем доступные устройства…");
  const minerRows = miners.slice(0, 20).map(miner => {
    const row = create("li");
    const label = safeLine(miner.label) || safeLine(miner.id);
    const info = create("div");
    const size = Number.isFinite(miner.size) && miner.size > 0 ? ` · ${n(miner.size / 1048576, 2)} МиБ` : "";
    const minerState = miner.installed ? "Установлен" : miner.state === "downloading" ? "Загрузка и проверка…" :
      miner.state === "failed" ? "Установка не подтверждена" : miner.available ? "Доступен для загрузки" : "Нет проверенного файла";
    info.append(create("h3", label), create("p", minerState + size, "subline"));
    if (miner.message) info.append(create("p", safeLine(miner.message), "subline"));
    row.append(info);
    if (!miner.installed && miner.available && miner.state !== "downloading" && typeof miner.id === "string") {
      row.append(actionButton("Загрузить " + label, () => discoveryMutation("setup/prepare", {adapter: miner.id},
        "Запрашиваем загрузку и проверку майнера…", "Загрузка запрошена. Воркеры не запущены.")));
    }
    return row;
  });
  $("miner-list").replaceChildren(...minerRows);
  if (data && !miners.length) $("miner-list").append(create("li", "Проверенные майнеры не настроены."));
  const available = new Set();
  const rows = devices.slice(0, 256).map(device => {
    const row = create("div");
    const dev = safeLine(device.device);
    const vendor = typeof device.vendor === "string" ? device.vendor.toLowerCase() : "";
    const vendorTitle = vendor === "amd" ? "AMD" : vendor === "nvidia" ? "NVIDIA" : "GPU";
    const matchedMiner = miners.find(miner => miner.vendor === vendor || new RegExp(vendor === "amd" ? "opencl|amd" : vendor === "nvidia" ? "cuda|nvidia" : "^$").test(String(miner.id)));
    const reason = configured.has(dev) ? "Уже добавлен" : !Number.isFinite(device.edge) || (vendor === "amd" && !Number.isFinite(device.hot))
      ? "Нет необходимых показаний температуры" : !matchedMiner?.installed ? "Майнер не установлен" : "";
    const allowed = /^(?:[a-f0-9]{4,8}:)?[a-f0-9]{2}:[a-f0-9]{2}\.[0-7]$/i.test(dev) && !reason;
    const label = create("label", undefined, "check");
    const check = create("input");
    check.type = "checkbox";
    check.disabled = !allowed;
    check.checked = allowed && setup.selected.has(dev);
    check.addEventListener("change", () => {
      if (check.checked) setup.selected.add(dev);
      else setup.selected.delete(dev);
      $("discovery-save").disabled = !setup.selected.size || setup.selected.size > GROUP_LIMIT;
      text($("discovery-message"), setup.selected.size > GROUP_LIMIT ? `Выберите не более ${GROUP_LIMIT} GPU за раз.` : "");
    });
    if (allowed) available.add(dev);
    label.append(check, create("span", `${vendorTitle} · ${dev}`, "mono"));
    row.append(label, create("p", reason || `Ядро ${fmt(device.edge, "°C")} · hotspot ${fmt(device.hot, "°C")}`, "subline"));
    return row;
  });
  for (const id of setup.selected) if (!available.has(id)) setup.selected.delete(id);
  $("device-list").replaceChildren(...rows);
  if (data && !devices.length) $("device-list").append(create("p", "Поддерживаемые PCI GPU не обнаружены. Проверьте драйвер и права доступа."));
  $("discovery-save").disabled = !setup.selected.size || setup.selected.size > GROUP_LIMIT;
}

function openDiscovery() {
  if (!state.online || $("discovery-dialog").open) return;
  $("discovery-dialog").showModal();
  loadDiscovery();
}

async function loadDiscovery(path, body, waitMessage, successMessage) {
  if (setup.busy || !$("discovery-dialog").open) return;
  const seq = ++setup.seq;
  setup.busy = true;
  setAuxBusy("discovery-dialog", true);
  if (!setup.data) text($("discovery-status"), "Проверяем доступные устройства…");
  if (waitMessage) text($("discovery-message"), waitMessage);
  try {
    if (path) {
      await api(path, body, path === "setup/prepare" ? 180000 : 30000);
      if (seq !== setup.seq) return;
      if (path === "setup/workers") {
        setup.selected.clear();
        await refreshStatus({fresh: true});
      }
      if (seq === setup.seq) text($("discovery-message"), successMessage);
    }
    const data = await api("setup");
    if (seq !== setup.seq || !$("discovery-dialog").open) return;
    setup.data = data;
    renderDiscovery();
  } catch (error) {
    if (seq !== setup.seq) return;
    if (error.status === 401) return showLogin(true);
    setup.data = null;
    renderDiscovery();
    text($("discovery-status"), "Сведения об устройствах недоступны.");
    text($("discovery-message"), errorText(error, "Операция не подтверждена."));
  } finally {
    if (seq === setup.seq) {
      setup.busy = false;
      setAuxBusy("discovery-dialog", false);
      $("discovery-save").disabled = !setup.selected.size || setup.selected.size > GROUP_LIMIT;
    }
  }
}

function discoveryMutation(path, body, waitMessage, successMessage) {
  return loadDiscovery(path, body, waitMessage, successMessage);
}

async function checkReleases() {
  const seq = ++releaseSeq;
  const btn = $("release-check"), link = $("release-link");
  btn.disabled = true;
  link.hidden = true;
  $("release-guide").hidden = true;
  link.removeAttribute("href");
  text($("release-status"), "Проверяем релизы…");
  try {
    const data = await api("releases");
    if (seq !== releaseSeq) return;
    const current = safeLine(data.current);
    const latest = safeLine(data.latest);
    const prefix = current ? `Установлено: ${current}. ` : "";
    if (data.state === "current") text($("release-status"), prefix + "Новых релизов нет.");
    else if (data.state === "available" && latest) {
      text($("release-status"), prefix + `Доступен релиз ${latest}.`);
      $("release-guide").hidden = false;
      try {
        const url = new URL(data.url);
        if (url.protocol === "https:" && !url.username && !url.password) {
          link.href = url.href;
          link.hidden = false;
        }
      } catch { /* Only an explicit HTTPS release address is linked. */ }
    } else text($("release-status"), prefix + "Не удалось проверить наличие обновления.");
  } catch (error) {
    if (seq !== releaseSeq) return;
    if (error.status === 401) return showLogin(true);
    text($("release-status"), "Проверка релизов недоступна.");
  } finally {
    if (seq === releaseSeq) btn.disabled = false;
  }
}

const NODE_STATES = new Map([
  ["pending", ["Ожидает подключения", "warn"]], ["online", ["На связи", "good"]],
  ["offline", ["Нет связи", "warn"]], ["expired", ["Код истёк", "warn"]], ["revoked", ["Доступ отозван", "neutral"]],
]);
const COMMAND_STATES = new Map([
  ["queued", "В очереди"], ["sent", "Передано ноде"], ["acknowledged", "Нода подтвердила обработку"], ["unknown", "Результат неизвестен"],
]);

function clearFleetInvite() {
  clearTimeout(fleet.inviteTimer);
  $("fleet-invite").hidden = true;
  for (const id of ["fleet-invite-id", "fleet-invite-code", "fleet-invite-command", "fleet-invite-expiry", "fleet-invite-hint"]) text($(id), "");
}

function clearFleet() {
  fleet.generation++;
  fleet.loading = fleet.busy = fleet.fresh = false;
  fleet.nodes = null;
  fleet.key = "";
  fleet.selected.clear();
  fleet.revokeId = null;
  clearFleetInvite();
  $("fleet-enroll").reset();
  $("fleet-enroll").hidden = true;
  $("node-list").replaceChildren();
  text($("fleet-status"), "Проверяем список нод…");
  text($("fleet-message"), "");
}

function showFleetInvite(data) {
  if (!/^[a-f0-9]{24}$/.test(data.id) || !/^[A-Za-z0-9_-]{24}$/.test(data.code) || !Number.isFinite(data.expires)) {
    throw new Error();
  }
  if (state.view !== "nodes") return;
  clearFleetInvite();
  text($("fleet-invite-id"), data.id);
  text($("fleet-invite-code"), data.code);
  text($("fleet-invite-expiry"), `Код доступен до ${clock(data.expires)}. Не передавайте его третьим лицам.`);
  const loopback = location.protocol === "http:" && location.hostname === "127.0.0.1";
  const canPair = location.protocol === "https:" || loopback;
  $("fleet-command-copy").hidden = !canPair;
  if (canPair) text($("fleet-invite-command"), `apollo-neb node-pair --origin ${location.origin} --id ${data.id}${loopback ? " --allow-loopback" : ""}`);
  text($("fleet-invite-hint"), loopback ?
    "Для удалённой ноды сначала откройте SSH-туннель до панели на тот же порт. Введите код, когда команда запросит его." :
    canPair ? "Выполните команду на ноде. Введите код только после запроса в терминале." : "Используйте внешний HTTPS-адрес панели или локальный SSH-туннель.");
  $("fleet-invite").hidden = false;
  fleet.inviteTimer = setTimeout(() => {
    clearFleetInvite();
    text($("fleet-message"), "Срок кода подключения истёк.");
  }, Math.max(0, Math.min(600000, data.expires * 1000 - Date.now())));
  $("fleet-hide-invite").focus();
}

function renderFleet() {
  const rows = inList(fleet.nodes);
  $("fleet-enroll").hidden = !fleet.fresh;
  $("fleet-label").disabled = fleet.busy;
  $("fleet-enroll").querySelector('button[type="submit"]').disabled = fleet.busy;
  $("fleet-refresh").disabled = fleet.loading || fleet.busy;
  if (fleet.fresh) text($("fleet-status"), rows.length ? `Нод: ${rows.length}. Команды не повторяются автоматически.` : "Подключённых нод пока нет.");
  else if (fleet.nodes) text($("fleet-status"), "Связь с реестром потеряна. Показаны последние полученные данные; команды недоступны.");
  const key = JSON.stringify([fleet.fresh, fleet.busy, rows.map(({last_seen, ...node}) => node)]);
  if (key === fleet.key) {
    for (const time of $("node-list").querySelectorAll("[data-node-time]")) {
      const node = rows.find(node => node.id === time.dataset.nodeTime);
      text(time, Number.isFinite(node?.last_seen) ? `Последний контакт: ${eventTime(node.last_seen)}` : "Контакт не зафиксирован.");
    }
    return;
  }
  fleet.key = key;
  const expanded = new Set([...$("node-list").querySelectorAll("details[open]")].map(el => el.dataset.nodeCommands));
  const activeElement = document.activeElement;
  const activeControl = activeElement?.closest("#node-list") ? activeElement.dataset.controlKey : null;
  const cards = rows.slice(0, 128).map(node => {
    const card = create("article", undefined, "surface node-card");
    const head = create("div", undefined, "panel-head");
    const info = create("div");
    const name = safeLine(node.label) || safeLine(node.id) || "Нода";
    const time = create("p", Number.isFinite(node.last_seen) ? `Последний контакт: ${eventTime(node.last_seen)}` : "Контакт не зафиксирован.");
    time.dataset.nodeTime = node.id;
    info.append(create("h2", name), time);
    const [nodeLabel, tone] = NODE_STATES.get(node.status) || ["Состояние неизвестно", "neutral"];
    const mark = create("span", nodeLabel, "state");
    mark.dataset.tone = tone;
    head.append(info, mark);
    card.append(head);
    const workers = inList(node.workers).slice(0, 128);
    const commands = inList(node.commands).slice(0, 10);
    const pending = commands.some(item => item.status === "queued" || item.status === "sent");
    const canControl = fleet.fresh && !fleet.busy && node.status === "online" && !pending;
    const selected = fleet.selected.get(node.id) || new Set();
    fleet.selected.set(node.id, selected);
    for (const id of selected) if (!workers.some(worker => worker.id === id)) selected.delete(id);
    const controls = create("div", undefined, "panel-actions");
    const selectIds = action => workers.filter(worker => selected.has(worker.id) &&
      (action === "stop" ? isRunning(worker) : ["stopped", "failed", "blocked", "protection"].includes(worker.state))).map(worker => worker.id);
    const start = actionButton("Запустить выбранные", () => changeFleet("command", {id: node.id, action: "start", ids: selectIds("start")}), "primary");
    const stop = actionButton("Остановить выбранные", () => changeFleet("command", {id: node.id, action: "stop", ids: selectIds("stop")}), "stop-button");
    const count = create("span", `Не более ${NODE_GROUP_LIMIT} воркеров за команду.`, "subline");
    start.dataset.controlKey = node.id + ":start";
    stop.dataset.controlKey = node.id + ":stop";
    const setControls = () => {
      start.disabled = !canControl || selected.size > NODE_GROUP_LIMIT || !selectIds("start").length;
      stop.disabled = !canControl || selected.size > NODE_GROUP_LIMIT || !selectIds("stop").length;
      count.hidden = selected.size <= NODE_GROUP_LIMIT;
    };
    if (workers.length) {
      const list = create("ul", undefined, "setup-rows");
      for (const worker of workers) {
        const row = create("li");
        const label = create("label", undefined, "check");
        const input = create("input");
        input.type = "checkbox";
        input.checked = selected.has(worker.id);
        input.disabled = !canControl;
        input.dataset.controlKey = node.id + ":worker:" + worker.id;
        input.addEventListener("change", () => {
          if (input.checked) selected.add(worker.id);
          else selected.delete(worker.id);
          setControls();
        });
        label.append(input, create("span", safeLine(worker.label) || safeLine(worker.id)));
        row.append(label, create("p", `${STATES.get(worker.state)?.[0] || "Неизвестно"} · ${rate(worker.rate_hs)}`, "subline"));
        list.append(row);
      }
      card.append(list);
      controls.append(start, stop, count);
      setControls();
    } else card.append(create("p", "Нода ещё не сообщила о воркерах.", "subline"));
    if (node.status !== "revoked" && node.status !== "expired") {
      const revoke = actionButton("Отозвать доступ", () => {
        fleet.revokeId = node.id;
        text($("fleet-revoke-target"), name);
        $("fleet-revoke-dialog").showModal();
      }, "quiet");
      revoke.dataset.controlKey = node.id + ":revoke";
      revoke.disabled = !fleet.fresh || fleet.busy;
      controls.append(revoke);
    }
    card.append(controls);
    if (commands.length) {
      const details = create("details");
      details.dataset.nodeCommands = node.id;
      details.open = expanded.has(node.id);
      details.append(create("summary", "Последние команды"));
      const list = create("ul", undefined, "result-list");
      for (const command of commands) {
        const status = COMMAND_STATES.get(command.status) || "Статус неизвестен";
        const action = command.action === "stop" ? "Остановка" : "Запуск";
        const identities = inList(command.workers).map(id => workers.find(worker => worker.id === id)?.label || id).join(", ");
        const warning = command.status === "unknown" ? " Не повторяйте без проверки состояния воркеров." : "";
        const li = create("li", `${action}: ${identities || "—"} · ${status}.${warning}`);
        li.dataset.tone = command.status === "unknown" ? "warn" : "good";
        list.append(li);
      }
      details.append(list);
      card.append(details);
    }
    return card;
  });
  $("node-list").replaceChildren(...cards);
  if (activeControl) [...$("node-list").querySelectorAll("[data-control-key]")].find(el => el.dataset.controlKey === activeControl && !el.disabled)?.focus({preventScroll: true});
}

async function loadFleet() {
  if (state.mode !== "app" || state.view !== "nodes" || fleet.loading || fleet.busy) return;
  const generation = fleet.generation;
  const authEpoch = state.authEpoch;
  fleet.loading = true;
  $("fleet-refresh").disabled = true;
  try {
    const data = await api("fleet");
    if (generation !== fleet.generation || authEpoch !== state.authEpoch) return;
    if (!Array.isArray(data.nodes)) throw new Error();
    fleet.nodes = data.nodes;
    fleet.fresh = true;
  } catch (error) {
    if (generation !== fleet.generation || authEpoch !== state.authEpoch) return;
    if (error.status === 401) { if (!state.authMutation) showLogin(true); return; }
    fleet.fresh = false;
    if (!fleet.nodes) text($("fleet-status"), error.status === 404 ? "Управление нодами не настроено на сервере." : "Реестр нод недоступен.");
  } finally {
    if (generation === fleet.generation) {
      fleet.loading = false;
      renderFleet();
    }
  }
}

async function changeFleet(kind, body) {
  if (!fleet.fresh || fleet.busy || state.view !== "nodes" || !state.online) return;
  if (kind === "command" && (!body.ids?.length || body.ids.length > NODE_GROUP_LIMIT || !fleet.nodes?.some(node => node.id === body.id && node.status === "online"))) return;
  const generation = ++fleet.generation;
  const authEpoch = state.authEpoch;
  fleet.loading = false;
  fleet.busy = true;
  renderFleet();
  text($("fleet-message"), "Отправляем запрос…");
  try {
    const data = await api("fleet/" + kind, body);
    if (generation !== fleet.generation) return;
    if (kind === "enroll") {
      $("fleet-enroll").reset();
      text($("fleet-message"), "");
      showFleetInvite(data);
    } else if (kind === "revoke") {
      text($("fleet-message"), "Доступ отозван. Состояние майнера этим не изменено.");
    } else {
      text($("fleet-message"), data.status === "queued" ? "Команда в очереди. Это не подтверждение исполнения." :
        "Состояние команды не подтверждено. Проверьте статус перед любым повтором.");
    }
  } catch (error) {
    if (generation !== fleet.generation) return;
    if (error.status === 401 && !state.authMutation && authEpoch === state.authEpoch) return showLogin(true);
    text($("fleet-message"), errorText(error, "Запрос не подтверждён. Проверьте список нод и последние команды."));
  } finally {
    if (generation === fleet.generation) {
      fleet.busy = false;
      fleet.fresh = false;
      renderFleet();
      await loadFleet();
    }
  }
}

function openProfile(address) {
  if (!state.online || !state.status || $("profile-dialog").open) return;
  const {profile, pools} = state.status;
  $("wallet").value = typeof address === "string" ? address : profile?.wallet || "";
  $("worker-name").value = profile?.worker || "";
  $("pool").replaceChildren(...inList(pools).map(pool => {
    const option = create("option", pool.label || pool.id);
    option.value = pool.id;
    return option;
  }));
  if (inList(pools).some(pool => pool.id === profile?.pool)) $("pool").value = profile.pool;
  $("profile-save").disabled = !inList(pools).length;
  text($("profile-message"), inList(pools).length ? "" : "На сервере не настроено ни одного пула.");
  $("profile-dialog").showModal();
  $("wallet").focus();
}

function openLimits() {
  if (!state.online || !state.status || $("limits-dialog").open) return;
  const limits = state.status.limits || {};
  for (const key of TEMPERATURES) $(`${key}-limit`).value = limits[`${key}_limit`] ?? "";
  $("limits-ack").checked = false;
  $("limits-save").disabled = true;
  text($("limits-message"), "");
  $("limits-dialog").showModal();
  $("edge-limit").focus();
}

for (const kind of ["profile", "limits"]) {
  const form = $(`${kind}-form`);
  const dialog = $(`${kind}-dialog`);
  const submit = $(`${kind}-save`);
  const msg = $(`${kind}-message`);
  function setBusy(busy) {
    form.dataset.busy = String(busy);
    for (const input of form.querySelectorAll("input, select")) input.disabled = busy;
    dialog.querySelector("[data-close]").disabled = busy;
    submit.disabled = busy || (kind === "limits" ? !$("limits-ack").checked : !$("pool").options.length);
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (submit.disabled) return;
    if (!state.online) return text(msg, "Нет связи с панелью. Повторите после переподключения.");
    const data = kind === "profile"
      ? {wallet: $("wallet").value.trim(), worker: $("worker-name").value.trim(), pool: $("pool").value}
      : {edge_limit: $("edge-limit").valueAsNumber, hot_limit: $("hot-limit").valueAsNumber,
        hbm_limit: $("hbm-limit").value === "" ? null : $("hbm-limit").valueAsNumber, ack: $("limits-ack").checked};
    if (kind === "limits" && !data.ack) return;
    if (kind === "limits" && data.edge_limit > data.hot_limit) return text(msg, "Предел ядра не может превышать предел hotspot.");
    const generation = state.generation;
    setBusy(true);
    text(msg, "Сохраняем…");
    try {
      await api(kind, data);
      if (generation !== state.generation) return;
      dialog.close();
      notice(kind === "profile" ? "Профиль сохранён." : "Температурные пределы сохранены.");
      await refreshStatus({fresh: true});
      refreshTimeline();
    } catch (error) {
      if (generation !== state.generation) return;
      if (error.status === 401) showLogin(true);
      else text(msg, errorText(error, "Не удалось сохранить изменения."));
    } finally { setBusy(false); }
  });
  dialog.addEventListener("cancel", event => {
    if (form.dataset.busy === "true") event.preventDefault();
  });
}

$("limits-form").addEventListener("input", event => {
  if (event.target !== $("limits-ack")) $("limits-ack").checked = false;
  $("limits-save").disabled = !$("limits-ack").checked;
});

$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const btn = event.currentTarget.querySelector("button");
  if (btn.disabled) return;
  const pass = $("password");
  const code = $("login-code");
  btn.disabled = pass.disabled = code.disabled = true;
  text($("login-message"), "Проверяем данные…");
  try {
    await api("login", {password: pass.value, code: code.value.trim()});
    pass.value = code.value = "";
    state.generation++;
    state.mode = "boot";
    $("login").hidden = true;
    $("boot").hidden = false;
    text($("boot-message"), "Ожидаем ответ локальной службы…");
    await refreshStatus({fresh: true});
    refreshAuthInfo();
    if (state.mode === "app") $("main").focus({preventScroll: true});
  } catch (error) {
    code.value = "";
    text($("login-message"), error.status === 401 ? "Неверный пароль или код."
      : errorText(error, "Не удалось войти."));
  } finally {
    btn.disabled = pass.disabled = code.disabled = false;
    if (!$("login").hidden) pass.focus();
  }
});

$("logout").addEventListener("click", async () => {
  const button = $("logout");
  if (button.disabled) return;
  button.disabled = true;
  try {
    await api("logout", {});
    showLogin();
  } catch (error) {
    if (error.status === 401) showLogin();
    else notice(errorText(error, "Не удалось завершить сеанс."), "warn");
  } finally { button.disabled = false; }
});

for (const button of all("[data-view]")) button.addEventListener("click", () => setView(button.dataset.view));
for (const button of all("[data-go]")) button.addEventListener("click", () => setView(button.dataset.go, true));
for (const button of all("[data-profile], #profile-open")) button.addEventListener("click", openProfile);
for (const button of all("[data-limits]")) button.addEventListener("click", openLimits);
for (const button of all("[data-close]")) button.addEventListener("click", () => {
  const dialog = button.closest("dialog");
  if (dialog.id === "totp-dialog") clearTotp();
  if (dialog.id === "wallets-dialog") resetWalletForms();
  dialog.close();
});
for (const button of all("[data-setup]")) button.addEventListener("click", () => {
  text($("copy-message"), "");
  $("setup-dialog").showModal();
});
for (const button of all("[data-copy]")) button.addEventListener("click", async () => {
  const command = button.parentNode.querySelector("code");
  try {
    await navigator.clipboard.writeText(command.textContent);
    text($("copy-message"), "Скопировано.");
  } catch {
    const selection = getSelection();
    if (selection) {
      const range = document.createRange();
      range.selectNodeContents(command);
      selection.removeAllRanges();
      selection.addRange(range);
    }
    text($("copy-message"), "Не удалось скопировать автоматически. Скопируйте текст вручную.");
  }
});

for (const button of all("[data-totp]")) button.addEventListener("click", openTotp);
for (const button of all("[data-wallets]")) button.addEventListener("click", openWallets);
for (const button of all("[data-discovery]")) button.addEventListener("click", openDiscovery);
for (const id of ["totp-dialog", "wallets-dialog", "discovery-dialog"]) {
  $(id).addEventListener("cancel", event => {
    if ($(id).dataset.busy === "true") event.preventDefault();
    else if (id === "totp-dialog") clearTotp();
    else if (id === "wallets-dialog") resetWalletForms();
  });
}
$("totp-dialog").addEventListener("close", () => { if (!$("totp-dialog").open) clearTotp(); });
$("totp-begin").addEventListener("submit", beginTotp);
$("totp-confirm").addEventListener("submit", confirmTotp);
$("totp-disable").addEventListener("submit", disableTotp);
$("totp-restart").addEventListener("click", totpInitial);
$("totp-copy").addEventListener("click", () => {
  const seq = totp.seq;
  if ($("totp-secret").textContent) copySensitive($("totp-secret").textContent, $("totp-message"), () => seq === totp.seq);
});
$("totp-recovery-copy").addEventListener("click", () => {
  const seq = totp.seq;
  const codes = [...$("totp-codes").children].map(row => row.textContent).join("\n");
  if (codes) copySensitive(codes, $("totp-message"), () => seq === totp.seq);
});

$("wallets-dialog").addEventListener("close", () => {
  if ($("wallets-dialog").open) return;
  vault.seq++;
  vault.busy = false;
  setAuxBusy("wallets-dialog", false);
  resetWalletForms();
  text($("wallets-message"), "");
});
$("wallets-refresh").addEventListener("click", loadWallets);
$("wallets-install").addEventListener("click", () => walletMutation("wallets/install", {}, "Запрашиваем загрузку проверенного модуля…", data => {
  if (typeof data.available !== "boolean") throw new Error();
  vault.data = data;
  renderWallets();
  text($("wallets-message"), vault.data.available ? "Модуль кошелька установлен." : "Загрузка запрошена. Статус можно обновить после проверки файла.");
}));
$("wallet-new").addEventListener("click", () => {
  resetWalletForms();
  text($("wallets-message"), "");
  $("wallet-create").hidden = false;
  $("wallet-label").focus();
});
for (const button of all("[data-wallet-cancel]")) button.addEventListener("click", () => {
  resetWalletForms();
  text($("wallets-message"), "");
  ($("wallet-new").hidden ? $("wallets-refresh") : $("wallet-new")).focus();
});
$("wallet-create").addEventListener("submit", event => {
  event.preventDefault();
  if (vault.busy || !vault.data?.available) return;
  const label = $("wallet-label").value.trim();
  const password = $("wallet-create-password").value;
  if (!label) return text($("wallets-message"), "Введите название кошелька.");
  if (password !== $("wallet-create-repeat").value) return text($("wallets-message"), "Пароли не совпадают.");
  $("wallet-create-password").value = $("wallet-create-repeat").value = "";
  walletMutation("wallets/create", {label, password}, "Создаём кошелёк…", data => {
    if (typeof data.id !== "string" || typeof data.address !== "string") throw new Error();
    resetWalletForms();
    vault.data = {...vault.data, items: [...inList(vault.data?.items).filter(item => item.id !== data.id), data]};
    renderWallets();
    text($("wallets-message"), "Кошелёк создан. Скачайте зашифрованную резервную копию.");
  });
});
$("wallet-export").addEventListener("submit", event => {
  event.preventDefault();
  if (vault.busy || !vault.exportId) return;
  const password = $("wallet-export-password").value;
  $("wallet-export-password").value = "";
  walletMutation("wallets/export", {id: vault.exportId, password}, "Готовим зашифрованную копию…", data => {
    saveEncryptedBackup(data);
    resetWalletForms();
    text($("wallets-message"), "Зашифрованный файл передан браузеру. Сохраните его и пароль отдельно.");
  });
});

$("discovery-dialog").addEventListener("close", () => {
  if ($("discovery-dialog").open) return;
  setup.seq++;
  setup.busy = false;
  setup.selected.clear();
  setAuxBusy("discovery-dialog", false);
  text($("discovery-message"), "");
});
$("discovery-refresh").addEventListener("click", () => loadDiscovery());
$("discovery-manual").addEventListener("click", () => {
  $("discovery-dialog").close();
  text($("copy-message"), "");
  $("setup-dialog").showModal();
});
$("discovery-add").addEventListener("submit", event => {
  event.preventDefault();
  if (setup.selected.size && setup.selected.size <= GROUP_LIMIT && !setup.busy && state.online) discoveryMutation("setup/workers", {devices: [...setup.selected]},
    "Добавляем воркеры…", "Воркеры добавлены. Майнинг не запущен.");
});
$("group-select").addEventListener("click", () => {
  const rows = inList(state.status?.workers);
  state.selected.clear();
  for (const worker of rows.slice(0, GROUP_LIMIT)) state.selected.add(worker.id);
  workerCards(rows);
  renderGroup(rows);
});
$("group-clear").addEventListener("click", () => {
  state.selected.clear();
  const rows = inList(state.status?.workers);
  workerCards(rows);
  renderGroup(rows);
});
for (const action of ["start", "stop"]) $(`group-${action}`).addEventListener("click", () => groupAction(action));
$("release-check").addEventListener("click", checkReleases);
$("fleet-refresh").addEventListener("click", loadFleet);
$("fleet-hide-invite").addEventListener("click", clearFleetInvite);
$("fleet-copy").addEventListener("click", () => {
  const command = $("fleet-invite-command").textContent;
  if (command) copySensitive(command, $("fleet-message"), () => !$("fleet-invite").hidden);
});
$("fleet-enroll").addEventListener("submit", event => {
  event.preventDefault();
  const label = $("fleet-label").value.trim();
  if (label) changeFleet("enroll", {label});
});
$("fleet-revoke-form").addEventListener("submit", event => {
  event.preventDefault();
  const id = fleet.revokeId;
  fleet.revokeId = null;
  $("fleet-revoke-dialog").close();
  if (id) changeFleet("revoke", {id});
});

$("period").addEventListener("change", () => {
  state.historyError = "";
  drawHistory();
  refreshTimeline();
});
$("metric").addEventListener("change", drawHistory);
$("event-filter").addEventListener("change", renderEvents);
$("retry").addEventListener("click", refreshStatus);
if ("ResizeObserver" in window) new ResizeObserver(drawHistory).observe($("chart-wrap"));
else window.addEventListener("resize", drawHistory);

let lastWake = 0;
function wake() {
  if (document.hidden || Date.now() - lastWake < 800) return;
  lastWake = Date.now();
  refreshStatus();
  refreshTimeline();
  loadFleet();
}
document.addEventListener("visibilitychange", wake);
window.addEventListener("focus", wake);
window.addEventListener("online", wake);
window.addEventListener("offline", () => {
  if (state.mode === "app") {
    state.online = false;
    connection("Нет связи", "warn");
    renderStatus();
    if (state.view === "nodes") {
      fleet.fresh = false;
      renderFleet();
    }
  }
});
setInterval(() => { if (!document.hidden) refreshStatus(); }, 4000);
setInterval(() => { if (!document.hidden) refreshTimeline(); }, 14000);
setInterval(() => { if (!document.hidden) loadFleet(); }, 5000);
setInterval(() => {
  if (!document.hidden && $("discovery-dialog").open && inList(setup.data?.miners).some(miner => miner.state === "downloading")) loadDiscovery();
}, 3000);
refreshAuthInfo();
refreshStatus();
