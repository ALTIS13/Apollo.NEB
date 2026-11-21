"use strict";

const $ = id => document.getElementById("view-" + id);
const decimal = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2});
const percent = new Intl.NumberFormat("ru-RU", {style: "percent", maximumFractionDigits: 2});
const relativeTime = new Intl.RelativeTimeFormat("ru-RU", {style: "short"});
const isRecord = value => value !== null && typeof value === "object" && !Array.isArray(value);
const isMeasurement = value => Number.isFinite(value) && value >= 0;
const isCount = value => Number.isSafeInteger(value) && value >= 0;
const measurement = (value, unit) => isMeasurement(value) ? decimal.format(value) + " " + unit : "—";
const count = value => isCount(value) ? decimal.format(value) : "—";
const pollInterval = 5000;
const staleAfter = 20;
const feeStates = new Map([
  ["disabled", "Выключена"],
  ["paused", "На паузе"],
  ["unavailable", "Недоступна"],
  ["idle", "Простой"],
  ["counting", "Учёт времени"],
  ["active", "Окно активно"],
]);

let timer;
let pending;

function write(id, value) {
  const element = $(id);
  if (element.textContent !== value) element.textContent = value;
}

function connection(label, tone) {
  $("connection").dataset.tone = tone;
  write("connection-label", label);
}

function hashrate(value) {
  if (!isMeasurement(value)) return "—";
  for (const [scale, unit] of [[1e12, "TH/s"], [1e9, "GH/s"], [1e6, "MH/s"], [1e3, "kH/s"]]) {
    if (value >= scale) return measurement(value / scale, unit);
  }
  return measurement(value, "H/s");
}

function freshness(stamp) {
  const element = $("time");
  const date = Number.isFinite(stamp) && stamp > 0 ? new Date(stamp * 1000) : null;
  element.hidden = !date || !Number.isFinite(date.getTime());
  if (element.hidden) {
    connection("Время замера неизвестно", "neutral");
    return;
  }
  element.dateTime = date.toISOString();
  element.title = date.toLocaleString("ru-RU");
  const age = Math.floor(Date.now() / 1000 - stamp);
  if (age < -5) {
    write("time", "Замер " + element.title);
    connection("Проверьте время на устройствах", "warn");
    return;
  }
  const [scale, unit] = age >= 86400 ? [86400, "day"] : age >= 3600 ? [3600, "hour"]
    : age >= 60 ? [60, "minute"] : [1, "second"];
  write("time", "Замер " + relativeTime.format(-Math.floor(Math.max(0, age) / scale), unit));
  if (age >= staleAfter) connection("Данные устарели", "warn");
  else connection("Данные актуальны", "good");
}

function render(data) {
  const firstSample = data?.time === 0;
  const {workers, hashrate_hs, watts, temperatures, shares, devfee} = firstSample ? {} : data ?? {};
  showLiveReading($("hashrate"), hashrate(hashrate_hs), hashrate_hs);
  showLiveReading($("watts"), measurement(watts, "Вт"), watts);
  write("workers", isCount(workers?.running) && isCount(workers?.total) && workers.running <= workers.total
    ? count(workers.running) + " из " + count(workers.total) : "—");
  write("attention", count(workers?.attention));
  for (const id of ["edge", "hot", "hbm"]) {
    const raw = temperatures?.[id];
    const reading = Number.isFinite(raw) ? Math.floor(raw) : null;
    showLiveReading($(id), measurement(reading, "°C"), reading);
  }
  for (const id of ["accepted", "rejected"]) write(id, count(shares?.[id]));
  write("devfee-state", devfee?.enabled === false ? "Выключена" : feeStates.get(devfee?.state) ?? "—");
  const points = devfee?.basis_points;
  write("devfee-share", isCount(points) && points <= 10000 ? percent.format(points / 10000) : "—");
  if (data && !firstSample) freshness(data.time);
  else {
    $("time").hidden = true;
    if (firstSample) connection("Ожидаем первый замер", "neutral");
    else connection("Служба недоступна", "warn");
  }
}

async function poll() {
  if (document.hidden || pending) return;
  const attempt = new AbortController();
  pending = attempt;
  const timeout = setTimeout(() => attempt.abort(), 4500);
  try {
    const response = await fetch("/api/public/status", {
      credentials: "omit", cache: "no-store", redirect: "error", signal: attempt.signal,
    });
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!isRecord(data) || ![data.workers, data.temperatures, data.shares, data.devfee].every(isRecord)) throw new Error();
    if (pending === attempt && !document.hidden) render(data);
  } catch {
    if (pending === attempt && !document.hidden) render(null);
  } finally {
    clearTimeout(timeout);
    if (pending === attempt) pending = null;
  }
}

function updatePolling() {
  clearInterval(timer);
  if (document.hidden) {
    pending?.abort();
    pending = null;
    return;
  }
  poll();
  timer = setInterval(poll, pollInterval);
}

document.addEventListener("visibilitychange", updatePolling);
updatePolling();
