#!/usr/bin/env python3
"""Check the agent's own AWS commands before it runs them. Python stdlib only; makes no AWS call.

During an assessment run the read-only guard lets the agent run an AWS command of its own (one that is not inside a
stage helper) only when this script approved it first. The agent writes RUN_DIR/agent-commands.json:

  {"commands": [{"id": "c1", "purpose": "why this read is needed",
                 "command": "aws ec2 describe-vpcs --region us-east-1 --profile assessment"}]}

and runs `python3 plan_check.py --run-dir RUN_DIR`. Each entry must be one plain aws command (no pipes, redirections,
substitutions, variables or wrappers) that the guard's read-only rules allow, on the run's profile and in its regions,
without options that change the endpoint or TLS checks, print request details, or read local files. When every entry
passes, RUN_DIR/agent-commands.approved.json lists them and the guard then runs exactly those commands. Otherwise the
script prints each rejected entry with its reason: fix those entries and run it again. After three rejected rounds in a
row it stops, and the person decides.

Exit codes: 0 approved, 2 rejected (fix and rerun), 3 stop and ask the person, 1 unusable input.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("moda_plan_hook", HERE / "hook.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

MAX_COMMANDS = 50
MAX_ROUNDS = 3
ROUNDS_FILE = ".agent-commands-rounds"
FORBIDDEN_OPTIONS = {"--endpoint-url", "--no-verify-ssl", "--ca-bundle", "--cli-input-json", "--cli-input-yaml", "--debug",
                     "--generate-cli-skeleton", "--no-sign-request"}
OUTPUT_FORMATS = {"json", "text", "table", "yaml", "yaml-stream"}
GLOBAL_SERVICES = {"iam", "organizations", "route53", "cloudfront", "sts", "support", "ce", "health", "budgets",
                   "globalaccelerator", "shield", "account", "s3control", "route53domains"}
REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
SHELL_SYNTAX = re.compile(r"[$`\\]|\$\(|<\(|>\(")
REDIRECTIONS = {">", ">>", "<", "<<", "<<<", ">&", "&>", "&>>", ">|", "<>"}


def check_entry(entry, ctx):
    """None when the entry may be approved, else the reason it may not."""
    if not isinstance(entry, dict):
        return "each entry must be an object with id, purpose and command"
    command, purpose = entry.get("command"), entry.get("purpose")
    if not isinstance(command, str) or not command.strip():
        return "command must be a non-empty string"
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 300:
        return "purpose must say in at most 300 characters why the read is needed"
    if SHELL_SYNTAX.search(command):
        return "write the command with literal values: no variables, substitutions or escapes"
    words = hook.shell_words(command)
    if any(w in hook.SEPARATORS or w in REDIRECTIONS for w in words):
        return "one plain aws command per entry: no pipes, separators or redirections"
    tokens = [w for w in words if w]
    if not tokens or os.path.basename(tokens[0]) not in hook.AWS_BINARIES:
        return "the command must start with aws (no environment assignments or wrappers)"
    service, operation, arguments = hook.aws_call(tokens)
    if service in {"configure", "sso", "login", "help"} or not operation:
        return "only AWS API calls go in the list; profile and sign-in commands do not need approval"
    reason = hook.judge(service, operation, arguments)
    if reason:
        return reason
    form = hook.cli_canonical(tokens)
    reason = hook.scope_reason(form, ctx)
    if reason:
        return reason
    options = {k: v for k, v in form[2] if k}
    for key, value in form[2]:
        if key in FORBIDDEN_OPTIONS:
            return f"{key} is not allowed in an approved command"
        if value.startswith(("file://", "fileb://")):
            return f"{key} reads a local file; write the values into the command instead"
    if options.get("--output") and options["--output"] not in OUTPUT_FORMATS:
        return f"--output must be one of {', '.join(sorted(OUTPUT_FORMATS))}"
    region = options.get("--region")
    if not region or not REGION.match(region):
        return "add --region with one of the run's regions"
    allowed = set(ctx["regions"]) | ({"us-east-1"} if service in GLOBAL_SERVICES else set())
    if ctx["regions"] and region not in allowed:
        return f"region {region} is outside the run's scope ({', '.join(ctx['regions'])})"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    plan_file, approved_file, rounds_file = run / hook.PLAN_FILE, run / hook.APPROVED_FILE, run / ROUNDS_FILE
    try:
        scope = json.loads((run / "scope.json").read_text(encoding="utf-8"))
        raw = plan_file.read_bytes()
        plan = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "unusable", "error": f"{type(exc).__name__}: needs scope.json and {hook.PLAN_FILE} in the run directory"}))
        return 1
    auth = scope.get("auth") or {}
    ctx = {"run": run, "root": run, "mode": auth.get("mode"), "profile": auth.get("profile"), "regions": scope.get("regions") or []}
    commands = plan.get("commands") if isinstance(plan, dict) else None
    if not isinstance(commands, list) or not commands or len(commands) > MAX_COMMANDS:
        print(json.dumps({"status": "unusable", "error": f"commands must be a list of 1 to {MAX_COMMANDS} entries"}))
        return 1
    rejected, seen = [], set()
    for k, entry in enumerate(commands):
        entry_id = str(entry.get("id") if isinstance(entry, dict) else "") or f"#{k + 1}"
        reason = "duplicate id" if entry_id in seen else check_entry(entry, ctx)
        seen.add(entry_id)
        if reason:
            rejected.append({"id": entry_id, "command": entry.get("command") if isinstance(entry, dict) else None, "reason": reason})
    if rejected:
        approved_file.unlink(missing_ok=True)
        try:
            rounds = int(rounds_file.read_text().strip() or 0) + 1
        except (OSError, ValueError):
            rounds = 1
        rounds_file.write_text(str(rounds))
        stop = rounds >= MAX_ROUNDS
        print(json.dumps({"status": "stop_and_ask" if stop else "rejected", "round": rounds, "accepted": len(commands) - len(rejected),
                          "rejected": rejected,
                          "next": ("Three rounds were rejected. Do not run these commands; tell the person which reads you need and why."
                                   if stop else "Fix only the rejected entries, then run plan_check.py again.")}, ensure_ascii=False, indent=1))
        return 3 if stop else 2
    rounds_file.unlink(missing_ok=True)
    approved = {"schema_version": "1.0", "approved_at": datetime.now(timezone.utc).isoformat(), "validator": "plan_check.py",
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "commands": [{"id": str(e["id"]), "purpose": e["purpose"], "command": e["command"],
                              "form": hook.cli_canonical([w for w in hook.shell_words(e["command"]) if w])} for e in commands]}
    approved_file.write_text(json.dumps(approved, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "approved", "commands": len(commands), "approved_file": str(approved_file)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
