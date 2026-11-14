#!/usr/bin/env python3
"""Verify CLI v2 and caller identity without persisting credentials (stdlib only)."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def checked_arguments(arguments):
    """Guard for every CLI invocation: list form only (no shell), the program name is a literal at the call site
    (`["aws", *checked_arguments(...)]`, resolved through PATH exactly as shutil.which resolved it), and every argument
    is a string without NUL or newline. Arguments come from our own tables, resource identifiers read from AWS
    responses, or validated CLI options."""
    if not isinstance(arguments, (list, tuple)) or not arguments:
        raise ValueError("command arguments must be a non-empty list")
    for item in arguments:
        if not isinstance(item, str) or "\x00" in item or "\n" in item or "\r" in item:
            raise ValueError("command arguments must be strings without control characters")
    return list(arguments)


def aws_environment(mode):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AWS_ENDPOINT_URL"):
            env.pop(key, None)
    env.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off",
               AWS_EC2_METADATA_DISABLED="true",
               AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true",
               AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="2")
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_DEFAULT_PROFILE", None)
    if mode == "profile":
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
            env.pop(key, None)
    else:
        if not env.get("AWS_ACCESS_KEY_ID") or not env.get("AWS_SECRET_ACCESS_KEY"):
            raise ValueError("environment_credentials_missing")
        if env["AWS_ACCESS_KEY_ID"].startswith("ASIA") and not env.get("AWS_SESSION_TOKEN"):
            raise ValueError("session_token_missing")
        env["AWS_CONFIG_FILE"] = os.devnull
        env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
        for key in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
                    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
            env.pop(key, None)
    return env


def error_code(stderr):
    match = re.search(r"\(([A-Za-z0-9_.-]+)\)", stderr or "")
    if match:
        return match.group(1)
    text = (stderr or "").lower()
    if "sso" in text and ("expired" in text or "invalid" in text):
        return "sso_session_expired"
    if "could not be found" in text and "profile" in text:
        return "profile_not_found"
    if "credentials" in text:
        return "credentials_unavailable"
    return "cli_or_network_error"


# Representative changes, data reads and credential operations. If the principal may perform any of them, the profile
# can change or read the account beyond metadata, and the assessment asks the person before it goes on.
WRITE_PROBES = (
    "ec2:RunInstances", "ec2:TerminateInstances", "ec2:AuthorizeSecurityGroupIngress", "ec2:CreateTags",
    "iam:CreateUser", "iam:CreateAccessKey", "iam:AttachRolePolicy", "iam:PutRolePolicy",
    "s3:PutObject", "s3:DeleteBucket", "s3:PutBucketPolicy", "s3:GetObject",
    "rds:DeleteDBInstance", "rds:ModifyDBInstance", "lambda:UpdateFunctionCode", "cloudformation:DeleteStack",
    "dynamodb:DeleteTable", "kms:ScheduleKeyDeletion", "secretsmanager:GetSecretValue", "organizations:LeaveOrganization",
)
# The one write the assessment may make (optional host inspection); reported, but not counted as write capability.
EXPECTED_WRITES = ("ssm:SendCommand",)


def principal_type(arn):
    if re.match(r"arn:aws[a-z-]*:iam::\d{12}:root$", arn):
        return "root"
    if ":assumed-role/" in arn:
        return "assumed_role"
    if re.match(r"arn:aws[a-z-]*:iam::\d{12}:user/", arn):
        return "iam_user"
    if ":federated-user/" in arn:
        return "federated_user"
    return "unknown"


def aws_json(argv, env, args):
    if args.profile:
        argv = [*argv, "--profile", args.profile]
    response = subprocess.run(["aws", *checked_arguments([*argv, "--output", "json", "--no-cli-pager"])],  # nosec B603 B607 - see checked_arguments
                              capture_output=True, text=True, timeout=45, env=env)
    if response.returncode:
        return None, error_code(response.stderr)
    return json.loads(response.stdout or "{}"), None


def write_capability(arn, env, args):
    """What the verified principal may do beyond metadata reads, judged by IAM's policy simulator (read-only calls).

    `no` only when every probe is denied; an error or an unsupported principal is `unknown`, which the setup stage
    treats like `yes`. The simulator evaluates identity policies and permission boundaries; organization SCPs and
    resource policies can still deny more, never less, so `yes` may overstate and `no` stays trustworthy."""
    kind = principal_type(arn)
    out = {"principal_type": kind, "long_term_credentials": kind == "iam_user"}
    if kind == "root":
        return dict(out, write_capability={"status": "yes", "reason": "root_user", "allowed": []})
    if kind == "iam_user":
        source = arn
    elif kind == "assumed_role":
        role_name = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
        role, failure = aws_json(["iam", "get-role", "--role-name", role_name, "--region", args.region], env, args)
        if failure or not role.get("Role", {}).get("Arn"):
            return dict(out, write_capability={"status": "unknown", "reason": failure or "role_not_readable", "allowed": []})
        source = role["Role"]["Arn"]
    else:
        return dict(out, write_capability={"status": "unknown", "reason": f"unsupported_principal_{kind}", "allowed": []})
    result, failure = aws_json(["iam", "simulate-principal-policy", "--policy-source-arn", source, "--region", args.region,
                                "--action-names", *WRITE_PROBES, *EXPECTED_WRITES], env, args)
    if failure:
        return dict(out, write_capability={"status": "unknown", "reason": failure, "allowed": []})
    allowed = sorted({r.get("EvalActionName") for r in result.get("EvaluationResults", []) if r.get("EvalDecision") == "allowed"})
    risky = [a for a in allowed if a not in EXPECTED_WRITES]
    return dict(out, write_capability={"status": "yes" if risky else "no", "method": "iam simulate-principal-policy",
                                       "probes": len(WRITE_PROBES), "allowed": risky,
                                       "expected_writes_allowed": [a for a in allowed if a in EXPECTED_WRITES]})


def verify(args):
    result = {
        "schema_version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "auth_mode": "profile" if args.profile else "environment",
        "profile": args.profile, "region": args.region,
        "expected_account": args.expected_account,
        "status": "failed",
    }
    executable = shutil.which("aws")
    if not executable:
        return dict(result, error_code="aws_cli_missing")
    try:
        version = subprocess.run(["aws", "--version"], capture_output=True, text=True, timeout=15)  # nosec B603 B607 - static argv
        match = re.search(r"aws-cli/(2\.[\d.]+)", version.stdout + version.stderr)
        if version.returncode or not match:
            return dict(result, error_code="aws_cli_v2_required")
        result["cli_version"] = match.group(1)
        result["cli_path"] = executable
        env = aws_environment(result["auth_mode"])
        argv = ["sts", "get-caller-identity", "--region", args.region, "--output", "json", "--no-cli-pager"]
        if args.profile:
            argv += ["--profile", args.profile]
        response = subprocess.run(["aws", *checked_arguments(argv)], capture_output=True, text=True,  # nosec B603 B607 - see checked_arguments
                                  timeout=45, env=env)
        if response.returncode:
            return dict(result, error_code=error_code(response.stderr))
        identity = json.loads(response.stdout)
        account = identity.get("Account", "")
        if not re.fullmatch(r"\d{12}", account):
            return dict(result, error_code="invalid_sts_response")
        result.update(account_id=account, principal_arn=identity.get("Arn"))
        if args.expected_account and account != args.expected_account:
            return dict(result, status="account_mismatch")
        try:
            result.update(write_capability(identity.get("Arn") or "", env, args))
        except (ValueError, subprocess.TimeoutExpired, OSError):
            result.update(principal_type=principal_type(identity.get("Arn") or ""),
                          write_capability={"status": "unknown", "reason": "check_failed", "allowed": []})
        return dict(result, status="verified")
    except ValueError as exc:
        # Never include AWS stdout, stderr, env, or exception repr in the report.
        allowed = {"environment_credentials_missing", "session_token_missing"}
        return dict(result, error_code=str(exc) if str(exc) in allowed else "invalid_json")
    except subprocess.TimeoutExpired:
        return dict(result, error_code="timeout")
    except OSError:
        return dict(result, error_code="execution_error")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--profile")
    auth.add_argument("--environment", action="store_true")
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.expected_account and not re.fullmatch(r"\d{12}", args.expected_account):
        parser.error("--expected-account must be 12 digits")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", args.region):
        parser.error("invalid region")
    if args.output.exists():
        parser.error("output exists; use a new run path")
    result = verify(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    try:
        args.output.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({"status": result["status"], "output": str(args.output),
                      "error_code": result.get("error_code"),
                      "principal_type": result.get("principal_type"),
                      "write_capability": (result.get("write_capability") or {}).get("status")}, ensure_ascii=False))
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
