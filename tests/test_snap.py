"""Regression tests for snap.py.

Run with: python3 -m pytest tests/

End-to-end tests run snap.py in a subprocess with HOME pointed at a temp dir and a
fake `sudo` first on PATH, so they never touch the real home or escalate privileges.
"""

import argparse
import io
import os
import re
import shlex
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


@pytest.fixture(autouse=True)
def reset_copy_failure():
    """Clear the module-level copy-failure flag so tests stay order-independent."""
    yield
    snap._copy_failed.clear()


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

    def run(*args, extra_env=None, answers=None):
        # answers is the text typed at the prompts; without it stdin is empty
        stdin = {"input": answers} if answers is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            [sys.executable, str(SNAP), *args],
            cwd=tmp_path,
            env=dict(run_env, **(extra_env or {})),
            capture_output=True,
            text=True,
            timeout=120,
            **stdin,
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
    assert (tmp_path / "home.bak" / "cat" / "a" / "orig.txt").read_text() == "a"


def test_rollback_runs_even_if_reporting_fails(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "b/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a", "b/orig.txt": "b"})
    archive = make_archive(tmp_path, "cat", src, ["a", "b"])

    fail_on_extract(monkeypatch, "b", OSError("disk full"))

    def broken_stderr(*args, **kwargs):
        raise BrokenPipeError

    monkeypatch.setattr(snap, "error", broken_stderr)

    with pytest.raises(BrokenPipeError):
        snap.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt", "b", "b/orig.txt"]


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
    assert (tmp_path / "home.bak" / "nvim" / ".config" / "nvim").is_symlink()


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

    backups = [p.read_text() for p in sorted(tmp_path.glob("home.bak*/cat/a.txt"))]
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


# --- Output problems stop a run where print() and tqdm always did --- #


def test_e2e_restore_to_closed_stdout_pipe_changes_nothing(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "edited"})

    # The pipe's reader is gone before snap.py writes anything
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [sys.executable, str(SNAP), "restore", "-r", str(env.root)],
            cwd=env.tmp, env=env.env, stdin=subprocess.DEVNULL, stdout=write_fd,
            stderr=subprocess.PIPE, text=True, timeout=120,
        )
    finally:
        os.close(write_fd)

    # The flush at the first bar fails, before any file changes
    assert result.returncode == 120, result.stderr
    assert (env.home / ".zshrc").read_text() == "edited"
    assert not list(env.tmp.glob("home.bak*"))


def test_e2e_restore_with_latin1_stdout_changes_nothing(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "edited"})

    # '✓ 1 archive verified' cannot be encoded, before any file changes
    result = env.run("restore", "-r", str(env.root), extra_env={"PYTHONIOENCODING": "latin-1"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert (env.home / ".zshrc").read_text() == "edited"
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
    ("user@host", "user@host", ".snap"),
    ("host:", "host", ".snap"),
    ("host:~", "host", "."),
    ("host:~/snap", "host", "snap"),
    ("host:/srv/snap", "host", "/srv/snap"),
])
def test_main_remote_snap_root(monkeypatch, root, expected_host, expected_root):
    # B6: a remote snap root is kept as (host, path), and args.root is a local copy of its
    # configs and scripts that main() removes at exit
    seen, fetched = [], []
    monkeypatch.setattr(snap, "__dry_run__", False)
    monkeypatch.setattr(snap, "__verbose__", False)
    monkeypatch.setattr(snap, "root_fetch", lambda *a: fetched.append(a))
    monkeypatch.setattr(snap, "cmd_capture", lambda args: seen.append(
        (args.root_host, str(args.root_path), args.root, args.root.is_dir())
    ))
    monkeypatch.setattr(snap.sys, "argv", ["snap", "capture", "-r", root, "--dry-run"])

    snap.main()

    [(host, path, local_copy, existed)] = seen
    assert (host, path) == (expected_host, expected_root)
    assert fetched == [(expected_host, snap.PurePosixPath(expected_root), local_copy)]
    assert existed and not local_copy.exists()


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


# --- Output: display code never crashes, and each failure prints once --- #


NO_SUCH_USER_ROOT = "~snap-test-no-such-user/root"


def unexpandable_root():
    """Return a root that expand_path() cannot expand (an unknown user's home)."""
    try:
        snap.expand_path(NO_SUCH_USER_ROOT)
    except RuntimeError:
        return NO_SUCH_USER_ROOT
    pytest.skip("the test user name exists on this machine")


def test_create_archives_failure_shows_unexpandable_root(tmp_path, capsys):
    root = unexpandable_root()
    outdir = tmp_path / "out"
    outdir.mkdir()

    assert snap.create_archives([("cat", root, [".zshrc"], outdir)]) == []

    captured = capsys.readouterr()
    assert "Error: Cannot create cat.tar.gz: " in captured.err
    assert f"  cat.tar.gz (root: {root})\n    failed (see the error above)\n" in captured.out
    assert list(outdir.iterdir()) == []


def test_archive_confirm_shows_unexpandable_root(tmp_path, monkeypatch, capsys):
    root = unexpandable_root()
    write_files(tmp_path / "src", {".zshrc": "x"})
    archive = make_archive(tmp_path, "cat", tmp_path / "src", [".zshrc"])
    monkeypatch.setattr(snap, "__dry_run__", True)

    # The header shows the root as given; the extraction reports the real error
    assert snap.extract_archives([archive], root_map={"cat": root}) == 0

    captured = capsys.readouterr()
    assert f"cat.tar.gz (root: {root})" in captured.out
    assert "add: .zshrc" in captured.out
    assert "Error: Cannot extract cat.tar.gz: " in captured.err


def test_archive_confirm_prompt_matches_the_targets(tmp_path, monkeypatch, capsys):
    write_files(tmp_path / "src", {".zshrc": "x", ".vimrc": "y"})
    archive = make_archive(tmp_path, "cat", tmp_path / "src", [".zshrc", ".vimrc"])
    empty = make_archive(tmp_path, "empty", tmp_path / "src", [".missing"])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(snap, "__dry_run__", True)

    # Every entry is new: no overwrite warning, and paths relative to the root
    assert snap.archive_confirm(archive, root=str(home))
    out = capsys.readouterr().out
    assert "     add: .vimrc\n" in out
    assert "     add: .zshrc\n" in out
    assert "Restore cat.tar.gz? [y/N]: y (assumed in a dry run)" in out
    assert "Existing files" not in out

    # One entry exists
    write_files(home, {".zshrc": "old"})
    assert snap.archive_confirm(archive, root=str(home))
    out = capsys.readouterr().out
    assert "     replace: .zshrc\n" in out
    assert "     add: .vimrc\n" in out
    assert "Restore cat.tar.gz? Existing files will be overwritten [y/N]: " in out

    # No entries at all
    assert snap.archive_confirm(empty, root=str(home))
    assert "Restore empty.tar.gz? It has no files [y/N]: " in capsys.readouterr().out


def test_rsync_parallel_reports_only_the_first_failed_copy(tmp_path, monkeypatch, capsys):
    def failing_rsync(cmd, **kwargs):
        raise subprocess.CalledProcessError(23, cmd, output="", stderr=f"rsync: bad {cmd[-2]}\n")

    monkeypatch.setattr(snap.subprocess, "run", failing_rsync)
    monkeypatch.setattr(snap, "__dry_run__", False)
    transfers = [(str(tmp_path / f"f{i}"), "host:/tmp/work/", f"f{i}") for i in range(4)]

    with pytest.raises(SystemExit) as exit_info:
        snap.rsync_parallel(transfers)

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err.count("Error: ") == 1
    assert err.count("rsync: bad ") == 1

    # The next group of copies reports its own failure again
    with pytest.raises(SystemExit):
        snap.rsync_parallel(transfers[:1])
    assert capsys.readouterr().err.count("Error: Copying f0 failed (rsync exit status 23)") == 1


def test_run_scripts_skips_the_step_when_every_script_is_missing(tmp_path, capsys):
    (tmp_path / "scripts").mkdir()
    config = {"scripts": {"before": ["scripts/a.sh", "scripts/b.sh"]}}

    snap.run_scripts(tmp_path, config, "before", working_dir=tmp_path)

    captured = capsys.readouterr()
    assert "Skipping before scripts (no scripts found)" in captured.out
    assert "Running" not in captured.out
    assert captured.err.count("Warning: Script scripts/") == 2


def test_run_scripts_names_the_migration_phase(tmp_path, monkeypatch, capsys):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "a.sh").write_text("true\n")
    config = {"scripts": {"before": ["scripts/a.sh"]}}
    monkeypatch.setattr(snap, "__dry_run__", True)

    snap.run_scripts(tmp_path, config, "before", working_dir=tmp_path, phase="capture")
    snap.run_scripts(tmp_path, config, "after", working_dir=tmp_path, phase="restore")

    out = capsys.readouterr().out
    assert "Running capture before scripts..." in out
    assert "Skipping restore after scripts (none configured)" in out


def test_sudo_restore_exit_code_survives_a_broken_stderr(tmp_path, monkeypatch):
    class FakePopen:
        def __init__(self, cmd):
            pass

        def wait(self):
            # The child ran; now the parent's stderr is gone
            broken = io.StringIO()
            broken.close()
            monkeypatch.setattr(snap.sys, "stderr", broken)
            return 3

    monkeypatch.setattr(snap.subprocess, "Popen", FakePopen)

    with pytest.raises(SystemExit) as exit_info:
        snap.sudo_restore(argparse.Namespace(root_host=None), tmp_path, tmp_path, {}, {})

    assert exit_info.value.code == 3


def test_stdout_is_flushed_before_stderr_only_when_they_share(tmp_path, monkeypatch):
    with open(tmp_path / "out", "w") as out, open(tmp_path / "err", "w") as err:
        monkeypatch.setattr(snap.sys, "stdout", out)
        assert not snap.shares_stdout(err)
        with open(tmp_path / "out", "a") as same:
            assert snap.shares_stdout(same)


# --- Fix A1: failures exit non-zero --- #


def fake_remote_tools(env):
    """Put ssh and rsync stand-ins first on PATH that record each call and fail."""
    calls = env.tmp / "remote-calls"
    for tool in ("ssh", "rsync"):
        script = env.tmp / "bin" / tool
        script.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit 255\n')
        script.chmod(0o755)
    return calls


def snapshot_dir(env):
    """Return the one snapshot under the snap root's captures directory."""
    [snapshot] = env.root.glob("captures/*/*/*")
    return snapshot


def test_e2e_capture_fails_when_an_archive_cannot_be_created(env):
    bad_root = unexpandable_root()
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"]), "broken": (bad_root, [".x"])})
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run("capture", "-r", str(env.root), extra_env={"TMPDIR": str(tmpdir)})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: Cannot create broken.tar.gz: " in result.stderr
    assert result.stderr.rstrip().endswith(
        "Error: Capture failed: 1 of 2 archives could not be created; no snapshot was saved"
    )
    assert "Snapshot saved" not in result.stdout
    assert not list(env.root.glob("captures/*/*/*"))
    assert list(tmpdir.iterdir()) == []  # the staging dir is gone

    # A dry run stops at the same point, in the present tense, and ends like any dry run
    result = env.run("capture", "-r", str(env.root), "--dry-run")
    assert result.returncode == 0
    assert "Capture would fail: 1 of 2 archives cannot be created" in result.stderr
    assert result.stdout.rstrip().endswith(
        "Dry run completed with 2 errors; no changes were made"
    )
    assert "Writing snapshot.toml" not in result.stdout


def test_e2e_migrate_capture_failure_stops_before_the_host(env):
    bad_root = unexpandable_root()
    calls = fake_remote_tools(env)
    write_files(env.home, {".zshrc": "v1"})
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        f'[capture.tar.broken]\nroot = "{bad_root}"\nfiles = [".x"]\n\n'
        '[restore.tar]\narchives = ["*"]\n'
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run(
        "migrate", "-r", str(env.root), "--to", "user@target.host",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Capture failed: 1 of 2 archives could not be created" in result.stderr
    assert not calls.exists()  # nothing was sent to the host
    assert list(tmpdir.iterdir()) == []


def capture_without_backups(env):
    """Capture a dotfiles archive and a system archive whose restore has no backups."""
    system = env.tmp / "sysroot"
    write_files(env.home, {".zshrc": "v1"})
    write_files(system, {"etc/app": "v1"})
    write_configs(
        env.root,
        {"dotfiles": ("$HOME", [".zshrc"]), "system": (str(system), ["etc"])},
        rollback="",
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    return system


def test_e2e_restore_without_backups_stops_on_an_extraction_error(env):
    system = capture_without_backups(env)
    write_files(env.home, {".zshrc": "edited"})
    (system / "etc" / "app").unlink()
    (system / "etc" / "app" / "sub").mkdir(parents=True)  # a file can't replace a dir

    result = env.run("restore", "-r", str(env.root), "--run-scripts", answers="y\ny\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: Cannot extract system.tar.gz: " in result.stderr
    assert "  1 of 2 archives extracted" in result.stdout
    assert (
        "Error: 1 of 2 archives failed to restore: system.tar.gz\n"
        "  restored: dotfiles.tar.gz\n"
        "  not run: symlinks, after scripts\n"
    ) in result.stderr
    assert "Restore completed" not in result.stdout
    assert (env.home / ".zshrc").read_text() == "v1"


def test_e2e_restore_without_backups_stops_on_an_unreadable_archive(env):
    capture_without_backups(env)
    write_files(env.home, {".zshrc": "edited"})

    # Replace the archive with junk that still matches its checksum
    snapshot = snapshot_dir(env)
    archive = snapshot / "system.tar.gz"
    old_digest = snap.sha256_file_digest(archive)
    archive.write_bytes(b"not a tar archive")
    toml_path = snapshot / "snapshot.toml"
    toml_path.write_text(
        toml_path.read_text().replace(old_digest, snap.sha256_file_digest(archive))
    )

    result = env.run("restore", "-r", str(env.root), answers="y\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: Cannot restore system.tar.gz: " in result.stderr
    assert "Error: 1 of 2 archives failed to restore: system.tar.gz" in result.stderr
    assert (env.home / ".zshrc").read_text() == "v1"


def test_e2e_declined_prompts_are_not_failures(env):
    capture_without_backups(env)
    write_files(env.home, {".zshrc": "edited"})

    for answers in ("n\nn\n", None):  # declined, then no answer at all
        result = env.run("restore", "-r", str(env.root), answers=answers)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Restore completed: no archives restored" in result.stdout
    assert (env.home / ".zshrc").read_text() == "edited"


def test_archive_confirm_tells_a_bad_archive_from_a_declined_one(tmp_path, monkeypatch):
    write_files(tmp_path / "src", {".zshrc": "x"})
    good = make_archive(tmp_path, "good", tmp_path / "src", [".zshrc"])
    bad = tmp_path / "archives" / "bad.tar.gz"
    bad.write_bytes(b"junk")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert snap.archive_confirm(bad, root=str(tmp_path)) is None
    assert snap.archive_confirm(good, root=str(tmp_path)) is False

    restored, failed = [], []
    assert snap.extract_archives(
        [good, bad], root_map={"good": str(tmp_path), "bad": str(tmp_path)},
        restored=restored, failed=failed,
    ) == 0
    assert (restored, failed) == ([], [bad])


# --- Fix A2: symlinks only for the archives restored --- #


def capture_linked_archives(env):
    """Capture two archives whose snapshot.toml tables each set a 'link'."""
    write_files(env.home, {".zshrc": "v1", "work/notes.txt": "n"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"]), "work": ("$HOME/work", ["."])})
    capture_toml = env.root / "configs" / "capture.toml"
    text = capture_toml.read_text()
    text = text.replace('[tar.dotfiles]\n', '[tar.dotfiles]\nlink = "$HOME/dotfiles-link"\n')
    text = text.replace('[tar.work]\n', '[tar.work]\nlink = "$HOME/work-link"\n')
    capture_toml.write_text(text)
    assert env.run("capture", "-r", str(env.root)).returncode == 0


def test_e2e_symlinks_only_for_selected_archives(env):
    capture_linked_archives(env)
    (env.root / "configs" / "restore.toml").write_text('[tar]\narchives = ["dotfiles"]\n')

    result = env.run("restore", "-r", str(env.root), "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dotfiles-link ->" in result.stdout
    assert "work-link" not in result.stdout

    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / "dotfiles-link").is_symlink()
    assert not os.path.lexists(env.home / "work-link")
    assert "1 archive restored, 1 symlink created" in result.stdout


def test_e2e_no_symlinks_for_declined_or_unselected_archives(env):
    capture_linked_archives(env)

    # Declined at the prompt: no link
    result = env.run("restore", "-r", str(env.root), "--disable-rollback", answers="y\nn\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / "dotfiles-link").is_symlink()
    assert not os.path.lexists(env.home / "work-link")

    # No archive selected: no links at all
    (env.home / "dotfiles-link").unlink()
    (env.root / "configs" / "restore.toml").write_text("[tar]\narchives = []\n")
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Creating symlinks" not in result.stdout
    assert not os.path.lexists(env.home / "dotfiles-link")


@unprivileged
def test_no_sudo_for_links_of_unselected_archives(tmp_path):
    home = tmp_path / "home"
    locked = tmp_path / "locked"
    write_files(home, {"a.txt": "a"})
    locked.mkdir(mode=0o555)
    config = {"tar": {
        "cat": {"root": str(home)},
        "other": {"root": str(home), "link": str(locked / "link")},
    }}
    try:
        assert not needs_sudo(tmp_path, home, ["a.txt"], snapshot_config=config)
    finally:
        locked.chmod(0o755)


# --- Fix A3: one backup rotation per root per run, one subdirectory per archive --- #


def test_restore_category_backs_up_archives_sharing_a_root(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a.txt": "new", "b.txt": "new"})
    write_files(home, {"a.txt": "old a", "b.txt": "old b"})
    write_files(tmp_path / "home.bak", {"earlier.txt": "earlier run"})
    first = make_archive(tmp_path, "first", src, ["a.txt"])
    second = make_archive(tmp_path, "second", src, ["b.txt"])

    rotated = set()
    assert snap.restore_category(first, str(home), ".bak", rotated=rotated)
    assert snap.restore_category(second, str(home), ".bak", rotated=rotated)

    # One rotation for the root; each archive has its own subdirectory
    [previous] = tmp_path.glob("home.bak_*")
    assert list_tree(previous) == ["earlier.txt"]
    assert list_tree(tmp_path / "home.bak") == ["first", "first/a.txt", "second", "second/b.txt"]
    assert (tmp_path / "home.bak" / "first" / "a.txt").read_text() == "old a"
    assert (tmp_path / "home.bak" / "second" / "b.txt").read_text() == "old b"


def test_backup_subdirectories_get_the_ancestor_owner_as_root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    write_files(src, {"fonts/a.ttf": "font"})
    archive = make_archive(tmp_path, "local", src, ["fonts"])
    calls = record_chown(monkeypatch)
    home_owner = (home.stat().st_uid, home.stat().st_gid, True)

    assert snap.restore_category(archive, str(home / ".local" / "share"), ".bak")

    assert calls[home / ".local/share.bak/local"] == home_owner


def test_e2e_archives_sharing_a_root_keep_their_backups(env):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"]), "vim": ("$HOME", [".vimrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    backups = env.tmp / "home.bak"

    write_files(env.home, {".zshrc": "edit 1", ".vimrc": "edit 1"})
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"dotfiles.tar.gz (root: {env.home}, backup: {backups}/dotfiles)" in result.stdout
    assert f"vim.tar.gz (root: {env.home}, backup: {backups}/vim)" in result.stdout
    assert (backups / "dotfiles" / ".zshrc").read_text() == "edit 1"
    assert (backups / "vim" / ".vimrc").read_text() == "edit 1"

    # The next run moves the whole backup directory aside once, with both archives in it
    write_files(env.home, {".zshrc": "edit 2", ".vimrc": "edit 2"})
    result = env.run("restore", "-r", str(env.root), "--verbose")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("rotate: ") == 1
    [previous] = env.tmp.glob("home.bak_*")
    assert (previous / "dotfiles" / ".zshrc").read_text() == "edit 1"
    assert (previous / "vim" / ".vimrc").read_text() == "edit 1"
    assert (backups / "dotfiles" / ".zshrc").read_text() == "edit 2"
    assert (backups / "vim" / ".vimrc").read_text() == "edit 2"


# --- Fix A4: restore only the archives snapshot.toml lists --- #


def test_archive_select_matches_only_available_archives(tmp_path):
    available = [tmp_path / "dotfiles.tar.gz", tmp_path / "ssh-keys.tar.gz"]
    (tmp_path / "unlisted.tar.gz").write_bytes(b"")

    assert snap.archive_select(available, []) == (available, [])
    assert snap.archive_select(available, ["unlisted", "ssh*", "dotfiles", "ssh-keys"]) == (
        [available[1], available[0]],
        ["unlisted"],
    )


def test_snapshot_archives_lists_existing_tables_only(tmp_path):
    snapshot = tmp_path / "snapshot"
    for rel in ("dotfiles.tar.gz", "unlisted.tar.gz", "sub/x.tar.gz", "../outside.tar.gz"):
        (snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / rel).write_bytes(b"")
    config = {"tar": {"dotfiles": {}, "missing": {}, "sub/x": {}, "../outside": {}}}

    assert snap.snapshot_archives(snapshot, config, ".tar.gz") == [snapshot / "dotfiles.tar.gz"]


def test_archive_category_strips_only_the_tar_extension():
    assert snap.archive_category(Path("dotfiles.tar.gz")) == "dotfiles"
    assert snap.archive_category(Path("my.tar.files.tar.xz")) == "my.tar.files"
    assert snap.archive_category(Path("plain.tar")) == "plain"


@pytest.mark.parametrize("archives", ['["*"]', '["evil", "dotfiles"]'])
def test_e2e_restore_ignores_archives_not_in_snapshot_toml(env, archives):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])}, rollback="")
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    # An archive dropped into the snapshot dir, listed nowhere, with no root
    evil_src = env.tmp / "evil-src"
    write_files(evil_src, {"evil.txt": "pwned"})
    with tarfile.open(snapshot_dir(env) / "evil.tar.gz", "w:gz") as tar:
        tar.add(evil_src / "evil.txt", arcname="evil.txt")
    (env.root / "configs" / "restore.toml").write_text(f"[tar]\narchives = {archives}\n")

    result = env.run("restore", "-r", str(env.root), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "evil.tar.gz" not in result.stdout
    assert "Restoring 1 archive without backups..." in result.stdout
    if "evil" in archives:
        assert "archives pattern 'evil' matches no archive in the snapshot" in result.stderr


# --- Fix A5: capture -t uses that config for the archives too --- #


def test_e2e_capture_uses_the_config_given_with_t(env):
    write_files(env.home, {".zshrc": "v1", ".other": "o"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    (env.root / "scripts").mkdir()
    (env.root / "scripts" / "after.sh").write_text('echo "$PWD" > "$AFTER_MARKER"\n')
    (env.root / "configs" / "other.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[tar.other]\nroot = "$HOME"\nfiles = [".other"]\n\n'
        '[scripts]\nafter = ["scripts/after.sh"]\n'
    )
    marker = env.tmp / "after-ran"

    result = env.run(
        "capture", "-r", str(env.root), "-t", "configs/other.toml", "--run-scripts",
        extra_env={"AFTER_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = snapshot_dir(env)
    assert sorted(p.name for p in snapshot.iterdir()) == ["other.tar.gz", "snapshot.toml"]
    # The after script ran in the staged snapshot, before it moved to captures/
    assert Path(marker.read_text().strip()).name == snapshot.name


def test_capture_after_scripts_use_the_capture_config(tmp_path, monkeypatch):
    # A migration's capture config comes from migrate.toml's [capture.*] tables, not a file
    ran = []
    monkeypatch.setattr(snap, "__dry_run__", True)
    monkeypatch.setattr(snap, "run_scripts", lambda root, config, when, **kw: ran.append(
        (when, config.get("scripts"))
    ))
    config = {"tar": {}, "scripts": {"after": ["scripts/dump.sh"]}}
    args = argparse.Namespace(run_scripts=True, config_toml="configs/migrate.toml")

    snap.capture(args, tmp_path, tmp_path / "out", config)

    assert ran == [("before", config["scripts"]), ("after", config["scripts"])]


# --- Fix A6: TOML files are written with toml_dumps --- #


def test_toml_dumps_writes_nested_tables():
    config = {
        "tarball": {"compress": "xz", "checksum": "sha256"},
        "tar": {
            "ssh-keys": {"root": "$HOME", "dirs": [".ssh"]},
            "dot.files": {"root": 'C:\\Users\\"me"', "link": "~/l\x7f🚀"},
            "empty": {},
        },
        "scripts": {"after": ["scripts/a.sh"], "count": 2, "on": True},
    }

    text = snap.toml_dumps(config)

    assert '[tar."ssh-keys"]\n' in text
    assert '[tar."dot.files"]\n' in text
    assert "[tar.empty]\n" in text
    assert "\n[tar]\n" not in text  # a table holding only tables needs no header
    assert snap.tomllib.loads(text) == config
    assert snap.toml_dumps({}) == ""


def old_snapshot_toml(compress, roll_ext, tables):
    """The snapshot.toml text generate_snapshot_toml built by hand before toml_dumps."""
    lines = ["#", "# Capture Configuration TOML", "#", "[tarball]"]
    if compress != "gzip":
        lines += [f'compress = "{compress}"  # tar compression type']
    if roll_ext:
        lines += [f'rollback = "{roll_ext}"  # rollback intermediate extension']
    lines += ['checksum = "sha256"  # checksum digest type', ""]
    for name, (root, link, checksum) in sorted(tables.items()):
        lines += [f"[tar.{name}]", f'root = "{root}"']
        if link:
            lines += [f'link = "{link}"']
        lines += [f'checksum = "{checksum}"', ""]
    return "\n".join(lines)


@pytest.mark.parametrize("compress, roll_ext", [("gzip", ".bak"), ("xz", None), ("", "~")])
def test_snapshot_toml_reads_like_the_old_format(tmp_path, compress, roll_ext):
    src = tmp_path / "src"
    write_files(src, {".zshrc": "z", ".ssh/id": "k"})
    ext, _ = snap.COMPRESS_MAP[compress]
    outdir = tmp_path / "out"
    outdir.mkdir()
    for name, paths in (("dotfiles", [".zshrc"]), ("ssh-keys", [".ssh"])):
        snap.archive_create(name, str(src), paths, outdir, compress)
    meta = {"dotfiles": {"root": "$HOME", "link": None},
            "ssh-keys": {"root": str(src), "link": "$HOME/keys"}}

    toml_path, checksum = snap.generate_snapshot_toml(outdir, compress, meta, roll_ext)

    digests = {name: snap.sha256_file_digest(outdir / f"{name}{ext}")
               for name in ("dotfiles", "ssh-keys")}
    old = old_snapshot_toml(compress, roll_ext, {
        "dotfiles": ("$HOME", None, digests["dotfiles"]),
        "ssh-keys": (str(src), "$HOME/keys", digests["ssh-keys"]),
    })
    with open(toml_path, "rb") as f:
        assert snap.tomllib.load(f) == snap.tomllib.loads(old)
    # The snapshot id is still the digest of the archive digests, in name order
    assert checksum == snap.sha256_digest(
        (digests["dotfiles"] + digests["ssh-keys"]).encode("utf-8")
    )


def test_write_capture_toml_keeps_every_value(tmp_path):
    config = {
        "tarball": {"compress": "gzip", "rollback": ".bak"},
        "tar": {"ssh-keys": {"root": 'C:\\"quoted"', "dirs": [".ssh"], "files": [], "n": 3}},
        "scripts": {"after": ["scripts/dump.sh"]},
    }

    path = snap.write_capture_toml(config)
    try:
        with open(path, "rb") as f:
            assert snap.tomllib.load(f) == config
    finally:
        path.unlink()


# --- Fix A7: tqdm is optional --- #


def test_dtqdm_without_tqdm_is_a_no_op_bar(monkeypatch, capsys):
    monkeypatch.setattr(snap, "tqdm", None)
    monkeypatch.setattr(snap, "__dry_run__", False)

    with snap.dtqdm(2, "Creating archives", autorefresh=True) as pbar:
        pbar.update(1)
        pbar.write("hello")
    snap.say("after the bar")

    assert capsys.readouterr().out == "hello\n  after the bar\n"


def test_e2e_capture_and_sudo_restore_run_without_tqdm(env):
    no_tqdm = env.tmp / "no-tqdm"
    no_tqdm.mkdir()
    (no_tqdm / "tqdm.py").write_text('raise ImportError("tqdm is not installed")\n')
    root = env.tmp / "force-sudo-root"
    write_files(root, {"f.txt": "v1"})
    write_configs(env.root, {"system": (str(root), ["f.txt"])})
    no_tqdm_env = {"PYTHONPATH": str(no_tqdm)}

    result = env.run("capture", "-r", str(env.root), extra_env=no_tqdm_env)
    assert result.returncode == 0, result.stdout + result.stderr
    write_files(root, {"f.txt": "edited"})

    # The sudo child also runs without tqdm
    site = env.tmp / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env.sudo.write_text(
        '#!/bin/sh\necho "$@" > "$SUDO_MARKER"\n'
        "export SNAP_TEST_FAKE_ROOT=1\nunset SNAP_TEST_FORCE_SUDO\nexec \"$@\"\n"
    )
    result = env.run("restore", "-r", str(env.root), extra_env={
        "SNAP_TEST_FORCE_SUDO": "1",
        "PYTHONPATH": f"{site}{os.pathsep}{no_tqdm}",
    })

    assert result.returncode == 0, result.stdout + result.stderr
    assert env.sudo_marker.exists()
    assert "Starting restore as root" in result.stdout
    assert (root / "f.txt").read_text() == "v1"


# --- Fix A8: no rsync --info=progress2 under --verbose --- #


def test_rsync_run_verbose_adds_no_progress_option(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(snap.subprocess, "run", fake_run)
    monkeypatch.setattr(snap, "__dry_run__", False)
    monkeypatch.setattr(snap, "__verbose__", True)

    snap.rsync_run(str(tmp_path / "f"), "host:/tmp/work/")

    [cmd] = commands
    assert cmd[:2] == ["rsync", "-az"]
    assert not any(arg.startswith("--info") for arg in cmd)


# --- Remote hosts, emulated on this machine --- #

# ssh and rsync stand-ins. Each host (the part after 'user@') is a pair of local dirs,
# <hosts>/<host>/home and <hosts>/<host>/tmp; nothing ever leaves this machine
FAKE_HOST_TOOLS = r'''
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HOSTS = Path(os.environ["FAKE_HOSTS"])


def log(line):
    with open(HOSTS / "calls.log", "a") as f:
        f.write(line.replace("\n", "\\n") + "\n")


def host_dirs(host):
    name = host.rpartition("@")[2]
    home, tmp = HOSTS / name / "home", HOSTS / name / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return name, home, tmp


def refused(name):
    if name in os.environ.get("FAKE_HOSTS_DOWN", "").split():
        print(f"ssh: connect to host {name} port 22: Connection refused", file=sys.stderr)
        return True
    return False


def ssh(args):
    # ssh [-o option]... [-t] host command...: bash -c runs the command in the host's home
    while args and args[0].startswith("-"):
        if args.pop(0) == "-o":
            args.pop(0)
    host, command = args[0], " ".join(args[1:])
    log(f"ssh {host} {command}")
    name, home, tmp = host_dirs(host)
    if refused(name):
        return 255
    fail = os.environ.get("FAKE_SSH_FAIL")
    if fail and fail in command:
        print(f"fake ssh: '{fail}' fails on {name}", file=sys.stderr)
        return int(os.environ.get("FAKE_SSH_FAIL_STATUS", "1"))
    env = dict(os.environ, HOME=str(home), TMPDIR=str(tmp), FAKE_HOST=name)
    env["PATH"] = os.pathsep.join([str(HOSTS / "bin"), env["PATH"]])
    return subprocess.run(["bash", "-c", command], cwd=home, env=env).returncode


def rsync(args):
    # host:path becomes the host's home + path (relative) or the path itself (absolute)
    log("rsync " + shlex.join(args))
    mapped = []
    for arg in args:
        match = None if arg.startswith("-") else re.fullmatch(r"([^/:]+):(.*)", arg, re.S)
        if match:
            name, home, _ = host_dirs(match.group(1))
            if refused(name):
                return 255
            path = match.group(2)
            arg = path if path.startswith("/") else os.path.join(home, path)
        elif not arg.startswith("-") and ("::" in arg or arg.startswith("rsync:")):
            print(f"fake rsync: refusing '{arg}'", file=sys.stderr)
            return 1
        mapped.append(arg)
    # --rsh=false: the real rsync can never open a connection of its own
    real = os.environ["FAKE_REAL_RSYNC"]
    return subprocess.run([real, "--rsh=false", *mapped]).returncode


if __name__ == "__main__":
    tool, *args = sys.argv[1:]
    sys.exit(ssh(args) if tool == "ssh" else rsync(args))
'''

# A host's sudo: logs the call, then runs the command as this user, which snap.py takes
# for root (SITECUSTOMIZE). A nested sudo fails, so a restore can never loop
FAKE_HOST_SUDO = """\
echo "sudo $FAKE_HOST $*" >> "$FAKE_HOSTS/calls.log"
if [ -n "$FAKE_SUDO_ACTIVE" ]; then
    echo "fake sudo: nested sudo" >&2
    exit 1
fi
export FAKE_SUDO_ACTIVE=1 SNAP_TEST_FAKE_ROOT=1
export PYTHONPATH="$FAKE_HOSTS/site${PYTHONPATH:+:$PYTHONPATH}"
exec "$@"
"""


def write_script(path, body):
    """Write an executable /bin/sh script."""
    path.write_text("#!/bin/sh\n" + body.rstrip("\n") + "\n")
    path.chmod(0o755)


@pytest.fixture
def hosts(env):
    """Emulate remote hosts on this machine, with fake ssh and rsync first on PATH.

    ssh runs its command with bash -c in <tmp>/hosts/<host>/home, with HOME there,
    TMPDIR=<tmp>/hosts/<host>/tmp, and python3 (the test's interpreter) and a fake sudo
    first on PATH. rsync maps host:path to that home (a relative path) or to the path
    itself (an absolute one), then runs the real rsync locally. Each ssh, rsync and host
    sudo call is logged. With FAKE_SSH_FAIL=<text>, ssh fails (exit 1) for commands that
    contain the text; FAKE_HOSTS_DOWN lists hosts that refuse connections (exit 255).
    """
    real_rsync = shutil.which("rsync")  # looked up before the fakes are on PATH
    if not real_rsync or not shutil.which("bash"):
        pytest.skip("rsync and bash are needed to emulate remote hosts")

    base = env.tmp / "hosts"
    (base / "bin").mkdir(parents=True)
    (base / "site").mkdir()
    (base / "site" / "sitecustomize.py").write_text(SITECUSTOMIZE)
    (base / "fakehost.py").write_text(FAKE_HOST_TOOLS)
    for tool in ("ssh", "rsync"):
        write_script(
            env.tmp / "bin" / tool, f'exec "{sys.executable}" "{base / "fakehost.py"}" {tool} "$@"'
        )
    write_script(base / "bin" / "python3", f'exec "{sys.executable}" "$@"')
    write_script(base / "bin" / "sudo", FAKE_HOST_SUDO)
    env.env.update(FAKE_HOSTS=str(base), FAKE_REAL_RSYNC=real_rsync)

    # Never fall through to the real ssh or rsync
    for tool in ("ssh", "rsync"):
        if shutil.which(tool, path=env.env["PATH"]) != str(env.tmp / "bin" / tool):
            pytest.skip(f"fake {tool} is not first on PATH")

    log = base / "calls.log"

    def calls():
        """Return the logged calls, one 'ssh <host> <command>' or 'rsync <args>' each."""
        return log.read_text().splitlines() if log.exists() else []

    def ssh_commands(host):
        """Return the commands ssh ran on host, in order."""
        prefix = f"ssh {host} "
        return [line[len(prefix):] for line in calls() if line.startswith(prefix)]

    def remote_args():
        """Return every path or command a host was given: ssh commands, rsync host:paths."""
        found = []
        for line in calls():
            tool, _, rest = line.partition(" ")
            if tool == "ssh":
                found.append(rest.partition(" ")[2])
            elif tool == "rsync":
                found += [arg for arg in shlex.split(rest) if re.match(r"[^/:-][^/:]*:", arg)]
        return found

    def clear():
        if log.exists():
            log.unlink()

    return argparse.Namespace(
        base=base,
        home=lambda host: base / host / "home",
        tmp=lambda host: base / host / "tmp",
        calls=calls,
        ssh_commands=ssh_commands,
        remote_args=remote_args,
        clear=clear,
    )


def remote_snap_root(hosts, host, categories, restore_toml='[tar]\narchives = ["*"]\n'):
    """Give an emulated host a .snap in its home with capture.toml and restore.toml."""
    root = hosts.home(host) / ".snap"
    (root / "configs").mkdir(parents=True)
    write_configs(root, categories)
    (root / "configs" / "restore.toml").write_text(restore_toml)
    return root


def work_dir_of(commands, purpose):
    """Return the work directory the 'mkdir -p' after mktemp created, from ssh commands."""
    [mkdir] = [c for c in commands if c.startswith("mkdir -p ") and f"/snap-{purpose}." in c]
    first = shlex.split(mkdir)[2]
    return first.rsplit("/", 1)[0]


# --- Fix B1: remote paths are relative to the host's login directory --- #


@pytest.mark.parametrize("path, expected", [
    ("~", "."),
    ("~/", "."),
    ("~/snaps", "snaps"),
    ("~/snaps/2026/", "snaps/2026"),
    (".snap/captures", ".snap/captures"),
    ("/srv/snap", "/srv/snap"),
    ("~other/x", "~other/x"),
    (Path("~/snap"), "snap"),
])
def test_normalize_remote_path(path, expected):
    assert str(snap.normalize_remote_path(path)) == expected


def test_remote_snap_root_is_the_r_path_only_on_its_host():
    args = argparse.Namespace(root_host="user@web-01", root_path=snap.PurePosixPath("/srv/x"))
    assert str(snap.remote_snap_root(args, "user@web-01")) == "/srv/x"
    assert str(snap.remote_snap_root(args, "user@web-02")) == ".snap"
    assert str(snap.remote_snap_root(argparse.Namespace(), "web-01")) == ".snap"


def test_e2e_remote_paths_are_relative_to_the_login_directory(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    remote = hosts.home("web-01")

    # host:~/path is relative to the login directory
    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01:~/snaps")
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = remote.glob("snaps/*")
    assert (snapshot / "dotfiles.tar.gz").is_file()
    assert "  To:   user@web-01:snaps\n" in result.stdout
    assert f"Snapshot saved to user@web-01:snaps/{snapshot.name}\n" in result.stdout

    # A host with no path gets .snap/captures/YYYY/MM-DD there, never the local root
    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01")
    assert result.returncode == 0, result.stdout + result.stderr
    [default] = remote.glob(".snap/captures/*/*/*")
    assert (default / "snapshot.toml").is_file()
    assert f"To:   user@web-01:{default.parent.relative_to(remote)}\n" in result.stdout

    # restore --from host:~/path pulls from the same place
    write_files(env.home, {".zshrc": "edited"})
    result = env.run(
        "restore", "-r", str(env.root), "--from", f"user@web-01:~/snaps/{snapshot.name}"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"  From: user@web-01:snaps/{snapshot.name}\n" in result.stdout
    assert (env.home / ".zshrc").read_text() == "v1"

    # No host was given a local path or a '~' to expand
    given = hosts.remote_args()
    assert given
    for arg in given:
        assert str(env.root) not in arg and str(env.home) not in arg and "~" not in arg


# --- Fix B2: work directories come from mktemp and are always removed --- #


def fake_ssh_runs(monkeypatch, outputs=None):
    """Replace subprocess.run: record (argv, timeout) and answer mktemp like a host."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("timeout")))
        command = cmd[-1]
        stdout = ""
        for text, output in (outputs or {}).items():
            if text in command:
                stdout = output
        if command.startswith("mktemp -d"):
            purpose = re.search(r"snap-(\w+)\.X", command).group(1)
            stdout = stdout or f"/tmp/snap-{purpose}.Ab3dEf9h\n"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(snap.subprocess, "run", run)
    monkeypatch.setattr(snap, "__dry_run__", False)
    monkeypatch.setattr(snap, "__verbose__", False)
    return calls


def test_remote_workdir_uses_mktemp_and_one_mkdir(monkeypatch, capsys):
    calls = fake_ssh_runs(monkeypatch)

    work_dir = snap.remote_workdir("user@web-01", "restore", ["configs", "scripts", "snapshot"])

    assert str(work_dir) == "/tmp/snap-restore.Ab3dEf9h"
    assert [cmd[-2:] for cmd, _ in calls] == [
        ["user@web-01", 'mktemp -d "${TMPDIR:-/tmp}/snap-restore.XXXXXXXX"'],
        ["user@web-01", "mkdir -p /tmp/snap-restore.Ab3dEf9h/configs "
                        "/tmp/snap-restore.Ab3dEf9h/scripts /tmp/snap-restore.Ab3dEf9h/snapshot"],
    ]
    assert capsys.readouterr().out == "\nCreating work directory on user@web-01...\n"


@pytest.mark.parametrize("output", ["", "/tmp/other-dir\n", "snap-restore.x\n"])
def test_remote_workdir_never_uses_an_odd_mktemp_path(monkeypatch, capsys, output):
    calls = fake_ssh_runs(monkeypatch, {"mktemp": output or "\n"})

    with pytest.raises(SystemExit):
        snap.remote_workdir("user@web-01", "restore", ["configs"])

    # Nothing but mktemp ran: no mkdir, and no rm -rf of a path mktemp didn't make
    assert [cmd[-1].split()[0] for cmd, _ in calls] == ["mktemp"]
    assert "Cannot create a work directory on user@web-01" in capsys.readouterr().err


def test_remote_workdir_is_removed_when_its_layout_fails(monkeypatch):
    calls = fake_ssh_runs(monkeypatch)
    real_run = snap.subprocess.run

    def run(cmd, **kwargs):
        if cmd[-1].startswith("mkdir"):
            calls.append((cmd, kwargs.get("timeout")))
            raise subprocess.CalledProcessError(1, cmd, "", "mkdir: No space left\n")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(snap.subprocess, "run", run)

    with pytest.raises(SystemExit):
        snap.remote_workdir("user@web-01", "capture", ["configs"])
    assert calls[-1][0][-1] == "rm -rf /tmp/snap-capture.Ab3dEf9h"


def test_remote_capture_removes_its_work_dir_on_ctrl_c(tmp_path, monkeypatch, capsys):
    (tmp_path / "configs").mkdir()
    write_configs(tmp_path, {"dotfiles": ("$HOME", [".zshrc"])})
    calls = fake_ssh_runs(monkeypatch)

    def interrupted(transfers):
        raise KeyboardInterrupt

    monkeypatch.setattr(snap, "rsync_parallel", interrupted)
    args = argparse.Namespace(config_toml=None, run_scripts=False)

    with pytest.raises(KeyboardInterrupt):
        snap.remote_capture(args, tmp_path, "user@web-01")

    assert calls[-1][0][-1] == "rm -rf /tmp/snap-capture.Ab3dEf9h"
    # The cleanup after a failure prints nothing of its own
    assert "Removing work directory" not in capsys.readouterr().out


@pytest.mark.parametrize("command, purpose, fail", [
    (["capture", "--from", "user@web-01"], "capture", "snap.py capture"),
    (["restore", "--to", "user@web-01"], "restore", "snap.py restore"),
    (["migrate", "--to", "user@web-01"], "restore", "snap.py restore"),
])
def test_e2e_remote_work_dir_is_removed_when_the_remote_step_fails(
    env, hosts, command, purpose, fail
):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[restore.tar]\narchives = ["*"]\n'
    )
    if command[0] == "restore":
        assert env.run("capture", "-r", str(env.root)).returncode == 0
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run(
        *command, "-r", str(env.root),
        extra_env={"FAKE_SSH_FAIL": fail, "TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"fake ssh: '{fail}' fails on web-01" in result.stderr
    # The run still ends with its own Error block
    assert result.stderr.rstrip().splitlines()[-1].startswith("Error: ")
    assert "Removing work directory" not in result.stdout

    # mktemp made a private work dir on the host; it is gone, and so are local temp dirs
    commands = hosts.ssh_commands("user@web-01")
    assert commands[0] == f'mktemp -d "${{TMPDIR:-/tmp}}/snap-{purpose}.XXXXXXXX"'
    work_dir = work_dir_of(commands, purpose)
    assert Path(work_dir).parent == hosts.tmp("web-01")
    assert commands[-1] == f"rm -rf {work_dir}"
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(tmpdir.iterdir()) == []


# --- Fix B5: the latest remote snapshot has the newest snapshot.toml --- #


def test_e2e_latest_remote_snapshot_has_the_newest_snapshot_toml(env, hosts):
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    snapshots = {}
    for content in ("older", "newer"):
        write_files(env.home, {".zshrc": content})
        result = env.run("capture", "-r", str(env.root), "--to", str(env.tmp / content))
        assert result.returncode == 0, result.stdout + result.stderr
        [snapshots[content]] = (env.tmp / content).iterdir()

    # The newer snapshot sits in the older-looking date dir; stray entries are newer still
    captures = hosts.home("web-01") / ".snap" / "captures"
    newer = captures / "2025" / "01-01" / snapshots["newer"].name
    older = captures / "2026" / "12-31" / snapshots["older"].name
    shutil.copytree(snapshots["newer"], newer)
    shutil.copytree(snapshots["older"], older)
    write_files(captures, {
        "2026/12-31/stray/notes.txt": "not a snapshot",
        "notes/12-31/abc1234/snapshot.toml": "not a date dir",
        "2026/1231/abc1234/snapshot.toml": "not a date dir",
        "2026/12-31/snapshot.toml": "not in a snapshot dir",
    })
    now = snap.datetime.now().timestamp()
    os.utime(older / "snapshot.toml", (now - 3600, now - 3600))
    os.utime(newer / "snapshot.toml", (now - 60, now - 60))
    write_files(env.home, {".zshrc": "edited"})

    # A dry run looks too (a read-only query), and shows the copy it would make
    result = env.run("restore", "-r", str(env.root), "--from", "user@web-01", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"latest: 2025/01-01/{newer.name}\n" in result.stdout
    assert f"user@web-01:.snap/captures/2025/01-01/{newer.name}/" in result.stdout
    assert (env.home / ".zshrc").read_text() == "edited"

    result = env.run("restore", "-r", str(env.root), "--from", "user@web-01")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  From: user@web-01:.snap/captures (latest)\n" in result.stdout
    assert f"latest: 2025/01-01/{newer.name}\n" in result.stdout
    assert (env.home / ".zshrc").read_text() == "newer"

    # The listing is one portable ls -t under sh
    [listing] = set(hosts.ssh_commands("user@web-01"))
    assert listing == (
        "sh -c 'ls -1t -- .snap/captures/[0-9][0-9][0-9][0-9]/[0-9][0-9]-[0-9][0-9]/*/"
        "snapshot.toml 2>/dev/null | head -1'"
    )


def test_e2e_remote_without_snapshots(env, hosts):
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(hosts.home("web-01") / ".snap" / "captures" / "2026", {"README": "x"})

    result = env.run("restore", "-r", str(env.root), "--from", "user@web-01")
    assert result.returncode == 1
    assert result.stderr.endswith("Error: No snapshots found in user@web-01:.snap/captures\n")

    result = env.run(
        "restore", "-r", str(env.root), "--from", "user@web-01",
        extra_env={"FAKE_HOSTS_DOWN": "web-01"},
    )
    assert result.returncode == 1
    assert (
        "  ssh: connect to host web-01 port 22: Connection refused\n"
        "Error: Finding the latest snapshot failed on user@web-01: ssh error (exit status 255)\n"
    ) in result.stderr


# --- Fix B6: -r host[:path] reads configs and scripts from the host --- #


def test_e2e_capture_and_restore_with_a_remote_snap_root(env, hosts):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    remote_root = remote_snap_root(
        hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"]), "vim": ("$HOME", [".vimrc"])},
        restore_toml='[tar]\narchives = ["dotfiles"]\n',
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()
    local_tmp = {"TMPDIR": str(tmpdir)}

    # A dry run reads the host's real capture.toml, and writes nothing there
    result = env.run("capture", "-r", "user@web-01", "--dry-run", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"  dotfiles.tar.gz (root: {env.home})\n" in result.stdout
    assert f"  vim.tar.gz (root: {env.home})\n" in result.stdout
    assert "Note: A real run would copy the snapshot to user@web-01:.snap/captures/" in (
        result.stdout
    )
    assert not (remote_root / "captures").exists()

    # capture saves to the host's captures directory
    result = env.run("capture", "-r", "user@web-01", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = remote_root.glob("captures/*/*/*")
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "dotfiles.tar.gz", "snapshot.toml", "vim.tar.gz",
    ]
    rel = snapshot.relative_to(hosts.home("web-01"))
    assert f"  To:   user@web-01:{rel.parent}\n" in result.stdout
    assert f"✓ Snapshot saved to user@web-01:{rel}\n" in result.stdout
    assert list(tmpdir.iterdir()) == []  # the local copy of the snap root is gone

    # restore takes the host's latest snapshot and its restore.toml ('dotfiles' only)
    write_files(env.home, {".zshrc": "edited", ".vimrc": "edited"})
    hosts.clear()
    result = env.run("restore", "-r", "user@web-01", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  From: user@web-01:.snap/captures (latest)\n" in result.stdout
    assert f"latest: {snapshot.relative_to(remote_root / 'captures')}\n" in result.stdout
    assert "Warning" not in result.stderr
    assert (env.home / ".zshrc").read_text() == "v1"
    assert (env.home / ".vimrc").read_text() == "edited"
    assert list(tmpdir.iterdir()) == []
    assert snapshot.is_dir()  # the host keeps its snapshot

    # Only reads reached the host: the snap root copy, the listing and the pull
    for line in hosts.calls():
        assert line.startswith(("rsync ", "ssh user@web-01 sh -c 'ls -1t ")), line
    assert not (env.home / ".snap").exists()


def test_e2e_remote_snap_root_errors_name_the_host(env, hosts):
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    # No .snap on the host: stops before the banner, like a missing local .snap
    result = env.run("capture", "-r", "user@web-01", extra_env={"TMPDIR": str(tmpdir)})
    assert result.returncode == 1
    assert "Starting capture" not in result.stdout
    assert (
        "Error: Copying the snap root from user@web-01 failed (rsync exit status 23)\n"
        "  from: user@web-01:.snap/\n"
        "  Create a .snap directory on user@web-01, or pass -r/--snap-root user@web-01:<path>\n"
    ) in result.stderr
    assert list(tmpdir.iterdir()) == []

    # A missing config is named by where it lives
    (hosts.home("web-01") / "snap" / "scripts").mkdir(parents=True)
    result = env.run("capture", "-r", "user@web-01:~/snap", "--dry-run")
    assert result.returncode == 1
    assert "Error: Config file not found: user@web-01:snap/configs/capture.toml\n" in (
        result.stderr
    )


def test_e2e_remote_snap_root_scripts_run_from_the_local_copy(env, hosts):
    remote_root = remote_snap_root(hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(env.home, {".zshrc": "v1"})
    write_files(remote_root, {"scripts/after.sh": 'echo "after in $PWD"\n'})
    capture_toml = remote_root / "configs" / "capture.toml"
    scripts = '\n[scripts]\nafter = ["scripts/after.sh"]\n'
    capture_toml.write_text(capture_toml.read_text() + scripts)

    result = env.run("capture", "-r", "user@web-01", "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "✓ after.sh completed" in result.stdout
    assert "    after in " in result.stdout


def test_sudo_restore_passes_the_local_copy_of_a_remote_snap_root(tmp_path, monkeypatch):
    commands = []

    class FakePopen:
        def __init__(self, cmd):
            commands.append(cmd)

        def wait(self):
            return 0

    monkeypatch.setattr(snap.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host="user@web-01", root_path=snap.PurePosixPath(".snap"))

    with pytest.raises(SystemExit):
        snap.sudo_restore(args, tmp_path / "copy", tmp_path / "cap", {}, {})

    [cmd] = commands
    assert cmd[cmd.index("-r") + 1] == str(tmp_path / "copy")
    assert not any("web-01" in arg for arg in cmd)


# --- Fix B7: only short ssh commands have a wall-clock timeout --- #


def test_long_running_commands_have_no_wall_clock_timeout(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    write_configs(tmp_path, {"dotfiles": ("$HOME", [".zshrc"])})
    calls = fake_ssh_runs(monkeypatch, {"ls -d": "/tmp/snap-capture.Ab3dEf9h/out/abc1234/\n"})
    args = argparse.Namespace(config_toml=None, run_scripts=False)

    pulled, tmpdir = snap.remote_capture(args, tmp_path, "user@web-01")
    assert pulled == tmpdir / "abc1234"
    shutil.rmtree(tmpdir)

    timeouts = {}
    for cmd, timeout in calls:
        name = cmd[0] if cmd[0] == "rsync" else cmd[-1].split()[0]
        if cmd[0] == "ssh" and "snap.py capture" in cmd[-1]:
            name = "remote capture"
        timeouts.setdefault(name, set()).add(timeout)
    assert timeouts == {
        "mktemp": {snap.COMMAND_TIMEOUT},
        "mkdir": {snap.COMMAND_TIMEOUT},
        "rsync": {None},
        "remote capture": {None},
        "ls": {snap.COMMAND_TIMEOUT},
        "rm": {snap.COMMAND_TIMEOUT},
    }
    # rsync keeps its own inactivity limit
    assert all(f"--timeout={snap.RSYNC_TIMEOUT}" in cmd for cmd, _ in calls if cmd[0] == "rsync")


def test_scripts_have_no_wall_clock_timeout(tmp_path, monkeypatch):
    # B3: scripts on a host are run by snap.py there, which runs them like this
    write_files(tmp_path, {"scripts/a.sh": "true\n"})
    calls = fake_ssh_runs(monkeypatch)
    config = {"scripts": {"before": ["scripts/a.sh"]}}

    snap.run_scripts(tmp_path, config, "before", working_dir=tmp_path)

    assert [timeout for _, timeout in calls] == [None]


# --- Fix B8: dry runs of remote flows run nothing that writes --- #


def test_ssh_run_in_a_dry_run_returns_a_placeholder_result(monkeypatch, capsys):
    calls = fake_ssh_runs(monkeypatch, {"ls": "2026/09-23/abc1234/snapshot.toml\n"})
    monkeypatch.setattr(snap, "__dry_run__", True)

    assert snap.ssh_run("user@web-01", "mkdir -p x") == (0, "", "")
    listing = snap.ssh_run("user@web-01", "ls", read_only=True)
    assert listing == (0, "2026/09-23/abc1234/snapshot.toml\n", "")

    # Only the read-only query ran; only the skipped command is shown
    assert [cmd[-1] for cmd, _ in calls] == ["ls"]
    assert capsys.readouterr().out == (
        "[DRY-RUN]   run: ssh -o ConnectTimeout=30 -o ServerAliveInterval=10 "
        "user@web-01 'mkdir -p x'\n"
    )


@pytest.mark.parametrize("command", [
    ["restore", "--to", "user@web-01", "--run-scripts"],
    ["restore", "--from", "user@web-02:.snap/captures/2026/09-23/abc1234",
     "--to", "user@web-01", "--run-scripts"],
    ["capture", "--from", "user@web-01", "--run-scripts"],
    ["migrate", "--to", "user@web-01", "--run-scripts"],
])
def test_e2e_remote_dry_runs_change_nothing(env, hosts, command):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/before.sh": "touch \"$HOME/before-ran\"\n"})
    scripts = '\n[scripts]\nbefore = ["scripts/before.sh"]\nafter = ["scripts/before.sh"]\n'
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])}, restore_extra=scripts)
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[restore.tar]\narchives = ["*"]\n\n'
        '[restore.scripts]\nbefore = ["scripts/before.sh"]\n'
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    hosts.clear()

    result = env.run(*command, "-r", str(env.root), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout.endswith("[DRY-RUN] Dry run completed; no changes were made\n")
    # Every command is shown, the work dir by name, and nothing ran on a host
    assert "run: ssh -o ConnectTimeout=30 -o ServerAliveInterval=10 user@web-01 " in (
        result.stdout
    )
    assert "<work dir>" in result.stdout
    assert hosts.calls() == []
    assert not (env.home / "before-ran").exists()


# --- Fix B3: snap.py runs on the host, with python3 from its PATH and no hard-coded sudo --- #


def child_command(work_dir, command, *options):
    """Return the shell line snap.py sends a host to run 'snap.py <command>' there.

    A capture runs under umask 077, so what it writes stays private.
    """
    argv = ["python3", "snap.py", command, *options]
    line = f"cd {shlex.quote(str(work_dir))} && {shlex.join(argv)}"
    if command == "capture":
        line = f"umask 077 && {line}"
    return line


def rsync_hosts(hosts):
    """Return the host:path arguments of each logged rsync call."""
    found = []
    for line in hosts.calls():
        if line.startswith("rsync "):
            args = shlex.split(line)[1:]
            found.append([arg for arg in args if re.match(r"[^/:-][^/:]*:", arg)])
    return found


def local_tmpdir(env):
    """Return an empty dir to use as this machine's TMPDIR, so leftovers show."""
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir(exist_ok=True)
    return tmpdir


def read_member(archive, name):
    """Return one file's text from a tar archive."""
    with tarfile.open(archive) as tar:
        return tar.extractfile(name).read().decode()


# A before or after script's cwd is the snapshot dir, so these see what travelled with it
BREW_SCRIPT = 'echo "brew from $(basename "$HOME")" > Brewfile\n'
SEEN_SCRIPT = (
    'cp Brewfile "$HOME/Brewfile-seen"\n'
    'cp ../configs/restore.toml "$HOME/restore-config-seen" 2>/dev/null || true\n'
    'echo "${SNAP_TEST_FAKE_ROOT:-user} in $PWD" > "$HOME/seen-by"\n'
)


def write_migrate_toml(root):
    """Write a migrate.toml with two archives, of which the restore selects one."""
    (root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\nrollback = ".bak"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[capture.tar.vim]\nroot = "$HOME"\nfiles = [".vimrc"]\n\n'
        '[capture.scripts]\nafter = ["scripts/sub/brew.sh"]\n\n'
        '[restore.tar]\narchives = ["dotfiles"]\n\n'
        '[restore.scripts]\nbefore = ["scripts/seen.sh"]\n'
    )
    write_files(root, {"scripts/sub/brew.sh": BREW_SCRIPT, "scripts/seen.sh": SEEN_SCRIPT})


def test_e2e_capture_from_host_runs_snap_py_there(env, hosts):
    # The -t config is the one the host gets, with its script at the same relative path
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT})
    write_configs(env.root, {"vim": ("$HOME", [".vimrc"])})
    (env.root / "configs" / "custom.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "capture", "-r", str(env.root), "-t", "configs/custom.toml", "--from", "user@web-01",
        "--run-scripts", "--verbose", extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "capture")
    assert child_command(
        work_dir, "capture", "-r", work_dir, "-t", "configs/capture.toml",
        "--to", f"{work_dir}/out", "--run-scripts", "--verbose",
    ) in commands
    assert f"ls -d {work_dir}/out/*/" in commands
    assert not any(sys.executable in c or "sudo" in c for c in commands)
    [script_copy] = [line for line in hosts.calls() if "brew.sh" in line]
    assert script_copy.endswith(f" user@web-01:{work_dir}/scripts/sub/brew.sh")

    # The local snapshot is named after the host's snapshot id, and holds the Brewfile
    # the host's after script wrote into it
    [snapshot_id] = re.findall(r"    ✓ Snapshot saved to \S+/out/(\w+)\n", result.stdout)
    [snapshot] = env.root.glob("captures/*/*/*")
    assert snapshot.name == snapshot_id
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "Brewfile", "dotfiles.tar.gz", "snapshot.toml",
    ]
    assert (snapshot / "Brewfile").read_text() == "brew from home\n"
    assert read_member(snapshot / "dotfiles.tar.gz", ".zshrc") == "on web-01"
    assert f"✓ Snapshot saved to {snapshot}\n" in result.stdout

    # Nothing is left behind on either side
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(tmpdir.iterdir()) == []


def test_e2e_restore_to_host_runs_snap_py_there(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT, "scripts/seen.sh": SEEN_SCRIPT})
    write_configs(
        env.root, {"dotfiles": ("$HOME", [".zshrc"])},
        restore_extra='\n[scripts]\nafter = ["scripts/seen.sh"]\n',
    )
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    assert env.run("capture", "-r", str(env.root), "--run-scripts").returncode == 0
    remote = hosts.home("web-01")
    write_files(remote, {".zshrc": "old"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "restore", "-r", str(env.root), "--to", "user@web-01", "--run-scripts",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (remote / ".zshrc").read_text() == "v1"
    assert (remote.parent / "home.bak" / "dotfiles" / ".zshrc").read_text() == "old"

    # snap.py there restores without sudo (nothing needs root) and runs the after script
    # itself, in its snapshot copy, which holds the Brewfile written at capture
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "restore")
    assert child_command(
        work_dir, "restore", "--from", f"{work_dir}/snapshot", "-r", work_dir,
        "-t", "configs/restore.toml", "--run-scripts",
    ) in commands
    assert not any("sudo" in c or sys.executable in c for c in commands)
    assert not any(line.startswith("sudo ") for line in hosts.calls())
    assert (remote / "Brewfile-seen").read_text() == "brew from home\n"
    assert (remote / "seen-by").read_text() == f"user in {work_dir}/snapshot\n"
    assert snap.tomllib.loads((remote / "restore-config-seen").read_text()) == (
        snap.tomllib.loads((env.root / "configs" / "restore.toml").read_text())
    )
    assert not (env.home / "seen-by").exists()

    # The host's own restore output sits between the parent's steps
    assert "\nRunning restore on user@web-01...\nStarting restore\n" in result.stdout
    assert "\n✓ Restore completed: 1 archive restored\n\nRemoving work directory" in (
        result.stdout
    )
    assert result.stdout.endswith("\nRestore on user@web-01 finished; see its summary above\n")
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(tmpdir.iterdir()) == []


def test_e2e_restore_to_host_re_runs_with_sudo_there_only_when_needed(env, hosts):
    write_files(env.home, {"force-sudo-root/f.txt": "v1"})
    seen = 'echo "${SNAP_TEST_FAKE_ROOT:-user}" > "$HOME/seen"\n'
    write_files(env.root, {"scripts/seen.sh": seen})
    write_configs(
        env.root, {"system": ("$HOME/force-sudo-root", ["f.txt"])},
        restore_extra='\n[scripts]\nbefore = ["scripts/seen.sh"]\n',
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    remote = hosts.home("web-01")
    write_files(remote, {"force-sudo-root/f.txt": "old"})

    # snap.py on the host sees the root as unwritable; the host's sudo makes it "root"
    site = hosts.base / "site"
    result = env.run(
        "restore", "-r", str(env.root), "--to", "user@web-01", "--run-scripts",
        extra_env={
            "SNAP_TEST_FORCE_SUDO": "1",
            "PYTHONPATH": f"{site}{os.pathsep}{env.env['PYTHONPATH']}",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (remote / "force-sudo-root" / "f.txt").read_text() == "v1"
    assert "Note: Restoring system.tar.gz needs root\n" in result.stdout
    assert "Re-running the restore with sudo...\nStarting restore as root\n" in result.stdout
    assert not env.sudo_marker.exists()  # never this machine's sudo

    # The host's sudo ran snap.py (keeping the login HOME), which ran the script as root
    [sudo_call] = [line for line in hosts.calls() if line.startswith("sudo ")]
    assert sudo_call.startswith(f"sudo web-01 env HOME={remote} ")
    assert " restore --from " in sudo_call and sudo_call.endswith(" --run-scripts")
    assert not any("sudo" in c for c in hosts.ssh_commands("user@web-01"))
    assert (remote / "seen").read_text() == "1\n"


def test_e2e_capture_to_host_copies_the_whole_snapshot(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )

    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01", "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = hosts.home("web-01").glob(".snap/captures/*/*/*")
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "Brewfile", "dotfiles.tar.gz", "snapshot.toml",
    ]
    # One copy of the whole directory
    rel = snapshot.relative_to(hosts.home("web-01"))
    assert rsync_hosts(hosts) == [[f"user@web-01:{rel}/"]]


# --- Fix B4: remote-to-remote copies go through this machine --- #


def test_e2e_restore_from_one_host_on_another(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT, "scripts/seen.sh": SEEN_SCRIPT})
    write_configs(
        env.root, {"dotfiles": ("$HOME", [".zshrc"])},
        restore_extra='\n[scripts]\nbefore = ["scripts/seen.sh"]\n',
    )
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01", "--run-scripts")
    assert result.returncode == 0, result.stdout + result.stderr
    hosts.clear()
    tmpdir = local_tmpdir(env)

    result = env.run(
        "restore", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02",
        "--run-scripts", extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    target = hosts.home("web-02")
    assert (target / ".zshrc").read_text() == "v1"
    assert (target / "Brewfile-seen").read_text() == "brew from home\n"
    assert result.stdout.endswith("\nRestore on user@web-02 finished; see its summary above\n")

    # Copied from web-01 to this machine, then from here to web-02; never host to host
    copies = rsync_hosts(hosts)
    assert all(len(found) == 1 for found in copies), copies
    assert [found[0].split(":")[0] for found in copies][:2] == ["user@web-01", "user@web-02"]
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-02").iterdir()) == []

    # A failed restore on web-02 still removes the local copy and the work dir
    result = env.run(
        "restore", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02",
        extra_env={"TMPDIR": str(tmpdir), "FAKE_SSH_FAIL": "snap.py restore"},
    )
    assert result.returncode == 1
    assert result.stderr.endswith("Error: Restore failed on user@web-02 (exit status 1)\n")
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-02").iterdir()) == []


def test_e2e_capture_from_one_host_to_another(env, hosts):
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "capture", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02:~/keep",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = hosts.home("web-02").glob("keep/*")
    assert read_member(snapshot / "dotfiles.tar.gz", ".zshrc") == "on web-01"
    assert f"✓ Snapshot saved to user@web-02:keep/{snapshot.name}\n" in result.stdout
    assert all(len(found) == 1 for found in rsync_hosts(hosts))
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_migrate_to_host_keeps_the_restore_selection(env, hosts):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    write_migrate_toml(env.root)
    remote = hosts.home("web-01")
    tmpdir = local_tmpdir(env)

    result = env.run(
        "migrate", "-r", str(env.root), "--to", "user@web-01", "--run-scripts",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # [restore.tar] archives picks dotfiles only; the host got it as its restore.toml
    assert (remote / ".zshrc").read_text() == "v1"
    assert not (remote / ".vimrc").exists()
    assert snap.tomllib.loads((remote / "restore-config-seen").read_text()) == {
        "tar": {"archives": ["dotfiles"]},
        "scripts": {"before": ["scripts/seen.sh"]},
        "snap": {"table": "restore.tar"},  # so its messages name [restore.tar]
    }
    # The capture script ran here; its Brewfile travelled in the snapshot to the host
    assert (remote / "Brewfile-seen").read_text() == f"brew from {env.home.name}\n"
    assert result.stdout.endswith(
        "\nMigration to user@web-01 finished; see the restore summary above\n"
    )
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_migrate_from_host(env, hosts):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    write_migrate_toml(env.root)
    write_files(hosts.home("web-01"), {".zshrc": "on web-01", ".vimrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "migrate", "-r", str(env.root), "--from", "user@web-01", "--run-scripts",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / ".zshrc").read_text() == "on web-01"
    assert (env.home / ".vimrc").read_text() == "v1"  # not in [restore.tar] archives
    # The capture script ran on the host (by snap.py there), the restore script here
    assert (env.home / "Brewfile-seen").read_text() == "brew from home\n"
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "capture")
    assert child_command(
        work_dir, "capture", "-r", work_dir, "-t", "configs/capture.toml",
        "--to", f"{work_dir}/out", "--run-scripts",
    ) in commands
    assert result.stdout.endswith("\n✓ Migration completed: 1 archive restored\n")
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_migrate_from_one_host_to_another(env, hosts):
    write_migrate_toml(env.root)
    write_files(hosts.home("web-01"), {".zshrc": "on web-01", ".vimrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "migrate", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02",
        "--run-scripts", extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    target = hosts.home("web-02")
    assert (target / ".zshrc").read_text() == "on web-01"
    assert not (target / ".vimrc").exists()
    assert (target / "Brewfile-seen").read_text() == "brew from home\n"
    assert all(len(found) == 1 for found in rsync_hosts(hosts))
    assert result.stdout.endswith(
        "\nMigration to user@web-02 finished; see the restore summary above\n"
    )
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(hosts.tmp("web-02").iterdir()) == []


def test_e2e_remote_snap_root_with_remote_capture_and_restore(env, hosts):
    # -r names where configs, scripts and captures live; --from/--to where the work runs
    remote_root = remote_snap_root(hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(hosts.home("web-02"), {".zshrc": "on web-02"})
    tmpdir = local_tmpdir(env)
    local_tmp = {"TMPDIR": str(tmpdir)}

    result = env.run("capture", "-r", "user@web-01", "--from", "user@web-02", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = remote_root.glob("captures/*/*/*")
    assert read_member(snapshot / "dotfiles.tar.gz", ".zshrc") == "on web-02"

    result = env.run("restore", "-r", "user@web-01", "--to", "user@web-03", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  From: user@web-01:.snap/captures (latest)\n" in result.stdout
    assert (hosts.home("web-03") / ".zshrc").read_text() == "on web-02"

    # snap.py never ran on the snap root's host
    assert not any("snap.py" in c for c in hosts.ssh_commands("user@web-01"))
    assert list(tmpdir.iterdir()) == []
    for host in ("web-01", "web-02", "web-03"):
        assert list(hosts.tmp(host).iterdir()) == []


def tree_state(root):
    """Return every path under root with its file content, to compare before and after."""
    state = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        state[rel] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return state


@pytest.mark.parametrize("scripts", [[], ["--run-scripts"]])
@pytest.mark.parametrize("command", [
    ["capture", "--to", "user@web-01"],
    ["capture", "--from", "user@web-01"],
    ["capture", "--from", "user@web-01", "--to", "user@web-02"],
    ["restore", "--to", "user@web-01"],
    ["restore", "--from", "user@web-01"],
    ["restore", "--from", "user@web-01", "--to", "user@web-02"],
    ["migrate", "--to", "user@web-01"],
    ["migrate", "--from", "user@web-01"],
    ["migrate", "--from", "user@web-01", "--to", "user@web-02"],
    ["capture", "-r", "user@web-01"],
    ["capture", "-r", "user@web-01", "--from", "user@web-02"],
    ["restore", "-r", "user@web-01"],
    ["restore", "-r", "user@web-01", "--to", "user@web-02"],
])
def test_e2e_every_remote_flow_has_a_dry_run(env, hosts, command, scripts):
    marker = 'touch "$HOME/script-ran"\n'
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/mark.sh": marker})
    both = '\n[scripts]\nbefore = ["scripts/mark.sh"]\nafter = ["scripts/mark.sh"]\n'
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])}, restore_extra=both)
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(capture_toml.read_text() + both)
    write_migrate_toml(env.root)
    remote_root = remote_snap_root(hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(remote_root, {"scripts/mark.sh": marker})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    assert env.run("capture", "-r", str(env.root), "--to", "user@web-01").returncode == 0
    for host in ("web-01", "web-02"):
        write_files(hosts.home(host), {".zshrc": f"on {host}"})
    hosts.clear()
    before = {"local": tree_state(env.tmp / "home"), "hosts": tree_state(hosts.base)}

    if "-r" not in command:
        command = [*command, "-r", str(env.root)]
    result = env.run(*command, *scripts, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "Error" not in result.stderr
    assert result.stdout.endswith("[DRY-RUN] Dry run completed; no changes were made\n")
    # Only read-only queries reached a host: the snap root copy and the latest lookup
    for line in hosts.calls():
        assert (
            line.startswith("rsync ") and "--include=/configs/" in line
            or re.match(r"ssh user@web-0\d sh -c 'ls -1t ", line)
        ), line
    after = {"local": tree_state(env.tmp / "home"), "hosts": tree_state(hosts.base)}
    after["hosts"].pop("calls.log", None)
    assert after == before


# --- Remote runs: unit checks --- #


def test_remote_scripts_keep_their_paths_in_the_snap_root(tmp_path):
    write_files(tmp_path, {"scripts/a.sh": "", "scripts/sub/b.sh": "", "top.sh": ""})
    config = {"scripts": {
        "before": ["scripts/a.sh", "./scripts/sub/b.sh", "scripts/missing.sh"],
        "after": ["top.sh", "scripts/a.sh"],
    }}

    scripts = snap.remote_scripts(tmp_path, config, "user@web-01")

    assert [str(s) for s in scripts] == ["scripts/a.sh", "scripts/sub/b.sh", "top.sh"]
    assert snap.remote_layout(["configs", "scripts"], scripts) == [
        "configs", "scripts", "scripts/sub",
    ]


@pytest.mark.parametrize("path", ["../outside.sh", "/usr/local/bin/setup.sh", "a/../../b.sh"])
def test_remote_scripts_outside_the_snap_root_stop_the_run(tmp_path, capsys, path):
    with pytest.raises(SystemExit):
        snap.remote_scripts(tmp_path, {"scripts": {"after": [path]}}, "user@web-01")
    assert f"Error: Cannot copy script {path} to user@web-01: it is outside" in (
        capsys.readouterr().err
    )


def test_remote_child_runs_python3_from_the_path(monkeypatch):
    monkeypatch.setattr(snap, "__verbose__", True)
    work_dir = snap.PurePosixPath("/tmp/snap-restore.Ab3dEf9h")

    line = snap.remote_child(work_dir, "restore", "--from", work_dir / "snapshot")

    assert line == (
        "cd /tmp/snap-restore.Ab3dEf9h && python3 snap.py restore "
        "--from /tmp/snap-restore.Ab3dEf9h/snapshot --verbose"
    )


@pytest.mark.parametrize("output", [
    "",
    "/tmp/w/configs/\n",
    "/tmp/w/out/a/\n/tmp/w/out/b/\n",
    "/tmp/w/out/../\n",
])
def test_remote_snapshot_id_needs_one_plain_directory(capsys, output):
    with pytest.raises(SystemExit):
        snap.remote_snapshot_id(output, snap.PurePosixPath("/tmp/w/out"), "user@web-01")
    assert "Error: Cannot find the snapshot on user@web-01" in capsys.readouterr().err

    motd = "Welcome to web-01\n/tmp/w/out/abc1234/\n"
    assert snap.remote_snapshot_id(motd, snap.PurePosixPath("/tmp/w/out"), "web-01") == "abc1234"


# --- Review fixes: remote paths, sudo env, backups, messages --- #


@pytest.mark.parametrize("value", [
    "host:a b", "host:$(id)", "host:x;y", "host:`id`", "host:x'y", "host:-x", "host:~/-x",
])
def test_parse_remote_arg_rejects_paths_rsync_cannot_pass_safely(value):
    with pytest.raises(SystemExit):
        snap.parse_remote_arg(value, "--to")


def test_parse_remote_arg_keeps_ordinary_remote_paths():
    assert snap.parse_remote_arg("user@host:~/snaps/v1.2_x+y=z,%@-") == (
        "user@host", Path("~/snaps/v1.2_x+y=z,%@-")
    )


def test_remote_workdir_never_uses_a_path_with_shell_characters(monkeypatch, capsys):
    calls = fake_ssh_runs(monkeypatch, {"mktemp": "/tmp/my dir/snap-restore.Ab3dEf9h\n"})

    with pytest.raises(SystemExit):
        snap.remote_workdir("user@web-01", "restore", ["configs"])

    assert [cmd[-1].split()[0] for cmd, _ in calls] == ["mktemp"]
    assert "Cannot create a work directory on user@web-01" in capsys.readouterr().err


def test_latest_snapshot_with_shell_characters_is_refused(monkeypatch, capsys):
    fake_ssh_runs(monkeypatch, {"ls -1t": "caps/2026/09-23/a$(id)/snapshot.toml\n"})

    with pytest.raises(SystemExit):
        snap.resolve_remote_snapshot("user@web-01", "caps")

    assert "Cannot use snapshot caps/2026/09-23/a$(id) on user@web-01" in (
        capsys.readouterr().err
    )


def test_sudo_restore_never_passes_variables_that_change_what_runs(tmp_path, monkeypatch):
    commands = []

    class FakePopen:
        def __init__(self, cmd):
            commands.append(cmd)

        def wait(self):
            return 0

    monkeypatch.setattr(snap.subprocess, "Popen", FakePopen)
    for name in ("PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, "/evil" if name != "XDG_DATA_HOME" else "/data")
    snapshot_config = {"tar": {
        "a": {"root": "${PYTHONPATH}/a"},
        "b": {"root": "$LD_PRELOAD", "link": "$DYLD_INSERT_LIBRARIES/b"},
        "c": {"root": "$BASH_ENV/c"},
        "d": {"root": "$PATH/d"},
        "e": {"root": "$XDG_DATA_HOME/e"},
    }}
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit):
        snap.sudo_restore(args, tmp_path, tmp_path, {"tar": {"archives": ["*"]}}, snapshot_config)

    env_args = [arg for arg in commands[0] if "=" in arg and not arg.startswith("-")]
    assert "XDG_DATA_HOME=/data" in env_args
    assert not any(arg.split("=")[0] in (
        "PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "PATH"
    ) for arg in env_args)


def test_sudo_restore_keeps_the_users_table_name(tmp_path, monkeypatch):
    configs = []

    class FakePopen:
        def __init__(self, cmd):
            configs.append(Path(cmd[cmd.index("-t") + 1]).read_text())

        def wait(self):
            return 0

    monkeypatch.setattr(snap.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit):
        snap.sudo_restore(
            args, tmp_path, tmp_path, {"tar": {"archives": ["x"]}}, {}, table="restore.tar"
        )

    assert snap.tomllib.loads(configs[0]) == {
        "tar": {"archives": ["x"]}, "snap": {"table": "restore.tar"},
    }


def test_same_root_spelled_two_ways_is_rotated_once(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a.txt": "new a", "b.txt": "new b"})
    write_files(home, {"a.txt": "old a", "b.txt": "old b"})
    (tmp_path / "home.bak").mkdir()  # from an earlier run
    (tmp_path / "alias").symlink_to(home)
    alpha = make_archive(tmp_path, "alpha", src, ["a.txt"])
    beta = make_archive(tmp_path, "beta", src, ["b.txt"])
    rotated = set()

    assert snap.restore_category(alpha, str(home), ".bak", rotated=rotated)
    assert snap.restore_category(beta, str(tmp_path / "alias" / ".." / "home"), ".bak",
                                 rotated=rotated)

    assert (tmp_path / "home.bak" / "alpha" / "a.txt").read_text() == "old a"
    assert (tmp_path / "home.bak" / "beta" / "b.txt").read_text() == "old b"
    assert len(list(tmp_path.glob("home.bak_*"))) == 1


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_archive_names_are_never_restored_or_captured(tmp_path, name):
    (tmp_path / f"{name}.tar.gz").write_bytes(b"")
    assert snap.snapshot_archives(tmp_path, {"tar": {name: {"root": "/x"}}}, ".tar.gz") == []

    with pytest.raises(SystemExit):
        snap.verify_capture_config({"tar": {name: {"root": "/x", "dirs": ["a"]}}}, "c.toml")


def test_e2e_restore_to_host_passes_disable_rollback(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run("restore", "-r", str(env.root), "--to", "user@web-01", "--disable-rollback")

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "restore")
    assert child_command(
        work_dir, "restore", "--from", f"{work_dir}/snapshot", "-r", work_dir,
        "-t", "configs/restore.toml", "--disable-rollback",
    ) in commands
    # Its prompt got no answer, so nothing was restored; the final line claims no success
    assert result.stdout.endswith("\nRestore on user@web-01 finished; see its summary above\n")


def test_e2e_interrupted_restore_on_host_exits_130(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run(
        "restore", "-r", str(env.root), "--to", "user@web-01",
        extra_env={"FAKE_SSH_FAIL": "snap.py restore", "FAKE_SSH_FAIL_STATUS": "130"},
    )

    assert result.returncode == 130
    assert "Error: Restore on user@web-01 was interrupted" in result.stderr
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_remote_capture_runs_under_a_private_umask(env, hosts):
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    write_configs(env.root, {
        "dotfiles": ("$HOME", [".zshrc"]),
        "empty": ("$HOME", [".nothing-here"]),
    })

    result = env.run("capture", "-r", str(env.root), "--from", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    assert any(c.startswith("umask 077 && cd ") for c in commands)
