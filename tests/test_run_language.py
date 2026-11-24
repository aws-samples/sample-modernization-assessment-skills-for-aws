"""One language per run: canonical artifacts are English, a non-English report is translated once at the end."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import run_python  # noqa: E402
from test_specialists import load, build_run, good_advice, write_review  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
kit = load("kit_for_language", "skills/aws-modernization-pathway/scripts/report_kit.py")
advice = load("advice_for_language", "skills/aws-modernization-pathway/scripts/advice.py")
report = load("report_for_language", "skills/aws-modernization-pathway/scripts/assessment_report.py")
review = load("review_for_language", "skills/aws-architecture-review/scripts/review.py")
diagram = load("diagram_for_language", "skills/aws-architecture-review/scripts/diagram.py")
locale = load("locale_for_language", "skills/aws-mcp-setup/scripts/localize_artifacts.py")
moda = load("moda_for_language", "skills/moda-skill/scripts/moda.py")
KO = "\ub9ac\ub354\ub97c \ucd94\uac00\ud558\ub294 \uac83\uc744 \uad8c\ud569\ub2c8\ub2e4"  # a Korean sentence


class RunLanguageTests(unittest.TestCase):
    def test_non_english_prose_is_told_apart_from_a_foreign_name(self):
        self.assertTrue(kit.non_english_prose(KO))
        self.assertTrue(kit.non_english_prose(f"Aurora {KO}"))
        self.assertFalse(kit.non_english_prose("Add a reader to cluster \uc8fc\ubb38-db in us-east-1a."), "a resource name in another script is allowed")
        self.assertFalse(kit.non_english_prose("Add a reader in us-east-1a."))

    def test_every_agent_written_input_refuses_another_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = build_run(Path(tmp))
            adv = good_advice()
            adv["summary"] = KO + ". " + KO + "."
            write_review(run, "database", adv)
            self.assertIn("not in English", " ".join(advice.validate(advice.Context(run), "database")["errors"]))
            issues = Path(tmp) / "key-issues.json"
            issues.write_text(json.dumps({"issues": [{"title": KO, "problem": "p", "impact": "i", "urgency": "now", "finding_ids": ["f0001"],
                                                      "action": "Adding a reader would keep the service up. A smaller class is the alternative."}]}))
            with self.assertRaisesRegex(ValueError, "not in English"):
                report.load_key_issues(issues, {"f0001"})
        self.assertTrue(review.manual_language_errors([{"id": "m0001", "observation": KO}]))
        self.assertEqual(review.manual_language_errors([{"id": "m0001", "observation": "One writer and no reader."}]), [])
        graph = {"groups": [], "nodes": [{"id": "n1", "label": KO}], "edges": []}
        with self.assertRaisesRegex(ValueError, "not in English"):
            diagram.validate_graph(graph)

    def test_status_names_the_deliverable_of_the_run_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "artifacts/w/20260101T000000Z"
            for rel in ("01-setup/access-check.json", "02-toolkit/toolkit-check.json", "03-as-is/inventory.json", "03-as-is-enriched/inventory.json", "04-review/findings.json",
                        "04-review/diagrams/as-is.aws.svg", "05-modernization/pathways.json", "06-assessment/assessment.html"):
                (run / rel).parent.mkdir(parents=True, exist_ok=True); (run / rel).write_text("{}")
            (run / "scope.json").write_text(json.dumps({"output_language": "ko"}))
            info = moda.inspect(run)
            self.assertEqual((info["language"], info["deliverable"]), ("ko", None))
            self.assertIn("not translated yet", moda.table(info))
            (run / "06-assessment-ko").mkdir(); (run / "06-assessment-ko/assessment.html").write_text("<html></html>")
            info = moda.inspect(run)
            self.assertEqual(info["deliverable"], "06-assessment-ko/assessment.html")
            (run / "scope.json").write_text(json.dumps({"output_language": "en"}))
            self.assertEqual(moda.inspect(run)["deliverable"], "06-assessment/assessment.html")

    def test_scope_defaults_to_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "scope.json"
            r = run_python(str(ROOT / "skills/aws-cli-setup/scripts/scope.py"), "write", "--account", "111111111111", "--regions", "us-east-1",
                           "--workload-name", "w", "--type", "account", "--profile", "p", "--confirmed-by", "defaults", "--output", str(out), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text())["output_language"], "en")

    def test_chunks_merge_and_check_list_every_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "06-assessment"; root.mkdir()
            (root / "assessment.md").write_text("# Key issues\n\nThe cluster has 1 writer and no reader.\n\nFound 2 issues in f0001.\n\nshopnow-db-1\n")
            plan = locale.prepare(root, {"output_language": "ko", "language_source": "explicit"})
            folder = Path(tmp) / "chunks"
            result = locale.chunks(plan, folder)
            self.assertEqual(result["units"], 2, "the glossary label is pre-filled and the identifier-only unit is kept")
            self.assertTrue((folder / "BRIEF.md").is_file()); self.assertTrue((folder / "glossary.json").is_file())
            src = json.loads((folder / "chunk-00.src.json").read_text())
            targets = {k: ("\ud074\ub7ec\uc2a4\ud130\uc5d0 \ub77c\uc774\ud130 1\uac1c\uac00 \uc788\uace0 \ub9ac\ub354\ub294 \uc5c6\uc2b5\ub2c8\ub2e4." if "writer" in v else "f0001\uc5d0\uc11c 3\uac1c \ubb38\uc81c") for k, v in src.items()}
            (folder / "chunk-00.ko.json").write_text(json.dumps(targets, ensure_ascii=False))
            merged = locale.merge(plan, folder)
            self.assertEqual((merged["merged"], merged["missing"]), (2, []))
            problems = locale.check(plan)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("missing {'2': 1}, added {'3': 1}", problems[0]["error"])
            key = problems[0]["unit"]
            plan["units"][key]["target"] = "f0001\uc5d0\uc11c 2\uac1c \ubb38\uc81c"
            self.assertEqual(locale.check(plan), [])
            out = locale.apply(plan, Path(tmp) / "06-assessment-ko")
            self.assertEqual(out["output_language"], "ko")
            again = locale.prepare(root, {"output_language": "ko", "language_source": "explicit"})
            self.assertEqual(locale.reuse(again, plan), 3, "a re-render reuses earlier translations and preserved units")


if __name__ == "__main__":
    unittest.main()
