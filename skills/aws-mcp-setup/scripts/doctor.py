#!/usr/bin/env python3
"""Inspect local prerequisites and optionally perform an AWS Knowledge MCP handshake."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from setup_toolkit import add_location_arguments, locations, MCP_URL



CERT_HINT = ("this Python cannot verify TLS certificates (python.org builds on macOS ship without CA roots). "
             "Run 'Install Certificates.command' in /Applications/Python 3.x or `python3 -m pip install certifi`. "
             "Kiro CLI, Claude Code and Codex use their own TLS stack, so the assessment itself still connects.")


def ssl_contexts():
    """Trust stores to try in order: the interpreter default, certifi, then the macOS system roots."""
    yield ssl.create_default_context()
    try:
        import certifi  # noqa: WPS433 (optional)
        yield ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if platform.system() == "Darwin" and shutil.which("security"):
        try:
            pem = subprocess.run(["security", "find-certificate", "-a", "-p", "/System/Library/Keychains/SystemRootCertificates.keychain"],  # nosec B603 B607 - static argv
                                 capture_output=True, text=True, timeout=20).stdout
            if pem.strip():
                context = ssl.create_default_context()
                context.load_verify_locations(cadata=pem)
                yield context
        except (OSError, subprocess.SubprocessError, ssl.SSLError):
            pass


def urlopen(request, timeout=20):
    """urlopen that falls back to other trust stores when the default one cannot verify the server."""
    if not str(request.full_url).startswith("https://"):
        raise ValueError("only https endpoints are probed")
    last = None
    for context in ssl_contexts():
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=context)  # nosec B310 - https only, fixed MCP_URL
        except urllib.error.URLError as exc:
            last = exc
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
    raise last


def rpc(method, request_id, params=None, session=None, protocol="2025-03-26"):
    payload = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": protocol}
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(MCP_URL, json.dumps(payload).encode(), headers=headers)
    with urlopen(request, timeout=20) as response:
        text = response.read(4_000_000).decode()
        if not text.strip():
            return {}, response.headers.get("Mcp-Session-Id")
        if text.lstrip().startswith("{"):
            body = json.loads(text)
        else:
            bodies = [json.loads(line[5:].strip()) for line in text.splitlines()
                      if line.startswith("data:") and line[5:].strip()]
            body = next((b for b in bodies if b.get("id") == request_id), {})
        if body.get("error"):
            raise ValueError("MCP JSON-RPC error")
        return body.get("result", {}), response.headers.get("Mcp-Session-Id")


def probe_mcp():
    try:
        init, session = rpc("initialize", 1, {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "moda-toolkit-doctor", "version": "1.0"},
        })
        protocol = init.get("protocolVersion", "2025-03-26")
        rpc("notifications/initialized", None, session=session, protocol=protocol)
        tools, _ = rpc("tools/list", 2, session=session, protocol=protocol)
        names = [tool["name"] for tool in tools.get("tools", [])]
        if not names:
            raise ValueError("empty tool list")
        return {"status": "connected", "url": MCP_URL,
                "protocol_version": protocol, "tools": names}
    except Exception as exc:
        reason = getattr(exc, "reason", None)
        result = {"status": "unavailable", "url": MCP_URL, "error_type": type(exc).__name__}
        if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(exc, ssl.SSLCertVerificationError):
            result["error_type"] = "SSLCertVerificationError"
            result["hint"] = CERT_HINT
        return result


# One literal argv per tool this doctor knows; nothing outside this table is ever started.
VERSION_PROBES = {
    "aws": lambda: subprocess.run(["aws", "--version"], capture_output=True, text=True, timeout=15),  # nosec B603 B607
    "git": lambda: subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15),  # nosec B603 B607
    "node": lambda: subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=15),  # nosec B603 B607
    "d2": lambda: subprocess.run(["d2", "--version"], capture_output=True, text=True, timeout=15),  # nosec B603 B607
}


def inspect_binary(name):
    executable = shutil.which(name)
    if not executable:
        return {"status": "missing"}
    try:
        result = VERSION_PROBES[name]()
        version = (result.stdout or result.stderr).strip().splitlines()[0]
        return {"status": "available" if not result.returncode else "failed",
                "version": version[:150], "path": executable}
    except (OSError, subprocess.SubprocessError, IndexError, KeyError):
        return {"status": "failed"}


def main(argv=None):
    """Command line entry point; the moda-skill CLI calls it in-process with an explicit argv."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_location_arguments(parser)
    parser.add_argument("--probe-mcp", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output exists; use a new path")
    binaries = {name: inspect_binary(name) for name in ("aws", "git", "node", "d2")}
    if binaries["aws"].get("version") and not binaries["aws"]["version"].startswith("aws-cli/2."):
        binaries["aws"]["status"] = "wrong_version"
    if binaries["node"].get("version"):
        try:
            if int(binaries["node"]["version"].lstrip("v").split(".")[0]) < 18:
                binaries["node"]["status"] = "wrong_version"
        except ValueError:
            binaries["node"]["status"] = "unknown_version"
    installed = {}
    core_skills = ("aws-cli-setup", "aws-mcp-setup", "aws-as-is-analysis",
                   "aws-architecture-review", "aws-modernization-pathway", "aws-skills-install", "moda-skill")
    required_files = {
        "aws-diagram-design": ("SKILL.md", "assets/aws-icons/INDEX.md",
                               "references/style-guide.md", "scripts/self_check.py"),
        "aws-bento-deck": ("SKILL.md", "assets/AWS_Deck_Template.template.bento.html",
                           "scripts/splice_deck.mjs", "scripts/validate_deck.mjs",
                           "scripts/render_slides.mjs"),
    }
    for target in args.targets:
        skill_root, config = locations(target, args.scope, args.project_root)
        core = {}
        for name in core_skills:
            stamp = skill_root / name / ".moda-install.json"
            if not (skill_root / name / "SKILL.md").is_file():
                core[name] = "missing"
            elif stamp.is_file():
                core[name] = "installed"
            else:
                core[name] = "unmanaged"  # present without a stamp: user copy, never overwrite
        installed[target] = {
            "skills": {name: all((skill_root / name / relative).is_file()
                                 and (skill_root / name / relative).stat().st_size > 0
                                 for relative in files)
                       for name, files in required_files.items()},
            "core_skills": core, "skills_dir": str(skill_root),
            "config_exists": config.is_file(), "config_path": str(config),
            "config_has_aws_knowledge": config.is_file() and "knowledge-mcp.global.api.aws" in config.read_text(encoding="utf-8", errors="replace"),
            "client_connection": "not_tested",
        }
    result = {
        "schema_version": "1.0", "checked_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0], "binaries": binaries, "clients": installed,
        "mcp": probe_mcp() if args.probe_mcp else {"status": "not_tested"},
        "browser_rendering": "not_tested",
    }
    local_ready = all(b["status"] == "available" for b in binaries.values()) and all(
        all(c["skills"].values()) and all(v != "missing" for v in c["core_skills"].values())
        for c in installed.values())
    result["status"] = "local_prerequisites_ready" if local_ready else "incomplete"
    # Plain-language next actions, so the agent (or a person) knows what to fix without reading the JSON.
    actions = []
    for name, info in binaries.items():
        if info["status"] != "available":
            actions.append(f"{name}: {info['status']}" + (" (install; D2 is optional but needed for diagram rendering)" if name == "d2" else ""))
    for target, c in installed.items():
        missing = [n for n, v in c["core_skills"].items() if v == "missing"]
        if missing:
            actions.append(f"{target}: core skills missing: {', '.join(missing)} (run `moda-skill config --harness {target}`, or follow aws-skills-install steps 1-4, 7)")
        if not all(c["skills"].values()):
            actions.append(f"{target}: companions missing: {', '.join(n for n, ok in c['skills'].items() if not ok)} (setup_toolkit.py --install-companions)")
        if not c["config_has_aws_knowledge"]:
            actions.append(f"{target}: aws-knowledge MCP not configured in {c['config_path']} (setup_toolkit.py --configure-mcp)")
    if args.probe_mcp and result["mcp"]["status"] != "connected":
        actions.append("MCP probe: " + result["mcp"]["hint"] if result["mcp"].get("hint")
                       else "MCP endpoint unreachable: check outbound network/proxy to knowledge-mcp.global.api.aws")
    result["next_actions"] = actions
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "mcp": result["mcp"]["status"],
                      "next_actions": actions, "output": str(args.output)}, ensure_ascii=False))
    return 0 if local_ready and (not args.probe_mcp or result["mcp"]["status"] == "connected") else 1


if __name__ == "__main__":
    sys.exit(main())
