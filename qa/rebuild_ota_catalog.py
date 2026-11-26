"""Refresh the compact public fallback catalog from published GitHub releases."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import neb_release as releases

VARIANTS = (("linux", None), ("linux", "6.6"), ("linux", "6.12"), ("windows", None), ("windows-desktop", None))


def catalog(items):
    rows = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        channel = "testing" if entry.get("prerelease") is True else "stable"
        names = set()
        for platform, kernel in VARIANTS:
            candidate = releases.select([entry], channel=channel, platform=platform, kernel=kernel)
            if candidate:
                names.add(candidate.name)
        if not names:
            continue
        row = {key: entry[key] for key in ("tag_name", "draft", "prerelease", "published_at", "html_url")}
        body = entry.get("body")
        row["body"] = body[:6000] if isinstance(body, str) else ""
        row["assets"] = [
            {key: asset[key] for key in ("name", "browser_download_url", "digest", "size", "state")}
            for asset in entry["assets"] if isinstance(asset, dict) and asset.get("name") in names
        ]
        rows.append(row)
    rows.sort(key=lambda entry: releases.version(entry["tag_name"]), reverse=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    endpoint = f"repos/{releases.REPO}/releases?per_page=100"
    data = json.loads(subprocess.check_output(["gh", "api", endpoint], cwd=ROOT, timeout=25))
    if not isinstance(data, list):
        raise ValueError("Expected a GitHub release list")
    result = json.dumps(catalog(data), ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(result, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output.name}")


if __name__ == "__main__":
    main()
