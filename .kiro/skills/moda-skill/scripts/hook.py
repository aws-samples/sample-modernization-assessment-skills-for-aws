#!/usr/bin/env python3
"""Hooks for moda-skill: a read-only guard before shell tools and a stop gate at the end of a turn. Python stdlib only.

  hook.py guard   PreToolUse: reads the hook event JSON on stdin, finds every `aws ...` invocation in the shell
                  command and exits 2 (blocked, reason on stderr) unless each one is a read-only metadata call.
  hook.py stop    Stop: if the latest run under ./artifacts is incomplete, was touched recently and is not paused,
                  prints {"decision": "block", "reason": ...} so the client starts another turn.
  hook.py pause "reason" / resume   Mark the latest run as waiting for the person (the stop gate then stays quiet).

The same file serves Kiro CLI (.kiro/hooks/*.json), Claude Code (.claude/settings.json) and Codex (.codex/hooks.json);
`moda-skill config --hooks` wires it. Exit codes follow the shared convention: 0 allow, 2 block.
"""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

READ_PREFIXES = ("describe-", "list-", "get-", "batch-get-", "head-", "lookup-", "search-", "query-", "check-", "estimate-", "generate-", "preview-", "test-", "validate-", "simulate-", "select-")
# Data content and credential material: denied even though the verb reads. Matches docs/iam/assessment-policy.json.
DENY = {
    ("secretsmanager", "get-secret-value"), ("ssm", "get-parameter"), ("ssm", "get-parameters"), ("ssm", "get-parameters-by-path"),
    ("s3api", "get-object"), ("s3api", "select-object-content"), ("logs", "get-log-events"), ("logs", "get-query-results"),
    ("logs", "filter-log-events"), ("kms", "decrypt"), ("dynamodb", "get-item"), ("dynamodb", "batch-get-item"), ("dynamodb", "scan"),
    ("dynamodb", "query"), ("dynamodb", "execute-statement"), ("lambda", "get-function"), ("ecr", "get-login-password"),
    ("ecr", "get-authorization-token"), ("sts", "get-session-token"), ("sts", "get-federation-token"), ("codecommit", "get-blob"),
    ("eks", "get-token"), ("rds-data", "execute-statement"), ("redshift-data", "execute-statement"), ("athena", "get-query-results"),
    ("cognito-idp", "admin-get-user"), ("iam", "get-login-profile"),
}
# The one write the assessment may perform (optional host inspection), plus the local CLI configuration verbs.
ALLOW_EXACT = {("ssm", "send-command"), ("ssm", "start-session"), ("sso", "login"), ("sso", "logout"), ("sts", "assume-role"),
               ("sts", "get-caller-identity"), ("s3", "ls"), ("s3api", "list-buckets"), ("cloudtrail", "lookup-events")}
ALLOW_SERVICES = {"configure", "help", "--version"}
GLOBAL_WITH_VALUE = {"--profile", "--region", "--output", "--query", "--endpoint-url", "--cli-connect-timeout", "--cli-read-timeout",
                     "--ca-bundle", "--color", "--cli-binary-format"}
RECENT_SECONDS = 90 * 60
MAX_CONTINUES = 3
STAGES_FILE_HINT = "01-setup/access-check.json, scope.json, 02-toolkit/toolkit-check.json, 03-as-is(-enriched)/inventory.json, 04-review/findings.json + diagrams/as-is.aws.svg, 05-modernization/pathways.json, 06-assessment/assessment.html"


def read_event():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def command_text(event):
    tool_input = event.get("tool_input") or event.get("toolInput") or event.get("input") or {}
    if isinstance(tool_input, str):
        return tool_input
    for key in ("command", "cmd", "script"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
    return event.get("command") if isinstance(event.get("command"), str) else ""


def segments(command):
    """Split a shell command line on separators and strip wrappers like env assignments, `timeout 60`, `sudo -u x`."""
    text = re.sub(r"\$\([^)]*\)", " ", command)  # substitutions are evaluated separately below
    inner = re.findall(r"\$\(([^)]*)\)", command)
    parts = re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", text) + inner
    out = []
    for part in parts:
        try:
            tokens = shlex.split(part, posix=True)
        except ValueError:
            tokens = part.split()
        while tokens and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]) or tokens[0] in {"env", "exec", "nohup", "time", "command"}):
            tokens.pop(0)
        if tokens and tokens[0] == "timeout":
            tokens = tokens[2:] if len(tokens) > 2 else []
        if tokens:
            out.append(tokens)
    return out


def aws_call(tokens):
    """(service, operation) of an aws invocation, or None when the tokens are not an aws command."""
    if not tokens or os.path.basename(tokens[0]) not in {"aws", "aws.exe", "aws2"}:
        return None
    rest, i = [], 1
    while i < len(tokens):
        t = tokens[i]
        if t in GLOBAL_WITH_VALUE:
            i += 2
            continue
        if t.startswith("--"):
            i += 1
            continue
        rest.append(t)
        i += 1
    service = rest[0] if rest else ""
    operation = rest[1] if len(rest) > 1 else ""
    return service, operation


def judge(service, operation):
    """None when allowed, else the reason the call is blocked."""
    if service in ALLOW_SERVICES or (service, operation) in ALLOW_EXACT:
        return None
    if (service, operation) in DENY:
        return f"aws {service} {operation} reads data content or credentials; the assessment never does (see docs/iam/assessment-policy.json)"
    if service == "s3" and operation != "ls":
        return f"aws s3 {operation} moves object data; only `aws s3 ls` and s3api metadata calls are allowed"
    if service == "ssm" and operation.startswith("get-command"):
        return None
    if operation.startswith(READ_PREFIXES):
        return None
    if not operation or operation == "help" or operation == "wait":
        return None  # `aws <service> help`, `aws <service> wait <read-condition>`
    return f"aws {service} {operation} is not a read-only metadata call; the assessment runs describe/list/get only"


def guard(event):
    command = command_text(event)
    if not command or "aws" not in command:
        return 0
    blocked = []
    for tokens in segments(command):
        call = aws_call(tokens)
        if call is None:
            continue
        reason = judge(*call)
        if reason:
            blocked.append(reason)
    if blocked:
        sys.stderr.write("moda-skill read-only guard blocked this command:\n- " + "\n- ".join(dict.fromkeys(blocked)) +
                         "\nUse the equivalent describe/list/get call, or tell the person what you needed and why.\n")
        return 2
    return 0


def latest_run(project_root):
    root = Path(project_root) / "artifacts"
    runs = [r for w in root.iterdir() if w.is_dir() for r in w.iterdir() if r.is_dir()] if root.is_dir() else []
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def newest_mtime(run):
    newest = run.stat().st_mtime
    for path in run.rglob("*"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    return newest


def stop(event, project_root):
    if event.get("stop_hook_active"):
        return 0
    run = latest_run(project_root)
    if run is None or (run / ".moda-paused").exists():
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import moda  # noqa: E402
    info = moda.inspect(run)
    if info["complete"]:
        summary = run / "06-assessment/assessment-summary.json"
        try:
            status = json.loads(summary.read_text(encoding="utf-8")).get("status", "")
        except (OSError, ValueError):
            status = ""
        if not str(status).startswith("Draft"):
            return 0
        reason = f"The report for {info['workload']}/{info['run_id']} is still '{status}'. Fix what the status names (diagram coverage or missing diagrams), re-run assessment_report.py, then finish."
    else:
        if time.time() - newest_mtime(run) > RECENT_SECONDS:
            return 0
        stage = next(s for s in info["stages"] if s["state"] != "done")
        reason = (f"Run {info['workload']}/{info['run_id']} is not finished: stage {stage['stage']} is {stage['state']} "
                  f"(missing {', '.join(stage['missing'])}). Continue with that stage skill. If you stopped because the person must decide something, "
                  f"run `python3 <skills>/moda-skill/scripts/hook.py pause \"<what you need>\"` and ask them.")
    counter = run / ".moda-stop-continues"
    try:
        count = int(counter.read_text().strip() or 0)
    except (OSError, ValueError):
        count = 0
    if count >= MAX_CONTINUES:
        return 0
    counter.write_text(str(count + 1))
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"guard", "stop", "pause", "resume"}:
        print(__doc__)
        return 2
    project_root = os.environ.get("MODA_PROJECT_ROOT") or os.getcwd()
    if sys.argv[1] == "guard":
        return guard(read_event())
    if sys.argv[1] == "stop":
        return stop(read_event(), project_root)
    run = latest_run(project_root)
    if run is None:
        print("no run to mark")
        return 0
    if sys.argv[1] == "pause":
        (run / ".moda-paused").write_text(" ".join(sys.argv[2:]) or "waiting for the person", encoding="utf-8")
        print(f"paused {run.parent.name}/{run.name}")
    else:
        for name in (".moda-paused", ".moda-stop-continues"):
            (run / name).unlink(missing_ok=True)
        print(f"resumed {run.parent.name}/{run.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
