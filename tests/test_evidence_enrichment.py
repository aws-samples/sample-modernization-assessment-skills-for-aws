"""Regression cases from workload assessments; all evidence is synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_workflow import ROOT, fixture, load, review
from harness import run_python

supplement = load("supplement", "skills/aws-as-is-analysis/scripts/merge_supplementary_evidence.py")
localize = load("enrichment_localize", "skills/aws-mcp-setup/scripts/localize_artifacts.py")


def pack(root, inventory, raw=b'{"BackupPlansList": []}'):
    root.mkdir()
    (root / "response.json").write_bytes(raw)
    record = {
        "id": "s0001", "service": "backup", "operation": "list-backup-plans",
        "region": "us-east-1", "arguments": ["--output", "json"],
        "collection_method": "aws_cli", "return_code": 0, "status": "ok",
        "error_code": None, "collected_at": "2026-10-20T00:00:00Z",
        "raw_path": "response.json", "sha256": hashlib.sha256(raw).hexdigest(),
        "raw_format": "json", "evidence_kind": "configuration", "response_complete": True,
        "resource_ids": [inventory["resources"][0]["id"]],
        "provenance": {"origin": "scoped account backup metadata", "redactions": []},
    }
    data = {"schema_version": "1.0", "account_id": inventory["account_id"],
            "regions": inventory["regions"], "records": [record]}
    write_pack(root, data)
    return data


def write_pack(root, data):
    (root / "supplementary-manifest.json").write_text(json.dumps(data))


class SupplementTests(unittest.TestCase):
    def test_successful_cli_with_invalid_payload_retains_zero_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _ = fixture(root / "base")
            data = pack(root / "supplement", inventory)
            data["records"][0].update(status="error", error_code="JSONDecodeError",
                                     validation_error="blank_cli_stdout", return_code=0,
                                     raw_path=None, sha256=None, response_complete=False)
            write_pack(root / "supplement", data)
            supplement.merge(root / "base", root / "supplement", root / "derived")
            _, manifest, calls = review.verify_inputs(root / "derived")
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(calls["s0001"]["return_code"], 0)
            self.assertEqual(calls["s0001"]["status"], "error")

    def test_import_preserves_inputs_identity_and_invocation_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, manifest = fixture(root / "base")
            manifest.update(api_calls_executed=7, read_only=True)
            (root / "base/collection-manifest.json").write_text(json.dumps(manifest))
            pack(root / "supplement", inventory)
            originals = {p: p.read_bytes() for folder in ("base", "supplement")
                         for p in (root / folder).rglob("*") if p.is_file()}
            command = [str(ROOT / "skills/aws-as-is-analysis/scripts/merge_supplementary_evidence.py"),
                       "--as-is", str(root / "base"), "--supplement-dir", str(root / "supplement"),
                       "--output", str(root / "derived")]
            result = run_python(*command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            merged, recorded, calls = review.verify_inputs(root / "derived")
            self.assertEqual(merged["resources"], inventory["resources"])
            self.assertEqual(recorded["services"], manifest["services"])
            self.assertEqual(recorded["api_calls_executed"], 7)
            self.assertEqual(recorded["supplementary_evidence_added"], 1)
            self.assertEqual(recorded["status"], "complete")
            self.assertEqual(calls["s0001"]["response_complete"], True)
            for path, before in originals.items():
                self.assertEqual(path.read_bytes(), before)
            self.assertNotEqual(run_python(*command, capture_output=True).returncode, 0)

    def test_denied_and_incomplete_supplements_cannot_make_collection_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _ = fixture(root / "base")
            data = pack(root / "supplement", inventory)
            data["records"][0]["response_complete"] = False
            denied = deepcopy(data["records"][0])
            denied.update(id="s0002", status="access_denied", return_code=254,
                          error_code="AccessDeniedException", raw_path=None, sha256=None)
            data["records"].append(denied)
            write_pack(root / "supplement", data)
            supplement.merge(root / "base", root / "supplement", root / "derived")
            _, manifest, calls = review.verify_inputs(root / "derived")
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(manifest["coverage"], {"ok": 2, "access_denied": 1})
            self.assertEqual(len(manifest["truncations"]), 2)
            self.assertIsNone(calls["s0002"]["raw_path"])

    def test_blank_malformed_and_scalar_payloads_are_rejected_before_writes(self):
        for raw in (b"", b" \n", b"{broken", b"null", b'""'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                inventory, _ = fixture(root / "base")
                pack(root / "supplement", inventory, raw)
                with self.assertRaises(ValueError):
                    supplement.merge(root / "base", root / "supplement", root / "derived")
                self.assertFalse((root / "derived").exists())

    def test_identity_integrity_provenance_and_status_must_agree(self):
        mutations = [
            lambda d: d.update(account_id="222222222222"),
            lambda d: d["records"][0].update(region="ap-northeast-2"),
            lambda d: d["records"][0].update(return_code=254),
            lambda d: d["records"][0].update(sha256="0" * 64),
            lambda d: d["records"][0].update(raw_path="../base/raw/e0001.json"),
            lambda d: d["records"][0].update(resource_ids=["unknown"]),
            lambda d: d["records"][0].update(evidence_kind="source_declared"),
            lambda d: d["records"][0].update(response_complete="true"),
            lambda d: d["records"][0].update(raw_format="text"),
            lambda d: d["records"].append(deepcopy(d["records"][0])),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                inventory, _ = fixture(root / "base")
                data = pack(root / "supplement", inventory)
                mutate(data)
                write_pack(root / "supplement", data)
                with self.assertRaises(ValueError):
                    supplement.merge(root / "base", root / "supplement", root / "derived")
                self.assertFalse((root / "derived").exists())

    def test_source_is_preserved_with_commit_but_not_promoted_to_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _ = fixture(root / "base")
            raw = b"if stored_password == supplied_password:\n    return user\n"
            data = pack(root / "supplement", inventory, raw)
            data["records"][0].update(raw_format="text", evidence_kind="source_declared")
            data["records"][0]["provenance"].update(revision="a" * 40, locator="auth.py:1")
            write_pack(root / "supplement", data)
            supplement.merge(root / "base", root / "supplement", root / "derived")
            _, _, calls = review.verify_inputs(root / "derived")
            record = calls["s0001"]
            self.assertEqual(record["evidence_kind"], "source_declared")
            self.assertEqual((root / "derived" / record["raw_path"]).read_bytes(), raw)


class AssessmentBasisTests(unittest.TestCase):
    def test_scope_weights_and_status_changes_are_distinguished(self):
        with tempfile.TemporaryDirectory() as temporary:
            inventory, manifest = fixture(Path(temporary) / "base")
            calls = {c["id"]: c for c in manifest["calls"]}
            baseline = review.evaluate(inventory, manifest, calls)
            changed = deepcopy(baseline)
            changed[0].update(status="WARN", assessment_reason="Compensating control verified separately")
            basis = review.assessment_basis(inventory, baseline, changed)
            self.assertTrue(basis["same_check_scope_and_weights"])
            self.assertEqual(basis["modified"][0]["changes"]["status"]["before"], "FAIL")
            self.assertEqual(basis["unexplained_modifications"], [])
            changed[0]["weight"] += 1
            basis = review.assessment_basis(inventory, baseline, changed)
            self.assertFalse(basis["same_check_scope_and_weights"])
            del changed[0]["assessment_reason"]
            self.assertEqual(len(review.assessment_basis(inventory, baseline, changed)["unexplained_modifications"]), 1)
            changed.pop()
            added = dict(changed[0], id="manual1", rule_id="WORKLOAD_DECLARED_CONTROL")
            changed.append(added)
            basis = review.assessment_basis(inventory, baseline, changed)
            self.assertEqual(len(basis["added"]), 1)
            self.assertEqual(len(basis["removed"]), 1)

    def test_duplicate_and_inferred_judgments_cannot_inflate_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            inventory, manifest = fixture(Path(temporary) / "base")
            calls = {c["id"]: c for c in manifest["calls"]}
            baseline = review.evaluate(inventory, manifest, calls)
            resources = {r["id"] for r in inventory["resources"]}
            with self.assertRaises(ValueError):
                review.score(baseline + [dict(baseline[0], id="duplicate")], calls, resources)
            for confidence in ("inferred", "unverified", "unknown-value"):
                changed = deepcopy(baseline)
                changed[0]["confidence"] = confidence
                with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                    review.score(changed, calls, resources)

    def test_existing_snapshot_blank_success_is_rejected_even_with_valid_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "base"
            _, manifest = fixture(root)
            (root / "raw/e0001.json").write_bytes(b"")
            manifest["calls"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
            (root / "collection-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                review.verify_inputs(root)

    def test_cli_keeps_baseline_scores_and_change_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, manifest = fixture(root / "base")
            calls = {c["id"]: c for c in manifest["calls"]}
            checks = review.evaluate(inventory, manifest, calls)
            expected = review.score(checks, calls, {r["id"] for r in inventory["resources"]})
            changed = deepcopy(checks)
            changed[0].update(weight=1, assessment_reason="Explicit workload-specific weighting")
            (root / "checks.json").write_text(json.dumps(changed))
            result = run_python(str(ROOT / "skills/aws-architecture-review/scripts/review.py"),
                "--as-is", str(root / "base"), "--output", str(root / "review"),
                "--checks", str(root / "checks.json"), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "review/scorecard.baseline.json").read_text()), expected)
            basis = json.loads((root / "review/assessment-basis.json").read_text())
            self.assertFalse(basis["same_check_scope_and_weights"])
            self.assertEqual(len(basis["modified"]), 1)
            self.assertEqual(json.loads((root / "review/findings.json").read_text())["checks"], changed)
            # New explanatory fields are translatable; evidence IDs remain protected.
            self.assertIn("assessment_reason", localize.NARRATIVE_KEYS)
            self.assertTrue(localize.PROTECTED.fullmatch("s0001"))


if __name__ == "__main__":
    unittest.main()


def alb_fixture(root):
    """Snapshot with one internet-facing ALB so SEC_WAF has a subject."""
    inventory, manifest = fixture(root)
    arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/app/fixture/abc123"
    raw = {"LoadBalancers": [{"LoadBalancerArn": arn, "Scheme": "internet-facing", "Type": "application"}]}
    raw_path = root / "raw/e0002.json"; raw_path.write_text(json.dumps(raw))
    inventory["resources"].append({"id": f"111111111111:us-east-1:elbv2.load-balancer:{arn}", "type": "elbv2.load-balancer",
                                   "native_id": arn, "name": "fixture", "account_id": "111111111111", "region": "us-east-1",
                                   "attributes": raw["LoadBalancers"][0], "evidence_ids": ["e0002"], "observations": {}})
    inventory["services"].append("elbv2"); manifest["services"].append("elbv2")
    manifest["calls"].append({"id": "e0002", "service": "elbv2", "operation": "describe-load-balancers", "status": "ok",
                              "raw_path": "raw/e0002.json", "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest()})
    manifest["coverage"] = {"ok": 2}
    (root / "inventory.json").write_text(json.dumps(inventory))
    (root / "collection-manifest.json").write_text(json.dumps(manifest))
    return inventory, manifest, arn


def waf_pack(root, inventory, arn, raw):
    """Supplementary wafv2 get-web-acl-for-resource record for the fixture ALB."""
    root.mkdir()
    (root / "waf.json").write_bytes(raw)
    record = {"id": "s0001", "service": "wafv2", "operation": "get-web-acl-for-resource", "region": "us-east-1",
              "arguments": ["--resource-arn", arn, "--output", "json"], "collection_method": "aws_cli",
              "return_code": 0, "status": "ok", "error_code": None, "collected_at": "2026-11-03T00:00:00Z",
              "raw_path": "waf.json", "sha256": hashlib.sha256(raw).hexdigest(), "raw_format": "json",
              "evidence_kind": "configuration", "response_complete": True,
              "resource_ids": [inventory["resources"][1]["id"]],
              "provenance": {"origin": "scoped account waf metadata", "redactions": []}}
    write_pack(root, {"schema_version": "1.0", "account_id": inventory["account_id"],
                      "regions": inventory["regions"], "records": [record]})


class WafAbsenceTests(unittest.TestCase):
    def test_documented_empty_waf_answer_is_a_judged_absence(self):
        """`wafv2 get-web-acl-for-resource` returns an empty body when no Web ACL is attached (exit 0).
        Field case 2026-10-30: this was recorded as validation_error and the check stayed UNKNOWN."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _, arn = alb_fixture(root / "base")
            waf_pack(root / "supplement", inventory, arn, b"{}")
            supplement.merge(root / "base", root / "supplement", root / "derived")
            merged, manifest, calls = review.verify_inputs(root / "derived")
            self.assertEqual(calls["s0001"]["empty_response_meaning"], "no_web_acl_associated")
            checks = review.evaluate(merged, manifest, calls)
            waf = [c for c in checks if c["rule_id"] == "SEC_WAF"]
            self.assertEqual(len(waf), 1)
            self.assertEqual(waf[0]["status"], "WARN")
            self.assertIn("s0001", waf[0]["evidence_ids"])
            self.assertEqual(waf[0]["confidence"], "configuration")

    def test_attached_web_acl_passes_and_other_blank_payloads_still_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _, arn = alb_fixture(root / "base")
            waf_pack(root / "supplement", inventory, arn, json.dumps({"WebACL": {"Name": "fixture-acl", "ARN": "arn:aws:wafv2:::x"}}).encode())
            supplement.merge(root / "base", root / "supplement", root / "derived")
            merged, manifest, calls = review.verify_inputs(root / "derived")
            waf = [c for c in review.evaluate(merged, manifest, calls) if c["rule_id"] == "SEC_WAF"]
            self.assertEqual(waf[0]["status"], "PASS")
        with tempfile.TemporaryDirectory() as temporary:
            # Blank stdout stays rejected everywhere, including for the allow-listed operation:
            # the collector must write the documented empty answer as '{}' so the payload is parseable.
            root = Path(temporary)
            inventory, _, arn = alb_fixture(root / "base")
            waf_pack(root / "supplement", inventory, arn, b"")
            with self.assertRaises(ValueError):
                supplement.merge(root / "base", root / "supplement", root / "derived")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, _ = fixture(root / "base")
            pack(root / "supplement", inventory, raw=b"")
            with self.assertRaises(ValueError):
                supplement.merge(root / "base", root / "supplement", root / "derived")

    def test_without_waf_evidence_internet_facing_alb_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, manifest, _ = alb_fixture(root / "base")
            calls = {c["id"]: c for c in manifest["calls"]}
            waf = [c for c in review.evaluate(inventory, manifest, calls) if c["rule_id"] == "SEC_WAF"]
            self.assertEqual([c["status"] for c in waf], ["UNKNOWN"])
