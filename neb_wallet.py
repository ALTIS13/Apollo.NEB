"""Offline NEB wallet creation and encrypted recovery using the pinned official CLI."""

import base64
from contextlib import contextmanager
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


VERSION = '1.0.2'
# https://github.com/SilasEigen/neb/releases/expanded_assets/v1.0.2
ARCHIVE_URL = 'https://github.com/SilasEigen/neb/releases/download/v1.0.2/neb-1.0.2-linux-x86_64.tar.gz'
ARCHIVE_SIZE = 3_360_538
ARCHIVE_SHA256 = '8004653193c147e47521b86d69a8026d580b345ec7c7c1a17d2479932441131a'
BINARY_SIZE = 1_076_512
BINARY_SHA256 = '16f9fbfc674d287c012438a4590c8041c88b481bae5b667202802480bffceed0'
MAX_WALLETS = 32
MAX_SCAN = 128
MAX_RECOVERY = 4096
CLI_TIMEOUT = 12
IDENT = re.compile(r'[0-9a-f]{32}')
NATIVE_FORMAT = re.compile(
    rb'# neb wallet, encrypted with a password\nneb-wallet-encrypted 1\n'
    rb'salt [0-9a-f]{32}\nnonce [0-9a-f]{48}\ndata (?:[0-9a-f]{2}){17,2048}\n'
)
MISSING = 'Для кошельков отдельно установите официальный neb-wallet.'
UNSAFE = 'Небезопасный или повреждённый каталог кошельков.'


def _platform_message():
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        return 'Официальный neb-wallet для панели поддерживает только Linux x86_64.'
    try:
        libc = os.confstr('CS_GNU_LIBC_VERSION')
        match = re.fullmatch(r'glibc ([0-9]+)\.([0-9]+)(?:\.[0-9]+)?', libc or '')
    except (OSError, ValueError):
        match = None
    if not match or tuple(map(int, match.groups())) < (2, 34):
        return 'Для официального neb-wallet требуется glibc 2.34 или новее.'
    return None


class _OfficialRedirect(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (target.scheme != 'https' or target.hostname not in ('github.com', 'release-assets.githubusercontent.com')
                or target.username is not None or target.password is not None or target.port not in (None, 443)):
            raise RuntimeError('Получена недопустимая переадресация загрузки neb-wallet.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download():
    deadline = time.monotonic() + 20
    request = Request(ARCHIVE_URL, headers={'Accept-Encoding': 'identity'})
    try:
        chunks = bytearray()
        with build_opener(_OfficialRedirect()).open(request, timeout=5) as response:
            while len(chunks) <= ARCHIVE_SIZE:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                block = response.read1(min(64 * 1024, ARCHIVE_SIZE + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
        if len(chunks) != ARCHIVE_SIZE:
            raise RuntimeError('Размер официального архива neb-wallet не совпадает.')
        return bytes(chunks)
    except (OSError, HTTPException, ValueError):
        raise RuntimeError('Не удалось загрузить официальный neb-wallet по HTTPS.') from None


def _verified_binary(data):
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError('SHA-256 официального архива neb-wallet не совпадает.')
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            binary = None
            for count, member in enumerate(archive):
                if count >= 16:
                    raise ValueError
                if member.name != 'neb-wallet':
                    continue
                if binary is not None or not member.isreg() or member.size != BINARY_SIZE:
                    raise ValueError
                with archive.extractfile(member) as source:
                    binary = source.read(BINARY_SIZE + 1)
        if binary is None or hashlib.sha256(binary).hexdigest() != BINARY_SHA256:
            raise ValueError
        return binary
    except (OSError, EOFError, tarfile.TarError, ValueError):
        raise RuntimeError('Не удалось проверить файл neb-wallet в официальном архиве.') from None


def _private_dir(path, *, make=False):
    try:
        good = path.is_absolute() and path.resolve() == path
    except RuntimeError:
        good = False
    if not good:
        raise RuntimeError(UNSAFE)
    if make:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        raise RuntimeError(UNSAFE)


def _read_private(path, limit, mode=0o600, *, sync=False):
    # Non-blocking opens reject unexpected FIFOs without waiting for a writer.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode or not 0 < info.st_size <= limit):
            raise RuntimeError(UNSAFE)
        data = handle.read(limit + 1)
        if len(data) != info.st_size:
            raise RuntimeError(UNSAFE)
        if sync:
            os.fsync(handle.fileno())
        return data


def _write_private(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _label(label):
    if not isinstance(label, str) or not label.strip().isprintable() or not 1 <= len(label.strip()) <= 48:
        raise ValueError('Укажите название кошелька: 1–48 печатных символов.')
    return label.strip()


def _password(password):
    if (not isinstance(password, str) or not 14 <= len(password) <= 128 or not password.strip()
            or not password.isprintable() or len(password.encode('utf-8')) > 256):
        raise ValueError('Пароль кошелька: 14–128 печатных символов (до 256 байт), без переносов строк.')
    return password


def _encrypted(path, *, sync=False):
    data = _read_private(path, MAX_RECOVERY, sync=sync)
    if not NATIVE_FORMAT.fullmatch(data):
        raise RuntimeError('Файл не является зашифрованной резервной копией neb-wallet.')
    return data


def _exec_tty():
    # Separate process so the threaded HTTP parent never uses preexec_fn or runs
    # wallet secrets through argv/environment. The private PTY is already fd 0.
    import fcntl
    import resource
    import termios

    binary, path, command = sys.argv[2:]
    if command not in ('new', 'list') or not Path(binary).is_absolute() or not Path(path).is_absolute():
        raise ValueError
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execv(binary, (binary, '--wallet', path, command))


def _run(binary, path, command, password):
    import pty
    import select
    import tty

    master, slave = pty.openpty()
    deadline = time.monotonic() + CLI_TIMEOUT
    proc = None
    try:
        # Set no-echo before feeding anything. rpassword opens /dev/tty and
        # restores our no-echo settings after each prompt.
        tty.setraw(slave)
        proc = subprocess.Popen(
            [sys.executable, '-I', str(Path(__file__).resolve()), '--wallet-tty', str(binary), str(path), command],
            stdin=slave, stderr=slave, stdout=subprocess.DEVNULL if command == 'new' else subprocess.PIPE,
            env={'LC_ALL': 'C', 'TERM': 'dumb', 'NO_COLOR': '1'},
            cwd=path.parent, start_new_session=True, umask=0o077,
        )
        os.close(slave)
        slave = None
        prompts = (b'new wallet password (empty: no encryption): ', b'again: ') if command == 'new' else (b'wallet password: ',)
        for expected in prompts:
            received = bytearray()
            while expected not in received:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                    raise TimeoutError
                block = os.read(master, 512)
                if not block or len(received) + len(block) > 2048:
                    raise OSError
                received.extend(block)
            line = password.encode('utf-8') + b'\n'
            if os.write(master, line) != len(line):
                raise OSError
        stdout, _ = proc.communicate(timeout=max(0.0, deadline - time.monotonic()))
        if proc.returncode != 0 or (stdout is not None and len(stdout) > 128):
            raise RuntimeError('Кошелёк не открыт: проверьте пароль и целостность файла.')
        return stdout or b''
    except (TimeoutError, subprocess.TimeoutExpired):
        raise RuntimeError('neb-wallet не завершил операцию за отведённое время.') from None
    except OSError:
        raise RuntimeError('Не удалось выполнить локальный neb-wallet.') from None
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        os.close(master)
        if slave is not None:
            os.close(slave)


def _address(binary, path, password):
    from neb_core import wallet as public_address

    try:
        return public_address(_run(binary, path, 'list', password).removesuffix(b'\n').decode('ascii'))
    except ValueError:
        raise RuntimeError('neb-wallet вернул недопустимый адрес.') from None


class WalletStore:
    """This host owns the encrypted files. The passphrase is never saved."""

    def __init__(self, state_dir: Path):
        self.state = Path(state_dir)
        self.root = self.state / 'wallets'
        self.binary = self.root / f'neb-wallet-v{VERSION}'
        self.busy = threading.Lock()

    @contextmanager
    def _operation(self):
        msg = _platform_message()
        if msg:
            raise RuntimeError(msg)
        if not self.busy.acquire(blocking=False):
            raise RuntimeError('Другая операция с кошельком ещё выполняется.')
        try:
            yield
        except (OSError, UnicodeError):
            raise RuntimeError(UNSAFE) from None
        finally:
            self.busy.release()

    def _root(self, *, make=False):
        _private_dir(self.state)
        _private_dir(self.root, make=make)

    def _verified_installed(self):
        digest = hashlib.sha256(_read_private(self.binary, BINARY_SIZE, mode=0o700)).hexdigest()
        if digest != BINARY_SHA256:
            raise RuntimeError('SHA-256 установленного neb-wallet не совпадает.')

    def _metadata(self, ident):
        from neb_core import wallet as public_address

        _private_dir(self.root / ident)
        try:
            record = json.loads(_read_private(self.root / ident / 'wallet.json', 1024))
            if not isinstance(record, dict) or set(record) != {'id', 'label', 'address'} or record['id'] != ident:
                raise ValueError
            return {'id': ident, 'label': _label(record['label']), 'address': public_address(record['address'])}
        except ValueError:
            raise RuntimeError(UNSAFE) from None

    def _items(self):
        items = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_SCAN:
                    raise RuntimeError(UNSAFE)
                if IDENT.fullmatch(entry.name):
                    items.append(self._metadata(entry.name))
        return sorted(items, key=lambda item: (item['label'].casefold(), item['id']))

    def status(self):
        items = []
        message = _platform_message()
        if message:
            return {'available': False, 'items': items, 'message': message}
        try:
            self._root()
            items = self._items()
            self._verified_installed()
            return {'available': True, 'items': items}
        except FileNotFoundError:
            message = MISSING
        except RuntimeError as error:
            message = str(error)
        except OSError:
            message = UNSAFE
        return {'available': False, 'items': items, 'message': message}

    def install(self):
        with self._operation():
            self._root(make=True)
            try:
                self._verified_installed()
            except FileNotFoundError:
                binary = _verified_binary(_download())
                with tempfile.TemporaryDirectory(dir=self.root, prefix='.install-') as folder:
                    candidate = Path(folder) / 'neb-wallet'
                    _write_private(candidate, binary, mode=0o700)
                    os.replace(candidate, self.binary)
                    _sync_dir(self.root)
            return self.status()

    def create(self, label, password):
        label, password = _label(label), _password(password)
        with self._operation():
            if not self.status()['available']:
                raise RuntimeError('Создание недоступно. Проверьте состояние neb-wallet.')
            if len(self._items()) >= MAX_WALLETS:
                raise RuntimeError('Достигнут предел кошельков на этой установке.')
            for _ in range(16):
                ident = secrets.token_hex(16)
                target = self.root / ident
                if not os.path.lexists(target):
                    break
            else:
                raise RuntimeError('Не удалось выбрать свободный идентификатор кошелька.')
            with tempfile.TemporaryDirectory(dir=self.root, prefix='.wallet-') as folder:
                pending = Path(folder)
                path = pending / 'wallet.neb'
                _run(self.binary, path, 'new', password)
                _encrypted(path, sync=True)
                item = {'id': ident, 'label': label, 'address': _address(self.binary, path, password)}
                _write_private(pending / 'wallet.json', json.dumps(item, ensure_ascii=False).encode('utf-8'))
                _sync_dir(pending)
                pending.rename(target)
                _sync_dir(self.root)
                return item

    def export(self, ident, password):
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise ValueError('Некорректный идентификатор кошелька.')
        password = _password(password)
        with self._operation():
            self._root()
            self._verified_installed()
            if not (self.root / ident).exists():
                raise ValueError('Кошелёк не найден.')
            item = self._metadata(ident)
            data = _encrypted(self.root / ident / 'wallet.neb')
            with tempfile.TemporaryDirectory(dir=self.root, prefix='.export-') as folder:
                # Verify the exact bytes that will be returned, not a later read
                # of the original path. The temporary file is ciphertext only.
                snapshot = Path(folder) / 'wallet.neb'
                _write_private(snapshot, data)
                if _address(self.binary, snapshot, password) != item['address']:
                    raise RuntimeError('Адрес резервной копии не совпадает с адресом кошелька.')
            return {'filename': f'neb-{ident}.neb', 'backup': base64.b64encode(data).decode('ascii')}


if __name__ == '__main__':
    try:
        if sys.argv[1:2] != ['--wallet-tty']:
            raise ValueError
        _exec_tty()
    except (OSError, ValueError):
        raise SystemExit(1) from None
