#!/usr/bin/env python3
"""Hooks for moda-skill: a read-only guard before shell tools and a stop gate at the end of a turn. Python stdlib only.

  hook.py guard   PreToolUse: reads the hook event JSON on stdin, finds every `aws ...` invocation in the shell
                  command (or the service and operation of Kiro CLI's `use_aws` tool) and exits 2 (blocked, reason
                  on stderr) unless each one is a read-only metadata call. A `use_aws` call without them is blocked.
  hook.py stop    Stop: if the latest run under ./artifacts is incomplete, was touched recently and is not paused,
                  prints {"decision": "block", "reason": ...} so the client starts another turn.
  hook.py pause "reason" / resume   Mark the latest run as waiting for the person (the stop gate then stays quiet).
  --root DIR      (guard, stop) the project folder whose run is enforced; `moda-skill config --hooks` writes it, so a
                  `cd` into a subfolder does not change which run the guard checks.

The same file serves Kiro CLI (.kiro/hooks/*.json), Claude Code (.claude/settings.json) and Codex (.codex/hooks.json);
`moda-skill config --hooks` wires it. Exit codes follow the shared convention: 0 allow, 2 block.
"""
import hashlib
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
    return strip_wrappers_with_env(tokens)[0]


def strip_wrappers_with_env(tokens):
    """strip_wrappers, plus the `NAME=value` assignments it dropped (they set the command's environment)."""
    assignments, changed = [], True
    while tokens and changed:
        changed = False
        while tokens and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]) or tokens[0] in LAUNCHERS):
            if "=" in tokens[0] and tokens[0] not in LAUNCHERS:
                assignments.append(tokens[0])
            tokens, changed = tokens[1:], True
        if tokens and tokens[0] == "timeout":
            tokens, changed = tokens[2:] if len(tokens) > 2 else [], True
        if tokens and tokens[0] == "sudo":
            tokens, changed = tokens[1:], True
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[2:] if tokens[0] in {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"} else tokens[1:]
    if tokens and tokens[0] in {"export", "declare", "typeset", "readonly", "set", "setenv"}:
        assignments += [t for t in tokens[1:] if "=" in t]
    return tokens, assignments


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
    return [tokens for tokens, _ in segments_with_env(command, depth)]


def segments_with_env(command, depth=0):
    """segments, each with the environment assignments in front of it (`AWS_PROFILE=x aws ...`, `export X=y`)."""
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
        tokens, assignments = strip_wrappers_with_env(tokens)
        if not tokens:
            if assignments:
                out.append(([], assignments))
            continue
        program = os.path.basename(tokens[0])
        inline = next((i for i, t in enumerate(tokens[1:], 1) if re.fullmatch(r"-[a-z]*c", t)), None) if program in SHELLS else None
        if depth < 5 and inline is not None and inline + 1 < len(tokens):
            out += segments_with_env(tokens[inline + 1], depth + 1)
        elif depth < 5 and program == "eval":
            out += segments_with_env(" ".join(tokens[1:]), depth + 1)
        else:
            out.append((tokens, assignments))
    for nested in inner:
        out += segments_with_env(nested, depth + 1) if depth < 5 else []
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


BOOTSTRAP = {("sts", "get-caller-identity"), ("sso", "login"), ("sso", "logout"), ("login", ""), ("help", ""), ("", "")}
# Environment variables that change which credentials, profile files or endpoint the AWS CLI uses.
CREDENTIAL_ENV = re.compile(r"^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AWS_SECURITY_TOKEN|AWS_CONFIG_FILE|"
                            r"AWS_SHARED_CREDENTIALS_FILE|AWS_ROLE_ARN|AWS_ROLE_SESSION_NAME|AWS_WEB_IDENTITY_TOKEN_FILE|"
                            r"AWS_CONTAINER_CREDENTIALS_[A-Z_]+|AWS_CONTAINER_AUTHORIZATION_TOKEN[A-Z_]*|AWS_ENDPOINT_URL[A-Z_]*|"
                            r"AWS_CA_BUNDLE|AWS_LOGIN_CACHE_DIRECTORY)=")
PROFILE_ENV = re.compile(r"^(AWS_PROFILE|AWS_DEFAULT_PROFILE)=(.*)$")
# Profile settings that decide which identity a profile resolves to; frozen once the run's profile is verified.
IDENTITY_KEYS = re.compile(r"(?i)(^|\.)(role_arn|source_profile|credential_source|credential_process|mfa_serial|"
                           r"web_identity_token_file|role_session_name|external_id|sso_[a-z_]+|endpoint_url|ca_bundle)$")
FILE_PATH_KEYS = ("path", "file_path", "filePath", "target_file", "notebook_path", "destination", "new_path", "old_path")
WRITE_PROGRAMS = {"cp", "mv", "rm", "tee", "truncate", "ln", "install", "chmod", "chown", "touch", "dd", "rsync", "unlink",
                  "rmdir", "mkdir", "shred", "patch", "ed"}
PLAN_FILE = "agent-commands.json"
APPROVED_FILE = "agent-commands.approved.json"


def run_context(project_root):
    """The assessment run the guard enforces, once stage 1 has written its scope: the latest run under ./artifacts
    with scope.json. None outside an assessment project, so the guard then only applies its read-only rules."""
    run = latest_run(project_root) if project_root else None
    if run is None or not (run / "scope.json").is_file():
        return None
    try:
        scope = json.loads((run / "scope.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        scope = {}
    auth = scope.get("auth") or {}
    return {"run": run, "root": Path(project_root).resolve(), "mode": auth.get("mode"), "profile": auth.get("profile"),
            "regions": scope.get("regions") or []}


def protected_paths(ctx):
    """Files the agent must not change while a run is active: AWS credentials and config, the installed skills and
    hook settings, and the run files that record what the person and the checks decided."""
    home, root, run = Path.home(), ctx["root"], ctx["run"]
    roots = [home / ".aws", home / ".kiro/skills", home / ".claude/skills", home / ".agents/skills", home / ".kiro/hooks",
             home / ".codex/hooks.json", home / ".claude/settings.json", home / ".local/share/moda-skill",
             root / ".kiro/skills", root / ".claude/skills", root / ".agents/skills", root / ".kiro/hooks",
             root / ".claude/settings.json", root / ".claude/settings.local.json", root / ".codex/hooks.json"]
    files = [run / "01-setup/access-check.json", run / "scope.json", run / APPROVED_FILE]
    return [p.resolve() for p in roots], [p.resolve() for p in files]


def expand(path, root):
    text = os.path.expandvars(os.path.expanduser(str(path).strip().strip("'\"")))
    candidate = Path(text)
    return (candidate if candidate.is_absolute() else Path(root) / candidate).resolve()


def protected_reason(path, ctx):
    roots, files = protected_paths(ctx)
    target = expand(path, ctx["root"])
    for item in files:
        if target == item:
            return f"{item.name} records the run's verified access and decisions; only the stage helpers write it"
    for item in roots:
        if target == item or item in target.parents:
            return f"{item} holds credentials, skills or hook settings; the agent does not change it during an assessment"
    return None


def file_tool_paths(event):
    """Paths a file-editing tool is about to write (Kiro write, Claude Code Edit/Write, Codex apply_patch)."""
    tool_input = event.get("tool_input") or event.get("toolInput") or event.get("input") or {}
    paths = []
    if isinstance(tool_input, dict):
        for key in FILE_PATH_KEYS:
            if isinstance(tool_input.get(key), str):
                paths.append(tool_input[key])
        for key in ("paths", "files"):
            if isinstance(tool_input.get(key), list):
                paths += [p if isinstance(p, str) else str(p.get("path", "")) for p in tool_input[key] if p]
        text = "\n".join(v for v in tool_input.values() if isinstance(v, str))
    else:
        text = str(tool_input)
    patch = r"(?m)^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$"  # Codex apply_patch
    paths += [p.strip() for p in re.findall(patch, text)]
    return [p for p in paths if p]


def shell_write_targets(tokens):
    """Paths a simple command writes: redirection targets, and the arguments of programs that change files."""
    targets = [tokens[k + 1] for k, t in enumerate(tokens[:-1]) if t in {">", ">>", ">|", "&>", "&>>", ">&"}]
    program = os.path.basename(tokens[0]) if tokens else ""
    edits_in_place = program in {"sed", "perl", "ruby"} and any(re.fullmatch(r"-[a-zA-Z]*i.*", t) for t in tokens[1:])
    if program in WRITE_PROGRAMS or edits_in_place:
        targets += [t for t in tokens[1:] if not t.startswith("-")]
    return targets


def canonical(service, operation, arguments):
    """A call in comparable form: service, operation and the sorted (option, value) pairs."""
    pairs, k, arguments = [], 0, list(arguments)
    while k < len(arguments):
        token = arguments[k]
        if token.startswith("--") and "=" in token:
            pairs.append(token.split("=", 1))
            k += 1
        elif token.startswith("--") and k + 1 < len(arguments) and not arguments[k + 1].startswith("--"):
            pairs.append([token, arguments[k + 1]])
            k += 2
        else:
            pairs.append([token, ""] if token.startswith("--") else ["", token])
            k += 1
    return [service, operation, sorted(pairs)]


def cli_canonical(tokens):
    """canonical() of the aws invocation in the tokens, with global options (`--profile`, `--region`) included."""
    start = next(i for i, t in enumerate(tokens) if os.path.basename(re.sub(r"^[$(`]+", "", t)) in AWS_BINARIES)
    positional, rest, i = [], [], start + 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            rest.append(t)
            if "=" not in t and i + 1 < len(tokens) and not tokens[i + 1].startswith("--") and (t in GLOBAL_WITH_VALUE or len(positional) >= 2):
                rest.append(tokens[i + 1])
                i += 1
        elif len(positional) < 2:
            positional.append(t)
        else:
            rest.append(t)
        i += 1
    positional += [""] * (2 - len(positional))
    return canonical(positional[0], positional[1], rest)


def tool_canonical(event):
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    service, operation, arguments = aws_tool_call(event)
    if tool_input.get("region"):
        arguments += ["--region", str(tool_input["region"])]
    if tool_input.get("profile_name") or tool_input.get("profile"):
        arguments += ["--profile", str(tool_input.get("profile_name") or tool_input.get("profile"))]
    return canonical(service, operation, arguments)


def call_profile(form):
    return next((v for k, v in form[2] if k == "--profile"), None)


def scope_reason(form, ctx, assignments=(), arguments=None):
    """Profile pinning: during a run every AWS call uses the profile stage 1 verified, and nothing swaps credentials."""
    for assignment in assignments:
        if CREDENTIAL_ENV.match(assignment):
            return f"{assignment.split('=', 1)[0]} would change the credentials or endpoint the AWS CLI uses during the assessment"
        match = PROFILE_ENV.match(assignment)
        if match and (ctx["mode"] != "profile" or match.group(2).strip("'\"") != ctx["profile"]):
            return f"{match.group(1)} must stay the run's profile ({ctx['profile'] or 'environment credentials'})"
    if form is None:
        return None
    service, operation = form[0], form[1]
    profile = call_profile(form)
    if service == "configure":
        if operation == "set":
            ordered = arguments if arguments is not None else [v for k, v in form[2] if k == ""]
            key = next((a for a in ordered if not a.startswith("-")), "")
            target = key.split(".")[1] if key.startswith("profile.") and key.count(".") >= 2 else (profile or "default")
            if IDENTITY_KEYS.search(key) and (ctx["mode"] != "profile" or target == ctx["profile"]):
                return f"aws configure set {key} would change which identity the run's verified profile uses"
        return None
    if (service, operation) in BOOTSTRAP or service in {"sso", "login", "help"}:
        return None
    if ctx["mode"] == "profile":
        if profile != ctx["profile"]:
            return (f"AWS calls in this run use --profile {ctx['profile']} (the profile stage 1 verified); "
                    f"this call uses {('--profile ' + profile) if profile else 'no --profile'}")
    elif profile:
        return "this run uses the environment credentials stage 1 verified; --profile would switch identities"
    return None


def plan_reason(form, ctx):
    """Approved-command mode: during a run the agent's own AWS calls must be ones plan_check.py approved."""
    service, operation = form[0], form[1]
    if service in {"configure", "sso", "login", "help"} or (service, operation) in BOOTSTRAP:
        return None
    hint = (f"write it to {ctx['run'] / PLAN_FILE}, run `python3 <skills>/moda-skill/scripts/plan_check.py --run-dir "
            f"{ctx['run']}`, then run exactly the approved command")
    approved_file, plan_file = ctx["run"] / APPROVED_FILE, ctx["run"] / PLAN_FILE
    try:
        approved = json.loads(approved_file.read_text(encoding="utf-8"))
        current = hashlib.sha256(plan_file.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return f"aws {service} {operation} is not in an approved command list; {hint}"
    if approved.get("source_sha256") != current:
        return f"{PLAN_FILE} changed after it was approved; {hint}"
    if not any(entry.get("form") == form for entry in approved.get("commands", [])):
        return f"aws {service} {operation} with these arguments is not in the approved list; {hint}"
    return None


def guard(event, project_root=None, pinned=False):
    """`pinned` is set when the hook command names the project (`--root`); the agent's current folder, which a `cd` can
    move into a subfolder, then does not decide which run is enforced."""
    ctx = run_context(project_root if pinned else (event.get("cwd") or project_root))
    blocked = []
    if ctx:
        for path in file_tool_paths(event):
            reason = protected_reason(path, ctx)
            if reason:
                blocked.append(reason)
        if blocked:
            sys.stderr.write("moda-skill read-only guard blocked this file change:\n- " + "\n- ".join(dict.fromkeys(blocked)) + "\n")
            return 2
    structured = aws_tool_call(event)
    if structured is not None:
        service, operation, arguments = structured
        reason = judge(service, operation, arguments) if service and operation else (
            "this AWS tool call names no service and operation, so it cannot be checked")
        if not reason and ctx:
            form = tool_canonical(event)
            reason = scope_reason(form, ctx) or plan_reason(form, ctx)
        if reason:
            sys.stderr.write("moda-skill read-only guard blocked this AWS tool call:\n- " + reason +
                             "\nUse the equivalent describe/list/get call, or tell the person what you needed and why.\n")
            return 2
        return 0
    command = command_text(event)
    if not command or not re.search(r"(?i)aws|boto|encodedcommand|-enc\b|\bpwsh\b|\bpowershell\b|\.kiro|\.claude|\.agents|\.codex|"
                                    + re.escape(APPROVED_FILE) + "|access-check|scope\\.json|moda-skill", command):
        return 0
    for tokens, assignments in segments_with_env(command):
        if ctx:
            reason = scope_reason(None, ctx, assignments)
            if reason:
                blocked.append(reason)
            for target in shell_write_targets(tokens):
                reason = protected_reason(target, ctx)
                if reason:
                    blocked.append(reason)
            if any(".aws/credentials" in t or re.search(r"\.aws/(sso|cli|login)/cache", t) for t in tokens):
                blocked.append("~/.aws/credentials and the credential caches hold secrets; the assessment never reads them")
        if not tokens:
            continue
        reason = inline_code_reason(tokens)
        if reason:
            blocked.append(reason)
            continue
        call = aws_call(tokens)
        if call is None:
            continue
        reason = judge(*call)
        if not reason and ctx:
            form = cli_canonical(tokens)
            reason = scope_reason(form, ctx, arguments=call[2]) or plan_reason(form, ctx)
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
    pinned = "--root" in sys.argv[2:-1]
    root_arg = sys.argv[sys.argv.index("--root") + 1] if pinned else None
    project_root = root_arg or os.environ.get("MODA_PROJECT_ROOT") or os.getcwd()
    if sys.argv[1] == "guard":
        return guard(read_event(), project_root, pinned=pinned)
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
