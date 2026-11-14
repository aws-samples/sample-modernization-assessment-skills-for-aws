"""Parallel collection: same inventory as sequential, concurrency cap, per-service budget share."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from harness import completed

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collect_parallel", ROOT / "skills/aws-as-is-analysis/scripts/collect.py")
collect = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collect
spec.loader.exec_module(collect)

ACCOUNT, REGION = "111111111111", "us-east-1"
BUCKETS = [f"bucket-{i:02d}" for i in range(40)]


class FakeAws:
    """Answers the collector's CLI calls; records peak concurrency."""

    def __init__(self, delay=0.0):
        self.delay, self.active, self.peak, self.lock = delay, 0, 0, threading.Lock()

    def __call__(self, command, **kwargs):
        if "--version" in command:
            return completed(0, "aws-cli/2.0.0", "")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return completed(0, json.dumps(self.reply(command)), "")
        finally:
            with self.lock:
                self.active -= 1

    @staticmethod
    def reply(command):
        op = tuple(command[1:3])
        if op == ("sts", "get-caller-identity"):
            return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/test"}
        if op == ("s3api", "list-buckets"):
            return {"Buckets": [{"Name": name} for name in BUCKETS]}
        if op == ("s3api", "get-bucket-location"):
            return {"LocationConstraint": None}
        if op == ("ec2", "describe-vpcs"):
            return {"Vpcs": [{"VpcId": "vpc-1"}]}
        if op == ("ec2", "describe-subnets"):
            return {"Subnets": [{"SubnetId": "subnet-1", "VpcId": "vpc-1"}]}
        if op == ("ecs", "list-clusters"):
            return {"clusterArns": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/c{i}" for i in range(3)]}
        if op == ("ecs", "describe-clusters"):
            arn = command[command.index("--clusters") + 1]
            return {"clusters": [{"clusterArn": arn, "clusterName": arn.rsplit("/", 1)[-1]}]}
        if op == ("ecs", "list-services"):
            cluster = command[command.index("--cluster") + 1]
            return {"serviceArns": [f"{cluster}/svc{i}" for i in range(2)]}
        if op == ("ecs", "describe-services"):
            arn = command[command.index("--services") + 1]
            return {"services": [{"serviceArn": arn, "serviceName": arn.rsplit("/", 1)[-1]}]}
        if op == ("cloudwatch", "describe-alarms"):
            return {"MetricAlarms": [{"AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:cpu", "AlarmName": "cpu"}]}
        if op == ("logs", "describe-log-groups"):
            return {"logGroups": [{"logGroupName": "/shop/app"}]}
        return {}


def run(services, workers, max_calls=500, delay=0.0):
    fake = FakeAws(delay)
    with tempfile.TemporaryDirectory() as temporary:
        args = argparse.Namespace(profile="assessment", output=Path(temporary) / "snapshot", expected_account=ACCOUNT,
                                  regions=[REGION], services=services, max_api_calls=max_calls,
                                  max_detail_resources=100, timeout=10, max_workers=workers, no_discovery=True)
        with patch.object(collect.shutil, "which", return_value="/fake/aws"), \
                patch.object(collect.subprocess, "run", side_effect=fake):
            code = collect.Collector(args).run()
        inventory = json.loads((args.output / "inventory.json").read_text())
        manifest = json.loads((args.output / "collection-manifest.json").read_text())
    return code, inventory, manifest, fake


def shape(inventory):
    return (sorted(r["id"] for r in inventory["resources"]),
            sorted((e["source"], e["target"], e["relation"]) for e in inventory["relationships"]))


class ParallelCollection(unittest.TestCase):
    SERVICES = ["ec2", "ecs", "s3", "cloudwatch"]

    def test_parallel_matches_sequential(self):
        sequential = run(self.SERVICES, workers=1)
        parallel = run(self.SERVICES, workers=8)
        self.assertEqual(sequential[0], parallel[0])
        self.assertEqual(shape(sequential[1]), shape(parallel[1]))
        self.assertEqual(sequential[2]["api_calls_executed"], parallel[2]["api_calls_executed"])
        ids = [c["id"] for c in parallel[2]["calls"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_concurrency_never_exceeds_max_workers(self):
        *_, fake = run(self.SERVICES, workers=4, delay=0.01)
        self.assertLessEqual(fake.peak, 4)
        self.assertGreater(fake.peak, 1)

    def test_large_service_cannot_starve_later_services(self):
        # 40 buckets need ~162 S3 calls. With a 60-call budget the old sequential order spent it all on S3
        # and skipped CloudWatch. Each service now has a reserved share, so CloudWatch still runs.
        code, inventory, manifest, _ = run(["s3", "cloudwatch"], workers=1, max_calls=60)
        ops = {(c["service"], c["operation"]): c["status"] for c in manifest["calls"]
               if c["service"] in ("cloudwatch", "logs")}
        self.assertEqual(ops[("cloudwatch", "describe-alarms")], "ok")
        self.assertEqual(ops[("logs", "describe-log-groups")], "ok")
        self.assertIn("logs.group", {r["type"] for r in inventory["resources"]})
        self.assertEqual(code, 2)  # S3 detail is cut short and reported, not hidden
        self.assertLessEqual(manifest["api_calls_executed"], 60)
        self.assertEqual(manifest["limits"]["reserved_calls_per_service"], 15)

    def test_budget_is_never_exceeded_in_parallel(self):
        _, _, manifest, _ = run(self.SERVICES, workers=8, max_calls=50)
        executed = sum(c["status"] != "skipped" for c in manifest["calls"])
        self.assertEqual(executed, manifest["api_calls_executed"])
        self.assertLessEqual(executed, 50)

    def test_finished_services_release_unused_share(self):
        # ec2 and cloudwatch need few calls; S3 runs last and may use what they left unused.
        _, _, manifest, _ = run(["ec2", "cloudwatch", "s3"], workers=1, max_calls=60)
        self.assertEqual(manifest["api_calls_executed"], 60)


if __name__ == "__main__":
    unittest.main()
