"""Build the unpackaged, self-contained x64 Windows candidate."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


SDK = "8.0.428"
RUNTIME = "8.0.34"
RID = "win-x64"
NUGET = "https://api.nuget.org/v3/index.json"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

REQUIRED = {
    "Apollo.NEB.Desktop.exe", "Apollo.NEB.Desktop.dll",
    "Apollo.NEB.Desktop.deps.json", "Apollo.NEB.Desktop.runtimeconfig.json",
    "Apollo.NEB.Desktop.pri",
    "hostfxr.dll", "hostpolicy.dll", "coreclr.dll", "System.Private.CoreLib.dll",
    "Microsoft.UI.Xaml.dll", "Microsoft.WindowsAppRuntime.dll",
}
FORBIDDEN_PARTS = {"bin", "obj", "smoke", "test", "tests", ".git", "__pycache__"}
FORBIDDEN_SUFFIXES = {".cer", ".crt", ".p12", ".pfx", ".pem", ".key", ".token", ".msix", ".appx",
                      ".msixbundle", ".appxbundle", ".cs", ".csproj", ".sqlite", ".nupkg"}
FORBIDDEN_NAMES = {"launchsettings.json", "appsettings.development.json", "usersecrets.json",
                   "fleet-link.json", "owner-login.json", "profile.json", "settings.json"}


def release_version(source):
    assignments = [
        item.value for item in ast.parse(source.read_text(encoding="utf-8")).body
        if isinstance(item, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in item.targets)
    ]
    if len(assignments) != 1:
        raise ValueError("Expected one VERSION assignment in neb_release.py.")
    version = ast.literal_eval(assignments[0])
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?", version):
        raise ValueError("Invalid release VERSION.")
    return version


def publish(project, area, version):
    env = dict(os.environ)
    env.setdefault("NUGET_PACKAGES", str(area / "packages"))
    env.update(DOTNET_CLI_HOME=str(area / "dotnet-home"), DOTNET_NOLOGO="1",
               DOTNET_CLI_TELEMETRY_OPTOUT="1", NUGET_HTTP_CACHE_PATH=str(area / "http-cache"))
    dotnet = shutil.which("dotnet.exe")
    if not dotnet:
        raise ValueError(".NET SDK is missing.")
    actual = subprocess.run([dotnet, "--version"], check=True, env=env, text=True,
                            capture_output=True).stdout.strip()
    if actual != SDK:
        raise ValueError(f"Expected .NET SDK {SDK}; found {actual}.")

    objects = area / "build"
    target = area / "publish"
    common = [str(project), "--runtime", RID, "--artifacts-path", str(objects)]
    subprocess.run([dotnet, "restore", *common, "--source", NUGET, "--locked-mode",
                    "-p:SelfContained=true"], check=True, env=env)
    subprocess.run([dotnet, "publish", *common, "--no-restore", "--configuration", "Release",
                    "--self-contained", "true", "--output", str(target), "--nologo",
                    f"-p:Version={version}", "-p:DebugSymbols=false", "-p:DebugType=none",
                    "-p:ContinuousIntegrationBuild=true"], check=True, env=env)
    return target


def runtime_files(target):
    if not all((target / name).is_file() for name in REQUIRED):
        raise ValueError("The publish directory lacks the app-local .NET or Windows App SDK runtime.")
    options = json.loads((target / "Apollo.NEB.Desktop.runtimeconfig.json").read_text(
        encoding="utf-8"))["runtimeOptions"]
    if options.get("framework") or options.get("frameworks") or not any(
        reference == {"name": "Microsoft.NETCore.App", "version": RUNTIME}
        for reference in options.get("includedFrameworks", [])
    ):
        raise ValueError(f"The package must include .NET Runtime {RUNTIME} for win-x64.")

    seen = set()
    for item in sorted(target.rglob("*")):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("The publish directory contains a link.")
        if not item.is_file():
            continue
        relative = item.relative_to(target)
        if (FORBIDDEN_PARTS.intersection(part.lower() for part in relative.parts)
                or item.suffix.lower() in FORBIDDEN_SUFFIXES or item.name.lower() in FORBIDDEN_NAMES
                or ".smoke." in item.name.lower()):
            raise ValueError(f"Unexpected file in publish directory: {relative.as_posix()}")
        if item.suffix.lower() == ".pdb":
            continue
        normalized = relative.as_posix().lower()
        if normalized in seen:
            raise ValueError("The archive has an ambiguous Windows path.")
        seen.add(normalized)
        yield relative.as_posix(), item


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_candidate(target, work, version):
    root = f"Apollo.NEB.Desktop-{version}-windows-x64"
    archive = work / f"apollo-neb-desktop-{version}-windows-x64.zip"
    with ZipFile(archive, "x", ZIP_DEFLATED) as dest:
        for relative, path in runtime_files(target):
            entry = ZipInfo(f"{root}/{relative}", ZIP_DATE)
            entry.compress_type = ZIP_DEFLATED
            with path.open("rb") as source, dest.open(entry, "w", force_zip64=True) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
        version_entry = ZipInfo(f"{root}/VERSION.txt", ZIP_DATE)
        dest.writestr(version_entry, version + "\n")
    with ZipFile(archive) as check:
        if check.testzip() is not None:
            raise ValueError("Archive content failed verification.")
    sidecar = archive.with_suffix(".zip.sha256")
    with sidecar.open("x", encoding="ascii", newline="\n") as output:
        output.write(f"{checksum(archive)}  {archive.name}\n")
    return archive, sidecar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent / "Apollo.NEB.Desktop.csproj"
    version = release_version(project.parents[2] / "neb_release.py")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    candidate = output / f"apollo-neb-desktop-{version}-windows-x64"
    if candidate.exists():
        raise FileExistsError(f"Candidate already exists: {candidate}")
    # The WinUI compiler still needs short paths for some of its NuGet inputs.
    # Keep only the final atomic rename on the selected destination volume.
    with tempfile.TemporaryDirectory(prefix="nebwin-") as scratch:
        work = Path(scratch)
        target = publish(project, work, version)
        archive, sidecar = archive_candidate(target, work, version)
        with tempfile.TemporaryDirectory(prefix=".nebpack-", dir=output) as staging:
            ready = Path(staging) / "candidate"
            ready.mkdir()
            shutil.move(archive, ready / archive.name)
            shutil.move(sidecar, ready / sidecar.name)
            ready.rename(candidate)
    print(candidate / archive.name)
    print(candidate / sidecar.name)


if __name__ == "__main__":
    main()
