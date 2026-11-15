"""Guardrails that keep an assessment read-only even when the profile could change the account: the write-capability
check and its gate, the guard's run mode (profile pinning, protected files, approved commands), plan_check.py, the
helper integrity check and the CloudTrail write audit. No AWS calls: the AWS CLI is replaced by fake results."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness import completed, run_python

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "skills/moda-skill/scripts/hook.py"
PLAN_CHECK = ROOT / "skills/moda-skill/scripts/plan_check.py"
SCOPE = ROOT / "skills/aws-cli-setup/scripts/scope.py"
ACCOUNT = "111111111111"
READ = "aws ec2 describe-vpcs --region us-east-1 --profile assessment"


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


access = load("guard_access", "skills/aws-cli-setup/scripts/verify_access.py")
collect = load("guard_collect", "skills/aws-as-is-analysis/scripts/collect.py")
audit = load("guard_audit", "skills/aws-modernization-pathway/scripts/write_audit.py")


def make_run(project, write_capability="no", acknowledged=None, profile="assessment", host_inspection=False):
    run = project / "artifacts/shop/20261114T000000Z"
    (run / "01-setup").mkdir(parents=True)
    (run / "01-setup/access-check.json").write_text(json.dumps({
        "status": "verified", "account_id": ACCOUNT, "checked_at": "2026-11-14T00:00:00+00:00",
        "principal_arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/Assessment/session",
        "write_capability": {"status": write_capability}}))
    scope = {"auth": {"mode": "profile", "profile": profile}, "regions": ["us-east-1", "ap-northeast-2"],
             "host_inspection": {"enabled": host_inspection, "instance_ids": ["i-0123456789abcdef0"] if host_inspection else []}}
    if acknowledged:
        scope["write_capable_acknowledged"] = {"reason": acknowledged, "at": "2026-11-14T00:01:00+00:00"}
    (run / "scope.json").write_text(json.dumps(scope))
    return run


class GuardRunMode(unittest.TestCase):
    """Inside an assessment project the guard pins the profile, protects files and runs only approved commands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home, self.project = base / "home", base / "project"
        (self.home / ".aws").mkdir(parents=True)
        self.project.mkdir()
        self.run = make_run(self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, event):
        return run_python(str(HOOK), "guard", input=json.dumps(event), capture_output=True, text=True, cwd=self.project,
                          env=dict(os.environ, HOME=str(self.home)))

    def shell(self, command):
        return self.guard({"tool_name": "shell", "tool_input": {"command": command}})

    def approve(self, *commands):
        (self.run / "agent-commands.json").write_text(json.dumps(
            {"commands": [{"id": f"c{k}", "purpose": "check the VPC layout", "command": c} for k, c in enumerate(commands, 1)]}))
        return run_python(str(PLAN_CHECK), "--run-dir", str(self.run), capture_output=True, text=True)

    def test_own_aws_reads_need_an_approved_list(self):
        first = self.shell(READ)
        self.assertEqual(first.returncode, 2)
        self.assertIn("plan_check.py", first.stderr)
        result = self.approve(READ)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.shell(READ).returncode, 0)
        self.assertEqual(self.shell(READ + " | head -n 5").returncode, 0, "output may be piped")
        self.assertEqual(self.shell(READ.replace("describe-vpcs", "describe-subnets")).returncode, 2, "only the approved call")
        self.assertEqual(self.shell(READ + " --filters Name=x,Values=y").returncode, 2, "with the approved arguments")
        (self.run / "agent-commands.json").write_text((self.run / "agent-commands.json").read_text() + " ")
        self.assertIn("changed after it was approved", self.shell(READ).stderr)

    def test_bootstrap_calls_and_helpers_need_no_approval(self):
        for command in ("aws sts get-caller-identity --profile assessment", "aws configure list-profiles", "aws --version",
                        "aws sso login --profile assessment",
                        'python3 "$HOME/.kiro/skills/aws-as-is-analysis/scripts/collect.py" --profile assessment --regions us-east-1'):
            with self.subTest(command=command):
                result = self.shell(command)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_call_stays_on_the_runs_profile(self):
        self.approve(READ)
        for command in (READ.replace("--profile assessment", "--profile admin"), READ.replace(" --profile assessment", ""),
                        "AWS_PROFILE=admin " + READ, "export AWS_PROFILE=admin", "export AWS_ACCESS_KEY_ID=AKIDEXAMPLE",
                        "AWS_SHARED_CREDENTIALS_FILE=/tmp/other " + READ, "AWS_ENDPOINT_URL=https://example.invalid " + READ,
                        "aws configure set role_arn arn:aws:iam::111111111111:role/Admin --profile assessment",
                        "aws configure set profile.assessment.source_profile admin"):
            with self.subTest(command=command):
                self.assertEqual(self.shell(command).returncode, 2)
        self.assertEqual(self.shell("aws configure set region us-west-2 --profile assessment").returncode, 0)
        self.assertEqual(self.shell("aws configure set role_arn arn:aws:iam::111111111111:role/X --profile other").returncode, 0,
                         "other profiles are the person's business")

    def test_use_aws_follows_the_same_rules(self):
        self.approve(READ)
        call = {"service_name": "ec2", "operation_name": "describe-vpcs", "region": "us-east-1", "profile_name": "assessment", "label": "x"}
        self.assertEqual(self.guard({"tool_name": "use_aws", "tool_input": call}).returncode, 0)
        self.assertEqual(self.guard({"tool_name": "use_aws", "tool_input": dict(call, profile_name="admin")}).returncode, 2)
        no_profile = {k: v for k, v in call.items() if k != "profile_name"}
        self.assertEqual(self.guard({"tool_name": "use_aws", "tool_input": no_profile}).returncode, 2)
        self.assertEqual(self.guard({"tool_name": "use_aws", "tool_input": dict(call, operation_name="describe-subnets")}).returncode, 2)

    def test_credentials_skills_hooks_and_run_records_are_protected(self):
        for command in ("echo '[profile x]' >> ~/.aws/config", "cat ~/.aws/credentials", "cp evil.py .kiro/skills/aws-as-is-analysis/scripts/collect.py",
                        f"sed -i 's/no/yes/' {self.run}/01-setup/access-check.json", f"rm {self.run}/scope.json",
                        f"echo '{{}}' > {self.run}/agent-commands.approved.json", "rm -rf ~/.local/share/moda-skill",
                        "tee .claude/settings.json < x"):
            with self.subTest(command=command):
                self.assertEqual(self.shell(command).returncode, 2, command)
        events = [{"tool_name": "write", "tool_input": {"command": "create", "path": str(self.home / ".aws/config"), "file_text": "x"}},
                  {"tool_name": "Write", "tool_input": {"file_path": str(self.run / "scope.json"), "content": "{}"}},
                  {"tool_name": "Edit", "tool_input": {"file_path": str(self.project / ".kiro/hooks/moda-skill.json"), "old_string": "a", "new_string": "b"}},
                  {"tool_name": "apply_patch", "tool_input": {"input": "*** Begin Patch\n*** Update File: .agents/skills/moda-skill/scripts/hook.py\n@@\n-x\n+y\n*** End Patch"}}]
        for event in events:
            with self.subTest(event=event["tool_name"]):
                self.assertEqual(self.guard(event).returncode, 2)
        allowed = [{"tool_name": "write", "tool_input": {"command": "create", "path": str(self.run / "agent-commands.json"), "file_text": "{}"}},
                   {"tool_name": "Write", "tool_input": {"file_path": str(self.run / "04-review/notes.md"), "content": "aws ec2 delete-vpc"}}]
        for event in allowed:
            with self.subTest(event=event["tool_input"]):
                self.assertEqual(self.guard(event).returncode, 0)
        self.assertEqual(self.shell("cat ~/.aws/config").returncode, 0, "reading the profile config is fine")

    def test_outside_an_assessment_only_the_read_only_rules_apply(self):
        other = self.project.parent / "elsewhere"
        other.mkdir()
        result = run_python(str(HOOK), "guard", input=json.dumps({"tool_input": {"command": READ.replace("assessment", "anything")}}),
                            capture_output=True, text=True, cwd=other, env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(result.returncode, 0, result.stderr)


class PlanCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = make_run(Path(self.tmp.name).resolve())

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *commands, purpose="check the VPC layout"):
        (self.run / "agent-commands.json").write_text(json.dumps(
            {"commands": [{"id": f"c{k}", "purpose": purpose, "command": c} for k, c in enumerate(commands, 1)]}))
        result = run_python(str(PLAN_CHECK), "--run-dir", str(self.run), capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_reads_on_the_runs_profile_and_regions_are_approved(self):
        code, out = self.check(READ, "aws iam list-roles --region us-east-1 --profile assessment --output json --no-paginate",
                               "aws rds describe-db-instances --region ap-northeast-2 --profile assessment --query 'DBInstances[].Engine'")
        self.assertEqual((code, out["status"]), (0, "approved"), out)
        approved = json.loads((self.run / "agent-commands.approved.json").read_text())
        self.assertEqual(approved["source_sha256"], hashlib.sha256((self.run / "agent-commands.json").read_bytes()).hexdigest())
        self.assertEqual(len(approved["commands"]), 3)

    def test_each_unsafe_entry_is_rejected_with_its_reason(self):
        cases = {
            "aws ec2 terminate-instances --instance-ids i-1 --region us-east-1 --profile assessment": "not a read-only",
            "aws secretsmanager get-secret-value --secret-id x --region us-east-1 --profile assessment": "data content or credentials",
            "aws ec2 describe-vpcs --region us-east-1 --profile admin": "--profile assessment",
            "aws ec2 describe-vpcs --region eu-west-1 --profile assessment": "outside the run's scope",
            "aws ec2 describe-vpcs --profile assessment": "add --region",
            READ + " | xargs echo": "one plain aws command",
            READ + " > out.json": "one plain aws command",
            "aws ec2 describe-vpcs --region $REGION --profile assessment": "literal values",
            READ + " --endpoint-url https://example.invalid": "--endpoint-url is not allowed",
            READ + " --debug": "--debug is not allowed",
            READ + " --debu": "--debu is not allowed",
            READ + " --endpoint https://example.invalid": "--endpoint is not allowed",
            READ + " --no-verify": "--no-verify is not allowed",
            "aws ec2 describe-vpcs --filters file://f.json --region us-east-1 --profile assessment": "reads a local file",
            "AWS_PROFILE=assessment " + READ: "must start with aws",
            "aws configure list-profiles": "do not need approval",
        }
        for command, reason in cases.items():
            with self.subTest(command=command):
                code, out = self.check(command)
                self.assertIn(code, (2, 3), out)
                self.assertIn(reason, out["rejected"][0]["reason"])
                (self.run / ".agent-commands-rounds").unlink(missing_ok=True)
        code, out = self.check(READ, purpose="")
        self.assertIn("purpose", out["rejected"][0]["reason"])

    def test_three_rejected_rounds_stop_and_a_pass_resets(self):
        bad = "aws ec2 delete-vpc --vpc-id v --region us-east-1 --profile assessment"
        self.assertEqual(self.check(bad)[0], 2)
        self.assertEqual(self.check(bad)[0], 2)
        code, out = self.check(bad)
        self.assertEqual((code, out["status"]), (3, "stop_and_ask"))
        self.assertFalse((self.run / "agent-commands.approved.json").exists())
        self.assertEqual(self.check(READ)[0], 0)
        self.assertFalse((self.run / ".agent-commands-rounds").exists())


class WriteCapability(unittest.TestCase):
    def args(self):
        return argparse.Namespace(profile="assessment", region="us-east-1")

    def simulate(self, allowed):
        results = [{"EvalActionName": a, "EvalDecision": "allowed" if a in allowed else "implicitDeny"}
                   for a in access.WRITE_PROBES + access.EXPECTED_WRITES]
        return completed(0, json.dumps({"EvaluationResults": results}))

    def test_admin_user_is_write_capable_with_long_term_keys(self):
        with patch.object(access.subprocess, "run", return_value=self.simulate({"ec2:TerminateInstances", "iam:CreateUser"})) as run:
            out = access.write_capability(f"arn:aws:iam::{ACCOUNT}:user/someone", {}, self.args())
        self.assertEqual(out["principal_type"], "iam_user")
        self.assertTrue(out["long_term_credentials"])
        self.assertEqual(out["write_capability"]["status"], "yes")
        self.assertEqual(out["write_capability"]["allowed"], ["ec2:TerminateInstances", "iam:CreateUser"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:3], ["iam", "simulate-principal-policy"])

    def test_read_only_role_resolves_its_path_and_is_not_write_capable(self):
        role = completed(0, json.dumps({"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_ReadOnly_x"}}))
        with patch.object(access.subprocess, "run", side_effect=[role, self.simulate({"ssm:SendCommand"})]) as run:
            out = access.write_capability(f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_ReadOnly_x/me", {}, self.args())
        self.assertEqual(out["write_capability"]["status"], "no")
        self.assertEqual(out["write_capability"]["expected_writes_allowed"], ["ssm:SendCommand"])
        self.assertIn("aws-reserved/sso.amazonaws.com", run.call_args_list[1][0][0][run.call_args_list[1][0][0].index("--policy-source-arn") + 1])

    def test_root_unreadable_and_unsupported_principals(self):
        self.assertEqual(access.write_capability(f"arn:aws:iam::{ACCOUNT}:root", {}, self.args())["write_capability"]["status"], "yes")
        denied = completed(255, "", "An error occurred (AccessDenied) when calling the SimulatePrincipalPolicy operation")
        with patch.object(access.subprocess, "run", return_value=denied):
            out = access.write_capability(f"arn:aws:iam::{ACCOUNT}:user/someone", {}, self.args())
        self.assertEqual((out["write_capability"]["status"], out["write_capability"]["reason"]), ("unknown", "AccessDenied"))
        out = access.write_capability(f"arn:aws:sts::{ACCOUNT}:federated-user/x", {}, self.args())
        self.assertEqual(out["write_capability"]["status"], "unknown")


class RunGateAndIntegrity(unittest.TestCase):
    def test_helpers_refuse_a_write_capable_run_until_the_person_decides(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run = make_run(project, write_capability="yes")
            reason = collect.assessment_gate(run / "03-as-is")
            self.assertIn("write_capable_profile_not_acknowledged", reason)
            scope = json.loads((run / "scope.json").read_text())
            scope["write_capable_acknowledged"] = {"reason": "sandbox account, the person accepted", "at": "2026-11-14T00:01:00Z"}
            (run / "scope.json").write_text(json.dumps(scope))
            self.assertIsNone(collect.assessment_gate(run / "03-as-is"))
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), write_capability="unknown")
            self.assertIsNotNone(collect.assessment_gate(run / "03-supplementary"))
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), write_capability="no")
            self.assertIsNone(collect.assessment_gate(run / "03-host/i-1"))
            self.assertIsNone(collect.assessment_gate(Path(tmp) / "standalone/out"), "a helper used on its own is not gated")

    def test_a_modified_helper_refuses_to_run(self):
        self.assertIsNone(collect.verify_skill_files(), "the repository's helpers match the manifest")
        with tempfile.TemporaryDirectory() as tmp:
            skills = Path(tmp)
            skill = skills / "aws-as-is-analysis"
            (skill / "scripts").mkdir(parents=True)
            (skill / "scripts/collect.py").write_text("print('original')\n")
            digest = hashlib.sha256(b"print('original')\n").hexdigest()
            (skills / "aws-skills-install/assets").mkdir(parents=True)
            (skills / "aws-skills-install/assets/install-manifest.json").write_text(json.dumps(
                {"skills": [{"name": "aws-as-is-analysis", "files": [{"path": "scripts/collect.py", "sha256": digest}]}]}))
            self.assertIsNone(collect.verify_skill_files(skill))
            (skill / "scripts/collect.py").write_text("print('changed')\n")
            self.assertIn("skill_file_modified", collect.verify_skill_files(skill))

    def test_scope_records_the_persons_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "scope.json"
            result = run_python(str(SCOPE), "write", "--account", ACCOUNT, "--regions", "us-east-1", "--workload-name", "shop",
                                "--type", "account", "--language", "en", "--profile", "assessment", "--confirmed-by", "interview",
                                "--acknowledge-write-capable", "sandbox account; the person accepts the risk", "--output", str(out),
                                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(out.read_text())
            self.assertEqual(doc["write_capable_acknowledged"]["reason"], "sandbox account; the person accepts the risk")
            doc["write_capable_acknowledged"] = {"reason": " "}
            out.write_text(json.dumps(doc))
            check = run_python(str(SCOPE), "validate", str(out), capture_output=True, text=True)
            self.assertNotEqual(check.returncode, 0)
            self.assertIn("write_capable_acknowledged", check.stdout + check.stderr)


class WriteAudit(unittest.TestCase):
    PRINCIPAL = f"arn:aws:sts::{ACCOUNT}:assumed-role/Assessment/session"

    def event(self, source, name, arn, agent="aws-cli/2.34.45 md/awscrt#0.32.2 ua/2.1 os/macos#25.6.0 app/kiro-cli"):
        return {"EventTime": "2026-11-14T00:10:00Z", "EventSource": source, "EventName": name,
                "CloudTrailEvent": json.dumps({"userIdentity": {"arn": arn}, "userAgent": agent})}

    def test_changes_by_the_principal_are_reported_and_others_ignored(self):
        events = [self.event("ec2.amazonaws.com", "TerminateInstances", self.PRINCIPAL),
                  self.event("ssm.amazonaws.com", "SendCommand", self.PRINCIPAL),
                  self.event("iam.amazonaws.com", "CreateUser", f"arn:aws:sts::{ACCOUNT}:assumed-role/Ops/other")]
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), host_inspection=True)
            with patch.object(audit.subprocess, "run", return_value=completed(0, json.dumps({"Events": events}))) as run_cli:
                result = audit.audit(run)
            self.assertEqual(result["status"], "writes_found")
            self.assertEqual({e["name"] for e in result["unexpected"]}, {"TerminateInstances"})
            self.assertEqual(result["unexpected"][0]["tool"], "aws-cli/2.34.45 app/kiro-cli", "the tool that made the call")
            self.assertEqual(result["unexpected_by_tool"], {"aws-cli/2.34.45 app/kiro-cli": 2}, "one event per region from the mock")
            self.assertEqual({e["name"] for e in result["expected"]}, {"SendCommand"})
            self.assertEqual(sorted(result["regions"]), ["ap-northeast-2", "us-east-1"])
            argv = run_cli.call_args[0][0]
            self.assertEqual(argv[1:3], ["cloudtrail", "lookup-events"])
            self.assertIn("AttributeKey=ReadOnly,AttributeValue=false", argv)

    def test_clean_and_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp))
            with patch.object(audit.subprocess, "run", return_value=completed(0, json.dumps({"Events": []}))):
                self.assertEqual(audit.audit(run)["status"], "clean")
            denied = completed(255, "", "An error occurred (AccessDeniedException) when calling the LookupEvents operation")
            with patch.object(audit.subprocess, "run", return_value=denied):
                result = audit.audit(run)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["regions"]["us-east-1"]["error_code"], "AccessDeniedException")

    def test_report_states_the_audit(self):
        sys.path.insert(0, str(ROOT / "skills/aws-modernization-pathway/scripts"))
        report = load("guard_report", "skills/aws-modernization-pathway/scripts/assessment_report.py")
        self.assertIn("not run", report.write_audit_text(None))
        clean = {"status": "clean", "regions": {"us-east-1": {}}, "window": {"start": "s", "end": "e"}, "expected": [], "note": "n"}
        self.assertIn("no change by the assessment's principal", report.write_audit_text(clean))
        found = dict(clean, status="writes_found", unexpected=[{"time": "t", "source": "ec2.amazonaws.com", "name": "TerminateInstances", "region": "us-east-1"}])
        self.assertIn("TerminateInstances", report.write_audit_html(found))
        found.update(unexpected_by_tool={"cloudformation.amazonaws.com": 3}, regions={"us-east-1": {"status": "error", "error_code": "page_limit_reached"}})
        text = report.write_audit_text(found)
        self.assertIn("cloudformation.amazonaws.com (3)", text)
        self.assertIn("another session", text)
        self.assertIn("us-east-1 (page_limit_reached)", text)

    def test_run_length_ends_at_the_last_stage_file(self):
        sys.path.insert(0, str(ROOT / "skills/aws-modernization-pathway/scripts"))
        report = load("guard_report_clock", "skills/aws-modernization-pathway/scripts/assessment_report.py")
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20261115T080000Z"
            stamp = lambda path, minute: (path.parent.mkdir(parents=True, exist_ok=True), path.write_text("{}"),
                                          os.utime(path, (1794729600 + minute * 60,) * 2))  # 2026-11-15T08:00:00Z + minutes
            stamp(run / "05-modernization/pathways.json", 40)
            stamp(run / "06-assessment/assessment.html", 55)
            stamp(run / "agent-commands.json", 300)
            report.WALL_CLOCK.update(minutes=None, end=report.last_stage_write(run))
            self.assertEqual(report.run_minutes({"run_id": run.name}), 40, "report folders and later files do not count")
            report.WALL_CLOCK.update(minutes=12)
            self.assertEqual(report.run_minutes({"run_id": run.name}), 12, "--wall-clock-minutes wins")


if __name__ == "__main__":
    unittest.main()
