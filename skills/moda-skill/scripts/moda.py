#!/usr/bin/env python3
"""Status of assessment runs under the current project, and the installed version. Python stdlib only.

  moda.py status [--project-root DIR] [--run-dir DIR] [--json]
  moda.py version
  moda.py preflight [--skills-dir DIR] [--client kiro|claude|codex] [--json]   # exit 4 when stage skills are missing

A run is `artifacts/<workload>/<run id>/`. Stage completion is read from the artifacts each stage writes, so
the entry skill never keeps state of its own.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

STAGES = (
    ("1 aws-cli-setup", "account verified and scope confirmed", ("01-setup/access-check.json", "scope.json")),
    ("2 aws-mcp-setup", "toolkit checked, run metadata started", ("02-toolkit/toolkit-check.json",)),
    ("3 aws-as-is-analysis", "inventory and workload evidence collected", ("03-as-is/inventory.json", "03-as-is-enriched/inventory.json")),
    ("4 aws-architecture-review", "findings, grades and current-state diagram", ("04-review/findings.json", "04-review/diagrams/as-is.aws.svg")),
    ("5 aws-modernization-pathway", "Specialist SA Agent reviews, directions and unified report", ("05-modernization/pathways.json", "06-assessment/assessment.html")),
)


MODELS = json.loads((Path(__file__).resolve().parents[1] / "assets/models.json").read_text(encoding="utf-8")) if (Path(__file__).resolve().parents[1] / "assets/models.json").is_file() else {"recommended": [], "tiers": {}}


def model_tier(model_id):
    """Classify a model id by family: frontier, reduced, unsuitable or unknown."""
    text = (model_id or "").lower()
    for tier, spec in MODELS.get("tiers", {}).items():
        if any(m in text for m in spec["match"]):
            return tier
    return "unknown"


def kiro_session_model(project_root):
    """Model of the most recent Kiro CLI session whose workspace covers project_root, from ~/.kiro/sessions."""
    root = Path.home() / ".kiro/sessions"
    project = str(Path(project_root).resolve())
    best = None
    for meta in root.glob("*/sess_*/session.json") if root.is_dir() else []:
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        paths = [str(Path(p).resolve()) for p in (info.get("workspacePaths") or []) if isinstance(p, str)]
        if any(project == p or project.startswith(p.rstrip("/") + "/") for p in paths):
            key = str(info.get("lastModifiedAt", ""))
            if best is None or key > best[0]:
                best = (key, info.get("modelId"), info.get("effortLevel"))
    return (best[1], best[2]) if best else (None, None)


def model_check(model_id, effort, client):
    tier = model_tier(model_id)
    spec = MODELS.get("tiers", {}).get(tier, {})
    recommended = ", ".join(MODELS.get("recommended", [])) or "the newest frontier model your client offers"
    result = {"client": client, "model": model_id, "effort": effort, "tier": tier, "recommended": MODELS.get("recommended", []),
              "effect": spec.get("effect"), "ok": tier == "frontier"}
    if model_id is None:
        result["message"] = f"Model not detected. Run the assessment with {recommended}; see /moda-skill help."
    elif tier == "frontier":
        result["message"] = f"{model_id}: recommended tier."
    elif tier == "unknown":
        result["message"] = f"{model_id} is not in the model guidance; {recommended} is the tested configuration."
    else:
        result["message"] = (f"{model_id} is a {tier}-tier model for this assessment: {spec.get('effect')}. "
                             f"Recommended: {recommended}. Switch with /model (Kiro, Claude Code) or -m (Codex) and start again, or continue knowing the report will be weaker.")
    if effort and effort.lower() not in {"high", "xhigh", "max"}:
        result["message"] += f" Effort is '{effort}'; set it to high or xhigh for the analysis stages."
        result["ok"] = False
    return result


STAGE_SKILLS = ("aws-cli-setup", "aws-mcp-setup", "aws-as-is-analysis", "aws-architecture-review", "aws-modernization-pathway")


def preflight(skills_dir=None, client="kiro"):
    """Which of the five stage skills are missing next to this entry skill."""
    base = Path(skills_dir) if skills_dir else Path(__file__).resolve().parents[2]
    missing = [n for n in STAGE_SKILLS if not (base / n / "SKILL.md").is_file()]
    fix = f"moda-skill config --harness {client}"
    message = ("ok: all five stage skills are installed in " + str(base)) if not missing else (
        "missing stage skills: " + ", ".join(missing) + f". This folder only has the bootstrap skills. Run `{fix}` in a terminal "
        "(add --update if it refuses), restart the client, then run start again.")
    return {"ok": not missing, "skills_dir": str(base), "missing": missing, "fix": fix, "message": message}


def version():
    here = Path(__file__).resolve()
    for parent in (here.parents[2], here.parents[3] if len(here.parents) > 3 else None):
        if parent and (parent / "VERSION").is_file():
            return (parent / "VERSION").read_text(encoding="utf-8").strip()
    stamp = here.parents[1] / ".moda-install.json"
    if stamp.is_file():
        return json.loads(stamp.read_text(encoding="utf-8")).get("version", "unknown")
    return "unknown"


def runs(project_root):
    root = Path(project_root) / "artifacts"
    found = []
    if root.is_dir():
        for workload in sorted(p for p in root.iterdir() if p.is_dir()):
            for run in sorted(p for p in workload.iterdir() if p.is_dir()):
                found.append(run)
    return sorted(found, key=lambda p: p.stat().st_mtime)


def inspect(run):
    stages = []
    next_stage = None
    for name, meaning, files in STAGES:
        present = [f for f in files if (run / f).is_file()]
        state = "done" if len(present) == len(files) else ("partial" if present else "pending")
        if state != "done" and next_stage is None:
            next_stage = name
        stages.append({"stage": name, "meaning": meaning, "state": state, "present": present,
                       "missing": [f for f in files if f not in present]})
    report = run / "06-assessment/assessment-summary.json"
    status = None
    if report.is_file():
        try:
            status = json.loads(report.read_text(encoding="utf-8")).get("status")
        except ValueError:
            status = "unreadable"
    localized = sorted(str(d.relative_to(run) / "assessment.html") for d in run.glob("06-assessment-*") if (d / "assessment.html").is_file())
    language = "en"
    if (run / "scope.json").is_file():
        try:
            language = json.loads((run / "scope.json").read_text(encoding="utf-8")).get("output_language") or "en"
        except ValueError:
            pass
    folder = run / "05-modernization/specialists"
    specialists = {d.name: ("advice written" if (d / "advice.json").is_file() else "brief only") for d in sorted(folder.iterdir()) if d.is_dir()} if folder.is_dir() else {}
    deliverable = "06-assessment/assessment.html" if language.split("-")[0] == "en" else f"06-assessment-{language}/assessment.html"
    return {"run_dir": str(run), "workload": run.parent.name, "run_id": run.name, "localized_reports": localized, "specialists": specialists,
            "language": language, "deliverable": deliverable if (run / deliverable).is_file() else None,
            "modified": datetime.fromtimestamp(run.stat().st_mtime, tz=timezone.utc).isoformat(timespec="minutes"),
            "stages": stages, "next_stage": next_stage, "complete": next_stage is None, "report_status": status}


def table(info):
    lines = [f"Run {info['workload']}/{info['run_id']}  (modified {info['modified']})", ""]
    for s in info["stages"]:
        mark = {"done": "done   ", "partial": "partial", "pending": "pending"}[s["state"]]
        lines.append(f"  {mark}  {s['stage']:28} {s['meaning']}")
    if info.get("specialists"):
        done = sum(1 for v in info["specialists"].values() if v == "advice written")
        lines.append(f"           specialist reviews {done}/{len(info['specialists'])}: " + ", ".join(f"{k} ({v})" for k, v in info["specialists"].items()))
    if info["complete"]:
        if info.get("language", "en").split("-")[0] != "en" and not info.get("deliverable"):
            lines += ["", f"English draft done ({info['report_status'] or 'status unknown'}); the {info['language']} report is not translated yet  →  resume to translate it"]
        else:
            lines += ["", f"Complete. Report ({info.get('language', 'en')}): {info['run_dir']}/{info.get('deliverable') or '06-assessment/assessment.html'} ({info['report_status'] or 'status unknown'})"]
    else:
        lines += ["", f"Next: {info['next_stage']}  →  resume to continue"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status")
    st.add_argument("--project-root", type=Path, default=Path.cwd())
    st.add_argument("--run-dir", type=Path)
    st.add_argument("--json", action="store_true")
    sub.add_parser("version")
    pf = sub.add_parser("preflight", help="are the five stage skills installed next to this skill?")
    pf.add_argument("--skills-dir", type=Path)
    pf.add_argument("--client", default="kiro")
    pf.add_argument("--json", action="store_true")
    mc = sub.add_parser("model-check", help="is the session model suitable for the assessment?")
    mc.add_argument("--model", help="model id; default: the latest Kiro CLI session for this project")
    mc.add_argument("--effort")
    mc.add_argument("--client", default="kiro-cli")
    mc.add_argument("--project-root", type=Path, default=Path.cwd())
    mc.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "model-check":
        model, effort = (args.model, args.effort) if args.model else kiro_session_model(args.project_root)
        result = model_check(model, args.effort or effort, args.client)
        print(json.dumps(result, ensure_ascii=False) if args.json else result["message"])
        return 0 if result["ok"] else 3
    if args.command == "preflight":
        result = preflight(args.skills_dir, args.client)
        print(json.dumps(result, ensure_ascii=False) if args.json else result["message"])
        return 0 if result["ok"] else 4
    if args.command == "version":
        print(f"moda-skill {version()}")
        return 0
    found = [args.run_dir] if args.run_dir else runs(args.project_root)
    if not found:
        message = {"runs": 0, "latest": None, "hint": "no run yet; `start` creates artifacts/<workload>/<run id>/"}
        print(json.dumps(message) if args.json else message["hint"])
        return 0
    latest = inspect(found[-1])
    if args.json:
        print(json.dumps({"runs": len(found), "latest": latest, "others": [str(r) for r in found[:-1]]}, ensure_ascii=False, indent=2))
    else:
        print(table(latest))
        if len(found) > 1:
            print(f"\n{len(found) - 1} earlier run(s): " + ", ".join(f"{r.parent.name}/{r.name}" for r in found[:-1][-3:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
