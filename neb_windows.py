"""Windows x64 controller account, paths, and exclusive state lock."""

import base64
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
import sys


def require_x64():
    if (sys.version_info < (3, 10) or platform.machine().lower() not in {"amd64", "x86_64"}
            or struct.calcsize("P") != 8):
        raise ValueError("Для контроллера Apollo.NEB нужен Windows x64 и Python 3.10+ x64.")


def defaults():
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Apollo.NEB" / "controller"
    return base / "config", base / "state"


_CHECK_ACL = r"""
$ErrorActionPreference = 'Stop'
$path = $env:APOLLO_NEB_PRIVATE_DIRECTORY
$principal = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$trusted = @($principal.Value, 'S-1-5-18', 'S-1-5-32-544')
$sidType = [System.Security.Principal.SecurityIdentifier]
if ($env:APOLLO_NEB_NEW_DIRECTORY -eq '1') {
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($principal)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in $trusted) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sidType::new($sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    [IO.Directory]::SetAccessControl($path, $acl)
}
function Confirm-Private($item) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 1 }
    $acl = if ($item -is [IO.DirectoryInfo]) {
        [IO.Directory]::GetAccessControl($item.FullName)
    } else {
        [IO.File]::GetAccessControl($item.FullName)
    }
    if ($acl.GetOwner($sidType).Value -notin $trusted) { exit 1 }
    $rules = $acl.GetAccessRules($true, $true, $sidType)
    $currentAllowed = $false
    foreach ($rule in $rules) {
        if ($rule.AccessControlType -eq 'Allow') {
            if ($rule.IdentityReference.Value -notin $trusted) { exit 1 }
            if ($rule.IdentityReference.Value -eq $principal.Value) { $currentAllowed = $true }
        }
    }
    if (!$currentAllowed) { exit 1 }
}
$directory = [IO.DirectoryInfo]::new($path)
Confirm-Private $directory
foreach ($item in $directory.GetFileSystemInfos()) {
    if ($item -is [IO.DirectoryInfo]) { exit 1 }
    Confirm-Private $item
}
"""


def private(path):
    if (not path.is_absolute() or path.drive.startswith("\\\\") or path != path.resolve()
            or path in (Path.home(), *Path.home().parents)):
        raise ValueError("Укажите абсолютный приватный каталог на локальном диске Windows.")
    try:
        path.mkdir(parents=True)
        created = True
    except FileExistsError:
        created = False
    if not path.is_dir() or path != path.resolve():
        raise ValueError("Ожидается отдельный каталог Windows без ссылок.")
    env = {**os.environ, "APOLLO_NEB_PRIVATE_DIRECTORY": str(path),
           "APOLLO_NEB_NEW_DIRECTORY": str(int(created))}
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    check = base64.b64encode(_CHECK_ACL.encode("utf-16le")).decode("ascii")
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-EncodedCommand", check], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Каталог Windows должен принадлежать текущему пользователю и иметь приватный NTFS ACL.")
    return path


def lock(state):
    import msvcrt
    path = state / "service.lock"
    if path.exists() and (not path.is_file() or path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Недопустимый файл блокировки контроллера.")
    handle = path.open("a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise ValueError("Остановите запущенный Apollo.NEB с этим каталогом состояния.") from None
    return handle
