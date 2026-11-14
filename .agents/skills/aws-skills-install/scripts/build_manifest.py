#!/usr/bin/env python3
"""Build or check assets/install-manifest.json and the bootstrap copies of this skill.

The manifest lets an agent install the assessment skills by hand (copy listed files, write the
stamp, verify hashes). The per-skill digest is setup_toolkit.digest_tree, so doctor.py and the
companion/update logic in setup_toolkit recognise a guide-driven installation as managed.

  python3 build_manifest.py            # rewrite the manifest and sync bootstrap copies
  python3 build_manifest.py --check    # exit 1 if the manifest or bootstrap copies are stale
  python3 build_manifest.py --digest DIR   # print the stamp digest of a skill directory
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

SKILL_DIR = Path(__file__).resolve().parents[1]
ROOT = SKILL_DIR.parents[1]
sys.path.insert(0, str(ROOT / "skills/aws-mcp-setup/scripts"))
import setup_toolkit  # noqa: E402

SKILLS = ("aws-cli-setup", "aws-mcp-setup", "aws-as-is-analysis", "aws-architecture-review", "aws-modernization-pathway", "moda-skill")
# This installer skill is not listed in its own manifest (the manifest is part of its content); the bootstrap
# copies under .claude/.agents/.kiro carry it and SKILL.md step 7 installs it.
BOOTSTRAP = (".claude/skills", ".agents/skills", ".kiro/skills")
IGNORED_PARTS = {"__pycache__", ".git", ".DS_Store"}
MANIFEST = SKILL_DIR / "assets/install-manifest.json"


def listed_files(skill_dir):
    files = []
    for file in sorted(skill_dir.rglob("*")):
        relative = file.relative_to(skill_dir)
        if any(p in IGNORED_PARTS for p in relative.parts) or not file.is_file():
            continue
        if file.name == setup_toolkit.STAMP or file.suffix == ".pyc":
            continue
        files.append({"path": relative.as_posix(), "bytes": file.stat().st_size,
                      "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    return files


def build():
    lock = setup_toolkit.dependencies()
    url = lock["mcp"]["url"]
    skills = []
    for name in SKILLS:
        skill_dir = ROOT / "skills" / name
        digest = setup_toolkit.digest_tree(skill_dir)
        skills.append({"name": name, "source": f"skills/{name}", "digest": digest,
                       "stamp": {"package": "moda-skills", "sha256": digest}, "files": listed_files(skill_dir)})
    return {
        "schema_version": "1.0",
        "digest_algorithm": "setup_toolkit.digest_tree: sha256 over sorted relative POSIX paths, NUL and file bytes; "
                            "excludes __pycache__, .git, .DS_Store, *.pyc and .moda-install.json",
        "stamp_file": setup_toolkit.STAMP,
        "paths": {
            "claude": {"project_skills": ".claude/skills", "user_skills": "~/.claude/skills", "project_mcp": ".mcp.json", "user_mcp": "~/.claude.json"},
            "codex": {"project_skills": ".agents/skills", "user_skills": "~/.agents/skills", "project_mcp": ".codex/config.toml", "user_mcp": "~/.codex/config.toml"},
            "kiro": {"project_skills": ".kiro/skills", "user_skills": "~/.kiro/skills", "project_mcp": ".kiro/settings/mcp.json", "user_mcp": "~/.kiro/settings/mcp.json"},
        },
        "mcp": {
            "name": lock["mcp"]["name"], "url": url, "aws_credentials_required": lock["mcp"]["aws_credentials_required"],
            "claude": {"mcpServers": {"aws-knowledge": {"type": "http", "url": url}}},
            "codex_toml": f'[mcp_servers.aws-knowledge]\nurl = "{url}"\n',
            "kiro": {"mcpServers": {"aws-knowledge": {"url": url, "disabled": False, "autoApprove": []}}},
            "merge_rules": ["parse the existing file first and keep every other server", "leave an existing aws-knowledge entry with the same URL untouched",
                            "report, do not change, an aws-knowledge entry with another URL or disabled=true", "write a backup beside the file before replacing it",
                            "never add wildcard auto-approval or AWS credentials"],
        },
        "companions": lock["companions"],
        "entry_skill": {"name": "moda-skill", "note": "skills/moda-skill/SKILL.template.md is rendered per harness by scripts/moda_skill_cli.py "
                        "(tokens {{INVOKE}} {{SKILLS_DIR}} {{HARNESS}} {{ARGS}} {{CLAUDE_FRONTMATTER}}); `moda-skill config` installs the rendered "
                        "copy, so its installed digest differs from this source digest by design"},
        "skills": skills,
    }


def render(manifest):
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def bootstrap_targets():
    return [ROOT / base / "aws-skills-install" for base in BOOTSTRAP]


ENTRY_HARNESS = {".claude/skills": "claude", ".agents/skills": "codex", ".kiro/skills": "kiro"}


def entry_targets():
    return [(ROOT / base / "moda-skill", harness) for base, harness in ENTRY_HARNESS.items()]


def rendered_entry(harness):
    sys.path.insert(0, str(ROOT / "scripts"))
    import moda_skill_cli  # noqa: E402
    return moda_skill_cli.render_entry(harness, "project")


def sync_bootstrap():
    source = ROOT / "skills/aws-skills-install"
    for target in bootstrap_targets():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", setup_toolkit.STAMP))
    # The entry skill is rendered per client so a plain `git clone` already offers /moda-skill ($moda-skill on Codex).
    for target, harness in entry_targets():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(ROOT / "skills/moda-skill", target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", setup_toolkit.STAMP))
        (target / "SKILL.md").write_text(rendered_entry(harness), encoding="utf-8")


def check():
    problems = []
    expected = render(build())
    if not MANIFEST.is_file() or MANIFEST.read_text(encoding="utf-8") != expected:
        problems.append("assets/install-manifest.json is stale; run build_manifest.py")
    source = ROOT / "skills/aws-skills-install"
    for target in bootstrap_targets():
        if not target.is_dir():
            problems.append(f"bootstrap copy missing: {target.relative_to(ROOT)}")
        elif setup_toolkit.digest_tree(target) != setup_toolkit.digest_tree(source):
            problems.append(f"bootstrap copy differs from skills/aws-skills-install: {target.relative_to(ROOT)}")
    for target, harness in entry_targets():
        if not (target / "SKILL.md").is_file() or (target / "SKILL.md").read_text(encoding="utf-8") != rendered_entry(harness):
            problems.append(f"rendered entry skill is stale: {target.relative_to(ROOT)}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--digest", metavar="DIR", help="print the install digest of a skill directory and exit")
    args = parser.parse_args()
    if args.digest:
        print(setup_toolkit.digest_tree(Path(args.digest)))
        return 0
    if args.check:
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        print(json.dumps({"status": "fail" if problems else "pass", "problems": len(problems)}))
        return 1 if problems else 0
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(render(build()), encoding="utf-8")
    sync_bootstrap()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    print(json.dumps({"status": "written", "skills": len(manifest["skills"]),
                      "files": sum(len(s["files"]) for s in manifest["skills"]),
                      "bootstrap": [str(t.relative_to(ROOT)) for t in bootstrap_targets()] + [str(t.relative_to(ROOT)) for t, _ in entry_targets()]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
