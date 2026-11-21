"""Read-only preflight for rootless Linux installer filesystem targets."""

import os
from pathlib import Path
import re
import stat
import sys


RELEASE = re.compile(r"release\.[A-Za-z0-9]{8}")


class Refusal(ValueError):
    def __init__(self, target):
        super().__init__(f"Refusing installer target: {target} (expected a private, canonical Apollo.NEB path).")


def _entry(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _writable_by_others(info):
    # Windows runs path-policy tests only; the Linux shell is the installer.
    return os.name == "posix" and bool(info.st_mode & 0o022)


def _directory(raw, target, uid):
    path = Path(raw)
    if (not path.is_absolute() or path == Path(path.anchor) or str(path) != raw
            or os.path.normpath(raw) != raw or not raw.isprintable()):
        raise Refusal(target)
    root_sticky_ancestor = False
    for element in (*reversed(path.parents), path):
        info = _entry(element)
        if info is None:
            # A different local user can create a previously missing component
            # below /tmp before the shell gets to mkdir. Use a private temp dir.
            if root_sticky_ancestor:
                raise Refusal(target)
            continue
        owner_ok = info.st_uid == uid if element == path else info.st_uid in (0, uid)
        root_sticky_ancestor = (element != path and info.st_uid == 0
                                and _writable_by_others(info) and bool(info.st_mode & stat.S_ISVTX))
        if (not stat.S_ISDIR(info.st_mode) or element.is_symlink() or not owner_ok
                or (_writable_by_others(info) and not root_sticky_ancestor)):
            raise Refusal(target)
    if path.resolve(strict=False) != path:
        raise Refusal(target)
    return path


def _regular_owned(info, uid):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
            and not _writable_by_others(info))


def _known_file(path, source, target, uid, executable=False):
    info = _entry(path)
    if info is None:
        return
    expected = source.read_bytes()
    if (not _regular_owned(info, uid) or info.st_size != len(expected)
            or (executable and os.name == "posix" and not info.st_mode & stat.S_IXUSR)
            or path.read_bytes() != expected):
        raise Refusal(target)


def check(prefix_raw, home_raw, service, source, uid):
    prefix = _directory(prefix_raw, "prefix", uid)
    bin_dir = _directory(str(prefix / 'bin'), "bin", uid)
    base = _directory(str(prefix / 'lib' / 'apollo-neb'), "release directory", uid)
    _known_file(bin_dir / 'apollo-neb', source / 'apollo-neb', "launcher", uid, executable=True)

    current = base / 'current'
    info = _entry(current)
    if info is not None:
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != uid:
            raise Refusal("current")
        name = os.readlink(current)
        if not RELEASE.fullmatch(name):
            raise Refusal("current")
        release = _directory(str(base / name), "current", uid)
        entry = _entry(release / 'neb.py')
        if entry is None or not _regular_owned(entry, uid):
            raise Refusal("current")

    if service:
        home = _directory(home_raw, "home", uid)
        unit_dir = _directory(str(home / '.config' / 'systemd' / 'user'), "service directory", uid)
        unit = 'apollo-neb.service'
        _known_file(unit_dir / unit, source / unit, "unit", uid)


def main():
    try:
        prefix, home, service = sys.argv[1:]
        check(prefix, home, service == '1', Path(__file__).resolve().parent, getattr(os, 'geteuid', lambda: 0)())
    except (Refusal, OSError, RuntimeError, ValueError) as error:
        print(str(error) if isinstance(error, Refusal) else "Unable to verify Apollo.NEB installer targets.", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
