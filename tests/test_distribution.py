"""Distribution: release packaging, install.sh, the moda-skill CLI and the rendered entry skill. No network, no AWS."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from harness import run_python, run_sh, run_pwsh

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/moda_skill_cli.py"
PACKAGE = ROOT / "scripts/package_release.py"
INSTALL = ROOT / "scripts/install.sh"


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load("moda_skill_cli", "scripts/moda_skill_cli.py")


class EntrySkillRendering(unittest.TestCase):
    def test_each_harness_gets_its_invoke_prefix_and_paths(self):
        for harness, invoke, skills_dir in (("kiro", "/", ".kiro/skills"), ("claude", "/", ".claude/skills"), ("codex", "$", ".agents/skills")):
            text = cli.render_entry(harness, "project")
            self.assertNotIn("{{", text, harness)
            self.assertIn(f"{invoke}moda-skill start [opts]", text)
            self.assertIn(f'"{skills_dir}/moda-skill/scripts/moda.py" version', text)
            self.assertIn(f"--targets {harness}", text)
        user = cli.render_entry("kiro", "user")
        self.assertIn("~/.kiro/skills/moda-skill", user)

    def test_frontmatter_is_valid_for_each_client(self):
        for harness in cli.HARNESSES:
            text = cli.render_entry(harness, "project")
            header = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
            self.assertIn("name: moda-skill", header)
            description = re.search(r'^description: (.+)$', header, re.M).group(1)
            self.assertTrue(description.startswith('"') and description.endswith('"'),
                            "Kiro drops a skill whose unquoted description contains ': '")
            self.assertLessEqual(len(description), 1024)
            if harness == "claude":
                self.assertIn("user-invocable: true", header)
                self.assertIn("argument-hint:", header)
            else:
                self.assertNotIn("user-invocable", header)
        self.assertIn("$ARGUMENTS", cli.render_entry("kiro", "project"))
        self.assertNotIn("$ARGUMENTS", cli.render_entry("codex", "project"), "Codex has no placeholder substitution")

    def test_template_and_agents_metadata_exist(self):
        self.assertTrue((ROOT / "skills/moda-skill/SKILL.template.md").is_file())
        self.assertIn("unrendered", (ROOT / "skills/moda-skill/SKILL.md").read_text(), "source SKILL.md is a stand-in; config renders the real one")
        self.assertIn("$moda-skill", (ROOT / "skills/moda-skill/agents/openai.yaml").read_text())


class PackageInstallConfig(unittest.TestCase):
    def run_cli(self, home, *args, cwd=None):
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"))
        return run_python(str(CLI), *args, capture_output=True, text=True, env=env, cwd=cwd or home)

    def test_release_install_config_doctor_in_clean_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dist, home, project = tmp / "dist", tmp / "home", tmp / "project"
            home.mkdir(); project.mkdir()
            built = json.loads(run_python(str(PACKAGE), "--output", str(dist), capture_output=True, text=True, check=True).stdout)
            self.assertEqual(built["version"], (ROOT / "VERSION").read_text().strip())
            for name in ("SHA256SUMS", "VERSION", "install.sh", "install.ps1", f"moda-skill-{built['version']}.zip"):
                self.assertTrue((dist / name).is_file(), name)
            env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"))
            env.pop("MODA_SKILL_INSTALL_ROOT", None); env.pop("MODA_SKILL_BIN_DIR", None)
            run = run_sh(str(INSTALL), "--from", str(dist / f"moda-skill-{built['version']}.zip"), capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Checksum verified", run.stderr)
            launcher = home / ".local/bin/moda-skill"
            self.assertTrue(launcher.is_file() and os.access(launcher, os.X_OK))
            self.assertEqual((home / ".local/share/moda-skill/current").read_text().strip(), built["version"])
            version = run_sh(launcher, "version", capture_output=True, text=True, env=env)
            self.assertEqual(version.stdout.strip(), f"moda-skill {built['version']}")
            # Tampered zip is refused.
            bad = dist / "tampered"; bad.mkdir()
            shutil.copy2(dist / "SHA256SUMS", bad / "SHA256SUMS")
            (bad / f"moda-skill-{built['version']}.zip").write_bytes((dist / f"moda-skill-{built['version']}.zip").read_bytes() + b"x")
            refused = run_sh(str(INSTALL), "--from", str(bad / f"moda-skill-{built['version']}.zip"), capture_output=True, text=True, env=env)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("checksum mismatch", refused.stderr)
            # Configure every harness in a project from the installed launcher.
            for harness, skills_dir, mcp in (("kiro", ".kiro/skills", ".kiro/settings/mcp.json"), ("claude", ".claude/skills", ".mcp.json"), ("codex", ".agents/skills", ".codex/config.toml")):
                out = run_sh(launcher, "config", "--harness", harness, "--skip-companions", capture_output=True, text=True, env=env, cwd=project)
                self.assertEqual(out.returncode, 0, out.stderr)
                result = json.loads(out.stdout)
                self.assertEqual({a["action"] for a in result["actions"]}, {"install", "merge-mcp"})
                if harness == "kiro":
                    # Doctor checks only the clients configured here, not all three.
                    only = run_sh(launcher, "doctor", "--no-probe", capture_output=True, text=True, env=env, cwd=project)
                    self.assertEqual(list(json.loads(only.stdout.strip().splitlines()[-1])["entry_skill_installed"]), ["kiro"])
                    self.assertNotIn("claude", json.loads(only.stdout.strip().splitlines()[0])["next_actions"].__str__())
                for name in cli.STAGE_SKILLS + ("moda-skill",):
                    self.assertTrue((project / skills_dir / name / "SKILL.md").is_file(), f"{harness}:{name}")
                stamp = json.loads((project / skills_dir / "moda-skill/.moda-install.json").read_text())
                self.assertEqual(stamp["version"], built["version"])
                self.assertIn("knowledge-mcp.global.api.aws", (project / mcp).read_text())
            again = json.loads(run_sh(launcher, "config", "--harness", "kiro", "--skip-companions", capture_output=True, text=True, env=env, cwd=project).stdout)
            self.assertTrue(all(a["action"] == "unchanged" for a in again["actions"]), "second config is idempotent")
            doctor = run_sh(launcher, "doctor", "--no-probe", capture_output=True, text=True, env=env, cwd=project)
            last = json.loads(doctor.stdout.strip().splitlines()[-1])
            self.assertEqual(last["entry_skill_installed"], {"claude": True, "codex": True, "kiro": True})
            first = json.loads(doctor.stdout.strip().splitlines()[0])
            self.assertFalse(any("core skills missing" in a for a in first["next_actions"]))
            # Version management.
            listing = run_sh(launcher, "list", capture_output=True, text=True, env=env)
            self.assertIn(f"* {built['version']}", listing.stdout)
            rollback = run_sh(launcher, "rollback", capture_output=True, text=True, env=env)
            self.assertEqual(rollback.returncode, 1)
            self.assertIn("no earlier version", rollback.stderr)
            # Preflight: a configured project has all five stage skills; a bootstrap-only folder (zip or clone) does not.
            moda_py = project / ".kiro/skills/moda-skill/scripts/moda.py"
            pre = run_python(str(moda_py), "preflight", "--client", "kiro", capture_output=True, text=True, cwd=project)
            self.assertEqual(pre.returncode, 0, pre.stdout)
            self.assertIn("all five stage skills", pre.stdout)
            bootstrap = Path(tmp) / "bootstrap-only/.kiro/skills"
            shutil.copytree(project / ".kiro/skills/moda-skill", bootstrap / "moda-skill")
            pre = run_python(str(bootstrap / "moda-skill/scripts/moda.py"), "preflight", "--client", "kiro", "--json", capture_output=True, text=True)
            self.assertEqual(pre.returncode, 4)
            missing = json.loads(pre.stdout)
            self.assertEqual(missing["missing"], ["aws-cli-setup", "aws-mcp-setup", "aws-as-is-analysis", "aws-architecture-review", "aws-modernization-pathway"])
            self.assertEqual(missing["fix"], "moda-skill config --harness kiro")
            self.assertIn("moda-skill config --harness kiro", missing["message"])
            # Status helper in the configured project.
            status = run_python(str(moda_py), "status", capture_output=True, text=True, cwd=project)
            self.assertIn("no run yet", status.stdout)
            run_dir = project / "artifacts/shop/2026-11-09T00-00-00Z"
            (run_dir / "01-setup").mkdir(parents=True)
            (run_dir / "01-setup/access-check.json").write_text("{}"); (run_dir / "scope.json").write_text("{}")
            status = json.loads(run_python(str(project / ".kiro/skills/moda-skill/scripts/moda.py"), "status", "--json", capture_output=True, text=True, cwd=project).stdout)
            self.assertEqual(status["latest"]["next_stage"], "2 aws-mcp-setup")
            self.assertEqual(status["latest"]["stages"][0]["state"], "done")
            # Uninstall needs --yes and then removes launcher and versions.
            self.assertEqual(run_sh(launcher, "uninstall", capture_output=True, text=True, env=env).returncode, 1)
            self.assertEqual(run_sh(launcher, "uninstall", "--yes", capture_output=True, text=True, env=env).returncode, 0)
            self.assertFalse(launcher.exists())

    def test_install_refuses_unverified_local_zip_without_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); home = tmp / "home"; home.mkdir()
            dist = tmp / "dist"
            run_python(str(PACKAGE), "--output", str(dist), capture_output=True, check=True)
            (dist / "SHA256SUMS").unlink()
            env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"))
            zip_path = next(dist.glob("moda-skill-*.zip"))
            refused = run_sh(str(INSTALL), "--from", str(zip_path), capture_output=True, text=True, env=env)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--yes", refused.stderr)
            accepted = run_sh(str(INSTALL), "--from", str(zip_path), "--yes", capture_output=True, text=True, env=env)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)


def find_pwsh():
    for name in ("pwsh", "pwsh-preview"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in ("/usr/local/microsoft/powershell/7-preview/pwsh", "/usr/local/microsoft/powershell/7/pwsh"):
        if Path(candidate).is_file():
            return candidate
    return None


@unittest.skipUnless(find_pwsh(), "PowerShell 7 not available")
class WindowsInstaller(unittest.TestCase):
    """install.ps1 under PowerShell 7 on this OS: download/verify/unpack/launcher logic is OS-neutral; the
    administrator check and the user PATH registry edit only run on Windows."""

    def test_install_ps1_verifies_unpacks_and_writes_launchers(self):
        pwsh = find_pwsh()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); dist = tmp / "dist"; root = tmp / "moda-skill"; project = tmp / "project"; project.mkdir()
            run_python(str(PACKAGE), "--output", str(dist), capture_output=True, check=True)
            self.assertIn("install.ps1", (dist / "SHA256SUMS").read_text())
            zip_path = next(dist.glob("moda-skill-*.zip"))
            env = dict(os.environ, MODA_SKILL_INSTALL_ROOT=str(root), LOCALAPPDATA=str(tmp))
            run = run_pwsh(pwsh, "-NoProfile", "-File", str(ROOT / "scripts/install.ps1"), "-From", str(zip_path), capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Checksum verified", run.stdout)
            version = (ROOT / "VERSION").read_text().strip()
            self.assertEqual((root / "current").read_text().strip(), version)
            for launcher in ("moda-skill.cmd", "moda-skill.ps1"):
                self.assertTrue((root / "bin" / launcher).is_file(), launcher)
            cmd = (root / "bin/moda-skill.cmd").read_text()
            self.assertIn("set /p MODA_VERSION=<", cmd)
            self.assertTrue(cmd.endswith("exit /b %ERRORLEVEL%\r\n") or "exit /b %ERRORLEVEL%" in cmd)
            launched = run_pwsh(pwsh, "-NoProfile", "-File", str(root / "bin/moda-skill.ps1"), "config", "--harness", "codex", "--skip-companions",
                                      capture_output=True, text=True, env=env, cwd=project)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertTrue((project / ".agents/skills/moda-skill/SKILL.md").is_file())
            self.assertIn("$moda-skill", (project / ".agents/skills/moda-skill/SKILL.md").read_text())
            bad = tmp / "bad"; bad.mkdir()
            shutil.copy2(dist / "SHA256SUMS", bad / "SHA256SUMS")
            (bad / zip_path.name).write_bytes(zip_path.read_bytes() + b"x")
            refused = run_pwsh(pwsh, "-NoProfile", "-File", str(ROOT / "scripts/install.ps1"), "-From", str(bad / zip_path.name),
                                     capture_output=True, text=True, env=dict(env, MODA_SKILL_INSTALL_ROOT=str(tmp / "other")))
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("checksum mismatch", refused.stderr)
            (bad / "SHA256SUMS").unlink()
            refused = run_pwsh(pwsh, "-NoProfile", "-File", str(ROOT / "scripts/install.ps1"), "-From", str(bad / zip_path.name),
                                     capture_output=True, text=True, env=dict(env, MODA_SKILL_INSTALL_ROOT=str(tmp / "other")))
            self.assertIn("-Yes", refused.stderr)


if __name__ == "__main__":
    unittest.main()
