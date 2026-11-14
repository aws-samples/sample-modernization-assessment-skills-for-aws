#!/usr/bin/env python3
"""Plan or run a fixed Linux SSH/SSM metadata probe for one explicitly named host."""
import argparse


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
import base64
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("moda_host_collect", HERE / "collect.py")
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)
PROBE = HERE / "_host_probe.py"
REGION = r"[a-z]{2}(?:-[a-z]+)+-\d+"
INSTANCE = r"(?:i-(?:[0-9a-f]{8}|[0-9a-f]{17})|mi-[0-9a-f]{17})"


def validate_args(args):
    if len(args.app_root) > 8:
        raise ValueError("at_most_eight_app_roots")
    for root in args.app_root:
        p = PurePosixPath(root)
        if (not p.is_absolute() or ".." in p.parts or root == "/" or len(root) > 400
                or any(ord(c) < 32 for c in root) or p.parts[1:2] in [("proc",), ("sys",), ("dev",)]):
            raise ValueError("invalid_app_root")
    if args.instance_id and not re.fullmatch(INSTANCE, args.instance_id):
        raise ValueError("invalid_instance_id")
    needs_aws = args.transport == "ssm" or bool(args.instance_id)
    if needs_aws:
        if not args.instance_id or not args.expected_account or not re.fullmatch(r"\d{12}", args.expected_account):
            raise ValueError("instance_and_expected_account_required")
        if not args.region or not re.fullmatch(REGION, args.region):
            raise ValueError("explicit_region_required")
        if not args.profile and not args.environment:
            raise ValueError("explicit_aws_auth_required")
    elif args.profile or args.environment or args.expected_account or args.region:
        raise ValueError("aws_binding_requires_instance_id")
    if args.transport == "ssh":
        if not args.host or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", args.host):
            raise ValueError("explicit_ssh_host_required")
        if not args.user or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", args.user):
            raise ValueError("explicit_ssh_user_required")
        if not 1 <= args.port <= 65535:
            raise ValueError("invalid_ssh_port")
        if args.instance_id and args.instance_id.startswith("mi-"):
            raise ValueError("ssh_ec2_binding_requires_ec2_instance")
    elif args.host or args.user or args.identity_file:
        raise ValueError("ssh_options_not_valid_for_ssm")


def payload(app_roots):
    config = base64.b64encode(json.dumps({"app_roots": app_roots}).encode()).decode()
    return "import base64,json\nCONFIG=json.loads(base64.b64decode(" + repr(config) + "))\n" + PROBE.read_text(encoding="utf-8")


def ssh_command(args):
    known = args.known_hosts.expanduser().resolve()
    if not known.is_file():
        raise ValueError("known_hosts_missing; verify_host_key_out_of_band")
    known_option = str(known).replace("\\", "\\\\").replace('"', '\\"')
    command = ["-F", os.devnull, "-T", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", f'UserKnownHostsFile="{known_option}"',
               "-o", f"GlobalKnownHostsFile={os.devnull}", "-o", "ForwardAgent=no",
               "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
               "-p", str(args.port), "-l", args.user]
    if args.identity_file:
        key = args.identity_file.expanduser().resolve()
        if not key.is_file():
            raise ValueError("identity_file_missing")
        command += ["-o", "IdentitiesOnly=yes", "-i", str(key)]
    return command + [args.host, "python3 -"]


def aws_json(args, env, service, operation, *arguments):
    command = [service, operation, *arguments, "--region", args.region,
               "--output", "json", "--no-cli-pager"]
    if args.profile:
        command += ["--profile", args.profile]
    response = subprocess.run(["aws", *checked_arguments(command)], capture_output=True, text=True, env=env, timeout=30)  # nosec B603 B607 - see checked_arguments
    if response.returncode:
        match = re.search(r"An error occurred \(([A-Za-z0-9_.-]+)\)", response.stderr)
        raise RuntimeError(match.group(1) if match else "aws_cli_failed")
    return json.loads(response.stdout)


def preflight_aws(args):
    env = collect.environment(args.profile)
    identity = aws_json(args, env, "sts", "get-caller-identity")
    if identity.get("Account") != args.expected_account:
        raise ValueError("account_mismatch")
    verified = {"account_id": identity["Account"], "principal_arn": identity.get("Arn"),
                "instance_id": args.instance_id, "region": args.region}
    if args.instance_id.startswith("i-"):
        instances = aws_json(args, env, "ec2", "describe-instances", "--instance-ids", args.instance_id)
        items = [i for r in instances.get("Reservations", []) for i in r.get("Instances", [])]
        if len(items) != 1 or items[0].get("InstanceId") != args.instance_id:
            raise ValueError("instance_binding_failed")
        item = items[0]
        if item.get("State", {}).get("Name") != "running":
            raise ValueError("instance_not_running")
        if item.get("Platform") == "windows":
            raise ValueError("windows_probe_not_supported")
        if args.transport == "ssh":
            addresses = {item.get(k) for k in ("PrivateIpAddress", "PublicIpAddress", "PrivateDnsName", "PublicDnsName")}
            if args.host not in addresses:
                raise ValueError("ssh_host_does_not_match_ec2_metadata")
            verified["binding"] = "ec2_endpoint_and_existing_ssh_host_key; not_guest_attestation"
    if args.transport == "ssm":
        info = aws_json(args, env, "ssm", "describe-instance-information",
                        "--filters", f"Key=InstanceIds,Values={args.instance_id}")
        nodes = info.get("InstanceInformationList", [])
        if len(nodes) != 1 or nodes[0].get("InstanceId") != args.instance_id:
            raise ValueError("ssm_node_not_found")
        if nodes[0].get("PingStatus") != "Online" or nodes[0].get("PlatformType") != "Linux":
            raise ValueError("ssm_requires_online_linux_node")
        verified["binding"] = "ssm_explicit_instance_id"
    return env, verified


def parse_probe(text):
    if len(text.encode()) > 21000:
        raise ValueError("probe_output_too_large")
    data = json.loads(text)
    expected = {"schema_version", "platform", "collector", "status", "processes", "services",
                "containers", "applications", "limitations", "kernel", "effective_uid", "os_release"}
    if (not isinstance(data, dict) or set(data) - expected or data.get("collector") != "moda-host-metadata-v1"
            or data.get("schema_version") != "1.0" or data.get("status") not in {"observed", "partial", "unsupported"}):
        raise ValueError("unexpected_probe_schema")
    for field in ("processes", "applications", "limitations"):
        if not isinstance(data.get(field), list):
            raise ValueError("unexpected_probe_fields")
    def records(items, allowed, limit):
        if not isinstance(items, list) or len(items) > limit or any(not isinstance(i, dict) or set(i) - allowed for i in items):
            raise ValueError("unexpected_probe_record")
    records(data["processes"], {"pid", "name", "exe", "cwd"}, 50)
    records(data["applications"], {"root", "status", "manifests", "running_pids_with_matching_cwd",
        "code_identity", "entry_file_names", "directory_listing_limited", "directory_listing_status",
        "git_branch", "git_commit", "git_status"}, 8)
    for app in data["applications"]:
        if "manifests" in app:
            records(app["manifests"], {"file", "sha256", "package", "version", "dependency_names",
                                     "parser_status", "module", "status"}, 6)
    for field, keys in [("services", {"unit", "active", "sub"}), ("containers", {"id", "image", "name", "state"})]:
        section = data.get(field)
        if not isinstance(section, dict) or set(section) != {"status", "items"}:
            raise ValueError("unexpected_probe_section")
        records(section["items"], keys, 30)
    return data


def execute(args, report):
    code = payload(args.app_root)
    report["probe_sha256"] = hashlib.sha256(code.encode()).hexdigest()
    if args.transport == "ssm" or args.instance_id:
        env, report["verified_target"] = preflight_aws(args)
    if args.transport == "ssh":
        command = ssh_command(args)
        report["execution_state"] = "ssh_attempted"
        response = subprocess.run(["ssh", *checked_arguments(command)], input=code, capture_output=True, text=True, timeout=45)  # nosec B603 B607 - see checked_arguments
        if response.returncode:
            # Never persist banners or stderr, which may contain private content.
            raise RuntimeError("ssh_or_remote_python_failed")
        report["execution_state"] = "ssh_exited_zero"
        return parse_probe(response.stdout)
    encoded = base64.b64encode(code.encode()).decode()
    command = "python3 -c \"import base64;exec(base64.b64decode('" + encoded + "'))\""
    params = {"commands": [command], "executionTimeout": ["30"]}
    report["execution_state"] = "send_attempted"
    sent = aws_json(args, env, "ssm", "send-command", "--instance-ids", args.instance_id,
                    "--document-name", "AWS-RunShellScript", "--parameters", json.dumps(params),
                    "--timeout-seconds", "30", "--comment", "Scoped host metadata assessment")
    command_id = sent.get("Command", {}).get("CommandId")
    if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", command_id):
        raise ValueError("invalid_ssm_command_id")
    report["command_id"] = command_id
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            invocation = aws_json(args, env, "ssm", "get-command-invocation",
                                  "--command-id", command_id, "--instance-id", args.instance_id)
        except RuntimeError as exc:
            if str(exc) != "InvocationDoesNotExist":
                raise
            time.sleep(2)
            continue
        status = invocation.get("Status")
        report["execution_state"] = status
        if status == "Success":
            if invocation.get("ResponseCode") != 0:
                raise ValueError("ssm_nonzero_response")
            return parse_probe(invocation.get("StandardOutputContent", ""))
        if status not in {"Pending", "InProgress", "Delayed"}:
            raise RuntimeError("ssm_execution_did_not_succeed")
        time.sleep(2)
    raise RuntimeError("ssm_poll_timeout_no_resubmission")


def write_result(root, report, probe=None):
    if probe is not None:
        raw = root / "raw" / "h0001.json"
        collect.write_json(raw, probe)
        report["evidence"] = {"id": "h0001", "raw_path": "raw/h0001.json",
                              "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}
    collect.write_json(root / "host-manifest.json", report)
    lines = ["# Host inspection", "", f"- Status: {report['status']}",
             f"- Transport: {report['transport']}", f"- Target: {report.get('instance_id') or report.get('host')}",
             "- Fixed metadata probe results; no application source execution, database queries or service restarts are performed.",
             "- SSM command history, temporary execution files and access audit logs may remain.", ""]
    if probe:
        lines += [f"- Processes: {len(probe['processes'])}", f"- Application roots: {len(probe['applications'])}",
                  f"- Gaps and limitations: {', '.join(probe['limitations']) or 'Runtime semantics require separate verification'}", "",
                  "## Running processes", "", "| PID | Name | Executable | Working directory |", "|---|---|---|---|"]
        def text(value):
            return html.escape(str(value)).replace("|", "&#124;")
        for item in probe["processes"]:
            lines.append("| " + " | ".join(text(item.get(k, "Unverified")) for k in ("pid", "name", "exe", "cwd")) + " |")
        lines += ["", "## Deployed code identity", "", "See raw/h0001.json for manifest fingerprints, dependency names and Git revisions from the specified application roots.",
                  "A deployed tree revision does not prove which code a running process has loaded.",
                  "Full argv/environ, systemd ExecStart, Docker Env/inspect, source bodies, logs and user data were not collected."]
    (root / "host-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "host-report.md").chmod(0o600)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transport", choices=("ssh", "ssm"), required=True)
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--known-hosts", type=Path, default=Path.home()/".ssh/known_hosts")
    p.add_argument("--identity-file", type=Path)
    p.add_argument("--instance-id")
    auth = p.add_mutually_exclusive_group()
    auth.add_argument("--profile")
    auth.add_argument("--environment", action="store_true")
    p.add_argument("--expected-account")
    p.add_argument("--region")
    p.add_argument("--app-root", action="append", default=[])
    p.add_argument("--execute", action="store_true", help="Execute the fixed probe; without this flag only create a local plan")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        validate_args(args)
        if args.output.exists():
            raise ValueError("output_exists_use_fresh_directory")
        args.output.mkdir(mode=0o700, parents=True)
    except (ValueError, OSError) as exc:
        print(f"Host inspection stopped: {exc}", file=sys.stderr)
        return 1
    report = {"schema_version": "1.0", "status": "planned", "transport": args.transport,
              "host": args.host, "instance_id": args.instance_id, "expected_account": args.expected_account,
              "region": args.region, "app_roots": args.app_root, "started_at": collect.now(),
              "application_changes_requested": False, "evidence": None,
              "ssm_control_plane_write": args.transport == "ssm" and args.execute,
              "probe_sha256": hashlib.sha256(payload(args.app_root).encode()).hexdigest()}
    data = None
    if args.execute:
        try:
            data = execute(args, report)
            report["status"] = data["status"]
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            report["status"] = "failed"
            report["error_type"] = type(exc).__name__
            report["error_code"] = str(exc) if type(exc) in (RuntimeError, ValueError) else "preflight_transport_or_response_failed"
            if report.get("execution_state"):
                report["remote_completion"] = "not_proven; inspect command_id before retry"
    report["completed_at"] = collect.now()
    write_result(args.output, report, data)
    print(json.dumps({"status": report["status"], "output": str(args.output), "command_id": report.get("command_id")}))
    return 1 if report["status"] in {"failed", "unsupported"} else 2 if report["status"] == "partial" else 0


if __name__ == "__main__":
    sys.exit(main())
