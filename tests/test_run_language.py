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
            # Only the numbers changed (an elapsed time after a re-render): the earlier translation with the new numbers.
            shifted = {"output_language": "ko", "units": {"a": {"source": "Run wall-clock: 24 h 3 min from the start", "target": None, "preserve": False}}}
            earlier = {"output_language": "ko", "units": {"b": {"source": "Run wall-clock: 23 h 39 min from the start", "target": "\uc2e4\ud589 \uc2dc\uac04 23\uc2dc\uac04 39\ubd84", "preserve": False}}}
            self.assertEqual(locale.reuse(shifted, earlier), 1)
            self.assertEqual(shifted["units"]["a"]["target"], "\uc2e4\ud589 \uc2dc\uac04 24\uc2dc\uac04 3\ubd84")

    def test_a_translated_report_redraws_its_diagrams_from_translated_labels(self):
        aws = load("aws_diagram_for_language", "skills/aws-architecture-review/scripts/aws_diagram.py")
        graph = {"title": "Data tier", "groups": [{"id": "vpc", "label": "VPC shop", "kind": "vpc", "parent": None, "resource_ids": []}],
                 "nodes": [{"id": "n1", "label": "Aurora writer orders", "detail": "the only instance", "service": "rds", "type": "rds.instance", "group": "vpc", "state": "retained", "resource_ids": []},
                           {"id": "n2", "label": "Aurora reader", "detail": "in another zone", "service": "rds", "type": "rds.instance", "group": "vpc", "state": "proposed", "resource_ids": []}],
                 "edges": [{"id": "e1", "source": "n1", "target": "n2", "label": "replication", "state": "proposed"}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "d"
            aws.render(graph, out, "diagram", "Data tier", "Arrows are configured paths.", None)
            inputs = json.loads((out / "diagram.render.json").read_text())
            self.assertEqual(inputs["graph"]["nodes"][1]["label"], "Aurora reader")
            import base64
            svg = (out / "diagram.aws.svg").read_text().strip()
            page = Path(tmp) / "06-assessment"; page.mkdir()
            (page / "assessment.html").write_text(f'<!doctype html><html lang="en"><body><p>Proposed reader.</p><figure data-render="{base64.b64encode((out / "diagram.render.json").read_bytes()).decode()}"><a>{svg}</a></figure></body></html>')
            plan = locale.prepare(page, {"output_language": "ko", "language_source": "explicit"})
            sources = {u["source"] for u in plan["units"].values()}
            self.assertIn("Aurora reader", sources); self.assertIn("in another zone", sources); self.assertIn("Legend", sources)
            self.assertNotIn("in another", sources, "labels are units whole, not as wrapped line fragments")
            terms = sorted(((t, r) for t, r in plan["glossary"]["terms"].items() if " " in t), key=lambda tr: -len(tr[0]))
            for u in plan["units"].values():
                if not u.get("target"):
                    text = u["source"]
                    for term, rendering in terms:
                        text = text.replace(term, rendering)
                    u["target"] = "\ubc88\uc5ed " + text if locale.NEEDS_WORDS.search(u["source"]) else None
                    u["preserve"] = u["target"] is None
            locale.apply(plan, Path(tmp) / "06-assessment-ko")
            html = (Path(tmp) / "06-assessment-ko/assessment.html").read_text()
            self.assertIn("\ubc88\uc5ed Aurora reader", html, "the redrawn drawing carries the translated label")
            self.assertIn('data-renderer="aws_diagram"', html)
            self.assertNotIn(">Aurora reader<", html)


if __name__ == "__main__":
    unittest.main()
