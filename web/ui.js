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
const PAGES = {overview: "Обзор фермы", workers: "Воркеры", events: "События"};
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
};
const cards = new Map();

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

async function api(path, data) {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), 12000);
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
  cards.clear();
  for (const id of ["worker-grid", "event-list", "chart-svg", "incident-list", "live-thermal"]) $(id).replaceChildren();
  for (const id of ["profile-form", "limits-form", "login-form"]) $(id).reset();
  for (const dialog of all("dialog[open]")) dialog.close();
  $("boot").hidden = $("app").hidden = $("logout").hidden = true;
  $("login").hidden = false;
  text($("login-message"), expired ? "Сеанс завершён. Войдите снова." : "");
  connection("Вход в панель", "neutral");
  notice("");
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
      start.addEventListener("click", () => workerAction(worker.id, "start"));
      stop.addEventListener("click", () => workerAction(worker.id, "stop"));
      entry = {el, fields, start, stop};
      cards.set(worker.id, entry);
      $("worker-grid").append(el);
    }
    const {el, fields, start, stop} = entry;
    const [label, tone] = STATES.get(worker.state) || ["Неизвестно", "neutral"];
    el.dataset.tone = tone;
    text(fields.label, worker.label || worker.id);
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
  $("first-run").querySelector("[data-setup]").classList.toggle("primary", Boolean(profile?.wallet && !workers.length));
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
  $("profile-open").disabled = !state.online;
  for (const button of all("[data-limits], [data-profile]")) button.disabled = !state.online;
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
  if (state.mode !== "app" || state.view === "workers") return;
  const seq = ++state.timelineSeq;
  const generation = state.generation;
  const view = state.view;
  const period = $("period").value;
  const current = () => generation === state.generation && state.mode === "app" && seq === state.timelineSeq
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
    if (error.status === 401) return showLogin(true);
    if (view === "overview") state.historyError = "История временно недоступна.";
    else state.eventsError = state.events ? "Журнал недоступен. Показаны последние полученные данные." : "Журнал временно недоступен.";
  }
  if (view === "overview") drawHistory();
  else renderEvents();
}

function setView(view, focus = false) {
  if (!Object.hasOwn(PAGES, view)) return;
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
  else refreshTimeline();
}

async function refreshStatus({fresh = false} = {}) {
  if (state.mode === "login") return;
  if (state.poll) {
    if (!fresh) return state.poll;
    if (state.pollGeneration === state.generation) state.pollAgain = true;
    await state.poll;
    if (state.poll) return state.poll;
  }
  const generation = state.generation;
  state.pollGeneration = generation;
  const promise = (async () => {
    try {
      const data = await api("status");
      if (generation !== state.generation) return;
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
      if (generation !== state.generation) return;
      if (error.status === 401) return showLogin(state.mode === "app");
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

function errorForAction(error, fallback) {
  if (error.status === 401) showLogin(true);
  else notice(errorText(error, fallback), "warn");
}

async function workerAction(id, action) {
  const worker = inList(state.status?.workers).find(row => row.id === id);
  if (!worker || state.actions.has(id) || !state.online
    || (action === "start" ? !canStart(worker) : !isRunning(worker))) return;
  const generation = state.generation;
  state.actions.add(id);
  notice("");
  renderStatus();
  try {
    await api(action, {id});
    if (generation !== state.generation) return;
    notice(action === "start" ? "Команда запуска принята." : "Команда остановки принята.");
  } catch (error) {
    if (generation === state.generation) errorForAction(error, "Не удалось выполнить команду.");
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

function openProfile() {
  if (!state.online || !state.status || $("profile-dialog").open) return;
  const {profile, pools} = state.status;
  $("wallet").value = profile?.wallet || "";
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
    await api("login", {password: pass.value, code: code.value});
    pass.value = code.value = "";
    state.generation++;
    state.mode = "boot";
    $("login").hidden = true;
    $("boot").hidden = false;
    text($("boot-message"), "Ожидаем ответ локальной службы…");
    await refreshStatus({fresh: true});
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
for (const button of all("[data-close]")) button.addEventListener("click", () => button.closest("dialog").close());
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
}
document.addEventListener("visibilitychange", wake);
window.addEventListener("focus", wake);
window.addEventListener("online", wake);
window.addEventListener("offline", () => {
  if (state.mode === "app") {
    state.online = false;
    connection("Нет связи", "warn");
    renderStatus();
  }
});
setInterval(() => { if (!document.hidden) refreshStatus(); }, 4000);
setInterval(() => { if (!document.hidden) refreshTimeline(); }, 14000);
refreshStatus();
