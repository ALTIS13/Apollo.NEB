"use strict";

const $ = id => document.getElementById(id);
const all = selector => [...document.querySelectorAll(selector)];
const n = (value, digits = 1) => Number.isFinite(value)
  ? value.toLocaleString("ru-RU", {maximumFractionDigits: digits}) : "—";
const fmt = (value, unit, digits) => Number.isFinite(value) ? `${n(value, digits)} ${unit}` : "—";
const temperature = value => Number.isFinite(value) ? fmt(Math.floor(value), "°C", 0) : "—";
const showTemperature = (element, value) => showLiveReading(element, temperature(value),
  Number.isFinite(value) ? Math.floor(value) : null);
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
  ["host-reboot", "Перезагрузка узла"], ["service-start", "Запуск панели"],
]);
const DEVFEE = {
  idle: ["Ожидает запуска", "neutral", "DevFee начнёт учитывать время после запуска воркера."],
  counting: ["Учёт времени", "good", "Сейчас добыча идёт на адрес оператора."],
  active: ["В работе", "good", ""],
  paused: ["На паузе", "neutral", "Владелец Apollo.NEB поставил DevFee на паузу."],
  unavailable: ["Недоступна", "warn", "Не удалось подтвердить работу DevFee. Проверьте журнал событий."],
  offline: ["Нет связи", "warn", "Нет свежих данных о воркерах."],
};
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
  eventsKind: null, focusEvent: null,
  timelineSeq: 0, online: false, actions: new Set(),
  authTotp: null, authInfoSeq: 0, authMutation: false, authEpoch: 0,
  selected: new Set(), groupBusy: false,
};
const cards = new Map();
const vault = {seq: 0, busy: false, data: null, exportId: null};
const setup = {seq: 0, busy: false, data: null, selected: new Set()};
const totp = {seq: 0, busy: false, timer: null, expiresAt: 0};
const fleet = {generation: 0, loading: false, busy: false, fresh: false, nodes: null, key: "", selected: new Map(), revokeId: null, inviteTimer: null};
const workerLog = {id: null, request: null, key: ""};
const network = {data: null, request: null, error: ""};
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
  state.eventsKind = state.focusEvent = null;
  Object.assign(workerLog, {id: null, request: null, key: ""});
  $("worker-log-entries").replaceChildren();
  for (const id of ["live-rate", "live-watts"]) showLiveReading($(id), "—", null);
  Object.assign(network, {data: null, request: null, error: ""});
  renderNetwork();
  state.historyError = state.eventsError = state.chartKey = state.eventsKey = state.incidentsKey = "";
  state.online = state.pollAgain = false;
  state.actions.clear();
  state.selected.clear();
  state.groupBusy = state.authMutation = false;
  resetAuxiliary();
  cards.clear();
  clearHistoryPlot($("chart-svg"));
  for (const id of ["worker-grid", "event-list", "incident-list", "live-thermal"]) $(id).replaceChildren();
  for (const id of ["profile-form", "limits-form", "login-form"]) $(id).reset();
  for (const dialog of all("dialog[open]")) dialog.close();
  $("boot").hidden = $("app").hidden = $("logout").hidden = $("devfee").hidden = true;
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
      const log = el.querySelector("[data-worker-log]");
      select.addEventListener("change", () => {
        if (select.checked) state.selected.add(worker.id);
        else state.selected.delete(worker.id);
        renderGroup(inList(state.status?.workers));
      });
      start.addEventListener("click", () => workerAction(worker.id, "start"));
      stop.addEventListener("click", () => workerAction(worker.id, "stop"));
      log.addEventListener("click", () => openWorkerLog(worker.id));
      entry = {el, fields, start, stop, select, log};
      cards.set(worker.id, entry);
      $("worker-grid").append(el);
    }
    const {el, fields, start, stop, select, log} = entry;
    const [label, tone] = STATES.get(worker.state) || ["Неизвестно", "neutral"];
    el.dataset.tone = tone;
    text(fields.label, worker.label || worker.id);
    select.setAttribute("aria-label", "Выбрать " + (worker.label || worker.id));
    log.setAttribute("aria-label", "Журнал: " + (worker.label || worker.id));
    select.checked = state.selected.has(worker.id);
    select.disabled = state.groupBusy || state.actions.has(worker.id);
    text(fields.device, [worker.device, worker.adapter].filter(Boolean).join(" · ") || "Устройство не указано");
    text(fields.state, state.actions.has(worker.id) ? "Выполняется…" : label);
    showLiveReading(fields.rate, rate(worker.rate_hs), worker.rate_hs);
    showLiveReading(fields.watts, fmt(worker.watts, "Вт"), worker.watts);
    for (const name of TEMPERATURES) {
      showTemperature(fields[name], worker[name]);
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
  const liveRate = !active.length && workers.length
    && workers.every(worker => !["starting", "stopping", "unavailable"].includes(worker.state)) ? 0 : sum(active, "rate_hs");
  const livePower = sum(devices, "watts");
  showLiveReading($("live-rate"), rate(liveRate), liveRate);
  showLiveReading($("live-watts"), fmt(livePower, "Вт"), livePower);
  text($("live-shares"), `${n(sum(active, "accepted"), 0)} / ${n(sum(active, "rejected"), 0)}`);
  $("first-run").hidden = Boolean(workers.length && profile?.wallet);
  text($("step-profile-title"), profile?.wallet ? "Профиль пула сохранён" : "Задайте профиль пула");
  text($("step-profile-action"), profile?.wallet ? "Изменить профиль" : "Настроить профиль");
  text($("step-workers-title"), workers.length ? "Воркеры добавлены" : "Добавьте воркеры");
  all("[data-profile]").forEach(button => button.classList.toggle("primary", !profile?.wallet));
  $("first-run").querySelector("[data-discovery]").classList.toggle("primary", Boolean(profile?.wallet && !workers.length));
  const supportPercent = fmt(support?.basis_points / 100, "%", 2);
  const [feeState, feeTone, feeDetails] = !state.online ? DEVFEE.offline : DEVFEE[support?.state] || DEVFEE.unavailable;
  const feeLabel = `DevFee · ${feeState.toLocaleLowerCase("ru")}`;
  $("devfee").hidden = !support?.enabled;
  $("devfee").dataset.tone = feeTone;
  text($("devfee-label"), feeLabel);
  const runtimeShare = `${supportPercent} активного времени майнинга.`;
  text($("setup-support"), support?.enabled ? `${feeLabel}. ${runtimeShare}` : "");
  $("setup-support").hidden = !support?.enabled;
  $("support").hidden = !support?.enabled || !$("first-run").hidden;
  text($("support-summary"), runtimeShare);
  const onSupport = inList(support?.active_workers);
  text($("support-active"), state.online && onSupport.length
    ? `Окно DevFee: ${onSupport.map(id => workers.find(worker => worker.id === id)?.label || id).join(", ")}.`
    : feeDetails);
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
    text($("attention-detail"), `${worker.label || worker.id}: ${METRICS[key][0]} ${temperature(worker[key])} · ${tone === "bad" ? "достигнут предел" : "приближение к пределу"}`);
  }
  renderThermalSensors(devices, limits);
  text($("worker-summary"), workers.length ? `В работе: ${active.length} из ${workers.length}` : "Нет настроенных воркеров.");
  $("workers-empty").hidden = Boolean(workers.length);
  workerCards(workers);
  renderGroup(workers);
  $("profile-open").disabled = !state.online;
  for (const button of all("[data-limits], [data-profile], [data-wallets], [data-discovery], [data-totp]")) button.disabled = !state.online;
}

function renderThermalSensors(devices, limits) {
  const sensors = $("live-thermal");
  if (!sensors.childElementCount) {
    for (const key of TEMPERATURES) {
      const row = create("div");
      row.append(create("dt", METRICS[key][0]), create("dd"), create("small"));
      sensors.append(row);
    }
  }
  for (const [index, key] of TEMPERATURES.entries()) {
    const row = sensors.children[index];
    const values = devices.map(device => device[key]).filter(Number.isFinite);
    const reading = values.length ? Math.max(...values) : null;
    const limit = limits?.[`${key}_limit`];
    const metric = row.querySelector("dd");
    showTemperature(metric, reading);
    metric.dataset.tone = thermalTone(reading, limit);
    text(row.querySelector("small"),
      `Датчики: ${values.length} / ${devices.length} · предел ${Number.isFinite(limit) ? temperature(limit) : "не задан"}`);
  }
}

function renderEvents() {
  const filter = $("event-filter").value;
  const loaded = state.eventsKind === filter && state.events !== null;
  const entries = loaded ? inList(state.events).slice().sort((a, b) => b.time - a.time) : [];
  const key = JSON.stringify([filter, entries]);
  if (state.eventsKey !== key) {
    const list = $("event-list");
    const focused = list.contains(document.activeElement) ? document.activeElement.id : "";
    list.replaceChildren(...entries.map(event => {
      const row = create("li");
      row.id = `event-${event.id}`;
      row.tabIndex = -1;
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
    if (focused && state.focusEvent === null) $(focused)?.focus({preventScroll: true});
    state.eventsKey = key;
  }
  if (loaded && state.focusEvent !== null) {
    $(`event-${state.focusEvent}`)?.focus();
    state.focusEvent = null;
  }
  $("events-empty").hidden = entries.length > 0 && !state.eventsError;
  text($("events-empty"), state.eventsError || (!loaded ? "Загружаем события…" :
    filter ? "Нет событий этого типа." : "Событий пока нет."));
}

function openWorkerLog(id) {
  const worker = inList(state.status?.workers).find(row => row.id === id);
  if (!worker) return;
  Object.assign(workerLog, {id, request: null, key: ""});
  text($("worker-log-name"), worker.label || worker.id);
  text($("worker-log-message"), "Загружаем журнал…");
  $("worker-log-entries").hidden = true;
  $("worker-log-entries").replaceChildren();
  $("worker-log-dialog").showModal();
  refreshWorkerLog();
}

async function refreshWorkerLog() {
  const dialog = $("worker-log-dialog");
  if (state.mode !== "app" || state.authMutation || !dialog.open || !workerLog.id || workerLog.request) return;
  const {id} = workerLog;
  const authEpoch = state.authEpoch;
  const request = workerLog.request = {};
  const current = () => dialog.open && request === workerLog.request && authEpoch === state.authEpoch;
  try {
    const response = await api(`logs?worker=${encodeURIComponent(id)}`);
    if (!current()) return;
    const entries = inList(response.entries);
    const list = $("worker-log-entries");
    const key = JSON.stringify(entries);
    if (key !== workerLog.key) {
      const scroll = list.scrollTop;
      list.replaceChildren(...entries.map(workerLogEntry));
      list.scrollTop = scroll;
      workerLog.key = key;
    }
    list.hidden = !entries.length;
    text($("worker-log-message"), entries.length ? "Последние записи с запуска панели. Обновляются автоматически."
      : "Записей пока нет. События и доступная телеметрия появятся после запуска воркера.");
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) { showLogin(true); return; }
    text($("worker-log-message"), $("worker-log-entries").hidden ? "Журнал временно недоступен."
      : "Журнал не обновился. Показаны последние полученные записи.");
  } finally {
    if (workerLog.request === request) workerLog.request = null;
  }
}

function workerLogEntry(entry) {
  const row = create("li");
  const time = create("time", eventTime(entry.time), "event-time");
  if (Number.isFinite(entry.time)) time.dateTime = new Date(entry.time * 1000).toISOString();
  row.append(time);
  if (entry.type === "telemetry") {
    row.append(create("strong", "Телеметрия", "log-metric"));
    const readings = create("span", undefined, "log-readings");
    if (Number.isFinite(entry.rate_hs)) readings.append(create("span", rate(entry.rate_hs)));
    if (Number.isFinite(entry.accepted)) readings.append(create("span", `Принято ${n(entry.accepted, 0)}`, "log-good"));
    if (Number.isFinite(entry.rejected)) readings.append(create("span", `Отклонено ${n(entry.rejected, 0)}`,
      entry.rejected > 0 ? "log-warn" : ""));
    row.append(readings);
  } else {
    const tone = entry.type === "thermal" || ["failed", "unconfirmed"].includes(entry.status) ? "log-warn"
      : entry.type === "start" ? "log-good" : "";
    row.append(create("strong", EVENTS.get(entry.type) || "Событие", tone),
      create("span", safeLine(entry.detail) || "—"));
  }
  return row;
}

function renderNetwork() {
  const snapshot = network.data;
  const data = snapshot?.data;
  $("network-stats").hidden = !data;
  const hs = data?.hashrate_hs;
  text($("network-height"), n(data?.height, 0));
  text($("network-hashrate"), hs >= 1e12 ? fmt(hs / 1e12, "ТХ/с", 3)
    : hs >= 1e9 ? fmt(hs / 1e9, "ГХ/с", 3) : rate(hs));
  text($("network-reward"), fmt(data?.reward_neb, "NEB", 4));
  const estimate = !network.error && snapshot?.state === "fresh" ? snapshot.estimate_neb_day : null;
  text($("network-estimate"), fmt(estimate, "NEB", 4));
  const last = data ? `Suprnova · ${eventTime(data.observed_at)}.` : "";
  let detail = "Данные Suprnova загружаются по запросу.";
  if (network.error || snapshot?.state === "unavailable") detail = "Данные сети недоступны.";
  else if (snapshot?.state === "loading") detail = "Обновляем данные сети…";
  else if (snapshot?.state === "stale") detail = "Данные сети устарели.";
  else if (snapshot?.state === "fresh") detail = Number.isFinite(estimate)
    ? `Валовая оценка до комиссий при среднем хешрейте ${rate(snapshot.local_rate_hs)} за последние 30 минут.`
    : "Для оценки нужно больше свежих замеров локального хешрейта за последние 30 минут.";
  text($("network-message"), [last, detail].filter(Boolean).join(" "));
  $("network-check").disabled = Boolean(network.request) || snapshot?.state === "loading";
}

async function loadNetwork(refresh = false) {
  if (state.view !== "overview" || state.mode !== "app" || state.authMutation || network.request) return;
  const request = network.request = {};
  const authEpoch = state.authEpoch;
  const current = () => request === network.request && authEpoch === state.authEpoch;
  if (refresh) text($("network-message"), "Обновляем данные сети…");
  $("network-check").disabled = true;
  try {
    const data = refresh ? await api("network/refresh", {}) : await api("network");
    if (!current()) return;
    network.data = data;
    network.error = "";
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) { showLogin(true); return; }
    network.error = "offline";
  } finally {
    if (request === network.request) {
      const active = current();
      network.request = null;
      if (active) renderNetwork();
    }
  }
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
  const period = $("period").value;
  const data = state.history?.range === period ? state.history : null;
  const metric = $("metric").value;
  const [label, unit] = METRICS[metric];
  const status = state.historyError || (data ? "" : "Загружаем историю…");
  const key = JSON.stringify([width, metric, data, status]);
  if (state.chartKey === key) return;
  state.chartKey = key;

  const chart = $("chart-svg");
  chart.setAttribute("aria-label", label + " · " + $("period").selectedOptions[0].textContent);
  text($("history-count"), Number.isFinite(data?.count) ? `Замеров: ${n(data.count, 0)}` : "");
  const help = status && data ? `${status} Показаны последние полученные данные.`
    : "Разрывы линии показывают пропуски измерений. Стрелки на клавиатуре перемещают указатель.";
  text($("chart-caption"), help);
  $("chart-empty").hidden = false;
  text($("chart-empty"), status || "Нет значений за этот период.");
  if (!data || !Number.isFinite(data.from) || !Number.isFinite(data.to) || data.to <= data.from) {
    clearHistoryPlot(chart);
    renderChartEvents([]);
    return;
  }

  const thresholds = TEMPERATURES.includes(metric) ? inList(data.thresholds) : [];
  const segments = thresholds.flatMap((row, i) => {
    const start = Math.max(row.t, data.from);
    const end = thresholds[i + 1]?.t ?? data.to;
    const limit = row[`${metric}_limit`];
    return Number.isFinite(limit) && start < end ? [{start, end, limit}] : [];
  });
  if (segments.length) text($("chart-caption"), `${help} Пунктир показывает действовавшие пороги.`);
  const markers = inList(data.markers ?? data.incidents).filter(event => Number.isFinite(event.time)
    && event.time >= data.from && event.time <= data.to).sort((a, b) => a.time - b.time).slice(-150);
  const hasValues = renderHistoryPlot({
    chart, width, data, metric, label, unit, segments, markers,
    formatNumber: n, formatTime: eventTime,
    formatTick: stamp => period.endsWith("d") ? localTime(stamp, {day: "2-digit", month: "2-digit"}) : clock(stamp),
    onEvent: openChartEvent,
  });
  renderChartEvents(markers);
  $("chart-empty").hidden = hasValues || Boolean(markers.length);
}

function renderChartEvents(markers) {
  const latest = markers.slice(-4).reverse();
  const key = JSON.stringify([latest, markers.length > 4]);
  if (state.incidentsKey === key) return;
  state.incidentsKey = key;
  $("chart-incidents").hidden = !latest.length;
  const buttons = latest.map(event => {
    const action = event.type === "exit" ? "Аварийное завершение" : EVENTS.get(event.type) || "Событие";
    const button = create("button", `${eventTime(event.time)} · ${action}`, "incident-link");
    button.type = "button";
    button.dataset.type = event.type;
    button.title = [event.worker, safeLine(event.detail)].filter(Boolean).join(" · ");
    button.addEventListener("click", () => openChartEvent(event));
    return button;
  });
  if (markers.length > 4) {
    const button = create("button", "Журнал событий", "incident-link");
    button.type = "button";
    button.addEventListener("click", () => openChartEvent());
    buttons.push(button);
  }
  $("incident-list").replaceChildren(...buttons);
}

function openChartEvent(event = null) {
  $("event-filter").value = event?.type || "";
  syncStyledSelect($("event-filter"));
  state.focusEvent = event?.id ?? null;
  state.eventsError = "";
  setView("events", true);
}

async function refreshTimeline() {
  if (state.mode !== "app" || !["overview", "events"].includes(state.view)) return;
  const seq = ++state.timelineSeq;
  const generation = state.generation;
  const authEpoch = state.authEpoch;
  const view = state.view;
  const period = $("period").value;
  const kind = $("event-filter").value;
  const current = () => generation === state.generation && authEpoch === state.authEpoch && state.mode === "app" && seq === state.timelineSeq
    && view === state.view && (view === "overview" ? period === $("period").value : kind === $("event-filter").value);
  try {
    if (view === "overview") {
      loadNetwork();
      const data = await api(`history?range=${encodeURIComponent(period)}`);
      if (!current()) return;
      state.history = data;
      state.historyError = "";
    } else {
      const data = await api(kind ? `events?kind=${encodeURIComponent(kind)}` : "events");
      if (!current()) return;
      state.events = data.events;
      state.eventsKind = kind;
      state.eventsError = "";
    }
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) { if (!state.authMutation) showLogin(true); return; }
    if (view === "overview") state.historyError = "История временно недоступна.";
    else state.eventsError = state.events && state.eventsKind === kind
      ? "Журнал недоступен. Показаны последние полученные данные." : "Журнал временно недоступен.";
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
  $("totp-recovery-download").hidden = true;
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
    $("totp-recovery-download").hidden = !codes.length;
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
        ($("totp-recovery-download").hidden ? $("totp-dialog").querySelector("[data-close]") : $("totp-recovery-download")).focus();
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
    row.append(label, create("p", reason || `Ядро ${temperature(device.edge)} · hotspot ${temperature(device.hot)}`, "subline"));
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
  syncStyledSelect($("pool"));
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
$("totp-recovery-download").addEventListener("click", () => {
  if ($("totp-dialog").open && !$("totp-recovery").hidden) downloadRecoveryCodes($("totp-codes"));
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
$("event-filter").addEventListener("change", () => {
  state.focusEvent = null;
  state.eventsError = "";
  renderEvents();
  refreshTimeline();
});
$("network-check").addEventListener("click", () => loadNetwork(true));
$("worker-log-dialog").addEventListener("close", () => {
  if (!$("worker-log-dialog").open) {
    Object.assign(workerLog, {id: null, request: null, key: ""});
    $("worker-log-entries").replaceChildren();
  }
});
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
  refreshWorkerLog();
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
setInterval(() => { if (!document.hidden) refreshWorkerLog(); }, 3000);
setInterval(() => {
  if (!document.hidden && $("discovery-dialog").open && inList(setup.data?.miners).some(miner => miner.state === "downloading")) loadDiscovery();
}, 3000);
refreshAuthInfo();
refreshStatus();
