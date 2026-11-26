"""Produce a first-boot Apollo.NEB disk from a verified Debian 12 generic image."""

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from package_release import ROOT, build, read_file, release_version


BUS_SHA256 = "4c3aaad251f876214fadb5580dd5f4925f73838832135e6d1dab981eb963be6b"
DISK_BYTES = 8 * 1024**3
LOADER = """#!/bin/bash
set -Eeuo pipefail
trap 'echo "NEB_IMAGE_LOADER_FAILED line=$LINENO" >/dev/ttyS0; systemctl --no-block poweroff' ERR
install -d /run/neb-image-media
mount -o ro "$(blkid -L CIDATA)" /run/neb-image-media
install -m0700 /run/neb-image-media/provision.sh /run/apollo-neb-image-provision.sh
umount /run/neb-image-media
rmdir /run/neb-image-media
exec /bin/bash /run/apollo-neb-image-provision.sh
"""


def digest(path, algorithm="sha256"):
    result = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def seed_iso(image, assets):
    import pycdlib

    config = (
        "#cloud-config\nhostname: apollo-neb\npreserve_hostname: false\nusers: []\n"
        "disable_root: true\nssh_pwauth: false\nssh_genkeytypes: []\n"
        "disable_network_activation: true\ngrowpart:\n  mode: auto\n  devices: ['/']\nresize_rootfs: true\nwrite_files:\n"
        "  - path: /run/neb-image-loader\n    permissions: '0700'\n    content: |\n"
        + "".join("      " + line + "\n" for line in LOADER.splitlines())
        + "runcmd:\n  - [/bin/bash, /run/neb-image-loader]\n"
    )
    contents = {
        "user-data": config.encode("ascii"),
        "meta-data": b"instance-id: apollo-neb-image-builder\n",
        "network-config": b"version: 2\nethernets: {}\n",
        **assets,
        "files.sha256": "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in assets.items()
        ).encode("ascii"),
    }
    iso = pycdlib.PyCdlib()
    try:
        iso.new(interchange_level=3, rock_ridge="1.09", vol_ident="CIDATA")
        for index, (name, data) in enumerate(contents.items(), 1):
            iso.add_fp(BytesIO(data), len(data), iso_path=f"/INPUT{index:02d}.BIN;1", rr_name=name)
        iso.write(str(image))
    finally:
        iso.close()


def qemu_path(path):
    return path.resolve(strict=True).as_posix().replace(",", ",,")


def build_disk(args):
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", args.commit + "^{commit}"],
        cwd=ROOT, text=True,
    ).strip()
    version, _ = release_version(read_file("neb_release.py", revision))
    if not re.fullmatch(r"[0-9a-f]{128}", args.base_sha512) or digest(args.base, "sha512") != args.base_sha512:
        raise ValueError("The Debian base SHA-512 does not match.")
    image_tool = str(args.qemu_img.resolve(strict=True))
    base_info = json.loads(subprocess.check_output(
        [image_tool, "info", "--output=json", str(args.base.resolve(strict=True))],
        timeout=15,
    ))
    if base_info["format"] != "qcow2" or base_info.get("backing-filename") or base_info["virtual-size"] > DISK_BYTES:
        raise ValueError("Use an independent Debian QCOW2 disk no larger than 8 GiB.")
    bus = args.dbus_user_session.read_bytes()
    if hashlib.sha256(bus).hexdigest() != BUS_SHA256:
        raise ValueError("Use the pinned Debian 12 dbus-user-session package.")
    name, payload = build("linux", revision)
    assets = {"apollo-neb.tar.gz": payload, "user-bus.deb": bus}
    for file in ("console-setup.sh", "provision.sh", "seal.sh"):
        assets[file] = read_file("image/linux/" + file, revision).replace(b"\r\n", b"\n")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"apollo-neb-{version}-linux-amd64.qcow2"
    receipt = output / f"apollo-neb-{version}-image.json"
    sidecar = target.with_suffix(".qcow2.sha256")
    serial = output / f"apollo-neb-{version}-image-build.log"
    host = output / f"apollo-neb-{version}-qemu-build.log"
    if any(path.exists() for path in (target, receipt, sidecar, serial, host)):
        raise FileExistsError("Use an output directory without files from this image build.")
    with tempfile.TemporaryDirectory(dir=output, prefix="image-") as temp:
        work = Path(temp)
        disk, seed, variables = (work / entry for entry in ("root.qcow2", "seed.iso", "vars.fd"))
        shutil.copyfile(args.base, disk)
        if base_info["virtual-size"] < DISK_BYTES:
            subprocess.run([image_tool, "resize", "-f", "qcow2", str(disk), str(DISK_BYTES)], check=True, timeout=30)
        shutil.copyfile(args.firmware_vars, variables)
        seed_iso(seed, assets)
        executable = args.qemu.resolve(strict=True)
        share = executable.parent / "share"
        command = [str(executable), *(("-L", str(share)) if share.is_dir() else ()),
            "-machine", "q35", "-accel", "tcg,thread=multi", "-cpu", "max",
            "-smp", "2", "-m", "1536", "-nic", "none", "-nodefaults", "-display", "none", "-monitor", "none",
            "-serial", "file:" + serial.as_posix(), "-no-reboot",
            "-drive", f"if=pflash,format=raw,readonly=on,file={qemu_path(args.firmware_code)}",
            "-drive", f"if=pflash,format=raw,file={qemu_path(variables)}",
            "-drive", f"if=virtio,format=qcow2,file={qemu_path(disk)}",
            "-drive", f"if=virtio,format=raw,readonly=on,file={qemu_path(seed)}",
        ]
        with host.open("xb") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=300)
        transcript = serial.read_text(encoding="utf-8", errors="replace")
        if "NEB_IMAGE_SEAL_OK" not in transcript or "NEB_IMAGE_SEAL_FAILED" in transcript:
            raise RuntimeError("The build guest did not confirm a sealed image. See the build logs.")
        disk.rename(target)
    sha256 = digest(target)
    sidecar.write_text(f"{sha256}  {target.name}\n", encoding="ascii")
    receipt.write_text(json.dumps({
        "version": version, "source_revision": revision, "base_name": args.base.name,
        "base_sha512": args.base_sha512, "linux_archive": name,
        "linux_archive_sha256": hashlib.sha256(payload).hexdigest(),
        "image_sha256": sha256, "architecture": "x86_64", "boot_mode": "UEFI", "disk_bytes": DISK_BYTES,
    }, indent=2) + "\n", encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("base", "dbus-user-session", "qemu", "qemu-img", "firmware-code", "firmware-vars", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--base-sha512", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--pycdlib-wheel", type=Path, help="load an already verified build dependency")
    args = parser.parse_args()
    if args.pycdlib_wheel:
        sys.path.insert(0, str(args.pycdlib_wheel.resolve(strict=True)))
    print(build_disk(args))


if __name__ == "__main__":
    main()
