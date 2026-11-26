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
const safeLine = value => typeof value === "string" ? value.slice(0, 350) : "";

const state = {
  mode: "boot", view: "overview", status: null, generation: 0, historySource: "local", atlasKey: "",
  history: null, historyError: "", events: null, eventsError: "",
  eventsKey: "", chartKey: "", incidentsKey: "", poll: null, pollAgain: false,
  eventsKind: null, focusEvent: null,
  timelineSeq: 0, online: false, actions: new Set(),
  authTotp: null, authInfoSeq: 0, authMutation: false, authEpoch: 0,
  selected: new Set(), groupBusy: false,
};
const setPublicOverview = setupPublicOverview();
const cards = new Map();
const setup = {seq: 0, busy: false, data: null, selected: new Set()};
const totp = {seq: 0, busy: false, timer: null, expiresAt: 0};
const fleet = {generation: 0, loading: false, busy: false, fresh: false, nodes: null, key: "", installTargets: [], installKey: "",
  selected: new Map(), revokeId: null, inviteTimer: null};
const gpuPower = {target: null, request: null};
const workerLog = {id: null, request: null, key: ""};
const network = {data: null, request: null, error: ""};
const releases = {seq: 0, verifySeq: 0, pending: "", target: null, info: null, versions: [], candidate: null,
  message: "", tone: "neutral", confirming: false, queued: false};
const RELEASE_HOME = "https://github.com/ALTIS13/Apollo.NEB/releases";
const RELEASE_CHANNELS = new Map([["stable", "Основной"], ["testing", "Тестовый"]]);
const RELEASE_PLATFORMS = new Map([["linux", "Linux"], ["windows", "Windows x64"],
  ["windows-desktop", "Apollo.NEB Desktop · Windows x64"], ["unsupported", "Не поддерживается"]]);

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
  text($("atlas-event-details"), "");
  $("history-source").value = "local";
  applyHistorySource();
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
  $("public-overview").hidden = false;
  text($("login-message"), expired ? "Сеанс завершён. Войдите снова." : "");
  connection("Режим просмотра", "neutral");
  notice("");
  setPublicOverview(true);
}

function canStart(worker) {
  return state.online && !state.actions.has(worker.id)
    && !["running", "mining", "starting", "stopping", "unavailable"].includes(worker.state)
    && Boolean(state.status?.profile?.wallet_configured);
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
    start.title = state.status?.profile?.wallet_configured ? "" : "Сначала задайте профиль пула";
    stop.disabled = !state.online || state.actions.has(worker.id) || !isRunning(worker);
  });
}

function thermalTone(value, limit) {
  if (!Number.isFinite(value) || !Number.isFinite(limit)) return "neutral";
  return value >= limit ? "bad" : value >= limit - 5 ? "warn" : "neutral";
}

function hasCapability(key) {
  return state.status?.capabilities?.[key] !== false;
}

function renderStatus() {
  const {workers: rawWorkers, profile, limits, support, overview, atlas} = state.status || {};
  const localWorkers = hasCapability("local_workers");
  const workers = localWorkers ? inList(rawWorkers) : [];
  $("controller-note").hidden = localWorkers;
  for (const control of all("[data-discovery], [data-limits], [data-profile], #profile-open, [data-view='workers']")) {
    control.hidden = !localWorkers;
  }
  if (!localWorkers && state.view === "workers") setView("nodes");
  const active = workers.filter(isRunning);
  const devices = workers.filter(worker => worker.device);
  const coverage = overview?.nodes;
  $("overview-coverage").hidden = !coverage?.total;
  const atlasCount = overview?.atlas_gpus;
  const atlasCoverage = atlasCount ? ` · Atlas ${n(atlasCount.active, 0)} / ${n(atlasCount.total, 0)} GPU` : "";
  text($("overview-coverage"), coverage?.total ? `Ноды на связи · ${n(coverage.online, 0)} из ${n(coverage.total, 0)}${atlasCoverage}` : "");
  text($("live-workers"), overview?.workers ? `${n(overview.workers.running, 0)} / ${n(overview.workers.total, 0)}` : "—");
  showLiveReading($("live-rate"), rate(overview?.hashrate_hs), overview?.hashrate_hs);
  showLiveReading($("live-watts"), fmt(overview?.watts, "Вт"), overview?.watts);
  text($("live-shares"), `${n(overview?.shares?.accepted, 0)} / ${n(overview?.shares?.rejected, 0)}`);
  $("first-run").hidden = !localWorkers || Boolean(workers.length && profile?.wallet_configured);
  text($("step-profile-title"), profile?.wallet_configured ? "Профиль пула сохранён" : "Задайте профиль пула");
  text($("step-profile-action"), profile?.wallet_configured ? "Изменить профиль" : "Настроить профиль");
  text($("step-workers-title"), workers.length ? "Воркеры добавлены" : "Добавьте воркеры");
  all("[data-profile]").forEach(button => button.classList.toggle("primary", !profile?.wallet_configured));
  $("first-run").querySelector("[data-discovery]").classList.toggle("primary", Boolean(profile?.wallet_configured && !workers.length));
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
  renderThermalSensors(devices, limits, overview?.temperatures);
  applyHistorySource();
  renderAtlasNode(atlas);
  text($("worker-summary"), workers.length ? `В работе: ${active.length} из ${workers.length}` : "Нет настроенных воркеров.");
  $("workers-empty").hidden = !localWorkers || Boolean(workers.length);
  workerCards(workers);
  renderGroup(workers);
  $("profile-open").disabled = !state.online;
  for (const button of all("[data-limits], [data-profile], [data-discovery], [data-totp]")) button.disabled = !state.online;
  if (state.view === "nodes") renderFleet();
}

function renderThermalSensors(devices, limits, farm) {
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
    const local = values.length ? Math.max(...values) : null;
    const reading = farm ? farm[key] : local;
    const limit = limits?.[`${key}_limit`];
    const metric = row.querySelector("dd");
    showTemperature(metric, reading);
    metric.dataset.tone = thermalTone(local, limit);
    text(row.querySelector("small"),
      devices.length ? `На этой ноде: ${values.length} / ${devices.length} датчиков · предел ${Number.isFinite(limit) ? temperature(limit) : "не задан"}` : "");
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
    ? `Оценка до комиссий по свежему хешрейту фермы ${rate(snapshot.farm_rate_hs)}.`
    : "Для оценки нужны свежие значения хешрейта воркеров.";
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

function applyHistorySource() {
  const selector = $("history-source");
  const farmAvailable = state.status?.capabilities?.farm_history === true;
  const atlasAvailable = Boolean(state.status?.atlas);
  $("history-source-row").hidden = !farmAvailable && !atlasAvailable;
  for (const [id, available] of [["history-farm-option", farmAvailable], ["history-atlas-option", atlasAvailable]]) {
    const option = $(id);
    if (option.hidden === available) option.hidden = !available;
  }
  if (selector.selectedOptions[0]?.hidden) selector.value = "local";
  const source = selector.value;
  if (state.historySource === source) return;
  state.historySource = source;
  state.history = null;
  state.historyError = "";
  const atlas = source === "atlas";
  const farm = source === "farm";
  const longest = $("history-max-period");
  longest.value = atlas ? "30d" : "31d";
  text(longest, atlas ? "30 дней" : "31 день");
  text($("history-active-metric"), atlas ? "Активные GPU" : "Активные воркеры");
  text($("history-edge-metric"), farm ? "Ядро · по ферме" : "Ядро · средняя");
  text($("history-memory-metric"), farm ? "Память · по ферме" : "Память · средняя");
  text($("history-title"), atlas ? "История Atlas" : farm ? "История фермы" : "Локальная история");
  for (const id of ["history-source", "metric", "period"]) syncStyledSelect($(id));
}

function drawHistory() {
  if (state.view !== "overview") return;
  const width = $("chart-wrap").clientWidth;
  if (!width) return;
  const period = $("period").value;
  const data = state.history?.range === period ? state.history : null;
  const metric = $("metric").value;
  const [, unit] = METRICS[metric];
  const label = $("metric").selectedOptions[0].textContent;
  const status = state.historyError || (data ? "" : "Загружаем историю…");
  const key = JSON.stringify([width, metric, state.historySource, data, status]);
  if (state.chartKey === key) return;
  state.chartKey = key;

  const chart = $("chart-svg");
  chart.setAttribute("aria-label", label + " · " + $("period").selectedOptions[0].textContent);
  text($("history-count"), Number.isFinite(data?.count) ? `Замеров: ${n(data.count, 0)}` : "");
  const help = status && data ? `${status} Показаны последние полученные данные.`
    : state.historySource === "farm" ? "Замер добавляется, когда все ноды на связи. Стрелки перемещают указатель."
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
  const more = state.historySource === "local" && markers.length > 4;
  const key = JSON.stringify([latest, more]);
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
  if (more) {
    const button = create("button", "Журнал событий", "incident-link");
    button.type = "button";
    button.addEventListener("click", () => openChartEvent());
    buttons.push(button);
  }
  $("incident-list").replaceChildren(...buttons);
}

function openChartEvent(event = null) {
  if (state.historySource === "atlas" && event) {
    text($("atlas-event-details"), [eventTime(event.time), safeLine(event.worker), safeLine(event.detail)].filter(Boolean).join(" · "));
    $("atlas-event-dialog").showModal();
    return;
  }
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
  const source = state.historySource;
  const kind = $("event-filter").value;
  const current = () => generation === state.generation && authEpoch === state.authEpoch && state.mode === "app" && seq === state.timelineSeq
    && view === state.view && (view === "overview" ? period === $("period").value && source === state.historySource
      : kind === $("event-filter").value);
  try {
    if (view === "overview") {
      loadNetwork();
      const data = await api("history?" + new URLSearchParams({range: period, source}));
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
  if (view === "workers" && !hasCapability("local_workers")) view = "nodes";
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
      $("boot").hidden = $("public-overview").hidden = true;
      setPublicOverview(false);
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
  setup.seq++;
  setup.busy = false;
  setup.data = null;
  setup.selected.clear();
  setAuxBusy("discovery-dialog", false);
  for (const id of ["miner-list", "device-list", "group-results"]) $(id).replaceChildren();
  for (const id of ["discovery-message", "discovery-status", "group-message"]) text($(id), "");
  $("group-results").hidden = true;
  resetReleases();
  clearFleet();
}

function actionButton(label, handler, css) {
  const btn = create("button", label, css);
  btn.type = "button";
  btn.addEventListener("click", handler);
  return btn;
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
  if (!state.online || !hasCapability("local_workers") || $("discovery-dialog").open) return;
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

function releaseCandidate(data) {
  const tag = safeLine(data.latest);
  const version = /^v?(\d{1,4}\.\d{1,4}\.\d{1,4})(?:-rc\.\d{1,4})?$/.exec(tag);
  const asset = data.asset;
  const desktop = data.platform === "windows-desktop";
  const maximum = (desktop ? 160 : 20) * 1024 ** 2;
  if (data.state !== "available" || !version || !asset
      || asset.url !== `${RELEASE_HOME}/download/${tag}/${asset.name}` || !/^[a-f0-9]{64}$/.test(asset.sha256)
      || !Number.isSafeInteger(asset.size) || asset.size <= 0 || asset.size > maximum) return null;
  const prefix = `apollo-neb-${version[1].split(".").map(Number).join(".")}`;
  const supportedNames = data.platform === "linux"
    ? [`${prefix}-linux.tar.gz`, `${prefix}-linux-x64-kernel-6.6.tar.gz`, `${prefix}-linux-x64-kernel-6.12.tar.gz`]
    : desktop ? [`apollo-neb-desktop-${tag.slice(1)}-windows-x64.zip`]
      : data.platform === "windows" ? [`${prefix}-windows-x64.zip`] : [];
  if (!supportedNames.includes(asset.name)) return null;
  return {
    asset, tag, notes: typeof data.notes === "string" ? data.notes.slice(0, 6000) : "",
    date: typeof data.published_at === "string" ? new Date(data.published_at) : null,
    page: data.release_url === `${RELEASE_HOME}/tag/${tag}` ? data.release_url : "",
    command: data.can_apply === true && data.platform === "linux" ? `apollo-neb upgrade --apply --version ${tag}` : "",
  };
}

function releaseVerification(message = "", tone = "neutral") {
  $("release-verify-message").dataset.tone = tone;
  text($("release-verify-message"), message);
}

function setReleaseCandidate(candidate) {
  releases.candidate = candidate;
  releases.confirming = false;
  releases.verifySeq++;
  $("release-file").value = "";
  releaseVerification();
  for (const id of ["release-notes", "release-verify", "release-guide"]) $(id).open = false;
}

function setReleaseVersions(versions) {
  releases.versions = versions;
  const select = $("release-version");
  select.replaceChildren(...versions.map(release => new Option(release.tag, release.tag)));
  setReleaseCandidate(versions[0] || null);
  syncStyledSelect(select);
}

function clearReleaseCandidate() {
  setReleaseVersions([]);
}

function renderReleases() {
  const {info, pending, target, candidate, tone, message, confirming, queued} = releases;
  const firstInstall = target?.kind === "install";
  const selection = RELEASE_CHANNELS.get(info?.channel);
  const label = selection === "Тестовый" ? "Тестовый канал" : "Основной канал";
  if (!target) text($("release-summary"), info ? `${info.current} · ${label}` : "Канал и версия панели");
  text($("release-current-label"), firstInstall ? "Подготовка" : "Установлена");
  text($("release-current"), info?.current || "—");
  text($("release-latest"), releases.versions[0]?.tag || "—");
  const platform = RELEASE_PLATFORMS.get(info?.platform) || "—";
  text($("release-platform"), info?.kernel ? `${platform} · ядро ${info.kernel}` : platform);
  text($("release-title"), target ? `${firstInstall ? "Установка" : "Обновления"} · ${target.label}` : "Обновления");
  text($("release-channel-help"), target ? "Выбор канала применяется к этому списку." : "Тестовый канал включает предварительные выпуски.");
  $("release-testing").checked = info?.channel === "testing";
  $("release-stable").checked = info?.channel === "stable";
  $("release-channels").disabled = !info || Boolean(pending);
  $("release-check").disabled = Boolean(pending);
  $("release-check").classList.toggle("primary", !candidate || !target);
  text($("release-check"), info ? (firstInstall ? "Найти выпуски" : "Проверить обновления") : "Повторить загрузку");
  $("release-status").dataset.tone = pending ? "neutral" : tone;
  text($("release-status"), pending || message);
  $("release-version-row").hidden = !releases.versions.length || Boolean(pending);
  $("release-version").disabled = Boolean(pending) || !releases.versions.length;
  syncStyledSelect($("release-version"));
  $("release-candidate").hidden = !candidate || Boolean(pending);
  for (const [id, url] of [["release-download", candidate?.asset.url], ["release-link", candidate?.page]]) {
    const link = $(id);
    link.hidden = !url;
    if (url) link.href = url;
    else link.removeAttribute("href");
  }
  $("release-guide").hidden = !candidate?.command;
  $("release-manual").hidden = !candidate || Boolean(candidate.command) || firstInstall;
  $("release-rollback").hidden = firstInstall;
  text($("release-manual"), target ? "Архив подобран для системы этой ноды." : "Сверьте контрольную сумму и следуйте инструкции на странице релиза.");
  $("release-notes").hidden = !candidate?.notes;
  text($("release-notes-text"), candidate?.notes || "");
  text($("release-filename"), candidate?.asset.name || "");
  $("release-size-row").hidden = !candidate;
  text($("release-size"), candidate ? fmt(candidate.asset.size, "байт", 0) : "");
  const hasDate = Number.isFinite(candidate?.date?.getTime());
  $("release-date-row").hidden = !hasDate;
  text($("release-date"), hasDate ? candidate.date.toLocaleDateString("ru-RU") : "");
  text($("release-digest"), candidate?.asset.sha256 || "");
  text($("release-command"), candidate?.command || "");
  $("release-file-row").hidden = !candidate || !window.crypto?.subtle;
  const canQueue = Boolean(target && candidate && info?.canQueue && !queued && !pending);
  $("release-apply").hidden = !canQueue || confirming;
  $("release-confirm").hidden = !canQueue || !confirming;
  text($("release-confirm-text"), canQueue && confirming
    ? `Установить ${candidate.tag} на «${target.label}»? ${firstInstall
      ? "После установки нода появится в списке. Майнинг запускается по вашей команде."
      : "На время обновления панель этой ноды перезапустится."}` : "");
}

function resetReleases() {
  releases.seq++;
  clearReleaseCandidate();
  $("release-rollback").open = false;
  Object.assign(releases, {target: null, info: null, pending: "", tone: "neutral", confirming: false, queued: false,
    message: "Загружаем настройки обновлений…"});
  renderReleases();
}

async function loadReleases({check = false, channel = null} = {}) {
  if (releases.pending) return;
  const seq = ++releases.seq;
  const focus = document.activeElement;
  const previous = releases.info;
  const target = releases.target;
  if (check || channel) clearReleaseCandidate();
  releases.pending = channel ? "Сохраняем канал…" : check ? "Проверяем обновления…" : "Загружаем настройки…";
  renderReleases();
  try {
    if (channel && !target) {
      const saved = await api("releases/channel", {channel});
      if (seq !== releases.seq) return;
      if (saved.channel !== channel) throw new Error();
    }
    let route = check ? "releases" : "releases?check=0";
    if (target) {
      const install = target.kind === "install";
      const params = new URLSearchParams({[install ? "target" : "node"]: target.id});
      if (channel || previous?.channel) params.set("channel", channel || previous.channel);
      if (!check) params.set("check", "0");
      route = (install ? "fleet/install/releases?" : "fleet/releases?") + params;
    }
    const data = await api(route, undefined, 18000);
    if (seq !== releases.seq) return;
    if (!RELEASE_CHANNELS.has(data.channel) || !RELEASE_PLATFORMS.has(data.platform) || !safeLine(data.current)) {
      throw new Error();
    }
    releases.info = {channel: data.channel, platform: data.platform, current: safeLine(data.current),
      kernel: ["6.6", "6.12"].includes(data.kernel) ? data.kernel : null, canQueue: data.can_queue === true};
    if (previous?.channel !== data.channel || previous?.current !== data.current || previous?.platform !== data.platform) {
      clearReleaseCandidate();
    }
    if (check) {
      const choices = Array.isArray(data.versions) ? data.versions.map(entry => releaseCandidate({
        ...data, ...entry, latest: entry?.tag,
      })) : [releaseCandidate(data)];
      setReleaseVersions(choices.filter(Boolean));
      releases.tone = "good";
      if (releases.candidate) releases.message = target?.kind === "install" ? "Выберите выпуск для установки." : "Доступно обновление.";
      else if (data.state === "current") releases.message = "В этом канале нет совместимого выпуска.";
      else {
        releases.tone = "warn";
        releases.message = safeLine(data.message) || "Не удалось подтвердить доступный архив.";
      }
    } else if (!releases.candidate) {
      releases.message = target?.kind === "install" ? "Выберите канал или загрузите выпуски." : "Нажмите «Проверить обновления».";
      releases.tone = "neutral";
    }
  } catch (error) {
    if (seq !== releases.seq) return;
    if (error.status === 401) return showLogin(true);
    clearReleaseCandidate();
    if (!check || target) releases.info = null;
    releases.message = target && error.status === 409 ? errorText(error, "Нужен свежий отчёт ноды о версии и системе.")
      : channel && !target ? "Изменение канала не подтверждено. Загрузите настройки снова."
      : check ? "Проверка обновлений недоступна." : "Не удалось загрузить настройки обновлений.";
    releases.tone = "warn";
  } finally {
    if (seq === releases.seq) {
      releases.pending = "";
      renderReleases();
      if ($("release-dialog").open && [document.body, $("release-dialog")].includes(document.activeElement)) {
        const radio = focus?.closest("#release-channels");
        const confirmed = $("release-testing").checked ? $("release-testing") : $("release-stable");
        (radio && releases.info ? confirmed : $("release-check")).focus({preventScroll: true});
      }
    }
  }
}

function openReleases(target = null) {
  if (state.mode !== "app") return;
  const dialog = $("release-dialog");
  if (dialog.open) return;
  resetReleases();
  releases.target = target;
  dialog.showModal();
  $("release-title").focus();
  loadReleases({check: target?.kind === "install"});
}

async function installRelease() {
  const {target, info, candidate} = releases;
  if (!target || !info?.canQueue || !candidate || !releases.confirming || releases.pending || releases.queued) return;
  const seq = ++releases.seq;
  const install = target.kind === "install";
  releases.confirming = false;
  releases.pending = install ? "Готовим установку…" : "Ставим обновление в очередь…";
  renderReleases();
  try {
    const selection = {tag: candidate.tag, channel: info.channel, sha256: candidate.asset.sha256};
    const data = await api(install ? "fleet/install" : "fleet/upgrade", install ? {...selection, target: target.id}
      : {...selection, node: target.id, current: info.current}, 18000);
    if (seq !== releases.seq) return;
    if (data.tag !== candidate.tag || (install ? data.stage !== "downloading" : data.status !== "queued")) throw new Error();
    releases.queued = true;
    releases.tone = "good";
    releases.message = install ? "Задание принято. Ход установки виден в списке подготовленных нод."
      : "Обновление в очереди. Ход установки виден в карточке ноды.";
    loadFleet();
  } catch (error) {
    if (seq !== releases.seq) return;
    if (error.status === 401) return showLogin(true);
    releases.tone = "warn";
    releases.message = errorText(error, "Задание не подтверждено. Проверьте карточку ноды.");
  } finally {
    if (seq === releases.seq) {
      releases.pending = "";
      renderReleases();
      $("release-check").focus({preventScroll: true});
    }
  }
}

async function verifyReleaseFile(event) {
  const input = event.currentTarget;
  const file = input.files[0];
  const asset = releases.candidate?.asset;
  input.value = "";
  if (!file || !asset || !window.crypto?.subtle) return;
  const seq = ++releases.verifySeq;
  if (file.size !== asset.size) {
    releaseVerification("Размер файла не совпадает с размером архива релиза.", "warn");
    return;
  }
  releaseVerification("Считаем SHA-256…");
  try {
    const digest = await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    if (seq !== releases.verifySeq) return;
    const actual = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
    if (actual === asset.sha256) releaseVerification("SHA-256 совпадает с суммой релиза.", "good");
    else releaseVerification("SHA-256 не совпадает. Скачайте архив заново.", "warn");
  } catch {
    if (seq === releases.verifySeq) releaseVerification("Не удалось прочитать файл.", "warn");
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
  fleet.installTargets = [];
  fleet.installKey = "";
  fleet.selected.clear();
  fleet.revokeId = null;
  gpuPower.target = gpuPower.request = null;
  $("gpu-power-form").reset();
  for (const id of ["gpu-power-device", "gpu-power-current", "gpu-power-factory", "gpu-power-preview",
    "gpu-power-range", "gpu-power-availability", "gpu-power-message"]) text($(id), "");
  for (const id of ["gpu-power-defaults", "gpu-power-presets", "gpu-power-preview"]) $(id).hidden = true;
  clearFleetInvite();
  $("fleet-enroll").reset();
  $("fleet-enroll").hidden = true;
  $("node-list").replaceChildren();
  state.atlasKey = "";
  $("atlas-node").hidden = true;
  $("atlas-node").replaceChildren();
  $("prepared-install").hidden = true;
  $("prepared-install-targets").replaceChildren();
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

function renderPreparedTargets() {
  const {installTargets, fresh, busy} = fleet;
  const key = JSON.stringify([installTargets, fresh, busy]);
  if (fleet.installKey === key) return;
  fleet.installKey = key;
  $("prepared-install").hidden = !installTargets.length;
  const active = installTargets.some(({install}) => ["downloading", "connecting", "waiting"].includes(install?.stage));
  const labels = {downloading: "Проверяет архив", connecting: "Устанавливается", waiting: "Ждёт первого отчёта",
    ready: "Нода подключена", failed: "Отказ до установки", unconfirmed: "Требует проверки"};
  $("prepared-install-targets").replaceChildren(...installTargets.map(target => {
    const item = create("li");
    const title = create("div");
    const label = safeLine(target.label);
    const stage = target.install?.stage;
    const kernel = ["6.6", "6.12"].includes(target.kernel) ? ` · ядро ${target.kernel}` : "";
    title.append(create("strong", label), create("p", `Linux x64${kernel}`, "subline"));
    item.append(title);
    const action = create("div", undefined, "panel-actions");
    if (stage) {
      const result = create("span", [safeLine(target.install?.tag), labels[stage]].filter(Boolean).join(" · "), "state");
      result.dataset.tone = stage === "ready" ? "good" : ["failed", "unconfirmed"].includes(stage) ? "warn" : "neutral";
      action.append(result);
    }
    if (!stage || stage === "failed") {
      const button = create("button", "Выбрать выпуск");
      button.type = "button";
      button.disabled = !fresh || busy || active;
      button.setAttribute("aria-label", `Выбрать выпуск для ${label}`);
      button.addEventListener("click", () => openReleases({id: target.id, label, kind: "install"}));
      action.append(button);
    }
    item.append(action);
    return item;
  }));
}

function powerRange(device) {
  const {min, max, current, default_watts: factory} = device.power_limit || {};
  if (![min, max, current].every(Number.isFinite) || min < 1 || max > 3000 || current < min || current > max
      || Math.ceil(min) > Math.floor(max)) return null;
  const defaultWatts = Number.isFinite(factory) && factory >= min && factory <= max ? factory : null;
  return {min: Math.ceil(min), max: Math.floor(max), current, reportedMin: min, defaultWatts};
}

function validGpuPowerWatts(range, watts) {
  return Boolean(range) && Number.isSafeInteger(watts) && watts >= range.min && watts <= range.max;
}

function gpuPowerPresets(range) {
  if (range.defaultWatts === null) return {};
  const toWatts = percent => {
    const nominal = range.defaultWatts * percent / 100;
    const watts = Math.round(nominal);
    return nominal >= range.reportedMin && validGpuPowerWatts(range, watts) ? watts : null;
  };
  const presets = {};
  let desk = 60;
  for (; desk < 100; desk++) {
    const watts = toWatts(desk);
    if (watts === null || watts >= range.defaultWatts) continue;
    presets.desk = {percent: desk, watts};
    break;
  }
  let previous = presets.desk?.watts ?? 0;
  for (const percent of [80, 90]) {
    const watts = toWatts(percent);
    if (percent <= desk || watts === null || watts <= previous || watts >= range.defaultWatts) continue;
    presets[String(percent)] = {percent, watts};
    previous = watts;
  }
  if (Number.isInteger(range.defaultWatts)) presets["100"] = {percent: 100, watts: range.defaultWatts};
  return presets;
}

function gpuLimitText(watts, factory) {
  const reading = fmt(watts, "Вт", 1);
  return factory === null ? reading : `${reading} · ${fmt(100 * watts / factory, "%", 1)} от штатного`;
}

function canQueueGpuPower(node) {
  return state.mode === "app" && state.view === "nodes" && state.online && !state.authMutation
    && fleet.fresh && !fleet.busy && location.protocol === "https:"
    && node?.status === "online" && node.release?.platform === "windows" && node.hardware?.power_control === true
    && !["queued", "sent", "claimed"].includes(node.power_job?.status);
}

function gpuPowerState(target) {
  const node = inList(fleet.nodes).find(row => row.id === target.node);
  const device = inList(node?.hardware?.devices).find(row => row.id === target.gpu);
  const range = device ? powerRange(device) : null;
  return {range, writable: Boolean(range) && canQueueGpuPower(node)};
}

function renderGpuPower() {
  const target = gpuPower.target;
  if (!target) return;
  const {range: latest, writable} = gpuPowerState(target);
  const range = target.range = latest || target.range;
  const waiting = Boolean(gpuPower.request) || target.queued;
  const input = $("gpu-power-watts");
  input.min = range.min;
  input.max = range.max;
  input.disabled = !writable || waiting;
  const watts = input.valueAsNumber;
  const valid = validGpuPowerWatts(range, watts);
  input.setAttribute("aria-invalid", String(input.validity.badInput || (input.value !== "" && !valid)));
  $("gpu-power-send").disabled = input.disabled || !valid;

  const hasDefault = range.defaultWatts !== null;
  const current = gpuLimitText(range.current, range.defaultWatts);
  $("gpu-power-defaults").hidden = !hasDefault;
  text($("gpu-power-current"), current);
  text($("gpu-power-factory"), fmt(range.defaultWatts, "Вт", 1));
  text($("gpu-power-range"), `${hasDefault ? "" : `Сейчас ${current}. `}Доступно ${range.min}–${range.max} Вт.`);

  const presets = gpuPowerPresets(range);
  $("gpu-power-presets").hidden = !Object.keys(presets).length;
  for (const button of all("#gpu-power-presets [data-power-preset]")) {
    const key = button.dataset.powerPreset;
    const preset = presets[key];
    button.hidden = !preset;
    button.disabled = input.disabled;
    button.setAttribute("aria-pressed", String(Boolean(preset) && preset.watts === watts));
    if (!preset) continue;
    button.dataset.watts = preset.watts;
    const label = key === "desk" ? "За ПК · " : key === "100" ? "Штатный · " : "";
    text(button.firstElementChild, `${label}${preset.percent} %`);
    text(button.lastElementChild, fmt(preset.watts, "Вт", 0));
  }
  const preview = $("gpu-power-preview");
  preview.hidden = !hasDefault;
  text(preview, valid ? `Выбрано ${gpuLimitText(watts, range.defaultWatts)}.` : "Введите целое число в доступном диапазоне.");
  text($("gpu-power-availability"), !waiting && !writable
    ? "Для отправки нужны свежие данные ноды и разрешение на изменение лимита." : "");
}

function openGpuPower(node, device) {
  const dialog = $("gpu-power-dialog");
  if (gpuPower.request || dialog.open) return;
  const target = {node: node.id, gpu: device.id};
  const {range, writable} = gpuPowerState(target);
  if (!writable) return;
  gpuPower.target = {...target, range, queued: false};
  const input = $("gpu-power-watts");
  input.value = validGpuPowerWatts(range, range.current) ? range.current : "";
  text($("gpu-power-device"), `${safeLine(node.label)} · ${safeLine(device.label)}`);
  text($("gpu-power-message"), "");
  renderGpuPower();
  dialog.showModal();
  ($("gpu-power-presets").querySelector("button:not([hidden]):not(:disabled)") || input).focus();
}

async function submitGpuPower(event) {
  event.preventDefault();
  const target = gpuPower.target;
  if (gpuPower.request || !target || target.queued || !$("gpu-power-dialog").open) return;
  const watts = $("gpu-power-watts").valueAsNumber;
  const {range, writable} = gpuPowerState(target);
  if (!writable || !validGpuPowerWatts(range, watts)) {
    renderGpuPower();
    return;
  }
  const seq = gpuPower.request = {};
  const visible = () => $("gpu-power-dialog").open && gpuPower.target === target;
  const msg = $("gpu-power-message");
  renderGpuPower();
  text(msg, "Ставим команду в очередь…");
  try {
    const data = await api("fleet/power", {node: target.node, gpu: target.gpu, watts});
    if (gpuPower.request !== seq) return;
    if (data.status !== "queued" || data.gpu !== target.gpu || data.watts !== watts) throw new Error();
    target.queued = true;
    if (visible()) text(msg, "Команда в очереди. Результат появится в карточке ноды.");
  } catch (error) {
    if (gpuPower.request !== seq) return;
    if (error.status === 401) return showLogin(true);
    if (visible()) text(msg, errorText(error, "Запрос не подтверждён. Проверьте результат в карточке ноды."));
  } finally {
    if (gpuPower.request === seq) await loadFleet();
    if (gpuPower.request === seq) {
      gpuPower.request = null;
      if (visible()) renderGpuPower();
    }
  }
}

function nodeGpuCards(devices, atlas = false, node = null) {
  const grid = create("div", undefined, "node-gpu-grid");
  for (const device of devices) {
    const gpu = create("section", undefined, "node-gpu");
    const identity = create("div");
    identity.append(create("h3", safeLine(device.label) || "GPU"), create("p", safeLine(device.id), "subline mono"));
    if (atlas) {
      const states = {mining: "Добыча", starting: "Запускается", retrying: "Повтор запуска", stopping: "Останавливается",
        busy: "Занята", error: "Сбой", paused: "На паузе", idle: "Простой", stopped: "Остановлена"};
      const label = states[device.state];
      if (label) identity.append(create("p", label, "subline"));
    }
    const readings = create("dl", undefined, "node-gpu-readings");
    const vram = Number.isFinite(device.memory_used_mib) && Number.isFinite(device.memory_total_mib)
      ? `${n(device.memory_used_mib, 0)} / ${n(device.memory_total_mib, 0)} МиБ` : "—";
    const metrics = [["Ядро", temperature(device.edge)], ["Потребление", fmt(device.watts, "Вт", 1)]];
    metrics.push(...(atlas ? [["Хешрейт", rate(device.rate_hs)], ["Hotspot", temperature(device.hot)]]
      : [["Загрузка GPU", fmt(device.utilization, "%", 0)], ["Память", vram]]));
    for (const [label, reading] of metrics) {
      const pair = create("div");
      pair.append(create("dt", label), create("dd", reading));
      readings.append(pair);
    }
    gpu.append(identity, readings);
    const power = powerRange(device);
    if (power) {
      const controls = create("div", undefined, "node-gpu-power");
      controls.append(create("span", `Предел ${gpuLimitText(power.current, power.defaultWatts)}`, "subline"));
      if (node?.hardware?.power_control === true && location.protocol === "https:") {
        const apply = actionButton("Изменить предел", () => openGpuPower(node, device), "quiet");
        apply.dataset.controlKey = `${node.id}:power:${device.id}`;
        apply.disabled = !canQueueGpuPower(node);
        controls.append(apply);
      } else if (node) {
        const hint = location.protocol === "https:"
          ? "В Apollo.NEB Desktop на этом ПК разрешите изменение лимита для текущей сессии. Нужны права администратора."
          : "Для управления откройте панель по HTTPS.";
        controls.append(create("p", hint, "node-gpu-power-help subline"));
      }
      gpu.append(controls);
    }
    grid.append(gpu);
  }
  return grid;
}

function renderAtlasNode(atlas) {
  const section = $("atlas-node");
  section.hidden = !atlas;
  const key = JSON.stringify([state.online, atlas]);
  if (key === state.atlasKey) return;
  state.atlasKey = key;
  if (!atlas) {
    section.replaceChildren();
    return;
  }
  const online = state.online && atlas.status === "online";
  const card = create("article", undefined, "surface node-card");
  const heading = create("div", undefined, "panel-head");
  const identity = create("div");
  identity.append(create("h2", "Atlas"), create("p", online ? `Последний замер · ${eventTime(atlas.time)}`
    : "Свежий снимок недоступен."));
  const badge = create("span", online ? "На связи" : "Нет данных", "state");
  badge.dataset.tone = online ? "good" : "warn";
  heading.append(identity, badge);
  card.append(heading);
  if (online) {
    card.append(create("p", `GPU в работе · ${n(atlas.gpu_counts.active, 0)} / ${n(atlas.gpu_counts.total, 0)}`, "subline"),
      nodeGpuCards(inList(atlas.gpus), true));
  }
  section.replaceChildren(card);
}

function renderFleet() {
  renderPreparedTargets();
  renderGpuPower();
  const rows = inList(fleet.nodes);
  $("fleet-enroll").hidden = !fleet.fresh;
  $("fleet-label").disabled = fleet.busy;
  $("fleet-enroll").querySelector('button[type="submit"]').disabled = fleet.busy;
  $("fleet-refresh").disabled = fleet.loading || fleet.busy;
  if (fleet.fresh) text($("fleet-status"), rows.length ? `Нод: ${rows.length}. Команды не повторяются автоматически.` : "Подключённых нод пока нет.");
  else if (fleet.nodes) text($("fleet-status"), "Связь с реестром потеряна. Показаны последние полученные данные; команды недоступны.");
  const key = JSON.stringify([state.online, state.authMutation, fleet.fresh, fleet.busy, rows.map(({last_seen, ...node}) => node)]);
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
    if (typeof node.release?.version === "string") {
      info.append(create("p", `Apollo.NEB ${safeLine(node.release.version)}`, "subline"));
    }
    const [nodeLabel, tone] = NODE_STATES.get(node.status) || ["Состояние неизвестно", "neutral"];
    const mark = create("span", nodeLabel, "state");
    mark.dataset.tone = tone;
    head.append(info, mark);
    card.append(head);
    const workers = inList(node.workers).slice(0, 128);
    const gpus = inList(node.hardware?.devices).slice(0, 32);
    const commands = inList(node.commands).slice(0, 10);
    const upgrade = node.upgrade;
    const upgradeActive = ["queued", "sent", "preparing", "applying"].includes(upgrade?.status);
    const pending = upgradeActive || commands.some(item => item.status === "queued" || item.status === "sent");
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
    } else if (node.release?.platform === "windows") {
      card.append(create("p", gpus.length ? "Мониторинг GPU" : "Датчики GPU недоступны.", "subline"));
      if (gpus.length) card.append(nodeGpuCards(gpus, false, node));
    } else {
      card.append(create("p", "Нода ещё не сообщила о воркерах.", "subline"));
    }
    if (node.status === "online" && node.release?.version) {
      const updates = actionButton("Выпуски", () => openReleases({id: node.id, label: name}), "quiet");
      updates.dataset.controlKey = node.id + ":releases";
      updates.disabled = !fleet.fresh || fleet.busy;
      controls.append(updates);
    }
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
    if (node.power_job) {
      const labels = {queued: "В очереди", sent: "Передано", claimed: "Проверяется на ноде", applied: "Предел подтверждён",
        refused: "Отклонено нодой", unknown: "Результат неизвестен", expired: "Срок истёк", cancelled: "Отменено"};
      const job = node.power_job;
      const detail = safeLine(job.gpu) || "GPU";
      const label = labels[job.status] || "Состояние неизвестно";
      card.append(create("p", `${detail} · ${fmt(job.watts, "Вт", 0)} · ${label}`, "subline"));
    }
    if (upgrade) {
      const labels = {queued: "В очереди", sent: "Передано", preparing: "Проверяет пакет", applying: "Устанавливается",
        installed: "Установлено", rolled_back: "Восстановлена прежняя версия", failed: "Ошибка установки",
        expired: "Срок задания истёк", unknown: "Итог не подтверждён", cancelled: "Отменено"};
      const label = labels[upgrade.status];
      if (label) card.append(create("p", `Обновление ${safeLine(upgrade.tag)} · ${label}`, "subline"));
    }
    if (commands.length) {
      const details = create("details");
      details.dataset.nodeCommands = node.id;
      details.open = expanded.has(node.id);
      details.append(create("summary", "Последние команды"));
      const list = create("ul", undefined, "result-list");
      for (const command of commands) {
        const status = COMMAND_STATES.get(command.status) || "Статус неизвестен";
        const action = {start: "Запуск", stop: "Остановка", restart: "Перезапуск"}[command.action] || "Команда";
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
    fleet.installTargets = inList(data.install_targets).slice(0, 16).filter(
      row => row && /^neb-[a-z0-9][a-z0-9-]{0,39}$/.test(row.id) && safeLine(row.label));
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

function openProfile() {
  if (!state.online || !state.status || !hasCapability("local_workers") || $("profile-dialog").open) return;
  const {profile, pools} = state.status;
  $("wallet").value = "";
  $("wallet").required = profile?.wallet_configured !== true;
  text($("wallet-hint"), $("wallet").required ? "" : "Оставьте поле пустым, чтобы сохранить текущий адрес.");
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
  if (!state.online || !state.status || !hasCapability("local_workers") || $("limits-dialog").open) return;
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
      ? {worker: $("worker-name").value.trim(), pool: $("pool").value}
      : {edge_limit: $("edge-limit").valueAsNumber, hot_limit: $("hot-limit").valueAsNumber,
        hbm_limit: $("hbm-limit").value === "" ? null : $("hbm-limit").valueAsNumber, ack: $("limits-ack").checked};
    if (kind === "profile" && $("wallet").value.trim()) data.wallet = $("wallet").value.trim();
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

$("unlock").addEventListener("click", () => {
  if (state.mode !== "login" || $("login").open) return;
  $("login").showModal();
  refreshAuthInfo();
  $("password").focus();
});
$("login").addEventListener("cancel", event => {
  if ($("login-form").querySelector('button[type="submit"]').disabled) event.preventDefault();
});
$("login").addEventListener("close", () => $("login-form").reset());

$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const btn = event.currentTarget.querySelector("button");
  if (btn.disabled) return;
  const pass = $("password");
  const code = $("login-code");
  const dismiss = $("login").querySelector("[data-close]");
  btn.disabled = pass.disabled = code.disabled = dismiss.disabled = true;
  text($("login-message"), "Проверяем данные…");
  try {
    await api("login", {password: pass.value, code: code.value.trim()});
    pass.value = code.value = "";
    state.generation++;
    state.mode = "boot";
    $("login").close();
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
    btn.disabled = pass.disabled = code.disabled = dismiss.disabled = false;
    if ($("login").open) pass.focus();
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
for (const button of all("[data-discovery]")) button.addEventListener("click", openDiscovery);
for (const id of ["totp-dialog", "discovery-dialog"]) {
  $(id).addEventListener("cancel", event => {
    if ($(id).dataset.busy === "true") event.preventDefault();
    else if (id === "totp-dialog") clearTotp();
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
$("release-open").addEventListener("click", () => openReleases());
$("release-check").addEventListener("click", () => loadReleases({check: Boolean(releases.info)}));
$("release-version").addEventListener("change", () => {
  const target = releases.versions.find(release => release.tag === $("release-version").value);
  if (!target || target === releases.candidate) return;
  setReleaseCandidate(target);
  renderReleases();
});
$("release-channels").addEventListener("change", event => {
  const channel = event.target.value;
  if (RELEASE_CHANNELS.has(channel) && channel !== releases.info?.channel) {
    loadReleases({channel, check: Boolean(releases.target)});
  }
});
$("release-file").addEventListener("change", verifyReleaseFile);
$("release-apply").addEventListener("click", () => {
  releases.confirming = true;
  renderReleases();
  $("release-confirm-submit").focus();
});
$("release-confirm-cancel").addEventListener("click", () => {
  releases.confirming = false;
  renderReleases();
  $("release-apply").focus();
});
$("release-confirm-submit").addEventListener("click", installRelease);
$("gpu-power-watts").addEventListener("input", renderGpuPower);
for (const button of all("#gpu-power-presets [data-power-preset]")) {
  button.addEventListener("click", () => {
    if (!gpuPower.target || button.hidden || button.disabled) return;
    $("gpu-power-watts").value = button.dataset.watts;
    renderGpuPower();
  });
}
$("gpu-power-form").addEventListener("submit", submitGpuPower);
$("gpu-power-dialog").addEventListener("close", () => {
  if ($("gpu-power-dialog").open) return;
  const target = gpuPower.target;
  gpuPower.target = null;
  if (target && state.mode === "app" && state.view === "nodes" && document.activeElement === document.body) {
    const key = `${target.node}:power:${target.gpu}`;
    const trigger = all("#node-list button[data-control-key]").find(button => button.dataset.controlKey === key && !button.disabled);
    (trigger || document.querySelector("[data-view='nodes']"))?.focus({preventScroll: true});
  }
});
$("fleet-refresh").addEventListener("click", loadFleet);
$("fleet-copy-key").addEventListener("click", () => {
  if ($("fleet-invite").hidden) return;
  const id = $("fleet-invite-id").textContent;
  const code = $("fleet-invite-code").textContent;
  if (/^[a-f0-9]{24}$/.test(id) && /^[A-Za-z0-9_-]{24}$/.test(code)) {
    copySensitive(`${id}.${code}`, $("fleet-message"), () => !$("fleet-invite").hidden);
  }
});
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
$("history-source").addEventListener("change", () => {
  applyHistorySource();
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
    if (state.view === "nodes") fleet.fresh = false;
    renderStatus();
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
if (window.location.pathname === "/view") showLogin();
else refreshStatus();
