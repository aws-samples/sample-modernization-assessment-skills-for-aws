#!/usr/bin/env python3
"""moda-skill command: configure a project for Claude Code, Codex or Kiro, check it, show versions.

Installed by install.sh as ~/.local/bin/moda-skill, which runs this file from the active version under
~/.local/share/moda-skill/versions/<version>/. Also runnable from a checkout: python3 scripts/moda_skill_cli.py ...

  moda-skill config --harness claude|codex|kiro [--scope project|user] [--project-root DIR] [--skip-mcp] [--update]
  moda-skill doctor  [--harness ...] [--scope ...] [--project-root DIR] [--no-probe]
  moda-skill version | where | list
  moda-skill use <version> | rollback | uninstall [--yes]

Python standard library only. Nothing here calls AWS.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aws-mcp-setup/scripts"))
import setup_toolkit  # noqa: E402
import doctor as doctor_module  # noqa: E402

HARNESSES = {
    # name: (skills dir token, invoke prefix, product name)
    "claude": {"dir": ".claude/skills", "invoke": "/", "product": "Claude Code"},
    "codex": {"dir": ".agents/skills", "invoke": "$", "product": "Codex CLI"},
    "kiro": {"dir": ".kiro/skills", "invoke": "/", "product": "Kiro CLI"},
}
STAGE_SKILLS = ("aws-skills-install", "aws-cli-setup", "aws-mcp-setup", "aws-as-is-analysis",
                "aws-architecture-review", "aws-modernization-pathway")
ENTRY = "moda-skill"
TEMPLATE = ROOT / "skills" / ENTRY / "SKILL.template.md"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").is_file() else "0.0.0"


WINDOWS = os.name == "nt"
# Same layout on every OS: <root>/versions/<version>/ and <root>/current, a one-line text file naming the active
# version (no symlink, so Windows needs neither Developer Mode nor administrator rights).
INSTALL_ROOT = Path(os.environ.get("MODA_SKILL_INSTALL_ROOT") or (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "moda-skill" if WINDOWS
    else Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "moda-skill"))
BIN_DIR = Path(os.environ.get("MODA_SKILL_BIN_DIR") or (INSTALL_ROOT / "bin" if WINDOWS else Path.home() / ".local/bin"))
LAUNCHERS = ("moda-skill.cmd", "moda-skill.ps1") if WINDOWS else ("moda-skill",)


def render_entry(harness, scope):
    """The entry SKILL.md for one harness: same body, harness-specific tokens and frontmatter."""
    text = TEMPLATE.read_text(encoding="utf-8")
    info = HARNESSES[harness]
    skills_dir = ("~/" if scope == "user" else "") + info["dir"]
    frontmatter = ""
    args_placeholder = "$ARGUMENTS"
    if harness == "claude":
        frontmatter = ("user-invocable: true\n"
                       "argument-hint: \"[prepare | start [opts] | status | resume | report | doctor | help | version]\"\n")
    if harness == "codex":
        args_placeholder = "the text after `$moda-skill`"
    text = (text.replace("{{CLAUDE_FRONTMATTER}}", frontmatter).replace("{{INVOKE}}", info["invoke"])
            .replace("{{SKILLS_DIR}}", skills_dir).replace("{{HARNESS}}", harness).replace("{{ARGS}}", args_placeholder))
    if "{{" in text:
        raise ValueError("unrendered token in entry skill: " + re.search(r"\{\{[A-Z_]+\}\}", text).group(0))
    return text


def stage_entry(harness, scope, staging):
    """Materialize skills/moda-skill for one harness into a staging directory (template rendered, scripts copied)."""
    target = staging / ENTRY
    shutil.copytree(ROOT / "skills" / ENTRY, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", setup_toolkit.STAMP))
    (target / "SKILL.md").write_text(render_entry(harness, scope), encoding="utf-8")
    return target


HOOK_MARK = "moda-skill/scripts/hook.py"


def python_token():
    return "py -3" if WINDOWS else "python3"


def hook_commands(harness, scope):
    skills_dir = ("~/" if scope == "user" else "") + HARNESSES[harness]["dir"]
    base = f'{python_token()} "{skills_dir}/moda-skill/scripts/hook.py"'
    return {"guard": base + " guard", "stop": base + " stop"}


def wire_hooks(harness, scope, project_root, dry_run=False):
    """Register the read-only guard (before shell tools) and the stop gate. Existing hooks of other tools are kept."""
    commands = hook_commands(harness, scope)
    root = Path.home() if scope == "user" else Path(project_root)
    if harness == "kiro":
        path = root / ".kiro/hooks/moda-skill.json"
        doc = {"version": "v1", "hooks": [
            {"name": "moda-skill read-only guard", "description": "Blocks aws CLI calls that are not describe/list/get metadata reads",
             "trigger": "PreToolUse", "matcher": "shell", "action": {"type": "command", "command": commands["guard"]}, "timeout": 20},
            {"name": "moda-skill stop gate", "description": "Keeps an assessment run going until its stages and report are complete",
             "trigger": "Stop", "action": {"type": "command", "command": commands["stop"]}, "timeout": 30},
        ]}
        content = json.dumps(doc, indent=2) + "\n"
        action = "unchanged" if path.is_file() and path.read_text(encoding="utf-8") == content else ("update" if path.exists() else "install")
        if not dry_run and action != "unchanged":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return {"action": action, "path": str(path), "trust": "Kiro asks to trust the workspace on first start; hooks run only in a trusted folder"}
    path = root / (".claude/settings.json" if harness == "claude" else ".codex/hooks.json")
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() and path.read_text(encoding="utf-8-sig").strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    wanted = {"PreToolUse": {"matcher": "Bash", "hooks": [{"type": "command", "command": commands["guard"], "timeout": 20}]},
              "Stop": {"hooks": [{"type": "command", "command": commands["stop"], "timeout": 30}]}}
    changed = False
    for event, entry in wanted.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"{path}: hooks.{event} must be a list")
        ours = [g for g in groups if isinstance(g, dict) and any(HOOK_MARK in str(h.get("command", "")) for h in g.get("hooks", []))]
        if ours == [entry]:
            continue
        hooks[event] = [g for g in groups if g not in ours] + [entry]
        changed = True
    action = "unchanged" if not changed else ("update" if path.exists() else "install")
    if not dry_run and changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak-moda"))
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    trust = ("Claude Code runs project hooks after you accept them when the session starts (or /hooks)" if harness == "claude"
             else "In Codex type /hooks, press t to trust, then Esc; hooks run from the next message")
    return {"action": action, "path": str(path), "trust": trust}


def config(args):
    import tempfile
    skills_dir, mcp_path = setup_toolkit.locations(args.harness, args.scope, args.project_root)
    actions = []
    with tempfile.TemporaryDirectory(prefix="moda-config-") as tmp:
        sources = [ROOT / "skills" / name for name in STAGE_SKILLS] + [stage_entry(args.harness, args.scope, Path(tmp))]
        plans = [setup_toolkit.copy_plan(s, skills_dir / s.name, update=args.update) for s in sources]
        for plan in plans:
            result = setup_toolkit.apply_copy(plan, dry_run=args.dry_run)
            if not args.dry_run and plan["action"] != "unchanged":
                stamp = plan["destination"] / setup_toolkit.STAMP
                data = json.loads(stamp.read_text(encoding="utf-8"))
                data["version"] = VERSION
                stamp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            actions.append(result)
    if not args.skip_mcp:
        actions.append(setup_toolkit.apply_config(setup_toolkit.config_plan(args.harness, mcp_path), dry_run=args.dry_run))
    companions = []
    if not args.skip_companions and not args.dry_run:
        wanted = [d["name"] for d in setup_toolkit.dependencies()["companions"]]
        missing = [n for n in wanted if not (skills_dir / n / "SKILL.md").is_file()]
        if missing:
            if shutil.which("git"):
                try:
                    with setup_toolkit.companion_sources() as sources:
                        for source in sources:
                            if source.name in missing or args.update:
                                companions.append(setup_toolkit.apply_copy(setup_toolkit.copy_plan(source, skills_dir / source.name, update=args.update)))
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    companions.append({"action": "skipped", "reason": f"companion fetch failed: {exc}", "fix": "re-run with network, or `/moda-skill prepare`"})
            else:
                companions.append({"action": "skipped", "reason": "git not found", "fix": "install git and re-run, or `/moda-skill prepare`"})
        else:
            companions = [{"action": "unchanged", "path": str(skills_dir / n)} for n in wanted]
    hooks = wire_hooks(args.harness, args.scope, args.project_root, dry_run=args.dry_run) if args.hooks else None
    info = HARNESSES[args.harness]
    next_step = {
        "claude": "open Claude Code in this folder (restart it if it is already open) and type /moda-skill",
        "codex": "run `codex` in this folder and type $moda-skill",
        "kiro": "run `kiro-cli chat` in this folder (restart it if it is already open), `/tools trust-all`, then type /moda-skill",
    }[args.harness]
    print(json.dumps({"harness": info["product"], "scope": args.scope, "skills_dir": str(skills_dir), "mcp_config": str(mcp_path),
                      "version": VERSION, "actions": actions, "companions": companions, "hooks": hooks, "next_step": next_step}, ensure_ascii=False, indent=2))
    return 0


def doctor(args):
    if not args.harness:
        configured = [h for h in sorted(HARNESSES)
                      if (setup_toolkit.locations(h, args.scope, args.project_root)[0] / ENTRY / "SKILL.md").is_file()]
        args.harness = configured or sorted(HARNESSES)
    out = Path(args.project_root) / ".moda" / "doctor.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    argv = ["--targets", ",".join(args.harness), "--scope", args.scope, "--project-root", str(args.project_root), "--output", str(out)]
    if not args.no_probe:
        argv.append("--probe-mcp")
    try:
        code = doctor_module.main(argv) or 0  # same interpreter, same process: no subprocess needed
    except SystemExit as exc:  # argparse errors
        code = exc.code if isinstance(exc.code, int) else 1
    sys.stdout.flush()
    entry = {h: (setup_toolkit.locations(h, args.scope, args.project_root)[0] / ENTRY / "SKILL.md").is_file() for h in args.harness}
    print(json.dumps({"entry_skill_installed": entry, "version": VERSION,
                      "hint": None if all(entry.values()) else "run `moda-skill config --harness <name>` in this folder"}))
    return code


def versions():
    base = INSTALL_ROOT / "versions"
    return sorted((p for p in base.iterdir() if p.is_dir() and (p / "VERSION").is_file()),
                  key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"[.-]", p.name)]) if base.is_dir() else []


def current_file():
    return INSTALL_ROOT / "current"


def current_version():
    marker = current_file()
    if marker.is_symlink():  # layout of the first 2.1.0 preview installs
        return marker.resolve().name
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


def use(version_name):
    target = INSTALL_ROOT / "versions" / version_name
    if not (target / "VERSION").is_file():
        raise ValueError(f"version {version_name} is not installed; see `moda-skill list`")
    marker = current_file()
    if marker.is_symlink() or marker.is_dir():
        marker.unlink()
    marker.write_text(version_name + "\n", encoding="utf-8")
    print(json.dumps({"current": version_name, "path": str(target)}))
    return 0


def rollback():
    installed = versions()
    current = current_version()
    older = [p for p in installed if p.name != current]
    if not older:
        raise ValueError("no earlier version installed")
    return use(older[-1].name)


def remove_windows_path_entry():
    """Undo the User PATH entry install.ps1 recorded as its own; never touch entries it did not add."""
    record = INSTALL_ROOT / "windows-path.json"
    if not WINDOWS or not record.is_file():
        return None
    entry = json.loads(record.read_text(encoding="utf-8")).get("added")
    if not entry:
        return None
    import winreg  # noqa: PLC0415  (Windows only)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return None
        parts = [p for p in value.split(";") if p and os.path.normcase(p.rstrip("\\")) != os.path.normcase(entry.rstrip("\\"))]
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    return entry


def uninstall(yes):
    launchers = [BIN_DIR / name for name in LAUNCHERS]
    if not yes:
        print(f"This removes {INSTALL_ROOT} and {', '.join(str(p) for p in launchers)} (project .kiro/.claude/.agents skills stay). Re-run with --yes.")
        return 1
    removed = []
    for launcher in launchers:
        if launcher.exists() and "moda" in launcher.read_text(encoding="utf-8", errors="replace"):
            launcher.unlink()
            removed.append(str(launcher))
    path_entry = remove_windows_path_entry()
    if INSTALL_ROOT.is_dir():
        shutil.rmtree(INSTALL_ROOT)
        removed.append(str(INSTALL_ROOT))
    print(json.dumps({"removed": removed, "user_path_entry_removed": path_entry}))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="moda-skill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("config", help="project the skills and MCP config into a project (or your home)")
    c.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    c.add_argument("--scope", choices=("project", "user"), default="project")
    c.add_argument("--project-root", type=Path, default=Path.cwd())
    c.add_argument("--skip-mcp", action="store_true")
    c.add_argument("--skip-companions", action="store_true", help="do not fetch the pinned aws-diagram-design / aws-bento-deck skills")
    c.add_argument("--hooks", action="store_true", help="also register the read-only guard and stop-gate hooks for the client (needs the client's hook trust)")
    c.add_argument("--update", action="store_true", help="replace an installed, unmodified older copy")
    c.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("doctor", help="check prerequisites, installed skills and the MCP connection")
    d.add_argument("--harness", nargs="+", choices=sorted(HARNESSES),
                   help="clients to check; default: those with the entry skill installed here, else all")
    d.add_argument("--scope", choices=("project", "user"), default="project")
    d.add_argument("--project-root", type=Path, default=Path.cwd())
    d.add_argument("--no-probe", action="store_true")
    r = sub.add_parser("render", help="print the entry SKILL.md for a harness (for inspection)")
    r.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    r.add_argument("--scope", choices=("project", "user"), default="project")
    sub.add_parser("version")
    sub.add_parser("where", help="print the active install path")
    sub.add_parser("list", help="installed versions")
    u = sub.add_parser("use", help="switch the active version")
    u.add_argument("version")
    sub.add_parser("rollback", help="switch to the previous installed version")
    up = sub.add_parser("update", help="install the latest release with install.sh and make it current")
    up.add_argument("--base-url", help="release base URL (default: GitHub releases/latest, or $MODA_SKILL_BASE_URL)")
    up.add_argument("--from", dest="from_zip", help="install from a local zip instead of downloading")
    x = sub.add_parser("uninstall")
    x.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "config":
            return config(args)
        if args.command == "doctor":
            return doctor(args)
        if args.command == "render":
            sys.stdout.write(render_entry(args.harness, args.scope))
            return 0
        if args.command == "version":
            print(f"moda-skill {VERSION}")
            return 0
        if args.command == "where":
            print(str(ROOT))
            return 0
        if args.command == "list":
            current = current_version()
            for p in versions():
                print(("* " if p.name == current else "  ") + p.name)
            return 0
        if args.command == "use":
            return use(args.version)
        if args.command == "rollback":
            return rollback()
        if args.command == "uninstall":
            return uninstall(args.yes)
        if args.command == "update":
            if args.base_url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%/-]*)?", args.base_url):
                raise ValueError("--base-url must be an https URL without query or credentials")
            if args.from_zip and not Path(args.from_zip).is_file():
                raise ValueError(f"--from: {args.from_zip} is not a file")
            env = dict(os.environ, MODA_SKILL_INSTALL_ROOT=str(INSTALL_ROOT), MODA_SKILL_BIN_DIR=str(BIN_DIR))
            # list form, no shell; the installer script path is fixed and the two user values were validated above
            if WINDOWS:
                options = [*(["-From", args.from_zip] if args.from_zip else []), *(["-BaseUrl", args.base_url] if args.base_url else [])]
                return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/install.ps1"), *options], env=env).returncode  # nosec B603 B607
            options = [*(["--from", args.from_zip] if args.from_zip else []), *(["--base-url", args.base_url] if args.base_url else [])]
            return subprocess.run(["sh", str(ROOT / "scripts/install.sh"), *options], env=env).returncode  # nosec B603 B607
    except (ValueError, OSError) as exc:
        print(f"moda-skill: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
