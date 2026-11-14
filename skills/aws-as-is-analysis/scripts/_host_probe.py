"""Fixed Linux metadata probe. No argv/environ, source bodies, logs or DB queries."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import time


PROBE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin", "LANG": "C", "LC_ALL": "C",
             "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}

MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml", "pom.xml")
RUNTIMES = ("node", "python", "java", "dotnet", "ruby", "php", "gunicorn", "uvicorn",
            "nginx", "httpd", "apache", "postgres", "mysqld", "redis", "mongod")
MAX_OUTPUT = 20000  # Below SSM's 24,000-character inline stdout limit.


def bounded_read(path, limit=65536):
    """Only bounded regular files; final-component symlinks/FIFOs are refused."""
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("not_regular")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file_limit")
        return data
    finally:
        os.close(descriptor)


def clean(value, limit=200):
    return "".join(c for c in str(value) if c.isprintable())[:limit]


def package_name(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^([A-Za-z0-9_@][A-Za-z0-9_./@-]{0,150})(?=$|[ \t<>=!~;\[])", value)
    return match.group(1) if match and "://" not in match.group(1) else None


def manifest_summary(name, data):
    result = {"file": name, "sha256": hashlib.sha256(data).hexdigest()}
    text = data.decode("utf-8")
    if name == "package.json":
        doc = json.loads(text)
        result["package"] = package_name(doc.get("name"))
        version = doc.get("version", "")
        result["version"] = version if isinstance(version, str) and re.fullmatch(r"[0-9][A-Za-z0-9.+-]{0,60}", version) else None
        deps = doc.get("dependencies", {})
        result["dependency_names"] = sorted(k for k in deps if isinstance(k, str) and package_name(k) == k)[:30] if isinstance(deps, dict) else []
    elif name in {"pyproject.toml", "Cargo.toml"}:
        try:
            import tomllib
            doc = tomllib.loads(text)
            metadata = doc.get("project" if name == "pyproject.toml" else "package", {})
            result["package"] = package_name(metadata.get("name"))
            deps = metadata.get("dependencies", []) if name == "pyproject.toml" else doc.get("dependencies", {})
            result["dependency_names"] = [p for v in deps if (p := package_name(v))][:30]
        except ImportError:
            result["parser_status"] = "tomllib_unavailable"
    elif name == "requirements.txt":
        result["dependency_names"] = [p for line in text.splitlines() if (p := package_name(line.strip()))][:30]
    elif name == "go.mod":
        match = re.search(r"^module ([A-Za-z0-9._/-]{1,180})$", text, re.M)
        result["module"] = match.group(1) if match else None
    # pom.xml is fingerprinted only; never resolve external XML entities.
    return result


def inspect_root(root, processes):
    root = Path(root).resolve()
    if str(root) == "/" or root.parts[1:2] in [("proc",), ("sys",), ("dev",)]:
        raise ValueError("resolved_root_not_allowed")
    result = {"root": clean(root, 400), "status": "observed", "manifests": [],
              "running_pids_with_matching_cwd": [p["pid"] for p in processes if p.get("cwd") == str(root)],
              "code_identity": "deployed_tree_only; not proof of code loaded by process"}
    if not root.is_dir():
        result["status"] = "unavailable"
        return result
    for name in MANIFESTS:
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("outside_root")
            result["manifests"].append(manifest_summary(name, bounded_read(path)))
        except (OSError, ValueError, TypeError, AttributeError):
            result["manifests"].append({"file": name, "status": "unreadable_or_unsupported"})
            result["status"] = "partial"
    try:
        entries = []
        with os.scandir(root) as iterator:
            for number, entry in enumerate(iterator):
                if number >= 100:
                    result["directory_listing_limited"] = True
                    break
                if entry.is_file(follow_symlinks=False) and entry.name.endswith((".jar", ".war", ".dll", ".py", ".js", ".mjs")):
                    entries.append(clean(entry.name))
        result["entry_file_names"] = entries[:20]
        if len(entries) > 20:
            result["directory_listing_limited"] = True
    except OSError:
        result["directory_listing_status"] = "unavailable"
    git = root / ".git"
    if git.is_dir() and not git.is_symlink():
        try:
            head = bounded_read(git / "HEAD", 512).decode().strip()
            commit = head
            if head.startswith("ref: refs/heads/"):
                ref = head[5:]
                if not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", ref) or ".." in Path(ref).parts:
                    raise ValueError("invalid_ref")
                result["git_branch"] = ref[len("refs/heads/"):]
                ref_file = git / ref
                if not ref_file.resolve().is_relative_to(git.resolve()):
                    raise ValueError("ref_outside_root")
                commit = bounded_read(ref_file, 128).decode().strip()
            result["git_commit"] = commit if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) else None
        except (OSError, ValueError):
            result["git_status"] = "unavailable_or_packed_ref"
    elif git.exists():
        result["git_status"] = "external_worktree_or_symlink_not_read"
    if result.get("directory_listing_limited"):
        result["status"] = "partial"
    return result


def command_rows(run, limit=30):
    """`run` performs one fixed, read-only command (static argv written at the call site, PROBE_ENV, 4 s timeout)."""
    try:
        response = run()
        if response.returncode:
            return {"status": "unavailable", "items": []}
        lines = response.stdout.splitlines()
        return {"status": "partial" if len(lines) > limit else "observed",
                "items": lines[:limit]}
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable", "items": []}


def probe(config, proc_root=Path("/proc")):
    started = time.monotonic()
    result = {"schema_version": "1.0", "platform": platform.system(),
              "collector": "moda-host-metadata-v1", "status": "observed",
              "processes": [], "services": {"status": "not_checked", "items": []},
              "containers": {"status": "not_checked", "items": []}, "applications": [], "limitations": []}
    if platform.system() != "Linux" or sys.version_info < (3, 9):
        result.update(status="unsupported")
        return result
    result["kernel"] = platform.release()
    result["effective_uid"] = os.geteuid()
    try:
        os_release = bounded_read("/etc/os-release", 8192).decode()
        result["os_release"] = {k: clean(v.strip('"')) for line in os_release.splitlines()
                                if "=" in line for k, v in [line.split("=", 1)] if k in ("NAME", "VERSION_ID")}
    except (OSError, ValueError):
        result["os_release"] = None
    missing = 0
    try:
        pids = sorted(int(p.name) for p in proc_root.iterdir() if p.name.isdigit())
        if len(pids) > 500:
            result["limitations"].append("process_scan_limit")
        for pid in pids[:500]:
            if time.monotonic() - started > 12:
                result["limitations"].append("probe_time_limit")
                break
            base = proc_root / str(pid)
            try:
                name = clean(bounded_read(base / "comm", 256).decode().strip(), 80)
                row = {"pid": pid, "name": name}
                for key in ("exe", "cwd"):
                    try:
                        row[key] = clean(os.readlink(base / key), 400)
                    except OSError:
                        missing += 1
                result["processes"].append(row)
            except (OSError, ValueError):
                missing += 1
        result["processes"].sort(key=lambda p: (not any(n in p["name"].lower() for n in RUNTIMES), p["pid"]))
        if len(result["processes"]) > 50:
            result["limitations"].append("process_output_limit")
            result["processes"] = result["processes"][:50]
    except OSError:
        result["limitations"].append("proc_unavailable")
    if missing:
        result["limitations"].append("process_fields_unavailable_or_exited")
    if Path("/usr/bin/systemctl").exists():
        services = command_rows(lambda: subprocess.run(["/usr/bin/systemctl", "list-units", "--type=service", "--state=running",  # nosec B603 B607 - static argv
                                                        "--no-legend", "--plain", "--no-pager"], capture_output=True, text=True, env=PROBE_ENV, timeout=4))
        result["services"] = {"status": services["status"], "items": [
            {"unit": clean(fields[0]), "active": clean(fields[2]), "sub": clean(fields[3])}
            for line in services["items"] if len(fields := line.split()) >= 4]}
    docker = any(Path(p).is_file() for p in ("/usr/bin/docker", "/usr/local/bin/docker"))  # resolved through PROBE_ENV's PATH
    if docker and Path("/var/run/docker.sock").exists():
        raw = command_rows(lambda: subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", "ps", "--format",  # nosec B603 B607 - static argv
                                                   '{"id":"{{.ID}}","image":"{{.Image}}","name":"{{.Names}}","state":"{{.State}}"}'],
                                                  capture_output=True, text=True, env=PROBE_ENV, timeout=4))
        rows = []
        for line in raw["items"]:
            try:
                d = json.loads(line)
                rows.append({k: clean(d.get(k, "")) for k in ("id", "image", "name", "state")})
            except ValueError:
                raw["status"] = "partial"
        result["containers"] = {"status": raw["status"], "items": rows}
    for root in config.get("app_roots", [])[:8]:
        try:
            result["applications"].append(inspect_root(root, result["processes"]))
        except (OSError, ValueError):
            result["applications"].append({"root": clean(root, 400), "status": "unavailable"})
    if not config.get("app_roots"):
        result["limitations"].append("application_roots_not_requested")
    if any(r.get("status") != "observed" for r in result["applications"]):
        result["limitations"].append("application_metadata_incomplete")
    if result["services"]["status"] != "observed" or result["containers"]["status"] != "observed":
        result["limitations"].append("service_or_container_coverage_incomplete")
    if result["limitations"]:
        result["status"] = "partial"
    # Preserve a valid JSON document when lists exceed inline transport capacity.
    while len(json.dumps(result, ensure_ascii=True).encode()) > MAX_OUTPUT:
        result["status"] = "partial"
        if "output_size_limit" not in result["limitations"]:
            result["limitations"].append("output_size_limit")
        if result["processes"]:
            result["processes"].pop()
        elif result["applications"]:
            result["applications"].pop()
        elif result["containers"]["items"]:
            result["containers"]["items"].pop()
        elif result["services"]["items"]:
            result["services"]["items"].pop()
        else:
            raise ValueError("output_limit")
    return result


if __name__ == "__main__":
    try:
        CONFIG  # noqa: B018 - defined by the two lines inspect_host.payload() prepends to this file
    except NameError:
        CONFIG = {}
    print(json.dumps(probe(CONFIG), ensure_ascii=True))
