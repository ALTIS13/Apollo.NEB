"use strict";

const $ = id => document.getElementById(id);
const unavailable = "Не удалось проверить состояние DevFee. Повторите проверку.";
const sessionEnded = "Сеанс завершён. Войдите снова.";
let paused = null;
let busy = false;
let enrollment = false;
let hasTotp = true;
let enrollmentTimer;

async function request(path, body) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 10000);
  try {
    const options = {credentials: "same-origin", cache: "no-store", signal: controller.signal};
    if (body !== undefined) {
      options.method = "POST";
      options.headers = {"Content-Type": "application/json"};
      options.body = JSON.stringify(body);
    }
    const response = await fetch("./api/" + path, options);
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error();
      error.status = response.status;
      error.noPending = data?.error === "Привязка недоступна. Начните её заново.";
      error.rateLimited = response.status === 429 || data?.error === "Слишком много попыток, повторите через минуту.";
      throw error;
    }
    if (!data || typeof data !== "object") throw new Error();
    return data;
  } finally {
    clearTimeout(deadline);
  }
}

function view(name) {
  for (const item of ["boot", "login", "enrollment", "policy"]) $(item).hidden = item !== name;
  $("logout").hidden = name !== "policy" && name !== "enrollment";
  if (name !== "enrollment") clearEnrollment();
}

function controls() {
  for (const id of ["login-submit", "logout", "boot-retry", "policy-refresh",
    "totp-begin-submit", "totp-confirm-submit", "totp-restart"]) $(id).disabled = busy;
  $("policy-change").disabled = busy || paused === null;
}

async function run(action) {
  if (busy) return;
  busy = true;
  controls();
  try {
    await action();
  } finally {
    busy = false;
    controls();
  }
}

function signIn(message = "") {
  paused = null;
  view("login");
  $("login-form").reset();
  $("login-message").textContent = message;
  $("code-field").hidden = !hasTotp;
  $("code").required = hasTotp;
  $("password").focus({preventScroll: true});
}

function clearEnrollment() {
  clearTimeout(enrollmentTimer);
  $("totp-begin").reset();
  $("totp-confirm").reset();
  $("totp-message").textContent = "";
  $("totp-secret").textContent = "";
  $("totp-qr").replaceChildren();
  $("totp-qr-wrap").hidden = true;
  $("totp-recovery-hint").hidden = true;
  $("totp-recovery-download").hidden = true;
  $("totp-codes").replaceChildren();
  $("totp-codes").hidden = true;
}

function showEnrollment(step) {
  clearEnrollment();
  view("enrollment");
  $("totp-begin").hidden = step !== "password";
  $("totp-enroll").hidden = step !== "code";
  $("totp-recovery").hidden = step !== "recovery";
  const focus = step === "password" ? "totp-password" : step === "code" ? "totp-code" : "enrollment-title";
  $(focus).focus({preventScroll: true});
}

function drawTotpQr(uri) {
  const QrCode = window.qrcodegen?.QrCode;
  if (!QrCode || typeof uri !== "string" || !uri.startsWith("otpauth://totp/") || uri.length > 1000) return;
  try {
    const qr = QrCode.encodeText(uri, QrCode.Ecc.MEDIUM);
    const size = qr.size + 8;
    const points = [];
    for (let y = 0; y < qr.size; y++) {
      for (let x = 0; x < qr.size; x++) {
        if (qr.getModule(x, y)) points.push(`M${x + 4} ${y + 4}h1v1h-1z`);
      }
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", points.join(""));
    path.setAttribute("fill", "currentColor");
    $("totp-qr").setAttribute("viewBox", `0 0 ${size} ${size}`);
    $("totp-qr").replaceChildren(path);
    $("totp-qr-wrap").hidden = false;
  } catch {
    // The same secret is available for manual entry.
  }
}

async function boot() {
  $("boot-retry").hidden = true;
  $("boot-message").textContent = "Подключаемся к панели…";
  try {
    const data = await request("auth/info");
    if (typeof data.totp !== "boolean") throw new Error();
    hasTotp = data.totp;
    enrollment = data.enrollment === true;
  } catch {
    $("boot-message").textContent = "Не удалось проверить способ входа. Повторите подключение.";
    $("boot-retry").hidden = false;
    return;
  }
  if (!enrollment) await refresh();
  else if (hasTotp) {
    showEnrollment("recovery");
    $("logout").hidden = true;
  } else signIn();
}

function showPolicy(state, message = "") {
  paused = state;
  view("policy");
  $("policy-status").textContent = state === null ? "Состояние неизвестно" : state ? "Приостановлена" : "Разрешена";
  $("policy-chip").dataset.tone = state === false ? "good" : "warn";
  $("policy-message").textContent = message;
  $("policy-impact").hidden = state === null;
  $("policy-impact").textContent = state
    ? "Панели Portable пропускают окна DevFee после получения паузы."
    : "Панели Portable могут открыть окно DevFee после получения политики и накопления времени.";
  const change = $("policy-change");
  change.hidden = state === null;
  change.textContent = state ? "Разрешить DevFee" : "Приостановить DevFee";
  change.classList.toggle("primary", state === true);
  change.classList.toggle("stop-button", state === false);
}

async function refresh() {
  const signedIn = !$("policy").hidden;
  $("boot-retry").hidden = true;
  $("boot-message").textContent = "Подключаемся к панели…";
  $("policy-message").textContent = "Проверяем состояние…";
  try {
    const data = await request("devfee");
    if (typeof data.paused !== "boolean") throw new Error();
    showPolicy(data.paused);
  } catch (error) {
    if (error.status === 401) signIn(signedIn ? sessionEnded : "");
    else if (signedIn) showPolicy(null, unavailable);
    else {
      $("boot-message").textContent = unavailable;
      $("boot-retry").hidden = false;
    }
  }
}

$("login-form").addEventListener("submit", event => {
  event.preventDefault();
  run(async () => {
    const password = $("password").value;
    const code = $("code").value.trim();
    $("login-form").reset();
    $("login-message").textContent = "Проверяем данные…";
    try {
      const response = await request("login", enrollment ? {password} : {password, code});
      if (response.ok !== true) throw new Error();
      if (enrollment) showEnrollment("password");
      else {
        view("policy");
        await refresh();
        if (!$("policy").hidden) $("policy-title").focus({preventScroll: true});
      }
    } catch (error) {
      $("login-message").textContent = error.rateLimited ? "Слишком много попыток. Повторите через минуту."
        : error.status === 401 ? "Пароль или код не принят." : "Не удалось выполнить вход. Повторите попытку.";
      $("password").focus({preventScroll: true});
    }
  });
});

$("totp-begin").addEventListener("submit", event => {
  event.preventDefault();
  if (!enrollment || $("totp-begin").hidden) return;
  run(async () => {
    const password = $("totp-password").value;
    $("totp-begin").reset();
    $("totp-message").textContent = "Подготавливаем настройку…";
    try {
      const data = await request("auth/totp/begin", {password});
      const remaining = Number.isFinite(data.expires_in)
        ? data.expires_in * 1000
        : data.expires_at * 1000 - Date.now();
      if (typeof data.secret !== "string" || !/^[A-Z2-7]{32}$/.test(data.secret)
          || !Number.isFinite(remaining) || remaining <= 0) throw new Error();
      showEnrollment("code");
      $("totp-secret").textContent = data.secret;
      drawTotpQr(data.uri);
      enrollmentTimer = setTimeout(() => {
        showEnrollment("password");
        $("totp-message").textContent = "Время настройки истекло. Введите пароль ещё раз.";
      }, Math.min(remaining, 2147483647));
    } catch (error) {
      if (error.status === 401 && !error.rateLimited) signIn(sessionEnded);
      else $("totp-message").textContent = error.rateLimited ? "Слишком много попыток. Повторите через минуту."
        : error.status === 409 ? "Пароль не принят или настройка уже завершена."
        : "Не удалось подготовить 2FA. Повторите попытку.";
    }
  });
});

$("totp-confirm").addEventListener("submit", event => {
  event.preventDefault();
  if (!enrollment || $("totp-enroll").hidden) return;
  run(async () => {
    const code = $("totp-code").value.trim();
    $("totp-confirm").reset();
    $("totp-message").textContent = "Проверяем код…";
    try {
      const data = await request("auth/totp/confirm", {code});
      if (data.ok !== true || !Array.isArray(data.recovery_codes)
          || !data.recovery_codes.length || data.recovery_codes.length > 20
          || data.recovery_codes.some(item => typeof item !== "string" || item.length > 80 || !item)) throw new Error();
      hasTotp = true;
      showEnrollment("recovery");
      $("totp-codes").replaceChildren(...data.recovery_codes.map(code => {
        const entry = document.createElement("li");
        entry.textContent = code;
        return entry;
      }));
      $("totp-codes").hidden = false;
      $("totp-recovery-hint").hidden = false;
      $("totp-recovery-download").hidden = false;
    } catch (error) {
      if (error.status === 401 && !error.rateLimited) signIn(sessionEnded);
      else if (error.noPending) {
        showEnrollment("password");
        $("totp-message").textContent = "Время настройки истекло. Введите пароль ещё раз.";
      } else $("totp-message").textContent = error.rateLimited ? "Слишком много попыток. Повторите через минуту."
        : error.status === 409 ? "Код не принят. Введите новый код из приложения."
        : "Не удалось подтвердить 2FA. Попробуйте ещё раз.";
    }
  });
});

$("totp-restart").addEventListener("click", () => showEnrollment("password"));
$("totp-recovery-download").addEventListener("click", () => {
  if (enrollment && !$("totp-recovery").hidden) downloadRecoveryCodes($("totp-codes"), true);
});

$("policy-form").addEventListener("submit", event => {
  event.preventDefault();
  if (paused === null) return;
  run(async () => {
    const next = !paused;
    $("policy-message").textContent = "Сохраняем состояние…";
    try {
      const data = await request("devfee", {paused: next});
      if (data.paused !== next) throw new Error();
      showPolicy(data.paused);
    } catch (error) {
      if (error.status === 401) signIn(sessionEnded);
      else showPolicy(null, "Изменение не подтверждено. Проверьте текущее состояние.");
    }
  });
});

$("logout").addEventListener("click", () => run(async () => {
  try {
    const response = await request("logout", {});
    if (response.ok !== true) throw new Error();
  } catch (error) {
    if (error.status !== 401) {
      $(enrollment ? "totp-message" : "policy-message").textContent = "Выход не подтверждён. Повторите попытку.";
      return;
    }
  }
  if (enrollment && hasTotp) {
    showEnrollment("recovery");
    $("logout").hidden = true;
  } else signIn();
}));

$("boot-retry").addEventListener("click", () => run(boot));
$("policy-refresh").addEventListener("click", () => run(refresh));
run(boot);
