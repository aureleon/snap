#!/usr/bin/env python3
"""
Backup and restore utility for creating system snapshots.
"""

__version__ = '0.1'

import sys

# Require Python 3.11+ for tomllib
if sys.version_info < (3, 11):
    print("Error: This script requires Python 3.11 or later", file=sys.stderr)
    sys.exit(1)

import argparse
import fnmatch
import hashlib
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import tarfile
import tempfile
import threading
import tomllib

import concurrent.futures as ccft

from datetime import datetime
from pathlib import Path
from tqdm import tqdm

# Global flags
DRY_RUN = False
VERBOSE = False

SCRIPT = Path(__file__).resolve()

# --- Subprocess Runners --- #

SSH_ALIVE_INTERVAL = 10
SSH_CONNECT_TIMEOUT = 30

TIMEOUT_BUFFER = 60
COMMAND_TIMEOUT = 300
MIN_RSYNC_TIMEOUT = 300
MAX_RSYNC_TIMEOUT = 7200

REMOTE_DIR_SUFFIX_LENGTH = 8
AVG_DOWNLOAD_RATE = 50

def ssh_run(host, *commands, check=True, stdin_data=None, tty=False):
    """Execute commands on remote host with timeout and proper error handling.

    Commands are joined with && for sequential execution.
    Returns tuple: (returncode, stdout, stderr)
    """
    command_str = " && ".join(commands)

    ssh_args = [
        "ssh",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", f"ServerAliveInterval={SSH_ALIVE_INTERVAL}"
    ]
    if tty:
        ssh_args.append("-t")
    ssh_args.extend([host, command_str])

    if DRY_RUN:
        print(f"[DRY-RUN] ssh {host}:")
        for cmd in commands:
            print(f"[DRY-RUN]   {cmd}")
        if stdin_data:
            if len(stdin_data) > 100:
                print(f"[DRY-RUN]   stdin: {stdin_data[:100]}...")
            else:
                print(f"[DRY-RUN]   stdin: {stdin_data}")
        return None

    try:
        # For TTY operations, don't capture output (interactive)
        if tty:
            result = subprocess.run(
                ssh_args,
                check=check,
                timeout=COMMAND_TIMEOUT,
                input=stdin_data,
                text=stdin_data is not None
            )
            return (result.returncode, "", "")

        # For non-TTY, capture output
        result = subprocess.run(
            ssh_args,
            check=check,
            timeout=COMMAND_TIMEOUT,
            input=stdin_data,
            text=True,
            capture_output=True
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        print(f"Error: Command timed out on {host}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        # Print captured output on error before exiting
        if e.stdout:
            print(e.stdout, end='')
        if e.stderr:
            print(e.stderr, end='', file=sys.stderr)
        print(f"Error: SSH command failed on {host}", file=sys.stderr)
        sys.exit(1)


def rsync_run(source, dest, check=True):
    """Execute rsync with timeout based on file size.

    Returns tuple: (returncode, stdout, stderr)
    """
    # Simple timeout calculation for archives and small files
    # We transfer: .tar.gz archives, tarball.toml, snap.py, configs, scripts
    timeout = MIN_RSYNC_TIMEOUT

    # Get file size if local for better timeout estimate
    if ":" not in source:  # Local file
        source_path = Path(source)
        if source_path.is_file():
            size_mb = source_path.stat().st_size / (1024 * 1024)
            # 1 minute per 50 MB transfer, minimum 5 minutes
            timeout = max(MIN_RSYNC_TIMEOUT, int(size_mb / AVG_DOWNLOAD_RATE) * 60)
            timeout = min(timeout, MAX_RSYNC_TIMEOUT)

    # Build rsync command with optional progress
    rsync_cmd = ["rsync", "-az"]
    if VERBOSE:
        rsync_cmd.append("--info=progress2")
    rsync_cmd.extend([f"--timeout={timeout}", source, dest])

    if DRY_RUN:
        print(f"[DRY-RUN] {shlex.join(rsync_cmd)}")
        # Show file size if available
        if ":" not in source and Path(source).is_file():
            size_mb = Path(source).stat().st_size / (1024 * 1024)
            print(f"[DRY-RUN]   file size: {size_mb:.2f} MB, timeout: {timeout}s")
        return None

    try:
        result = subprocess.run(
            rsync_cmd,
            check=check,
            timeout=timeout + TIMEOUT_BUFFER,
            capture_output=True,
            text=True
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        print(f"Error: rsync timed out copying {source} to {dest}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        # Print captured output on error before exiting
        if e.stdout:
            print(e.stdout, end='')
        if e.stderr:
            print(e.stderr, end='', file=sys.stderr)
        print(f"Error: rsync failed copying {source} to {dest}", file=sys.stderr)
        sys.exit(1)


def dtqdm(total, desc="", unit="item", autorefresh=None, **kwargs):
    """Return tqdm progress bar or no-op context in DRY_RUN mode.

    For unknown totals (total=None) or when autorefresh=True, starts a background
    thread to refresh the display every second so elapsed time updates even during
    long operations.
    """

    class _dtqdm:
        """No-op progress bar for dry-run mode."""
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def update(self, n=1):
            pass

        def write(self, msg):
            print(msg)

    class _AutoRefreshProgress:
        """Wrapper that auto-refreshes progress bar."""
        def __init__(self, pbar):
            self.pbar = pbar
            self.stop_event = threading.Event()
            self.thread = None

        def __enter__(self):
            # Start refresh thread
            self.thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self.thread.start()
            return self

        def __exit__(self, *args):
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=2)
            return self.pbar.__exit__(*args)

        def _refresh_loop(self):
            """Refresh display every second to show elapsed time."""
            while not self.stop_event.wait(1.0):
                self.pbar.refresh()

        def update(self, n=1):
            return self.pbar.update(n)

        def write(self, msg):
            return self.pbar.write(msg)

    if DRY_RUN:
        return _dtqdm()

    pbar = tqdm(total=total, desc=desc, unit=unit, **kwargs)

    # Auto-refresh if requested or for unknown totals
    if autorefresh or (autorefresh is None and total is None):
        return _AutoRefreshProgress(pbar)

    return pbar


def rsync_parallel(transfers):
    """Execute multiple rsync operations in parallel."""
    if not transfers:
        return 0

    if DRY_RUN:
        print(f"[DRY-RUN] {len(transfers)} parallel transfer(s):")
        for source, dest, desc in transfers:
            print(f"[DRY-RUN]   {desc}: {source} -> {dest}")
        return len(transfers)

    print(f"Transferring {len(transfers)} file(s) in parallel...")
    success_count = 0

    with dtqdm(len(transfers), "  Transfer progress", " files", bar_format='{desc}: {n}/{total} [{elapsed}, {rate_fmt}]', autorefresh=True) as pbar:
        def transfer_with_desc(transfer):
            source, dest, desc = transfer
            try:
                if VERBOSE:
                    pbar.write(f"  Starting: {desc}")
                returncode, stdout, stderr = rsync_run(source, dest)
                # Print any output after transfer completes
                if stdout and VERBOSE:
                    pbar.write(stdout.strip())
                if stderr:
                    pbar.write(f"  rsync stderr: {stderr.strip()}")
                if VERBOSE:
                    pbar.write(f"  Completed: {desc}")
                return True
            except Exception as e:
                pbar.write(f"  Error transferring {desc}: {e}")
                return False

        with ccft.ThreadPoolExecutor(max_workers=len(transfers)) as exc:
            futures = {exc.submit(transfer_with_desc, t): t for t in transfers}
            for future in ccft.as_completed(futures):
                if future.result():
                    success_count += 1
                pbar.update(1)

    print()
    if success_count < len(transfers):
        print(f"Warning: Only {success_count}/{len(transfers)} transfers succeeded", file=sys.stderr)

    return success_count


def run_scripts(root_dir, config, when, working_dir, remote_host=None, remote_dir=None):
    """Run scripts before or after an operation (local or remote).

    Args:
        root_dir: Root directory containing configs and scripts
        config: Loaded config dict
        when: "before" or "after"
        working_dir: Directory to cd into before running scripts (backup directory)
        remote_host: If set, run on remote host
        remote_dir: Remote directory (when remote_host is set)
    """
    if not config or "scripts" not in config:
        return

    if when not in config["scripts"]:
        return

    scripts = config["scripts"][when]
    if not scripts:
        return

    if remote_host:
        print(f"\nRunning {when} scripts on remote...")
    else:
        print(f"\nRunning {when} scripts...")

    for run_script_path in scripts:
        script_path = root_dir / run_script_path

        if not script_path.exists():
            print(f"  Warning: Script {script_path} not found, skipping", file=sys.stderr)
            continue

        script_name = script_path.name
        print(f"  Executing {script_name}...")

        if remote_host:
            # Run on remote with sudo, in the remote backup directory
            # Use proper shell escaping
            escaped_dir = shlex.quote(remote_dir)
            escaped_script = shlex.quote(script_name)
            cmds = [
                f"cd {escaped_dir}",
                f"chmod +x {escaped_script}",
                f"sudo bash {escaped_script}"
            ]
            returncode, stdout, stderr = ssh_run(remote_host, *cmds)
            # Print script output after completion
            if stdout:
                print(stdout, end='')
            if stderr:
                print(stderr, end='', file=sys.stderr)
        else:
            # Run locally in the backup directory
            script_cmd = ["bash", str(script_path)]
            if DRY_RUN:
                print(f"[DRY-RUN] {shlex.join(script_cmd)}")
                if working_dir:
                    print(f"[DRY-RUN]   cwd: {working_dir}")
                print(f"[DRY-RUN]   timeout: {COMMAND_TIMEOUT}s")
            else:
                try:
                    result = subprocess.run(
                        script_cmd,
                        check=True,
                        cwd=working_dir,
                        timeout=COMMAND_TIMEOUT,
                        capture_output=True,
                        text=True
                    )
                    # Print script output after completion
                    if result.stdout:
                        print(result.stdout, end='')
                    if result.stderr:
                        print(result.stderr, end='', file=sys.stderr)
                except subprocess.TimeoutExpired:
                    print(f"Error: Script {script_name} timed out", file=sys.stderr)
                    sys.exit(1)
                except subprocess.CalledProcessError as e:
                    # Print captured output on error
                    if e.stdout:
                        print(e.stdout, end='')
                    if e.stderr:
                        print(e.stderr, end='', file=sys.stderr)
                    print(f"Error: Script {script_name} failed", file=sys.stderr)
                    sys.exit(1)


# --- Snap Configuration --- #

DEFAULT_ROOT_SNAP = "~/.snap"

DEFAULT_CONFIG_BACKUP = "configs/backup.toml"
DEFAULT_CONFIG_RESTORE = "configs/restore.toml"
DEFAULT_CONFIG_DEPLOY = "configs/deploy.toml"

DEFAULT_TARBALL_TOML = "tarball.toml"

def expand_path(path_str):
    """Expand environment variables and user home in a path string."""
    return Path(os.path.expandvars(path_str)).expanduser()

def resolve_root(specified_root):
    """Resolve the snap root directory.

    Search order:
    1. Use specified_root if provided (and it's not the default)
    2. Check for .snap in current directory
    3. Check for ~/.snap
    4. Error if none exist
    """
    # Resolve both paths for comparison
    default_resolved = Path(DEFAULT_ROOT_SNAP).expanduser().resolve()
    specified_resolved = Path(specified_root).expanduser().resolve()

    # If user specified a root explicitly (not the default), use it (don't validate)
    if specified_resolved != default_resolved:
        return specified_resolved

    # Check PWD for .snap
    pwd_root = Path.cwd() / ".snap"
    if pwd_root.exists() and pwd_root.is_dir():
        return pwd_root.resolve()

    # Check home directory for .snap
    home_root = Path(DEFAULT_ROOT_SNAP).expanduser()
    if home_root.exists() and home_root.is_dir():
        return home_root.resolve()

    # None found - error
    print("Error: No .snap directory found", file=sys.stderr)
    print(f"  Searched: {pwd_root}", file=sys.stderr)
    print(f"  Searched: {home_root}", file=sys.stderr)
    print("\nCreate a .snap directory manually or specify a custom location with --snap-root", file=sys.stderr)
    sys.exit(1)

def load_config(root_dir, config_name):
    """Load a TOML config file from the root directory."""
    config_file = root_dir / config_name

    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        print(f"Error: Config file {config_file} not found", file=sys.stderr)
        sys.exit(1)
    except tomllib.TOMLDecodeError as e:
        print(f"Error parsing TOML: {e}", file=sys.stderr)
        sys.exit(1)


def verify_backup_config(config, config_name):
    """Verify backup configuration structure."""
    if "tar" not in config:
        print(f"Error: {config_name} missing required [tar] section", file=sys.stderr)
        sys.exit(1)

    tar_config = config["tar"]
    if not tar_config or not isinstance(tar_config, dict):
        print(f"Error: {config_name} [tar] section is empty or invalid", file=sys.stderr)
        sys.exit(1)

    # Verify each category has required fields
    for category, data in tar_config.items():
        if not isinstance(data, dict):
            print(f"Error: {config_name} [tar.{category}] must be a table", file=sys.stderr)
            sys.exit(1)

        if "root" not in data:
            print(f"Error: {config_name} [tar.{category}] missing required 'root' field", file=sys.stderr)
            sys.exit(1)

        if "dirs" not in data and "files" not in data:
            print(f"Error: {config_name} [tar.{category}] must have 'dirs' or 'files' field", file=sys.stderr)
            sys.exit(1)


def verify_restore_config(config, config_name):
    """Verify restore/deploy configuration structure."""
    if "tar" not in config:
        print(f"Error: {config_name} missing required [tar] section", file=sys.stderr)
        sys.exit(1)

    tar_config = config["tar"]
    if not isinstance(tar_config, dict):
        print(f"Error: {config_name} [tar] section must be a table", file=sys.stderr)
        sys.exit(1)

    if "archives" not in tar_config:
        print(f"Error: {config_name} [tar] section missing 'archives' field", file=sys.stderr)
        sys.exit(1)

    archives = tar_config["archives"]
    if archives is not None and not isinstance(archives, list):
        print(f"Error: {config_name} [tar].archives must be a list or null", file=sys.stderr)
        sys.exit(1)


def verify_backup(backup_dir, require_checksum=False):
    """Validate that a backup directory contains required files."""
    if not backup_dir.exists():
        print(f"Error: Backup directory {backup_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    toml_path = backup_dir / DEFAULT_TARBALL_TOML
    if not toml_path.exists():
        print(f"Error: {DEFAULT_TARBALL_TOML} not found in {backup_dir}", file=sys.stderr)
        sys.exit(1)

    # Load compression type from tarball.toml
    with open(toml_path, "rb") as f:
        tarball_config = tomllib.load(f)

    compress_type = tarball_config.get("tarball", {}).get("compress", "gzip")
    if compress_type not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
        sys.exit(1)

    ext, _ = COMPRESS_MAP[compress_type]

    # Check that at least one tar archive exists
    archives = list(backup_dir.glob(f"*{ext}"))
    if not archives:
        print(f"Error: No tar archives found in {backup_dir}", file=sys.stderr)
        sys.exit(1)

    return toml_path


# --- Dry-Run Helper Functions --- #

def dry_run_create_archive(name, root_path, expanded_paths, archive_path):
    """Dry-run for archive creation - shows what would be added."""
    print(f"[DRY-RUN] Create archive: {archive_path}")
    if VERBOSE:
        for path in expanded_paths:
            full_path = root_path / path
            if full_path.exists():
                arcname = str(path)
                print(f"[DRY-RUN]   add: {arcname}")
            else:
                print(f"[DRY-RUN]   Warning: {full_path} not found, skipping", file=sys.stderr)
    return archive_path


# --- Archive Handlers --- #

# Compression type mappings: extension, write mode
# Read mode is always 'r:*' for auto-detection
COMPRESS_MAP = {
    "gzip":  (".tar.gz",  "w:gz"),
    "bzip2": (".tar.bz2", "w:bz2"),
    "xz":    (".tar.xz",  "w:xz"),
    "":      (".tar",     "w"),
}


def archive_filter(tarinfo):
    """Filters metadata files from archives."""
    if sys.platform == 'darwin':
        # Skip ._* resource fork files and .DS_Store
        re_metadata = r'^(?:\._.*)|(?:.*/?.DS_Store)$'
        if re.match(re_metadata, tarinfo.name):
            return None
    return tarinfo


def archive_expand(patterns, root_path):
    """Expand glob patterns in file/directory lists."""
    expanded = []
    matched_paths = set()

    for pattern in patterns:
        # Check if pattern contains glob characters
        if any(char in pattern for char in ['*', '?', '[', ']']):
            # Use glob to find matches relative to root
            matches = list(root_path.glob(pattern))
            if matches:
                for match in matches:
                    # Store relative path from root
                    rel_path = match.relative_to(root_path)
                    if str(rel_path) not in matched_paths:
                        expanded.append(str(rel_path))
                        matched_paths.add(str(rel_path))
            else:
                print(f"  Warning: Pattern '{pattern}' matched no files, skipping", file=sys.stderr)
        else:
            # Literal path - add as-is
            if pattern not in matched_paths:
                expanded.append(pattern)
                matched_paths.add(pattern)

    return expanded


def archive_create(name, root, paths, outdir, compress="gzip"):
    """Create a compressed tar archive for a backup category.

    Args:
        compress: Compression type ("gzip", "bzip2", "xz", or "" for no compression)

    Returns tuple: (archive_path, warnings_list)
    """
    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    ext, mode = COMPRESS_MAP[compress]
    archive_path = outdir / f"{name}{ext}"
    root_path = expand_path(root)
    warnings = []

    # Expand any glob patterns in the paths
    expanded_paths = archive_expand(paths, root_path)

    if DRY_RUN:
        return dry_run_create_archive(name, root_path, expanded_paths, archive_path), []

    # Don't show individual progress bars during parallel creation
    # (avoids terminal corruption and empty lines)
    with tarfile.open(archive_path, mode) as tar:
        for path in expanded_paths:
            full_path = root_path / path
            if full_path.exists():
                # Store with relative path (relative to root)
                arcname = str(path)
                tar.add(full_path, arcname=arcname, filter=archive_filter)

                if VERBOSE:
                    warnings.append(f"  {name}: Added {full_path.name}")
            else:
                warnings.append(f"  Warning: {full_path.name} not found, skipping")

    return archive_path, warnings


def create_archives(tasks, compress="gzip"):
    """Create multiple archives in parallel.

    Args:
        tasks: List of (category, root, paths, outdir) tuples
        compress: Compression type to use for all archives
    """
    if not tasks:
        return []

    print(f"Creating {len(tasks)} archive(s)...")
    results = []
    all_warnings = []

    with ccft.ThreadPoolExecutor(max_workers=len(tasks)) as exc:
        futures = {exc.submit(archive_create, *task, compress): task for task in tasks}
        with dtqdm(len(tasks), "Overall progress", unit=" archives", bar_format='{desc}: {n}/{total} [{elapsed}, {rate_fmt}]', autorefresh=True) as pbar:
            for future in ccft.as_completed(futures):
                task = futures[future]
                category = task[0]
                try:
                    archive_path, warnings = future.result()
                    results.append(archive_path)
                    all_warnings.extend(warnings)
                except Exception as e:
                    pbar.write(f"\nError creating archive {category}: {e}")
                pbar.update(1)

    print()

    # Print all collected warnings after parallel operations complete
    for warning in all_warnings:
        if "Warning:" in warning:
            print(warning, file=sys.stderr)
        elif VERBOSE:
            print(warning)

    return results


def generate_tarball_toml(outdir, tasks, compress="gzip", category_meta=None, backups_ext=None):
    """Generate tarball.toml with checksums for all archives.

    Args:
        outdir: Directory containing compressed tar archives
        tasks: List of (category, root, paths, outdir) tuples from create_archives
        compress: Compression type used for archives
        category_meta: Dict mapping category name to {"root": str, "link": str_or_None}
        backups_ext: Backup file extension (e.g., ".bak") or None

    Returns:
        Path to generated tarball.toml file
    """
    if category_meta is None:
        category_meta = {}
    print("Generating tarball.toml...")

    # Find archives based on compression type
    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    ext, _ = COMPRESS_MAP[compress]
    archives = list(outdir.glob(f"*{ext}"))

    if DRY_RUN:
        toml_path = outdir / DEFAULT_TARBALL_TOML
        print(f"[DRY-RUN] Generate tarball.toml at: {toml_path}")
        for archive in archives:
            print(f"[DRY-RUN]   calculate checksum for: {archive.name}")
        return toml_path

    # Calculate checksums for all archives
    checksums = {}
    with dtqdm(len(archives), "  Calculating checksums", " archives",
               bar_format='{desc}: {n}/{total} [{elapsed}, {rate_fmt}]',
               autorefresh=True) as pbar:
        for archive in archives:
            category = archive_category(archive)
            checksum = calculate_checksum(archive, show_progress=False)
            checksums[category] = checksum
            pbar.update(1)

    print()

    # Generate TOML content
    toml_lines = [
        '[tarball]',
        f'compress = "{compress}"  # compression type',
        'checksum = "sha256"  # digest type',
    ]
    if backups_ext:
        toml_lines.append(f'backups = "{backups_ext}"  # backup file extension')
    toml_lines.append('')

    for category in sorted(checksums.keys()):
        checksum = checksums[category]
        toml_lines.append(f'[tar.{category}]')

        # Add root and link if available in category_meta
        meta = category_meta.get(category, {})
        if "root" in meta:
            toml_lines.append(f'root = "{meta["root"]}"')
        if meta.get("link"):
            toml_lines.append(f'link = "{meta["link"]}"')

        # Split checksum into 64-character chunks
        chunks = []
        for i in range(0, len(checksum), 64):
            chunks.append(f'    {checksum[i:i+64]}')

        toml_lines.append('checksum = [')
        toml_lines.extend([chunk + ',' for chunk in chunks[:-1]])  # Add commas to all but last
        toml_lines.append(chunks[-1])  # Last one without comma
        toml_lines.append(']')
        toml_lines.append('')

    # Write TOML file
    toml_path = outdir / DEFAULT_TARBALL_TOML
    with open(toml_path, 'w') as f:
        f.write('\n'.join(toml_lines))

    print(f"  Done: {toml_path.name}")
    return toml_path


# --- Archive Extraction Handlers --- #

def archive_category(archive):
    """Extract category name from archive filename."""
    return archive.stem.replace(".tar", "")

def archive_entries(tar):
    """Get set of top-level entry names from a tar archive."""
    entries = set()
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if parts:
            entries.add(parts[0])
    return entries

def archive_confirm(archive, compress="gzip", root=None):
    """Ask user for confirmation to extract archive."""
    name = archive_category(archive)

    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    if DRY_RUN:
        print(f"\n[DRY-RUN] Restore {name}?  [Y/N]: Y")
        if root:
            print(f"[DRY-RUN]  Extraction target: {root}")
        return True

    # Get top-level directories from the archive (auto-detect compression)
    with tarfile.open(archive, 'r:*') as tar:
        entries = archive_entries(tar)

        # Format paths for display
        top_level_dirs = set()
        for entry in entries:
            if root:
                # Show paths relative to root
                top_level_dirs.add(str(Path(root) / entry))
            else:
                # Legacy: show absolute paths
                top_level_dirs.add("/" + entry)

        # Show what will be restored
        print(f"\n{name} will restore to:")
        for d in sorted(top_level_dirs):
            print(f"  {d}")

        # Prompt user for confirmation
        try:
            msg = f"Restore {name}? This will overwrite any existing files... [Y/N]:"
            response = input(msg).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print(f"\n  Skipping {name}")
            return False

        if response not in ('y', 'yes'):
            print(f"  Skipping {name}")
            return False

        return True


def archive_select(available, selected_archives, tmpdir, compress="gzip"):
    """Select which archives to restore based on patterns."""
    if not selected_archives:
        return available

    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    ext, _ = COMPRESS_MAP[compress]

    archives_to_restore = []
    matched_names = set()

    for pattern in selected_archives:
        # Try exact match first
        archive_file = tmpdir / f"{pattern}{ext}"
        if archive_file.exists():
            if pattern not in matched_names:
                archives_to_restore.append(archive_file)
                matched_names.add(pattern)
        else:
            # Try glob pattern match
            pattern_matched = False
            for archive in available:
                archive_name = archive_category(archive)
                if fnmatch.fnmatch(archive_name, pattern):
                    if archive_name not in matched_names:
                        archives_to_restore.append(archive)
                        matched_names.add(archive_name)
                        pattern_matched = True

            if not pattern_matched:
                print(f"Warning: No archives match pattern '{pattern}', skipping", file=sys.stderr)

    return archives_to_restore


def archive_extract(archive, compress="gzip", root=None):
    """Extract an archive without prompting (assumes already confirmed).
    """
    name = archive_category(archive)

    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    # Determine extraction path
    if root:
        extract_path = expand_path(root)
    else:
        # Legacy: extract to / for absolute paths
        extract_path = Path("/")

    try:
        # Don't show individual progress bars during parallel extraction
        # (avoids terminal corruption and empty lines)
        # Use 'r:*' to auto-detect compression type
        with tarfile.open(archive, 'r:*') as tar:
            # Extract to root; use 'tar' filter if available (Python 3.12+)
            # 'tar' filter provides security without breaking symlinks or permissions
            members = tar.getmembers()
            for member in members:
                try:
                    tar.extract(member, extract_path, filter='tar')
                except TypeError:
                    # Python < 3.12 doesn't support filter parameter
                    tar.extract(member, extract_path)
        return True
    except Exception as e:
        print(f"\nError extracting {name}: {e}", file=sys.stderr)
        return False


def restore_category(archive, root, backup_ext, compress="gzip"):
    """Transactionally restore a single category with rollback on failure.

    Interleaves backup and restore per-entry:
    1. Backs up existing entry (copy, not move)
    2. Removes original
    3. Extracts new entry from archive
    4. On any error: rolls back by restoring from backup
    """
    category = archive_category(archive)

    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        return False

    root_path = expand_path(root)
    backup_dir = Path(str(root_path) + backup_ext)

    if DRY_RUN:
        print(f"\n[DRY-RUN] Restore category: {category} -> {root_path}")
        if backup_dir.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            old_backup = Path(str(backup_dir) + f"_{timestamp}")
            print(f"[DRY-RUN]   mv {backup_dir} {old_backup}")
        print(f"[DRY-RUN]   mkdir {backup_dir}")

        # Show what would be backed up and extracted
        if VERBOSE:
            with tarfile.open(archive, 'r:*') as tar:
                entries = archive_entries(tar)
                for entry in entries:
                    entry_path = root_path / entry
                    if entry_path.exists():
                        if entry_path.is_dir():
                            print(f"[DRY-RUN]   cp -r {entry_path} {backup_dir / entry}")
                        else:
                            print(f"[DRY-RUN]   cp {entry_path} {backup_dir / entry}")
                    print(f"[DRY-RUN]   extract: {entry} (from {archive.name})")
        return True

    # Handle existing backup directory
    if backup_dir.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        old_backup = Path(str(backup_dir) + f"_{timestamp}")
        backup_dir.rename(old_backup)
        if VERBOSE:
            print(f"  Moved existing backup: {backup_dir} -> {old_backup}")

    # Create fresh backup directory
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"Restoring {category}...")
    restored = []  # Track what we've touched for rollback

    try:
        with tarfile.open(archive, 'r:*') as tar:
            entries = archive_entries(tar)
            all_members = tar.getmembers()

            # Separate into directories and files
            for entry in entries:
                entry_path = root_path / entry

                # Backup existing entry if it exists
                if entry_path.exists():
                    if entry_path.is_dir():
                        shutil.copytree(str(entry_path), str(backup_dir / entry), symlinks=True)
                        shutil.rmtree(str(entry_path))
                    else:
                        shutil.copy2(str(entry_path), str(backup_dir / entry))
                        entry_path.unlink()

                    if VERBOSE:
                        print(f"  Backed up: {entry}")

                # Extract this entry's members from the archive
                members = [m for m in all_members
                          if Path(m.name).parts and Path(m.name).parts[0] == entry]

                for member in members:
                    try:
                        tar.extract(member, root_path, filter='tar')
                    except TypeError:
                        # Python < 3.12 doesn't support filter parameter
                        tar.extract(member, root_path)

                restored.append(entry)
                if VERBOSE:
                    print(f"  Restored: {entry}")

        print(f"  ✓ {category} restored")
        return True

    except Exception as e:
        # Rollback: delete extracted entries and restore from backup
        print(f"\nError restoring {category}: {e}", file=sys.stderr)
        print(f"Rolling back {len(restored)} entry(s)...", file=sys.stderr)

        for entry in restored:
            entry_path = root_path / entry
            backup_path = backup_dir / entry

            # Remove the partially extracted entry
            if entry_path.exists():
                if entry_path.is_dir():
                    shutil.rmtree(str(entry_path))
                else:
                    entry_path.unlink()

            # Restore from backup if it exists
            if backup_path.exists():
                if backup_path.is_dir():
                    shutil.copytree(str(backup_path), str(entry_path), symlinks=True)
                else:
                    shutil.copy2(str(backup_path), str(entry_path))

        print(f"✓ Rollback complete for {category}", file=sys.stderr)
        return False


def extract_archives(archives, skip_confirm=False, root_map=None, compress="gzip"):
    """Extract multiple archives in parallel (after confirmation)."""
    if not archives:
        return 0

    if root_map is None:
        root_map = {}

    # First, collect confirmations sequentially (unless skipped)
    confirmed = []
    if skip_confirm:
        confirmed = archives
    else:
        for archive in archives:
            category = archive_category(archive)
            root = root_map.get(category)
            if archive_confirm(archive, compress, root):
                confirmed.append(archive)

    if not confirmed:
        return 0

    # Extract confirmed archives in parallel
    print(f"\nExtracting {len(confirmed)} archive(s)...")
    success_count = 0

    with ccft.ThreadPoolExecutor(max_workers=len(confirmed)) as exc:
        # Build futures with root for each archive
        futures = {}
        for archive in confirmed:
            category = archive_category(archive)
            root = root_map.get(category)
            futures[exc.submit(archive_extract, archive, compress, root)] = archive

        with dtqdm(len(confirmed), "Extraction progress", " archives", bar_format='{desc}: {n}/{total} [{elapsed}, {rate_fmt}]', autorefresh=True) as pbar:
            for future in ccft.as_completed(futures):
                archive = futures[future]
                try:
                    if future.result():
                        success_count += 1
                except Exception as e:
                    pbar.write(f"\nError extracting {archive.name}: {e}")
                pbar.update(1)

    return success_count


def restore_archives(backup_dir, selected_archives, compress="gzip", skip_confirm=False, root_map=None):
    """Restore archives from backup directory.

    Args:
        backup_dir: Directory containing archives
        selected_archives: List of archive patterns to restore (empty list = all)
        compress: Compression type from tarball.toml
        skip_confirm: Skip user confirmation if True
        root_map: Dict mapping category name to root path
    """
    if compress not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    ext, _ = COMPRESS_MAP[compress]

    # Find all available archives with the specified compression type
    available = list(backup_dir.glob(f"*{ext}"))

    if not available:
        print("Error: No archives found to restore", file=sys.stderr)
        sys.exit(1)

    if DRY_RUN:
        print(f"[DRY-RUN] Found {len(available)} archives")

    # Select which archives to restore based on patterns
    archives_to_restore = archive_select(available, selected_archives, backup_dir, compress)

    # Extract selected archives in parallel
    success_count = extract_archives(archives_to_restore, skip_confirm, root_map, compress)

    print(f"\n✓ {success_count}/{len(archives_to_restore)} archive(s) restored")


def create_symlinks(tarball_config):
    """Create symlinks from link -> root for each category with a link field."""
    tar_sections = tarball_config.get("tar", {})
    if not tar_sections:
        return

    symlinks_created = []
    for category, section_data in tar_sections.items():
        link = section_data.get("link")
        root = section_data.get("root")

        if not link or not root:
            continue

        # Expand environment variables
        link_path = expand_path(link)
        root_path = expand_path(root)

        if DRY_RUN:
            print(f"[DRY-RUN] ln -s {root_path} {link_path}")
            symlinks_created.append(str(link_path))
            continue

        # Check if link already exists
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink():
                # Check if it points to the correct target
                current_target = link_path.resolve()
                if current_target == root_path.resolve():
                    # Already correct, skip
                    continue
                else:
                    print(f"Error: {link_path} already exists as symlink to {current_target}, expected {root_path}", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Error: {link_path} already exists and is not a symlink", file=sys.stderr)
                sys.exit(1)

        # Create parent directory if needed
        link_path.parent.mkdir(parents=True, exist_ok=True)

        # Create symlink
        link_path.symlink_to(root_path)
        symlinks_created.append(str(link_path))

    if symlinks_created:
        print(f"\n✓ Created {len(symlinks_created)} symlink(s)")
        for link in symlinks_created:
            print(f"  {link}")


# --- Checksum Handlers --- #

CHECKSUM_CHUNK_SIZE = 8192

def calculate_checksum(filepath, show_progress=False):
    """Calculate a SHA-256 digest."""
    sha256 = hashlib.sha256()
    file_size = filepath.stat().st_size

    if show_progress and file_size > 0:
        with open(filepath, "rb") as f:
            with dtqdm(file_size, "  Computing checksum", "B", unit_scale=True, unit_divisor=1024, bar_format='{desc}: {n_fmt}/{total_fmt} [{elapsed}, {rate_fmt}]', leave=False) as pbar:
                for chunk in iter(lambda: f.read(CHECKSUM_CHUNK_SIZE), b""):
                    sha256.update(chunk)
                    pbar.update(len(chunk))
    else:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(CHECKSUM_CHUNK_SIZE), b""):
                sha256.update(chunk)

    return sha256.hexdigest()


def verify_archives_from_toml(backup_dir):
    """Verify all archives using tarball.toml checksums.

    Args:
        backup_dir: Directory containing tarball.toml and .tar.gz archives

    Returns:
        True if all checksums match, exits on failure
    """
    toml_path = backup_dir / DEFAULT_TARBALL_TOML

    if not toml_path.exists():
        print(f"Error: {DEFAULT_TARBALL_TOML} not found in {backup_dir}", file=sys.stderr)
        sys.exit(1)

    # Load tarball.toml
    with open(toml_path, "rb") as f:
        tarball_config = tomllib.load(f)

    # Get tarball options
    tarball_opts = tarball_config.get("tarball", {})
    compress_type = tarball_opts.get("compress", "gzip")
    digest_type = tarball_opts.get("checksum")

    # Verify compression type
    if compress_type not in COMPRESS_MAP:
        print(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
        sys.exit(1)

    archive_ext, _ = COMPRESS_MAP[compress_type]

    if DRY_RUN:
        print(f"[DRY-RUN] Verify archives from: {toml_path}")
        return tarball_config

    # If no checksum specified in options, skip verification
    if not digest_type:
        print("Warning: No checksum type specified in tarball.toml, skipping verification")
        return tarball_config

    print("Verifying archive integrity...")

    if digest_type != "sha256":
        print(f"Error: Unsupported digest type: {digest_type}", file=sys.stderr)
        sys.exit(1)

    # Get all tar sections
    tar_sections = tarball_config.get("tar", {})

    if not tar_sections:
        print("Error: No tar sections found in tarball.toml", file=sys.stderr)
        sys.exit(1)

    # Verify each archive
    all_valid = True
    with dtqdm(len(tar_sections), "  Verifying", " archives",
               bar_format='{desc}: {n}/{total} [{elapsed}, {rate_fmt}]',
               autorefresh=True) as pbar:
        for category, section_data in tar_sections.items():
            archive_path = backup_dir / f"{category}{archive_ext}"

            if not archive_path.exists():
                pbar.write(f"Error: Archive {archive_path.name} not found")
                all_valid = False
                pbar.update(1)
                continue

            # Rejoin checksum chunks
            checksum_chunks = section_data.get("checksum", [])
            if not checksum_chunks:
                pbar.write(f"Error: No checksum found for {category}")
                all_valid = False
                pbar.update(1)
                continue

            expected_checksum = ''.join(checksum_chunks)

            # Calculate actual checksum
            actual_checksum = calculate_checksum(archive_path, show_progress=False)

            if actual_checksum != expected_checksum:
                pbar.write(f"Error: Checksum mismatch for {archive_path.name}")
                all_valid = False

            pbar.update(1)

    print()

    if not all_valid:
        print("Error: Checksum verification failed! Backup may be corrupted.", file=sys.stderr)
        sys.exit(1)

    print("✓ All archives verified")
    return tarball_config


# --- Backup Logic --- #

def backup(args, root_dir, outdir, config):
    """Create backup archives in specified directory.

    Returns:
        Path to final backup directory (with checksum subdirectory)
    """
    # Get compression type and backups extension from config
    tarball_opts = config.get("tarball", {})
    compress = tarball_opts.get("compress", "gzip")
    backups_ext = tarball_opts.get("backups")

    # Collect all archive tasks and build category metadata
    tar_config = config.get("tar", {})
    tasks = []
    category_meta = {}
    for category, data in tar_config.items():
        if not isinstance(data, dict):
            continue

        root = data.get("root")
        if not root:
            continue

        # Combine dirs and files
        paths = data.get("dirs", []) + data.get("files", [])
        if not paths:
            continue

        tasks.append((category, root, paths, outdir))

        # Store root and link for tarball.toml
        category_meta[category] = {
            "root": root,
            "link": data.get("link")
        }

    # Create archives in parallel
    create_archives(tasks, compress)

    # Generate tarball.toml with checksums
    toml_path = generate_tarball_toml(outdir, tasks, compress, category_meta, backups_ext)

    # Calculate checksum of tarball.toml for directory naming
    if DRY_RUN:
        print(f"[DRY-RUN] Generate checksum of tarball.toml")
        short_hash = "abcd123"  # Placeholder for dry-run
    else:
        print("Generating backup checksum...")
        checksum = calculate_checksum(toml_path, show_progress=True)
        short_hash = checksum[:7]

    # Create checksum-named subdirectory and move files
    final_dir = outdir.parent / outdir.name / short_hash

    if DRY_RUN:
        print(f"[DRY-RUN] mkdir {final_dir}")
        print(f"[DRY-RUN] Move all tar archives and tarball.toml to: {final_dir}")
    else:
        final_dir.mkdir(parents=True, exist_ok=False)

        # Move all tar archives to checksum directory
        ext, _ = COMPRESS_MAP[compress]
        for archive in outdir.glob(f"*{ext}"):
            archive.rename(final_dir / archive.name)

        # Move tarball.toml to checksum directory
        toml_path.rename(final_dir / DEFAULT_TARBALL_TOML)

        print(f"✓ Backup files moved to {final_dir.name}")

    # Run after-backup scripts if requested
    if args.run_scripts:
        # Determine which config to use
        config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_BACKUP
        scripts_config = load_config(root_dir, config_name)
        # Skip verification for scripts-only config (may not have [tar] section)
        run_scripts(root_dir, scripts_config, "after", working_dir=final_dir)

    return final_dir


def remote_backup(backup_dir, dest_host, intended_path):
    """Copy completed backup directory to remote host."""
    print(f"\nCopying backup to {dest_host}...")

    # Ensure remote directory exists
    escaped_path = shlex.quote(str(intended_path))
    ssh_run(dest_host, f"mkdir -p {escaped_path}")

    # Copy all tar archives and tarball.toml
    transfers = []

    # Add tarball.toml
    toml_path = backup_dir / DEFAULT_TARBALL_TOML
    if toml_path.exists():
        transfers.append((str(toml_path), f"{dest_host}:{intended_path}/", DEFAULT_TARBALL_TOML))

        # Load compression type from tarball.toml
        with open(toml_path, "rb") as f:
            tarball_config = tomllib.load(f)
        compress_type = tarball_config.get("tarball", {}).get("compress", "gzip")
        if compress_type not in COMPRESS_MAP:
            print(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
            sys.exit(1)
        ext, _ = COMPRESS_MAP[compress_type]

        # Add all tar archives with the specified compression type
        for archive in backup_dir.glob(f"*{ext}"):
            transfers.append((str(archive), f"{dest_host}:{intended_path}/", archive.name))
    else:
        print(f"Error: {DEFAULT_TARBALL_TOML} not found in {backup_dir}", file=sys.stderr)
        sys.exit(1)

    if not transfers:
        print("Error: No backup files found to copy", file=sys.stderr)
        sys.exit(1)

    success_count = rsync_parallel(transfers)
    if success_count < len(transfers):
        print("Error: Failed to copy all files to remote", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Backup copied to {dest_host}:{intended_path}")

# --- Restore Logic --- #

def restore(args, root_dir, backup_dir=None):
    """Restore from local backup directory."""
    if backup_dir is None:
        # Use combined backup_dir if set (includes checksum), otherwise fall back to dir
        backup_dir = getattr(args, 'backup_dir', args.dir).resolve()

    toml_path = verify_backup(backup_dir)

    # Verify all archives using tarball.toml and load config
    tarball_config = verify_archives_from_toml(backup_dir)
    print()

    # Get compression type and backups extension from tarball.toml
    tarball_opts = tarball_config.get("tarball", {})
    compress = tarball_opts.get("compress", "gzip")
    backups_ext = tarball_opts.get("backups")

    # Build root_map from tar sections
    root_map = {}
    tar_sections = tarball_config.get("tar", {})
    for category, section_data in tar_sections.items():
        root = section_data.get("root")
        if root:
            root_map[category] = root

    # Check if running with sudo (Unix only)
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        print("Note: This script needs sudo privileges to restore files to system locations.")
        print("Re-running with sudo...")
        cmd = ["sudo", sys.executable] + sys.argv
        os.execvp("sudo", cmd)

    # Load restore config
    config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_RESTORE
    restore_config = load_config(root_dir, config_name)
    verify_restore_config(restore_config, config_name)

    # Get archives from config
    # Semantics: None/[] = skip restore, ["*"] = restore all, [patterns...] = restore matching
    # Patterns support glob matching: ["ssh*", "dotfiles", "*-config"]
    archives = restore_config.get("tarball", {}).get("archives", None)
    if archives == []:
        archives = None  # Empty list in config means don't restore
    elif archives == ["*"] or "*" in archives:
        archives = []  # Wildcard means restore all available

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "before", working_dir=backup_dir)

    # Restore archives
    if archives is not None:
        # Find matching archive files
        ext, _ = COMPRESS_MAP[compress]
        available = list(backup_dir.glob(f"*{ext}"))
        selected = archive_select(available, archives, backup_dir, compress)

        if not selected:
            print("No archives selected for restore")
        elif getattr(args, 'disable_backups', False):
            # No backup protection - use existing extract_archives with confirmation
            extract_archives(selected, skip_confirm=False, root_map=root_map, compress=compress)
        else:
            # Transactional restore with rollback support
            if not backups_ext:
                print("Warning: No backups extension specified in tarball.toml, falling back to interactive confirmation", file=sys.stderr)
                extract_archives(selected, skip_confirm=False, root_map=root_map, compress=compress)
            else:
                print(f"\nRestoring {len(selected)} category archive(s)...")
                failed = []
                for archive in selected:
                    category = archive_category(archive)
                    root = root_map.get(category)
                    if not root:
                        print(f"Warning: No root for {category}, skipping", file=sys.stderr)
                        continue
                    success = restore_category(archive, root, backups_ext, compress)
                    if not success:
                        failed.append(category)

                if failed:
                    print(f"\nError: {len(failed)} category(s) failed: {', '.join(failed)}", file=sys.stderr)
                    sys.exit(1)
                else:
                    print(f"\n✓ Restored {len(selected)} category archive(s)")

    # Create symlinks from link -> root
    create_symlinks(tarball_config)

    # Run after scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "after", working_dir=backup_dir)

    print("\n✓ Restore completed successfully")


def remote_mkdir(dest_host):
    """Generate a unique remote work directory path and create it."""
    chars = string.ascii_lowercase + string.digits
    suffix = ''.join(random.choices(chars, k=REMOTE_DIR_SUFFIX_LENGTH))
    remote_dir = f"/tmp/{SCRIPT.stem}-restore-{suffix}"

    # Create remote directory
    print("Creating remote directory...")
    ssh_run(dest_host, f"mkdir -p {shlex.quote(remote_dir)}")
    return remote_dir

def remote_restore(args, root_dir, source_host):
    """Pull backup from remote and restore locally."""
    backup_dir_path = str(args.backup_dir)
    print(f"Pulling backup from {source_host}:{backup_dir_path}...\n")

    tmpdir = Path(tempfile.mkdtemp(prefix=f"{SCRIPT.stem}-"))

    try:
        # Rsync entire backup directory (includes all .tar.gz and tarball.toml)
        # Use trailing slash to copy contents into tmpdir
        host_path = f'{source_host}:{backup_dir_path}/'
        result = rsync_run(host_path, str(tmpdir) + "/")

        if result is None:  # DRY_RUN
            pass
        elif result[0] != 0:
            print("Error: Failed to pull backup from remote", file=sys.stderr)
            sys.exit(1)

        restore(args, root_dir, backup_dir=tmpdir)

    finally:
        shutil.rmtree(tmpdir)


def remote_deploy(args, root_dir, dest_host, source_host=None):
    """Deploy backup to remote and restore (from local or remote source)."""

    # Determine source paths (local or remote)
    if source_host:
        # Remote source - use combined backup_dir (includes checksum)
        backup_dir_path = str(args.backup_dir) if isinstance(args.backup_dir, Path) else args.backup_dir
        print(f"Deploying from {source_host} to {dest_host}...\n")
        config_src_base = f"{source_host}:{root_dir}"
        local_backup = None
    else:
        # Local source - use combined backup_dir (includes checksum)
        local_backup = args.backup_dir.resolve()
        print(f"Deploying to {dest_host}...\n")

        toml_path = verify_backup(local_backup)
        config_src_base = None

    config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_DEPLOY
    deploy_config = load_config(root_dir, config_name)
    verify_restore_config(deploy_config, config_name)

    remote_dir = remote_mkdir(dest_host)

    # Collect all transfers to execute in parallel
    transfers = []

    # Add snap.py script
    transfers.append((str(SCRIPT), f"{dest_host}:{remote_dir}/", SCRIPT.name))

    # Add backup files (tarball.toml and all tar archives)
    if source_host:
        # Remote source - need to fetch tarball.toml first to determine archives
        print(f"Fetching backup metadata from {source_host}...")

        # Create temp file for tarball.toml
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.toml', delete=False) as tmp:
            tmp_toml_path = Path(tmp.name)

        try:
            # Fetch tarball.toml from remote source
            remote_toml = f"{source_host}:{backup_dir_path}/{DEFAULT_TARBALL_TOML}"
            result = rsync_run(remote_toml, str(tmp_toml_path))

            if result is None:  # DRY_RUN
                print(f"[DRY-RUN] Fetch {remote_toml}")
                print(f"[DRY-RUN] Enumerate archives from remote tarball.toml")
                compress_type = "gzip"
                # In dry-run, show placeholder archives
                tar_sections = {"archive1": {}, "archive2": {}, "archive-N": {}}
            elif result[0] != 0:
                print(f"Error: Failed to fetch {DEFAULT_TARBALL_TOML} from {source_host}", file=sys.stderr)
                sys.exit(1)
            else:
                # Load tarball.toml to get compression type and archive list
                with open(tmp_toml_path, "rb") as f:
                    tarball_config = tomllib.load(f)
                compress_type = tarball_config.get("tarball", {}).get("compress", "gzip")
                tar_sections = tarball_config.get("tar", {})

                if compress_type not in COMPRESS_MAP:
                    print(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
                    sys.exit(1)
        finally:
            # Clean up temp file
            if tmp_toml_path.exists():
                tmp_toml_path.unlink()

        # Add tarball.toml transfer
        transfers.append((f"{source_host}:{backup_dir_path}/{DEFAULT_TARBALL_TOML}",
                         f"{dest_host}:{remote_dir}/", DEFAULT_TARBALL_TOML))

        # Add all tar archives based on tar sections
        ext, _ = COMPRESS_MAP[compress_type]
        for category in tar_sections.keys():
            archive_name = f"{category}{ext}"
            transfers.append((f"{source_host}:{backup_dir_path}/{archive_name}",
                            f"{dest_host}:{remote_dir}/", archive_name))
    else:
        # Local source - we can enumerate files directly
        toml_path = local_backup / DEFAULT_TARBALL_TOML
        if not toml_path.exists():
            print(f"Error: {DEFAULT_TARBALL_TOML} not found in {local_backup}", file=sys.stderr)
            sys.exit(1)

        transfers.append((str(toml_path), f"{dest_host}:{remote_dir}/", DEFAULT_TARBALL_TOML))

        # Load compression type from tarball.toml
        with open(toml_path, "rb") as f:
            tarball_config = tomllib.load(f)
        compress_type = tarball_config.get("tarball", {}).get("compress", "gzip")
        if compress_type not in COMPRESS_MAP:
            print(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
            sys.exit(1)
        ext, _ = COMPRESS_MAP[compress_type]

        # Add all tar archives with the specified compression type
        for archive in local_backup.glob(f"*{ext}"):
                transfers.append((str(archive), f"{dest_host}:{remote_dir}/", archive.name))

    # Add config transfer
    if source_host:
        config_src = f"{config_src_base}/{config_name}"
    else:
        config_src = str(root_dir / config_name)
    transfers.append((config_src, f"{dest_host}:{remote_dir}/{DEFAULT_CONFIG_RESTORE}", config_name))

    # Add script transfers
    if deploy_config and "scripts" in deploy_config:
        for when in ["before", "after"]:
            if when in deploy_config["scripts"]:
                for run_script_path in deploy_config["scripts"][when]:
                    if source_host:
                        script_src = f"{config_src_base}/{run_script_path}"
                        script_name = Path(run_script_path).name
                        transfers.append((script_src, f"{dest_host}:{remote_dir}/", script_name))
                    else:
                        script_path = root_dir / run_script_path
                        if not script_path.exists():
                            print(f"Warning: Script {script_path} not found, skipping", file=sys.stderr)
                            continue
                        script_name = Path(run_script_path).name
                        transfers.append((str(script_path), f"{dest_host}:{remote_dir}/", script_name))

    # Execute all transfers in parallel
    success_count = rsync_parallel(transfers)
    if success_count < len(transfers):
        print("Error: Failed to copy all files to remote", file=sys.stderr)
        sys.exit(1)

    if args.run_scripts:
        run_scripts(
                root_dir, deploy_config, "before", 
                working_dir=None, remote_host=dest_host, remote_dir=remote_dir
        )

    # Execute restore on remote
    escaped_remote_dir = shlex.quote(remote_dir)
    program = f'{sys.executable} {SCRIPT.name}'
    remote_cmd = f"cd {escaped_remote_dir} && sudo {program} restore -b {escaped_remote_dir}"
    print(f"\nExecuting restore on {dest_host}...")
    print(f"Command: {remote_cmd}\n")
    ssh_run(dest_host, remote_cmd, tty=True)

    if args.run_scripts:
        run_scripts(
                root_dir, deploy_config, "after", 
                working_dir=None, remote_host=dest_host, remote_dir=remote_dir
        )

    print("\nCleaning up remote directory...")
    ssh_run(dest_host, f"rm -rf {shlex.quote(remote_dir)}")

    print(f"\n✓ Deploy completed successfully")


# --- Main Execution --- #

def cmd_backup(args):
    """Execute backup command (local or send to remote)."""
    root_dir = args.root
    dest_host = getattr(args, 'host', None)
    config = load_config(root_dir, DEFAULT_CONFIG_BACKUP)
    verify_backup_config(config, DEFAULT_CONFIG_BACKUP)

    # Determine intended destination path
    if args.dir:
        intended_path = args.dir.resolve()
    else:
        now = datetime.now()
        year = now.strftime("%Y")
        month_day = now.strftime("%m-%d")
        intended_path = root_dir / "backups" / year / month_day

    # Always use temp directory for creation (atomic operation)
    tmpdir = None
    if DRY_RUN:
        outdir = Path("/tmp/dry-run") / intended_path.name
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix=f"{SCRIPT.stem}-"))
        outdir = tmpdir / intended_path.name
        outdir.mkdir(parents=True, exist_ok=False)

    print(f"Backup destination: {intended_path}\n")

    try:
        # Create backup archives (returns final path with checksum subdirectory)
        final_dir = backup(args, root_dir, outdir, config)

        if DRY_RUN:
            print(f"[DRY-RUN] mv {final_dir} {intended_path / final_dir.name}")
        elif dest_host:
            # Copy backup to remote host
            remote_path = intended_path / final_dir.name
            remote_backup(final_dir, dest_host, remote_path)
        else:
            # Move backup to local destination
            final_dest = intended_path / final_dir.name

            # Ensure parent directory exists
            final_dest.parent.mkdir(parents=True, exist_ok=True)

            # Check if destination already exists
            if final_dest.exists():
                print(f"Error: Backup directory {final_dest} already exists", file=sys.stderr)
                sys.exit(1)

            # Move from temp to final location
            shutil.move(str(final_dir), str(final_dest))
            print(f"\n✓ Backup saved to {final_dest}")

    finally:
        # Cleanup temp directory if we created one
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)


def cmd_restore(args):
    """Execute restore command (local or remote)."""
    root_dir = args.root
    source_host = getattr(args, 'source', None)
    dest_host = getattr(args, 'host', None)

    if not args.dir:
        print("Error: -b/--backup-path is required", file=sys.stderr)
        sys.exit(1)

    # Combine date directory with checksum to get full backup path
    # args.dir is the date directory (e.g., ~/.snap/backups/2024/01-15)
    # args.checksum is the checksum prefix (e.g., a1b2c3d)
    # Result: ~/.snap/backups/2024/01-15/a1b2c3d

    # Auto-select most recent checksum if not provided
    if not args.checksum:
        date_dir = args.dir
        if not date_dir.exists():
            print(f"Error: Backup directory {date_dir} does not exist", file=sys.stderr)
            sys.exit(1)

        subdirs = [d for d in date_dir.iterdir() if d.is_dir()]
        if not subdirs:
            print(f"Error: No backup found in {date_dir}", file=sys.stderr)
            sys.exit(1)

        # Sort by modification time (most recent first)
        latest = max(subdirs, key=lambda d: d.stat().st_mtime)
        args.checksum = latest.name
        print(f"Auto-selected latest backup: {args.checksum}")

    backup_path = args.dir / args.checksum

    # Store combined path back in args for subfunctions
    args.backup_dir = backup_path

    # Case 1: Remote to remote (source -> dest)
    if source_host and dest_host:
        remote_deploy(args, root_dir, dest_host, source_host=source_host)
    # Case 2: Local to remote (deploy)
    elif dest_host:
        remote_deploy(args, root_dir, dest_host)
    # Case 3: Remote to local (pull and restore)
    elif source_host:
        remote_restore(args, root_dir, source_host)
    # Case 4: Local restore
    else:
        restore(args, root_dir)


def setup_parser():
    # Helper to add configuration to any parser
    def add_snap_options(parser, dir_help):
        group = parser.add_argument_group('configuration')
        group.add_argument("-r", "--snap-root", metavar="<root>", type=Path, default=DEFAULT_ROOT_SNAP,
                           dest='root', help=f"Root directory for backups, scripts, etc (default: {DEFAULT_ROOT_SNAP})")
        group.add_argument("-b", "--backup-path", metavar='<dir>', type=Path, dest='dir', help=dir_help)
        group.add_argument("-t", "--config-toml", metavar='<config>', dest='config_toml', help="Use custom TOML config file")
        group.add_argument("--run-scripts", action="store_true", help="Run before/after scripts with default config")

    # Helper to add CLI options to any parser
    def add_cli_options(parser):
        group = parser.add_argument_group('cli options')
        group.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
        group.add_argument("--verbose", action="store_true", help="Show detailed progress information")
        group.add_argument('--help', action="help", help="Show this help message and exit")

    # Main parser: displays configuration at top level (snap.py --help)
    raw_formatter = argparse.RawDescriptionHelpFormatter
    main_parser = argparse.ArgumentParser(
        add_help=False, formatter_class=raw_formatter,
        description="Backup and restore utility for creating system snapshots",
    )
    subparsers = main_parser.add_subparsers(dest="command", required=True, title='commands', metavar='')

    add_cli_options(main_parser)

    # Backup subcommand
    backup_parser = subparsers.add_parser("backup", formatter_class=raw_formatter, add_help=False,
                                          help=f"Creates a backup (can send to remote)")
    add_snap_options(backup_parser, "Backup to a custom directory (checksum subdir will be created)")

    backup_group = backup_parser.add_argument_group('backup options')
    backup_group.add_argument("--host", metavar="<host>", help="Destination host to copy backup to (user@hostname)")

    add_cli_options(backup_parser)

    # Restore subcommand
    restore_parser = subparsers.add_parser("restore", formatter_class=raw_formatter,
                                           add_help=False, help="Restore from a backup (local or remote)")
    add_snap_options(restore_parser, "Date directory containing backup (e.g., <root>/backups/YYYY/MM-DD)")

    restore_group = restore_parser.add_argument_group('restore options')
    restore_group.add_argument("-c", "--checksum", metavar="<hash>", help="Backup checksum prefix (auto-selects latest if omitted)")
    restore_group.add_argument("-s", "--source-host", metavar="<host>", dest='source', help="Source host to pull backup from (user@hostname)")
    restore_group.add_argument("-h", "--restore-host", metavar="<host>", dest='host', help="Destination host to restore on (user@hostname)")
    restore_group.add_argument("--disable-backups", action="store_true", help="Disable automatic backup of existing files (requires confirmation)")

    add_cli_options(restore_parser)

    return main_parser


def main():
    global DRY_RUN, VERBOSE

    parser = setup_parser()
    args = parser.parse_args()

    # Set global flags
    DRY_RUN = getattr(args, 'dry_run', False)
    VERBOSE = getattr(args, 'verbose', False)

    # Resolve root directory (checks PWD for .snap, then ~/.snap)
    args.root = resolve_root(args.root)

    if DRY_RUN:
        print("[DRY-RUN] No changes will be made\n")

    if args.command == "backup":
        cmd_backup(args)
    elif args.command == "restore":
        cmd_restore(args)


if __name__ == "__main__":
    main()

