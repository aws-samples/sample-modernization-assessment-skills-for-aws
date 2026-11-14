"""Hooks: read-only guard decisions, stop-gate decisions, and `config --hooks` wiring. No AWS calls, no network."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from harness import run_python

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "skills/moda-skill/scripts/hook.py"
CLI = ROOT / "scripts/moda_skill_cli.py"

spec = importlib.util.spec_from_file_location("moda_hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def run_hook(mode, event, cwd):
    return run_python(str(HOOK), mode, input=json.dumps(event), capture_output=True, text=True, cwd=cwd)


class ReadOnlyGuard(unittest.TestCase):
    ALLOWED = [
        "aws ec2 describe-instances --region us-east-1",
        "AWS_PROFILE=x aws --profile p sts get-caller-identity --output json",
        "aws configure list-profiles && aws configure set region us-east-1 --profile a",
        "ACCT=$(aws sts get-caller-identity --query Account --output text); aws ec2 describe-vpcs",
        "aws s3 ls && aws s3api list-buckets && aws s3api get-bucket-encryption --bucket b",
        "aws ssm send-command --instance-ids i-1 --document-name AWS-RunShellScript --parameters commands=uptime",
        "aws ssm get-command-invocation --command-id c --instance-id i-1",
        "aws wafv2 get-web-acl-for-resource --resource-arn a",
        "aws cloudtrail lookup-events --max-items 5",
        "aws iam get-role-policy --role-name r --policy-name p",
        "aws cloudformation get-template --stack-name s",
        "aws codecommit get-file --repository-name r --file-path app.py",
        "python3 collect.py --profile p | tee out.log",
        "aws sso login --profile assessment",
        "aws ec2 help",
    ]
    BLOCKED = [
        ("aws ec2 terminate-instances --instance-ids i-1", "not a read-only"),
        ("aws cloudformation deploy --stack-name x --template-file t.yaml", "not a read-only"),
        ("aws iam create-user --user-name x", "not a read-only"),
        ("aws secretsmanager get-secret-value --secret-id x", "data content or credentials"),
        ("aws ssm get-parameter --name /x --with-decryption", "data content or credentials"),
        ("aws s3api get-object --bucket b --key k out", "data content or credentials"),
        ("aws s3 cp s3://b/k .", "moves object data"),
        ("aws s3 sync s3://b .", "moves object data"),
        ("aws logs get-log-events --log-group-name g --log-stream-name s", "data content or credentials"),
        ("aws lambda get-function --function-name f", "data content or credentials"),
        ("aws ecr get-login-password | docker login --username AWS --password-stdin x", "data content or credentials"),
        ("aws eks update-kubeconfig --name c", "not a read-only"),
        ("aws ec2 describe-instances; aws ec2 modify-instance-attribute --instance-id i --no-source-dest-check", "not a read-only"),
        ("timeout 60 aws rds delete-db-instance --db-instance-identifier d", "not a read-only"),
        ("cd /tmp && env AWS_REGION=us-east-1 aws dynamodb scan --table-name t", "data content or credentials"),
        ("aws kms generate-data-key --key-id k --key-spec AES_256", "not a read-only"),
        ("aws apigateway test-invoke-method --rest-api-id a --resource-id r --http-method GET", "not a read-only"),
        ("aws ssm start-session --target i-1", "not a read-only"),
    ]
    # Verb families that change something. The guard decides on the command alone, before any call is made, so the
    # result is the same whatever the credentials allow (an administrator role included).
    MUTATING_VERBS = ("create", "delete", "put", "update", "modify", "run", "start", "stop", "terminate", "reboot",
                      "attach", "detach", "associate", "disassociate", "register", "deregister", "enable", "disable",
                      "tag", "untag", "add", "remove", "set", "reset", "replace", "restore", "import", "copy", "upload",
                      "publish", "send", "invoke", "execute", "rotate", "authorize", "revoke", "accept", "reject",
                      "cancel", "apply", "generate", "test")
    MUTATING_SERVICES = ("ec2", "iam", "s3api", "rds", "lambda", "ecs", "eks", "cloudformation", "dynamodb", "kms",
                         "secretsmanager", "ssm", "sns", "sqs", "elbv2", "route53", "organizations", "sts")

    @staticmethod
    def decide(command):
        reasons = [hook.judge(*call) for tokens in hook.segments(command) for call in [hook.aws_call(tokens)] if call]
        return [r for r in reasons if r]

    def test_allowed_commands_pass(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), [])
        result = run_hook("guard", {"tool_name": "execute_bash", "tool_input": {"command": self.ALLOWED[0]}}, ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_writes_and_data_reads_are_blocked_with_a_reason(self):
        for command, reason in self.BLOCKED:
            with self.subTest(command=command):
                reasons = self.decide(command)
                self.assertTrue(reasons, command)
                self.assertIn(reason, " ".join(reasons))
        result = run_hook("guard", {"tool_name": "Bash", "tool_input": {"command": self.BLOCKED[0][0]}}, ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("read-only guard blocked", result.stderr)

    def test_non_aws_commands_and_malformed_events_are_ignored(self):
        self.assertEqual(run_hook("guard", {"tool_input": {"command": "ls -la && git status"}}, ROOT).returncode, 0)
        self.assertEqual(run_python(str(HOOK), "guard", input="not json", capture_output=True, text=True).returncode, 0)
        self.assertEqual(run_hook("guard", {"tool_input": {"command": "echo 'aws is a cloud'"}}, ROOT).returncode, 0)

    def test_judge_covers_every_denied_pair(self):
        for service, operation in hook.DENY:
            self.assertIsNotNone(hook.judge(service, operation), (service, operation))

    def test_every_mutating_verb_is_blocked_on_every_service(self):
        allowed_writes = {("ssm", "send-command")}  # the optional host inspection, scoped by the sample IAM policy
        for service in self.MUTATING_SERVICES:
            for verb in self.MUTATING_VERBS:
                operation = f"{verb}-resource"
                if (service, operation) in allowed_writes:
                    continue
                with self.subTest(service=service, operation=operation):
                    self.assertIsNotNone(hook.judge(service, operation))
                    self.assertTrue(self.decide(f"aws {service} {operation} --region us-east-1"))
        self.assertIsNone(hook.judge("ssm", "send-command"))

    def test_use_aws_tool_calls_are_checked_like_the_cli(self):
        def call(tool_input):
            return run_hook("guard", {"tool_name": "use_aws", "tool_input": tool_input}, ROOT)
        read = call({"service_name": "ec2", "operation_name": "describe-instances", "region": "us-east-1", "label": "x"})
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(call({"service_name": "ec2", "operation_name": "DescribeInstances"}).returncode, 0)
        for tool_input, reason in (
                ({"service_name": "ec2", "operation_name": "terminate-instances", "parameters": {"instance-ids": "i-1"}}, "not a read-only"),
                ({"service_name": "ec2", "operation_name": "TerminateInstances"}, "not a read-only"),
                ({"service_name": "iam", "operation_name": "create-access-key"}, "not a read-only"),
                ({"service_name": "secretsmanager", "operation_name": "get-secret-value"}, "data content or credentials"),
                ({"service_name": "kms", "operation_name": "generate-data-key"}, "not a read-only"),
                ({}, "names no service and operation"),
                ({"service_name": "ec2"}, "names no service and operation")):
            with self.subTest(tool_input=tool_input):
                result = call(tool_input)
                self.assertEqual(result.returncode, 2, tool_input)
                self.assertIn(reason, result.stderr)
        for service in self.MUTATING_SERVICES:
            with self.subTest(service=service):
                self.assertEqual(call({"service_name": service, "operation_name": "delete-resource"}).returncode, 2)
        other_tool = run_hook("guard", {"tool_name": "mcp_aws-knowledge_aws___search_documentation",
                                        "tool_input": {"search_phrase": "aws ec2 terminate"}}, ROOT)
        self.assertEqual(other_tool.returncode, 0, "tools that call no AWS API are left alone")


class StopGate(unittest.TestCase):
    def make_run(self, root, complete=False, stale=False, draft=False):
        run = root / "artifacts/shop/run1"
        (run / "01-setup").mkdir(parents=True)
        (run / "01-setup/access-check.json").write_text("{}")
        (run / "scope.json").write_text("{}")
        if complete:
            for rel in ("02-toolkit/toolkit-check.json", "03-as-is/inventory.json", "03-as-is-enriched/inventory.json", "04-review/findings.json",
                        "04-review/diagrams/as-is.aws.svg", "05-modernization/pathways.json", "06-assessment/assessment.html"):
                (run / rel).parent.mkdir(parents=True, exist_ok=True)
                (run / rel).write_text("{}")
            (run / "06-assessment/assessment-summary.json").write_text(json.dumps({"status": "Draft - diagrams pending" if draft else "Final"}))
        if stale:
            old = time.time() - 4 * 3600
            for path in [run] + list(run.rglob("*")):
                os.utime(path, (old, old))
        return run

    def test_incomplete_recent_run_asks_the_agent_to_continue_at_most_three_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.make_run(root)
            decisions = [run_hook("stop", {}, root) for _ in range(4)]
            for result in decisions[:3]:
                self.assertEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["decision"], "block")
                self.assertIn("2 aws-mcp-setup", payload["reason"])
                self.assertIn("02-toolkit/toolkit-check.json", payload["reason"])
            self.assertEqual(decisions[3].stdout.strip(), "", "the gate gives up after MAX_CONTINUES")
            self.assertEqual((run / ".moda-stop-continues").read_text(), "3")

    def test_pause_resume_stale_complete_and_reentry_stay_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.make_run(root)
            run_python(str(HOOK), "pause", "need the profile", cwd=root, check=True, capture_output=True)
            self.assertEqual((run / ".moda-paused").read_text(), "need the profile")
            self.assertEqual(run_hook("stop", {}, root).stdout.strip(), "")
            run_python(str(HOOK), "resume", cwd=root, check=True, capture_output=True)
            self.assertFalse((run / ".moda-paused").exists())
            self.assertIn("block", run_hook("stop", {}, root).stdout)
            self.assertEqual(run_hook("stop", {"stop_hook_active": True}, root).stdout.strip(), "", "Claude Code re-entry flag")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_run(root, stale=True)
            self.assertEqual(run_hook("stop", {}, root).stdout.strip(), "", "a run untouched for hours is not in progress")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_run(root, complete=True)
            self.assertEqual(run_hook("stop", {}, root).stdout.strip(), "", "Final report: nothing to continue")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_run(root, complete=True, draft=True)
            self.assertIn("Draft", json.loads(run_hook("stop", {}, root).stdout)["reason"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_hook("stop", {}, Path(tmp)).stdout.strip(), "", "no run at all")


class HookWiring(unittest.TestCase):
    def test_config_hooks_writes_each_client_and_keeps_foreign_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".claude").mkdir()
            (project / ".claude/settings.json").write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo other-tool"}]}]}}))
            for harness in ("kiro", "claude", "codex"):
                out = run_python(str(CLI), "config", "--harness", harness, "--skip-mcp", "--skip-companions", "--hooks",
                                     capture_output=True, text=True, cwd=project)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(json.loads(out.stdout)["hooks"]["action"], "install" if harness != "claude" else "update")
                again = json.loads(run_python(str(CLI), "config", "--harness", harness, "--skip-mcp", "--skip-companions", "--hooks",
                                                  capture_output=True, text=True, cwd=project).stdout)
                self.assertEqual(again["hooks"]["action"], "unchanged")
            kiro = json.loads((project / ".kiro/hooks/moda-skill.json").read_text())
            self.assertEqual({h["trigger"] for h in kiro["hooks"]}, {"PreToolUse", "Stop"})
            self.assertEqual(next(h for h in kiro["hooks"] if h["trigger"] == "PreToolUse")["matcher"], "shell")
            guards = [h for h in kiro["hooks"] if h["trigger"] == "PreToolUse"]
            self.assertEqual({h["matcher"] for h in guards}, {"shell", "use_aws"}, "Kiro's AWS tool is guarded too")
            self.assertTrue(all(h["action"]["command"].endswith(" guard") for h in guards))
            self.assertIn(".kiro/skills/moda-skill/scripts/hook.py", kiro["hooks"][0]["action"]["command"])
            claude = json.loads((project / ".claude/settings.json").read_text())
            self.assertEqual(claude["permissions"], {"allow": ["Read"]}, "other settings untouched")
            commands = [h["command"] for g in claude["hooks"]["PreToolUse"] for h in g["hooks"]]
            self.assertIn("echo other-tool", commands, "foreign hook kept")
            self.assertTrue(any(".claude/skills/moda-skill/scripts/hook.py\" guard" in c for c in commands))
            self.assertTrue((project / ".claude/settings.json.bak-moda").is_file())
            codex = json.loads((project / ".codex/hooks.json").read_text())
            self.assertIn("Stop", codex["hooks"])
            self.assertIn(".agents/skills/moda-skill/scripts/hook.py", codex["hooks"]["PreToolUse"][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
