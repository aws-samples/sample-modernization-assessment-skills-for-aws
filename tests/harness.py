"""The one place where the test suite spawns processes, and the fake process results the collector tests feed back.

Every test runs this repository's own programs: Python scripts under skills/ and scripts/, the sh and PowerShell
installers, and the launcher those installers create, always with arguments the test itself wrote. Each call names
its program with a literal (`python3`, `sh`, `pwsh`); the interpreter that runs the tests is put first on PATH through a
shim directory, so `python3` resolves to the same interpreter without ever passing sys.executable as a program."""
import os
from pathlib import Path
import subprocess  # nosec B404 - list-form invocations of our own scripts, never a shell
import sys
import tempfile
from types import SimpleNamespace

_SHIM = None


def interpreter_dir():
    """A directory whose `python3` is the interpreter running the tests."""
    global _SHIM
    if _SHIM is None:
        shim = Path(tempfile.mkdtemp(prefix="moda-test-python-"))
        if os.name == "nt":
            (shim / "python3.cmd").write_text(f'@"{sys.executable}" %*\r\n', encoding="utf-8")
        else:
            os.symlink(sys.executable, shim / "python3")
        _SHIM = shim
    return _SHIM


def with_path(kwargs, *directories):
    """Return kwargs with an env whose PATH starts with the given directories (keeps a caller-supplied env)."""
    env = dict(kwargs.pop("env", None) or os.environ)
    env["PATH"] = os.pathsep.join([*(str(d) for d in directories), env.get("PATH", "")])
    return dict(kwargs, env=env)


def run_python(*argv, **kwargs):
    """Run a repository script with the interpreter that runs the tests."""
    return subprocess.run(["python3", "-B", *(str(a) for a in argv)], **with_path(kwargs, interpreter_dir()))  # nosec B603 B607


def run_sh(*argv, **kwargs):
    """Run a POSIX shell script (the installer or the launcher it writes) with the test interpreter first on PATH."""
    return subprocess.run(["sh", *(str(a) for a in argv)], **with_path(kwargs, interpreter_dir()))  # nosec B603 B607


def run_pwsh(pwsh_path, *argv, **kwargs):
    """Run PowerShell 7 as `pwsh`, through a shim directory that points at the binary the test located (it may be
    installed as pwsh-preview or under /usr/local/microsoft/powershell)."""
    shim = interpreter_dir()
    link = shim / ("pwsh.cmd" if os.name == "nt" else "pwsh")
    if not link.exists():
        if os.name == "nt":
            link.write_text(f'@"{pwsh_path}" %*\r\n', encoding="utf-8")
        else:
            os.symlink(str(pwsh_path), link)
    return subprocess.run(["pwsh", *(str(a) for a in argv)], **with_path(kwargs, shim))  # nosec B603 B607


def completed(returncode, stdout="", stderr=""):
    """What a fake `aws` returns: the three attributes the collectors read from a finished process."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
