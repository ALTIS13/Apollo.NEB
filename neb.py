#!/usr/bin/env python3
"""Apollo.NEB local installer configuration and supervisor entrypoint."""
import argparse
import base64
import hashlib
import getpass
import logging
import json
import os
import platform
import secrets
import signal
import stat
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from contextlib import closing

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import neb_app
import neb_core as core
import neb_runtime
import neb_setup
import neb_release
from neb_release import VERSION


def private(path):
    if sys.platform == "win32":
        import neb_windows
        return neb_windows.private(path)
    if not path.is_absolute() or path != path.resolve() or path in (Path.home(), *Path.home().parents):
        raise ValueError("Укажите абсолютный путь к приватному каталогу, не ссылку.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = path.stat()
    if meta.st_uid != os.geteuid() or not stat.S_ISDIR(meta.st_mode) or meta.st_mode & 0o077:
        raise ValueError("Нужен отдельный каталог текущего пользователя с правами 0700. Существующие права не изменены.")
    return path


def paths(args, name="apollo-neb"):
    if sys.platform == "win32":
        import neb_windows
        default_cfg, default_state = neb_windows.defaults()
    else:
        default_cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / name
        default_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / name
    cfg = args.config_dir or default_cfg
    state = args.state_dir or default_state
    if (not cfg.is_absolute() or not state.is_absolute() or cfg != cfg.resolve() or state != state.resolve()
            or cfg == state or cfg in state.parents or state in cfg.parents):
        raise ValueError("Каталоги настроек и состояния должны быть отдельными абсолютными путями без ссылок.")
    return cfg, state


def settings_path(cfg):
    return cfg / "settings.json"


def release_support():
    policy = core.read(HERE / "developer-support.json", {})
    if not isinstance(policy, dict) or policy.get("basis_points") != core.DEVFEE_BASIS_POINTS:
        raise ValueError("Установите полный официальный пакет Apollo.NEB.")
    return core.support_config({"enabled": True, "basis_points": policy["basis_points"], "wallet": policy.get("wallet")})


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
    support = release_support()
    if support["wallet"] == profile["wallet"]:
        raise ValueError("Адрес поддержки и адрес добычи не могут совпадать.")
    limits = core.limits(core.read(cfg / "limits.json", core.LIMITS))
    settings, enrollment = configure_login(args, cfg, state, old)
    core.store(settings_path(cfg), settings)
    core.store(cfg / "profile.json", profile)
    core.store(cfg / "pools.json", list(checked_pools.values()))
    core.store(cfg / "workers.json", workers)
    core.store(cfg / "limits.json", limits)
    print(f"Настройки: {cfg}\nАдрес панели: http://127.0.0.1:{settings['port']}\n"
          f"Воркеров: {len(workers)}. Майнинг не запущен.")
    print("DevFee: 1,1% активного времени майнинга.")
    enroll(enrollment)


def configure_controller(args):
    cfg, state = paths(args)
    private(cfg)
    with lock_state(private(state)):
        old = core.read(settings_path(cfg))
        if old is not None:
            require_controller_settings(old)
        settings, enrollment = configure_login(args, cfg, state, old or {})
        core.store(settings_path(cfg), {**settings, "mode": "windows-controller"})
        print(f"Контроллер нод Windows: http://127.0.0.1:{settings['port']}")
        enroll(enrollment)


def require_controller_settings(settings):
    if not isinstance(settings, dict) or settings.get("mode") != "windows-controller":
        raise ValueError("Для контроллера Windows нужен отдельный каталог настроек.")


def configure_login(args, cfg, state, old):
    origin = neb_app.validated_origin(args.origin if args.origin is not None else old.get("origin"))
    port = args.port if args.port is not None else old.get("port", 18763)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Порт должен быть в диапазоне 1024–65535.")
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
    return {"origin": origin, "port": port}, enrollment


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
    if sys.platform == "win32":
        import neb_windows
        return neb_windows.lock(state)
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
    if sys.platform == "win32":
        private(cfg)
        require_controller_settings(core.read(settings_path(cfg)))
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


def linux_boot():
    try:
        boot_id = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()))
    except (OSError, ValueError):
        return None, None
    try:
        with Path("/proc/stat").open(encoding="ascii") as stats:
            reboot_at = next((int(line.split()[1]) for line in stats if line.startswith("btime ")), None)
    except (OSError, ValueError, IndexError):
        reboot_at = None
    return boot_id, reboot_at


def serve(args, *, controller=False, panel_factory=None, fleet_only=False):
    import neb_fleet
    cfg, state = paths(args)
    if controller:
        private(cfg)
        private(state)
    login = core.read(cfg / "login.json", None) if panel_factory is None else None
    settings = core.read(settings_path(cfg), None)
    if settings is None or (panel_factory is None and login is None):
        raise ValueError("Сначала выполните configure.")
    if controller:
        require_controller_settings(settings)
    port = settings["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Укажите допустимый непривилегированный порт.")
    support = None if controller else release_support()
    local_workers = not (controller or fleet_only)
    if not controller:
        private(cfg)
        private(state)
    with lock_state(state):
        origin = settings.get("origin")
        if panel_factory is None:
            auth = core.Auth(login, state / "auth.sqlite")
            if origin and not auth.is_totp_enabled():
                raise ValueError("Внешний адрес допускается только с TOTP.")
        workers = neb_runtime.validate_workers(core.read(cfg / "workers.json", [])) if local_workers else []
        runtime = neb_runtime.Manager(workers, state)
        service = neb_app.Service(
            cfg, state, runtime, core.read(cfg / "pools.json", core.DEFAULT_POOLS), support,
            setup=neb_setup.Setup(state) if local_workers else None,
            releases=neb_release.Releases(), fleet=neb_fleet.Fleet(state / "fleet.sqlite"),
            controller_only=not local_workers,
        )
        from neb_remote_install import RemoteInstaller
        try:
            service.remote_install = RemoteInstaller.load(
                cfg, state, settings.get("origin"), service.releases, service.fleet)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            logging.warning("Apollo.NEB remote install: invalid configuration")
        from neb_atlas import AtlasSource
        try:
            service.atlas = AtlasSource.load(cfg)
        except (OSError, ValueError, RecursionError):
            logging.warning("Apollo.NEB Atlas source: invalid configuration")
        address = ("127.0.0.1", port)
        if panel_factory is None:
            server = neb_app.Server(address, service, auth, HERE / "web", origin)
        else:
            server = panel_factory(address, service, HERE / "web", origin)
        boot_id, reboot_at = (None, None) if controller else linux_boot()
        service.archive.note_start(int(time.time()), boot_id=boot_id, reboot_at=reboot_at)
        service.poll()
        background = threading.Thread(target=service.loop, name="neb-monitor", daemon=True)
        background.start()
        threading.Thread(target=service.farm_history_loop, name="neb-farm-history", daemon=True).start()
        if not controller:
            threading.Thread(target=service.devfee.loop, args=(service.stopping,),
                             name="neb-devfee", daemon=True).start()
        if local_workers:
            link = core.read(cfg / "fleet-link.json")
            if isinstance(link, dict) and link.get("paired") is True:
                follower = neb_fleet.Client(link["origin"], link["id"], link["key"], link_receipts(state, link),
                                            allow_loopback=link.get("allow_loopback") is True)
                system = node_system(reboot_at) if link.get("share_diagnostics") is True else None
                import neb_node_update
                updates = None
                if neb_node_update.supported(cfg, state, HERE / "neb.py", link):
                    updates = neb_node_update.Updates(upgrade_journal(state, link), HERE / "neb.py", service, follower)
                threading.Thread(target=node_loop, args=(service, follower, system, updates), daemon=True, name="neb-node").start()

            import neb_platform
            try:
                platform_token = neb_platform.configured_token(cfg)
            except (OSError, ValueError, RecursionError):
                logging.warning("Apollo Platform publisher: invalid configuration")
                platform_token = None
            if platform_token is not None:
                threading.Thread(target=neb_platform.run, args=(service.public_status, platform_token, service.stopping),
                                 daemon=True, name="neb-platform-publisher").start()

        def stop(_signum, _frame):
            raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, stop)
        label = "Apollo.NEB Windows · контроллер нод" if controller else "Apollo.NEB"
        print(f"{label} {VERSION}: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.4)
        except KeyboardInterrupt:
            pass
        finally:
            service.close()
            server.server_close()
            background.join(timeout=3)

def node_system(boot_time):
    from neb_fleet import SYSTEM_LABEL

    uname = platform.uname()
    system = {"platform": uname.system, "kernel": uname.release, "arch": uname.machine}
    system = {field: name if SYSTEM_LABEL.fullmatch(name) else None for field, name in system.items()}
    try:
        memory_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        memory_bytes = None
    return {**system, "cpu_count": os.cpu_count(),
            "memory_bytes": memory_bytes if memory_bytes and memory_bytes > 0 else None, "boot_time": boot_time}


def node_details(service, system):
    from neb_fleet import EVENT_LIMIT, EVENT_STATES, EVENT_TYPES, WORKER_ID

    events = [{field: row[field] for field in ("time", "worker", "type", "status")}
              for row in service.archive.events() if row["type"] in EVENT_TYPES and row["status"] in EVENT_STATES
              and WORKER_ID.fullmatch(row["worker"])]
    return {"system": system, "events": events[:EVENT_LIMIT]}


def node_loop(service, follower, system=None, updates=None):
    def execute(action, ids):
        result = service.group(action,ids)
        if not all(row["ok"] for row in result["results"]):
            raise RuntimeError("Результат действия подтверждён не полностью.")
    last_fault = -60.
    release = None
    if neb_release.host_platform() == "linux" and platform.machine().lower() in {"amd64", "x86_64"}:
        release = {"version": VERSION, "platform": "linux", "arch": "x64", "kernel": neb_release.host_kernel()}
        if updates is not None:
            release["upgrade"] = True
    while not service.stopping.is_set():
        try:
            details = node_details(service, system) if system is not None else None
            reported = False
            try:
                follower.cycle(service.status()["workers"], execute, details=details, release=release)
                reported = True
            finally:
                if updates is not None:
                    updates.cycle(reported=reported)
        except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError):
            if time.monotonic() - last_fault >= 60:
                logging.warning("Apollo.NEB could not update its remote node link.")
                last_fault = time.monotonic()
        service.stopping.wait(4)


def link_receipts(state, link):
    ident = hashlib.sha256(("\0".join(link[field] for field in ("origin","id","key"))).encode()).hexdigest()[:24]
    return state/f"node-commands-{ident}.sqlite"

def upgrade_journal(state, link):
    return link_receipts(state, link).with_suffix(".upgrade.json")


def apply_node_update(args):
    import neb_fleet
    from neb_node_update import run_helper
    from neb_update_apply import apply

    cfg, state = paths(args)
    home = Path.home()
    if cfg != home / ".config/apollo-neb" or state != home / ".local/state/apollo-neb":
        raise ValueError("Автоматическое обновление поддерживает стандартные каталоги службы.")
    private(cfg)
    private(state)
    link = core.read(cfg / "fleet-link.json", {})
    if not link.get("paired") or link.get("allow_upgrades") is not True or not link["origin"].startswith("https://"):
        raise ValueError("Обновление из центральной панели отключено.")
    client = neb_fleet.Client(link["origin"], link["id"], link["key"], link_receipts(state, link))
    port = core.read(settings_path(cfg))["port"]
    run_helper(upgrade_journal(state, link), args.id, config=cfg, state=state,
               prefix=home / ".local", port=port, client=client, apply=apply)


def pair_node(args):
    import neb_fleet
    cfg, state = paths(args)
    private(cfg)
    private(state)
    file = cfg/"fleet-link.json"
    if core.read(cfg/"login.json") is None:
        raise ValueError("Сначала установите и настройте эту панель.")
    if args.allow_upgrades and not args.origin.startswith("https://"):
        raise ValueError("Обновление из центральной панели требует HTTPS.")
    with lock_state(state):
        previous = core.read(file, {})
        ident, code = args.id, None
        if ident is None:
            supplied = (sys.stdin.readline(129) if args.key_stdin
                        else getpass.getpass("Ключ подключения из центральной панели: "))
            ident, _, code = supplied.strip().partition(".")
            if not neb_fleet.NODE_ID.fullmatch(ident) or not neb_fleet.CODE.fullmatch(code):
                raise ValueError("Введите ключ подключения целиком в формате идентификатор.код.")
        elif args.key_stdin:
            raise ValueError("Передайте полный ключ подключения через stdin без --id.")
        if previous and (previous["origin"] != args.origin or previous["id"] != ident):
            raise ValueError("Сначала отзовите существующее подключение и выполните apollo-neb node-unpair.")
        if previous.get("paired") is True:
            raise ValueError("Этот узел уже подключён.")
        new = previous or {"origin":args.origin,"id":ident,"key":secrets.token_urlsafe(32),
                           "allow_loopback":args.allow_loopback,"paired":False}
        client = neb_fleet.Client(new["origin"],new["id"],new["key"],link_receipts(state,new),
                                  allow_loopback=new["allow_loopback"])
        core.store(file,new)
        if code is None:
            code = getpass.getpass("Одноразовый код из центральной панели: ").strip()
        client.join(code)
        paired = {**new, "paired": True, "share_diagnostics": args.share_diagnostics}
        if args.allow_upgrades:
            paired["allow_upgrades"] = True
        core.store(file, paired)
        print("Нода подключена. Запустите эту панель, чтобы отправлять статус.")


def check_node(args):
    import neb_fleet

    cfg, _ = paths(args)
    try:
        link = core.read(cfg / "fleet-link.json")
    except (OSError, ValueError, RecursionError):
        raise ValueError("Не удалось прочитать локальную привязку ноды.") from None
    if not isinstance(link, dict) or link.get("paired") is not True:
        raise ValueError("Сначала привяжите ноду командой apollo-neb node-pair.")
    client = neb_fleet.Client(link.get("origin"), link.get("id"), link.get("key"), None,
                              allow_loopback=link.get("allow_loopback") is True)
    last_seen = client.check()["last_seen"]
    if last_seen is None:
        report = "Первый отчёт ноды ещё не получен."
    else:
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(last_seen))
        except (OverflowError, OSError, ValueError):
            raise ValueError("Центральная панель вернула недопустимое время последнего отчёта.") from None
        report = "Последний отчёт ноды — " + stamp + "."
    print("Связь с центральной панелью подтверждена. " + report)


def main():
    controller = sys.platform == "win32"
    parser = argparse.ArgumentParser(description="Apollo.NEB · контроллер Linux-нод для Windows x64" if controller
                                     else "Apollo.NEB · локальное управление добычей")
    parser.add_argument("--version", action="version", version=f"Apollo.NEB {VERSION}")
    parser.add_argument("--config-dir", type=Path, help="явный путь вместо XDG_CONFIG_HOME")
    parser.add_argument("--state-dir", type=Path, help="явный путь вместо XDG_STATE_HOME")
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("configure", help="проверить и сохранить параметры",
                             description="Настройка входа в контроллер Linux-нод." if controller else
                             "DevFee 1,1% активного времени. Майнинг запускаете вы.")
    if not controller:
        command.add_argument("--wallet", help="публичный адрес NEB Mainnet")
        command.add_argument("--worker", help="метка воркера в пуле")
        command.add_argument("--pool", help="идентификатор из pools.json")
        command.add_argument("--pools", type=Path, help="импорт списка пулов из JSON")
        command.add_argument("--workers", type=Path, help="импорт списка воркеров из JSON")
        command.add_argument("--publish-current",type=Path,help=argparse.SUPPRESS)
    command.add_argument("--port", type=int, help="порт HTTP на 127.0.0.1 (по умолчанию 18763)")
    command.add_argument("--origin", help="внешний HTTPS origin (только за своим обратным прокси)")
    command.add_argument("--totp", action="store_true", help="на первой установке подключить двухфакторный вход")
    command.add_argument("--password-stdin", action="store_true", help="для автоматизации: прочитать пароль из stdin, не передавать в аргументах")
    command = sub.add_parser("password", help="сменить пароль в остановленной панели и отозвать сессии")
    command.add_argument("--totp", action="store_true", help="выдать новый ключ TOTP")
    command.add_argument("--password-stdin", action="store_true")
    sub.add_parser("serve", help="запустить контроллер Linux-нод" if controller else
                   "запустить локальную панель без автоматического запуска воркеров")
    command = sub.add_parser("upgrade", help="проверить официальный релиз" if controller else
                             "проверить официальный релиз; установка только с --apply")
    command.add_argument("--version", help="точный тег GitHub Releases")
    command.add_argument("--apply", action="store_true", help=argparse.SUPPRESS if controller else
                         "установить выбранный релиз Linux")
    command.add_argument("--service", action="store_true", help=argparse.SUPPRESS if controller else
                         "запустить пользовательскую службу после установки")
    if not controller:
        sub.add_parser("devices", help="показать обнаруженные PCI GPU и доступность показаний")
        command = sub.add_parser("node-pair", help="подключить этот узел к центральной панели")
        command.add_argument("--origin", required=True, help="HTTPS адрес центральной панели")
        command.add_argument("--id", help="идентификатор для прежнего формата подключения")
        command.add_argument("--key-stdin", action="store_true", help=argparse.SUPPRESS)
        command.add_argument("--allow-loopback", action="store_true",
                             help="разрешить HTTP только в локальном SSH-туннеле")
        command.add_argument("--share-diagnostics", action="store_true",
                             help="передавать сведения о системе, PCI и события NEB и разрешить перезапуск воркеров")
        command.add_argument("--allow-upgrades", action="store_true", help="разрешить обновления из центральной панели через HTTPS")
        command = sub.add_parser("node-update-helper", help=argparse.SUPPRESS)
        command.add_argument("id")
        sub.add_parser("node-check", help="проверить связь привязанной ноды с центральной панелью")
        sub.add_parser("node-unpair", help="отключить исходящее соединение этой панели")
    args = parser.parse_args()
    try:
        if controller:
            import neb_windows
            neb_windows.require_x64()
        elif sys.platform != "linux" or os.geteuid() == 0:
            raise ValueError("Apollo.NEB запускается в Linux от обычного выделенного пользователя, без sudo.")
        else:
            os.umask(0o077)
        if args.action == "configure":
            (configure_controller if controller else configure)(args)
        elif args.action == "password":
            rotate_password(args)
        elif args.action == "devices":
            print(json.dumps(neb_runtime.discover_devices(), ensure_ascii=False, indent=2))
        elif args.action == "node-pair":
            pair_node(args)
        elif args.action == "node-check":
            check_node(args)
        elif args.action == "node-update-helper":
            apply_node_update(args)
        elif args.action == "node-unpair":
            cfg, state = paths(args)
            with lock_state(private(state)):
                (cfg/"fleet-link.json").unlink(missing_ok=True)
            print("Локальная привязка снята.")
        elif args.action == "upgrade":
            if controller and (args.apply or args.service):
                raise ValueError("Обновление Windows устанавливается распаковкой проверенного ZIP.")
            cfg, state = paths(args)
            if args.apply:
                with lock_state(private(state)):
                    pass
            parent = HERE.parent
            prefix = parent.parent.parent if parent.name == "apollo-neb" and parent.parent.name == "lib" else Path.home()/".local"
            neb_release.upgrade(cfg, state, prefix=prefix, apply=args.apply, service=args.service, tag=args.version)
        else:
            serve(args, controller=controller)
    except (ValueError, RuntimeError, OSError, EOFError, KeyboardInterrupt, subprocess.SubprocessError) as exc:
        print(f"Apollo.NEB: {exc or 'отменено'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
