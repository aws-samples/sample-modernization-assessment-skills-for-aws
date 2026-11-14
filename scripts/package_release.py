#!/usr/bin/env python3
"""Build a release: dist/moda-skill-<version>.zip, SHA256SUMS, VERSION, install.sh and install.ps1.

The zip holds the tracked repository files (git archive semantics) under moda-skill-<version>/. Upload the five
files to the GitHub release v<version>; install.sh / install.ps1 download VERSION, the zip and SHA256SUMS from
releases/latest/download/ (or any --base-url that serves the same names).

  python3 scripts/package_release.py [--output dist] [--check]
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile



ROOT = Path(__file__).resolve().parents[1]


def version():
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def tracked_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)  # nosec B603 B607 - static argv
    files = [f for f in out.stdout.decode().split("\0") if f]
    # Untracked but required at runtime in a fresh checkout: nothing; everything ships from git.
    return [f for f in files if (ROOT / f).is_file()]


def build(output):
    v = version()
    output.mkdir(parents=True, exist_ok=True)
    zip_path = output / f"moda-skill-{v}.zip"
    prefix = f"moda-skill-{v}/"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for relative in sorted(tracked_files()):
            path = ROOT / relative
            info = zipfile.ZipInfo(prefix + relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if path.suffix in {".sh", ".py"} or relative.startswith("scripts/") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    shutil.copy2(ROOT / "scripts/install.sh", output / "install.sh")
    shutil.copy2(ROOT / "scripts/install.ps1", output / "install.ps1")
    (output / "VERSION").write_text(v + "\n", encoding="utf-8")
    sums = []
    for name in (zip_path.name, "install.sh", "install.ps1"):
        sums.append(f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}")
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return {"version": v, "zip": str(zip_path), "bytes": zip_path.stat().st_size, "files": len(tracked_files()),
            "sha256": sums[0].split()[0], "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
