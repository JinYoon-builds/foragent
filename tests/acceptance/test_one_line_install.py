"""Acceptance test for one-line-install (#1475): one installer, a thin npm wrapper, README and clean-machine CI.

Every run is offline: uv, curl, wget, claude and codex are stubs on a narrowed PATH,
and HOME is a fresh temporary directory.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install.sh"
REPO = "JinYoon-builds/foragent"

STUB = r"""#!/bin/sh
printf '%s' "$(basename "$0")" >> "$FA_STUB_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$FA_STUB_LOG"; done
printf '\n' >> "$FA_STUB_LOG"
"""

# A fake uv: `tool install` puts a fake fa next to it; `tool dir --bin` answers.
UV = STUB + r"""
bin="$HOME/.local/bin"
case "$1 $2" in
  "tool install") mkdir -p "$bin"; printf '#!/bin/sh\necho fa stub\n' > "$bin/fa"; chmod +x "$bin/fa" ;;
  "tool dir") echo "$bin" ;;
  "--version "*|"--version") echo "uv 0.9.0" ;;
esac
exit 0
"""

# A fake downloader: serves this repository's install.sh for a foragent install.sh URL,
# and otherwise a script that installs the fake uv the way uv's own installer would.
FETCH = STUB + r"""
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--output|-O) out="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
case "$url" in
  *foragent*install.sh) body="$(cat "$FA_REPO_INSTALLER")" ;;
  *) body="mkdir -p \"\$HOME/.local/bin\"; cp \"$FA_UV_STUB\" \"\$HOME/.local/bin/uv\"; chmod +x \"\$HOME/.local/bin/uv\"" ;;
esac
if [ -n "$out" ] && [ "$out" != "-" ]; then printf '%s\n' "$body" > "$out"; else printf '%s\n' "$body"; fi
exit 0
"""


class Machine:
    """A clean machine: fake HOME, stub tools, no network, no terminal to ask on."""

    def __init__(self, tmp: Path, *, uv: bool, codex: bool = False):
        self.home = tmp / "home"
        self.home.mkdir()
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        self.log = tmp / "calls.log"
        self.log.touch()
        self.uv_stub = tmp / "uv-stub"
        self.uv_stub.write_text(UV)
        self.uv_stub.chmod(0o755)
        tools = {"curl": FETCH, "wget": FETCH, "claude": STUB + "exit 0\n"}
        if codex:
            tools["codex"] = STUB + "exit 0\n"
        if uv:
            tools["uv"] = UV
        for name, body in tools.items():
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.stubs}:{self.home / '.local/bin'}:/usr/bin:/bin",
            "FA_STUB_LOG": str(self.log),
            "FA_UV_STUB": str(self.uv_stub),
            "FA_REPO_INSTALLER": str(INSTALLER),
            "LANG": "C.UTF-8",
        }

    def run(self, argv):
        result = subprocess.run(argv, env=self.env, cwd=self.home, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=120, start_new_session=True)
        return result, result.stdout + result.stderr

    def calls(self):
        return [line.split() for line in self.log.read_text().splitlines() if line.strip()]

    def files(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in sorted(self.home.rglob("*")) if p.is_file() and not p.is_symlink()}


def posix_sh():
    return shutil.which("dash") or "/bin/sh"


def installs_foragent(calls):
    return [c for c in calls if c[:3] == ["uv", "tool", "install"] and any("foragent" in a for a in c[3:])]


def fetched(calls):
    return [c for c in calls if c[0] in ("curl", "wget")]


def test_installer_asks_first_then_sets_up_uv_and_foragent_on_a_clean_machine(tmp_path):
    assert INSTALLER.is_file(), "Ship install.sh at the repository root"
    syntax = subprocess.run([posix_sh(), "-n", str(INSTALLER)], capture_output=True, text=True)
    assert syntax.returncode == 0, f"install.sh must parse as POSIX sh: {syntax.stderr}"

    machine = Machine(tmp_path, uv=False, codex=True)
    _, output = machine.run([posix_sh(), str(INSTALLER)])
    calls = machine.calls()
    assert not fetched(calls) and not installs_foragent(calls), (
        f"With no terminal to ask on and no --yes, install nothing: {calls}")
    assert not (machine.home / ".local/bin").exists(), "Nothing is written to HOME without consent"
    assert "--yes" in output, "Print how to run the install without a prompt"

    result, output = machine.run([posix_sh(), str(INSTALLER), "--yes"])
    assert result.returncode == 0, output
    calls = machine.calls()
    assert fetched(calls), f"With no uv on the machine the installer fetches uv: {calls}"
    assert (machine.home / ".local/bin/uv").is_file(), "uv ends up installed in the person's HOME"
    assert len(installs_foragent(calls)) == 1, f"Install Foragent exactly once with uv: {calls}"
    assert [c for c in calls if c[:2] == ["claude", "plugin"]], (
        f"Register the Foragent Claude Code plugin with the claude plugin commands: {calls}")
    assert "/fa" in output, "End by telling the person to type /fa in Claude Code"
    assert re.search(r"codex[^\n]*experimental|experimental[^\n]*codex", output, re.I), (
        "With Codex present, say that Codex PM is experimental")


def test_installer_is_idempotent_and_reuses_an_existing_uv(tmp_path):
    assert INSTALLER.is_file(), "Ship install.sh at the repository root"
    machine = Machine(tmp_path, uv=True)
    first, output = machine.run([posix_sh(), str(INSTALLER), "--yes"])
    assert first.returncode == 0, output
    assert not re.search(r"codex[^\n]*experimental|experimental[^\n]*codex", output, re.I), (
        "Only mention Codex PM when Codex is on the machine")
    after_first = machine.files()
    second, output = machine.run([posix_sh(), str(INSTALLER), "--yes"])
    assert second.returncode == 0, output
    calls = machine.calls()
    assert not fetched(calls), f"An existing uv is reused, nothing is downloaded: {calls}"
    assert len(installs_foragent(calls)) == 2, f"Each run installs Foragent once: {calls}"
    assert machine.files() == after_first, "A second run leaves the same files in HOME (no duplicated lines)"


def foragent_npm_package():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "*package.json"],
                             capture_output=True, text=True, check=True).stdout.split()
    for relative in tracked:
        manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        if manifest.get("name") == "foragent":
            return ROOT / relative, manifest
    pytest.fail("Ship a thin npm package named foragent in this repository")


def test_readme_npx_and_curl_one_liners_take_the_installer_path(tmp_path):
    assert INSTALLER.is_file(), "Ship install.sh at the repository root"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    script = INSTALLER.read_text(encoding="utf-8")
    pattern = (r"curl[^\n|]*https://[^\s|]*" + re.escape(REPO) + r"[^\s|]*/install\.sh[^\n|]*\|\s*(?:ba)?sh\b[^\n`]*")
    lines = [m.group(0).strip() for m in re.finditer(pattern, readme)]
    assert lines, "README shows a curl one-liner that pipes this repository's install.sh to sh"
    assert any(line in script for line in lines), "README's one-liner is the one install.sh documents"
    assert re.search(r"\bnpx foragent\b", readme), "README shows `npx foragent`"

    path, manifest = foragent_npm_package()
    bins = manifest.get("bin")
    bin_path = bins if isinstance(bins, str) else (bins or {}).get("foragent")
    assert bin_path, "`npx foragent` needs a bin named foragent"
    assert not manifest.get("dependencies"), "Keep the npm package thin: no runtime dependencies"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine")

    for name in ("sh", "npm"):
        (tmp_path / name).mkdir()
    shell, npm = Machine(tmp_path / "sh", uv=True), Machine(tmp_path / "npm", uv=True)
    direct, out = shell.run([posix_sh(), str(INSTALLER), "--yes"])
    assert direct.returncode == 0, out
    wrapped, out = npm.run([node, str(path.parent / bin_path), "--yes"])
    assert wrapped.returncode == 0, out
    assert installs_foragent(npm.calls()), f"npx foragent installs Foragent: {npm.calls()}"
    assert sorted(npm.calls()) == sorted(shell.calls()), "npx foragent takes the same path as install.sh"


def clean_install_jobs():
    jobs = []
    for workflow in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        triggers = document.get("on", document.get(True))
        for name, job in (document.get("jobs") or {}).items():
            runs = [str(step.get("run", "")) for step in job.get("steps", [])]
            if any("install.sh" in run for run in runs):
                jobs.append((workflow.name, triggers, name, job, runs))
    return jobs


def test_ci_installs_on_clean_macos_and_ubuntu_then_runs_fa_doctor():
    jobs = clean_install_jobs()
    assert jobs, "A workflow job runs install.sh"
    runners = set()
    for workflow, triggers, name, job, runs in jobs:
        trigger_names = triggers if isinstance(triggers, (list, str)) else list(triggers or {})
        assert "pull_request" in trigger_names, f"{workflow}: run the clean install on pull requests"
        install = next(i for i, run in enumerate(runs) if "install.sh" in run)
        assert not re.search(r"https?://\S*install\.sh", runs[install]), (
            f"{workflow}/{name}: run the checked-out install.sh, not main's copy from a URL, "
            "so the PR that adds or changes it tests its own version")
        doctor = [i for i, run in enumerate(runs) if re.search(r"\bfa doctor\b", run)]
        assert doctor and doctor[-1] > install, f"{workflow}/{name}: run `fa doctor` after the install"
        uses = " ".join(str(step.get("uses", "")) for step in job.get("steps", []))
        assert "setup-uv" not in uses, f"{workflow}/{name}: a clean machine has no uv until install.sh sets it up"
        assert "secrets." not in yaml.safe_dump(job), f"{workflow}/{name}: no logins or secrets in the clean install"
        matrix = json.dumps((job.get("strategy") or {}).get("matrix") or {})
        runners.update(re.findall(r"(macos|ubuntu)-", f"{job.get('runs-on')} {matrix}"))
    assert runners == {"macos", "ubuntu"}, f"Run the clean install on macOS and Ubuntu runners: {runners}"
