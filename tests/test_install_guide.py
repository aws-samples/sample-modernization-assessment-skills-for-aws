"""Guide-driven installation: manifest parity with the scripted installer and a simulated manual install."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from harness import run_python

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("setup_toolkit", "skills/aws-mcp-setup/scripts/setup_toolkit.py")
MANIFEST = json.loads((ROOT / "skills/aws-skills-install/assets/install-manifest.json").read_text(encoding="utf-8"))


class ManifestTests(unittest.TestCase):
    def test_manifest_is_current_and_bootstrap_copies_match(self):
        result = run_python(str(ROOT / "skills/aws-skills-install/scripts/build_manifest.py"), "--check", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manifest_lists_every_file_with_matching_hashes(self):
        for skill in MANIFEST["skills"]:
            source = ROOT / skill["source"]
            self.assertEqual(skill["digest"], setup.digest_tree(source), skill["name"])
            self.assertEqual(skill["stamp"]["sha256"], skill["digest"])
            on_disk = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()
                       and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != setup.STAMP}
            self.assertEqual({f["path"] for f in skill["files"]}, on_disk, skill["name"])
            for item in skill["files"]:
                self.assertEqual(item["sha256"], hashlib.sha256((source / item["path"]).read_bytes()).hexdigest(), item["path"])
        self.assertEqual(MANIFEST["mcp"]["url"], setup.MCP_URL)
        self.assertEqual(MANIFEST["companions"], setup.dependencies()["companions"])
        self.assertIn("aws-skills-install", {p.name for p in (ROOT / ".kiro/skills").iterdir()})

    def test_manual_install_from_manifest_is_recognised_as_managed(self):
        """Copying the listed files and writing the stamp, as the guide instructs, yields an installation the toolkit sees as unchanged."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "proj"
            skills_dir = project / MANIFEST["paths"]["kiro"]["project_skills"]
            for skill in MANIFEST["skills"]:
                dest = skills_dir / skill["name"]
                for item in skill["files"]:
                    target = dest / item["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / skill["source"] / item["path"]).read_bytes())
                (dest / MANIFEST["stamp_file"]).write_text(json.dumps(dict(skill["stamp"], installed_at="2026-10-20T00:00:00+00:00")))
                self.assertEqual(setup.copy_plan(ROOT / skill["source"], dest)["action"], "unchanged", skill["name"])
            # MCP snippet from the manifest merges without conflict and preserves other servers
            config = project / MANIFEST["paths"]["kiro"]["project_mcp"]
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
            plan = setup.config_plan("kiro", config)
            merged = json.loads(plan["content"])
            self.assertEqual(merged["mcpServers"]["aws-knowledge"], MANIFEST["mcp"]["kiro"]["mcpServers"]["aws-knowledge"])
            self.assertIn("other", merged["mcpServers"])
            # a hand-edited installed file is preserved, not overwritten, by a managed update
            edited = skills_dir / "aws-cli-setup/SKILL.md"
            edited.write_text(edited.read_text() + "\nlocal note\n")
            with self.assertRaises(ValueError):
                setup.copy_plan(ROOT / "skills/aws-cli-setup", skills_dir / "aws-cli-setup", update=True)


if __name__ == "__main__":
    unittest.main()


class IamPolicyTests(unittest.TestCase):
    def test_sample_policy_scopes_send_command_and_denies_data_reads(self):
        policy = json.loads((ROOT / "docs/iam/assessment-policy.json").read_text(encoding="utf-8"))
        by_sid = {s["Sid"]: s for s in policy["Statement"]}
        send = by_sid["HostInspectionRunShellScriptOnNamedInstances"]
        self.assertEqual(send["Action"], ["ssm:SendCommand"])
        self.assertNotIn("*", send["Resource"])  # remote execution is never granted on every managed node
        self.assertIn("arn:aws:ssm:*:*:document/AWS-RunShellScript", send["Resource"])
        self.assertTrue(any(r.startswith("arn:aws:ec2:") and ":instance/" in r for r in send["Resource"]))
        for statement in policy["Statement"]:
            if statement["Effect"] == "Allow" and statement["Resource"] == "*":
                self.assertFalse(any(a.endswith("SendCommand") for a in statement["Action"]), statement["Sid"])
        self.assertIn("s3:GetObject", by_sid["DenyDataContentReads"]["Action"])
