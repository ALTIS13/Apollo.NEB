"""Build allowlisted Apollo.NEB release archives without touching installed state."""

import argparse
import ast
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "neb.py", "neb_app.py", "neb_core.py", "neb_devfee.py", "neb_fleet.py", "neb_history.py",
    "neb_network.py", "neb_overview.py", "neb_release.py", "neb_runtime.py", "neb_setup.py",
    "neb_remote_install.py", "neb_remote_bootstrap.py", "neb_atlas.py",
)
WEB = tuple(f"web/{file}" for file in (
    "brand.svg", "history-chart.js", "index.html", "live-metrics.js", "qr.js",
    "recovery-codes.js", "select-menu.js", "ui.css", "ui.js", "view.js",
))
DOCS = ("README.md", "docs/onboarding-and-distribution.md", "docs/release-checks.md")
LINUX = (
    "apollo-neb", "apollo-neb.service", "install.sh", "neb_install_check.py", "neb_platform.py",
    "neb_node_update.py", "neb_update_apply.py",
    "examples/custom.json", "examples/suprminer-amd.json", "examples/suprminer-nvidia.json",
)
WINDOWS = ("Apollo.NEB.cmd", "neb_windows.py")
TARGETS = ("linux", "linux-6.6", "linux-6.12", "windows-x64")
EXECUTABLE = {"apollo-neb", "install.sh"}
VERSION_PATTERN = re.compile(r"([0-9]+\.[0-9]+\.[0-9]+)(?:-rc\.[1-9][0-9]*)?")


def read_file(name, revision):
    if revision:
        return subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
    path = ROOT / name
    if not path.is_file() or path.resolve(strict=True) != path:
        raise ValueError(f"Unexpected release source path: {name}")
    return path.read_bytes()


def release_version(source):
    tree = ast.parse(source)
    versions = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(versions) != 1 or not (match := VERSION_PATTERN.fullmatch(versions[0])):
        raise ValueError("Expected one final or release-candidate VERSION in neb_release.py.")
    return versions[0], match[1]


def file_bytes(name, revision):
    data = read_file(name, revision)
    if name in EXECUTABLE:
        return data.replace(b"\r\n", b"\n")
    if name.endswith(".cmd"):
        return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def tar_payload(root, entries):
    output = BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            folders = {str(parent) for name in entries for parent in PurePosixPath(root, name).parents
                       if str(parent) != "."}
            for folder in sorted(folders):
                item = tarfile.TarInfo(folder)
                item.type, item.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(item)
            for name, data in sorted(entries.items()):
                item = tarfile.TarInfo(f"{root}/{name}")
                item.mode, item.size = (0o755 if name in EXECUTABLE else 0o644), len(data)
                archive.addfile(item, BytesIO(data))
    return output.getvalue()


def zip_payload(root, entries):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in sorted(entries.items()):
            item = zipfile.ZipInfo(f"{root}/{name}")
            archive.writestr(item, data, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def build(target, revision):
    version, numeric = release_version(read_file("neb_release.py", revision))
    windows = target == "windows-x64"
    kernel = target.removeprefix("linux-") if target.startswith("linux-") else None
    suffix = f"linux-x64-kernel-{kernel}" if kernel else target
    name = f"apollo-neb-{numeric}-{suffix}" + (".zip" if windows else ".tar.gz")
    root = f"Apollo.NEB-{numeric}-{suffix}"
    sources = (*MODULES, *WEB, *DOCS, f"docs/releases/{version}.md", "developer-support.json",
               *(WINDOWS if windows else LINUX))
    entries = {file: file_bytes(file, revision) for file in sources}
    compatibility = {
        "version": version,
        "platform": "windows" if windows else "linux",
        "role": "linux-node-controller" if windows else "worker-and-controller",
        "python_minimum": "3.10",
        **({"architecture": "x64"} if windows or kernel else {}),
        **({"kernel_family": kernel} if kernel else {}),
        **({} if windows else {"requires_pidfd": True}),
        **({"source_revision": revision} if revision else {}),
    }
    entries["compatibility.json"] = (json.dumps(compatibility, indent=2) + "\n").encode("utf-8")
    return name, (zip_payload(root, entries) if windows else tar_payload(root, entries))


def write_archive(directory, name, payload):
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / name
    checksum = archive.with_name(name + ".sha256")
    digest = hashlib.sha256(payload).hexdigest()
    written = []
    try:
        with archive.open("xb") as file:
            written.append(archive)
            file.write(payload)
        with checksum.open("x", encoding="ascii", newline="\n") as file:
            written.append(checksum)
            file.write(f"{digest}  {name}\n")
    except OSError:
        for path in written:
            path.unlink()
        raise
    return digest, archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", help="build from a verified Git commit instead of the working tree")
    args = parser.parse_args()
    revision = (subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options",
                                        args.commit + "^{commit}"], cwd=ROOT, text=True).strip()
                if args.commit else None)
    name, payload = build(args.target, revision)
    digest, archive = write_archive(args.output, name, payload)
    print(f"{digest}  {archive}")


if __name__ == "__main__":
    main()
