"""Tests for install.sh.

Every run uses a copy of the checkout in a temp dir, a temp HOME, a temp install dir, and a
PATH of shims: fake brew, apt-get, sudo, uname and python3 (which intercepts 'python3 -m pip')
in front of links to the few real tools install.sh needs. Nothing is installed for real and
nothing runs as root.
"""

import os
import pty
import shutil
import stat
import subprocess
import sys
import tomllib

from pathlib import Path

import pytest

# Every test here runs install.sh in a subprocess
pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parent.parent

# Real tools install.sh uses; everything else on PATH is a shim
TOOLS = [
    "basename", "cat", "chmod", "cmp", "cp", "dirname", "grep", "head", "id", "ls", "ln",
    "mkdir", "readlink", "tail",
]

SHIMS = {
    "brew": """
        echo "brew $* [no_install_upgrade=${HOMEBREW_NO_INSTALL_UPGRADE:-}]" >> "$SHIM_LOG"
        case "$1 $2" in
            "bundle check") exit "${FAKE_BREW_CHECK:-1}" ;;
            "bundle install")
                if [ -n "${FAKE_BREW_FAIL:-}" ]; then
                    echo "Error: rsync: download failed" >&2
                    exit 1
                fi
                ;;
        esac
        exit 0
    """,
    "apt-get": """
        echo "apt-get $*" >> "$SHIM_LOG"
        if [ -n "${FAKE_APT_FAIL:-}" ]; then
            echo "E: Unable to locate package $*" >&2
            exit 100
        fi
        exit 0
    """,
    # Logs the command and runs it as the same user: never escalates
    "sudo": """
        echo "sudo $*" >> "$SHIM_LOG"
        exec "$@"
    """,
    "uname": """
        echo "${FAKE_UNAME:-Darwin}"
    """,
    "rsync": """
        exit 0
    """,
    "python3": """
        echo "python3 $*" >> "$SHIM_LOG"
        if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
            if [ -n "${FAKE_PIP_FAIL:-}" ]; then
                echo "error: externally-managed-environment" >&2
                echo "This environment is externally managed" >&2
                exit 1
            fi
            exit 0
        fi
        if [ "$1" = "-c" ] && [ "$2" = "import tqdm" ] && [ -n "${FAKE_NO_TQDM:-}" ]; then
            exit 1
        fi
        if [ "$1" = "-c" ] && [ -n "${FAKE_OLD_PYTHON:-}" ]; then
            case "$2" in
                *version_info*) exit 1 ;;
                *python_version*) echo 3.9.6; exit 0 ;;
            esac
        fi
        exec "$REAL_PYTHON" "$@"
    """,
}


def write_shim(path, body):
    """Write an executable /bin/sh script."""
    lines = [line[8:] if line.startswith(" " * 8) else line for line in body.splitlines()]
    path.write_text("#!/bin/sh\n" + "\n".join(lines).strip() + "\n")
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    """A checkout copy, temp HOME and shim PATH, and a run() for install.sh."""
    src = tmp_path / "src"
    src.mkdir()
    for name in ["install.sh", "snap.py", "requirements.txt", "Brewfile", "packages.txt"]:
        shutil.copy2(REPO / name, src / name)
    shutil.copytree(REPO / "configs", src / "configs")
    shutil.copytree(REPO / "scripts", src / "scripts")

    home = tmp_path / "home"
    home.mkdir()

    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in SHIMS.items():
        write_shim(shims / name, body)

    # /bin/bash is bash 3.2 on macOS: the oldest bash install.sh must run on
    tools = tmp_path / "tools"
    tools.mkdir()
    bash = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")
    (tools / "bash").symlink_to(bash)
    for name in TOOLS:
        found = shutil.which(name)
        if not found:
            pytest.skip(f"{name} not found")
        (tools / name).symlink_to(found)

    log = tmp_path / "shim.log"
    log.touch()
    env = {
        "HOME": str(home),
        "PATH": f"{shims}{os.pathsep}{tools}",
        "SHIM_LOG": str(log),
        "REAL_PYTHON": sys.executable,
    }
    target = tmp_path / "snaproot"

    def run(*args, extra_env=None, dest=target, stdin=subprocess.DEVNULL):
        argv = [bash, str(src / "install.sh"), *args]
        if dest is not None:
            argv.append(str(dest))
        return subprocess.run(
            argv, cwd=tmp_path, env=dict(env, **(extra_env or {})), stdin=stdin,
            capture_output=True, text=True, timeout=60,
        )

    class Installer:
        pass

    inst = Installer()
    inst.src, inst.home, inst.shims, inst.log, inst.target = src, home, shims, log, target
    inst.env, inst.run = env, run
    inst.bin = home / ".local" / "bin"
    inst.calls = lambda: log.read_text().splitlines()
    return inst


def tree(root):
    """Every path under root (not following links), relative and sorted."""
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            paths.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(paths)


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_fresh_install(installer):
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    target = installer.target

    assert tree(target) == [
        "captures", "configs", "configs/capture.toml", "configs/migrate.toml",
        "configs/restore.toml", "scripts", "scripts/capture-brew.sh",
        "scripts/restore-brew.sh", "snap.py",
    ]
    for path in [target, target / "configs", target / "scripts", target / "captures"]:
        assert mode(path) == 0o700
    assert mode(target / "configs" / "capture.toml") == 0o600
    assert (target / "configs" / "capture.toml").read_bytes() == (
        REPO / "configs" / "example-capture.toml").read_bytes()

    link = installer.bin / "snap"
    assert link.is_symlink()
    assert os.readlink(link) == str(target / "snap.py")

    # Nothing else in HOME (no ~/Snapshots link)
    assert tree(installer.home) == [".local", ".local/bin", ".local/bin/snap"]

    brewfile = installer.src / "Brewfile"
    assert installer.calls()[-2:] == [
        f"brew bundle check --no-upgrade --file={brewfile} [no_install_upgrade=]",
        f"brew bundle install --no-upgrade --file={brewfile} [no_install_upgrade=1]",
    ]
    assert not any(call.startswith("sudo") for call in installer.calls())
    lines = result.stdout.splitlines()
    assert lines[0] == "Starting install"
    assert lines[-1] == f"✓ Install completed in {target}"
    # A custom install dir says how to use it
    assert f"snap capture -r {target}" in result.stdout
    assert result.stderr == ""


def test_installed_configs_reference_installed_scripts(installer):
    assert installer.run("--yes").returncode == 0
    configs = installer.target / "configs"
    tables = {
        "capture.toml": [("scripts",)],
        "restore.toml": [("scripts",)],
        "migrate.toml": [("capture", "scripts"), ("restore", "scripts")],
    }
    seen = 0
    for name, paths in tables.items():
        config = tomllib.loads((configs / name).read_text())
        for keys in paths:
            table = config
            for key in keys:
                table = table[key]
            for when in ("before", "after"):
                for script in table.get(when, []):
                    assert (installer.target / script).is_file(), f"{name}: {script}"
                    seen += 1
    assert seen >= 4


def test_installed_command_runs_a_dry_run_capture(installer):
    assert installer.run("--yes").returncode == 0
    (installer.home / ".ssh").mkdir()
    (installer.home / ".ssh" / "config").write_text("Host *\n")
    (installer.home / ".zshrc").write_text("# rc\n")
    env = dict(os.environ, HOME=str(installer.home))
    result = subprocess.run(
        [str(installer.bin / "snap"), "capture", "-r", str(installer.target), "--dry-run"],
        env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    assert "Dry run completed; no changes were made" in result.stdout
    assert tree(installer.target / "captures") == []


def test_reinstall_keeps_user_files_and_makes_no_loops(installer):
    assert installer.run("--yes").returncode == 0
    capture = installer.target / "configs" / "capture.toml"
    capture.write_text("# mine\n")
    script = installer.target / "scripts" / "capture-brew.sh"
    script.write_text("# my script\n")
    before = tree(installer.target)

    for _ in range(2):
        result = installer.run("--yes")
        assert result.returncode == 0, result.stderr
    assert capture.read_text() == "# mine\n"
    assert script.read_text() == "# my script\n"
    assert tree(installer.target) == before
    assert not (installer.target / "captures" / "captures").exists()
    assert "skip: configs/capture.toml (exists)" in result.stdout
    assert "snap.py: up to date" in result.stdout
    assert os.readlink(installer.bin / "snap") == str(installer.target / "snap.py")


def test_reinstall_updates_snap_py(installer):
    assert installer.run("--yes").returncode == 0
    (installer.target / "snap.py").write_text("#!/usr/bin/env python3\n\"\"\"\nold\n\"\"\"\n")
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    assert "✓ snap.py updated" in result.stdout
    assert (installer.target / "snap.py").read_bytes() == (REPO / "snap.py").read_bytes()


def test_reinstall_leaves_a_linked_snap_py_alone(installer, tmp_path):
    installer.target.mkdir()
    own = tmp_path / "checkout-snap.py"
    own.write_text("# a developer's copy\n")
    (installer.target / "snap.py").symlink_to(own)
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    assert f"skip: snap.py (a link to {own})" in result.stdout
    assert own.read_text() == "# a developer's copy\n"


def test_no_tty_uses_the_defaults_and_says_so(installer):
    result = installer.run()   # stdin is /dev/null, no --yes
    assert result.returncode == 0, result.stderr
    assert "no terminal to ask on" in result.stdout
    assert (installer.bin / "snap").is_symlink()
    assert result.stdout.splitlines()[-1].startswith("✓ Install completed")


def test_bin_dir_option(installer, tmp_path):
    bin_dir = tmp_path / "mybin"
    result = installer.run("--bin-dir", str(bin_dir))
    assert result.returncode == 0, result.stderr
    assert os.readlink(bin_dir / "snap") == str(installer.target / "snap.py")
    assert "is not on your PATH" in result.stdout
    assert not (installer.bin / "snap").exists()


def test_existing_snap_on_path_installs_as_snaps(installer):
    write_shim(installer.shims / "snap", "echo snapd")
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    assert not (installer.bin / "snap").exists()
    assert os.readlink(installer.bin / "snaps") == str(installer.target / "snap.py")
    assert f"'snap' is another program ({installer.shims / 'snap'}); using the name 'snaps'" \
        in result.stdout
    assert "snaps capture -r" in result.stdout

    # A re-run picks the same name
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    assert sorted(os.listdir(installer.bin)) == ["snaps"]


def test_name_option_overrides(installer):
    write_shim(installer.shims / "snap", "echo snapd")
    result = installer.run("--yes", "--name", "snapper")
    assert result.returncode == 0, result.stderr
    assert sorted(os.listdir(installer.bin)) == ["snapper"]


def test_never_replaces_another_program(installer):
    installer.bin.mkdir(parents=True)
    other = installer.bin / "snap"
    other.write_text("#!/bin/sh\necho mine\n")
    result = installer.run("--yes", "--name", "snap")
    assert result.returncode == 1
    assert f"Error: {other} exists and is not snap.py; not replacing it" in result.stderr
    assert other.read_text() == "#!/bin/sh\necho mine\n"
    assert "✓ Install completed" not in result.stdout


def test_old_copied_command_is_replaced_by_the_link(installer):
    # Older versions of install.sh copied snap.py to ~/.local/bin/snap
    installer.bin.mkdir(parents=True)
    shutil.copy2(REPO / "snap.py", installer.bin / "snap")
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    assert os.readlink(installer.bin / "snap") == str(installer.target / "snap.py")


def test_failing_brew_fails_the_install(installer):
    result = installer.run("--yes", extra_env={"FAKE_BREW_FAIL": "1"})
    assert result.returncode == 1
    assert "Error: brew bundle install failed" in result.stderr
    assert "rsync: download failed" in result.stdout
    assert "✓ Install completed" not in result.stdout
    assert "✓ Brewfile" not in result.stdout
    assert not installer.target.exists()


def test_brew_already_satisfied_installs_nothing(installer):
    result = installer.run("--yes", extra_env={"FAKE_BREW_CHECK": "0"})
    assert result.returncode == 0, result.stderr
    assert not any("bundle install" in call for call in installer.calls())


@pytest.fixture
def linux(installer):
    """Make install.sh see Linux with one package to install."""
    (installer.src / "packages.txt").write_text("# needed\nsnap-test-pkg\nrsync\n")
    installer.env["FAKE_UNAME"] = "Linux"
    return installer


def test_linux_installs_missing_packages_with_sudo(linux):
    result = linux.run("--yes")
    assert result.returncode == 0, result.stderr
    calls = linux.calls()
    assert "sudo apt-get update" in calls
    assert "sudo apt-get install -y snap-test-pkg" in calls
    assert "✓ system packages installed" in result.stdout


def test_linux_failing_package_manager_fails_the_install(linux):
    result = linux.run("--yes", extra_env={"FAKE_APT_FAIL": "1"})
    assert result.returncode == 1
    assert "Error: Cannot install snap-test-pkg: 'sudo apt-get update' failed" in result.stderr
    assert "✓ system packages" not in result.stdout
    assert "✓ Install completed" not in result.stdout
    assert not linux.target.exists()


def test_linux_without_a_package_manager_fails(linux):
    (linux.shims / "apt-get").unlink()
    result = linux.run("--yes")
    assert result.returncode == 1
    assert "no supported package manager found" in result.stderr


def test_failing_pip_is_only_a_warning(installer):
    result = installer.run("--yes", extra_env={"FAKE_NO_TQDM": "1", "FAKE_PIP_FAIL": "1"})
    assert result.returncode == 0, result.stderr
    assert "Warning: Cannot install tqdm with pip; snap runs without progress bars" \
        in result.stderr
    assert "externally managed" in result.stdout
    assert "install.sh failed" not in result.stderr
    assert not any("--break-system-packages" in call for call in installer.calls())
    assert result.stdout.splitlines()[-1].startswith("✓ Install completed")


def test_pip_installs_tqdm_when_missing(installer):
    result = installer.run("--yes", extra_env={"FAKE_NO_TQDM": "1"})
    assert result.returncode == 0, result.stderr
    assert "✓ tqdm installed" in result.stdout
    assert any(call.startswith("python3 -m pip install --user -r") for call in installer.calls())


def test_old_python_fails(installer):
    result = installer.run("--yes", extra_env={"FAKE_OLD_PYTHON": "1"})
    assert result.returncode == 1
    assert "Error: snap.py needs Python 3.11 or later (found 3.9.6)" in result.stderr
    assert not installer.target.exists()


def test_refuses_a_non_empty_directory_that_is_not_a_snap_root(installer):
    installer.target.mkdir()
    (installer.target / "notes.txt").write_text("x")
    result = installer.run("--yes")
    assert result.returncode == 1
    assert "is not empty and is not a snap root" in result.stderr
    assert tree(installer.target) == ["notes.txt"]


def test_unknown_option_fails_with_a_message(installer):
    result = installer.run("--bogus", dest=None)
    assert result.returncode == 1
    assert "Error: Unknown option '--bogus'" in result.stderr


def test_default_install_dir_needs_no_r(installer):
    result = installer.run("--yes", dest=None)
    assert result.returncode == 0, result.stderr
    assert (installer.home / ".snap" / "snap.py").is_file()
    assert "capture -r" not in result.stdout


def run_on_a_terminal(installer, answer, *args):
    """Run install.sh with a terminal as stdin, and answer typed ahead."""
    leader, follower = pty.openpty()
    try:
        os.write(leader, answer.encode())
        return installer.run(*args, stdin=follower)
    finally:
        os.close(follower)
        os.close(leader)


@pytest.mark.parametrize("answer, command", [("\n", "snap"), ("1\n", "snap"), ("3\n", None)])
def test_terminal_asks_where_the_command_goes(installer, answer, command):
    result = run_on_a_terminal(installer, answer)
    assert result.returncode == 0, result.stderr
    assert "Where should the command go?" in result.stdout
    assert "Choose 1-3 [1]:" in result.stdout
    if command:
        assert (installer.bin / command).is_symlink()
    else:
        assert not installer.bin.exists()
        assert f"skip: command (run {installer.target}/snap.py directly)" in result.stdout
        assert f"{installer.target}/snap.py capture -r" in result.stdout


def test_terminal_invalid_choice_fails_with_a_message(installer):
    result = run_on_a_terminal(installer, "9\n")
    assert result.returncode == 1
    assert "Error: Invalid choice '9'" in result.stderr


def test_unexpected_failure_is_reported(installer):
    installer.target.mkdir()
    (installer.target / "snap.py").write_text("")
    (installer.target / "scripts").write_text("not a directory")
    result = installer.run("--yes")
    assert result.returncode != 0
    assert "Error: install.sh failed at line" in result.stderr
    assert "✓ Install completed" not in result.stdout


def test_brew_never_upgrades_installed_packages(installer):
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    installs = [call for call in installer.calls() if "bundle install" in call]
    assert installs and all("--no-upgrade" in call for call in installs)
    assert all("[no_install_upgrade=1]" in call for call in installs)


def test_refuses_a_symlink_to_the_checkout(installer, tmp_path):
    link = tmp_path / "checkout-link"
    link.symlink_to(installer.src)
    before = tree(installer.src)

    result = installer.run("--yes", dest=link)

    assert result.returncode == 1
    assert "Cannot install into the checkout itself" in result.stderr
    assert tree(installer.src) == before


def test_restore_brew_script_runs_brew_as_the_user_under_sudo(installer, tmp_path):
    work = tmp_path / "snapshot"
    work.mkdir()
    (work / "Brewfile").write_text('brew "jq"\n')
    fake_root = tmp_path / "fake-root"
    fake_root.mkdir()
    write_shim(fake_root / "id", 'if [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi')
    write_shim(fake_root / "sudo", 'echo "sudo $*" >> "$SHIM_LOG"')  # never runs anything
    env = dict(installer.env, SUDO_USER="alice")
    env["PATH"] = f"{fake_root}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["/bin/bash", str(installer.src / "scripts" / "example-restore-brew.sh")],
        cwd=work, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "sudo -u alice -H env HOMEBREW_NO_INSTALL_UPGRADE=1 "
        f"HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 brew bundle install --no-upgrade "
        f"--file={work}/Brewfile"
        in installer.calls()
    )
