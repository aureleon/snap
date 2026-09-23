#!/usr/bin/env python3
"""
Snapshot and restore utility.
"""

__version__ = "0.1"

import sys

# Require Python 3.11+ for tomllib
if sys.version_info < (3, 11):
    print("Error: This script requires Python 3.11 or later", file=sys.stderr)
    sys.exit(1)

import argparse
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import stat
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

__dry_run__ = False
__verbose__ = False

__script__ = Path(__file__).resolve()


def printd(*args, **kwargs):
    """Adds [DRY-RUN] to output if __dry_run__=True."""
    if not __dry_run__:
        print(*args, **kwargs)
        return
    if not args:
        print("[DRY-RUN]", **kwargs)
        return
    space = {"\r", "\n", "\r\n"}
    nargs = [(f"[DRY-RUN]" if arg in space else f"[DRY-RUN] {arg}") for arg in args]
    if "sep" not in kwargs:
        kwargs["sep"] = "\n"
    print(*nargs, **kwargs)


# --- Subprocess Runners --- #

SSH_ALIVE_INTERVAL = 10
SSH_CONNECT_TIMEOUT = 30

TIMEOUT_BUFFER = 60
COMMAND_TIMEOUT = 300
MIN_RSYNC_TIMEOUT = 300
MAX_RSYNC_TIMEOUT = 7200

REMOTE_DIR_SUFFIX_LENGTH = 8
AVG_DOWNLOAD_RATE = 50


def ssh_run(host, *commands, check=True, stdin_data=None, tty=False, quiet=False):
    """Execute commands on remote host with timeout and proper error handling."""
    command_str = " && ".join(commands)

    ssh_args = [
        "ssh",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o",
        f"ServerAliveInterval={SSH_ALIVE_INTERVAL}",
    ]
    if tty:
        ssh_args.append("-t")
    ssh_args.extend([host, command_str])

    if __dry_run__:
        if quiet:
            return None
        printd(f"ssh {host}:")
        for cmd in commands:
            printd(f"  {cmd}")
        if stdin_data:
            if len(stdin_data) > 100:
                printd(f"  stdin: {stdin_data[:100]}...")
            else:
                printd(f"  stdin: {stdin_data}")
        return None

    try:
        # For TTY operations, don't capture output (interactive)
        if tty:
            result = subprocess.run(
                ssh_args,
                check=check,
                timeout=COMMAND_TIMEOUT,
                input=stdin_data,
                text=stdin_data is not None,
            )
            return (result.returncode, "", "")

        # For non-TTY, capture output
        result = subprocess.run(
            ssh_args,
            check=check,
            timeout=COMMAND_TIMEOUT,
            input=stdin_data,
            text=True,
            capture_output=True,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        printd(f"Error: Command timed out on {host}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        # Print captured output on error before exiting
        if e.stdout:
            printd(e.stdout, end="")
        if e.stderr:
            printd(e.stderr, end="", file=sys.stderr)
        printd(f"Error: SSH command failed on {host}", file=sys.stderr)
        sys.exit(1)


def rsync_run(source, dest, check=True):
    """Execute rsync with timeout based on file size.

    Returns tuple: (returncode, stdout, stderr)
    """
    # Simple timeout calculation for archives and small files
    # We transfer: .tar.gz archives, snapshot TOML, snap.py, configs, scripts
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
    if __verbose__:
        rsync_cmd.append("--info=progress2")
    rsync_cmd.extend([f"--timeout={timeout}", source, dest])

    if __dry_run__:
        printd(f"{shlex.join(rsync_cmd)}")
        # Show file size if available
        if ":" not in source and Path(source).is_file():
            size_mb = Path(source).stat().st_size / (1024 * 1024)
            printd(f"  file size: {size_mb:.2f} MB, timeout: {timeout}s")
        return None

    try:
        result = subprocess.run(
            rsync_cmd,
            check=check,
            timeout=timeout + TIMEOUT_BUFFER,
            capture_output=True,
            text=True,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        printd(f"Error: rsync timed out copying {source} to {dest}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        # Print captured output on error before exiting
        if e.stdout:
            printd(e.stdout, end="")
        if e.stderr:
            printd(e.stderr, end="", file=sys.stderr)
        printd(f"Error: rsync failed copying {source} to {dest}", file=sys.stderr)
        sys.exit(1)


def dtqdm(total, desc="", unit="item", autorefresh=None, **kwargs):
    """Return tqdm progress bar or no-op context in __dry_run__ mode.

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
            printd(msg)

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

    if __dry_run__:
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

    printd(f"Transferring {len(transfers)} file(s) in parallel...")
    if __verbose__:
        for source, dest, desc in transfers:
            printd(f"  {desc}: {source} -> {dest}")

    if __dry_run__:
        return len(transfers)
    success_count = 0

    with dtqdm(
        len(transfers),
        "  Transfer progress",
        " files",
        bar_format="{desc}: {n}/{total} [{elapsed}, {rate_fmt}]",
        autorefresh=True,
    ) as pbar:

        def transfer_with_desc(transfer):
            source, dest, desc = transfer
            try:
                if __verbose__:
                    pbar.write(f"  Starting: {desc}")
                returncode, stdout, stderr = rsync_run(source, dest)
                # Print any output after transfer completes
                if stdout and __verbose__:
                    pbar.write(stdout.strip())
                if stderr:
                    pbar.write(f"  rsync stderr: {stderr.strip()}")
                if __verbose__:
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

    printd()
    if success_count < len(transfers):
        printd(
            f"Warning: Only {success_count}/{len(transfers)} transfers succeeded",
            file=sys.stderr,
        )

    return success_count


def run_scripts(root_dir, config, when, working_dir, remote_host=None, remote_dir=None):
    """Run scripts before or after an operation (local or remote).

    Args:
        root_dir: Root directory containing configs and scripts
        config: Loaded config dict
        when: "before" or "after"
        working_dir: Directory to cd into before running scripts (capture directory)
        remote_host: If set, run on remote host
        remote_dir: Remote directory (when remote_host is set)
    """
    if not config or "scripts" not in config:
        printd(
            "\n",
            f"Config has no [scripts] table, skipping {when} script execution...",
            "\n",
        )
        return

    if when not in config["scripts"]:
        printd(
            "\n",
            f"No {when} list defined in [scripts] table, skipping execution...",
            "\n",
        )
        return

    scripts = config["scripts"][when]
    if not scripts:
        printd("\n", f"Skipping {when} script execution ([scripts].{when} = [])", "\n")
        return

    if remote_host:
        printd("\n", f"Running {when} scripts on remote...", "\n")
    else:
        printd("\n", f"Running {when} scripts...", "\n")

    for run_script_path in scripts:
        script_path = root_dir / run_script_path
        if not script_path.exists():
            printd(
                f"  Warning: Script {script_path} not found, skipping", file=sys.stderr
            )
            continue
        script_name = script_path.name

        if remote_host:
            # Run on remote with sudo, in the remote capture directory
            # Use proper shell escaping
            escaped_dir = shlex.quote(remote_dir)
            escaped_script = shlex.quote(script_name)
            cmds = [
                f"cd {escaped_dir}",
                f"chmod +x {escaped_script}",
                f"sudo bash {escaped_script}",
            ]
            returncode, stdout, stderr = ssh_run(remote_host, *cmds)
            # Print script output after completion
            if stdout:
                printd(stdout, end="")
            if stderr:
                printd(stderr, end="", file=sys.stderr)
            printd(f"return code: {returncode}")
        else:
            # Run locally in the capture directory
            script_cmd = ["bash", str(script_path)]
            if __dry_run__:
                printd(f"{shlex.join(script_cmd)}")
                if working_dir:
                    printd(f"  cwd: {working_dir}")
                printd(f"  timeout: {COMMAND_TIMEOUT}s")
            else:
                try:
                    result = subprocess.run(
                        script_cmd,
                        check=True,
                        cwd=working_dir,
                        timeout=COMMAND_TIMEOUT,
                        capture_output=True,
                        text=True,
                    )
                    # Print script output after completion
                    if result.stdout:
                        printd(result.stdout, end="")
                    if result.stderr:
                        printd(result.stderr, end="", file=sys.stderr)
                    printd(f"return code: {result.returncode}")
                except subprocess.TimeoutExpired:
                    printd(f"Error: Script {script_name} timed out", file=sys.stderr)
                    sys.exit(1)
                except subprocess.CalledProcessError as e:
                    # Print captured output on error
                    if e.stdout:
                        printd(e.stdout, end="")
                    if e.stderr:
                        printd(e.stderr, end="", file=sys.stderr)
                    printd(f"Error: Script {script_name} failed", file=sys.stderr)
                    sys.exit(1)


# --- Snap Configuration --- #

DEFAULT_ROOT_SNAP = "~/.snap"

DEFAULT_CONFIG_CAPTURE = "configs/capture.toml"
DEFAULT_CONFIG_DEPLOY = "configs/deploy.toml"
DEFAULT_CONFIG_RESTORE = "configs/restore.toml"
DEFAULT_CONFIG_MIGRATE = "configs/migrate.toml"

DEFAULT_SNAPSHOT_TOML = "snapshot.toml"


def parse_remote_arg(value):
    """Parse a [user@]host:[path], user@host, or local path argument into (host, path).

    Returns (None, None) for empty values, (None, path) for local paths,
    or (host, path) for remote. 'user@host' and 'host:' return (host, None).
    A local path containing '@' needs a '/' (e.g. './name@tag').
    """
    if not value:
        return None, None

    # Match [user@]host: or [user@]host:path
    match = re.fullmatch(r"((?:[^/:]+@)?[^/:]+):(.*)", value)
    if match:
        host, path = match.group(1), match.group(2)
    elif re.fullmatch(r"[^/:]+@[^/:]+", value):
        # user@host with no colon
        host, path = value, ""
    else:
        return None, Path(value)

    # Reject empty parts and hosts that ssh/rsync would parse as options
    user, at, hostname = host.rpartition("@")
    if not hostname or hostname.startswith("-") or user.startswith("-") or (at and not user):
        printd(f"Error: Invalid host '{host}'", file=sys.stderr)
        sys.exit(1)

    return host, Path(path) if path else None


def expand_path(path_str):
    """Expand environment variables and user home in a path string."""
    return Path(os.path.expandvars(path_str)).expanduser()


def resolve_root(specified_root, root_host=None):
    """Resolve the snap root directory.

    When root_host is set, trust the remote path without local checks.

    Search order (local only):
    1. Use specified_root if provided (and it's not the default)
    2. Check for .snap in current directory
    3. Check for ~/.snap
    4. Error if none exist
    """
    # Remote root - trust the path, can't check remotely
    if root_host:
        if specified_root:
            return Path(specified_root)
        return Path(DEFAULT_ROOT_SNAP)

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
    printd("Error: No .snap directory found", file=sys.stderr)
    printd(f"  Searched: {pwd_root}", file=sys.stderr)
    printd(f"  Searched: {home_root}", file=sys.stderr)
    printd(
        "\n",
        "Create a .snap directory manually or specify a custom location with --snap-root",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_snapshot_root(captures_dir):
    """Find the most recent capture snapshot under captures/.

    Tries today first, then scans all YYYY/MM-DD directories for the latest.
    """
    if not captures_dir.exists():
        printd(f"Error: Captures directory {captures_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    # Try today first
    now = datetime.now()
    today_path = captures_dir / now.strftime("%Y") / now.strftime("%m-%d")
    if today_path.exists() and any(today_path.iterdir()):
        return today_path

    # Scan all YYYY/MM-DD directories and find the most recent
    latest = None
    for year_dir in sorted(captures_dir.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        for date_dir in sorted(year_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            if any(date_dir.iterdir()):
                latest = date_dir
                break
        if latest:
            break

    if not latest:
        printd(f"Error: No captures found in {captures_dir}", file=sys.stderr)
        sys.exit(1)

    printd(f"Auto-selected latest date: {latest.relative_to(captures_dir)}")
    return latest


def resolve_remote_snapshot(host, captures_dir):
    """Find the most recent capture snapshot on a remote host via SSH.

    Returns the full path to the latest checksum directory (YYYY/MM-DD/hash).
    Uses reverse-sorted ls to find the newest date dir, then newest subdir.
    """
    escaped = shlex.quote(str(captures_dir))

    # Find latest YYYY/MM-DD/checksum in one command:
    # ls -1d captures/YYYY/MM-DD/*/ sorted reverse, take first
    find_cmd = (
        f"find {escaped} -mindepth 3 -maxdepth 3 -type d"
        f" | sort -r | head -1"
    )

    if __dry_run__:
        printd(f"Finding latest snapshot on {host}...")
        ssh_run(host, find_cmd, quiet=True)
        # Return a placeholder path for dry-run
        return captures_dir / "YYYY" / "MM-DD" / "latest"

    result = ssh_run(host, find_cmd, check=False)
    if result is None or result[0] != 0 or not result[1].strip():
        printd(
            f"Error: No captures found on {host}:{captures_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    latest = Path(result[1].strip())
    printd(f"Auto-selected latest snapshot on {host}: {latest.relative_to(captures_dir)}")
    return latest


def load_config(root_dir, config_name):
    """Load a TOML config file from the root directory."""
    config_file = root_dir / config_name

    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        printd(f"Error: Config file {config_file} not found", file=sys.stderr)
        sys.exit(1)
    except tomllib.TOMLDecodeError as e:
        printd(f"Error parsing TOML: {e}", file=sys.stderr)
        sys.exit(1)


def toml_string(value):
    """Format a string as a TOML basic string."""
    # JSON escapes quotes, backslashes, and control characters the way TOML does,
    # except DEL, which TOML also requires to be escaped
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_value(value):
    """Format a string, number, bool, or list of them as a TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {value!r}")


def toml_dumps(config):
    """Format a dict of tables of values (one level deep) as TOML."""
    lines = []
    for table, values in config.items():
        lines.append(f"[{toml_string(table)}]")
        for key, value in values.items():
            lines.append(f"{toml_string(key)} = {toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def load_remote_config(root_host, root_path, config_name):
    """Fetch and load a TOML config from a remote host."""
    remote_src = f"{root_host}:{root_path / config_name}"
    tmp = Path(tempfile.mktemp(suffix=".toml", prefix="remote-config-"))

    try:
        if __dry_run__:
            printd(f"Fetch config from {remote_src}")
            return {}

        result = rsync_run(remote_src, str(tmp))
        if result is None or result[0] != 0:
            printd(
                f"Error: Failed to fetch {config_name} from {root_host}",
                file=sys.stderr,
            )
            sys.exit(1)

        with open(tmp, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        printd(f"Error parsing remote TOML: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if tmp.exists():
            tmp.unlink()


def verify_capture_config(config, config_name):
    """Verify capture configuration structure."""
    if "tar" not in config:
        printd(f"Error: {config_name} missing required [tar] section", file=sys.stderr)
        sys.exit(1)

    tar_config = config["tar"]
    if not tar_config or not isinstance(tar_config, dict):
        printd(
            f"Error: {config_name} [tar] section is empty or invalid", file=sys.stderr
        )
        sys.exit(1)

    # Verify each category has required fields
    for category, data in tar_config.items():
        if not isinstance(data, dict):
            printd(
                f"Error: {config_name} [tar.{category}] must be a table",
                file=sys.stderr,
            )
            sys.exit(1)

        if "root" not in data:
            printd(
                f"Error: {config_name} [tar.{category}] missing required 'root' field",
                file=sys.stderr,
            )
            sys.exit(1)

        if "dirs" not in data and "files" not in data:
            printd(
                f"Error: {config_name} [tar.{category}] must have 'dirs' or 'files' field",
                file=sys.stderr,
            )
            sys.exit(1)


def verify_restore_config(config, config_name):
    """Verify restore/deploy configuration structure."""
    if "tar" not in config:
        printd(f"Error: {config_name} missing required [tar] section", file=sys.stderr)
        sys.exit(1)

    tar_config = config["tar"]
    if not isinstance(tar_config, dict):
        printd(f"Error: {config_name} [tar] section must be a table", file=sys.stderr)
        sys.exit(1)

    if "archives" not in tar_config:
        printd(
            f"Error: {config_name} [tar] section missing 'archives' field",
            file=sys.stderr,
        )
        sys.exit(1)

    archives = tar_config["archives"]
    if archives is not None and not isinstance(archives, list):
        printd(
            f"Error: {config_name} [tar].archives must be a list or null",
            file=sys.stderr,
        )
        sys.exit(1)


def verify_snapshot(capture_dir):
    """Validate that a capture directory contains required files."""
    if __dry_run__ and not capture_dir.exists():
        return None

    if not capture_dir.exists():
        printd(
            f"Error: Snapshot directory {capture_dir} does not exist", file=sys.stderr
        )
        sys.exit(1)

    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML
    if not toml_path.exists():
        printd(
            f"Error: {DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load compression type from snapshot TOML
    with open(toml_path, "rb") as f:
        snapshot_config = tomllib.load(f)

    compress_type = snapshot_config.get("tarball", {}).get("compress", "gzip")
    if compress_type not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
        sys.exit(1)

    ext, _ = COMPRESS_MAP[compress_type]

    # Check that at least one tar archive exists
    archives = list(capture_dir.glob(f"*{ext}"))
    if not archives:
        printd(f"Error: No tar archives found in {capture_dir}", file=sys.stderr)
        sys.exit(1)

    return toml_path


# --- Archive Handlers --- #


# Compression type mappings: extension, write mode
# Read mode is always 'r:*' for auto-detection
COMPRESS_MAP = {
    "gzip": (".tar.gz", "w:gz"),
    "bzip2": (".tar.bz2", "w:bz2"),
    "xz": (".tar.xz", "w:xz"),
    "": (".tar", "w"),
}


def archive_classify(paths, truthy, falsey, cond):
    """
    Formats `paths` into two lists of text, 'truthy' and 'falsey',
    decided based on lambda `cond(path)` for each path in `paths`.
    """
    groups = {truthy: [], falsey: []}
    for path in paths:
        if cond(path):
            groups[truthy] += [path]
        else:
            groups[falsey] += [path]
    if not groups[truthy]:
        return [], []
    if __verbose__:
        for prefix in groups:
            group = groups[prefix]
            for n, path in enumerate(group):
                group[n] = f"{prefix}: '{path}'"
    else:
        for prefix in groups:
            if not groups[prefix]:
                continue
            length = len(groups[prefix])
            suffix = "s" * (length > 1)
            msg = f"{prefix}: {length} path{suffix}"
            groups[prefix] = [msg]
    return groups[truthy], groups[falsey]


def archive_filter(tarinfo):
    """Filters metadata files from archives."""
    if sys.platform == "darwin":
        # Skip ._* resource fork files and .DS_Store
        re_metadata = r"^(?:\._.*)|(?:.*/?.DS_Store)$"
        if re.match(re_metadata, tarinfo.name):
            return None
    return tarinfo


def archive_expand(patterns, root_path):
    """Expand glob patterns in file/directory lists."""
    matched_paths = set()
    expanded, warnings = [], []
    for pattern in patterns:
        # Check if pattern contains glob characters
        if any(char in pattern for char in ["*", "?", "[", "]"]):
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
                warnings += [f"skipped: pattern '{pattern}'"]
        else:
            # Literal path - add as-is
            if pattern not in matched_paths:
                expanded.append(pattern)
                matched_paths.add(pattern)
    return expanded, warnings


def archive_create(name, root, paths, outdir, compress="gzip"):
    """Create a compressed tar archive for a category."""
    if compress not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    ext, mode = COMPRESS_MAP[compress]
    archive_path = outdir / f"{name}{ext}"
    root_path = expand_path(root)
    warnings = []

    # Expand any glob patterns in the paths
    expanded_paths, expand_warnings = archive_expand(paths, root_path)

    lines = [f"  {archive_path.name} (root: '{root_path}')"]
    includes, warnings = archive_classify(
        expanded_paths,
        "include",
        "skipping",
        cond=lambda path: (root_path / path).exists(),
    )
    lines += includes
    warnings += expand_warnings

    if __dry_run__:
        return archive_path, lines, warnings

    # Don't show individual progress bars during parallel creation
    # (avoids terminal corruption and empty lines)
    with tarfile.open(archive_path, mode) as tar:
        for path in expanded_paths:
            full_path = root_path / path
            if not full_path.exists():
                continue
            tar.add(full_path, arcname=str(path), filter=archive_filter)

    return archive_path, lines, warnings


def create_archives(tasks, compress="gzip"):
    """Create multiple archives in parallel.

    Args:
        tasks: List of (category, root, paths, outdir) tuples
        compress: Compression type to use for all archives
    """
    if not tasks:
        return []

    results, buffer = [], {}
    printd("\n", f"Creating {len(tasks)} archive(s)...", "\n")
    with ccft.ThreadPoolExecutor(max_workers=len(tasks)) as exc:
        futures = {exc.submit(archive_create, *task, compress): task for task in tasks}
        with dtqdm(
            len(tasks),
            "Overall progress",
            unit=" archives",
            bar_format="{desc}: {n}/{total} [{elapsed}, {rate_fmt}]",
            autorefresh=True,
        ) as pbar:
            for future in ccft.as_completed(futures):
                task = futures[future]
                category = task[0]
                try:
                    archive_path, lines, warnings = future.result()
                    buffer[archive_path.name] = [*lines, *warnings]
                    results.append(archive_path)
                except Exception as e:
                    pbar.write(f"\nError creating archive {category}: {e}")
                pbar.update(1)
    for arcname in sorted(buffer):
        header, *lines = buffer[arcname]
        printd(header)
        if not any("include: " in l for l in lines):
            lines = ["warning: no files matched"]
        for line in map(lambda s: f"    {s}", lines):
            if "include:" in line:
                printd(line)
            else:
                printd(line, file=sys.stderr)
        printd()
    return results


def generate_snapshot_toml(
    outdir, compress="gzip", category_meta=None, roll_ext=None
):
    """Generate snapshot TOML with checksums for all archives."""
    if category_meta is None:
        category_meta = {}

    # Find archives based on compression type
    if compress not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    ext, _ = COMPRESS_MAP[compress]

    toml_path = outdir / DEFAULT_SNAPSHOT_TOML
    printd(f"  writing {toml_path.name}")
    printd(f"    added tarball options")

    if __dry_run__:
        printd(f"    calculated archive checksums")
        return toml_path, "abcd123"

    archives = list(outdir.glob(f"*{ext}"))

    # Calculate checksums for all archives
    checksums = {}
    with dtqdm(
        len(archives),
        "  Calculating checksums",
        " archives",
        bar_format="{desc}: {n}/{total} [{elapsed}, {rate_fmt}]",
        autorefresh=True,
    ) as pbar:
        for archive in archives:
            category = archive_category(archive)
            checksum = calculate_file_checksum(archive, show=False)
            checksums[category] = checksum
            pbar.update(1)

    printd()

    # Generate TOML content
    toml_lines = ["#", "# Capture Configuration TOML", "#", "[tarball]"]
    if compress != "gzip":
        toml_lines += [f'compress = "{compress}"  # tar compression type']
    if roll_ext:
        toml_lines += [f'rollback = "{roll_ext}"  # rollback intermediate extension']
    toml_lines += ['checksum = "sha256"  # checksum digest type']
    toml_lines += [""]

    for category in sorted(checksums.keys()):
        checksum = checksums[category]
        toml_lines += [f"[tar.{category}]"]

        meta = category_meta.get(category, {})
        if "root" in meta:
            toml_lines += [f'root = "{meta["root"]}"']
        if meta.get("link"):
            toml_lines += [f'link = "{meta["link"]}"']

        toml_lines += [f'checksum = "{checksum}"']
        toml_lines += [""]

    # Write TOML file
    with open(toml_path, "w") as f:
        f.write("\n".join(toml_lines))
    printd(f"  Done: {toml_path.name}")

    # Combined checksum from all archive checksums
    combined = "".join(checksums[cat] for cat in sorted(checksums))
    return toml_path, calculate_checksum(combined.encode("utf-8"))


# --- Archive Extraction Handlers --- #


def archive_category(archive):
    """Extract category name from archive filename."""
    return archive.stem.replace(".tar", "")


def archive_entries(tar, warn=True):
    """Group archive members by captured path, in archive order.

    A captured path is a member with no ancestor member: capturing '.config/nvim'
    yields '.config/nvim', not '.config', so restore never touches its siblings.
    Members below a symlink member are skipped, since extracting them would write
    through the link into paths that were never backed up.
    Raises ValueError for absolute or '..' member paths.
    """
    members = tar.getmembers()
    names, links = set(), set()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe path in archive: '{member.name}'")
        if path.parts:
            names.add(path)
            if member.issym():
                links.add(path)

    groups = {}
    for member in members:
        path = Path(member.name)
        if not path.parts:
            continue

        # Ancestors from shallowest to deepest, without '.'
        ancestors = list(reversed(path.parents))[1:]
        if any(ancestor in links for ancestor in ancestors):
            if warn:
                printd(
                    f"Warning: skipping '{member.name}' below a symlink in the archive",
                    file=sys.stderr,
                )
            continue

        entry = next((a for a in ancestors if a in names), path)
        groups.setdefault(str(entry), []).append(member)
    return groups


def archive_confirm(archive, compress="gzip", root=None):
    """Ask user for confirmation to extract archive."""
    name = archive_category(archive)

    if compress not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    # Get captured paths from the archive (auto-detect compression)
    try:
        with tarfile.open(archive, "r:*") as tar:
            entries = archive_entries(tar)
    except (tarfile.TarError, ValueError, OSError) as e:
        printd(f"Error: Cannot restore {name}: {e}", file=sys.stderr)
        return False

    # Format paths for display
    top_level_dirs = set()
    for entry in entries:
        if root:
            top_level_dirs.add(str(Path(root) / entry))
        else:
            top_level_dirs.add("/" + entry)

    # Show what will be restored
    printd("\n", f"{name} will restore to:")
    for d in sorted(top_level_dirs):
        printd(f"  {d}")

    if __dry_run__:
        printd(f"Restore {name}?  [Y/N]: Y")
        return True

    # Prompt user for confirmation
    try:
        msg = f"Restore {name}? This will overwrite any existing files... [Y/N]:"
        response = input(msg).strip().lower()
    except (KeyboardInterrupt, EOFError):
        printd("\n", f"  Skipping {name}")
        return False

    if response not in ("y", "yes"):
        printd(f"  Skipping {name}")
        return False

    return True


def archive_select(available, selected_archives, tmpdir, compress="gzip"):
    """Select which archives to restore based on patterns."""
    if not selected_archives:
        return available

    if compress not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
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
                printd(f"skipped: pattern '{pattern}'", file=sys.stderr)

    return archives_to_restore


def copy_with_ownership(src, dst):
    """Copy a file, symlink, or directory tree, preserving ownership when running as root.

    Symlinks are copied as links, never followed.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    make_parents(dst_path)

    if src_path.is_symlink():
        os.symlink(os.readlink(src_path), dst_path)
    elif src_path.is_dir():
        shutil.copytree(src_path, dst_path, symlinks=True)
    else:
        shutil.copy2(src_path, dst_path)

    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return

    # Preserve ownership of every copied path without following symlinks
    copied = [dst_path]
    if dst_path.is_dir() and not dst_path.is_symlink():
        for root, dirs, files in os.walk(dst_path):
            copied += [Path(root) / name for name in dirs + files]
    for path in copied:
        src_stat = (src_path / path.relative_to(dst_path)).lstat()
        os.chown(path, src_stat.st_uid, src_stat.st_gid, follow_symlinks=False)


def make_parents(path, created=None):
    """Create the missing parent dirs of a path and return them, deepest first.

    Each new dir is added to `created` as soon as it exists, so a caller can remove
    it on rollback even if a later mkdir fails. When running as root, new dirs get
    the owner of their nearest existing ancestor, so restoring into a user's home
    doesn't leave root-owned dirs.
    """
    if created is None:
        created = []
    parent = Path(path).parent
    missing = []
    while not os.path.lexists(parent) and parent.parent != parent:
        missing.append(parent)
        parent = parent.parent

    owner = parent.stat()
    for directory in reversed(missing):
        directory.mkdir()
        created.insert(0, directory)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(directory, owner.st_uid, owner.st_gid)
    return created


def remove_path(path):
    """Remove a file, symlink, or directory tree if it exists.

    Read-only dirs inside the tree are made writable so their contents can go.
    """
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():

        def retry(func, failed, error):
            parent = Path(failed).parent
            inside = parent == path or path in parent.parents
            if func not in (os.unlink, os.rmdir) or not inside:
                raise error
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(parent, stat.S_IMODE(parent.lstat().st_mode) | stat.S_IRWXU)
            func(failed)

        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=lambda func, failed, info: retry(func, failed, info[1]))
    elif path.exists():
        path.unlink()


def extract_members(tar, path, members):
    """Extract archive members under path, with the 'tar' safety filter when available."""
    try:
        tar.extractall(path, members=members, filter="tar")
    except TypeError:
        # Python < 3.11.4 doesn't support the filter parameter
        tar.extractall(path, members=members)


def archive_extract(archive, compress="gzip", root=None):
    """Extract an archive without prompting (assumes already confirmed)."""
    name = archive_category(archive)

    if compress not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        sys.exit(1)

    # Determine extraction path
    if root:
        extract_path = expand_path(root)
    else:
        # Legacy: extract to / for absolute paths
        extract_path = Path("/")

    if __dry_run__:
        return True

    try:
        # Don't show individual progress bars during parallel extraction
        # (avoids terminal corruption and empty lines)
        # Use 'r:*' to auto-detect compression type
        with tarfile.open(archive, "r:*") as tar:
            # Same members as a rollback restore (nothing below a symlink), in archive
            # order; when running as root, tarfile preserves ownership from the archive
            kept = {id(m) for group in archive_entries(tar, warn=False).values() for m in group}
            members = [m for m in tar.getmembers() if id(m) in kept]
            extract_members(tar, extract_path, members)
        return True
    except Exception as e:
        printd("\n", f"Error extracting {name}: {e}", file=sys.stderr)
        return False


def restore_category(archive, root, roll_ext, compress="gzip"):
    """Transactionally restore a single category with rollback on failure.

    Replaces only the paths captured in the archive, one at a time:
    1. Backs up the existing path (copy, not move)
    2. Removes the original
    3. Extracts the captured path from the archive
    4. On any error or Ctrl-C: restores every touched path from backup
    """
    category = archive_category(archive)

    if compress not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress}", file=sys.stderr)
        return False

    root_path = expand_path(root)
    rollbackd = Path(str(root_path) + roll_ext)

    # Always open archive and classify entries
    try:
        with tarfile.open(archive, "r:*") as tar:
            groups = archive_entries(tar)
    except (tarfile.TarError, ValueError, OSError) as e:
        printd(f"Error: Cannot restore {category}: {e}", file=sys.stderr)
        return False

    # Classify entries: replace (existing) vs extract (new)
    replaces, extracts = archive_classify(
        list(groups),
        "replace",
        "extract",
        cond=lambda entry: os.path.lexists(root_path / entry),
    )

    printd(f"  {archive.name} (root: '{root_path}')")
    for line in extracts:
        printd(f"    {line}")
    for line in replaces:
        printd(f"    {line}", file=sys.stderr)

    if __dry_run__:
        return True

    # Move any existing backup aside, then create a fresh backup directory
    try:
        if rollbackd.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            prevbackd = Path(f"{rollbackd}_{timestamp}")
            count = 1
            while prevbackd.exists():
                prevbackd = Path(f"{rollbackd}_{timestamp}_{count}")
                count += 1
            rollbackd.rename(prevbackd)
            if __verbose__:
                printd(f"  Moved existing backup: {rollbackd} -> {prevbackd}")
        # Creates rollbackd and any missing parents with their parent's owner
        make_parents(rollbackd / "entry")
    except OSError as e:
        printd(f"Error: Cannot create backup directory {rollbackd}: {e}", file=sys.stderr)
        return False

    printd(f"  Restoring {category}...")
    touched = []  # (entry, has_backup, created parent dirs) for every changed path

    try:
        with tarfile.open(archive, "r:*") as tar:
            links = []
            for entry, members in groups.items():
                entry_path = root_path / entry
                has_backup = os.path.lexists(entry_path)

                # Back up the existing path, then remove it
                if has_backup:
                    copy_with_ownership(entry_path, rollbackd / entry)
                created = []
                touched.append((entry, has_backup, created))
                remove_path(entry_path)
                make_parents(entry_path, created)

                # Extract this entry's members from the archive
                extract_members(tar, root_path, [m for m in members if not m.islnk()])
                links += [m for m in members if m.islnk()]

            # Hardlinks last, so they link to restored files instead of old ones
            extract_members(tar, root_path, links)

        printd(f"  ✓ {category} restored")
        return True

    except (Exception, KeyboardInterrupt) as e:
        # Ignore further Ctrl-C so the rollback itself can't be cut short
        try:
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except ValueError:
            previous_sigint = None  # Not in the main thread
        try:
            restore_rollback(category, root_path, rollbackd, touched, e)
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)

        if isinstance(e, KeyboardInterrupt):
            raise
        return False


def restore_rollback(category, root_path, rollbackd, touched, error):
    """Delete touched paths and restore them from backup; exit if that fails."""
    reason = "interrupted" if isinstance(error, KeyboardInterrupt) else error
    printd("\n", f"Error restoring {category}: {reason}", file=sys.stderr)
    printd(f"Rolling back {len(touched)} path(s)...", file=sys.stderr)

    failures = []
    for entry, has_backup, created in reversed(touched):
        entry_path = root_path / entry
        try:
            remove_path(entry_path)
            if has_backup:
                copy_with_ownership(rollbackd / entry, entry_path)
        except OSError as rollback_error:
            failures.append(f"{entry}: {rollback_error}")
            continue

        # Remove parent dirs that were created for the extraction
        for directory in created:
            try:
                directory.rmdir()
            except OSError:
                break

    if failures:
        # Stop before a later category rotates these backups away
        printd(
            f"Error: Rollback incomplete for {category}, backups are in {rollbackd}",
            file=sys.stderr,
        )
        for failure in failures:
            printd(f"  {failure}", file=sys.stderr)
        printd("Stopping restore so the backups stay in place.", file=sys.stderr)
        sys.exit(1)

    printd(f"✓ Rollback complete for {category}", file=sys.stderr)


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
    printd("\n", f"Extracting {len(confirmed)} archive(s)...")
    success_count = 0

    with ccft.ThreadPoolExecutor(max_workers=len(confirmed)) as exc:
        # Build futures with root for each archive
        futures = {}
        for archive in confirmed:
            category = archive_category(archive)
            root = root_map.get(category)
            futures[exc.submit(archive_extract, archive, compress, root)] = archive

        with dtqdm(
            len(confirmed),
            "Extraction progress",
            " archives",
            bar_format="{desc}: {n}/{total} [{elapsed}, {rate_fmt}]",
            autorefresh=True,
        ) as pbar:
            for future in ccft.as_completed(futures):
                archive = futures[future]
                try:
                    if future.result():
                        success_count += 1
                except Exception as e:
                    pbar.write(f"\nError extracting {archive.name}: {e}")
                pbar.update(1)

    return success_count


def create_symlinks(snapshot_config):
    """Create symlinks from link -> root for each category with a link field."""
    tar_sections = snapshot_config.get("tar", {})
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

        # Check if link already exists
        if os.path.lexists(link_path):
            if os.path.islink(link_path):
                current_target = link_path.resolve()
                if current_target == root_path.resolve():
                    continue
                else:
                    printd(
                        f"Error: {link_path} already exists as symlink to {current_target}, expected {root_path}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            else:
                printd(
                    f"Error: {link_path} already exists and is not a symlink",
                    file=sys.stderr,
                )
                sys.exit(1)

        if not __dry_run__:
            make_parents(link_path)
            link_path.symlink_to(root_path)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                owner = link_path.parent.stat()
                os.chown(link_path, owner.st_uid, owner.st_gid, follow_symlinks=False)

        symlinks_created.append(str(link_path))

    if symlinks_created:
        printd("\n", f"✓ Created {len(symlinks_created)} symlink(s)")
        for link in symlinks_created:
            printd(f"  {link}")


# --- Checksum Handlers --- #

CHECKSUM_CHUNK_SIZE = 8192


def sha256_digest(data):
    """Calculate a SHA-256 digest."""
    sha256 = hashlib.sha256()
    sha256.update(data)
    return sha256.hexdigest()


def sha256_file_digest(filepath, show=False):
    """Calculate a SHA-256 digest from a file."""
    sha256 = hashlib.sha256()
    file_size = filepath.stat().st_size
    if show and file_size > 0:
        with open(filepath, "rb") as f:
            with dtqdm(
                file_size,
                "  Computing checksum",
                "B",
                unit_scale=True,
                unit_divisor=1024,
                bar_format="{desc}: {n_fmt}/{total_fmt} [{elapsed}, {rate_fmt}]",
                leave=False,
            ) as pbar:
                for chunk in iter(lambda: f.read(CHECKSUM_CHUNK_SIZE), b""):
                    sha256.update(chunk)
                    pbar.update(len(chunk))
    else:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(CHECKSUM_CHUNK_SIZE), b""):
                sha256.update(chunk)
    return sha256.hexdigest()


def calculate_checksum(data):
    """Calculate checksum digest."""
    # Only SHA-256 for now
    return sha256_digest(data)


def calculate_file_checksum(filepath, show=False):
    # Only SHA-256 for now
    return sha256_file_digest(filepath, show)


def verify_archives_from_toml(capture_dir):
    """Verify all archives using tarball TOML checksums."""
    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML

    if __dry_run__ and not toml_path.exists():
        printd("✓ All archives verified (dry-run)")
        return {}

    if not toml_path.exists():
        printd(
            f"Error: {DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load snapshot TOML
    with open(toml_path, "rb") as f:
        snapshot_config = tomllib.load(f)

    # Get tarball options
    tarball_opts = snapshot_config.get("tarball", {})
    compress_type = tarball_opts.get("compress", "gzip")
    digest_type = tarball_opts.get("checksum")

    # Verify compression type
    if compress_type not in COMPRESS_MAP:
        printd(f"Error: Unsupported compression type: {compress_type}", file=sys.stderr)
        sys.exit(1)

    archive_ext, _ = COMPRESS_MAP[compress_type]

    # If no checksum specified in options, skip verification
    if not digest_type:
        printd(
            f"Warning: No checksum type specified in {DEFAULT_SNAPSHOT_TOML}, skipping verification"
        )
        return snapshot_config

    printd("Verifying archive integrity...")

    if digest_type != "sha256":
        printd(f"Error: Unsupported digest type: {digest_type}", file=sys.stderr)
        sys.exit(1)

    # Get all tar sections
    tar_sections = snapshot_config.get("tar", {})

    if not tar_sections:
        printd(
            f"Error: No tar sections found in {DEFAULT_SNAPSHOT_TOML}", file=sys.stderr
        )
        sys.exit(1)

    # List archives to verify
    for category in sorted(tar_sections.keys()):
        archive_path = capture_dir / f"{category}{archive_ext}"
        printd(f"  verify: {archive_path.name}")

    if __dry_run__:
        printd("✓ All archives verified (dry-run)")
        return snapshot_config

    # Verify each archive
    all_valid = True
    with dtqdm(
        len(tar_sections),
        "  Verifying",
        " archives",
        bar_format="{desc}: {n}/{total} [{elapsed}, {rate_fmt}]",
        autorefresh=True,
    ) as pbar:
        for category, section_data in tar_sections.items():
            archive_path = capture_dir / f"{category}{archive_ext}"

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

            expected_checksum = "".join(checksum_chunks)

            # Calculate actual checksum
            actual_checksum = calculate_file_checksum(archive_path, show=False)

            if actual_checksum != expected_checksum:
                pbar.write(f"Error: Checksum mismatch for {archive_path.name}")
                all_valid = False

            pbar.update(1)

    printd()

    if not all_valid:
        printd(
            "Error: Checksum verification failed! Snapshot may be corrupted.",
            file=sys.stderr,
        )
        sys.exit(1)

    printd("✓ All archives verified")
    return snapshot_config


# --- Capture Logic --- #


def capture(args, root_dir, outdir, config):
    """Create archives in specified directory."""

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, config, "before", working_dir=outdir)

    # Get compression type and rollback extension from config
    tarball_opts = config.get("tarball", {})
    compress = tarball_opts.get("compress", "gzip")
    roll_ext = tarball_opts.get("rollback")

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

        # Store root and link for tarball TOML
        category_meta[category] = {"root": root, "link": data.get("link")}

    # Create archives in parallel
    create_archives(tasks, compress)

    # Generate snapshot TOML with checksum
    printd(f"Creating snapshot metadata...", "\n")
    toml_path, checksum = generate_snapshot_toml(
        outdir, compress, category_meta, roll_ext
    )

    printd(f"  Snapshot checksum: '{checksum[:7]}...'")

    # Create checksum-named subdirectory and move files
    printd("\n", f"Setting up final snapshot layout...", "\n")
    dst_dir = outdir.parent / outdir.name / checksum[:7]

    ext, _ = COMPRESS_MAP[compress]
    printd(f"  mkdir {dst_dir}")
    printd(f"  mv *{ext} -> {dst_dir.name}/")
    printd(f"  mv {DEFAULT_SNAPSHOT_TOML} -> {dst_dir.name}/")

    if not __dry_run__:
        dst_dir.mkdir(parents=True, exist_ok=False)

        # Move all tar archives to checksum directory
        for archive in outdir.glob(f"*{ext}"):
            archive.rename(dst_dir / archive.name)

        # Move snapshot TOML to checksum directory
        toml_path.rename(dst_dir / DEFAULT_SNAPSHOT_TOML)

    # Run after-capture scripts if requested
    if args.run_scripts:
        # Determine which config to use
        config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_CAPTURE
        scripts_config = load_config(root_dir, config_name)
        # Skip verification for scripts-only config (may not have [tar] section)
        run_scripts(root_dir, scripts_config, "after", working_dir=dst_dir)

    return dst_dir


def capture_deploy(capture_dir, dest_host, intended_path):
    """Copy capture to remote host."""
    printd("\n", f"Copying snapshot to {dest_host}...")

    # Ensure remote directory exists
    escaped_path = shlex.quote(str(intended_path))
    ssh_run(dest_host, f"mkdir -p {escaped_path}")

    # Copy all tar archives and snapshot TOML
    transfers = []

    # Add snapshot TOML
    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML
    if toml_path.exists():
        transfers.append(
            (str(toml_path), f"{dest_host}:{intended_path}/", DEFAULT_SNAPSHOT_TOML)
        )

        # Load compression type from snapshot TOML
        with open(toml_path, "rb") as f:
            snapshot_config = tomllib.load(f)
        compress_type = snapshot_config.get("tarball", {}).get("compress", "gzip")
        if compress_type not in COMPRESS_MAP:
            printd(
                f"Error: Unsupported compression type: {compress_type}", file=sys.stderr
            )
            sys.exit(1)
        ext, _ = COMPRESS_MAP[compress_type]

        # Add all tar archives with the specified compression type
        for archive in capture_dir.glob(f"*{ext}"):
            transfers.append(
                (str(archive), f"{dest_host}:{intended_path}/", archive.name)
            )
    else:
        printd(
            f"Error: {DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not transfers:
        printd("Error: No snapshot files found to copy", file=sys.stderr)
        sys.exit(1)

    success_count = rsync_parallel(transfers)
    if success_count < len(transfers):
        printd("Error: Failed to copy all files to remote", file=sys.stderr)
        sys.exit(1)

    printd(f"✓ Snapshot copied to {dest_host}:{intended_path}")


def write_capture_toml(capture_config):
    """Write capture_config to a temporary capture.toml file."""
    tmp = Path(tempfile.mktemp(suffix=".toml", prefix="capture-"))
    lines = []

    tarball = capture_config.get("tarball", {})
    if tarball:
        lines.append("[tarball]")
        for key, val in tarball.items():
            lines.append(
                f'{key} = "{val}"' if isinstance(val, str) else f"{key} = {val}"
            )
        lines.append("")

    for category, data in capture_config.get("tar", {}).items():
        lines.append(f"[tar.{category}]")
        for key, val in data.items():
            if isinstance(val, str):
                lines.append(f'{key} = "{val}"')
            elif isinstance(val, list):
                items = ", ".join(f'"{v}"' for v in val)
                lines.append(f"{key} = [{items}]")
        lines.append("")

    scripts = capture_config.get("scripts", {})
    if scripts:
        lines.append("[scripts]")
        for when, script_list in scripts.items():
            items = ", ".join(f'"{s}"' for s in script_list)
            lines.append(f"{when} = [{items}]")
        lines.append("")

    tmp.write_text("\n".join(lines))
    return tmp


def remote_capture(args, root_dir, source_host, capture_config=None):
    """Execute capture on a remote host and pull results back.

    If capture_config is None, transfers existing capture.toml from root_dir.
    If capture_config is provided (e.g. from migrate), writes a temp TOML.
    """

    # Set up remote working directory
    remote_work_dir = remote_mkdir(source_host, "capture")

    # Determine config to transfer
    root_host = getattr(args, "root_host", None)

    if capture_config:
        # Migrate path: write in-memory config to temp TOML
        tmp_capture_toml = write_capture_toml(capture_config)
        config_name = DEFAULT_CONFIG_CAPTURE
    else:
        # Standalone capture: load config for script discovery
        config_name = getattr(args, "config_toml", None) or DEFAULT_CONFIG_CAPTURE
        if root_host:
            capture_config = load_remote_config(root_host, root_dir, config_name)
        else:
            capture_config = load_config(root_dir, config_name)
        verify_capture_config(capture_config, config_name)
        tmp_capture_toml = None

    try:
        if tmp_capture_toml:
            config_src = str(tmp_capture_toml)
        elif root_host:
            config_src = f"{root_host}:{root_dir / config_name}"
        else:
            config_src = str(root_dir / config_name)

        transfers = [
            (str(__script__), f"{source_host}:{remote_work_dir}/", __script__.name),
            (
                config_src,
                f"{source_host}:{remote_work_dir}/{DEFAULT_CONFIG_CAPTURE}",
                DEFAULT_CONFIG_CAPTURE,
            ),
        ]

        # Transfer all capture scripts (before and after) to source
        scripts = capture_config.get("scripts", {})
        if args.run_scripts:
            for when in ("before", "after"):
                for run_script_path in scripts.get(when, []):
                    if root_host:
                        script_src = f"{root_host}:{root_dir / run_script_path}"
                    else:
                        script_path = root_dir / run_script_path
                        if not script_path.exists():
                            printd(
                                f"Warning: Script {script_path} not found, skipping",
                                file=sys.stderr,
                            )
                            continue
                        script_src = str(script_path)
                    script_name = Path(run_script_path).name
                    transfers.append(
                        (script_src, f"{source_host}:{remote_work_dir}/", script_name)
                    )

        success_count = rsync_parallel(transfers)
        if success_count < len(transfers):
            printd("Error: Failed to copy files to source host", file=sys.stderr)
            sys.exit(1)
    finally:
        if tmp_capture_toml and tmp_capture_toml.exists():
            tmp_capture_toml.unlink()

    # Run capture on source (snap capture handles before/after scripts)
    escaped_dir = shlex.quote(remote_work_dir)
    program = f"{sys.executable} {__script__.name}"
    script_flag = "--run-scripts" if args.run_scripts else ""
    capture_cmd = (
        f"cd {escaped_dir} && {program} capture" f" --to {escaped_dir} {script_flag}"
    )
    printd("\n", f"Running capture on {source_host}...")
    ssh_run(source_host, capture_cmd)

    # Pull capture to local temp directory
    if __dry_run__:
        tmpdir = Path("/tmp/dry-run/remote-capture")
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))

    # Find the capture directory on source (date/checksum structure)
    result = ssh_run(
        source_host,
        f"ls -d {escaped_dir}/*/",
    )
    if result is not None and result[0] != 0:
        printd("Error: Failed to find capture on source", file=sys.stderr)
        if tmpdir.exists():
            shutil.rmtree(tmpdir)
        sys.exit(1)

    # Pull capture files
    remote_capture_dir = result[1].strip() if result else f"{escaped_dir}/*/"
    host_path = f"{source_host}:{remote_capture_dir}"
    pull_result = rsync_run(host_path, str(tmpdir) + "/")
    if pull_result is not None and pull_result[0] != 0:
        printd("Error: Failed to pull capture from source", file=sys.stderr)
        if tmpdir.exists():
            shutil.rmtree(tmpdir)
        sys.exit(1)

    # Cleanup source working directory
    printd("\n", "Cleaning up source directory...")
    ssh_run(source_host, f"rm -rf {escaped_dir}")

    return tmpdir, tmpdir if not __dry_run__ else None


# --- Restore Logic --- #


def path_writable(path):
    """Check if a path, or its nearest existing parent when missing, is writable."""
    path = Path(path)
    while not os.path.lexists(path) and path.parent != path:
        path = path.parent
    return os.access(path, os.W_OK)


def tree_replaceable(path):
    """Check if the current user can back up and delete a path and everything below it."""
    path = Path(path)
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return False
    if path.is_symlink():
        return True
    if not path.is_dir():
        return os.access(path, os.R_OK)

    errors = []
    for root, _, files in os.walk(path, onerror=errors.append):
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            return False
        for name in files:
            file_path = os.path.join(root, name)
            if not os.path.islink(file_path) and not os.access(file_path, os.R_OK):
                return False
    return not errors


def archive_writable(archive, root, roll_ext):
    """Check if the current user can restore an archive without sudo."""
    root_path = expand_path(root) if root else Path("/")
    try:
        with tarfile.open(archive, "r:*") as tar:
            groups = archive_entries(tar, warn=False)
    except (tarfile.TarError, ValueError):
        return True  # The restore itself reports the error
    except OSError:
        return False  # Unreadable archive, which root can read

    if roll_ext:
        # Rotating and creating the backup dir happen in its parent
        if not path_writable(Path(str(root_path) + roll_ext).parent):
            return False
        for entry in groups:
            entry_path = root_path / entry
            if os.path.lexists(entry_path):
                if not tree_replaceable(entry_path):
                    return False
            elif not path_writable(entry_path.parent):
                return False
        return True

    # Extraction without rollback rewrites existing files in place and creates the rest
    for members in groups.values():
        for member in members:
            target = root_path / member.name
            if os.path.lexists(target) and not target.is_symlink():
                if target.is_file() and not os.access(target, os.W_OK):
                    return False
            elif not path_writable(target.parent):
                return False
    return True


def restore_needs_sudo(selected, root_map, roll_ext, snapshot_config):
    """Check if restoring the selected archives and links needs sudo."""
    for archive in selected:
        root = root_map.get(archive_category(archive))
        # Rollback restore skips categories without a root
        if roll_ext and not root:
            continue
        if not archive_writable(archive, root, roll_ext):
            return True

    # create_symlinks() writes a link for every category with one
    for section in snapshot_config.get("tar", {}).values():
        link, root = section.get("link"), section.get("root")
        if link and root:
            link_path = expand_path(link)
            if not os.path.lexists(link_path) and not path_writable(link_path.parent):
                return True
    return False


def sudo_restore(args, root_dir, capture_dir, restore_config, snapshot_config):
    """Run this restore again under sudo, then exit with its status.

    The child restores from the local capture_dir with the same restore config, so
    remote pulls and captures aren't repeated as root and the caller's temp dirs
    still get cleaned up. sudo resets the environment, so HOME and the variables
    used by roots and links are passed through, or '$HOME' and '~' would expand
    to root's home.
    """
    names = {"HOME"}
    for section in snapshot_config.get("tar", {}).values():
        for key in ("root", "link"):
            names.update(re.findall(r"\$\{?(\w+)", str(section.get(key, ""))))
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.setdefault("HOME", str(Path.home()))
    env_args = [f"{name}={value}" for name, value in sorted(env.items())]

    # Pass what restore() reads from the config through a temp file (it may come
    # from migrate.toml)
    archives = restore_config.get("tar", {}).get("archives") or []
    child_config = {"tar": {"archives": archives}}
    scripts = restore_config.get("scripts", {})
    child_scripts = {when: scripts[when] for when in ("before", "after") if when in scripts}
    if "scripts" in restore_config:
        child_config["scripts"] = child_scripts
    root_host = getattr(args, "root_host", None)
    root_arg = f"{root_host}:{root_dir}" if root_host else str(root_dir)

    fd, config_path = tempfile.mkstemp(prefix=f"{__script__.stem}-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(toml_dumps(child_config))

        cmd = ["sudo", "env", *env_args, sys.executable, str(__script__), "restore"]
        cmd += ["--from", str(capture_dir), "-r", root_arg, "-t", config_path]
        if getattr(args, "disable_rollback", False):
            cmd.append("--disable-rollback")
        if getattr(args, "run_scripts", False):
            cmd.append("--run-scripts")
        if __verbose__:
            cmd.append("--verbose")

        sys.stdout.flush()
        sys.stderr.flush()
        child = subprocess.Popen(cmd)
        while True:
            try:
                returncode = child.wait()
                break
            except KeyboardInterrupt:
                # The child got the same Ctrl-C and is rolling back; wait for it
                continue
    finally:
        os.unlink(config_path)

    # A child killed by a signal reports -N; exit like a shell would (128 + N)
    sys.exit(128 - returncode if returncode < 0 else returncode)


def restore(args, root_dir, capture_dir=None, restore_config=None):
    """Restore from local capture directory."""
    if capture_dir is None:
        capture_dir = args.capture_dir if __dry_run__ else args.capture_dir.resolve()

    verify_snapshot(capture_dir)

    # Verify all archives using snapshot TOML and load config
    snapshot_config = verify_archives_from_toml(capture_dir)
    printd()

    # Get compression type and rollback extension from snapshot TOML
    tarball_opts = snapshot_config.get("tarball", {})
    compress = tarball_opts.get("compress", "gzip")
    roll_ext = tarball_opts.get("rollback")

    # Build root_map from tar sections
    root_map = {}
    tar_sections = snapshot_config.get("tar", {})
    for category, section_data in tar_sections.items():
        root = section_data.get("root")
        if root:
            root_map[category] = root

    # Load restore config (use provided or load from file)
    if restore_config is None:
        config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_RESTORE
        restore_config = load_config(root_dir, config_name)
        verify_restore_config(restore_config, config_name)

    # Get archives from config
    # Semantics: None/[] = skip restore, ["*"] = restore all, [patterns...] = restore matching
    # Patterns support glob matching: ["ssh*", "dotfiles", "*-config"]
    archives = restore_config.get("tar", {}).get("archives", None)
    if not archives or archives == []:
        archives = None  # Empty list in config means don't restore
    elif archives == ["*"] or "*" in archives:
        archives = []  # Wildcard means restore all available

    # Find matching archive files
    selected = []
    if archives is not None:
        ext, _ = COMPRESS_MAP[compress]
        available = list(capture_dir.glob(f"*{ext}"))
        selected = archive_select(available, archives, capture_dir, compress)

    # Re-run with sudo if any restore target is not writable (Unix only)
    use_rollback = bool(roll_ext) and not getattr(args, "disable_rollback", False)
    can_sudo = hasattr(os, "geteuid") and os.geteuid() != 0
    if can_sudo and restore_needs_sudo(
        selected, root_map, roll_ext if use_rollback else None, snapshot_config
    ):
        if __dry_run__:
            printd("Note: A real run would re-run with sudo to restore to system locations.")
        else:
            printd(
                "Note: This script needs sudo privileges to restore files to system locations."
            )
            printd("Re-running with sudo...")
            sudo_restore(args, root_dir, capture_dir, restore_config, snapshot_config)

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "before", working_dir=capture_dir)

    # Restore archives
    if archives is not None:
        if not selected:
            printd("No archives selected for restore")
        elif getattr(args, "disable_rollback", False):
            # No rollback protection - use existing extract_archives with confirmation
            extract_archives(
                selected, skip_confirm=False, root_map=root_map, compress=compress
            )
        else:
            # Transactional restore with rollback support
            if not roll_ext:
                printd(
                    "Warning: No rollback extension specified in snapshot TOML, falling back to interactive confirmation",
                    file=sys.stderr,
                )
                extract_archives(
                    selected, skip_confirm=False, root_map=root_map, compress=compress
                )
            else:
                printd(f"Restoring {len(selected)} category archive(s)...")
                failed = []
                for archive in selected:
                    category = archive_category(archive)
                    root = root_map.get(category)
                    if not root:
                        printd(
                            f"Warning: No root for {category}, skipping",
                            file=sys.stderr,
                        )
                        continue
                    success = restore_category(archive, root, roll_ext, compress)
                    if not success:
                        failed.append(category)

                if failed:
                    printd(
                        "\n",
                        f"Error: {len(failed)} category(s) failed: {', '.join(failed)}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                else:
                    printd("\n", f"✓ Restored {len(selected)} category archive(s)")

    # Create symlinks from link -> root
    create_symlinks(snapshot_config)

    # Run after scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "after", working_dir=capture_dir)

    printd("\n", "✓ Restore completed successfully")


def remote_mkdir(dest_host, purpose="work"):
    """Generate a unique remote work directory path and create it."""
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(random.choices(chars, k=REMOTE_DIR_SUFFIX_LENGTH))
    remote_dir = f"/tmp/{__script__.stem}-{purpose}-{suffix}"

    # Create remote directory
    printd("Creating remote directory...")
    ssh_run(dest_host, f"mkdir -p {shlex.quote(remote_dir)}")
    return remote_dir


def remote_restore(args, root_dir, source_host):
    """Pull snapshot from remote and restore locally."""
    capture_dir_path = str(args.capture_dir)
    printd(f"Pulling snapshot from {source_host}:{capture_dir_path}...", "\n")

    if __dry_run__:
        tmpdir = Path("/tmp/dry-run/remote-restore")
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))

    try:
        host_path = f"{source_host}:{capture_dir_path}/"
        result = rsync_run(host_path, str(tmpdir) + "/")

        if result is not None and result[0] != 0:
            printd("Error: Failed to pull snapshot from remote", file=sys.stderr)
            sys.exit(1)

        restore(args, root_dir, capture_dir=tmpdir)

    finally:
        if not __dry_run__:
            shutil.rmtree(tmpdir)


def remote_deploy(
    args,
    root_dir,
    dest_host,
    source_host=None,
    deploy_config=None,
    before_scripts=True,
):
    """Deploy snapshot to remote and restore (from local or remote source)."""
    root_host = getattr(args, "root_host", None)

    # Determine source paths (local or remote)
    if source_host:
        # Remote source - use combined capture_dir (includes checksum)
        capture_dir_path = (
            str(args.capture_dir)
            if isinstance(args.capture_dir, Path)
            else args.capture_dir
        )
        printd(f"Deploying from {source_host} to {dest_host}...", "\n")
        # Config/scripts come from root_host if set, otherwise source_host
        if root_host:
            config_src_base = f"{root_host}:{root_dir}"
        else:
            config_src_base = f"{source_host}:{root_dir}"
        local_capture = None
    else:
        # Local source - use combined capture_dir (includes checksum)
        local_capture = args.capture_dir if __dry_run__ else args.capture_dir.resolve()
        printd(f"Deploying to {dest_host}...", "\n")

        if not __dry_run__:
            verify_snapshot(local_capture)
        # Config/scripts from root_host if set, otherwise local
        config_src_base = f"{root_host}:{root_dir}" if root_host else None

    if deploy_config is None:
        if args.config_toml:
            config_name = args.config_toml
        elif not root_host and (root_dir / DEFAULT_CONFIG_DEPLOY).exists():
            config_name = DEFAULT_CONFIG_DEPLOY
        else:
            config_name = DEFAULT_CONFIG_RESTORE
        if root_host:
            deploy_config = load_remote_config(root_host, root_dir, config_name)
        else:
            deploy_config = load_config(root_dir, config_name)
        verify_restore_config(deploy_config, config_name)
    else:
        config_name = None

    remote_dir = remote_mkdir(dest_host, "restore")

    # Collect all transfers to execute in parallel
    transfers = []

    # Add snap.py script
    transfers.append((str(__script__), f"{dest_host}:{remote_dir}/", __script__.name))

    # Add archive files (snapshot TOML and all tar archives)
    if source_host:
        # Remote source - need to fetch snapshot TOML first to determine archives
        printd(f"Fetching snapshot metadata from {source_host}...")

        # Create temp file for snapshot TOML
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".toml", delete=False
        ) as tmp:
            tmp_toml_path = Path(tmp.name)

        try:
            # Fetch snapshot TOML from remote source
            remote_toml = f"{source_host}:{capture_dir_path}/{DEFAULT_SNAPSHOT_TOML}"
            result = rsync_run(remote_toml, str(tmp_toml_path))

            if result is None:  # __dry_run__
                printd(f"Fetch {remote_toml}")
                printd(f"Enumerate archives from remote snapshot TOML")
                compress_type = "gzip"
                # In dry-run, show placeholder archives
                tar_sections = {"archive1": {}, "archive2": {}, "archive-N": {}}
            elif result[0] != 0:
                printd(
                    f"Error: Failed to fetch {DEFAULT_SNAPSHOT_TOML} from {source_host}",
                    file=sys.stderr,
                )
                sys.exit(1)
            else:
                # Load snapshot TOML to get compression type and archive list
                with open(tmp_toml_path, "rb") as f:
                    snapshot_config = tomllib.load(f)
                compress_type = snapshot_config.get("tarball", {}).get(
                    "compress", "gzip"
                )
                tar_sections = snapshot_config.get("tar", {})

                if compress_type not in COMPRESS_MAP:
                    printd(
                        f"Error: Unsupported compression type: {compress_type}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
        finally:
            # Clean up temp file
            if tmp_toml_path.exists():
                tmp_toml_path.unlink()

        # Add snapshot TOML transfer
        transfers.append(
            (
                f"{source_host}:{capture_dir_path}/{DEFAULT_SNAPSHOT_TOML}",
                f"{dest_host}:{remote_dir}/",
                DEFAULT_SNAPSHOT_TOML,
            )
        )

        # Add all tar archives based on tar sections
        ext, _ = COMPRESS_MAP[compress_type]
        for category in tar_sections.keys():
            archive_name = f"{category}{ext}"
            transfers.append(
                (
                    f"{source_host}:{capture_dir_path}/{archive_name}",
                    f"{dest_host}:{remote_dir}/",
                    archive_name,
                )
            )
    else:
        # Local source - enumerate files directly
        toml_path = local_capture / DEFAULT_SNAPSHOT_TOML

        if __dry_run__ and not toml_path.exists():
            # Dry-run with placeholder capture dir (e.g. from remote_capture)
            printd(f"Enumerate archives from {local_capture}")
            transfers.append(
                (str(toml_path), f"{dest_host}:{remote_dir}/", DEFAULT_SNAPSHOT_TOML)
            )
        else:
            if not toml_path.exists():
                printd(
                    f"Error: {DEFAULT_SNAPSHOT_TOML} not found in {local_capture}",
                    file=sys.stderr,
                )
                sys.exit(1)

            transfers.append(
                (str(toml_path), f"{dest_host}:{remote_dir}/", DEFAULT_SNAPSHOT_TOML)
            )

            # Load compression type from snapshot TOML
            with open(toml_path, "rb") as f:
                snapshot_config = tomllib.load(f)
            compress_type = snapshot_config.get("tarball", {}).get("compress", "gzip")
            if compress_type not in COMPRESS_MAP:
                printd(
                    f"Error: Unsupported compression type: {compress_type}",
                    file=sys.stderr,
                )
                sys.exit(1)
            ext, _ = COMPRESS_MAP[compress_type]

            # Add all tar archives with the specified compression type
            for archive in local_capture.glob(f"*{ext}"):
                transfers.append(
                    (str(archive), f"{dest_host}:{remote_dir}/", archive.name)
                )

    # Add config transfer (remote restore needs a restore config)
    tmp_config = None
    if config_name:
        if source_host or root_host:
            config_src = f"{config_src_base}/{config_name}"
        else:
            config_src = str(root_dir / config_name)
        transfers.append(
            (
                config_src,
                f"{dest_host}:{remote_dir}/{DEFAULT_CONFIG_RESTORE}",
                config_name,
            )
        )
    else:
        # Generate minimal restore config for remote (restore all archives)
        tmp_config = Path(tempfile.mktemp(suffix=".toml", prefix="restore-"))
        tmp_config.write_text('[tar]\narchives = ["*"]\n')
        transfers.append(
            (
                str(tmp_config),
                f"{dest_host}:{remote_dir}/{DEFAULT_CONFIG_RESTORE}",
                "restore.toml",
            )
        )

    # Add script transfers
    script_whens = ["before", "after"] if before_scripts else ["after"]
    if deploy_config and "scripts" in deploy_config:
        for when in script_whens:
            if when in deploy_config["scripts"]:
                for run_script_path in deploy_config["scripts"][when]:
                    if source_host or root_host:
                        script_src = f"{config_src_base}/{run_script_path}"
                        script_name = Path(run_script_path).name
                        transfers.append(
                            (script_src, f"{dest_host}:{remote_dir}/", script_name)
                        )
                    else:
                        script_path = root_dir / run_script_path
                        if not script_path.exists():
                            printd(
                                f"Warning: Script {script_path} not found, skipping",
                                file=sys.stderr,
                            )
                            continue
                        script_name = Path(run_script_path).name
                        transfers.append(
                            (
                                str(script_path),
                                f"{dest_host}:{remote_dir}/",
                                script_name,
                            )
                        )

    # Execute all transfers in parallel
    success_count = rsync_parallel(transfers)

    # Clean up temp restore config if generated
    if tmp_config and tmp_config.exists():
        tmp_config.unlink()

    if success_count < len(transfers):
        printd("Error: Failed to copy all files to remote", file=sys.stderr)
        sys.exit(1)

    if args.run_scripts and before_scripts:
        run_scripts(
            root_dir,
            deploy_config,
            "before",
            working_dir=None,
            remote_host=dest_host,
            remote_dir=remote_dir,
        )

    # Execute restore on remote
    escaped_remote_dir = shlex.quote(remote_dir)
    program = f"{sys.executable} {__script__.name}"
    remote_cmd = (
        f"cd {escaped_remote_dir} && sudo {program} restore"
        f" --from {escaped_remote_dir}"
    )
    printd("\n", f"Executing restore on {dest_host}...")
    printd(f"Command: {remote_cmd}", "\n")
    ssh_run(dest_host, remote_cmd, tty=True)

    if args.run_scripts:
        run_scripts(
            root_dir,
            deploy_config,
            "after",
            working_dir=None,
            remote_host=dest_host,
            remote_dir=remote_dir,
        )

    printd("\n", "Cleaning up remote directory...")
    ssh_run(dest_host, f"rm -rf {shlex.quote(remote_dir)}")

    printd("\n", f"✓ Deploy completed successfully")


# --- Migrate Logic --- #


def split_migrate_config(config, config_name):
    """Split migrate config into separate capture and restore configs.

    Migrate config has [capture.*] and [restore.*] namespaces with shared
    [tarball] options. Returns (capture_config, restore_config) dicts
    compatible with existing capture() and restore() functions.
    """
    tarball = config.get("tarball", {})
    capture_section = config.get("capture", {})
    restore_section = config.get("restore", {})

    if "tar" not in capture_section:
        printd(
            f"Error: {config_name} missing required [capture.tar] section",
            file=sys.stderr,
        )
        sys.exit(1)

    capture_config = {"tarball": tarball, "tar": capture_section.get("tar", {})}
    if "scripts" in capture_section:
        capture_config["scripts"] = capture_section["scripts"]

    restore_config = {"tarball": tarball}
    if "tar" in restore_section:
        restore_config["tar"] = restore_section["tar"]
    if "scripts" in restore_section:
        restore_config["scripts"] = restore_section["scripts"]

    return capture_config, restore_config


# --- Command Execution --- #


def require_remote(args, attr, host):
    """Exit if --from/--to was given but does not name a remote host."""
    value = getattr(args, attr, None)
    if value is not None and not host:
        flag = "--from" if attr == "src" else "--to"
        printd(
            f"Error: {flag} '{value}' is not a remote host; use user@host or host:",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_check(args):
    """Calculate and display checksums for files."""
    files = getattr(args, "files", []) or []

    if not files:
        print("Error: Specify at least one file", file=sys.stderr)
        sys.exit(1)

    ignore_invalid = getattr(args, "ignore_invalid", False)
    short_hash = getattr(args, "short_hash", False)
    full_path = getattr(args, "full_path", False)
    no_path = getattr(args, "no_path", False)

    # Calculate and display checksums
    for file_path in files:
        path = Path(file_path)
        if not (path.exists() or path.is_file()):
            if ignore_invalid:
                continue
            checksum = "null"
        else:
            checksum = calculate_file_checksum(path, show=False)
        if short_hash:
            checksum = checksum[:7]
        if no_path:
            print(checksum)
        elif full_path:
            print(f"{path}: {checksum}")
        else:
            print(f"{path.name}: {checksum}")


def cmd_capture(args):
    """Execute capture command (local or remote source, local or remote destination)."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, _ = parse_remote_arg(getattr(args, "src", None))
    to_host, to_path = parse_remote_arg(getattr(args, "dst", None))
    require_remote(args, "src", from_host)

    # Determine intended destination path
    if to_path:
        intended_path = to_path if to_host else to_path.resolve()
    else:
        now = datetime.now()
        year = now.strftime("%Y")
        month_day = now.strftime("%m-%d")
        intended_path = root_dir / "captures" / year / month_day

    printd(f"Snapshot destination: {intended_path}")

    if from_host:
        # Run capture on remote host, pull results back
        dst_dir, tmpdir = remote_capture(args, root_dir, from_host)
    else:
        # Local capture
        if root_host:
            config = load_remote_config(root_host, root_dir, DEFAULT_CONFIG_CAPTURE)
        else:
            config = load_config(root_dir, DEFAULT_CONFIG_CAPTURE)
        verify_capture_config(config, DEFAULT_CONFIG_CAPTURE)

        tmpdir = None
        if __dry_run__:
            outdir = Path("/tmp/dry-run") / intended_path.name
        else:
            tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))
            outdir = tmpdir / intended_path.name
            outdir.mkdir(parents=True, exist_ok=False)

        dst_dir = capture(args, root_dir, outdir, config)

    try:
        if __dry_run__:
            final_dst = intended_path / dst_dir.name
        elif to_host:
            # Copy snapshot to remote destination
            remote_path = intended_path / dst_dir.name
            capture_deploy(dst_dir, to_host, remote_path)
            final_dst = remote_path
        else:
            # Move snapshot to local destination
            final_dst = intended_path / dst_dir.name
            final_dst.parent.mkdir(parents=True, exist_ok=True)

            if final_dst.exists():
                printd(
                    f"Error: Snapshot directory {final_dst} already exists",
                    file=sys.stderr,
                )
                sys.exit(1)

            shutil.move(str(dst_dir), str(final_dst))
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)

    printd("\n", f"✓ Snapshot saved to {final_dst}")


def cmd_migrate(args):
    """Execute migrate command: capture on source, restore on destination."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, _ = parse_remote_arg(getattr(args, "src", None))
    to_host, _ = parse_remote_arg(getattr(args, "dst", None))
    require_remote(args, "src", from_host)
    require_remote(args, "dst", to_host)

    if not from_host and not to_host:
        printd(
            "Error: At least one of --from or --to must specify a remote host",
            file=sys.stderr,
        )
        sys.exit(1)

    config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_MIGRATE
    if root_host:
        config = load_remote_config(root_host, root_dir, config_name)
    else:
        config = load_config(root_dir, config_name)
    capture_config, restore_config = split_migrate_config(config, config_name)
    verify_capture_config(capture_config, config_name)

    # Phase 1: Capture on source
    if from_host:
        printd(f"Migrating from {from_host} to {to_host or 'local'}...", "\n")
        capture_dir, tmpdir = remote_capture(args, root_dir, from_host, capture_config)
    else:
        printd(f"Migrating from local to {to_host}...", "\n")
        tmpdir = None
        dir_name = datetime.now().strftime("%m-%d")
        if __dry_run__:
            outdir = Path("/tmp/dry-run") / dir_name
        else:
            tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))
            outdir = tmpdir / dir_name
            outdir.mkdir(parents=True, exist_ok=False)
        capture_dir = capture(args, root_dir, outdir, capture_config)

    try:
        # Phase 2: Restore on destination
        args.capture_dir = capture_dir

        if to_host:
            remote_deploy(
                args,
                root_dir,
                to_host,
                deploy_config=restore_config,
                before_scripts=True,
            )
        else:
            restore(
                args,
                root_dir,
                capture_dir=capture_dir,
                restore_config=restore_config,
            )

        printd("\n", f"✓ Migration completed successfully")
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)


def cmd_restore(args):
    """Execute restore command (local or remote)."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, from_path = parse_remote_arg(getattr(args, "src", None))
    to_host, _ = parse_remote_arg(getattr(args, "dst", None))
    require_remote(args, "dst", to_host)

    # Determine snapshot source path
    if from_host:
        # Remote source: find latest on remote or use provided path
        captures_dir = root_dir / "captures"
        if from_path:
            capture_path = from_path
        else:
            capture_path = resolve_remote_snapshot(from_host, captures_dir)
    elif from_path:
        capture_path = from_path.resolve()
    else:
        # Local default: find latest capture
        captures_dir = root_dir / "captures"
        capture_path = resolve_snapshot_root(captures_dir)

    # For local paths, auto-select latest checksum subdir if path is a date dir
    if not from_host and capture_path.exists() and capture_path.is_dir():
        toml_check = capture_path / DEFAULT_SNAPSHOT_TOML
        if not toml_check.exists():
            subdirs = [d for d in capture_path.iterdir() if d.is_dir()]
            if not subdirs:
                printd(f"Error: No snapshot found in {capture_path}", file=sys.stderr)
                sys.exit(1)

            latest = max(subdirs, key=lambda d: d.stat().st_mtime)
            capture_path = latest
            printd(f"Auto-selected latest snapshot: {latest.name}")

    # Store capture path in args for subfunctions
    args.capture_dir = capture_path

    # Case 1: Remote to remote (source -> dest)
    if from_host and to_host:
        remote_deploy(args, root_dir, to_host, source_host=from_host)
    # Case 2: Local to remote (deploy)
    elif to_host:
        remote_deploy(args, root_dir, to_host)
    # Case 3: Remote to local (pull and restore)
    elif from_host:
        remote_restore(args, root_dir, from_host)
    # Case 4: Local restore
    else:
        restore(args, root_dir)


# --- Argument Parsing --- #


def add_cli_options(parser):
    """Add common CLI options (dry-run, verbose, help)."""
    group = parser.add_argument_group("cli options")
    group.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without doing it",
    )
    group.add_argument(
        "--verbose", action="store_true",
        help="Show detailed progress information",
    )
    group.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )


def add_snap_options(parser):
    """Add shared snap configuration options."""
    group = parser.add_argument_group("snap options")
    group.add_argument(
        "-r", "--snap-root", dest="root", metavar="[[user@]host:]root",
        default=DEFAULT_ROOT_SNAP,
        help=f"Root directory for configs and scripts (default: {DEFAULT_ROOT_SNAP})",
    )
    group.add_argument(
        "-t", "--config-toml", dest="config_toml", metavar="config",
        help="Use custom TOML config file",
    )
    group.add_argument(
        "--run-scripts", action="store_true",
        help="Run before/after scripts, if defined",
    )


def add_host_options(parser, from_help, to_help):
    """Add --from and --to host options."""
    group = parser.add_argument_group(
        "host options",
        "A local path containing '@' needs a '/' (e.g. ./name@tag), "
        "or it is read as user@host.",
    )
    group.add_argument(
        "--from", dest="src", metavar="[user@]host[:path]",
        help=from_help,
    )
    group.add_argument(
        "--to", dest="dst", metavar="[user@]host[:path]",
        help=to_help,
    )


def setup_parser():
    """Build the argument parser with all subcommands."""
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        add_help=False,
        formatter_class=formatter,
        description="Snapshot and restore utility",
    )

    subparsers = parser.add_subparsers(
        dest="command", title="commands", metavar="",
    )

    # check
    check = subparsers.add_parser(
        "check", formatter_class=formatter, add_help=False,
        help="Calculate and display file checksums",
    )
    check.add_argument(
        "files", nargs="*", metavar="file",
        help="File(s) to checksum",
    )
    chk = check.add_argument_group("checksum options")
    chk.add_argument(
        "--ignore-invalid", action="store_true",
        help="Ignore and skip any invalid or missing files",
    )
    chk.add_argument(
        "--short-hash", action="store_true",
        help="Generate a shorter 7 character hash",
    )
    chk_cli = check.add_argument_group("cli options")
    chk_cli.add_argument(
        "--full-path", action="store_true",
        help="Show full file path instead of just name",
    )
    chk_cli.add_argument(
        "--no-path", action="store_true",
        help="Show only the checksum (no file name)",
    )
    chk_cli.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    # capture
    cap = subparsers.add_parser(
        "capture", formatter_class=formatter, add_help=False,
        help="Create a snapshot (can send to remote)",
    )
    add_snap_options(cap)
    add_host_options(
        cap,
        from_help="Source host to capture from (default: local)",
        to_help="Destination for snapshot (default: <root>/captures/YYYY/MM-DD)",
    )
    add_cli_options(cap)

    # restore
    rst = subparsers.add_parser(
        "restore", formatter_class=formatter, add_help=False,
        help="Restore from a snapshot (local or remote)",
    )
    add_snap_options(rst)
    add_host_options(
        rst,
        from_help="Snapshot source path (default: latest in <root>/captures)",
        to_help="Destination host to restore to (default: local)",
    )
    rst_group = rst.add_argument_group("restore options")
    rst_group.add_argument(
        "--disable-rollback", action="store_true",
        help="Disable backup/rollback of existing files",
    )
    add_cli_options(rst)

    # migrate
    mig = subparsers.add_parser(
        "migrate", formatter_class=formatter, add_help=False,
        help="Capture on source, restore on destination",
    )
    add_snap_options(mig)
    add_host_options(
        mig,
        from_help="Source host to capture from (default: local)",
        to_help="Destination host to restore to (default: local)",
    )
    mig_group = mig.add_argument_group("migrate options")
    mig_group.add_argument(
        "--disable-rollback", action="store_true",
        help="Disable backup/rollback of existing files",
    )
    add_cli_options(mig)

    # Top-level help only
    parser.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    return parser


def main():
    parser = setup_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check":
        cmd_check(args)
        return

    global __dry_run__, __verbose__
    __dry_run__ = getattr(args, "dry_run", False)
    __verbose__ = getattr(args, "verbose", False)

    # Parse --snap-root for remote host support
    root_host, root_path = parse_remote_arg(args.root)
    args.root_host = root_host
    args.root = resolve_root(root_path if root_host else args.root, root_host=root_host)

    commands = {
        "capture": (cmd_capture, "Starting capture!"),
        "restore": (cmd_restore, "Starting restoration!"),
        "migrate": (cmd_migrate, "Starting migration!"),
    }

    cmd_func, message = commands[args.command]
    if __dry_run__:
        message += " (No changes will be made)"

    printd(message, "\n")
    cmd_func(args)


if __name__ == "__main__":
    main()
