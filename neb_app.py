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
from neb_fleet import FleetAuthError, NODE_ID
from neb_history import Archive, SAMPLE, observations, value
from neb_network import Network
from neb_overview import summarize
from neb_release import VERSION, channel_value, read_channel

HERE = Path(__file__).resolve().parent


class Service:
    def __init__(self, config, state, runtime, pools, support, *, setup=None, releases=None, fleet=None,
                 devfee=None, controller_only=False):
        self.config, self.runtime = config, runtime
        self.controller_only = controller_only
        self.lock, self.stopping = threading.RLock(), threading.Event()
        self.upgrading = False
        self.archive = Archive(state / "history.sqlite")
        self.farm_archive = Archive(state / "farm-history.sqlite")
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
        self.setup, self.releases, self.fleet = setup, releases, fleet
        self.remote_install = None
        self.atlas = None

    def register(self, ids):
        if not self.setup:
            raise ValueError("Настройка устройств недоступна.")
        with self.lock:
            if self.upgrading:
                raise ValueError("На ноде выполняется обновление.")
            current = core.read(self.config / "workers.json", [])
            new = self.setup.worker_configs(ids,current)
            self.runtime.register(new, lambda rows: core.store(self.config/"workers.json", rows))
            self.rows = self.runtime.snapshot(self.limits)
            return {"ids":[r["id"] for r in new]}

    def group(self, action, ids):
        if self.controller_only:
            raise ValueError("Для управления воркерами откройте раздел Ноды.")
        if action not in ("start","stop","restart") or not isinstance(ids,list) or not 1 <= len(ids) <= 24 or any(
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
        if self.controller_only:
            raise ValueError("Для управления воркерами откройте раздел Ноды.")
        with self.lock:
            if self.upgrading and command != "stop":
                raise ValueError("На ноде выполняется обновление.")
            if command in ("start", "stop", "restart"):
                id_ = body.get("id")
                if not isinstance(id_, str) or id_ not in {row["id"] for row in self.rows}:
                    raise ValueError("Нет такого настроенного воркера.")
                if command == "restart" and not any(row["id"] == id_ and row["state"] == "running"
                                                    for row in self.runtime.snapshot(self.limits)):
                    raise ValueError("Перезапуск доступен только для работающего воркера.")
                if command != "start":
                    self.runtime.stop(id_)
                    self.clock.finish(id_)
                    self.suspended_support.discard(id_)
                if command != "stop":
                    self.launch(id_, self.profile["wallet"])
                    self.suspended_support.discard(id_)
            elif command == "profile":
                if any(row["state"] == "running" for row in self.runtime.snapshot(self.limits)):
                    raise ValueError("Остановите воркеры перед сменой адреса или пула.")
                new = core.chosen_profile({"wallet": self.profile["wallet"], **body}, self.pools)
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

    def farm_history_loop(self):
        while not self.stopping.is_set():
            try:
                atlas = self.atlas.snapshot() if self.atlas else None
                overview = self.overview(atlas=atlas)
                if overview["nodes"]["total"] > int(not self.controller_only) or self.farm_archive.has_samples():
                    self.farm_archive.farm_sample(int(time.time()), overview)
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                logging.warning("Could not record Apollo.NEB farm history")
            self.stopping.wait(SAMPLE)

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
            profile = {"worker": self.profile["worker"], "pool": self.profile["pool"],
                       "wallet_configured": bool(self.profile["wallet"])}
            return {"time": self.last, "workers": self.rows, "profile": profile,
                    "capabilities": {"local_workers": not self.controller_only, "wallet_tools": False},
                    "pools": [{"id": p["id"], "label": p["label"]} for p in self.pools.values()],
                    "limits": self.limits, "support": {"enabled": self.clock.cfg["enabled"],
                    "basis_points": self.clock.cfg["basis_points"], "active_workers": active, "state": support_state}}

    def network_status(self, *, refresh=False):
        stats = self.network.refresh() if refresh else self.network.status()
        data = stats["data"]
        rate = estimate = None
        if stats["state"] == "fresh":
            atlas = self.atlas.snapshot() if self.atlas else None
            rate = self.overview(atlas=atlas)["hashrate_hs"]
            if rate is not None and data["difficulty"] >= 1:
                estimate = rate * 86400 / data["difficulty"] * data["reward_neb"]
        return {**stats, "farm_rate_hs": rate, "estimate_neb_day": estimate,
                "local_rate_hs": self.archive.recent_rate_hs() if stats["state"] == "fresh" else None}

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

    def overview(self, status=None, *, atlas=None):
        if status is None:
            status = self.status()
        now = int(time.time())
        fleet = self.fleet.observations(now=now) if self.fleet else {}
        return {**summarize(status, fleet, now=now, atlas=atlas),
                "devfee": {key: status["support"][key] for key in ("enabled", "basis_points", "state")}}

    def runtime_status(self):
        with self.lock:
            return {"version": VERSION, "idle": bool(self.last) and self.runtime.is_idle()}

    def reserve_upgrade(self):
        with self.lock:
            if self.controller_only or self.upgrading or self.stopping.is_set() or not self.runtime_status()["idle"]:
                return False
            self.upgrading = True
            return True

    def finish_upgrade(self):
        with self.lock:
            self.upgrading = False

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

    def __init__(self, address, service, auth, web_dir, origin, *, handler=None):
        if address[0] != "127.0.0.1":
            raise ValueError("Прямой доступ разрешён только с loopback.")
        self.service, self.auth, self.public_origin = service, auth, validated_origin(origin)
        self.active_requests = threading.BoundedSemaphore(24)
        self.assets = {}
        for route, file, mime in (("/", "index.html", "text/html; charset=utf-8"),
                                  ("/ui.css", "ui.css", "text/css; charset=utf-8"),
                                  ("/ui.js", "ui.js", "text/javascript; charset=utf-8"),
                                  ("/brand.svg", "brand.svg", "image/svg+xml"),
                                  ("/favicon.ico", "brand.svg", "image/svg+xml"),
                                  ("/recovery-codes.js", "recovery-codes.js", "text/javascript; charset=utf-8"),
                                  ("/qr.js", "qr.js", "text/javascript; charset=utf-8")):
            if route in ("/","/ui.css","/ui.js") or (web_dir / file).is_file():
                self.assets[route] = ((web_dir / file).read_bytes(), mime)
        self.assets["/view"] = self.assets["/"]
        self.assets["/view.js"] = ((web_dir / "view.js").read_bytes(), "text/javascript; charset=utf-8")
        self.assets["/live-metrics.js"] = ((web_dir / "live-metrics.js").read_bytes(), "text/javascript; charset=utf-8")
        self.assets["/history-chart.js"] = ((web_dir / "history-chart.js").read_bytes(), "text/javascript; charset=utf-8")
        self.assets["/select-menu.js"] = ((web_dir / "select-menu.js").read_bytes(), "text/javascript; charset=utf-8")
        super().__init__(address, handler or Handler)
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
            session = SimpleCookie(self.headers.get("Cookie", "")).get("neb_sid")
            return session.value if session else None
        except CookieError:
            return None

    def cookie(self, token, age):
        secure = "; Secure" if self.origin().startswith("https://") else ""
        return f"neb_sid={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"

    def node_releases(self, query):
        args = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=3)
        if (args.keys() - {"node", "channel", "check"} or "node" not in args
                or any(len(values) != 1 for values in args.values())
                or ("check" in args and args["check"] != ["0"]) or not NODE_ID.fullmatch(args["node"][0])):
            return self.fail(400, "Укажите ноду и канал обновлений.")
        service = self.server.service
        channel = channel_value(args["channel"][0]) if "channel" in args else read_channel(service.config)
        target = service.fleet.release_target(args["node"][0])
        if target is None:
            return self.fail(409, "Нужен свежий отчёт ноды о версии и системе.")
        platform = "windows-desktop" if target["platform"] == "windows" else target["platform"]
        data = {"channel": channel, "current": target["version"], "platform": platform,
                "kernel": target["kernel"], "can_apply": False,
                "can_queue": target.get("upgrade") is True, "state": "unchecked"}
        if "check" not in args:
            try:
                versions = service.releases.versions(service.config, current=target["version"], channel=channel,
                                                     platform=platform, kernel=target["kernel"])
            except ValueError as error:
                data.update(state="unavailable", message=str(error))
            else:
                data.update(versions=versions, state="available" if versions else "current")
        return self.respond(200, data)

    def queue_node_upgrade(self, body):
        if body.keys() != {"node", "current", "tag", "channel", "sha256"} or any(
            not isinstance(value, str) for value in body.values()
        ):
            return self.fail(400, "Укажите ноду, текущую и выбранную версии, канал и SHA-256.")
        service = self.server.service
        target = service.fleet.release_target(body["node"])
        if not target or target.get("upgrade") is not True or target["version"] != body["current"]:
            return self.fail(409, "Версия или доступность ноды изменилась. Проверьте выпуски снова.")
        candidates = service.releases.versions(service.config, current=target["version"], channel=body["channel"],
                                               platform=target["platform"], kernel=target["kernel"])
        if not any(candidate["tag"] == body["tag"] and candidate["asset"]["sha256"] == body["sha256"]
                   for candidate in candidates):
            return self.fail(409, "Выбранный пакет больше не подтверждается каталогом.")
        return self.respond(202, service.fleet.queue_upgrade(
            body["node"], current=body["current"], tag=body["tag"], sha256=body["sha256"], channel=body["channel"]))

    def install_releases(self, query, installer):
        args = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=3)
        if (args.keys() - {"target", "channel", "check"} or "target" not in args
                or any(len(values) != 1 for values in args.values())
                or ("check" in args and args["check"] != ["0"])):
            return self.fail(400, "Укажите подготовленную ноду и канал обновлений.")
        config = self.server.service.config
        channel = channel_value(args["channel"][0]) if "channel" in args else read_channel(config)
        check = "check" not in args
        target, versions = installer.catalog(config, args["target"][0], channel, check=check)
        return self.respond(200, {"channel": channel, "platform": "linux", "kernel": target["kernel"],
                                  "current": "Готова к установке", "can_queue": True, "versions": versions,
                                  "state": ("available" if versions else "current") if check else "unchecked"})

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
            if path.path == "/api/auth/info":
                return self.respond(200, {"totp": self.server.auth.is_totp_enabled()})
            if path.path == "/api/public/status":
                return self.respond(200, self.server.service.public_status())
            if path.path == "/api/public/overview":
                return self.respond(200, self.server.service.overview())
            if path.path == "/api/runtime" and self.origin() == f"http://127.0.0.1:{self.server.server_port}":
                return self.respond(200, self.server.service.runtime_status())
            if not self.server.auth.valid(self.token()):
                return self.fail(401, "Войдите в панель.")
            service = self.server.service
            if path.path == "/api/status":
                status = service.status()
                atlas = service.atlas.snapshot() if service.atlas else None
                status["capabilities"]["farm_history"] = service.farm_archive.has_samples()
                return self.respond(200, {**status, "overview": service.overview(status, atlas=atlas), "atlas": atlas})
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
            if path.path == "/api/releases" and service.releases:
                if path.query not in ("", "check=0"):
                    return self.fail(400, "Неизвестный режим проверки релизов.")
                releases = service.releases.status(service.config, check=not path.query)
                if releases["state"] in ("current", "available"):
                    releases["versions"] = service.releases.versions(service.config, channel=releases["channel"])
                return self.respond(200, releases)
            if path.path == "/api/fleet" and service.fleet:
                fleet = service.fleet.list()
                installer = getattr(service, "remote_install", None)
                if installer:
                    fleet["install_targets"] = installer.list()
                return self.respond(200, fleet)
            if path.path == "/api/fleet/releases" and service.fleet and service.releases:
                return self.node_releases(path.query)
            if path.path == "/api/fleet/install/releases":
                installer = getattr(service, "remote_install", None)
                if installer:
                    return self.install_releases(path.query, installer)
            if path.path == "/api/history":
                query = parse_qs(path.query, strict_parsing=True, keep_blank_values=True, max_num_fields=2)
                if query.keys() - {"source", "range"} or any(len(values) != 1 for values in query.values()):
                    return self.fail(400, "Укажите один источник и период.")
                selected = query.get("range", ["30m"])[0]
                source = query.get("source", ["local"])[0]
                if source == "farm":
                    return self.respond(200, {**service.farm_archive.history(selected), "source": "farm"})
                if source == "atlas":
                    if not service.atlas:
                        return self.fail(404, "Архив Atlas не подключён.")
                    archive = service.atlas.history(selected)
                    if archive is None:
                        return self.fail(503, "Архив Atlas временно недоступен.")
                    return self.respond(200, {**archive, "source": "atlas"})
                if source != "local":
                    return self.fail(400, "Неизвестный источник истории.")
                return self.respond(200, {**service.archive.history(selected), "limits": service.status()["limits"]})
            self.fail(404, "Страница не найдена.")
        except ValueError as error:
            self.fail(400, str(error))
        except (OSError, sqlite3.Error):
            self.fail(503, "Данные временно недоступны.")

    def do_POST(self):
        origin = self.origin()
        is_node = self.path in ("/api/node/pair", "/api/node/poll", "/api/node/check", "/api/node/upgrade", "/api/node/power")
        if origin is None or (self.headers.get_all("Origin", []) != ([] if is_node else [origin])):
            return self.fail(403, "Запрос пришёл не с адреса панели.")
        if self.path in ("/api/node/power", "/api/fleet/power") and not origin.startswith("https://"):
            return self.fail(403, "Управление мощностью GPU требует HTTPS.")
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
                if not service.fleet or self.headers.get("Cookie"):
                    return self.fail(403, "Подключение ноды недоступно.")
                if self.path.endswith("/pair"):
                    reply = service.fleet.pair(body.get("id"),body.get("code"),body.get("key"))
                elif self.path.endswith("/check"):
                    if body.keys() != {"id", "key"}:
                        return self.fail(400, "Неверный запрос проверки связи.")
                    reply = service.fleet.check(body.get("id"), body.get("key"))
                elif self.path.endswith("/upgrade"):
                    if body.keys() != {"id", "key", "receipt"}:
                        return self.fail(400, "Неверный запрос обновления ноды.")
                    reply = service.fleet.upgrade_poll(body["id"], body["key"], body["receipt"])
                elif self.path.endswith("/power"):
                    if body.keys() != {"id", "key", "receipt"}:
                        return self.fail(400, "Неверный запрос мощности GPU.")
                    reply = service.fleet.power_poll(body["id"], body["key"], body["receipt"])
                else:
                    reply = service.fleet.poll(body.get("id"),body.get("key"),body.get("workers"),body.get("acks"),
                                               details=body.get("details"), release=body.get("release"),
                                               hardware=body.get("hardware"))
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
            if self.path == "/api/releases/channel" and service.releases:
                if set(body) != {"channel"}:
                    return self.fail(400, "Ожидается только канал обновлений.")
                return self.respond(200, service.releases.set_channel(service.config, body["channel"]))
            if self.path == "/api/network/refresh":
                return self.respond(200, service.network_status(refresh=True))
            if service.fleet:
                if self.path == "/api/fleet/power":
                    if body.keys() != {"node", "gpu", "watts"}:
                        return self.fail(400, "Укажите ноду, GPU и предел мощности.")
                    return self.respond(202, service.fleet.queue_power(body["node"], body["gpu"], body["watts"]))
                if self.path == "/api/fleet/install" and getattr(service, "remote_install", None):
                    if body.keys() != {"target", "channel", "tag", "sha256"} or any(
                        not isinstance(value, str) for value in body.values()
                    ):
                        return self.fail(400, "Укажите ноду и выбранный пакет.")
                    return self.respond(202, service.remote_install.start(service.config, body["target"],
                                                                         body["channel"], body["tag"], body["sha256"]))
                if self.path == "/api/fleet/upgrade" and service.releases:
                    return self.queue_node_upgrade(body)
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
