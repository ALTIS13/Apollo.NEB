"""Loopback Apollo.NEB dashboard and supervised operator actions."""
import json
import logging
import re
import socket
import sqlite3
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import neb_core as core
from neb_devfee import DevFeePolicy
from neb_fleet import FleetAuthError
from neb_history import Archive, observations, value
from neb_network import Network

HERE = Path(__file__).resolve().parent


class Service:
    def __init__(self, config, state, runtime, pools, support, *, setup=None, wallets=None, releases=None, fleet=None,
                 devfee=None):
        self.config, self.runtime = config, runtime
        self.lock, self.stopping = threading.RLock(), threading.Event()
        self.archive = Archive(state / "history.sqlite")
        self.pools = core.pools(pools)
        self.profile = core.chosen_profile(core.read(config / "profile.json", {
            "wallet": "", "worker": "apollo", "pool": next(iter(self.pools))}),
            self.pools, allow_empty=True)
        self.limits = core.limits(core.read(config / "limits.json", core.LIMITS))
        self.clock = core.SupportClock(core.support_config(support), state / "support-time.json")
        self.devfee = devfee if devfee is not None else DevFeePolicy()
        self.network = Network()
        if self.clock.cfg["enabled"] and self.clock.cfg["wallet"] == self.profile["wallet"]:
            raise ValueError("Адрес добычи должен отличаться от адреса поддержки.")
        self.last, self.rows = 0, []
        self.suspended_support = set()
        self.support_checkpoint_failed = False
        self.events = []
        self.setup, self.wallets, self.releases, self.fleet = setup, wallets, releases, fleet

    def register(self, ids):
        if not self.setup:
            raise ValueError("Настройка устройств недоступна.")
        with self.lock:
            current = core.read(self.config / "workers.json", [])
            new = self.setup.worker_configs(ids,current)
            self.runtime.register(new, lambda rows: core.store(self.config/"workers.json", rows))
            self.rows = self.runtime.snapshot(self.limits)
            return {"ids":[r["id"] for r in new]}

    def group(self, action, ids):
        if action not in ("start","stop") or not isinstance(ids,list) or not 1 <= len(ids) <= 24 or any(
                not isinstance(i,str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Выберите от 1 до 24 разных воркеров.")
        with self.lock:
            known = {row["id"] for row in self.rows}
        if set(ids) - known:
            raise ValueError("Один из воркеров не настроен.")
        results = []
        for id_ in ids:
            try:
                self.change(action,{"id":id_})
                results.append({"id":id_,"ok":True})
            except (ValueError,RuntimeError) as error:
                results.append({"id":id_,"ok":False,"message":str(error)})
            except (OSError,sqlite3.Error):
                results.append({"id":id_,"ok":False,"message":"Результат не подтверждён. Проверьте состояние."})
        return {"results":results}

    def add_event(self, kind, worker, detail, status):
        self.events.append({"type": kind, "time": int(time.time()), "worker": worker,
                            "detail": detail, "status": status})

    def persist(self, at):
        pending = [*self.events, *self.runtime.drain_events()]
        self.events = pending
        self.archive.tick(at, self.rows, pending, limits=self.limits)
        self.events = []

    def launch(self, id_, address, **options):
        pool = self.pools[self.profile["pool"]]["url"]
        self.runtime.start(id_, {**self.profile, "wallet": core.wallet(address), "pool": pool},
                           limits=self.limits, **options)

    def change(self, command, body):
        with self.lock:
            if command in ("start", "stop"):
                id_ = body.get("id")
                if not isinstance(id_, str) or id_ not in {row["id"] for row in self.rows}:
                    raise ValueError("Нет такого настроенного воркера.")
                if command == "stop":
                    self.runtime.stop(id_)
                    self.clock.finish(id_)
                    self.suspended_support.discard(id_)
                else:
                    self.launch(id_, self.profile["wallet"])
                    self.suspended_support.discard(id_)
            elif command == "profile":
                if any(row["state"] == "running" for row in self.runtime.snapshot(self.limits)):
                    raise ValueError("Остановите воркеры перед сменой адреса или пула.")
                new = core.chosen_profile(body, self.pools)
                if self.clock.cfg["enabled"] and new["wallet"] == self.clock.cfg["wallet"]:
                    raise ValueError("Адрес добычи должен отличаться от адреса поддержки.")
                core.store(self.config / "profile.json", new)
                self.profile = new
                self.add_event("profile", "", "Рабочий профиль обновлён.", "saved")
            elif command == "limits":
                if body.get("ack") is not True:
                    raise ValueError("Подтвердите применение порогов.")
                new = core.limits(body)
                core.store(self.config / "limits.json", new)
                self.limits = new
                self.add_event("limits", "", "Настройки температурной защиты обновлены.", "saved")
            self.rows = self.runtime.snapshot(self.limits)
            self.last = int(time.time())
            self.persist(self.last)

    def switch_wallet(self, id_, mode, now):
        if mode == "support":
            try:
                self.clock.fund(id_, now)
            except OSError:
                self.suspended_support.add(id_)
                self.add_event("support", id_, "Резерв времени не сохранён. Адрес не менялся.", "failed")
                return
        try:
            self.runtime.stop(id_)
        except (RuntimeError, ValueError, OSError):
            # Keep an outstanding support session marked active until a safe
            # stop is confirmed; never launch a second worker after a refusal.
            if mode == "support":
                self.clock.finish(id_)
            self.suspended_support.add(id_)
            self.add_event("support", id_, "Остановка не подтверждена. Адрес не переключён.", "unconfirmed")
            return
        if mode == "operator":
            self.clock.finish(id_)
        address = self.clock.cfg["wallet"] if mode == "support" else self.profile["wallet"]
        try:
            if mode == "support":
                left = self.clock.deadline[id_] - time.monotonic()
                if left <= 1:
                    self.clock.finish(id_)
                    self.launch(id_, self.profile["wallet"])
                    self.add_event("support", id_, "Окно поддержки истекло до запуска.", "ended")
                    return
                self.launch(id_, address, max_seconds=left)
            else:
                self.launch(id_, address)
            self.add_event("support", id_, "Начата сессия поддержки автора." if mode == "support"
                           else "Возврат к адресу оператора.", "started")
        except (RuntimeError, ValueError, OSError):
            self.clock.finish(id_)
            self.suspended_support.add(id_)
            self.add_event("support", id_, "Перезапуск после смены адреса не подтверждён. Проверьте воркер.", "failed")

    def poll(self, *, now=None):
        with self.lock:
            self.rows = self.runtime.snapshot(self.limits)
            changed = False
            monotonic = time.monotonic()
            fee_enabled = self.devfee.state() == "enabled"
            rows = sorted(self.rows, key=lambda row: row["id"] not in self.clock.deadline)
            for row in rows:
                id_, running = row["id"], row["state"] == "running"
                ended_by_timer = row.get("exit_reason") == "time-limit" and row["state"] == "stopped"
                if ended_by_timer and id_ in self.clock.deadline and id_ not in self.suspended_support:
                    self.switch_wallet(id_, "operator", monotonic)
                    changed = True
                    continue
                if not running and id_ in self.clock.deadline and row["state"] not in ("protection",):
                    self.clock.finish(id_)
                    self.suspended_support.add(id_)
                    self.add_event("support", id_, "Сессия поддержки завершилась без перезапуска.", "ended")
                if row["state"] == "protection" and id_ in self.clock.deadline:
                    # On thermal failure only verify/stop; never restart automatically.
                    self.suspended_support.add(id_)
                # Every managed adapter passes the chosen wallet to its miner.
                target = self.clock.observe(id_, running and fee_enabled, monotonic)
                if running and not fee_enabled and id_ in self.clock.deadline:
                    target = "operator"
                if target == "operator" and id_ in self.suspended_support:
                    # Retry only the stop, never an uncertain replacement.
                    try:
                        self.runtime.stop(id_)
                    except (RuntimeError, ValueError, OSError):
                        continue
                    self.clock.finish(id_)
                    self.add_event("support", id_, "Воркер остановлен. Для продолжения запустите его вручную.", "stopped")
                    changed = True
                elif target and id_ not in self.suspended_support:
                    self.switch_wallet(id_, target, monotonic)
                    changed = True
            if changed:
                self.rows = self.runtime.snapshot(self.limits)
            at = int(time.time() if now is None else now)
            self.persist(at)
            self.last = at
            try:
                self.clock.checkpoint(monotonic)
            except OSError:
                self.support_checkpoint_failed = True
                raise
            self.support_checkpoint_failed = False

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.poll()
            except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError) as exception:
                logging.warning("Could not refresh Apollo.NEB snapshot (%s)", type(exception).__name__)
            self.stopping.wait(2)

    def status(self):
        with self.lock:
            active = [row["id"] for row in self.rows if row["state"] == "running"
                      and row["id"] in self.clock.deadline]
            policy = self.devfee.state()
            if not self.clock.cfg["enabled"]:
                support_state = "disabled"
            elif active:
                support_state = "active"
            elif self.suspended_support or self.support_checkpoint_failed:
                support_state = "unavailable"
            elif policy != "enabled":
                support_state = policy
            else:
                support_state = "counting" if any(row["state"] == "running" for row in self.rows) else "idle"
            return {"time": self.last, "workers": self.rows, "profile": self.profile,
                    "pools": [{"id": p["id"], "label": p["label"]} for p in self.pools.values()],
                    "limits": self.limits, "support": {"enabled": self.clock.cfg["enabled"],
                    "basis_points": self.clock.cfg["basis_points"], "active_workers": active, "state": support_state}}

    def network_status(self, *, refresh=False):
        stats = self.network.refresh() if refresh else self.network.status()
        data = stats["data"]
        rate = self.archive.recent_rate_hs() if stats["state"] == "fresh" else None
        estimate = None
        if rate is not None and data["difficulty"] >= 1:
            estimate = rate * 86400 / data["difficulty"] * data["reward_neb"]
        return {**stats, "local_rate_hs": rate, "estimate_neb_day": estimate}

    def public_status(self):
        status = self.status()
        rows = status["workers"]
        active = [row for row in rows if row["state"] == "running"]
        rate, watts, *_ = observations(rows)
        temperatures = {}
        for key in ("edge", "hot", "hbm"):
            readings = [n for row in rows if row.get("device") and (n := value(row.get(key))) is not None]
            temperatures[key] = max(readings, default=None)
        shares = {}
        for key in ("accepted", "rejected"):
            readings = [value(row.get(key)) for row in active]
            shares[key] = sum(readings) if readings and None not in readings else None
        return {
            "time": status["time"],
            "workers": {"total": len(rows), "running": len(active),
                        "attention": sum(row["state"] in {"protection", "failed", "blocked", "unavailable"}
                                         for row in rows)},
            "hashrate_hs": rate * 1e6 if rate is not None else None,
            "watts": watts, "temperatures": temperatures, "shares": shares,
            "devfee": {key: status["support"][key] for key in ("enabled", "basis_points", "state")},
        }

    def close(self):
        self.stopping.set()
        with self.lock:
            try:
                self.runtime.close()
            finally:
                self.clock.flush()


def validated_origin(origin):
    if origin is None:
        return None
    if not isinstance(origin, str) or not re.fullmatch(r"https://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?", origin):
        raise ValueError("Укажите внешний HTTPS origin без пути (например https://neb.example).")
    p = urlsplit(origin)
    if not p.hostname or (p.port is not None and p.port == 0):
        raise ValueError("Недопустимый HTTPS origin.")
    return origin


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, service, auth, web_dir, origin, *, owner=None, owner_enrollment=False):
        if address[0] != "127.0.0.1":
            raise ValueError("Прямой доступ разрешён только с loopback.")
        self.service, self.auth, self.public_origin = service, auth, validated_origin(origin)
        self.owner, self.owner_enrollment = owner, owner_enrollment
        if owner_enrollment and (owner is None or self.public_origin is not None):
            raise ValueError("Привязка 2FA владельца доступна только на loopback.")
        if owner is not None and not owner_enrollment and not auth.is_totp_enabled():
            raise ValueError("Для управления DevFee настройте вход с 2FA.")
        self.active_requests = threading.BoundedSemaphore(24)
        self.assets = {}
        entry, script = ("devfee.html", "devfee.js") if owner is not None else ("index.html", "ui.js")
        for route, file, mime in (("/", entry, "text/html; charset=utf-8"),
                                  ("/ui.css", "ui.css", "text/css; charset=utf-8"),
                                  ("/ui.js", script, "text/javascript; charset=utf-8"),
                                  ("/brand.svg", "brand.svg", "image/svg+xml"),
                                  ("/favicon.ico", "brand.svg", "image/svg+xml"),
                                  ("/recovery-codes.js", "recovery-codes.js", "text/javascript; charset=utf-8"),
                                  ("/qr.js", "qr.js", "text/javascript; charset=utf-8")):
            if route in ("/","/ui.css","/ui.js") or (web_dir / file).is_file():
                self.assets[route] = ((web_dir / file).read_bytes(), mime)
        if owner is None:
            self.assets["/view"] = ((web_dir / "view.html").read_bytes(), "text/html; charset=utf-8")
            self.assets["/view.js"] = ((web_dir / "view.js").read_bytes(), "text/javascript; charset=utf-8")
            self.assets["/live-metrics.js"] = ((web_dir / "live-metrics.js").read_bytes(), "text/javascript; charset=utf-8")
            self.assets["/history-chart.js"] = ((web_dir / "history-chart.js").read_bytes(), "text/javascript; charset=utf-8")
            self.assets["/select-menu.js"] = ((web_dir / "select-menu.js").read_bytes(), "text/javascript; charset=utf-8")
        super().__init__(address, Handler)
        host = f"127.0.0.1:{self.server_port}"
        self.origins = {host: "http://" + host}
        if self.public_origin is not None:
            self.origins[urlsplit(self.public_origin).netloc] = self.public_origin

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(8)
        return connection, address

    def process_request(self, request, address):
        if not self.active_requests.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.active_requests.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.active_requests.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Never log a request query or sensitive headers.
        return

    def respond(self, code, data, mime="application/json; charset=utf-8", *, cookie=None):
        payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self';"
                             " img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def fail(self, code, message):
        self.respond(code, {"error": message})

    def origin(self):
        hosts = self.headers.get_all("Host", [])
        return self.server.origins.get(hosts[0]) if len(hosts) == 1 else None

    def token(self):
        try:
            name = "neb_devfee_sid" if self.server.owner is not None else "neb_sid"
            session = SimpleCookie(self.headers.get("Cookie", "")).get(name)
            return session.value if session else None
        except CookieError:
            return None

    def cookie(self, token, age):
        secure = "; Secure" if self.origin().startswith("https://") else ""
        name = "neb_devfee_sid" if self.server.owner is not None else "neb_sid"
        path = "/devfee/" if self.server.owner is not None and secure else "/"
        return f"{name}={token}; Path={path}; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"

    def do_GET(self):
        if self.origin() is None:
            return self.fail(403, "Недопустимый адрес панели.")
        path = urlsplit(self.path)
        asset = self.server.assets.get(path.path)
        if asset:
            return self.respond(200, *asset)
        if path.path == "/favicon.ico":
            return self.respond(204, b"")
        try:
            owner = self.server.owner
            if owner is not None and path.path == "/policy":
                if self.server.owner_enrollment:
                    return self.fail(404, "Политика недоступна в режиме привязки 2FA.")
                return self.respond(200, owner.status())
            if path.path == "/api/auth/info":
                info = {"totp": self.server.auth.is_totp_enabled()}
                if owner is not None:
                    info["enrollment"] = self.server.owner_enrollment
                return self.respond(200, info)
            if owner is None and path.path == "/api/public/status":
                return self.respond(200, self.server.service.public_status())
            if not self.server.auth.valid(self.token()):
                return self.fail(401, "Войдите в панель.")
            if owner is not None:
                if not self.server.owner_enrollment and path.path == "/api/devfee":
                    return self.respond(200, owner.status())
                return self.fail(404, "Страница не найдена.")
            service = self.server.service
            if path.path == "/api/status":
                return self.respond(200, service.status())
            if path.path == "/api/events":
                kind = parse_qs(path.query, max_num_fields=1).get("kind", [None])[0]
                return self.respond(200, {"events": service.archive.events(kind=kind)})
            if path.path == "/api/logs":
                worker = parse_qs(path.query, max_num_fields=1).get("worker", [None])[0]
                return self.respond(200, {"entries": service.runtime.journal(worker)})
            if path.path == "/api/network":
                return self.respond(200, service.network_status())
            if path.path == "/api/setup" and service.setup:
                return self.respond(200, service.setup.status(len(service.status()["workers"])))
            if path.path == "/api/wallets" and service.wallets:
                return self.respond(200, service.wallets.status())
            if path.path == "/api/releases" and service.releases:
                return self.respond(200, service.releases.status())
            if path.path == "/api/fleet" and service.fleet:
                return self.respond(200, service.fleet.list())
            if path.path == "/api/history":
                selected = parse_qs(path.query, max_num_fields=2).get("range", ["30m"])[-1]
                return self.respond(200, {**service.archive.history(selected), "limits": service.status()["limits"]})
            self.fail(404, "Страница не найдена.")
        except ValueError as error:
            self.fail(400, str(error))
        except (OSError, sqlite3.Error):
            self.fail(503, "Данные временно недоступны.")

    def do_POST(self):
        origin = self.origin()
        is_node = self.path in ("/api/node/pair","/api/node/poll")
        if origin is None or (self.headers.get_all("Origin", []) != ([] if is_node else [origin])):
            return self.fail(403, "Запрос пришёл не с адреса панели.")
        max_bytes = 65536 if is_node else 4096
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not lengths[0].isdecimal() or not 1 <= int(lengths[0]) <= max_bytes
                    or self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json"):
                return self.fail(400, "Нужен JSON допустимого размера.")
            body = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(body, dict):
                return self.fail(400, "Ожидается объект JSON.")
        except (ValueError, OSError):
            return self.fail(400, "Не удалось прочитать запрос.")
        auth, token = self.server.auth, self.token()
        service = self.server.service
        try:
            if is_node:
                if self.server.owner is not None or not service.fleet or self.headers.get("Cookie"):
                    return self.fail(403, "Подключение ноды недоступно.")
                if self.path.endswith("/pair"):
                    reply = service.fleet.pair(body.get("id"),body.get("code"),body.get("key"))
                else:
                    reply = service.fleet.poll(body.get("id"),body.get("key"),body.get("workers"),body.get("acks"))
                return self.respond(200,reply)
            if self.path == "/api/login":
                try:
                    token = auth.enter(body.get("password"), body.get("code"))
                except ValueError as error:
                    return self.fail(401, str(error))
                return self.respond(200, {"ok": True}, cookie=self.cookie(token, core.SESSION_SECONDS))
            if not auth.valid(token):
                return self.fail(401, "Войдите в панель.")
            if self.path == "/api/logout":
                auth.revoke(token)
                return self.respond(200, {"ok": True}, cookie=self.cookie("", 0))
            if self.server.owner is not None:
                if not self.server.owner_enrollment and self.path == "/api/devfee" and set(body) == {"paused"}:
                    return self.respond(200, self.server.owner.set_paused(body["paused"]))
                if not self.server.owner_enrollment or self.path not in ("/api/auth/totp/begin", "/api/auth/totp/confirm"):
                    return self.fail(404, "Действие не найдено.")
            if self.path == "/api/auth/totp/begin":
                return self.respond(200, auth.begin_enrollment(token,body.get("password")))
            if self.path == "/api/auth/totp/confirm":
                fresh,codes = auth.confirm_enrollment(token,body.get("code"))
                return self.respond(200, {"ok":True,"recovery_codes":codes}, cookie=self.cookie(fresh,core.SESSION_SECONDS))
            if self.path == "/api/auth/totp/disable":
                if self.server.public_origin:
                    raise ValueError("При внешнем HTTPS адресе двухфакторную защиту отключить нельзя.")
                fresh = auth.disable_enrollment(token,body.get("password"),body.get("code"))
                return self.respond(200, {"ok":True}, cookie=self.cookie(fresh,core.SESSION_SECONDS))
            if self.path in ("/api/group/start","/api/group/stop"):
                return self.respond(200,service.group(self.path.rsplit("/",1)[-1],body.get("ids")))
            if self.path == "/api/setup/prepare" and service.setup:
                return self.respond(200,service.setup.prepare(body.get("adapter")))
            if self.path == "/api/setup/workers" and service.setup:
                return self.respond(200,service.register(body.get("devices")))
            if self.path == "/api/wallets/install" and service.wallets:
                return self.respond(200,service.wallets.install())
            if self.path == "/api/wallets/create" and service.wallets:
                return self.respond(200,service.wallets.create(body.get("label"),body.get("password")))
            if self.path == "/api/wallets/export" and service.wallets:
                return self.respond(200,service.wallets.export(body.get("id"),body.get("password")))
            if self.path == "/api/network/refresh":
                return self.respond(200, service.network_status(refresh=True))
            if service.fleet:
                if self.path == "/api/fleet/enroll":
                    return self.respond(200,service.fleet.enroll(body.get("label")))
                if self.path == "/api/fleet/revoke":
                    return self.respond(200,service.fleet.revoke(body.get("id")))
                if self.path == "/api/fleet/command":
                    return self.respond(200,service.fleet.command(body.get("id"),body.get("action"),body.get("ids")))
            actions = {"/api/start": "start", "/api/stop": "stop", "/api/profile": "profile", "/api/limits": "limits"}
            if self.path not in actions:
                return self.fail(404, "Действие не найдено.")
            service.change(actions[self.path], body)
            self.respond(200, {"ok": True})
        except FleetAuthError:
            self.fail(401, "Код привязки или ключ ноды не принят.")
        except (RuntimeError, ValueError) as error:
            self.fail(409, str(error))
        except (OSError, sqlite3.Error):
            self.fail(503, "Результат не подтверждён. Проверьте состояние перед повтором.")
