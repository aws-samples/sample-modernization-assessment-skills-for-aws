"""Run metadata (LLM model, inference time, tokens): recorder, validation and report rendering. No AWS access."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_workflow import fixture  # noqa: E402
from harness import run_python


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kit = load("report_kit", "skills/aws-architecture-review/scripts/report_kit.py")
RECORDER = ROOT / "skills/aws-mcp-setup/scripts/run_metadata.py"


def run(*args):
    return run_python(str(RECORDER), *args, capture_output=True, text=True)


class RecorderTests(unittest.TestCase):
    def test_init_add_import_show_and_totals(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run-metadata.json"
            self.assertEqual(run("init", "--output", str(path), "--model", "model-x", "--client", "kiro-cli").returncode, 0)
            self.assertEqual(run("init", "--output", str(path)).returncode, 1, "existing file is not overwritten without --force")
            self.assertEqual(run("add", "--file", str(path), "--label", "s4", "--inference-seconds", "90", "--input-tokens", "1000", "--output-tokens", "200", "--requests", "3").returncode, 0)
            self.assertEqual(run("add", "--file", str(path), "--label", "empty").returncode, 1, "empty entries need --allow-empty")
            self.assertEqual(run("add", "--file", str(path), "--label", "unknown", "--allow-empty").returncode, 0)
            self.assertEqual(run("add", "--file", str(path), "--label", "bad", "--input-tokens", "-1").returncode, 1)
            stats = Path(temporary) / "stats.json"
            stats.write_text(json.dumps([{"timestamp": "2026-10-20T07:00:00Z", "duration_ms": 1500, "input_tokens": 100, "output_tokens": 50},
                                         {"timestamp": "2026-10-20T09:00:00Z", "duration_ms": 500, "input_tokens": 10, "output_tokens": 5}]))
            imported = run("import-kiro-stats", "--file", str(path), "--stats", str(stats), "--label", "kiro", "--since", "2026-10-20T08:00:00Z")
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(json.loads(imported.stdout)["requests"], 1)
            meta = kit.load_run_metadata(path)
            self.assertEqual(meta["totals"], {"inference_seconds": 90.5, "input_tokens": 1010, "output_tokens": 205, "requests": 4, "total_tokens": 1215})
            self.assertEqual(meta["models_seen"], ["model-x"])
            self.assertEqual(meta["partial_fields"], ["inference_seconds", "input_tokens", "output_tokens", "requests"])
            shown = run("show", "--file", str(path))
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("1215", shown.stdout)

    def test_loader_rejects_invalid_and_keeps_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "m.json"
            path.write_text(json.dumps({"schema_version": "1.0", "entries": []}))
            meta = kit.load_run_metadata(path)
            self.assertIsNone(meta["totals"]["total_tokens"])
            self.assertIn(("LLM model", "not recorded"), kit.run_metadata_rows(meta))
            for bad in ({"schema_version": "2.0"}, {"schema_version": "1.0", "model_id": 5},
                        {"schema_version": "1.0", "entries": [{"label": "x", "input_tokens": -3}]},
                        {"schema_version": "1.0", "entries": [{"label": "x", "input_tokens": 1.5}]},
                        {"schema_version": "1.0", "entries": [{"input_tokens": 1}]}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    kit.load_run_metadata(path)
            self.assertIsNone(kit.load_run_metadata(None))
            self.assertIn("not recorded", kit.run_metadata_html(None))
            self.assertEqual(kit.fmt_seconds(3725), "1 h 2 min")
            self.assertEqual(kit.fmt_seconds(None), "not recorded")


class ReportIntegrationTests(unittest.TestCase):
    def test_reports_show_provenance_or_not_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / "03-as-is")
            meta = root / "run-metadata.json"
            run("init", "--output", str(meta), "--model", "model-x", "--client", "test-client")
            run("add", "--file", str(meta), "--label", "all stages", "--inference-seconds", "3725", "--input-tokens", "12345", "--output-tokens", "678", "--requests", "9")
            review = run_python(str(ROOT / "skills/aws-architecture-review/scripts/review.py"), "--as-is", str(root / "03-as-is"),
                                     "--output", str(root / "04"), "--run-metadata", str(meta), capture_output=True, text=True)
            self.assertEqual(review.returncode, 0, review.stderr)
            html = (root / "04/review.html").read_text()
            md = (root / "04/review.md").read_text()
            for page in (html, md):
                self.assertIn("model-x", page)
                self.assertIn("1 h 2 min", page)
                self.assertIn("13,023", page)
            self.assertIn('id="provenance"', html)
            self.assertEqual(json.loads((root / "04/findings.json").read_text())["run_metadata"]["totals"]["total_tokens"], 13023)
            recommend = run_python(str(ROOT / "skills/aws-modernization-pathway/scripts/recommend.py"), "--inventory", str(root / "03-as-is/inventory.json"),
                                        "--findings", str(root / "04/findings.json"), "--workload", "fixture", "--output", str(root / "05"), "--run-metadata", str(meta),
                                       capture_output=True, text=True)
            self.assertEqual(recommend.returncode, 0, recommend.stderr)
            self.assertIn("13,023", (root / "05/recommendations.html").read_text())
            self.assertEqual(json.loads((root / "05/pathways.json").read_text())["run_metadata"]["model_id"], "model-x")
            plain = run_python(str(ROOT / "skills/aws-architecture-review/scripts/review.py"), "--as-is", str(root / "03-as-is"),
                                    "--output", str(root / "04-plain"), capture_output=True, text=True)
            self.assertEqual(plain.returncode, 0, plain.stderr)
            self.assertIn("model not recorded", (root / "04-plain/review.html").read_text())
            self.assertIsNone(json.loads((root / "04-plain/findings.json").read_text())["run_metadata"])


if __name__ == "__main__":
    unittest.main()


class ClientImportTests(unittest.TestCase):
    """Claude Code transcript and Codex rollout importers (formats as documented publicly; fixtures are synthetic)."""

    def test_claude_transcript_dedupes_and_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            usage = {"input_tokens": 100, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300, "output_tokens": 40}
            lines = [{"type": "user", "uuid": "u1", "timestamp": "2026-10-20T07:00:00Z", "message": {"content": "secret prompt text"}},
                     {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-20T07:00:05Z", "message": {"id": "m1", "model": "model-a", "usage": usage}},
                     {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-20T07:00:05Z", "message": {"id": "m1", "model": "model-a", "usage": usage}},
                     {"type": "assistant", "uuid": "a2", "timestamp": "2026-10-20T07:30:00Z", "message": {"id": "m2", "model": "model-a", "usage": {"input_tokens": 1, "output_tokens": 2}}},
                     {"type": "assistant", "uuid": "a3", "timestamp": "2026-10-20T09:00:00Z", "message": {"id": "m3", "model": "model-b", "usage": usage}}]
            transcript = root / "t.jsonl"
            transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
            meta = root / "m.json"
            run("init", "--output", str(meta), "--client", "claude-code")
            result = run("import-claude-transcript", "--file", str(meta), "--transcript", str(transcript), "--label", "s", "--since", "2026-10-20T07:00:00Z", "--until", "2026-10-20T08:00:00Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            out = json.loads(result.stdout)
            self.assertEqual((out["requests"], out["models"]), (2, ["model-a"]))
            loaded = kit.load_run_metadata(meta)
            entry = loaded["entries"][0]
            self.assertEqual((entry["input_tokens"], entry["output_tokens"], entry["model_id"]), (5401, 42, "model-a"))
            self.assertEqual(entry["breakdown"]["cache_read_input"], 5000)
            self.assertNotIn("secret prompt text", meta.read_text())
            self.assertIn("cache_read_input 5,000", kit.run_metadata_html(loaded))
            self.assertEqual(run("import-claude-transcript", "--file", str(meta), "--transcript", str(transcript), "--label", "x", "--since", "2027-01-01T00:00:00Z").returncode, 1)

    def test_codex_rollout_uses_cumulative_delta(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def snap(stamp, inp, cached, out, reasoning):
                return {"timestamp": stamp, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}}}}
            lines = [{"timestamp": "2026-10-20T06:50:00Z", "type": "session_meta", "payload": {"id": "s"}},
                     snap("2026-10-20T06:55:00Z", 1000, 900, 50, 10),
                     {"timestamp": "2026-10-20T07:01:00Z", "type": "turn_context", "payload": {"model": "model-c"}},
                     snap("2026-10-20T07:02:00Z", 64284, 61956, 430, 266), snap("2026-10-20T07:02:01Z", 64284, 61956, 430, 266),
                     snap("2026-10-20T07:05:00Z", 124314, 120960, 552, 384), snap("2026-10-20T09:00:00Z", 999999, 0, 9999, 0)]
            rollout = root / "rollout.jsonl"
            rollout.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            meta = root / "m.json"
            run("init", "--output", str(meta), "--client", "codex")
            result = run("import-codex-rollout", "--file", str(meta), "--rollout", str(rollout), "--label", "s", "--since", "2026-10-20T07:00:00Z", "--until", "2026-10-20T08:00:00Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            out = json.loads(result.stdout)
            self.assertEqual((out["requests"], out["input_tokens"], out["output_tokens"], out["models"]), (2, 123314, 502, ["model-c"]))
            entry = kit.load_run_metadata(meta)["entries"][0]
            self.assertEqual(entry["breakdown"]["cached_input"], 120060)
            self.assertEqual(entry["breakdown"]["reasoning_output"], 374)
            self.assertIsNone(entry["inference_seconds"])
            self.assertEqual(run("import-codex-rollout", "--file", str(meta), "--rollout", str(rollout), "--label", "x", "--since", "2027-01-01T00:00:00Z").returncode, 1)
