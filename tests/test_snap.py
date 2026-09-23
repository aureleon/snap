"""Regression tests for snap.py.

Run with: python3 -m pytest tests/

End-to-end tests run snap.py in a subprocess with HOME pointed at a temp dir and a
fake `sudo` first on PATH, so they never touch the real home or escalate privileges.
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile

from pathlib import Path

import pytest
import tqdm

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import snap  # noqa: E402

SNAP = REPO / "snap.py"


# --- Helpers --- #


def write_files(root, files):
    """Create files under root from a {relative path: content} dict."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def make_archive(tmp_path, name, root, paths):
    """Create a gzip category archive with snap.archive_create."""
    outdir = tmp_path / "archives"
    outdir.mkdir(exist_ok=True)
    archive, _, _ = snap.archive_create(name, str(root), paths, outdir)
    return archive


def list_tree(root):
    """List every path under root, relative and sorted."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def backup_files(tmp_path, name):
    """Find a file name in any rollback dir next to tmp_path/home."""
    return [p for p in tmp_path.glob("home.bak*/**/*") if p.name == name]


unprivileged = pytest.mark.skipif(os.geteuid() == 0, reason="needs an unprivileged user")
unprivileged_only = unprivileged


@pytest.fixture
def env(tmp_path):
    """A temp HOME, snap root, and fake sudo for end-to-end runs."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "snaproot"
    (root / "configs").mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\necho "$@" > "$SUDO_MARKER"\nexit 1\n')
    sudo.chmod(0o755)

    # Keep tqdm importable even when it lives in the real user site-packages
    tqdm_path = str(Path(tqdm.__file__).resolve().parent.parent)
    run_env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        PYTHONPATH=tqdm_path,
        SUDO_MARKER=str(tmp_path / "sudo-called"),
    )

    # Never fall through to the real sudo
    if shutil.which("sudo", path=run_env["PATH"]) != str(sudo):
        pytest.skip("fake sudo is not first on PATH")
    try:
        subprocess.run([str(sudo)], env=run_env, check=False)
    except OSError:
        pytest.skip("fake sudo cannot run from the temp dir")
    if not (tmp_path / "sudo-called").exists():
        pytest.skip("fake sudo cannot run from the temp dir")
    (tmp_path / "sudo-called").unlink()

    def run(*args, extra_env=None):
        return subprocess.run(
            [sys.executable, str(SNAP), *args],
            cwd=tmp_path,
            env=dict(run_env, **(extra_env or {})),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    return argparse.Namespace(
        tmp=tmp_path, home=home, root=root, run=run, sudo=sudo, env=run_env,
        sudo_marker=tmp_path / "sudo-called",
    )


def write_configs(root, categories, rollback=".bak", restore_extra=""):
    """Write capture.toml and an all-archives restore.toml."""
    lines = ["[tarball]", 'compress = "gzip"', 'checksum = "sha256"']
    if rollback:
        lines.append(f'rollback = "{rollback}"')
    for name, (cat_root, dirs) in categories.items():
        dirs_toml = ", ".join(f'"{d}"' for d in dirs)
        lines += ["", f"[tar.{name}]", f'root = "{cat_root}"', f"dirs = [{dirs_toml}]"]
    (root / "configs" / "capture.toml").write_text("\n".join(lines) + "\n")
    restore_toml = '[tar]\narchives = ["*"]\n' + restore_extra
    (root / "configs" / "restore.toml").write_text(restore_toml)


# --- Fix: restore only replaces captured paths --- #


def test_archive_entries_returns_captured_paths(tmp_path):
    write_files(tmp_path / "src", {".config/nvim/init.lua": "x", ".zshrc": "y"})
    archive = make_archive(tmp_path, "cat", tmp_path / "src", [".config/nvim", ".zshrc"])

    with tarfile.open(archive) as tar:
        assert list(snap.archive_entries(tar)) == [".config/nvim", ".zshrc"]


def test_archive_entries_skips_members_below_symlinks(tmp_path):
    archive = tmp_path / "cat.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo(".config")
        link.type = tarfile.SYMTYPE
        link.linkname = "dotfiles/config"
        tar.addfile(link)
        info = tarfile.TarInfo(".config/nvim/init.lua")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"new"))

    with tarfile.open(archive) as tar:
        groups = snap.archive_entries(tar)
    assert {entry: [m.name for m in members] for entry, members in groups.items()} == {
        ".config": [".config"],
    }


@pytest.mark.parametrize("name", ["../outside", "/etc/outside"])
def test_archive_entries_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))

    with tarfile.open(archive) as tar:
        with pytest.raises(ValueError):
            snap.archive_entries(tar)


def test_restore_keeps_uncaptured_siblings(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {".config/nvim/init.lua": "new", ".config/fish/config.fish": "fish"})
    write_files(home, {
        ".config/gh/hosts.yml": "token",
        ".config/git/config": "git",
        ".config/nvim/old.lua": "old",
    })
    nvim = make_archive(tmp_path, "nvim", src, [".config/nvim"])
    fish = make_archive(tmp_path, "fish", src, [".config/fish"])

    assert snap.restore_category(nvim, str(home), ".bak")
    assert snap.restore_category(fish, str(home), ".bak")

    assert list_tree(home) == [
        ".config",
        ".config/fish",
        ".config/fish/config.fish",
        ".config/gh",
        ".config/gh/hosts.yml",
        ".config/git",
        ".config/git/config",
        ".config/nvim",
        ".config/nvim/init.lua",
    ]
    # The replaced path is backed up, not lost
    assert [p.read_text() for p in backup_files(tmp_path, "old.lua")] == ["old"]


def test_restore_rejects_unsafe_archive_without_deleting(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    write_files(outside, {"keep.txt": "keep"})

    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(outside, arcname="../outside")

    assert not snap.restore_category(archive, str(home), ".bak")
    assert (outside / "keep.txt").read_text() == "keep"
    assert not (tmp_path / "home.bak").exists()


def fail_on_extract(monkeypatch, name, error):
    """Make tar extraction raise `error` for members under the top-level dir `name`."""
    original_extract = tarfile.TarFile.extract
    original_extractall = tarfile.TarFile.extractall

    def check(member):
        member_name = member if isinstance(member, str) else member.name
        if member_name.split("/")[0] == name:
            raise error

    def extract(self, member, *args, **kwargs):
        check(member)
        return original_extract(self, member, *args, **kwargs)

    def extractall(self, path=".", members=None, **kwargs):
        for member in members or []:
            check(member)
        return original_extractall(self, path, members=members, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extract", extract)
    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)


def test_rollback_restores_the_failing_entry(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "b/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a", "b/orig.txt": "b"})
    archive = make_archive(tmp_path, "cat", src, ["a", "b"])

    fail_on_extract(monkeypatch, "b", OSError("disk full"))

    assert not snap.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt", "b", "b/orig.txt"]


def test_rollback_removes_new_paths_and_created_parents(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "deep/er/n.txt": "n", "zz/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a"})
    archive = make_archive(tmp_path, "cat", src, ["a", "deep/er/n.txt", "zz"])

    fail_on_extract(monkeypatch, "zz", OSError("disk full"))

    assert not snap.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt"]


def test_incomplete_rollback_stops_restore(tmp_path, monkeypatch, capsys):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "zz/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a"})
    archive = make_archive(tmp_path, "cat", src, ["a", "zz"])

    fail_on_extract(monkeypatch, "zz", OSError("disk full"))
    original_copy = snap.copy_with_ownership

    def copy(src_path, dst_path):
        if ".bak" in str(src_path):
            raise OSError("read-only file system")
        return original_copy(src_path, dst_path)

    monkeypatch.setattr(snap, "copy_with_ownership", copy)

    with pytest.raises(SystemExit):
        snap.restore_category(archive, str(home), ".bak")
    assert "backups are in" in capsys.readouterr().err
    assert (tmp_path / "home.bak" / "a" / "orig.txt").read_text() == "a"


def test_rollback_runs_on_keyboard_interrupt(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a"})
    archive = make_archive(tmp_path, "cat", src, ["a"])

    fail_on_extract(monkeypatch, "a", KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        snap.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt"]


@unprivileged_only
def test_rollback_removes_read_only_dirs_from_archive(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    write_files(src, {"a/ro/f.txt": "new", "zz/new.txt": "new"})
    (src / "a" / "ro").chmod(0o555)
    archive = make_archive(tmp_path, "cat", src, ["a", "zz"])
    (src / "a" / "ro").chmod(0o755)

    fail_on_extract(monkeypatch, "zz", OSError("disk full"))

    assert not snap.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == []


def test_cross_group_hardlink_links_to_restored_file(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/x": "new", "b/z": "z"})
    (src / "b" / "y").hardlink_to(src / "a" / "x")
    write_files(home, {"a/x": "old", "b/y": "old"})
    archive = make_archive(tmp_path, "cat", src, ["b/z", "a", "b"])

    assert snap.restore_category(archive, str(home), ".bak")

    assert (home / "a" / "x").read_text() == "new"
    assert (home / "b" / "y").read_text() == "new"
    assert (home / "a" / "x").samefile(home / "b" / "y")


def record_chown(monkeypatch):
    """Pretend to be root and record chown calls instead of making them."""
    calls = {}

    def chown(path, uid, gid, *, follow_symlinks=True):
        calls[Path(path)] = (uid, gid, follow_symlinks)

    monkeypatch.setattr(snap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(snap.os, "chown", chown)
    return calls


def test_new_dirs_get_nearest_ancestor_owner_as_root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    write_files(src, {"fonts/a.ttf": "font"})
    archive = make_archive(tmp_path, "local", src, ["fonts"])
    calls = record_chown(monkeypatch)
    home_owner = (home.stat().st_uid, home.stat().st_gid, True)

    assert snap.restore_category(archive, str(home / ".local" / "share"), ".bak")

    for new_dir in [".local", ".local/share", ".local/share.bak"]:
        assert calls[home / new_dir] == home_owner


def test_copy_with_ownership_never_follows_links_as_root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    write_files(src, {"dir/f.txt": "x"})
    (src / "dir" / "link").symlink_to("/etc/hosts")
    calls = record_chown(monkeypatch)

    snap.copy_with_ownership(src / "dir", tmp_path / "copy")

    assert set(calls) == {tmp_path / "copy", tmp_path / "copy/f.txt", tmp_path / "copy/link"}
    assert all(follow is False for _, _, follow in calls.values())


def test_restore_over_symlinked_dir_keeps_link_target(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    write_files(src, {".config/nvim/init.lua": "new"})
    write_files(dotfiles, {"nvim/init.lua": "stow"})
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "nvim").symlink_to(dotfiles / "nvim")
    archive = make_archive(tmp_path, "nvim", src, [".config/nvim"])

    assert snap.restore_category(archive, str(home), ".bak")

    restored = home / ".config" / "nvim"
    assert not restored.is_symlink()
    assert (restored / "init.lua").read_text() == "new"
    assert (dotfiles / "nvim" / "init.lua").read_text() == "stow"
    assert (tmp_path / "home.bak" / ".config" / "nvim").is_symlink()


def test_repeated_restores_rotate_backups_without_collision(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a.txt": "new"})
    write_files(home, {"a.txt": "old"})
    archive = make_archive(tmp_path, "cat", src, ["a.txt"])

    # Freeze the clock so every rotation gets the same timestamp
    frozen = snap.datetime(2026, 1, 2, 3, 4, 5)
    monkeypatch.setattr(snap, "datetime", type("D", (), {"now": staticmethod(lambda: frozen)}))

    for _ in range(3):
        assert snap.restore_category(archive, str(home), ".bak")

    backups = [p.read_text() for p in sorted(tmp_path.glob("home.bak*/a.txt"))]
    assert sorted(backups) == ["new", "new", "old"]


def test_e2e_restore_keeps_files_created_after_capture(env):
    write_files(env.home, {".config/nvim/init.lua": "v1", ".config/gh/hosts.yml": "token"})
    write_configs(env.root, {"nvim": ("$HOME", [".config/nvim"])})

    result = env.run("capture", "-r", str(env.root))
    assert result.returncode == 0, result.stderr

    write_files(env.home, {".config/nvim/init.lua": "edited", ".config/git/config": "git"})
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr

    assert (env.home / ".config/nvim/init.lua").read_text() == "v1"
    assert (env.home / ".config/gh/hosts.yml").read_text() == "token"
    assert (env.home / ".config/git/config").read_text() == "git"
    assert not env.sudo_marker.exists()


# --- Fix: dry-run never extracts --- #


def test_dry_run_archive_extract_changes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    write_files(src, {"a.txt": "new"})
    write_files(dst, {"a.txt": "old"})
    archive = make_archive(tmp_path, "cat", src, ["a.txt"])

    monkeypatch.setattr(snap, "__dry_run__", True)
    assert snap.archive_extract(archive, root=str(dst))
    assert snap.extract_archives([archive], root_map={"cat": str(dst)}) == 1

    assert (dst / "a.txt").read_text() == "old"


def test_extract_archives_skips_members_below_symlinks(tmp_path):
    dst = tmp_path / "dst"
    dotfiles = dst / "dotfiles"
    write_files(dotfiles, {"nvim/init.lua": "user edit"})
    (dst / ".config").symlink_to("dotfiles")

    archive = tmp_path / "cat.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo(".config")
        link.type = tarfile.SYMTYPE
        link.linkname = "dotfiles"
        tar.addfile(link)
        info = tarfile.TarInfo(".config/nvim/init.lua")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"new"))

    assert snap.archive_extract(archive, root=str(dst))
    assert (dotfiles / "nvim" / "init.lua").read_text() == "user edit"


@pytest.mark.parametrize("flags", [[], ["--disable-rollback"]])
def test_e2e_dry_run_restore_changes_nothing(env, flags):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    write_files(env.home, {".zshrc": "edited"})
    result = env.run("restore", "-r", str(env.root), "--dry-run", *flags)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (env.home / ".zshrc").read_text() == "edited"
    assert list_tree(env.tmp / "home") == [".zshrc"]
    assert not list(env.tmp.glob("home.bak*"))


# --- Fix: sudo only when needed, with the user's HOME --- #


def test_path_writable_checks_nearest_existing_parent(tmp_path):
    assert snap.path_writable(tmp_path / "missing" / "deeper")
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o555)
        assert not snap.path_writable(locked / "missing")


@unprivileged
def test_path_writable_below_unsearchable_dir(tmp_path):
    locked = tmp_path / "locked"
    (locked / "sub").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert not snap.path_writable(locked / "sub" / "missing")
    finally:
        locked.chmod(0o755)


def needs_sudo(tmp_path, root, paths, roll_ext=".bak", snapshot_config=None):
    """Capture paths under root and check if restoring them back needs sudo."""
    archive = make_archive(tmp_path, "cat", root, paths)
    return snap.restore_needs_sudo([archive], {"cat": str(root)}, roll_ext, snapshot_config or {})


@unprivileged
def test_needs_sudo_for_locked_dir_inside_captured_path(tmp_path):
    home = tmp_path / "home"
    write_files(home, {"fonts/a.txt": "a", "fonts/locked/b.txt": "b"})
    (home / "fonts" / "locked").chmod(0o555)
    try:
        assert needs_sudo(tmp_path, home, ["fonts"])
        # Merging without rollback only rewrites files, which stay writable
        assert not needs_sudo(tmp_path, home, ["fonts"], roll_ext=None)
    finally:
        (home / "fonts" / "locked").chmod(0o755)


@unprivileged
def test_needs_sudo_for_unwritable_backup_parent(tmp_path):
    parent = tmp_path / "opt"
    write_files(parent / "app", {"a.txt": "a"})
    parent.chmod(0o555)
    try:
        assert needs_sudo(tmp_path, parent / "app", ["a.txt"])
        assert not needs_sudo(tmp_path, parent / "app", ["a.txt"], roll_ext=None)
    finally:
        parent.chmod(0o755)


@unprivileged
def test_needs_sudo_for_unwritable_link_parent(tmp_path):
    home = tmp_path / "home"
    locked = tmp_path / "locked"
    write_files(home, {"a.txt": "a"})
    locked.mkdir(mode=0o555)
    config = {"tar": {"cat": {"root": str(home), "link": str(locked / "link")}}}
    try:
        assert needs_sudo(tmp_path, home, ["a.txt"], snapshot_config=config)
    finally:
        locked.chmod(0o755)


@unprivileged
def test_needs_sudo_for_unreadable_file_inside_captured_path(tmp_path):
    home = tmp_path / "home"
    write_files(home, {"gcloud/config": "a", "gcloud/credentials.db": "secret"})
    archive = make_archive(tmp_path, "cat", home, ["gcloud"])
    (home / "gcloud" / "credentials.db").chmod(0)
    try:
        assert snap.restore_needs_sudo([archive], {"cat": str(home)}, ".bak", {})
    finally:
        (home / "gcloud" / "credentials.db").chmod(0o644)


@unprivileged
def test_needs_sudo_for_unreadable_archive(tmp_path):
    home = tmp_path / "home"
    write_files(home, {"a.txt": "a"})
    archive = make_archive(tmp_path, "cat", home, ["a.txt"])
    archive.chmod(0)
    try:
        assert snap.restore_needs_sudo([archive], {"cat": str(home)}, ".bak", {})
    finally:
        archive.chmod(0o644)


def test_no_sudo_for_writable_home(tmp_path):
    home = tmp_path / "home"
    write_files(home, {".config/nvim/init.lua": "x"})
    assert not needs_sudo(tmp_path, home, [".config/nvim"])


def test_sudo_restore_runs_child_on_local_copy(tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd):
            self.waits = 0
            config_path = cmd[cmd.index("-t") + 1]
            calls.append((cmd, Path(config_path).read_text(), config_path))

        def wait(self):
            # The first Ctrl-C reaches the parent too; it must keep waiting
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            assert Path(calls[0][2]).exists()
            return 3

    monkeypatch.setattr(snap.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.setenv("XDG_DATA_HOME", "/Users/someone/.local/share")
    args = argparse.Namespace(root_host=None, disable_rollback=True, run_scripts=True)
    restore_config = {
        "tarball": {"rollback": ".bak"},
        "tar": {"archives": ["dots*", "🚀\x7f"]},
        "scripts": {"after": ["s.sh"], "notes": {"a": 1}},
    }
    snapshot_config = {"tar": {
        "data": {"root": "${XDG_DATA_HOME}/app"},
        "dots": {"root": "$HOME", "link": "$HOME/link"},
    }}

    with pytest.raises(SystemExit) as exit_info:
        snap.sudo_restore(args, tmp_path / "root", tmp_path / "cap", restore_config,
                          snapshot_config)

    assert exit_info.value.code == 3
    [(cmd, config_text, config_path)] = calls
    assert cmd == [
        "sudo", "env",
        "HOME=/Users/someone",
        "XDG_DATA_HOME=/Users/someone/.local/share",
        sys.executable, str(snap.__script__), "restore",
        "--from", str(tmp_path / "cap"), "-r", str(tmp_path / "root"), "-t", config_path,
        "--disable-rollback", "--run-scripts",
    ]
    assert snap.tomllib.loads(config_text) == {
        "tar": {"archives": ["dots*", "🚀\x7f"]},
        "scripts": {"after": ["s.sh"]},
    }
    assert not Path(config_path).exists()


@pytest.mark.parametrize("archives", [None, []])
def test_sudo_restore_keeps_skip_restore_semantics(tmp_path, monkeypatch, archives):
    configs = []

    class FakePopen:
        def __init__(self, cmd):
            configs.append(Path(cmd[cmd.index("-t") + 1]).read_text())

        def wait(self):
            return -2

    monkeypatch.setattr(snap.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit) as exit_info:
        snap.sudo_restore(args, tmp_path, tmp_path, {"tar": {"archives": archives}}, {})

    assert exit_info.value.code == 130  # killed by SIGINT, like a shell reports it
    assert snap.tomllib.loads(configs[0]) == {"tar": {"archives": []}}


SITECUSTOMIZE = """
import os
if os.environ.get("SNAP_TEST_FAKE_ROOT"):
    os.geteuid = lambda: 0
elif os.environ.get("SNAP_TEST_FORCE_SUDO"):
    real_access = os.access

    def access(path, mode, *args, **kwargs):
        if "force-sudo" in str(path) and mode & os.W_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    os.access = access
"""


def test_e2e_sudo_child_restores_from_local_copy(env):
    root = env.tmp / "force-sudo-root"
    write_files(root, {"f.txt": "v1"})
    write_configs(env.root, {"system": (str(root), ["f.txt"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(root, {"f.txt": "edited"})

    # The parent sees the root as unwritable; the "sudo" child pretends to be root
    site = env.tmp / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env.sudo.write_text(
        '#!/bin/sh\necho "$@" > "$SUDO_MARKER"\n'
        "export SNAP_TEST_FAKE_ROOT=1\nunset SNAP_TEST_FORCE_SUDO\nexec \"$@\"\n"
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run("restore", "-r", str(env.root), extra_env={
        "SNAP_TEST_FORCE_SUDO": "1",
        "PYTHONPATH": f"{site}{os.pathsep}{env.env['PYTHONPATH']}",
        "TMPDIR": str(tmpdir),
    })

    assert result.returncode == 0, result.stdout + result.stderr
    assert env.sudo_marker.exists()
    assert (root / "f.txt").read_text() == "v1"
    assert list(tmpdir.iterdir()) == []


@unprivileged
def test_e2e_sudo_only_for_unwritable_roots(env):
    locked = env.tmp / "locked"
    write_files(locked, {"f.txt": "v1"})
    locked.chmod(0o555)
    write_configs(env.root, {"system": (str(locked), ["f.txt"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    # Dry-run reports the need for sudo but never runs it
    result = env.run("restore", "-r", str(env.root), "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would re-run with sudo" in result.stdout
    assert not env.sudo_marker.exists()

    # A real run escalates, keeping the invoking user's HOME
    env.run("restore", "-r", str(env.root))
    sudo_args = env.sudo_marker.read_text().split()
    assert sudo_args[:2] == ["env", f"HOME={env.home}"]
    assert sudo_args[sudo_args.index("restore") + 1] == "--from"
    locked.chmod(0o755)


@unprivileged
def test_e2e_sudo_for_locked_dir_keeps_files(env):
    fonts = env.home / ".local/share/fonts"
    write_files(fonts, {"a.txt": "v1", "locked/b.txt": "b"})
    (fonts / "locked").chmod(0o555)
    write_configs(env.root, {"local": ("$HOME/.local/share", ["fonts"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    env.run("restore", "-r", str(env.root))

    assert env.sudo_marker.exists()
    assert (fonts / "a.txt").read_text() == "v1"
    (fonts / "locked").chmod(0o755)


def test_e2e_no_sudo_for_unselected_locked_category(env):
    locked = env.tmp / "locked"
    write_files(locked, {"f.txt": "v1"})
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {
        "system": (str(locked), ["f.txt"]),
        "dotfiles": ("$HOME", [".zshrc"]),
    })
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (env.root / "configs" / "restore.toml").write_text('[tar]\narchives = ["dotfiles"]\n')
    locked.chmod(0o555)

    result = env.run("restore", "-r", str(env.root))

    locked.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not env.sudo_marker.exists()


@unprivileged
def test_e2e_dry_run_on_unsearchable_root_does_not_crash(env):
    prot = env.tmp / "prot"
    write_files(prot, {"sub/f.txt": "v1"})
    write_configs(env.root, {"priv": (str(prot), ["sub"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    prot.chmod(0)

    result = env.run("restore", "-r", str(env.root), "--dry-run")

    prot.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would re-run with sudo" in result.stdout
    assert not env.sudo_marker.exists()


def test_e2e_dry_run_does_not_run_scripts(env):
    marker = env.tmp / "script-ran"
    (env.root / "scripts").mkdir()
    (env.root / "scripts" / "touch.sh").write_text(f"touch {marker}\n")
    write_files(env.home, {".zshrc": "v1"})
    write_configs(
        env.root,
        {"dotfiles": ("$HOME", [".zshrc"])},
        restore_extra='\n[scripts]\nbefore = ["scripts/touch.sh"]\nafter = ["scripts/touch.sh"]\n',
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run("restore", "-r", str(env.root), "--dry-run", "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


# --- Fix: user@host without a colon is a remote host --- #


@pytest.mark.parametrize("value, expected", [
    (None, (None, None)),
    ("user@host", ("user@host", None)),
    ("user@host:", ("user@host", None)),
    ("host:", ("host", None)),
    ("host:/srv/snap", ("host", Path("/srv/snap"))),
    ("user@host:~/snap", ("user@host", Path("~/snap"))),
    ("./name@tag", (None, Path("./name@tag"))),
    ("/abs/path", (None, Path("/abs/path"))),
    ("relative/path", (None, Path("relative/path"))),
])
def test_parse_remote_arg(value, expected):
    assert snap.parse_remote_arg(value) == expected


@pytest.mark.parametrize("value", [
    "-oProxyCommand=evil:",
    "user@-oProxyCommand=evil:/path",
    "user@-x",
    "@-x:p",
    "@host:",
])
def test_parse_remote_arg_rejects_bad_hosts(value):
    with pytest.raises(SystemExit):
        snap.parse_remote_arg(value)


@pytest.mark.parametrize("root, expected_host, expected_root", [
    ("user@host", "user@host", Path("~/.snap")),
    ("host:", "host", Path("~/.snap")),
    ("host:/srv/snap", "host", Path("/srv/snap")),
])
def test_main_remote_snap_root(monkeypatch, root, expected_host, expected_root):
    seen = []
    monkeypatch.setattr(snap, "__dry_run__", False)
    monkeypatch.setattr(snap, "__verbose__", False)
    monkeypatch.setattr(snap, "cmd_capture", seen.append)
    monkeypatch.setattr(snap.sys, "argv", ["snap", "capture", "-r", root, "--dry-run"])

    snap.main()

    assert seen[0].root_host == expected_host
    assert seen[0].root == expected_root


def test_restore_to_user_at_host_deploys_remotely(tmp_path, monkeypatch):
    capture_dir = tmp_path / "snapshot"
    capture_dir.mkdir()
    (capture_dir / "snapshot.toml").write_text("")
    calls = []
    monkeypatch.setattr(snap, "remote_deploy", lambda *a, **k: calls.append("remote"))
    monkeypatch.setattr(snap, "restore", lambda *a, **k: calls.append("local"))

    args = argparse.Namespace(
        root=tmp_path, root_host=None, src=str(capture_dir), dst="user@target.host"
    )
    snap.cmd_restore(args)

    assert calls == ["remote"]


@pytest.mark.parametrize("args", [
    ["migrate", "--from", "source.host", "--to", "user@target.host"],
    ["migrate", "--to", "./dir"],
    ["migrate", "--from", "user@source.host", "--to", ""],
    ["capture", "--from", "source.host"],
    ["restore", "--to", ""],
])
def test_e2e_non_host_from_or_to_is_rejected(env, args):
    result = env.run(*args, "-r", str(env.root), "--dry-run")

    assert result.returncode == 1
    assert "is not a remote host" in result.stderr


def test_e2e_restore_to_non_host_is_rejected(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "edited"})

    result = env.run("restore", "-r", str(env.root), "--to", "target.host")

    assert result.returncode == 1
    assert "is not a remote host" in result.stderr
    assert (env.home / ".zshrc").read_text() == "edited"
