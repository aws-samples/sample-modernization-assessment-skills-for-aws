#!/usr/bin/env python3
"""Hooks for moda-skill: a read-only guard before shell tools and a stop gate at the end of a turn. Python stdlib only.

  hook.py guard   PreToolUse: reads the hook event JSON on stdin, finds every `aws ...` invocation in the shell
                  command (or the service and operation of Kiro CLI's `use_aws` tool) and exits 2 (blocked, reason
                  on stderr) unless each one is a read-only metadata call. A `use_aws` call without them is blocked.
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

# `generate-` and `test-` are left out on purpose: `kms generate-data-key` returns key material and
# `apigateway test-invoke-method` calls the backend. The helpers use neither.
READ_PREFIXES = ("describe-", "list-", "get-", "batch-get-", "head-", "lookup-", "search-", "query-", "check-", "estimate-", "preview-", "validate-", "simulate-", "select-")
# Data content and credential material: denied even though the verb reads. Mirrors DenyDataContentReads in
# docs/iam/assessment-policy.json and adds the calls that return secrets, passwords, tokens or credentials.
DENY = {
    ("secretsmanager", "get-secret-value"), ("secretsmanager", "batch-get-secret-value"), ("ssm", "get-parameter"),
    ("ssm", "get-parameters"), ("ssm", "get-parameters-by-path"), ("ssm", "get-parameter-history"),
    ("s3api", "get-object"), ("s3api", "select-object-content"), ("logs", "get-log-events"), ("logs", "get-query-results"),
    ("logs", "filter-log-events"), ("logs", "start-query"), ("kms", "decrypt"), ("dynamodb", "get-item"),
    ("dynamodb", "batch-get-item"), ("dynamodb", "scan"), ("dynamodb", "query"), ("dynamodb", "execute-statement"),
    ("kinesis", "get-records"), ("sqs", "receive-message"), ("lambda", "get-function"), ("lambda", "get-layer-version"),
    ("rds", "download-db-log-file-portion"), ("ecr", "get-login-password"), ("ecr", "get-authorization-token"),
    ("ecr-public", "get-login-password"), ("ecr-public", "get-authorization-token"), ("codeartifact", "get-authorization-token"),
    ("sts", "get-session-token"), ("sts", "get-federation-token"), ("sts", "assume-role"), ("sso", "get-role-credentials"),
    ("codecommit", "get-blob"), ("eks", "get-token"), ("ec2", "get-password-data"), ("ec2", "get-console-output"),
    ("ec2", "get-console-screenshot"), ("redshift", "get-cluster-credentials"), ("redshift", "get-cluster-credentials-with-iam"),
    ("rds-data", "execute-statement"), ("redshift-data", "execute-statement"), ("athena", "get-query-results"),
    ("cognito-idp", "admin-get-user"), ("cognito-identity", "get-credentials-for-identity"), ("iam", "get-login-profile"),
    ("configure", "export-credentials"),
}
# Any other call whose name says it returns secret material is denied as well.
SENSITIVE_OPERATION = re.compile(r"secret-value|password|credential|token|parameter-history|console-output|console-screenshot")
# The one write the assessment may perform (optional host inspection, AWS-RunShellScript only), plus local CLI verbs.
ALLOW_EXACT = {("ssm", "send-command"), ("sso", "login"), ("sso", "logout"),
               ("sts", "get-caller-identity"), ("s3", "ls"), ("s3api", "list-buckets"), ("cloudtrail", "lookup-events")}
HOST_INSPECTION_DOCUMENT = "AWS-RunShellScript"
# `aws configure` subcommands that only read or set profile options; `get`/`set` of a credential key is refused.
CONFIGURE_ALLOWED = {"", "list", "list-profiles", "get", "set", "sso", "sso-session", "help"}
CREDENTIAL_KEY = re.compile(r"(?i)(aws_access_key_id|aws_secret_access_key|aws_session_token|aws_security_token)$")
ALLOW_SERVICES = {"help", "--version"}
# Metadata calls whose names match SENSITIVE_OPERATION but return no secret.
SENSITIVE_METADATA = {("iam", "get-account-password-policy"), ("iam", "get-credential-report"),
                      ("iam", "list-service-specific-credentials")}
AWS_BINARIES = {"aws", "aws.exe", "aws2"}
LAUNCHERS = {"env", "exec", "nohup", "time", "command", "nice", "ionice", "stdbuf", "caffeinate", "builtin"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INLINE_FLAGS = {"python": {"-c"}, "python3": {"-c"}, "py": {"-c"}, "node": {"-e", "--eval", "-p", "--print"},
                "nodejs": {"-e", "--eval", "-p", "--print"}, "perl": {"-e", "-E"}, "ruby": {"-e"},
                "pwsh": {"-c", "-command"}, "powershell": {"-c", "-command"}}
INTERPRETERS = set(INLINE_FLAGS) | {"pwsh.exe", "powershell.exe", "python.exe", "py.exe"}
INLINE_AWS = re.compile(r"(?i)\b(boto3|botocore|aws-sdk|awspowershell)\b|@aws-sdk/|AWS\.Tools|\baws\s+[a-z0-9-]+\s+[a-z]+-[a-z-]+|['\"]aws(\.exe)?['\"]")
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
            argv = [str(v) for v in value]
            if len(argv) >= 3 and os.path.basename(argv[0]) in SHELLS and re.fullmatch(r"-[a-z]*c", argv[1]):
                return argv[2]  # ["bash", "-lc", "<script>"]: the script is what runs
            return shlex.join(argv)
    return event.get("command") if isinstance(event.get("command"), str) else ""


AWS_TOOLS = {"use_aws"}  # Kiro CLI's built-in AWS tool: it calls AWS without a shell command line


def cli_name(value):
    """`DescribeInstances` or `describe_instances` -> `describe-instances`; CLI names pass through unchanged."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", str(value).strip())
    return text.replace("_", "-").lower()


def aws_tool_call(event):
    """(service, operation, arguments) for a structured AWS tool call, ("", "", []) when its fields are missing,
    None for other tools. The tool's parameters become CLI-style `--name value` arguments."""
    tool_input = event.get("tool_input") or event.get("toolInput") or event.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    service = tool_input.get("service_name") or tool_input.get("serviceName") or tool_input.get("service")
    operation = tool_input.get("operation_name") or tool_input.get("operationName") or tool_input.get("operation")
    tool = str(event.get("tool_name") or event.get("toolName") or "")
    if tool not in AWS_TOOLS and not (isinstance(service, str) and isinstance(operation, str)):
        return None
    if not isinstance(service, str) or not isinstance(operation, str) or not service.strip() or not operation.strip():
        return "", "", []
    parameters = tool_input.get("parameters") if isinstance(tool_input.get("parameters"), dict) else {}
    arguments = [part for key, value in parameters.items() for part in ("--" + cli_name(key), str(value))]
    return cli_name(service), cli_name(operation), arguments


def split_substitutions(command):
    """The command with every `$(...)` and backtick substitution blanked out, and the inner commands (nested ones too)."""
    pattern = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
    text, inner = command, []
    match = pattern.search(text)
    while match:
        inner.append(match.group(1) if match.group(1) is not None else match.group(2))
        text = text[:match.start()] + " " + text[match.end():]
        match = pattern.search(text)
    return text, inner


def strip_wrappers(tokens):
    """Drop env assignments and launchers (`env`, `nohup`, `timeout 60`, `sudo -u x`, ...) in front of a command."""
    changed = True
    while tokens and changed:
        changed = False
        while tokens and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]) or tokens[0] in LAUNCHERS):
            tokens, changed = tokens[1:], True
        if tokens and tokens[0] == "timeout":
            tokens, changed = tokens[2:] if len(tokens) > 2 else [], True
        if tokens and tokens[0] == "sudo":
            tokens, changed = tokens[1:], True
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[2:] if tokens[0] in {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"} else tokens[1:]
    return tokens


SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", ";;", "|&"}


def shell_words(text):
    """Quote-aware split into words and control operators, so `;` or `&` inside quotes stays part of a word."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to a plain split so an aws call is still seen
        return re.split(r"\s+|(?<=[;&|()\n])|(?=[;&|()\n])", text)


def segments(command, depth=0):
    """Split a shell command line into simple commands: operators, substitutions, wrappers, `sh -c` and `eval`."""
    text, inner = split_substitutions(command)
    parts, current = [], []
    for word in shell_words(text):
        if word in SEPARATORS:
            parts.append(current)
            current = []
        elif word:
            current.append(word)
    parts.append(current)
    out = []
    for tokens in parts:
        tokens = strip_wrappers(tokens)
        if not tokens:
            continue
        program = os.path.basename(tokens[0])
        inline = next((i for i, t in enumerate(tokens[1:], 1) if re.fullmatch(r"-[a-z]*c", t)), None) if program in SHELLS else None
        if depth < 5 and inline is not None and inline + 1 < len(tokens):
            out += segments(tokens[inline + 1], depth + 1)
        elif depth < 5 and program == "eval":
            out += segments(" ".join(tokens[1:]), depth + 1)
        else:
            out.append(tokens)
    for nested in inner:
        out += segments(nested, depth + 1) if depth < 5 else []
    return out


def inline_code_reason(tokens):
    """Inline interpreter code that calls AWS (`python3 -c "import boto3..."`) cannot be checked, so it is refused."""
    program = os.path.basename(tokens[0]).lower()
    if program not in INTERPRETERS:
        return None
    for i, token in enumerate(tokens[1:], 1):
        flag = token.lower()
        if flag in {"-encodedcommand", "-ec", "-enc", "-e"} and program.startswith(("pwsh", "powershell")):
            return f"{program} {token} hides the command it runs; run the aws CLI directly"
        if flag in INLINE_FLAGS.get(program.split(".")[0], set()):
            code = " ".join(tokens[i + 1:i + 2])
            if INLINE_AWS.search(code):
                return f"inline {program} code that calls AWS cannot be checked; run the aws CLI or a helper script"
    return None


def aws_call(tokens):
    """(service, operation, arguments) of the aws invocation in the tokens, found anywhere so that `xargs aws ...`,
    `find -exec aws ...` and `watch aws ...` are checked too; None when no aws binary appears."""
    start = next((i for i, t in enumerate(tokens) if os.path.basename(re.sub(r"^[$(`]+", "", t)) in AWS_BINARIES), None)
    if start is None:
        return None
    positional, i = [], start + 1
    while i < len(tokens) and len(positional) < 2:
        t = tokens[i]
        if t in GLOBAL_WITH_VALUE:
            i += 2
        elif t.startswith("--"):
            i += 1
        else:
            positional.append(t)
            i += 1
    service = positional[0] if positional else ""
    operation = positional[1] if len(positional) > 1 else ""
    return service, operation, tokens[i:]


def option_value(arguments, name):
    for k, token in enumerate(arguments):
        if token == name and k + 1 < len(arguments):
            return arguments[k + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return None


def judge(service, operation, arguments=()):
    """None when allowed, else the reason the call is blocked."""
    arguments = list(arguments)
    if service in ALLOW_SERVICES:
        return None
    if service == "configure":
        key = next((a for a in arguments if not a.startswith("-")), "")
        if operation not in CONFIGURE_ALLOWED:
            return f"aws configure {operation} is not allowed; the assessment only lists profiles and sets profile options"
        if operation in {"get", "set"} and CREDENTIAL_KEY.search(key):
            return f"aws configure {operation} {key} reads or writes credential material; use a profile with a role"
        return None
    if (service, operation) in DENY:
        return f"aws {service} {operation} reads data content or credentials; the assessment never does (see docs/iam/assessment-policy.json)"
    if (service, operation) == ("ssm", "send-command"):
        document = option_value(arguments, "--document-name")
        if document != HOST_INSPECTION_DOCUMENT:
            return f"aws ssm send-command is allowed only with the {HOST_INSPECTION_DOCUMENT} document the host inspection uses"
        return None
    if (service, operation) in ALLOW_EXACT:
        return None
    if SENSITIVE_OPERATION.search(operation) and (service, operation) not in SENSITIVE_METADATA:
        return f"aws {service} {operation} returns secret material; the assessment never reads it"
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
    structured = aws_tool_call(event)
    if structured is not None:
        service, operation, arguments = structured
        reason = judge(service, operation, arguments) if service and operation else (
            "this AWS tool call names no service and operation, so it cannot be checked")
        if reason:
            sys.stderr.write("moda-skill read-only guard blocked this AWS tool call:\n- " + reason +
                             "\nUse the equivalent describe/list/get call, or tell the person what you needed and why.\n")
            return 2
        return 0
    command = command_text(event)
    if not command or not re.search(r"(?i)aws|boto|encodedcommand|-enc\b|\bpwsh\b|\bpowershell\b", command):
        return 0
    blocked = []
    for tokens in segments(command):
        reason = inline_code_reason(tokens)
        if reason:
            blocked.append(reason)
            continue
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
