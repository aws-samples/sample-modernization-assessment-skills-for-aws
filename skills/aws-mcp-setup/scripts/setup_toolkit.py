#!/usr/bin/env python3
"""Install pinned companion skills and merge AWS Knowledge MCP configuration."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid


def checked_arguments(arguments):
    """Guard for every CLI invocation: list form only (no shell), the program name is a literal at the call site
    (`["git", *checked_arguments(...)]`), and every argument is a string without NUL or newline. Arguments come from
    our own tables, pinned lock-file values or validated CLI options."""
    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise ValueError("command arguments must be a non-empty list")
    for item in arguments:
        if not isinstance(item, str) or "\x00" in item or "\n" in item or "\r" in item:
            raise ValueError("command arguments must be strings without control characters")
    return list(arguments)

SKILL_DIR = Path(__file__).resolve().parents[1]
STAMP = ".moda-install.json"
MCP_URL = "https://knowledge-mcp.global.api.aws"
TARGETS = ("claude", "codex", "kiro")


def targets(value):
    result = list(dict.fromkeys(value.split(",")))
    if not result or any(x not in TARGETS for x in result):
        raise argparse.ArgumentTypeError("targets: claude,codex,kiro")
    return result


def locations(target, scope, project_root, home=None):
    root = Path(home or Path.home()) if scope == "user" else Path(project_root)
    skill_paths = {"claude": ".claude/skills", "codex": ".agents/skills",
                   "kiro": ".kiro/skills"}
    configs = {"claude": ".claude.json" if scope == "user" else ".mcp.json",
               "codex": ".codex/config.toml", "kiro": ".kiro/settings/mcp.json"}
    return root.resolve() / skill_paths[target], root.resolve() / configs[target]


def digest_tree(path):
    digest = hashlib.sha256()
    for file in sorted(Path(path).rglob("*")):
        if file.is_symlink():
            raise ValueError(f"symlink not supported in skill content: {file}")
        relative = file.relative_to(path)
        if any(x in {"__pycache__", ".git", ".DS_Store"} for x in relative.parts):
            continue
        if file.is_file() and file.name != STAMP and file.suffix != ".pyc":
            digest.update(relative.as_posix().encode())
            digest.update(b"\0")
            digest.update(file.read_bytes())
    return digest.hexdigest()


def copy_plan(source, destination, update=False):
    source, destination = Path(source), Path(destination)
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"SKILL.md missing: {source}")
    source_digest = digest_tree(source)
    if destination.is_symlink():
        raise ValueError(f"existing symlink preserved: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"destination is not a directory: {destination}")
        current_digest = digest_tree(destination)
        if current_digest == source_digest:
            return {"source": source, "destination": destination, "action": "unchanged",
                    "digest": source_digest}
        stamp_file = destination / STAMP
        if not update or not stamp_file.is_file():
            raise ValueError(f"existing skill preserved; managed update required: {destination}")
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        if stamp.get("package") != "moda-skills" or stamp.get("sha256") != current_digest:
            raise ValueError(f"locally modified skill preserved: {destination}")
        action = "update"
    else:
        action = "install"
    return {"source": source, "destination": destination, "action": action,
            "digest": source_digest}


def apply_copy(plan, dry_run=False):
    display = {"action": plan["action"], "path": str(plan["destination"])}
    if dry_run or plan["action"] == "unchanged":
        return display
    destination = plan["destination"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / (destination.name + ".new-" + uuid.uuid4().hex[:8])
    backup = None
    try:
        shutil.copytree(plan["source"], staging,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".DS_Store", STAMP))
        (staging / STAMP).write_text(json.dumps({
            "package": "moda-skills", "sha256": plan["digest"],
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            # Keep backups outside skills/ so clients do not discover duplicate skills.
            backup_root = destination.parent.parent / ".moda-backups" / "skills"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (destination.name + ".bak-" + uuid.uuid4().hex[:8])
            destination.rename(backup)
            display["backup"] = str(backup)
        staging.rename(destination)
    except Exception:
        if backup and not destination.exists():
            backup.rename(destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return display


def config_plan(target, path):
    """Parse before changing anything. Never replace another server or credentials."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if target == "codex":
        data = tomllib.loads(text)
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"invalid mcp_servers: {path}")
        if "aws-knowledge" in servers:
            if servers["aws-knowledge"].get("url") != MCP_URL:
                raise ValueError(f"MCP name conflict preserved: {path}")
            if servers["aws-knowledge"].get("enabled") is False:
                raise ValueError(f"existing MCP is disabled: {path}")
            return {"path": path, "action": "unchanged", "content": text}
        content = text.rstrip() + '\n\n[mcp_servers.aws-knowledge]\nurl = "' + MCP_URL + '"\n'
        tomllib.loads(content)
    else:
        data = json.loads(text) if text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError(f"config must be an object: {path}")
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"invalid mcpServers: {path}")
        wanted = ({"type": "http", "url": MCP_URL} if target == "claude" else
                  {"url": MCP_URL, "disabled": False, "autoApprove": []})
        if "aws-knowledge" in servers:
            server = servers["aws-knowledge"]
            if not isinstance(server, dict) or server.get("url") != MCP_URL:
                raise ValueError(f"MCP name conflict preserved: {path}")
            if server.get("disabled") is True or (target == "claude" and server.get("type") not in {"http", "streamable-http"}):
                raise ValueError(f"existing MCP needs manual repair: {path}")
            return {"path": path, "action": "unchanged", "content": text}
        servers["aws-knowledge"] = wanted
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return {"path": path, "action": "merge-mcp", "content": content}


def apply_config(plan, dry_run=False):
    display = {"action": plan["action"], "path": str(plan["path"])}
    if dry_run or plan["action"] == "unchanged":
        return display
    path = plan["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + uuid.uuid4().hex[:8])
        shutil.copy2(path, backup)
        try:
            backup.chmod(0o600)
        except OSError:
            pass
        display["backup"] = str(backup)
    descriptor, staging = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(plan["content"])
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    return display


def dependencies():
    return json.loads((SKILL_DIR / "assets/dependencies.lock.json").read_text(encoding="utf-8"))


@contextmanager
def companion_sources():
    if not shutil.which("git"):
        raise ValueError("Git is required for --install-companions")
    with tempfile.TemporaryDirectory(prefix="moda-companions-") as temporary:
        root = Path(temporary)
        sources = []
        for item in dependencies()["companions"]:
            repo = root / (item["name"] + "-repo")
            steps = [
                ["init", str(repo)],
                ["-C", str(repo), "remote", "add", "origin", item["repository"]],
                ["-C", str(repo), "fetch", "--depth", "1", "origin", item["commit"]],
                ["-C", str(repo), "checkout", "--detach", "FETCH_HEAD"],
            ]
            for arguments in steps:
                response = subprocess.run(["git", *checked_arguments(arguments)], capture_output=True, text=True, timeout=180)  # nosec B603 B607 - pinned repo/commit from the lock file
                if response.returncode:
                    raise ValueError(f"pinned checkout failed: {item['name']}")
            revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=15).strip()  # nosec B603 B607 - static argv
            if revision != item["commit"]:
                raise ValueError("upstream commit mismatch")
            prepared = root / item["name"]
            shutil.copytree(repo / item["path"], prepared)
            for name in item.get("license_files", []):
                source = repo / name
                if source.is_file():
                    shutil.copy2(source, prepared / ("UPSTREAM-" + source.name))
            (prepared / "UPSTREAM.json").write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")
            sources.append(prepared)
        yield sources


def add_location_arguments(parser):
    parser.add_argument("--targets", type=targets, default=list(TARGETS))
    parser.add_argument("--scope", choices=("project", "user"), default="project")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update", action="store_true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_location_arguments(parser)
    parser.add_argument("--install-companions", action="store_true")
    parser.add_argument("--configure-mcp", action="store_true")
    args = parser.parse_args()
    if not args.install_companions and not args.configure_mcp:
        parser.error("choose --install-companions and/or --configure-mcp")
    try:
        places = [locations(t, args.scope, args.project_root) for t in args.targets]
        configs = [config_plan(t, p[1]) for t, p in zip(args.targets, places)] if args.configure_mcp else []
        actions = []
        if args.install_companions:
            if args.dry_run:
                actions += [{"action": "fetch-pinned-and-install", "name": d["name"],
                             "commit": d["commit"], "path": str(p[0] / d["name"])}
                            for d in dependencies()["companions"] for p in places]
            else:
                with companion_sources() as sources:
                    plans = [copy_plan(s, p[0] / s.name, args.update) for s in sources for p in places]
                    actions += [apply_copy(p) for p in plans]
        actions += [apply_config(p, args.dry_run) for p in configs]
        print(json.dumps({"dry_run": args.dry_run, "actions": actions}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Exception text above contains paths or fixed labels, never config content.
        print(f"Setup stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
