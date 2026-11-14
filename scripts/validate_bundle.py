#!/usr/bin/env python3
"""Validate this repository's portable skill metadata, resources and JSON assets."""
import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ("aws-skills-install", "aws-cli-setup", "aws-mcp-setup", "aws-as-is-analysis",
            "aws-architecture-review", "aws-modernization-pathway")


def validate(root=ROOT):
    errors = []
    for name in EXPECTED + ("moda-skill",):
        folder = root / "skills" / name
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
        if name == "moda-skill":  # validate the template too; it carries a placeholder line inside its frontmatter
            template = (folder / "SKILL.template.md").read_text(encoding="utf-8").replace("{{CLAUDE_FRONTMATTER}}", "")
            if not re.match(r"^---\nname: moda-skill\ndescription: \"[^\n]+\"\n---\n", template):
                errors.append("moda-skill: SKILL.template.md frontmatter must be name + double-quoted description")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not match:
            errors.append(f"{name}: missing frontmatter")
            continue
        header = match.group(1)
        found_name = re.search(r"^name: (.+)$", header, re.M)
        description = re.search(r"^description: (.+)$", header, re.M)
        if not found_name or found_name[1] != name:
            errors.append(f"{name}: name mismatch")
        if not description or not 1 <= len(description[1]) <= 1024:
            errors.append(f"{name}: invalid description")
        elif ": " in description[1] and not (description[1].startswith('"') and description[1].endswith('"')):
            errors.append(f"{name}: a description containing ': ' must be double-quoted (Kiro's frontmatter parser drops the skill otherwise)")
        metadata = (folder / "agents/openai.yaml").read_text(encoding="utf-8")
        if "$" + name not in metadata:
            errors.append(f"{name}: invocation metadata mismatch")
        short = re.search(r'short_description: "([^"]+)"', metadata)
        if not short or not 25 <= len(short[1]) <= 64:
            errors.append(f"{name}: short description must be 25..64 characters")
    for base in (root / "skills", root / "scripts", root / "tests"):
        for file in base.rglob("*"):
            if file.is_file() and file.suffix == ".json":
                try:
                    json.loads(file.read_text(encoding="utf-8"))
                except ValueError as exc:
                    errors.append(f"{file.relative_to(root)}: {exc}")
            elif file.is_file() and file.suffix == ".py":
                try:
                    ast.parse(file.read_text(encoding="utf-8"))
                except SyntaxError as exc:
                    errors.append(f"{file.relative_to(root)}: {exc}")
    for file in list((root / "skills").rglob("*.md")) + [p for p in (root / "README.md", root / "README.ko.md") if p.is_file()]:
        text = file.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            if "://" in target or target.startswith(("#", "$")):
                continue
            path = target.split("#", 1)[0]
            if path and not (file.parent / path).exists():
                errors.append(f"{file.relative_to(root)}: broken link {target}")
    return errors


if __name__ == "__main__":
    failures = validate()
    for failure in failures:
        print(failure, file=sys.stderr)
    print(json.dumps({"skills": len(EXPECTED), "status": "fail" if failures else "pass",
                      "errors": len(failures)}))
    sys.exit(bool(failures))
