#!/usr/bin/env python3
"""Apollo.NEB local installer configuration and supervisor entrypoint."""
import argparse
import base64
import hashlib
import getpass
import logging
import json
import os
import secrets
import signal
import stat
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from contextlib import closing

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import neb_app
import neb_core as core
import neb_runtime
import neb_setup
import neb_release
import neb_wallet
from neb_release import VERSION


def private(path):
    if not path.is_absolute() or path != path.resolve() or path in (Path.home(), *Path.home().parents):
        raise ValueError("Укажите абсолютный путь к приватному каталогу, не ссылку.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = path.stat()
    if meta.st_uid != os.geteuid() or not stat.S_ISDIR(meta.st_mode) or meta.st_mode & 0o077:
        raise ValueError("Нужен отдельный каталог текущего пользователя с правами 0700. Существующие права не изменены.")
    return path


def paths(args):
    cfg = args.config_dir or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "apollo-neb"
    state = args.state_dir or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "apollo-neb"
    if (not cfg.is_absolute() or not state.is_absolute() or cfg != cfg.resolve() or state != state.resolve()
            or cfg == state or cfg in state.parents or state in cfg.parents):
        raise ValueError("Каталоги настроек и состояния должны быть отдельными абсолютными путями без ссылок.")
    return cfg, state


def settings_path(cfg):
    return cfg / "settings.json"


def configure(args):
    cfg, state = paths(args)
    target = args.publish_current
    if target is not None and (target.resolve() != target or HERE.parent != target/"lib"/"apollo-neb"
                               or not HERE.name.startswith("release.")):
        raise ValueError("Публикация допустима только из нового каталога установки.")
    with lock_state(private(state)):
        configure_locked(args, cfg, state)
        if target is not None:
            switch = HERE/".switch"
            try:
                switch.symlink_to(HERE.name)
                switch.replace(HERE.parent/"current")
            finally:
                switch.unlink(missing_ok=True)


def persisted_totp(login, path):
    """Do not migrate the live login database while preparing new package files."""
    if not path.is_file():
        return bool(login.get("totp"))
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri,uri=True,timeout=3)) as db:
            fields = {row[1] for row in db.execute("PRAGMA table_info(auth)")}
            if "totp_secret" in fields:
                value = db.execute("SELECT totp_secret FROM auth WHERE id=1").fetchone()
                if value is not None:
                    return value[0] is not None
    except sqlite3.Error:
        raise ValueError("Не удалось прочитать текущую настройку входа. База не изменена.") from None
    return bool(login.get("totp"))


def configure_locked(args, cfg, state):
    old = core.read(settings_path(cfg), {})
    raw_pools = core.read(args.pools, None) if args.pools else core.read(cfg / "pools.json", core.DEFAULT_POOLS)
    raw_workers = core.read(args.workers, None) if args.workers else core.read(cfg / "workers.json", [])
    checked_pools = core.pools(raw_pools)
    workers = neb_runtime.validate_workers(raw_workers)
    profile = core.read(cfg / "profile.json", {"wallet": "", "worker": "apollo", "pool": next(iter(checked_pools))})
    for key in ("wallet", "worker", "pool"):
        value = getattr(args, key)
        if value is not None:
            profile[key] = value
    profile = core.chosen_profile(profile, checked_pools, allow_empty=args.wallet is None)
    support = old.get("support", {"enabled": False})
    if args.disable_support:
        if args.support_wallet or args.support_bps is not None or args.accept_support:
            raise ValueError("Нельзя одновременно включить и выключить поддержку.")
        support = {"enabled": False}
    elif args.support_wallet or args.support_bps is not None or args.accept_support:
        if args.accept_support is not True:
            raise ValueError("Укажите получателя, долю и явное согласие --accept-support; см. --help.")
        recommendation = core.read(HERE / "developer-support.json", {})
        support = {"enabled": True, "wallet": args.support_wallet or recommendation.get("wallet"),
                   "basis_points": args.support_bps if args.support_bps is not None else recommendation.get("basis_points")}
    checked_support = core.support_config(support)
    if checked_support["enabled"] and checked_support["wallet"] == profile["wallet"]:
        raise ValueError("Адрес поддержки и адрес добычи не могут совпадать.")
    origin = neb_app.validated_origin(args.origin if args.origin is not None else old.get("origin"))
    port = args.port if args.port is not None else old.get("port", 18763)
    if not 1024 <= port <= 65535:
        raise ValueError("Порт должен быть в диапазоне 1024–65535.")
    limits = core.limits(core.read(cfg / "limits.json", core.LIMITS))
    login_path = cfg / "login.json"
    login = core.read(login_path, None)
    enrollment = None
    has_totp = persisted_totp(login,state/"auth.sqlite") if login is not None else args.totp
    if login is not None and args.totp and not has_totp:
        raise ValueError("Сначала включите 2FA в панели или выполните apollo-neb password --totp.")
    if origin and not has_totp:
        raise ValueError("Сначала включите 2FA. Для новой установки с внешним адресом используйте --totp.")
    if login is None:
        login, enrollment = prompt_login(args)
    private(cfg)
    if not login_path.exists():
        core.store(login_path, login)
    core.store(settings_path(cfg), {"origin": origin, "port": port, "support": support})
    core.store(cfg / "profile.json", profile)
    core.store(cfg / "pools.json", list(checked_pools.values()))
    core.store(cfg / "workers.json", workers)
    core.store(cfg / "limits.json", limits)
    print(f"Настройки: {cfg}\nАдрес панели: http://127.0.0.1:{port}\nВоркеров: {len(workers)}. Майнинг не запущен.")
    if checked_support["enabled"]:
        print(f"Поддержка автора: {checked_support['basis_points'] / 100:.2f}% времени; {checked_support['wallet']}")
    else:
        print("Поддержка автора выключена. Комиссии Apollo.NEB нет.")
    enroll(enrollment)


def prompt_login(args):
    if args.password_stdin:
        password = sys.stdin.readline(1026).rstrip("\r\n")
    else:
        password = getpass.getpass("Пароль для входа (12+ символов): ")
        if password != getpass.getpass("Повторите пароль: "):
            raise ValueError("Пароли не совпадают.")
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii") if args.totp else None
    return core.new_login(password, secret), secret


def enroll(secret):
    if secret:
        print("Добавьте запись Apollo.NEB (TOTP, 6 цифр / 30 секунд) в приложение-аутентификатор.")
        print("Ключ привязки TOTP (не пересылайте): " + secret)


def lock_state(state):
    import fcntl
    handle = (state / "service.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("Остановите запущенный экземпляр Apollo.NEB с этим каталогом состояния.") from None
    return handle


def rotate_password(args):
    cfg, state = paths(args)
    old = core.read(cfg / "login.json")
    if old is None:
        raise ValueError("Сначала выполните configure.")
    with lock_state(private(state)):
        new, enrollment = prompt_login(args)
        auth = core.Auth(old,state/"auth.sqlite")
        auth.reset_after_password_change(enrollment)
        core.store(cfg / "login.json", new)
        enroll(enrollment)
        print("Пароль обновлён. Прежние сессии отозваны.")


def supervised():
    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        if not any(line.endswith("/apollo-neb.service") for line in membership.splitlines()):
            return False
        command = subprocess.run(["systemctl", "--user", "show", "--property=KillMode",
                                  "--value", "apollo-neb.service"], capture_output=True, text=True, timeout=3)
        return command.returncode == 0 and command.stdout.strip() == "control-group"
    except (OSError, subprocess.SubprocessError):
        return False


def serve(args):
    import neb_fleet
    cfg, state = paths(args)
    login = core.read(cfg / "login.json", None)
    settings = core.read(settings_path(cfg), None)
    if login is None or settings is None:
        raise ValueError("Сначала выполните ./install.sh или apollo-neb configure.")
    port = settings["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Укажите допустимый непривилегированный порт.")
    private(cfg)
    private(state)
    with lock_state(state):
        auth = core.Auth(login, state / "auth.sqlite")
        if settings.get("origin") and not auth.is_totp_enabled():
            raise ValueError("Внешний адрес допускается только с TOTP.")
        workers = neb_runtime.validate_workers(core.read(cfg / "workers.json", []))
        runtime = neb_runtime.Manager(workers, state)
        support = settings.get("support")
        if core.support_config(support)["enabled"] and not supervised():
            print("Поддержка автора отключена вне управляемой службы systemd.", file=sys.stderr)
            support = None
        service = neb_app.Service(cfg,state,runtime,core.read(cfg/"pools.json",core.DEFAULT_POOLS),support,
                                  setup=neb_setup.Setup(state),wallets=neb_wallet.WalletStore(state),
                                  releases=neb_release.Releases(),fleet=neb_fleet.Fleet(state/"fleet.sqlite"))
        server = neb_app.Server(("127.0.0.1", port), service, auth, HERE / "web", settings.get("origin"))
        service.poll()
        background = threading.Thread(target=service.loop, name="neb-monitor", daemon=True)
        background.start()
        link = core.read(cfg/"fleet-link.json")
        if isinstance(link,dict) and link.get("paired") is True:
            follower = neb_fleet.Client(link["origin"],link["id"],link["key"],link_receipts(state,link),
                                        allow_loopback=link.get("allow_loopback") is True)
            threading.Thread(target=node_loop,args=(service,follower),daemon=True,name="neb-node").start()

        def stop(_signum, _frame):
            raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, stop)
        print(f"Apollo.NEB {VERSION}: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.4)
        except KeyboardInterrupt:
            pass
        finally:
            service.close()
            server.server_close()
            background.join(timeout=3)


def node_loop(service, follower):
    def execute(action, ids):
        result = service.group(action,ids)
        if not all(row["ok"] for row in result["results"]):
            raise RuntimeError("Результат действия подтверждён не полностью.")
    last_fault = -60.
    while not service.stopping.is_set():
        try:
            follower.cycle(service.status()["workers"],execute)
        except (ValueError,RuntimeError,OSError):
            if time.monotonic() - last_fault >= 60:
                logging.warning("Apollo.NEB could not update its remote node link.")
                last_fault = time.monotonic()
        service.stopping.wait(4)


def link_receipts(state, link):
    ident = hashlib.sha256(("\0".join(link[field] for field in ("origin","id","key"))).encode()).hexdigest()[:24]
    return state/f"node-commands-{ident}.sqlite"


def pair_node(args):
    import neb_fleet
    cfg, state = paths(args)
    private(cfg)
    private(state)
    file = cfg/"fleet-link.json"
    if core.read(cfg/"login.json") is None:
        raise ValueError("Сначала установите и настройте эту панель.")
    with lock_state(state):
        previous = core.read(file,{})
        if previous and (previous["origin"] != args.origin or previous["id"] != args.id):
            raise ValueError("Сначала отзовите существующее подключение и выполните apollo-neb node-unpair.")
        if previous.get("paired") is True:
            raise ValueError("Этот узел уже подключён.")
        new = previous or {"origin":args.origin,"id":args.id,"key":secrets.token_urlsafe(32),
                           "allow_loopback":args.allow_loopback,"paired":False}
        client = neb_fleet.Client(new["origin"],new["id"],new["key"],link_receipts(state,new),
                                  allow_loopback=new["allow_loopback"])
        core.store(file,new)
        client.join(getpass.getpass("Одноразовый код из центральной панели: ").strip())
        core.store(file,{**new,"paired":True})
        print("Нода подключена. Запустите эту панель, чтобы отправлять статус.")


def main():
    parser = argparse.ArgumentParser(description="Apollo.NEB · локальное управление добычей")
    parser.add_argument("--version", action="version", version=f"Apollo.NEB {VERSION}")
    parser.add_argument("--config-dir", type=Path, help="явный путь вместо XDG_CONFIG_HOME")
    parser.add_argument("--state-dir", type=Path, help="явный путь вместо XDG_STATE_HOME")
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("configure", help="проверить и сохранить параметры")
    command.add_argument("--wallet", help="публичный адрес NEB Mainnet")
    command.add_argument("--worker", help="метка воркера в пуле")
    command.add_argument("--pool", help="идентификатор из pools.json")
    command.add_argument("--pools", type=Path, help="импорт списка пулов из JSON")
    command.add_argument("--workers", type=Path, help="импорт списка воркеров из JSON")
    command.add_argument("--port", type=int, help="порт HTTP на 127.0.0.1 (по умолчанию 18763)")
    command.add_argument("--origin", help="внешний HTTPS origin (только за своим обратным прокси)")
    command.add_argument("--support-wallet", help="публичный NEB-адрес автора")
    command.add_argument("--support-bps", type=int, help="доля времени: 50 = 0,5%%, предел 100")
    command.add_argument("--accept-support", action="store_true", help="подтвердить поддержку автора и использовать указанный адрес")
    command.add_argument("--disable-support", action="store_true", help="выключить поддержку автора")
    command.add_argument("--totp", action="store_true", help="на первой установке подключить двухфакторный вход")
    command.add_argument("--password-stdin", action="store_true", help="для автоматизации: прочитать пароль из stdin, не передавать в аргументах")
    command.add_argument("--publish-current",type=Path,help=argparse.SUPPRESS)
    command = sub.add_parser("password", help="сменить пароль в остановленной панели и отозвать сессии")
    command.add_argument("--totp", action="store_true", help="выдать новый ключ TOTP")
    command.add_argument("--password-stdin", action="store_true")
    sub.add_parser("devices", help="показать обнаруженные PCI GPU и доступность показаний")
    sub.add_parser("serve", help="запустить локальную панель без автоматического запуска воркеров")
    command = sub.add_parser("upgrade",help="проверить официальный релиз; установка только с --apply")
    command.add_argument("--apply",action="store_true")
    command.add_argument("--service",action="store_true",help="запустить пользовательскую службу после установки")
    command = sub.add_parser("node-pair",help="подключить этот узел к центральной панели")
    command.add_argument("--origin",required=True,help="HTTPS адрес центральной панели")
    command.add_argument("--id",required=True,help="идентификатор из центральной панели")
    command.add_argument("--allow-loopback",action="store_true",help="разрешить HTTP только в локальном SSH-туннеле")
    sub.add_parser("node-unpair",help="отключить исходящее соединение этой панели; сначала отзовите узел на центральной")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise ValueError("Apollo.NEB запускается в Linux от обычного выделенного пользователя, без sudo.")
        if args.action == "configure":
            configure(args)
        elif args.action == "password":
            rotate_password(args)
        elif args.action == "devices":
            print(json.dumps(neb_runtime.discover_devices(), ensure_ascii=False, indent=2))
        elif args.action == "node-pair":
            pair_node(args)
        elif args.action == "node-unpair":
            cfg, state = paths(args)
            with lock_state(private(state)):
                (cfg/"fleet-link.json").unlink(missing_ok=True)
            print("Локальная привязка снята.")
        elif args.action == "upgrade":
            cfg, state = paths(args)
            if args.apply:
                with lock_state(private(state)):
                    pass
            parent = HERE.parent
            prefix = parent.parent.parent if parent.name == "apollo-neb" and parent.parent.name == "lib" else Path.home()/".local"
            neb_release.upgrade(cfg,state,prefix=prefix,apply=args.apply,service=args.service)
        else:
            serve(args)
    except (ValueError, RuntimeError, OSError, EOFError, KeyboardInterrupt, subprocess.SubprocessError) as exc:
        print(f"Apollo.NEB: {exc or 'отменено'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
