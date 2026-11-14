import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from harness import run_python, completed

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("setup_toolkit", "skills/aws-mcp-setup/scripts/setup_toolkit.py")
access = load("access", "skills/aws-cli-setup/scripts/verify_access.py")
collect = load("collect", "skills/aws-as-is-analysis/scripts/collect.py")
review = load("review", "skills/aws-architecture-review/scripts/review.py")
diagram = load("diagram", "skills/aws-architecture-review/scripts/diagram.py")
recommend = load("recommend", "skills/aws-modernization-pathway/scripts/recommend.py")


def fixture(root):
    """A synthetic snapshot with real evidence hashes; no AWS credentials."""
    root.mkdir(parents=True)
    (root / "raw").mkdir()
    raw = {"SecurityGroups": [{"GroupId": "sg-fixture", "IpPermissions": [
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]}]}
    raw_path = root / "raw/e0001.json"
    raw_path.write_text(json.dumps(raw))
    resource = {"id": "111111111111:us-east-1:ec2.security-group:sg-fixture", "type": "ec2.security-group",
                "native_id": "sg-fixture", "name": "fixture", "account_id": "111111111111", "region": "us-east-1",
                "attributes": raw["SecurityGroups"][0], "evidence_ids": ["e0001"], "observations": {}}
    common = {"schema_version": "1.0", "run_id": "fixture", "account_id": "111111111111",
              "regions": ["us-east-1"], "services": ["ec2"]}
    inventory = dict(common, collection_status="complete", resources=[resource], relationships=[])
    manifest = dict(common, status="complete", completed_at="2026-10-16T00:00:00Z", truncations=[],
                    coverage={"ok": 1}, calls=[{"id": "e0001", "service": "ec2",
                    "operation": "describe-security-groups", "status": "ok", "raw_path": "raw/e0001.json",
                    "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest()}])
    (root / "inventory.json").write_text(json.dumps(inventory))
    (root / "collection-manifest.json").write_text(json.dumps(manifest))
    return inventory, manifest


class InstallationTests(unittest.TestCase):
    def test_paths_all_clients_and_scopes(self):
        for target, prefix in (("claude", ".claude"), ("codex", ".agents"), ("kiro", ".kiro")):
            personal, _ = setup.locations(target, "user", "/project", home="/synthetic-user")
            project, _ = setup.locations(target, "project", "/synthetic-project", home="/unused")
            self.assertEqual(personal, Path("/synthetic-user") / prefix / "skills")
            self.assertEqual(project, Path("/synthetic-project") / prefix / "skills")

    def test_config_merge_preserves_other_servers_and_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in setup.TARGETS:
                path = root / (target + (".toml" if target == "codex" else ".json"))
                original = ('model = "existing"\n# preserve comment\n[mcp_servers.other]\nurl = "https://example.invalid"\n'
                            if target == "codex" else json.dumps({"unrelated": {"setting": 7}, "mcpServers": {"other": {"command": "existing"}}}))
                path.write_text(original)
                plan = setup.config_plan(target, path)
                setup.apply_config(plan)
                self.assertEqual(setup.config_plan(target, path)["action"], "unchanged")
                if target == "codex":
                    self.assertIn('# preserve comment', path.read_text())
                    self.assertEqual(setup.tomllib.loads(path.read_text())["model"], "existing")
                else:
                    self.assertEqual(json.loads(path.read_text())["unrelated"]["setting"], 7)
                self.assertTrue(list(root.glob(path.name + ".bak-*")))

    def test_config_conflict_and_invalid_file_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".mcp.json"
            for text in ('{"mcpServers":{"aws-knowledge":{"type":"http","url":"https://other.invalid"}}}', "{bad"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    setup.config_plan("claude", path)
                self.assertEqual(path.read_text(), text)

    def test_copy_update_and_user_edit_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, dest = root / "src", root / "skills" / "dst"
            source.mkdir()
            (source / "SKILL.md").write_text("initial")
            plan = setup.copy_plan(source, dest)
            setup.apply_copy(plan, dry_run=True)
            self.assertFalse(dest.exists())
            setup.apply_copy(plan)
            self.assertEqual(setup.copy_plan(source, dest)["action"], "unchanged")
            (source / "SKILL.md").write_text("next")
            setup.apply_copy(setup.copy_plan(source, dest, update=True))
            self.assertEqual((dest / "SKILL.md").read_text(), "next")
            self.assertFalse(list(dest.parent.glob(dest.name + ".bak-*")))
            (dest / "SKILL.md").write_text("user edit")
            (source / "SKILL.md").write_text("third")
            with self.assertRaises(ValueError):
                setup.copy_plan(source, dest, update=True)

    def test_toolkit_cli_configures_mcp_dry_run_and_second_run(self):
        with tempfile.TemporaryDirectory(prefix="moda space \ud55c\uae00 ") as temporary:
            root = Path(temporary) / "project"
            command = [str(ROOT / "skills/aws-mcp-setup/scripts/setup_toolkit.py"), "--targets", "kiro",
                       "--project-root", str(root), "--configure-mcp"]
            self.assertEqual(run_python(*command, "--dry-run", capture_output=True).returncode, 0)
            self.assertFalse(root.exists())
            first = run_python(*command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_python(*command, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(all(a["action"] == "unchanged" for a in json.loads(second.stdout)["actions"]))


    def test_doctor_reports_core_skills_and_plain_next_actions(self):
        """`prepare moda-skill` starts with doctor.py: it must tell an empty machine what is missing in
        words, and recognise an unmanaged copy without calling it installed."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".kiro/skills/aws-cli-setup").mkdir(parents=True)
            (home / ".kiro/skills/aws-cli-setup/SKILL.md").write_text("---\nname: aws-cli-setup\n---\n")
            out = home / "doctor.json"
            result = run_python(str(ROOT / "skills/aws-mcp-setup/scripts/doctor.py"), "--targets", "kiro",
                                     "--scope", "user", "--output", str(out), capture_output=True, text=True,
                                    env={**os.environ, "HOME": str(home)})
            self.assertEqual(result.returncode, 1, result.stderr)
            data = json.loads(out.read_text())
            core = data["clients"]["kiro"]["core_skills"]
            self.assertEqual(core["aws-cli-setup"], "unmanaged")
            self.assertEqual(core["aws-architecture-review"], "missing")
            self.assertEqual(data["status"], "incomplete")
            joined = " ".join(data["next_actions"])
            self.assertIn("core skills missing", joined)
            self.assertIn("aws-knowledge MCP not configured", joined)
            self.assertNotIn("aws-cli-setup", joined.split("core skills missing")[1].split("(")[0])
            summary = json.loads(result.stdout)
            self.assertEqual(summary["next_actions"], data["next_actions"])

    def test_install_skill_has_one_line_trigger_defaults_and_troubleshooting(self):
        text = (ROOT / "skills/aws-skills-install/SKILL.md").read_text(encoding="utf-8")
        front = text.split("---")[1]
        self.assertIn("prepare moda-skill", front)
        self.assertIn("moda-skill 준비해줘", front)
        for heading in ("## Defaults when the user gives none", "## Run order: diagnose, fix, verify",
                        "## Troubleshooting", "## Readiness table"):
            self.assertIn(heading, text)
        self.assertIn("doctor.py", text.split("## Run order")[1].split("## ")[0])
class CollectionTests(unittest.TestCase):
    def test_empty_sqs_cli_output_is_normalized_by_query(self):
        def fake(command, **kwargs):
            if "--version" in command:
                return completed(0, "aws-cli/2.0.0", "")
            if command[1:3] == ["sts", "get-caller-identity"]:
                return completed(0, '{"Account":"111111111111"}', "")
            if "--query" in command:
                self.assertEqual(command[command.index("--query")+1], "{QueueUrls: QueueUrls || `[]`}")
                return completed(0, '{"QueueUrls":[]}', "")
            return completed(0, "", "")
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"snapshot", expected_account="111111111111",
                regions=["us-east-1"], services=["sqs"], max_api_calls=10, no_discovery=True, max_detail_resources=10, timeout=10)
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=fake):
                self.assertEqual(collect.Collector(args).run(), 0)
            inventory, manifest, _ = review.verify_inputs(args.output)
            self.assertEqual(inventory["resources"], [])
            self.assertEqual(manifest["coverage"]["ok"], 2)

    def test_optional_platform_metadata_collection_and_relationships(self):
        account, region = "111111111111", "us-east-1"
        queue = f"https://sqs.{region}.amazonaws.com/{account}/work"
        dlq = f"https://sqs.{region}.amazonaws.com/{account}/failed"
        queue_arn, dlq_arn = f"arn:aws:sqs:{region}:{account}:work", f"arn:aws:sqs:{region}:{account}:failed"
        topic = f"arn:aws:sns:{region}:{account}:updates"
        cluster = f"arn:aws:kafka:{region}:{account}:cluster/example/fixture"
        rule = f"arn:aws:events:{region}:{account}:rule/default/to-queue"
        replies = {
            ("sts", "get-caller-identity"): {"Account": account},
            ("amp", "list-workspaces"): {"workspaces": [{"workspaceId": "ws-a", "alias": "metrics"}]},
            ("amp", "describe-workspace"): {"workspace": {"workspaceId": "ws-a", "status": {"statusCode": "ACTIVE"}}},
            ("grafana", "list-workspaces"): {"workspaces": [{"id": "g-a", "name": "dashboards"}]},
            ("grafana", "describe-workspace"): {"workspace": {"id": "g-a", "authentication": {"providers": ["AWS_SSO"]}}},
            ("kafka", "list-clusters-v2"): {"ClusterInfoList": [{"ClusterArn": cluster, "ClusterName": "example"}]},
            ("kafka", "describe-cluster-v2"): {"ClusterInfo": {"ClusterArn": cluster, "ClusterType": "SERVERLESS"}},
            ("sqs", "list-queues"): {"QueueUrls": [queue, dlq]},
            ("sns", "list-topics"): {"Topics": [{"TopicArn": topic}]},
            ("sns", "get-topic-attributes"): {"Attributes": {"TopicArn": topic, "FifoTopic": "false"}},
            ("events", "list-event-buses"): {"EventBuses": [{"Name": "default"}]},
            ("events", "describe-event-bus"): {"Name": "default"},
            ("events", "list-rules"): {"Rules": [{"Arn": rule, "Name": "to-queue", "State": "ENABLED"}]},
            ("events", "list-targets-by-rule"): [{"Id": "queue", "Arn": queue_arn, "RetryPolicy": {"MaximumRetryAttempts": 2}}],
        }
        commands = []
        def fake(command, **kwargs):
            commands.append(command)
            if "--version" in command:
                return completed(0, "aws-cli/2.0.0", "")
            op = tuple(command[1:3])
            self.assertIn(({v: k for k, v in collect.CLI_NAME.items()}.get(op[0], op[0]), op[1]), collect.ALLOWED)
            if op == ("sqs", "get-queue-attributes"):
                is_dlq = command[command.index("--queue-url")+1] == dlq
                payload = {"Attributes": {"QueueArn": dlq_arn if is_dlq else queue_arn}}
                if not is_dlq:
                    payload["Attributes"]["RedrivePolicy"] = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"})
            else:
                payload = replies[op]
            return completed(0, json.dumps(payload), "")
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"snapshot", expected_account=account,
                regions=[region], services=["amp","grafana","kafka","sqs","sns","events"],
                max_api_calls=50, no_discovery=True, max_detail_resources=10, timeout=10)
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=fake):
                self.assertEqual(collect.Collector(args).run(), 0)
            inventory, manifest, calls = review.verify_inputs(args.output)
            self.assertEqual(len(inventory["resources"]), 8)
            self.assertTrue(all(c["status"] == "ok" for c in calls.values()))
            self.assertIn("configured_target", {e["relation"] for e in inventory["relationships"]})
            self.assertIn("configured_dead_letter", {e["relation"] for e in inventory["relationships"]})
            for cmd in commands:
                if cmd[1:3] == ["events", "list-targets-by-rule"]:
                    query = cmd[cmd.index("--query")+1]
                    self.assertNotIn("Input", query)
                    self.assertIn("DeadLetterConfig", query)
                if cmd[1:3] == ["sqs", "list-queues"]:
                    self.assertEqual(cmd[cmd.index("--page-size")+1], "1000")

    def test_optional_detail_limit_and_failure_preserve_discovered_resources(self):
        def fake(command, **kwargs):
            if "--version" in command:
                return completed(0, "aws-cli/2.0.0", "")
            if command[1:3] == ["sts", "get-caller-identity"]:
                return completed(0, '{"Account":"111111111111"}', "")
            if command[1:3] == ["amp", "list-workspaces"]:
                return completed(0, '{"workspaces":[{"workspaceId":"one"},{"workspaceId":"two"}]}', "")
            return completed(255, "", "An error occurred (AccessDeniedException)")
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"snapshot", expected_account="111111111111",
                regions=["us-east-1"], services=["amp"], max_api_calls=10, no_discovery=True, max_detail_resources=1, timeout=10)
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=fake):
                self.assertEqual(collect.Collector(args).run(), 2)
            inventory, manifest, _ = review.verify_inputs(args.output)
            self.assertEqual(len(inventory["resources"]), 2)
            self.assertEqual(manifest["truncations"][0]["detailed"], 1)
            self.assertEqual(manifest["coverage"]["access_denied"], 1)

    def test_platform_allowlist_excludes_data_plane_and_mutations(self):
        for operation in [("sqs","receive-message"),("sqs","send-message"),("sqs","delete-message"),
                          ("sqs","purge-queue"),("sns","publish"),("events","put-events"),("events","start-replay"),
                          ("amp","create-workspace"),("grafana","create-workspace-api-key")]:
            with self.subTest(operation=operation):
                self.assertNotIn(operation, collect.ALLOWED)

    def test_profile_removes_static_environment_credentials(self):
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "example-key", "AWS_SECRET_ACCESS_KEY": "example-secret",
                                     "AWS_SESSION_TOKEN": "example-session", "AWS_ENDPOINT_URL": "https://example.invalid"}):
            for env in (access.aws_environment("profile"), collect.environment("assessment")):
                self.assertNotIn("AWS_ACCESS_KEY_ID", env)
                self.assertNotIn("AWS_SESSION_TOKEN", env)
                self.assertNotIn("AWS_ENDPOINT_URL", env)

    def test_environment_mode_isolated_from_profile_and_files(self):
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "example-key", "AWS_SECRET_ACCESS_KEY": "example-secret",
                                     "AWS_PROFILE": "unexpected", "AWS_ROLE_ARN": "unexpected"}):
            for env in (access.aws_environment("environment"), collect.environment(None)):
                self.assertNotIn("AWS_PROFILE", env)
                self.assertNotIn("AWS_ROLE_ARN", env)
                self.assertEqual(env["AWS_CONFIG_FILE"], os.devnull)

    def test_flatten_instances_and_missing_data(self):
        self.assertEqual(collect.select({"Reservations": [{"Instances": [{"InstanceId": "i-a"}]},
                                                           {"Instances": [{"InstanceId": "i-b"}]}]}, "Reservations.Instances"),
                         [{"InstanceId": "i-a"}, {"InstanceId": "i-b"}])
        self.assertEqual(collect.select(None, "Clusters"), [])

    def test_wrong_account_stops_after_sts(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"out", expected_account="111111111111",
                                      regions=["us-east-1"], services=["ec2"], max_api_calls=20, no_discovery=True, max_detail_resources=5, timeout=10)
            executed = []
            def fake(command, **kwargs):
                executed.append(command)
                if command[1:] == ["--version"]:
                    return completed(0, "aws-cli/2.0.0", "")
                return completed(0, json.dumps({"Account": "222222222222", "Arn": "arn:aws:iam::222222222222:role/test"}), "")
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=fake):
                code = collect.Collector(args).run()
            self.assertEqual(code, 1)
            self.assertEqual(len(executed), 2)
            manifest = json.loads((args.output/"collection-manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual([c["service"] for c in manifest["calls"]], ["sts"])

    def test_s3api_pagination_and_failures_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"out", expected_account="111111111111",
                                      regions=["us-east-1"], services=["s3"], max_api_calls=3, no_discovery=True, max_detail_resources=2, timeout=10)
            seen = []
            def fake(command, **kwargs):
                seen.append(command)
                if "--version" in command:
                    return completed(0, "aws-cli/2.0.0", "")
                if command[1:3] == ["s3api", "list-buckets"]:
                    return completed(0, '{"Buckets":[]}', "")
                return completed(255, "", "An error occurred (AccessDenied)")
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=fake):
                collector = collect.Collector(args)
                data, first = collector.call("s3", "list-buckets", "us-east-1")
                missing, second = collector.call("ec2", "describe-instances", "us-east-1")
            self.assertEqual(data, {"Buckets": []})
            self.assertEqual(first["status"], "ok")
            self.assertIsNone(missing)
            self.assertEqual(second["status"], "access_denied")
            self.assertTrue(any(cmd[1:3] == ["s3api", "list-buckets"] for cmd in seen))
            self.assertTrue(all("--max-items" not in cmd and "--no-paginate" not in cmd for cmd in seen))
            self.assertTrue(all((cmd[1], cmd[2]) in collect.ALLOWED or cmd[1] == "s3api" for cmd in seen if "--version" not in cmd))

    def test_budget_and_mutation_not_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"out", expected_account="111111111111",
                                      regions=["us-east-1"], services=["ec2"], max_api_calls=1, no_discovery=True, max_detail_resources=2, timeout=10)
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run",
                return_value=completed(0, "aws-cli/2.0.0", "")):
                c = collect.Collector(args)
            c.used = 1
            data, record = c.call("ec2", "describe-instances", "us-east-1")
            self.assertIsNone(data)
            self.assertEqual(record["error_code"], "api_budget_exhausted")
            with self.assertRaises(ValueError):
                c.call("ec2", "terminate-instances", "us-east-1")

    def test_successful_empty_s3_versioning_is_unversioned_not_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(profile="assessment", output=Path(temporary)/"out", expected_account="111111111111",
                                      regions=["us-east-1"], services=["s3"], max_api_calls=5, no_discovery=True, max_detail_resources=2, timeout=10)
            replies = [completed(0, "aws-cli/2.0.0", ""),
                       completed(0, "", "")]
            with patch.object(collect.shutil, "which", return_value="/fake/aws"), patch.object(collect.subprocess, "run", side_effect=replies):
                c = collect.Collector(args)
                data, record = c.call("s3", "get-bucket-versioning", "us-east-1", "--bucket", "example-bucket")
            self.assertEqual(data, {})
            self.assertEqual(record["status"], "ok")
            self.assertEqual(json.loads((args.output/record["raw_path"]).read_text()), {})


class AssessmentTests(unittest.TestCase):
    def test_skill_definitions_and_canonical_report_drafts_are_english(self):
        """Skill text is English. The one exception is a quoted phrase a user may type to trigger a skill
        ("moda-skill 준비해줘", "평가 시작해", "확정"): those must stay recognisable in the user's language."""
        import re
        quoted = re.compile(r"[\"`“]([^\"`”\n]{1,40})[\"`”]")
        for path in (ROOT/"skills").rglob("*"):
            if path.suffix == ".json" and (path.name.startswith("glossary.") or path.name == "next-steps.json"):
                continue  # target-language glossaries and the next-steps catalog's `ko` renderings carry other languages by design
            if path.is_file() and path.suffix in {".md", ".py", ".json", ".yaml", ".mjs"}:
                with self.subTest(path=path):
                    text = path.read_text(encoding="utf-8")
                    if path.suffix == ".md":
                        text = quoted.sub("", text)  # drop short quoted trigger phrases / reply tokens
                    self.assertIsNone(re.search(r"[\uac00-\ud7a3]", text))

    def test_sg_protocol_and_rule_scope(self):
        world = [{"CidrIp": "0.0.0.0/0"}]
        self.assertFalse(review.public_admin_permission([
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": world},
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]},
            {"IpProtocol": "icmp", "FromPort": 22, "ToPort": 22, "IpRanges": world},
        ]))
        self.assertTrue(review.public_admin_permission([
            {"IpProtocol": "-1", "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}]))
        self.assertIsNone(review.public_admin_permission(None))

    def test_unknown_scores_and_tampered_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/"as-is"
            fixture(root)
            inventory, manifest, calls = review.verify_inputs(root)
            checks = review.evaluate(inventory, manifest, calls)
            scores = review.score(checks, calls, {r["id"] for r in inventory["resources"]})
            self.assertIsNone(scores["Performance Efficiency"]["score"])
            self.assertEqual(scores["Performance Efficiency"]["evidence_coverage_percent"], 0)
            (root/"raw/e0001.json").write_text("{}")
            with self.assertRaises(ValueError):
                review.verify_inputs(root)

    def test_s3_effective_flags_combine_account_and_bucket(self):
        fields = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
        account = {"id": "a", "type": "s3.account-public-access-block", "attributes": {
            "PublicAccessBlockConfiguration": dict(zip(fields, (True, True, False, False)))}, "evidence_ids": ["e1"]}
        bucket = {"id": "b", "type": "s3.bucket", "attributes": {}, "evidence_ids": ["e2"], "observations": {
            "public_access_block": {"data": {"PublicAccessBlockConfiguration": dict(zip(fields, (False, False, True, True)))},
                                    "status": "ok", "evidence_id": "e2"}}}
        calls = {e: {"id": e, "status": "ok"} for e in ("e1", "e2")}
        inventory = {"resources": [account, bucket], "services": ["s3"]}
        manifest = {"calls": [], "truncations": []}
        checks = review.evaluate(inventory, manifest, calls)
        self.assertEqual(next(c for c in checks if c["rule_id"] == "S3_PUBLIC_ACCESS_BLOCK")["status"], "PASS")

    def test_evaluated_score_requires_successful_evidence(self):
        check = {"id": "f", "status": "PASS", "pillar": "Security", "resource_id": None, "weight": 1, "evidence_ids": []}
        with self.assertRaises(ValueError):
            review.score([check], {}, set())

    def test_modernization_unknown_and_blocker_cannot_rank(self):
        candidates = recommend.bootstrap()
        rated = recommend.rank(candidates, {"r1"}, {"f1"})
        self.assertTrue(all(c["score"] is None and c["rank"] is None for c in rated))
        c = candidates[0]
        c.update(status="candidate", resource_ids=["r1"], finding_ids=["f1"], target_services=["Amazon ECS"],
                 rollback=["validated previous deployment"], blockers=["unverified native dependency"])
        for criterion in c["criteria"].values():
            criterion.update(score=5, rationale="synthetic verified evidence")
        rated = recommend.rank(candidates, {"r1"}, {"f1"})
        self.assertEqual(rated[0]["score"], 100)
        self.assertFalse(rated[0]["rank_eligible"])
        c["blockers"] = []
        self.assertEqual(recommend.rank(candidates, {"r1"}, {"f1"})[0]["rank"], 1)
        c["finding_ids"] = ["unknown"]
        with self.assertRaises(ValueError):
            recommend.rank(candidates, {"r1"}, {"f1"})

    def test_graph_fidelity_and_escaping(self):
        graph = {"nodes": [{"id": "a", "label": 'app \"x\"\\n<untrusted>'},
                           {"id": "b", "label": "db"}],
                 "edges": [{"id": "e", "source": "a", "target": "b", "label": "SQL"}]}
        text = diagram.to_d2(graph)
        self.assertIn('\\"x\\"', text)
        with tempfile.TemporaryDirectory() as temporary:
            svg = Path(temporary)/"final.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><g data-node-id="a"/><g data-node-id="b"/><g data-edge-id="e"/></svg>')
            empty = {"resources": [], "relationships": [], "regions": ["us-east-1"]}
            self.assertEqual(diagram.audit(graph, svg, empty)["status"], "pass")
            self.assertEqual(diagram.audit(graph, svg)["status"], "fail", "an audit without the inventory cannot prove coverage")
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><g data-node-id="a"/></svg>')
            self.assertEqual(diagram.audit(graph, svg, empty)["status"], "fail")

    def test_legacy_candidates_preserved_and_devops_unassessed(self):
        candidates = [c for c in recommend.bootstrap() if c["pathway"] != "modern-devops"]
        candidates[0]["rationale"] = "Previously reviewed workload constraints"
        original = deepcopy(candidates)
        rated = recommend.rank(candidates, set(), set())
        self.assertEqual(candidates, original)
        self.assertEqual([c["pathway"] for c in rated[:-1]], [c["pathway"] for c in original])
        self.assertEqual(rated[0]["rationale"], original[0]["rationale"])
        devops = rated[-1]
        self.assertEqual(devops["pathway"], "modern-devops")
        self.assertEqual(devops["status"], "not_assessed")
        self.assertIsNone(devops["score"])
        self.assertIsNone(devops["rank"])
        self.assertEqual(devops["known_weight_percent"], 0)

    def test_pathway_upgrade_rejects_other_missing_duplicate_and_unknown(self):
        valid = recommend.bootstrap()
        invalid = [
            valid[1:],  # modern-devops present, container missing: not legacy
            valid[:-2],
            valid + [deepcopy(valid[0])],
            [dict(c, pathway="unexpected") if c["pathway"] == "modern-devops" else c for c in valid],
            [None],
            {},
        ]
        for candidates in invalid:
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                recommend.rank(candidates, set(), set())

    def test_modern_devops_uses_same_evidence_and_ranking_gates(self):
        candidates = recommend.bootstrap()
        devops = next(c for c in candidates if c["pathway"] == "modern-devops")
        devops.update(status="candidate", target_services=["Existing CI platform"],
                      validation=["Proven deployment failure gate"], rollback=["Restore verified artifact"])
        for criterion in devops["criteria"].values():
            criterion.update(score=4, rationale="Verified delivery history")
        with self.assertRaises(ValueError):
            recommend.rank(candidates, {"r1"}, {"f1"})
        devops.update(resource_ids=["r1"], finding_ids=["f1"], blockers=["Rollback drill pending"])
        rated = recommend.rank(candidates, {"r1"}, {"f1"})[-1]
        self.assertEqual(rated["score"], 80)
        self.assertIsNone(rated["rank"])
        devops["blockers"] = []
        self.assertEqual(recommend.rank(candidates, {"r1"}, {"f1"})[-1]["rank"], 1)
        devops["criteria"]["cost_confidence"]["score"] = None
        self.assertIsNone(recommend.rank(candidates, {"r1"}, {"f1"})[-1]["rank"])

    def test_ai_catalog_starts_unselected_and_unassessed(self):
        candidates = recommend.bootstrap()
        genai = next(c for c in candidates if c["pathway"] == "genai")
        self.assertEqual({o["option_id"] for o in genai["ai_options"]}, {
            "security-agent", "devops-agent", "bedrock", "bedrock-agentcore", "textract",
            "rekognition", "polly", "transcribe", "comprehend", "sagemaker-ai", "trainium", "inferentia",
        })
        options = recommend.validate_ai_options(genai["ai_options"], set(), set())
        self.assertTrue(all(not o["selected"] and not o["adoption_ready"] for o in options))
        self.assertTrue(all(o["status"] == "not_assessed" for o in options))
        self.assertTrue(all(set(o["readiness"].values()) == {"unknown"} for o in options))

    def test_ai_option_validation_rejects_bad_catalog_and_references(self):
        original = recommend.bootstrap_ai_options()
        for invalid in [original[:-1], original+[original[0]], [dict(o, option_id="other") for o in original], {}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                recommend.validate_ai_options(invalid, set(), set())
        for changes in [{"selected": True}, {"selected": "false"}, {"resource_ids": ["unknown"]},
                        {"finding_ids": ["unknown"]}, {"source_checked_at": "2099-01-01"}]:
            options = deepcopy(original)
            options[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                recommend.validate_ai_options(options, set(), set())

    def test_accelerator_requires_compatibility_and_measured_benchmark(self):
        for key in ("trainium", "inferentia"):
            options = recommend.bootstrap_ai_options()
            accelerator = next(o for o in options if o["option_id"] == key)
            accelerator.update(status="candidate", selected=True, use_case="Measured existing model workload",
                               resource_ids=["r1"], finding_ids=["f1"], validation=["Quality and latency comparison"],
                               source_checked_at="2026-10-20", cost_assumptions=["Measured synthetic workload baseline"])
            accelerator["readiness"] = dict.fromkeys(accelerator["readiness"], "confirmed")
            for gate in ("model_compatibility", "benchmark"):
                accelerator["readiness"][gate] = "unknown"
                with self.subTest(key=key, gate=gate), self.assertRaises(ValueError):
                    recommend.validate_ai_options(options, {"r1"}, {"f1"})
                accelerator["readiness"][gate] = "confirmed"
            result = recommend.validate_ai_options(options, {"r1"}, {"f1"})
            self.assertTrue(next(o for o in result if o["option_id"] == key)["adoption_ready"])

    def test_genai_rank_waits_for_all_selected_options(self):
        candidates = recommend.bootstrap()
        genai = next(c for c in candidates if c["pathway"] == "genai")
        genai.update(status="candidate", resource_ids=["r1"], finding_ids=["f1"],
                     target_services=["Amazon Bedrock"], rollback=["Previous workflow"])
        for criterion in genai["criteria"].values():
            criterion.update(score=5, rationale="Verified synthetic comparison")
        options = {o["option_id"]: o for o in genai["ai_options"]}
        model, agent = options["bedrock"], options["bedrock-agentcore"]
        for o in (model, agent):
            o.update(status="conditional", selected=True, resource_ids=["r1"], finding_ids=["f1"],
                     use_case="Known document workflow", validation=["Representative quality and auth tests"],
                     source_checked_at="2026-10-20", cost_assumptions=["Measured sample usage"])
        model["readiness"] = dict.fromkeys(model["readiness"], "confirmed")
        model["status"] = "candidate"
        agent["readiness"]["security"] = "blocked"
        result = next(c for c in recommend.rank(candidates, {"r1"}, {"f1"}) if c["pathway"] == "genai")
        self.assertEqual(result["score"], 100)
        self.assertIsNone(result["rank"])
        agent["selected"] = False
        result = next(c for c in recommend.rank(candidates, {"r1"}, {"f1"}) if c["pathway"] == "genai")
        self.assertEqual(result["rank"], 1)
        model["selected"] = False
        result = next(c for c in recommend.rank(candidates, {"r1"}, {"f1"}) if c["pathway"] == "genai")
        self.assertIsNone(result["rank"])

    def test_legacy_genai_assessment_keeps_input_but_defers_rank(self):
        candidates = recommend.bootstrap()
        genai = next(c for c in candidates if c["pathway"] == "genai")
        del genai["ai_options"]
        genai.update(status="candidate", resource_ids=["r1"], finding_ids=["f1"],
                     target_services=["Existing proposal"], rollback=["Previous version"])
        for criterion in genai["criteria"].values():
            criterion.update(score=4, rationale="Historical assessment")
        original = deepcopy(candidates)
        result = next(c for c in recommend.rank(candidates, {"r1"}, {"f1"}) if c["pathway"] == "genai")
        self.assertEqual(candidates, original)
        self.assertEqual(result["score"], 80)
        self.assertEqual(result["status"], "candidate")
        self.assertIsNone(result["rank"])
        self.assertEqual(len(result["ai_options"]), 12)

    def test_ai_reports_preserve_assessment_details_and_escape_html(self):
        options = recommend.bootstrap_ai_options()
        options[0].update(use_case="<script>alert('test')</script>", rationale="Known evidence & constraints", status="conditional",
                          risks=["User isolation is unverified"], validation=["Explicit permission test"])
        lines, markup = recommend.ai_option_reports(options)
        self.assertIn(options[0]["use_case"], "\n".join(lines))
        self.assertIn("User isolation is unverified", markup)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)
        for item in recommend.AI_OPTIONS.values():
            self.assertIn(item["name"], markup, "every option is still named")
            self.assertIn(item["name"], "\n".join(lines))
        # Options without evidence collapse into one line instead of a full section each.
        self.assertEqual(markup.count("<h4>"), 1)
        self.assertIn("Reviewed without evidence of need", markup)

    def test_platform_candidates_default_and_legacy_rank_preservation(self):
        candidates = recommend.bootstrap()
        devops = next(c for c in candidates if c["pathway"] == "modern-devops")
        native = next(c for c in candidates if c["pathway"] == "cloud-native")
        self.assertEqual(len(devops["observability_options"]), 3)
        self.assertEqual(len(native["messaging_options"]), 4)
        del devops["observability_options"]
        del native["messaging_options"]
        devops.update(status="candidate", resource_ids=["r1"], finding_ids=["f1"],
                      target_services=["Existing CI"], rollback=["Previous artifact"])
        for criterion in devops["criteria"].values():
            criterion.update(score=4, rationale="Measured delivery evidence")
        original = deepcopy(candidates)
        rated = recommend.rank(candidates, {"r1"}, {"f1"})
        self.assertEqual(candidates, original)
        result = next(c for c in rated if c["pathway"] == "modern-devops")
        self.assertEqual(result["rank"], 1)
        self.assertTrue(all(not o["selected"] for o in result["observability_options"]))

    def test_platform_selected_combination_and_specific_gates(self):
        for pathway, field, key, gate in [
            ("modern-devops","observability_options","opentelemetry","pipeline_resilience"),
            ("cloud-native","messaging_options","msk","kafka_compatibility"),
        ]:
            candidates = recommend.bootstrap()
            c = next(c for c in candidates if c["pathway"] == pathway)
            c.update(status="candidate", resource_ids=["r1"], finding_ids=["f1"],
                     target_services=[key], rollback=["Previous configuration"])
            for criterion in c["criteria"].values():
                criterion.update(score=5, rationale="Measured fixture")
            option = next(o for o in c[field] if o["option_id"] == key)
            option.update(status="conditional", selected=True, resource_ids=["r1"], finding_ids=["f1"],
                          use_case="Known workload problem", validation=["Compatibility and failure tests"],
                          source_checked_at="2026-10-20", cost_assumptions=["Measured workload"])
            option["readiness"] = dict.fromkeys(option["readiness"], "confirmed")
            option["readiness"][gate] = "unknown"
            rated = next(x for x in recommend.rank(candidates, {"r1"}, {"f1"}) if x["pathway"] == pathway)
            self.assertIsNone(rated["rank"])
            option["status"] = "candidate"
            with self.assertRaises(ValueError):
                recommend.rank(candidates, {"r1"}, {"f1"})
            option["readiness"][gate] = "confirmed"
            rated = next(x for x in recommend.rank(candidates, {"r1"}, {"f1"}) if x["pathway"] == pathway)
            self.assertEqual(rated["rank"], 1)

    def test_platform_reports_and_catalog_isolation(self):
        for pathway in ("modern-devops", "cloud-native"):
            candidate = next(c for c in recommend.bootstrap() if c["pathway"] == pathway)
            group = recommend.PLATFORM_GROUPS[pathway]
            field = group["field"]
            candidate[field][0]["rationale"] = "<script>example</script>"
            lines, markup = recommend.candidate_option_reports(candidate)
            self.assertIn(group["title"], markup)
            self.assertIn("&lt;script&gt;", markup)
            self.assertNotIn("<script>", markup)
            for o in group["options"]:
                self.assertIn(o["name"], "\n".join(lines))
            candidate[field][0]["option_id"] = "bedrock"
            candidates = recommend.bootstrap()
            candidates[[c["pathway"] for c in candidates].index(pathway)] = candidate
            with self.assertRaises(ValueError):
                recommend.rank(candidates, set(), set())

    def test_report_svg_stays_native_svg_in_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            svg = Path(temporary)/"figure.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100"/></svg>')
            markup = review.read_svg(svg)
            self.assertTrue(markup.startswith("<svg "))
            self.assertNotIn("<ns0:svg", markup)

    def test_offline_end_to_end_reports(self):
        with tempfile.TemporaryDirectory(prefix="moda evidence ") as temporary:
            root = Path(temporary)
            fixture(root/"03-as-is")
            evaluated = run_python(str(ROOT/"skills/aws-architecture-review/scripts/review.py"),
                                   "--as-is", str(root/"03-as-is"), "--output", str(root/"04-review"), capture_output=True, text=True)
            self.assertEqual(evaluated.returncode, 0, evaluated.stderr)
            generated = run_python(str(ROOT/"skills/aws-modernization-pathway/scripts/recommend.py"),
                                        "--inventory", str(root/"03-as-is/inventory.json"), "--findings", str(root/"04-review/findings.json"),
                                        "--workload", "synthetic-fixture", "--output", str(root/"05-modernization"),
                                       capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            pathways = json.loads((root/"05-modernization/pathways.json").read_text())
            self.assertEqual(len(pathways["candidates"]), 7)
            self.assertEqual(pathways["pathway_catalog_version"], "2.0")
            self.assertEqual(pathways["candidates"][-1]["pathway"], "modern-devops")
            self.assertEqual(pathways["ai_option_catalog_version"], "1.0")
            self.assertEqual(pathways["platform_option_catalog_version"], "1.0")
            self.assertEqual(len(next(c for c in pathways["candidates"] if c["pathway"] == "genai")["ai_options"]), 12)
            self.assertTrue((root/"04-review/review.html").is_file())
            self.assertTrue((root/"05-modernization/recommendations.html").is_file())
            for path in (root/"04-review/review.html", root/"05-modernization/recommendations.html"):
                self.assertIn('lang="en"', path.read_text())

    def test_legacy_cli_reports_upgrade_without_rewriting_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _ = fixture(root/"03-as-is")
            findings = {"schema_version": inventory["schema_version"], "run_id": inventory["run_id"],
                        "account_id": inventory["account_id"], "checks": []}
            (root/"findings.json").write_text(json.dumps(findings))
            legacy = [c for c in recommend.bootstrap() if c["pathway"] != "modern-devops"]
            del next(c for c in legacy if c["pathway"] == "genai")["ai_options"]
            del next(c for c in legacy if c["pathway"] == "cloud-native")["messaging_options"]
            source = root/"legacy.json"
            source.write_text(json.dumps(legacy))
            original = source.read_bytes()
            run = run_python(str(ROOT/"skills/aws-modernization-pathway/scripts/recommend.py"),
                "--inventory", str(root/"03-as-is/inventory.json"), "--findings", str(root/"findings.json"),
                "--candidates", str(source), "--workload", "legacy-fixture", "--output", str(root/"reports"),
                capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(source.read_bytes(), original)
            result = json.loads((root/"reports/pathways.json").read_text())
            self.assertEqual(len(result["candidates"]), 7)
            self.assertEqual(len(result["input_notes"]), 3)
            self.assertIsNone(result["candidates"][-1]["rank"])
            for extension in ("md", "html"):
                text = (root/f"reports/recommendations.{extension}").read_text()
                self.assertIn("Move to Modern DevOps", text)
                for note in result["input_notes"]:
                    self.assertIn(note, text)
                self.assertIn("Amazon Bedrock AgentCore", text)
                self.assertIn("Observability", text)
                self.assertIn("Amazon MSK", text)


if __name__ == "__main__":
    unittest.main()
