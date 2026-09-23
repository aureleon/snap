#!/usr/bin/env python3
"""
Snapshot and restore utility.
"""

__version__ = "0.1"

import sys

# Require Python 3.11+ for tomllib
if sys.version_info < (3, 11):
    print(
        f"Error: snap.py requires Python 3.11 or later (found {sys.version.split()[0]})",
        file=sys.stderr,
    )
    sys.exit(1)

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import platform
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import tomllib

import concurrent.futures as ccft

from datetime import datetime
from pathlib import Path, PurePosixPath

# tqdm is optional: without it, dtqdm() hands out no-op bars, so the sudo re-run and
# remote children also run where tqdm isn't installed
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

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
    nargs = [("[DRY-RUN]" if arg in space else f"[DRY-RUN] {arg}") for arg in args]
    if "sep" not in kwargs:
        kwargs["sep"] = "\n"
    print(*nargs, **kwargs)


# --- Output Helpers --- #

INDENT = "  "
BAR_FORMAT = "  {desc}: {n}/{total} [{elapsed}]"

# Dry-run stand-ins for paths and values a real run would create
DRY_RUN_DIR = "/tmp/dry-run"
DRY_RUN_WORK_DIR = "/tmp/snap-work.dry-run"  # a work directory on a host
DRY_RUN_CHECKSUM = "abcd123"

# Display names for the dry-run stand-ins, most specific first
DRY_RUN_NAMES = [
    (DRY_RUN_WORK_DIR, "<work dir>"),
    (f"{DRY_RUN_DIR}/remote-capture", "<pulled snapshot>"),
    (f"{DRY_RUN_DIR}/remote-restore", "<pulled snapshot>"),
    (DRY_RUN_DIR, "<staging dir>"),
    ("YYYY/MM-DD/latest", "<latest>"),
    (DRY_RUN_CHECKSUM, "<checksum>"),
]

COMPRESS_HELP = "supported: gzip, bzip2, xz, '' (no compression)"

# Non-fatal Error lines printed so far; finish() reports them
_reported = {"errors": 0}

# emit()'s default stream, sys.stdout at call time; an explicit None (a closed fd) drops text
_STDOUT = object()

# Keeps each message block whole when worker threads print. tqdm's own lock exists only
# after the first bar, and making it earlier would start a multiprocessing helper process
_output_lock = threading.RLock()

# Set, under _output_lock, by the first rsync_run copy that fails; when parallel copies
# fail together, only that one prints its output and Error block
_copy_failed = threading.Event()


def flush_stream(stream, strict=False):
    """Flush a stream before other output goes to the same terminal or pipe.

    Most flushes only keep stdout and stderr in order, so a reader that went away is
    ignored there. A strict flush is one print() and tqdm already made (a bar starting,
    the sudo re-run), so a broken stream still stops the run at that point.
    """
    if stream is None:
        return  # The fd was closed at startup; there is nothing to flush
    if strict:
        stream.flush()
        return
    try:
        stream.flush()
    except (OSError, ValueError):
        pass  # The next strict flush, a full buffer or the exit reports the broken pipe


def shares_stdout(file):
    """Check if text for file could show up ahead of buffered stdout text.

    That happens when stdout is a terminal, or when both streams go to the same file or
    pipe. When the fds can't be checked, assume they share, so the order is kept.
    """
    stdout = sys.stdout
    if stdout is None:
        return False  # The fd was closed at startup; nothing is buffered
    try:
        if stdout.isatty():
            return True
        return os.path.samestat(os.fstat(stdout.fileno()), os.fstat(file.fileno()))
    except (AttributeError, OSError, ValueError):
        return True  # No usable fd (a StringIO, a closed fd); the flush is harmless


def emit(text="", level=0, file=_STDOUT):
    """Print text through printd, one indented line at a time, without breaking live bars.

    A closed stream (None) drops the text, as print() does. Write and encoding errors
    stop the run, as they did from print(). Terminals and stderr are line-buffered; a
    piped stdout stays block-buffered. It is flushed before stderr lines only when the
    order shows (a terminal, or one file or pipe for both), and before bars, prompts
    and child processes.
    """
    if file is _STDOUT:
        file = sys.stdout  # looked up per call, so redirects and pytest capsys work
    if file is None:
        return  # The fd was closed at startup; print() drops the text too
    if file is not sys.stdout and shares_stdout(file):
        flush_stream(sys.stdout)  # keep the order when stdout and stderr share a pipe

    lines = [line.rstrip() for line in str(text).split("\n")]

    # tqdm has its lock only after the first bar; before that there is no bar to clear
    if tqdm is not None and hasattr(tqdm, "_lock"):
        bar_safe = tqdm.external_write_mode(file=file)
    else:
        bar_safe = contextlib.nullcontext()

    with _output_lock, bar_safe:
        for line in lines:
            if line:
                printd(INDENT * level + line, file=file)
            else:
                printd(file=file)


def plural(count, word, many=None):
    """Format a count with its noun: '1 archive', '2 archives', '3 entries'."""
    if count != 1:
        word = many or word + "s"
    return f"{count} {word}"


def labeled(label, value):
    """Format 'label value' for a detail line, aligning further lines of value under it."""
    first, *rest = str(value).split("\n")
    pad = " " * (len(label) + 1)
    return "\n".join([f"{label} {first}", *(pad + line for line in rest)])


def shown(value):
    """Return a value for display, naming dry-run stand-ins instead of showing them."""
    text = str(value)
    if __dry_run__:
        for placeholder, name in DRY_RUN_NAMES:
            text = text.replace(placeholder, name)
    return text


def is_placeholder(path):
    """Check if a path is a dry-run stand-in for something a real run would create."""
    return __dry_run__ and str(path).startswith(DRY_RUN_DIR)


def here():
    """Name this machine for From/To lines."""
    return f"{platform.node() or 'localhost'} (this machine)"


def banner(noun):
    """Print a run's first line: 'Starting <noun>[ as root][ (no changes will be made)]'."""
    title = f"Starting {noun}"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        title += " as root"
    if __dry_run__:
        title += " (no changes will be made)"
    emit(title)


def context(source, dest):
    """Print the From/To lines under the banner."""
    emit(f"From: {shown(source)}", 1)
    emit(f"To:   {shown(dest)}", 1)


def step(title):
    """Start a step: one blank line, then '<title>...' at column 0."""
    emit()
    emit(f"{title}...")


def step_skipped(what, reason):
    """Print a skipped step: one blank line, then 'Skipping <what> (<reason>)'."""
    emit()
    emit(f"Skipping {what} ({reason})")


def note(msg):
    """Print a 'Note:' line on stdout after one blank line."""
    emit()
    emit(f"Note: {msg}")


def say(msg, level=1):
    """Print an item (level 1) or a detail (level 2) on stdout."""
    emit(msg, level)


def echo(argv, level=1, verbose=False, runs=False):
    """Show a command as 'run: <shell line>'.

    A dry run shows every command it skips. runs=True marks a read-only command that runs
    in a dry run too; it shows only as in a real run. verbose=True shows it with --verbose.
    """
    skipped = __dry_run__ and not runs
    if skipped or (verbose and __verbose__):
        emit("run: " + shlex.join(shown(arg) for arg in argv), level)


def ok(msg, level=1, dry=None):
    """Print '✓ msg' in a real run; in a dry run print `dry` without the mark, or nothing."""
    if not __dry_run__:
        emit(f"✓ {msg}", level)
    elif dry:
        emit(dry, level)


def message_block(first, details):
    """Join a message line and its details, each detail line indented one level."""
    lines = [first]
    for detail in details:
        lines += [INDENT + line for line in str(detail).split("\n")]
    return "\n".join(lines)


def warn(msg, *details):
    """Print 'Warning: msg' and indented details on stderr; the run continues.

    The block is one emit(), so a live bar is cleared once for it.
    """
    emit(message_block(f"Warning: {msg}", details), file=sys.stderr)


def error(msg, *details):
    """Print 'Error: msg' and indented details on stderr, and count it for finish().

    The block is one emit(), so a live bar is cleared once for it.
    """
    with _output_lock:
        _reported["errors"] += 1
        emit(message_block(f"Error: {msg}", details), file=sys.stderr)


def fatal(msg, *details):
    """Print an error and exit with status 1."""
    error(msg, *details)
    sys.exit(1)


def dry_run_stop(msg, *details):
    """End a dry run where a real run would stop: the Error, then the dry run's final line.

    Like every dry run it exits 0; the final line counts the error.
    """
    error(msg, *details)
    finish("")
    sys.exit(0)


def compress_error(value, source, stop=True):
    """Report an unknown [tarball] compress value and where it came from."""
    report = fatal if stop else error
    report(f"Unsupported compression '{value}' in {source}", COMPRESS_HELP)


def relay(text, level=2, file=_STDOUT):
    """Print captured child output after it ends, indented; CR redraws keep their last state."""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")  # TimeoutExpired output can be bytes
    text = (text or "").rstrip()
    if not text:
        return

    lines = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r").rsplit("\r", 1)[-1]
        if line.strip() or "\r" not in raw:
            lines.append(line)

    # Leading blank lines would separate the output from the line that introduced it
    while lines and not lines[0].strip():
        lines.pop(0)
    emit("\n".join(lines), level, file)


def ask(question, level=2):
    """Ask a yes/no question; only 'y' or 'yes' (any case) is yes, EOF and Ctrl-C are no."""
    prompt = f"{question} [y/N]: "
    if __dry_run__:
        emit(f"{prompt}y (assumed in a dry run)", level)
        return True

    # A terminal echoes an answer typed before the prompt appears, so input() won't
    # show it after the prompt
    typed_ahead = False
    try:
        if sys.stdin.isatty():
            typed_ahead = bool(select.select([sys.stdin], [], [], 0)[0])
    except (AttributeError, OSError, ValueError):
        pass  # No usable stdin; input() below reports it

    flush_stream(sys.stdout)
    flush_stream(sys.stderr)
    try:
        answer = input(INDENT * level + prompt)
    except (EOFError, KeyboardInterrupt):
        emit()  # end the prompt line
        emit("skip: no answer", level)
        return False
    except RuntimeError as e:
        # input() lost stdin, stdout or stderr ('input(): lost sys.stdout'); this exits 1
        # where the traceback did
        stream = "stdin"
        for name in ("stdout", "stderr"):
            if name in str(e):
                stream = name
        fatal(f"Cannot ask '{question}': {stream} is closed")

    # Only a terminal on both ends shows the typed answer after the prompt; otherwise
    # (piped input, output to a pipe or log, or an answer typed ahead) echo it so the
    # line ends and logs show it
    if typed_ahead or not (sys.stdin.isatty() and sys.stdout.isatty()):
        emit(answer.strip())
    if answer.strip().lower() in ("y", "yes"):
        return True
    emit("skip: declined", level)
    return False


def finish(msg, success=True):
    """Print the command's one final line after a blank line."""
    emit()
    errors = _reported["errors"]
    if __dry_run__:
        extra = f" with {plural(errors, 'error')}" if errors else ""
        emit(f"Dry run completed{extra}; no changes were made")
    elif errors:
        emit(f"{msg} ({plural(errors, 'error')} reported above)")
    elif success:
        emit(f"✓ {msg}")
    else:
        emit(msg)


# --- Subprocess Runners --- #

SSH_ALIVE_INTERVAL = 10
SSH_CONNECT_TIMEOUT = 30

# Wall-clock limit for short ssh commands (mktemp, mkdir, ls, rm). The remote capture and
# restore, scripts and rsync take as long as their data does, so they get none
COMMAND_TIMEOUT = 300

# rsync's own --timeout: a copy stops after this many seconds with no data moving
RSYNC_TIMEOUT = 300


def ssh_argv(host, command, tty=False):
    """Build the ssh argv that runs one shell command line on a host."""
    argv = [
        "ssh",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o",
        f"ServerAliveInterval={SSH_ALIVE_INTERVAL}",
    ]
    if tty:
        argv.append("-t")
    return argv + [host, command]


def ssh_run(
    host,
    *commands,
    check=True,
    stdin_data=None,
    tty=False,
    quiet=False,
    desc="Remote command",
    level=1,
    timeout=COMMAND_TIMEOUT,
    read_only=False,
):
    """Execute commands on remote host with timeout and proper error handling.

    Returns (returncode, stdout, stderr). desc names the action in error lines; level
    indents the run: echo and relayed output. timeout=None sets no wall-clock limit, for
    commands that take as long as their data (ConnectTimeout and ServerAliveInterval
    still apply). A dry run only shows the command and returns (0, '', ''), unless
    read_only marks a query that changes nothing, which runs in a dry run too.
    """
    ssh_args = ssh_argv(host, " && ".join(commands), tty)

    if not quiet:
        echo(ssh_args, level, verbose=True, runs=read_only)
    if __dry_run__ and not read_only:
        return (0, "", "")

    try:
        # For TTY operations, don't capture output (interactive)
        if tty:
            # The child writes to the terminal directly; print everything before it first
            flush_stream(sys.stdout)
            flush_stream(sys.stderr)
            result = subprocess.run(
                ssh_args,
                check=check,
                timeout=timeout,
                input=stdin_data,
                text=stdin_data is not None,
            )
            return (result.returncode, "", "")

        # For non-TTY, capture output
        result = subprocess.run(
            ssh_args,
            check=check,
            timeout=timeout,
            input=stdin_data,
            text=True,
            capture_output=True,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired as e:
        # Print what the command wrote before it hung, then the error
        relay(e.stdout, level)
        relay(e.stderr, level, sys.stderr)
        fatal(f"{desc} timed out on {host} after {timeout}s")
    except subprocess.CalledProcessError as e:
        # Print captured output on error before exiting
        relay(e.stdout, level)
        relay(e.stderr, level, sys.stderr)
        if e.returncode == 255:
            fatal(f"{desc} failed on {host}: ssh error (exit status 255)")
        fatal(f"{desc} failed on {host} (exit status {e.returncode})")


def rsync_run(source, dest, check=True, desc=None, options=(), read_only=False):
    """Execute rsync with its inactivity timeout.

    Returns tuple: (returncode, stdout, stderr), or None in a dry run. desc names the copy
    in error lines; options go before the paths. There is no wall-clock limit, so a large
    archive takes as long as it needs: rsync's --timeout stops a copy that moves no data
    for RSYNC_TIMEOUT seconds. read_only marks a copy that only reads a host into a local
    temp dir, which runs in a dry run too.
    """
    if desc is None:
        desc = Path(source).name

    # No --info=progress2 with --verbose: macOS openrsync rejects it, and rsync's stdout
    # is never shown
    rsync_cmd = ["rsync", "-az", f"--timeout={RSYNC_TIMEOUT}", *options, source, dest]

    if __dry_run__ and not read_only:
        echo(rsync_cmd)
        return None

    try:
        result = subprocess.run(
            rsync_cmd,
            check=check,
            capture_output=True,
            text=True,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.CalledProcessError as e:
        # Print rsync's errors before exiting (without -v its stdout is empty). One block,
        # since this can run in an rsync_parallel worker; only the first failed copy
        # prints it, and the others exit quietly
        with _output_lock:
            if _copy_failed.is_set():
                sys.exit(1)
            _copy_failed.set()
            relay(e.stderr, 1, sys.stderr)
            fatal(
                f"Copying {desc} failed (rsync exit status {e.returncode})",
                labeled("from:", source),
                labeled("to:  ", dest),
            )


def dtqdm(total, desc="", unit="item", autorefresh=None, **kwargs):
    """Return a tqdm progress bar, or a no-op one in a dry run or without tqdm.

    For unknown totals (total=None) or when autorefresh=True, starts a background
    thread to refresh the display every second so elapsed time updates even during
    long operations.
    """

    class _dtqdm:
        """No-op progress bar for a dry run, or when tqdm isn't installed."""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def update(self, n=1):
            pass

        def write(self, msg, file=_STDOUT):
            emit(msg, file=file)

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

        def write(self, msg, file=_STDOUT):
            emit(msg, file=file)

    if __dry_run__:
        return _dtqdm()

    kwargs.setdefault("bar_format", BAR_FORMAT)
    kwargs.setdefault("leave", False)  # a finished bar clears its line
    kwargs.setdefault("disable", None)  # draw only when stderr is a terminal
    # tqdm drops bar_format on a terminal that reports 0 columns; shutil falls back to 80
    kwargs.setdefault("ncols", shutil.get_terminal_size().columns)

    # Starting a bar flushes stderr and stdout, as a drawn tqdm bar always has, even when
    # no bar is drawn; a broken stream stops the run here, as it always has
    flush_stream(sys.stderr, strict=True)
    flush_stream(sys.stdout, strict=True)
    if tqdm is None:
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

    # A failed copy exits the run, so this starts a new group of copies
    _copy_failed.clear()

    # A dry run shows each rsync command it skips
    if __dry_run__:
        for source, dest, desc in transfers:
            rsync_run(source, dest, desc=desc)
        return len(transfers)

    if __verbose__:
        for source, dest, desc in transfers:
            say(f"{desc}: {source} -> {dest}")
    success_count = 0

    with dtqdm(len(transfers), "Copying files", autorefresh=True) as pbar:

        def transfer_with_desc(transfer):
            source, dest, desc = transfer
            try:
                _, _, stderr = rsync_run(source, dest, desc=desc)
                # Print any problems after the transfer completes, as one block
                if stderr:
                    with _output_lock:
                        warn(f"rsync reported problems copying {desc}")
                        relay(stderr, 1, sys.stderr)
                return True
            except Exception as e:
                error(f"Cannot copy {desc}: {e}")
                return False

        with ccft.ThreadPoolExecutor(max_workers=len(transfers)) as exc:
            futures = {exc.submit(transfer_with_desc, t): t for t in transfers}
            for future in ccft.as_completed(futures):
                if future.result():
                    success_count += 1
                pbar.update(1)

    # Per-file results print after the bar closes, in transfer order
    if __verbose__:
        for future, (_, _, desc) in futures.items():
            if future.result():
                ok(f"{desc} copied")

    return success_count


def rsync_copy(source, dest, desc, label=None):
    """Copy one path with rsync_run; --verbose lists it like rsync_parallel.

    desc names the copy in error lines; label names it in the verbose lines.
    """
    label = label or desc
    if __verbose__ and not __dry_run__:
        say(labeled(f"{label}:", f"{source} -> {dest}"))
    _copy_failed.clear()  # a single copy always reports its own failure
    result = rsync_run(source, dest, desc=desc)
    if result is not None and result[0] == 0 and result[2].strip():
        warn(f"rsync reported problems copying {desc}")
        relay(result[2], 1, sys.stderr)
    if __verbose__ and result is not None and result[0] == 0:
        ok(f"{label} copied")
    return result


def run_scripts(root_dir, config, when, working_dir, phase=None):
    """Run a config's before or after scripts on this machine, in working_dir.

    On a host, snap.py runs there and runs the scripts itself (--run-scripts is passed
    to it), so they run as in a local restore or capture. phase is display only; it
    names the migration phase ('capture before scripts').
    """
    what = f"{when} scripts"
    if phase:
        what = f"{phase} {what}"

    scripts = None
    if config and "scripts" in config and when in config["scripts"]:
        scripts = config["scripts"][when]
    if not scripts:
        step_skipped(what, "none configured")
        return

    # With every script missing there is nothing to run under a 'Running' step
    try:
        missing = [path for path in scripts if not (root_dir / path).exists()]
    except (TypeError, OSError):
        missing = []  # A bad entry fails in the loop below, where it always has
    if missing and len(missing) == len(scripts):
        step_skipped(what, "no scripts found")
        for run_script_path in missing:
            warn(f"Script {run_script_path} not found in {root_shown(root_dir)}; skipping it")
        return

    step(f"Running {what}")

    for run_script_path in scripts:
        script_path = root_dir / run_script_path
        if not script_path.exists():
            warn(f"Script {run_script_path} not found in {root_shown(root_dir)}; skipping it")
            continue
        script_name = script_path.name
        say(script_name)

        # Run in the capture directory
        script_cmd = ["bash", str(script_path)]
        if __dry_run__:
            echo(script_cmd, 2)
            if working_dir:
                say(f"cwd: {shown(working_dir)}", 2)
            continue
        try:
            # A script takes as long as it needs: no wall-clock limit
            result = subprocess.run(
                script_cmd,
                check=True,
                cwd=working_dir,
                capture_output=True,
                text=True,
            )
            # Print script output after completion
            relay(result.stdout, 2)
            relay(result.stderr, 2, sys.stderr)
            ok(f"{script_name} completed")
        except subprocess.CalledProcessError as e:
            # Print captured output on error
            relay(e.stdout, 2)
            relay(e.stderr, 2, sys.stderr)
            fatal(f"Script {script_name} failed (exit status {e.returncode})")


# --- Snap Configuration --- #

DEFAULT_ROOT_SNAP = "~/.snap"

# A host's snap root when no path is given, relative to its login directory
DEFAULT_REMOTE_ROOT = ".snap"

DEFAULT_CONFIG_CAPTURE = "configs/capture.toml"
DEFAULT_CONFIG_DEPLOY = "configs/deploy.toml"
DEFAULT_CONFIG_RESTORE = "configs/restore.toml"
DEFAULT_CONFIG_MIGRATE = "configs/migrate.toml"

DEFAULT_SNAPSHOT_TOML = "snapshot.toml"


def parse_remote_arg(value, flag=None):
    """Parse a [user@]host:[path], user@host, or local path argument into (host, path).

    Returns (None, None) for empty values, (None, path) for local paths,
    or (host, path) for remote. 'user@host' and 'host:' return (host, None).
    A local path containing '@' needs a '/' (e.g. './name@tag').
    flag names the option the value came from in error lines.
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
    where = f"{flag} '{value}'" if flag else f"'{value}'"
    user, at, hostname = host.rpartition("@")
    if not hostname or hostname.startswith("-") or user.startswith("-") or (at and not user):
        fatal(
            f"Invalid host '{host}' in {where}",
            "Use [user@]host[:path]; the user and host cannot be empty or start with '-'",
        )

    problem = remote_path_problem(path) if path else None
    if problem:
        fatal(f"Invalid path '{path}' in {where}: {problem}", REMOTE_PATH_HINT)

    return host, Path(path) if path else None


# Characters a remote path may hold. rsync before 3.2.4, and macOS's /usr/bin/rsync,
# pass remote paths to the remote shell unescaped, so any other character could be
# split or expanded there
REMOTE_PATH_CHARS = re.compile(r"[A-Za-z0-9._/~+=,%@-]*")
REMOTE_PATH_HINT = "Remote paths can use letters, digits and . _ / ~ + = , % @ -"


def remote_path_problem(path):
    """Return why a path can't be used on a remote host, or None if it can."""
    if not REMOTE_PATH_CHARS.fullmatch(str(path)):
        return "it has characters rsync can't pass to a host safely"
    parts = normalize_remote_path(path).parts
    if parts and parts[0].startswith("-"):
        return "it starts with '-', which ssh and rsync would read as an option"
    return None


def expand_path(path_str):
    """Expand environment variables and user home in a path string."""
    return Path(os.path.expandvars(path_str)).expanduser()


def normalize_remote_path(path):
    """Normalize a path on a remote host: '~' -> '.', '~/x' -> 'x', others as given.

    ssh commands and rsync both resolve a relative path against the remote login
    directory, so the path never depends on how a remote shell expands '~'.
    """
    parts = PurePosixPath(path).parts
    if parts and parts[0] == "~":
        parts = parts[1:]
    return PurePosixPath(*parts)


def remote_snap_root(args, host):
    """Return the snap root on a host: the -r path for the -r host, else .snap."""
    if host and host == getattr(args, "root_host", None):
        return args.root_path
    return PurePosixPath(DEFAULT_REMOTE_ROOT)


# A remote snap root (-r host:path) is read from a local copy of its configs/ and
# scripts/; messages name that copy by the host:path it came from
_root_names = {}

# rsync filter rules that copy only a snap root's configs/ and scripts/ (captures/ and
# anything else stay on the host). Either may be missing
ROOT_COPY_FILTER = [
    "--include=/configs/",
    "--include=/configs/**",
    "--include=/scripts/",
    "--include=/scripts/**",
    "--exclude=*",
]


def root_shown(path):
    """Return a path for messages, naming a remote snap root's copy by its host:path."""
    text = str(path)
    for copy, name in _root_names.items():
        if text == copy or text.startswith(copy + os.sep):
            return name + text[len(copy):]
    return text


def root_fetch(host, path, local_dir):
    """Copy a remote snap root's configs/ and scripts/ into local_dir.

    This only reads the host, so a dry run copies too and can read the real configs.
    The caller creates and removes local_dir.
    """
    source = f"{host}:{path}/"
    result = rsync_run(
        source,
        f"{local_dir}/",
        check=False,
        desc=f"the snap root from {host}",
        options=ROOT_COPY_FILTER,
        read_only=True,
    )
    if result[0] != 0:
        relay(result[2], 1, sys.stderr)
        details = [labeled("from:", source)]
        if str(path) == DEFAULT_REMOTE_ROOT:
            details.append(
                f"Create a {DEFAULT_REMOTE_ROOT} directory on {host}, "
                f"or pass -r/--snap-root {host}:<path>"
            )
        fatal(f"Copying the snap root from {host} failed (rsync exit status {result[0]})", *details)
    _root_names[str(local_dir)] = f"{host}:{path}"


def root_display(root):
    """Expand an archive root for display, or show it as given if it can't be expanded.

    Display only: the action that uses the root reports the real error.
    """
    try:
        return expand_path(root) if root else Path("/")
    except (RuntimeError, KeyError, OSError, TypeError):
        return Path(str(root))


def resolve_root(specified_root):
    """Resolve the local snap root directory (main() handles a remote one).

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
    fatal(
        "No .snap directory found",
        f"searched: {pwd_root}",
        f"searched: {home_root.resolve()}",
        "Create a .snap directory, or pass -r/--snap-root <path>",
    )


def resolve_snapshot_root(captures_dir):
    """Find the most recent capture snapshot under captures/.

    Tries today first, then scans all YYYY/MM-DD directories for the latest.
    """
    if not captures_dir.exists():
        fatal(
            f"Captures directory not found: {captures_dir}",
            "Run 'snap.py capture' first, or pass --from <snapshot>",
        )

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
        fatal(
            f"No snapshots found in {captures_dir}",
            "Run 'snap.py capture' first, or pass --from <snapshot>",
        )

    return latest


# A snapshot's snapshot.toml below a captures directory: YYYY/MM-DD/<id>/snapshot.toml.
# Stray files and dirs elsewhere don't match
SNAPSHOT_TOML_GLOB = f"[0-9][0-9][0-9][0-9]/[0-9][0-9]-[0-9][0-9]/*/{DEFAULT_SNAPSHOT_TOML}"


def resolve_remote_snapshot(host, captures_dir):
    """Find the latest snapshot on a host: the one whose snapshot.toml is newest.

    Returns the snapshot directory's path on the host. The listing runs under sh, so the
    glob and 'ls -t' work alike on BSD and GNU hosts whatever the login shell. It only
    reads, so a dry run looks too.
    """
    captures = PurePosixPath(captures_dir)
    listing = (
        f"ls -1t -- {shlex.quote(str(captures))}/{SNAPSHOT_TOML_GLOB} 2>/dev/null | head -1"
    )

    step(f"Finding the latest snapshot on {host}")
    code, stdout, stderr = ssh_run(
        host,
        f"sh -c {shlex.quote(listing)}",
        check=False,
        desc="Finding the latest snapshot",
        read_only=True,
    )
    if code != 0:
        relay(stderr, 1, sys.stderr)
        if code == 255:
            fatal(f"Finding the latest snapshot failed on {host}: ssh error (exit status 255)")
        fatal(f"Finding the latest snapshot failed on {host} (exit status {code})")

    lines = stdout.strip().splitlines()
    if not lines:
        fatal(f"No snapshots found in {host}:{captures}")

    latest = PurePosixPath(lines[0]).parent
    problem = remote_path_problem(latest)
    if problem:
        fatal(f"Cannot use snapshot {latest} on {host}: {problem}", REMOTE_PATH_HINT)
    try:
        say(f"latest: {latest.relative_to(captures)}")
    except ValueError:
        say(f"latest: {latest}")
    return latest


def load_config(root_dir, config_name):
    """Load a TOML config file from the root directory."""
    config_file = root_dir / config_name

    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        fatal(f"Config file not found: {root_shown(config_file)}")
    except tomllib.TOMLDecodeError as e:
        fatal(f"Cannot parse {root_shown(config_file)}: {e}")


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


def toml_key(key):
    """Format a key or table name: bare if it has only letters, digits and '_', else quoted."""
    if re.fullmatch(r"[A-Za-z0-9_]+", key):
        return key
    return toml_string(key)


def toml_dumps(config):
    """Format a dict as TOML: its plain values first, then each dict value as a table.

    Nested dicts get dotted headers ([tar."ssh-keys"]). A table that holds only tables
    gets no header of its own; an empty table keeps its header, so it still exists.
    Raises TypeError for values toml_value() can't format.
    """
    lines = []

    def add_table(path, table):
        values = {key: value for key, value in table.items() if not isinstance(value, dict)}
        tables = {key: value for key, value in table.items() if isinstance(value, dict)}

        if path and (values or not tables):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(toml_key(name) for name in path) + "]")
        for key, value in values.items():
            lines.append(f"{toml_key(key)} = {toml_value(value)}")

        for name, subtable in tables.items():
            add_table([*path, name], subtable)

    add_table([], config)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def verify_capture_config(config, config_name, table="tar"):
    """Verify capture configuration structure.

    table names the [tar] table in error lines (migrate.toml calls it [capture.tar]).
    """
    if "tar" not in config:
        fatal(f"{config_name}: missing required [{table}] table")

    tar_config = config["tar"]
    if not tar_config or not isinstance(tar_config, dict):
        fatal(f"{config_name}: [{table}] must be a non-empty table")

    # Verify each category has required fields
    for category, data in tar_config.items():
        if category in ("", ".", "..") or "/" in category:
            fatal(f"{config_name}: [{table}] table name '{category}' can't name an archive")
        if not isinstance(data, dict):
            fatal(f"{config_name}: [{table}.{category}] must be a table")

        if "root" not in data:
            fatal(f"{config_name}: [{table}.{category}] is missing the required 'root' key")

        if "dirs" not in data and "files" not in data:
            fatal(f"{config_name}: [{table}.{category}] needs a 'dirs' or 'files' key")


def verify_restore_config(config, config_name):
    """Verify restore/deploy configuration structure."""
    if "tar" not in config:
        fatal(f"{config_name}: missing required [tar] table")

    tar_config = config["tar"]
    if not isinstance(tar_config, dict):
        fatal(f"{config_name}: [tar] must be a table")

    if "archives" not in tar_config:
        fatal(f"{config_name}: [tar] is missing the required 'archives' key")

    archives = tar_config["archives"]
    if archives is not None and not isinstance(archives, list):
        fatal(f"{config_name}: the 'archives' key in [tar] must be an array of names or patterns")


def verify_snapshot(capture_dir, label=None):
    """Validate that a capture directory contains required files.

    label names the snapshot in error lines (defaults to capture_dir).
    """
    if label is None:
        label = capture_dir

    if not capture_dir.exists():
        if is_placeholder(capture_dir):
            return None
        if __dry_run__:
            error(f"Snapshot directory not found: {label}", "A real run stops here")
            return None
        fatal(
            f"Snapshot directory not found: {label}",
            "Pass --from a snapshot directory (captures/YYYY/MM-DD/<checksum>), "
            "a date directory, or [user@]host: for a remote snapshot",
        )

    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML
    if not toml_path.exists():
        fatal(
            f"{DEFAULT_SNAPSHOT_TOML} not found in {label}",
            "A snapshot directory looks like captures/YYYY/MM-DD/<checksum>",
        )

    # Load compression type from snapshot TOML
    try:
        with open(toml_path, "rb") as f:
            snapshot_config = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        fatal(f"Cannot parse {label}/{DEFAULT_SNAPSHOT_TOML}: {e}")

    compress_type = snapshot_config.get("tarball", {}).get("compress", "gzip")
    if compress_type not in COMPRESS_MAP:
        compress_error(compress_type, DEFAULT_SNAPSHOT_TOML)

    ext, _ = COMPRESS_MAP[compress_type]

    # Check that at least one tar archive exists
    archives = list(capture_dir.glob(f"*{ext}"))
    if not archives:
        fatal(f"No archives found in {label}")

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
    Each group is one count line, or one sorted line per path with --verbose.
    """
    groups = {truthy: [], falsey: []}
    for path in paths:
        if cond(path):
            groups[truthy] += [path]
        else:
            groups[falsey] += [path]

    lines = {truthy: [], falsey: []}
    for prefix, group in groups.items():
        if __verbose__:
            lines[prefix] = [f"{prefix}: {path}" for path in sorted(group)]
        elif group:
            lines[prefix] = [f"{prefix}: {plural(len(group), 'path')}"]
    return lines[truthy], lines[falsey]


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
                warnings += [f"skip: pattern '{pattern}' (no match)"]
        else:
            # Literal path - add as-is
            if pattern not in matched_paths:
                expanded.append(pattern)
                matched_paths.add(pattern)
    return expanded, warnings


def archive_create(name, root, paths, outdir, compress="gzip"):
    """Create a compressed tar archive for a category.

    Returns (archive_path, lines, warnings): the item header and include lines, then the
    skip lines, for the caller to print.
    """
    if compress not in COMPRESS_MAP:
        compress_error(compress, "the capture config's [tarball] table")

    ext, mode = COMPRESS_MAP[compress]
    archive_path = outdir / f"{name}{ext}"
    root_path = expand_path(root)

    # Expand any glob patterns in the paths
    expanded_paths, expand_warnings = archive_expand(paths, root_path)

    lines = [f"{archive_path.name} (root: {root_path})"]
    includes, skips = archive_classify(
        expanded_paths,
        "include",
        "skip",
        cond=lambda path: (root_path / path).exists(),
    )
    lines += includes
    warnings = [f"{line} (not found)" for line in skips] + expand_warnings

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

    step(f"Creating {plural(len(tasks), 'archive')}")

    # Check once here, so the workers don't each print the same error
    if compress not in COMPRESS_MAP:
        compress_error(compress, "the capture config's [tarball] table")
    ext, _ = COMPRESS_MAP[compress]

    results, buffer, failed = [], {}, set()
    with ccft.ThreadPoolExecutor(max_workers=len(tasks)) as exc:
        futures = {exc.submit(archive_create, *task, compress): task for task in tasks}
        with dtqdm(len(tasks), "Creating archives", autorefresh=True) as pbar:
            for future in ccft.as_completed(futures):
                task = futures[future]
                category = task[0]
                try:
                    archive_path, lines, warnings = future.result()
                    buffer[archive_path.name] = [*lines, *warnings]
                    results.append(archive_path)
                except Exception as e:
                    # capture() stops after the list below, without saving the snapshot
                    arcname = f"{category}{ext}"
                    error(f"Cannot create {arcname}: {e}")
                    # A failed archive keeps its place in the list below
                    failed.add(arcname)
                    buffer[arcname] = [f"{arcname} (root: {root_display(task[1])})"]
                pbar.update(1)

    # Print each archive's block after the bar closes, in name order
    for arcname in sorted(buffer):
        header, *lines = buffer[arcname]
        say(header)
        if arcname in failed:
            say("failed (see the error above)", 2)
            continue
        empty = not any(line.startswith("include:") for line in lines)
        if empty:
            lines.insert(0, "include: 0 paths")
        for line in lines:
            say(line, 2)
        if empty:
            warn(f"{arcname}: none of its paths exist; the archive is empty")
    return results


def generate_snapshot_toml(
    outdir, compress="gzip", category_meta=None, roll_ext=None
):
    """Generate snapshot TOML with checksums for all archives."""
    if category_meta is None:
        category_meta = {}

    # Find archives based on compression type
    if compress not in COMPRESS_MAP:
        compress_error(compress, "the capture config's [tarball] table")

    ext, _ = COMPRESS_MAP[compress]

    toml_path = outdir / DEFAULT_SNAPSHOT_TOML

    # The caller prints the step; a dry run has no archives to checksum
    if __dry_run__:
        return toml_path, DRY_RUN_CHECKSUM

    archives = list(outdir.glob(f"*{ext}"))

    # Calculate checksums for all archives
    checksums = {}
    with dtqdm(len(archives), "Computing checksums", autorefresh=True) as pbar:
        for archive in archives:
            category = archive_category(archive)
            checksum = calculate_file_checksum(archive, show=False)
            checksums[category] = checksum
            pbar.update(1)

    # [tarball]: the compression (when not gzip), the rollback extension and the digest.
    # root, link and rollback are written as strings, as the capture config's values
    # always were
    tarball = {}
    if compress != "gzip":
        tarball["compress"] = compress
    if roll_ext:
        tarball["rollback"] = str(roll_ext)
    tarball["checksum"] = "sha256"

    # One [tar.<name>] table per archive, in name order
    tar_tables = {}
    for category in sorted(checksums.keys()):
        meta = category_meta.get(category, {})
        table = {}
        if "root" in meta:
            table["root"] = str(meta["root"])
        if meta.get("link"):
            table["link"] = str(meta["link"])
        table["checksum"] = checksums[category]
        tar_tables[category] = table

    snapshot = {"tarball": tarball}
    if tar_tables:
        snapshot["tar"] = tar_tables

    # tomllib reads TOML as UTF-8, whatever the locale
    with open(toml_path, "w", encoding="utf-8") as f:
        f.write("# Snapshot written by snap.py capture\n")
        f.write(toml_dumps(snapshot))

    # Combined checksum from all archive checksums
    combined = "".join(checksums[cat] for cat in sorted(checksums))
    return toml_path, calculate_checksum(combined.encode("utf-8"))


# --- Archive Extraction Handlers --- #


def archive_category(archive):
    """Return an archive's [tar.<name>] name: its file name without the tar extension."""
    name = Path(archive).name
    # Longest extensions first, so 'x.tar.gz' loses '.tar.gz', not just '.gz'
    for ext in sorted((ext for ext, _ in COMPRESS_MAP.values()), key=len, reverse=True):
        if name.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)]
    return Path(archive).stem


def archive_entries(tar, report=True):
    """Group archive members by captured path, in archive order.

    A captured path is a member with no ancestor member: capturing '.config/nvim'
    yields '.config/nvim', not '.config', so restore never touches its siblings.
    Members below a symlink member are skipped, since extracting them would write
    through the link into paths that were never backed up. report prints one Warning
    per archive for them (their names with --verbose).
    Raises ValueError for absolute or '..' member paths.
    """
    members = tar.getmembers()
    names, links = set(), set()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe path '{member.name}'")
        if path.parts:
            names.add(path)
            if member.issym():
                links.add(path)

    groups, skipped = {}, []
    for member in members:
        path = Path(member.name)
        if not path.parts:
            continue

        # Ancestors from shallowest to deepest, without '.'
        ancestors = list(reversed(path.parents))[1:]
        if any(ancestor in links for ancestor in ancestors):
            skipped.append(member.name)
            continue

        entry = next((a for a in ancestors if a in names), path)
        groups.setdefault(str(entry), []).append(member)

    if report and skipped:
        archive_name = Path(tar.name or "archive").name
        entries = plural(len(skipped), "entry", "entries")
        warn(
            f"{archive_name}: skipping {entries} below a symlink in the archive",
            *(skipped if __verbose__ else []),
        )
    return groups


def archive_confirm(archive, compress="gzip", root=None):
    """Show what restoring an archive changes, then ask before it is extracted.

    Returns True for yes, False for no (declined or no answer), and None when the
    archive can't be read or is unsafe (the error is reported here).
    """
    if compress not in COMPRESS_MAP:
        compress_error(compress, DEFAULT_SNAPSHOT_TOML)

    # The item header comes first, so an error opening the archive appears under it. A
    # root that can't be expanded shows as given; archive_extract reports the error
    root_path = root_display(root)
    say(f"{archive.name} (root: {root_path})")

    # Get captured paths from the archive (auto-detect compression)
    try:
        with tarfile.open(archive, "r:*") as tar:
            entries = archive_entries(tar)
    except (tarfile.TarError, ValueError, OSError) as e:
        error(f"Cannot restore {archive.name}: {e}")
        say("failed (see the error above)", 2)
        return None

    # Show each path the prompt would overwrite, then each it would add, relative to
    # the root in the header
    if not entries:
        say("empty archive, no files to replace or add", 2)
    names = sorted(entries)
    existing = {name for name in names if os.path.lexists(root_path / name)}
    for name in names:
        if name in existing:
            say(f"replace: {name}", 2)
    for name in names:
        if name not in existing:
            say(f"add: {name}", 2)

    # Every archive asks once; only 'y' or 'yes' restores, and a dry run assumes yes
    if not entries:
        question = f"Restore {archive.name}? It has no files"
    elif existing:
        question = f"Restore {archive.name}? Existing files will be overwritten"
    else:
        question = f"Restore {archive.name}?"
    return ask(question)


def snapshot_archives(capture_dir, snapshot_config, ext):
    """List the archives a snapshot can restore: its [tar.<name>] tables whose file exists.

    Archive files that snapshot.toml doesn't list are never restored, so they can't skip
    the checksum check or extract without a root.
    """
    available = []
    for name in snapshot_config.get("tar", {}):
        # A name with a '/' would point outside the snapshot dir, and '.' or '..' would
        # put its backups outside the backup directory
        if name in ("", ".", ".."):
            continue
        archive = capture_dir / f"{name}{ext}"
        if archive.parent == capture_dir and archive.is_file():
            available.append(archive)
    return available


def archive_select(available, patterns):
    """Select the archives to restore from `available` by name or glob pattern.

    An empty pattern list selects every archive. Returns (archives, unmatched
    patterns); the caller reports the unmatched ones.
    """
    if not patterns:
        return available, []

    names = {archive_category(archive): archive for archive in available}
    archives_to_restore = []
    matched_names = set()
    unmatched = []

    for pattern in patterns:
        # An exact name first, then a glob over every archive in the snapshot; a match
        # already selected by an earlier pattern still counts
        if pattern in names:
            matches = [pattern]
        else:
            matches = [name for name in names if fnmatch.fnmatch(name, pattern)]
        if not matches:
            unmatched.append(pattern)

        for name in matches:
            if name not in matched_names:
                archives_to_restore.append(names[name])
                matched_names.add(name)

    return archives_to_restore, unmatched


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
    if compress not in COMPRESS_MAP:
        compress_error(compress, DEFAULT_SNAPSHOT_TOML)

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
            kept = {id(m) for group in archive_entries(tar, report=False).values() for m in group}
            members = [m for m in tar.getmembers() if id(m) in kept]
            extract_members(tar, extract_path, members)
        return True
    except Exception as e:
        # Runs in a worker thread; error() is safe while the bar is live
        error(f"Cannot extract {archive.name}: {e}")
        return False


def backup_rotation_path(rollbackd):
    """Pick the unused name an existing backup dir moves to: <dir>_<timestamp>[_<n>]."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prevbackd = Path(f"{rollbackd}_{timestamp}")
    count = 1
    while prevbackd.exists():
        prevbackd = Path(f"{rollbackd}_{timestamp}_{count}")
        count += 1
    return prevbackd


def backup_dir(root_path, roll_ext):
    """Return a root's backup directory, <root><roll_ext>; each archive gets a subdirectory."""
    return Path(str(root_path) + roll_ext)


def restore_category(archive, root, roll_ext, compress="gzip", rotated=None):
    """Transactionally restore a single category with rollback on failure.

    Replaces only the paths captured in the archive, one at a time:
    1. Backs up the existing path (copy, not move) to <root><roll_ext>/<archive name>
    2. Removes the original
    3. Extracts the captured path from the archive
    4. On any error or Ctrl-C: restores every touched path from backup

    rotated is the set of backup directories already moved aside in this restore run.
    The first archive with a given root moves that root's old backup directory aside;
    later ones reuse the fresh one, so they never rotate each other's backups. None
    makes this call a run of its own.
    """
    if compress not in COMPRESS_MAP:
        compress_error(compress, DEFAULT_SNAPSHOT_TOML, stop=False)
        return False
    if rotated is None:
        rotated = set()

    root_path = expand_path(root)
    backups = backup_dir(root_path, roll_ext)
    rollbackd = backups / archive_category(archive)

    # The item header comes first, so an error opening the archive appears under it
    say(f"{archive.name} (root: {root_path}, backup: {rollbackd})")

    # Always open archive and classify entries
    try:
        with tarfile.open(archive, "r:*") as tar:
            groups = archive_entries(tar)
    except (tarfile.TarError, ValueError, OSError) as e:
        error(f"Cannot restore {archive.name}: {e}")
        say("failed (see the error above)", 2)
        return False

    # Classify entries: replace (existing) vs add (new)
    replaces, adds = archive_classify(
        list(groups),
        "replace",
        "add",
        cond=lambda entry: os.path.lexists(root_path / entry),
    )

    if not groups:
        say("empty archive, no files to replace or add", 2)
    for line in replaces + adds:
        say(line, 2)

    # The same root can be spelled two ways (a symlink, '..'); rotate it once all the same
    rotation_key = os.path.realpath(backups)
    rotate = rotation_key not in rotated
    if __dry_run__:
        # Show the rotation a real run would make; nothing is renamed
        rotated.add(rotation_key)
        if __verbose__ and rotate:
            try:
                if backups.exists():
                    say(f"rotate: {backups} -> {backup_rotation_path(backups)}", 2)
            except OSError:
                pass  # A real run reports it when it creates the backup directory
        return True

    # Move the root's old backup directory aside (once per run), then create this
    # archive's subdirectory in the fresh one
    try:
        if rotate:
            if backups.exists():
                prevbackd = backup_rotation_path(backups)
                backups.rename(prevbackd)
                if __verbose__:
                    say(f"rotate: {backups} -> {prevbackd}", 2)
            rotated.add(rotation_key)
        # Creates rollbackd and any missing parents with their parent's owner
        make_parents(rollbackd / "entry")
    except OSError as e:
        error(f"Cannot create backup directory {rollbackd}: {e.strerror or e}")
        say("failed (see the error above)", 2)
        return False

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

        ok(f"{archive.name} restored")
        return True

    except (Exception, KeyboardInterrupt) as e:
        # Ignore further Ctrl-C so the rollback itself can't be cut short
        try:
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except ValueError:
            previous_sigint = None  # Not in the main thread
        try:
            restore_rollback(archive.name, root_path, rollbackd, touched, e)
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)

        if isinstance(e, KeyboardInterrupt):
            raise
        return False


def restore_rollback(name, root_path, rollbackd, touched, cause):
    """Delete touched paths and restore them from backup; exit if that fails.

    name is the archive file name, used only in messages; cause is the exception.
    """
    # Roll back before printing anything, so an output error can't skip the rollback
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

    reason = "interrupted" if isinstance(cause, KeyboardInterrupt) else cause
    error(f"Cannot restore {name}: {reason}")
    if touched:
        say(f"rollback: {plural(len(touched), 'path')} from {rollbackd}", 2)
    else:
        say("rollback: nothing was changed", 2)

    if failures:
        # Stop before a later category rotates these backups away
        fatal(
            f"Rollback of {name} is incomplete; backups are in {rollbackd}",
            *failures,
            "Stopping the restore so the backups stay in place",
        )

    ok(f"{name} rolled back")


def extract_archives(
    archives, skip_confirm=False, root_map=None, compress="gzip", restored=None, failed=None
):
    """Extract multiple archives in parallel (after confirmation); returns how many.

    When the caller passes lists, `restored` gets each extracted archive and `failed`
    each one that can't be read or extracted, in the given order. Declined archives
    are in neither.
    """
    if restored is None:
        restored = []
    if failed is None:
        failed = []
    if not archives:
        return 0

    if root_map is None:
        root_map = {}

    # First, collect confirmations sequentially (unless skipped)
    confirmed, unreadable = [], set()
    if skip_confirm:
        confirmed = list(archives)
    else:
        for archive in archives:
            category = archive_category(archive)
            root = root_map.get(category)
            answer = archive_confirm(archive, compress, root)
            if answer is None:
                unreadable.add(archive)
            elif answer:
                confirmed.append(archive)

    # Extract confirmed archives in parallel (the caller printed the step)
    extracted = set()
    if confirmed:
        with ccft.ThreadPoolExecutor(max_workers=len(confirmed)) as exc:
            # Build futures with root for each archive
            futures = {}
            for archive in confirmed:
                category = archive_category(archive)
                root = root_map.get(category)
                futures[exc.submit(archive_extract, archive, compress, root)] = archive

            with dtqdm(len(confirmed), "Extracting archives", autorefresh=True) as pbar:
                for future in ccft.as_completed(futures):
                    archive = futures[future]
                    try:
                        if future.result():
                            extracted.add(archive)
                    except Exception as e:
                        error(f"Cannot extract {archive.name}: {e}")
                    pbar.update(1)

        # Step result: the prompts above have no results of their own
        total = plural(len(confirmed), "archive")
        if len(extracted) == len(confirmed):
            ok(f"{total} extracted")
        else:
            say(f"{len(extracted)} of {total} extracted")

    for archive in archives:
        if archive in extracted:
            restored.append(archive)
        elif archive in unreadable or archive in confirmed:
            failed.append(archive)
    return len(extracted)


def create_symlinks(snapshot_config, restored):
    """Create symlinks from link -> root for the restored archives with a 'link' key.

    restored holds the [tar.<name>] names of the archives this run restored (in a dry
    run, the ones it would restore); other archives' links are left alone. Returns the
    number of links created.
    """
    tar_sections = snapshot_config.get("tar", {})
    if not tar_sections:
        return 0

    symlinks_created = []
    started = False
    for category, section_data in tar_sections.items():
        if category not in restored:
            continue
        link = section_data.get("link")
        root = section_data.get("root")

        if not link or not root:
            continue

        # Expand environment variables
        link_path = expand_path(link)
        root_path = expand_path(root)

        # Nothing to do when the link already points at the root
        if os.path.islink(link_path) and link_path.resolve() == root_path.resolve():
            continue

        # The step header prints before the first link that needs work
        if not started:
            step("Creating symlinks")
            started = True

        # Never replace an existing file or a link that points elsewhere
        if os.path.islink(link_path):
            fatal(
                f"Cannot create symlink {link_path}: it points to {link_path.resolve()}, "
                f"not {root_path.resolve()}",
                f"Set by the 'link' key in the [tar.{category}] table of "
                f"{DEFAULT_SNAPSHOT_TOML}; fix or remove the existing link, then restore again",
            )
        if os.path.lexists(link_path):
            fatal(
                f"Cannot create symlink {link_path}: a file or directory is already there",
                "Move it aside, then restore again",
            )

        if not __dry_run__:
            make_parents(link_path)
            link_path.symlink_to(root_path)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                owner = link_path.parent.stat()
                os.chown(link_path, owner.st_uid, owner.st_gid, follow_symlinks=False)

        symlinks_created.append(str(link_path))
        ok(f"{link_path} -> {root_path}", dry=f"{link_path} -> {root_path}")

    return len(symlinks_created)


# --- Checksum Handlers --- #

CHECKSUM_CHUNK_SIZE = 8192

UNVERIFIED_WARNING = (
    f"{DEFAULT_SNAPSHOT_TOML} sets no [tarball] checksum; skipping archive verification"
)


def sha256_digest(data):
    """Calculate a SHA-256 digest."""
    sha256 = hashlib.sha256()
    sha256.update(data)
    return sha256.hexdigest()


def sha256_file_digest(filepath, show=False):
    """Calculate a SHA-256 digest from a file.

    show is accepted for existing callers and ignored; callers show their own bar.
    """
    sha256 = hashlib.sha256()
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


def verify_archives_from_toml(capture_dir, warn_unverified=True):
    """Verify all archives using tarball TOML checksums.

    warn_unverified=False leaves the Warning about a snapshot with no checksum to the
    caller, so a sudo re-run doesn't print it twice.
    """
    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML

    # verify_snapshot already reported a missing path; a pulled snapshot doesn't exist yet
    # (restore() adds a Note where the restore would run)
    if __dry_run__ and not toml_path.exists():
        return {}

    if not toml_path.exists():
        fatal(f"{DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}")

    # Load snapshot TOML
    with open(toml_path, "rb") as f:
        snapshot_config = tomllib.load(f)

    # Get tarball options
    tarball_opts = snapshot_config.get("tarball", {})
    compress_type = tarball_opts.get("compress", "gzip")
    digest_type = tarball_opts.get("checksum")

    # Verify compression type
    if compress_type not in COMPRESS_MAP:
        compress_error(compress_type, DEFAULT_SNAPSHOT_TOML)

    archive_ext, _ = COMPRESS_MAP[compress_type]

    # If no checksum specified in options, skip verification
    if not digest_type:
        if warn_unverified:
            warn(UNVERIFIED_WARNING)
        return snapshot_config

    if digest_type != "sha256":
        fatal(
            f"Unsupported checksum '{digest_type}' in {DEFAULT_SNAPSHOT_TOML}",
            "supported: sha256",
        )

    # Get all tar sections
    tar_sections = snapshot_config.get("tar", {})

    if not tar_sections:
        fatal(f"{DEFAULT_SNAPSHOT_TOML} lists no archives (no [tar.<name>] tables)")

    count = plural(len(tar_sections), "archive")
    step(f"Verifying {count}")

    # List archives to verify
    if __verbose__:
        for category in sorted(tar_sections.keys()):
            archive_path = capture_dir / f"{category}{archive_ext}"
            say(f"verify: {archive_path.name}")

    if __dry_run__:
        say("checksums are not checked in a dry run")
        return snapshot_config

    # Verify each archive; problems print as one Error block after the bar
    problems = []
    bad = 0
    with dtqdm(len(tar_sections), "Verifying archives", autorefresh=True) as pbar:
        for category, section_data in tar_sections.items():
            archive_path = capture_dir / f"{category}{archive_ext}"

            if not archive_path.exists():
                problems.append(f"{archive_path.name}: not found")
                bad += 1
                pbar.update(1)
                continue

            # Rejoin checksum chunks
            checksum_chunks = section_data.get("checksum", [])
            if not checksum_chunks:
                problems.append(f"{category}{archive_ext}: no checksum in {DEFAULT_SNAPSHOT_TOML}")
                bad += 1
                pbar.update(1)
                continue

            expected_checksum = "".join(checksum_chunks)

            # Calculate actual checksum
            actual_checksum = calculate_file_checksum(archive_path, show=False)

            if actual_checksum != expected_checksum:
                problems.append(f"{archive_path.name}: checksum mismatch")
                if __verbose__:
                    problems.append(f"  expected: {expected_checksum}")
                    problems.append(f"  actual:   {actual_checksum}")
                bad += 1

            pbar.update(1)

    if problems:
        fatal(
            f"Snapshot verification failed for {bad} of {count}",
            *problems,
            "The snapshot may be corrupted; capture a new one or pick another with --from",
        )

    ok(f"{count} verified")
    return snapshot_config


# --- Remote Work Directories --- #


def remote_workdir(host, purpose, subdirs=()):
    """Create a private work directory on a host, with its subdirectories; returns its path.

    mktemp -d makes it 0700 with an unpredictable name under the host's $TMPDIR (or /tmp),
    and prints the path. subdirs are created in it with one mkdir -p. A dry run shows the
    commands and returns a named placeholder. The caller removes the directory with
    remote_workdir_remove() in a finally block.
    """
    step(f"Creating work directory on {host}")
    template = f"${{TMPDIR:-/tmp}}/snap-{purpose}.XXXXXXXX"
    _, stdout, _ = ssh_run(host, f'mktemp -d "{template}"', desc="Creating the work directory")

    if __dry_run__:
        work_dir = PurePosixPath(DRY_RUN_WORK_DIR)
    else:
        # The path is the last line (a login script may print before it). Anything but
        # an absolute snap-<purpose>.* path is never used, so rm -rf only gets mktemp's
        lines = stdout.strip().splitlines()
        work_dir = PurePosixPath(lines[-1].strip() if lines else "")
        usable = work_dir.is_absolute() and work_dir.name.startswith(f"snap-{purpose}.")
        if not usable or remote_path_problem(work_dir):
            fatal(
                f"Cannot create a work directory on {host}: mktemp printed no usable path",
                labeled("output:", stdout.strip() or "(none)"),
            )

    created = False
    try:
        if subdirs:
            paths = " ".join(shlex.quote(str(work_dir / name)) for name in subdirs)
            ssh_run(host, f"mkdir -p {paths}", desc="Creating the work directory")
        created = True
    finally:
        if not created:
            remote_workdir_remove(host, work_dir, report=False)
    return work_dir


def remote_workdir_remove(host, work_dir, report=True):
    """Remove a work directory from a host, best effort.

    After a run that went well (report=True) it prints its step, and a failed rm is a
    Warning. After a failure or Ctrl-C (report=False) it prints nothing, so the run still
    ends with its own Error block, and never raises for ssh problems.
    """
    command = f"rm -rf {shlex.quote(str(work_dir))}"

    if report:
        step(f"Removing work directory from {host}")
        code, _, stderr = ssh_run(
            host, command, check=False, desc="Removing the work directory"
        )
        if code != 0:
            relay(stderr, 1, sys.stderr)
            cause = "ssh error (exit status 255)" if code == 255 else f"exit status {code}"
            warn(f"Cannot remove work directory {work_dir} from {host}: {cause}")
        return

    if __dry_run__:
        return  # Nothing was created
    try:
        subprocess.run(
            ssh_argv(host, command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # Best effort; the run is already failing


# --- Remote snap.py Runs --- #

# snap.py's name in a host's work directory
REMOTE_SCRIPT = "snap.py"


def remote_scripts(root_dir, config, host):
    """List the before and after scripts to copy to a host for --run-scripts.

    Returns their paths relative to the snap root. Each is copied to the same path in the
    work directory, so snap.py there (-r <work dir>) finds it where the config says.
    Missing scripts are left out; snap.py on the host warns about them. A path outside the
    snap root (absolute, or with '..') has no place in the work directory, so it stops
    the run before the host is touched.
    """
    table = config.get("scripts") if isinstance(config, dict) else None
    if not isinstance(table, dict):
        return []

    scripts = []
    for when in ("before", "after"):
        entries = table.get(when) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, str):
                continue
            path = PurePosixPath(entry)
            if path.is_absolute() or ".." in path.parts:
                fatal(
                    f"Cannot copy script {entry} to {host}: it is outside the snap root",
                    f"Move it into the snap root (for example scripts/{path.name}) "
                    "and list that path",
                )
            if path not in scripts and (root_dir / path).is_file():
                scripts.append(path)
    return scripts


def remote_layout(base, scripts):
    """Return a work directory's subdirectories: base, then the scripts' parent dirs."""
    subdirs = list(base)
    for script in scripts:
        parent = str(script.parent)
        if parent != "." and parent not in subdirs:
            subdirs.append(parent)
    return subdirs


def remote_copies(root_dir, host, work_dir, config_src, config_name, scripts):
    """List the copies (source, dest, desc) that give a work directory what snap.py needs.

    That is snap.py, the config (config_src, copied to config_name) and the scripts at
    their paths relative to the snap root.
    """
    dest = f"{host}:{work_dir}"
    transfers = [
        (str(__script__), f"{dest}/{REMOTE_SCRIPT}", REMOTE_SCRIPT),
        (str(config_src), f"{dest}/{config_name}", config_name),
    ]
    for script in scripts:
        transfers.append((str(root_dir / script), f"{dest}/{script}", str(script)))
    return transfers


def remote_child(work_dir, command, *options, private=False):
    """Build the shell line that runs 'snap.py <command> [options]' in a host's work dir.

    python3 comes from the host's PATH (this machine's interpreter path means nothing
    there). --verbose passes through; the caller passes the command's other flags.
    private=True runs it under umask 077, so what it writes stays private to the user,
    even if it outlives an interrupted run and recreates the work directory.
    """
    argv = ["python3", REMOTE_SCRIPT, command, *(str(option) for option in options)]
    if __verbose__:
        argv.append("--verbose")
    line = f"cd {shlex.quote(str(work_dir))} && {shlex.join(argv)}"
    if private:
        line = f"umask 077 && {line}"
    return line


def restore_child_config(restore_config, table="tar"):
    """Return what restore() reads from a restore config, for a snap.py run with -t.

    That is the [tar] 'archives' key (a missing or empty one becomes [], which skips the
    archive restore just the same) and the before and after [scripts]. table is the name
    the user's config gives the [tar] table ('restore.tar' in migrate.toml); it goes in
    [snap] so the other run's messages name it the same way.
    """
    archives = restore_config.get("tar", {}).get("archives") or []
    child_config = {"tar": {"archives": archives}}
    if table != "tar":
        child_config["snap"] = {"table": table}
    if "scripts" in restore_config:
        scripts = restore_config["scripts"]
        child_config["scripts"] = {
            when: scripts[when] for when in ("before", "after") if when in scripts
        }
    return child_config


def write_config_toml(config, config_name, purpose):
    """Write a config to a temporary TOML file and return its path.

    config_name and purpose ('the remote capture') name it in the error line.
    """
    try:
        text = toml_dumps(config)
    except TypeError as e:
        fatal(f"Cannot write {config_name} for {purpose}: {e}")

    fd, tmp = tempfile.mkstemp(prefix=f"{Path(config_name).stem}-", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(tmp)


# --- Capture Logic --- #


def capture(args, root_dir, outdir, config, config_name=DEFAULT_CONFIG_CAPTURE, phase=None):
    """Create archives in specified directory.

    Display only: config_name names the config in messages; phase ('capture' in a
    migration) prefixes the script steps.
    """

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, config, "before", working_dir=outdir, phase=phase)

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

    # Create archives in parallel; a snapshot missing any of them is never saved (the
    # caller removes the staging dir)
    if not tasks:
        warn(f"{config_name}: lists no paths; the snapshot has no archives")
    created = create_archives(tasks, compress)
    if len(created) < len(tasks):
        count = f"{len(tasks) - len(created)} of {plural(len(tasks), 'archive')}"
        if __dry_run__:
            dry_run_stop(
                f"Capture would fail: {count} cannot be created; a real run saves no snapshot"
            )
        fatal(f"Capture failed: {count} could not be created; no snapshot was saved")

    # Generate snapshot TOML with checksum
    step(f"Writing {DEFAULT_SNAPSHOT_TOML}")
    toml_path, checksum = generate_snapshot_toml(
        outdir, compress, category_meta, roll_ext
    )
    if not __dry_run__:
        say(f"snapshot id: {checksum[:7]}")

    # Create checksum-named subdirectory and move files
    dst_dir = outdir.parent / outdir.name / checksum[:7]
    ext, _ = COMPRESS_MAP[compress]

    if not __dry_run__:
        dst_dir.mkdir(parents=True, exist_ok=False)

        # Move all tar archives to checksum directory
        for archive in outdir.glob(f"*{ext}"):
            archive.rename(dst_dir / archive.name)

        # Move snapshot TOML to checksum directory
        toml_path.rename(dst_dir / DEFAULT_SNAPSHOT_TOML)

    # Run after-capture scripts from the same config as the archives and before scripts
    if args.run_scripts:
        run_scripts(root_dir, config, "after", working_dir=dst_dir, phase=phase)

    return dst_dir


def capture_deploy(capture_dir, dest_host, intended_path):
    """Copy a snapshot directory, with every file in it, to intended_path on a host.

    Files that after-capture scripts wrote into the snapshot (a Brewfile) go with it.
    """
    if not (capture_dir / DEFAULT_SNAPSHOT_TOML).exists():
        fatal(f"{DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}")

    step(f"Copying snapshot to {dest_host}")

    # rsync creates only the last directory of its destination
    escaped_path = shlex.quote(str(intended_path))
    ssh_run(dest_host, f"mkdir -p {escaped_path}", desc="Creating the snapshot directory")
    rsync_copy(
        f"{capture_dir}/",
        f"{dest_host}:{intended_path}/",
        f"the snapshot to {dest_host}",
        label="snapshot",
    )


def write_capture_toml(capture_config):
    """Write capture_config to a temporary capture.toml file and return its path."""
    return write_config_toml(capture_config, DEFAULT_CONFIG_CAPTURE, "the remote capture")


def remote_snapshot_id(stdout, out_dir, host):
    """Return the snapshot id from 'ls -d <out_dir>/*/' output on a host.

    out/ holds only the snapshot the capture saved, so exactly one directory is listed.
    Its name becomes a local directory name, so it must be a plain name.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    found = [PurePosixPath(line) for line in lines if PurePosixPath(line).parent == out_dir]
    if len(found) != 1 or not re.fullmatch(r"\w[\w.-]*", found[0].name):
        fatal(
            f"Cannot find the snapshot on {host}: {out_dir} should hold one snapshot directory",
            labeled("output:", "\n".join(lines) or "(none)"),
        )
    return found[0].name


def remote_capture(args, root_dir, source_host, capture_config=None):
    """Run snap.py capture on a host, then copy the snapshot it saved into a local temp dir.

    Without capture_config the host gets the capture config from -t or the default in
    root_dir; migrate passes its [capture.*] half, which is written to a temp TOML. With
    --run-scripts the host also gets the config's scripts, and snap.py there runs them.
    Returns (the snapshot copy, named after the host's snapshot id; the temp dir to
    remove, or None in a dry run). The work directory on the host is removed however the
    capture ends.
    """
    # The config is read first, so a config problem stops before the host is touched
    if capture_config is None:
        config_name = getattr(args, "config_toml", None) or DEFAULT_CONFIG_CAPTURE
        capture_config = load_config(root_dir, config_name)
        verify_capture_config(capture_config, config_name)
        config_src = root_dir / config_name
    else:
        config_src = None  # migrate: written to a temp TOML below
    scripts = []
    if args.run_scripts:
        scripts = remote_scripts(root_dir, capture_config, source_host)

    tmp_capture_toml = None
    work_dir = None
    tmpdir = None
    done = False
    try:
        if config_src is None:
            tmp_capture_toml = write_capture_toml(capture_config)
            config_src = tmp_capture_toml

        # Set up remote working directory
        layout = remote_layout(["configs", "scripts"], scripts)
        work_dir = remote_workdir(source_host, "capture", layout)

        transfers = remote_copies(
            root_dir, source_host, work_dir, config_src, DEFAULT_CONFIG_CAPTURE, scripts
        )
        step(f"Copying {plural(len(transfers), 'file')} to {source_host}")
        success_count = rsync_parallel(transfers)
        if success_count < len(transfers):
            failed = len(transfers) - success_count
            fatal(
                f"Copying to {source_host} failed for {failed} of "
                f"{plural(len(transfers), 'file')}"
            )

        # snap.py on the host saves the snapshot in out/, and runs the scripts itself.
        # It takes as long as the archives do: no wall-clock limit
        out_dir = work_dir / "out"
        options = ["-r", work_dir, "-t", DEFAULT_CONFIG_CAPTURE, "--to", out_dir]
        if args.run_scripts:
            options.append("--run-scripts")
        step(f"Running capture on {source_host}")
        code, stdout, stderr = ssh_run(
            source_host,
            remote_child(work_dir, "capture", *options, private=True),
            check=False,
            desc="Capture",
            timeout=None,
        )

        # Show the remote capture's own output: nested under its host when it succeeded
        # (its Warnings first, so its final line comes last), or ahead of the Error line
        # when it failed
        if code == 0:
            if stdout.strip() or stderr.strip():
                say(f"output from {source_host}:")
            relay(stderr, 2, sys.stderr)
            relay(stdout, 2)
        else:
            relay(stdout, 1)
            relay(stderr, 1, sys.stderr)
        if code == 255:
            fatal(f"Capture failed on {source_host}: ssh error (exit status 255)")
        if code != 0:
            fatal(f"Capture failed on {source_host} (exit status {code})")

        # Find the snapshot in out/; a dry run shows the listing it would make
        step(f"Copying snapshot from {source_host}")
        _, stdout, _ = ssh_run(
            source_host,
            f"ls -d {shlex.quote(str(out_dir))}/*/",
            desc="Finding the snapshot",
        )
        if __dry_run__:
            snapshot_id = DRY_RUN_CHECKSUM
        else:
            snapshot_id = remote_snapshot_id(stdout, out_dir, source_host)
        tmpdir = remote_pull_dir("remote-capture")

        # Copy the whole snapshot directory, with any files the after scripts wrote
        snapshot = tmpdir / snapshot_id
        rsync_copy(
            f"{source_host}:{out_dir / snapshot_id}/",
            f"{snapshot}/",
            f"the snapshot from {source_host}",
            label="snapshot",
        )
        done = True
    finally:
        if tmp_capture_toml and tmp_capture_toml.exists():
            tmp_capture_toml.unlink()
        # A failed capture leaves no pulled copy behind
        if not done and tmpdir and not __dry_run__ and tmpdir.exists():
            shutil.rmtree(tmpdir)
        if work_dir is not None:
            remote_workdir_remove(source_host, work_dir, report=done)

    return snapshot, tmpdir if not __dry_run__ else None


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
            groups = archive_entries(tar, report=False)
    except (tarfile.TarError, ValueError):
        return True  # The restore itself reports the error
    except OSError:
        return False  # Unreadable archive, which root can read

    if roll_ext:
        # Rotating and creating <root><roll_ext> happen in its parent; each archive's
        # subdirectory is then created inside the fresh one
        if not path_writable(backup_dir(root_path, roll_ext).parent):
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
    """Check if restoring the selected archives and links needs sudo.

    Returns what needs it, for messages (an archive file name or 'symlink <path>'), or False.
    """
    for archive in selected:
        root = root_map.get(archive_category(archive))
        # Rollback restore skips categories without a root
        if roll_ext and not root:
            continue
        if not archive_writable(archive, root, roll_ext):
            return archive.name

    # create_symlinks() writes the links of the archives it restores, at most these
    tar_tables = snapshot_config.get("tar", {})
    for archive in selected:
        table = tar_tables.get(archive_category(archive), {})
        link, root = table.get("link"), table.get("root")
        if link and root:
            link_path = expand_path(link)
            if not os.path.lexists(link_path) and not path_writable(link_path.parent):
                return f"symlink {link_path}"
    return False


# Variables never passed to the sudo run (see sudo_restore)
SUDO_ENV_BLOCKED = re.compile(r"PATH|IFS|ENV|BASH_ENV|SHELLOPTS|PYTHON\w*|LD_\w*|DYLD_\w*")


def sudo_restore(args, root_dir, capture_dir, restore_config, snapshot_config, table="tar"):
    """Run this restore again under sudo, then exit with its status.

    The child restores from the local capture_dir with the same restore config, so
    remote pulls and captures aren't repeated as root and the caller's temp dirs
    still get cleaned up. Its snap root is the local root_dir (for a remote snap root,
    the local copy), so it never reads a host as root. sudo resets the environment, so
    HOME and the variables used by roots and links are passed through, or '$HOME' and
    '~' would expand to root's home.
    """
    names = {"HOME"}
    for section in snapshot_config.get("tar", {}).values():
        for key in ("root", "link"):
            names.update(re.findall(r"\$\{?(\w+)", str(section.get(key, ""))))
    # Never pass variables that change what the root run executes or loads, even if
    # snapshot.toml (which may come from another host) names them
    names = {name for name in names if not SUDO_ENV_BLOCKED.fullmatch(name)}
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.setdefault("HOME", str(Path.home()))
    env_args = [f"{name}={value}" for name, value in sorted(env.items())]

    # Pass what restore() reads from the config through a temp file (it may come
    # from migrate.toml)
    child_config = restore_child_config(restore_config, table)

    fd, config_path = tempfile.mkstemp(prefix=f"{__script__.stem}-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                f.write(toml_dumps(child_config))
            except (TypeError, AttributeError) as e:
                fatal(
                    "Cannot re-run the restore with sudo: the restore config holds an "
                    f"unsupported value: {e}"
                )

        cmd = ["sudo", "env", *env_args, sys.executable, str(__script__), "restore"]
        cmd += ["--from", str(capture_dir), "-r", str(root_dir), "-t", config_path]
        if getattr(args, "disable_rollback", False):
            cmd.append("--disable-rollback")
        if getattr(args, "run_scripts", False):
            cmd.append("--run-scripts")
        if __verbose__:
            cmd.append("--verbose")

        # The child writes to the terminal directly; print everything before it first.
        # A closed or broken stdout or stderr stops the run here, before sudo, as it
        # always has
        echo(cmd, verbose=True)
        if sys.stdout is None or sys.stderr is None:
            fatal("Cannot re-run the restore with sudo: stdout or stderr is closed")
        flush_stream(sys.stdout, strict=True)
        flush_stream(sys.stderr, strict=True)
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
    code = 128 - returncode if returncode < 0 else returncode

    # The child printed its own output and final line; add the parent's view of a
    # failure. A broken stderr drops these lines but does not change the exit code
    action = "Re-running the restore with sudo"
    try:
        if returncode == -signal.SIGINT:
            error(f"{action} was interrupted")
        elif returncode < 0:
            try:
                name = signal.Signals(-returncode).name
            except ValueError:
                name = f"signal {-returncode}"
            error(f"{action} failed (killed by {name})")
        elif returncode != 0:
            error(f"{action} failed (exit status {returncode})")
    except (OSError, ValueError):
        # Send the unwritten text to /dev/null, so Python's exit-time flush can't fail
        # and replace the child's exit code with 120
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())
        except (AttributeError, OSError, ValueError):
            pass
    sys.exit(code)


def restore_stop(args, selected, done, failed, checked):
    """Stop a restore in which archives failed, with one Error block (exit 1).

    done and failed are the restored and failed archives. checked says whether the
    snapshot has checksums, which a real run checks first and a dry run doesn't.
    """
    not_run = "not run: symlinks"
    stops = "stop before the symlinks"
    if args.run_scripts:
        not_run += ", after scripts"
        stops += ", after scripts"

    count = f"{len(failed)} of {plural(len(selected), 'archive')}"
    failed_names = ", ".join(archive.name for archive in failed)

    # A dry run only previewed the others, so it claims nothing was restored; a real
    # run restores the others first, then stops where this one does
    if __dry_run__:
        if done:
            hint = f"a real run would restore the others, then {stops}"
        else:
            hint = f"a real run would {stops}"
        if checked:
            hint = f"If the checksums match, {hint}"
        else:
            hint = hint[0].upper() + hint[1:]
        dry_run_stop(f"{count} cannot be restored: {failed_names}", hint)

    fatal(
        f"{count} failed to restore: {failed_names}",
        f"restored: {', '.join(archive.name for archive in done) or 'none'}",
        not_run,
    )


def restore(
    args, root_dir, capture_dir=None, restore_config=None, label=None, table="tar", phase=None
):
    """Restore from local capture directory.

    label names the snapshot in error lines (defaults to capture_dir); table names the
    restore config's [tar] table in messages (migrate.toml calls it [restore.tar]);
    phase ('restore' in a migration) prefixes the script steps.
    Returns (summary, success) for the command's final line.
    """
    if capture_dir is None:
        capture_dir = args.capture_dir if __dry_run__ else args.capture_dir.resolve()

    verify_snapshot(capture_dir, label)

    # Verify all archives using snapshot TOML and load config. Its Warnings print after
    # the sudo check below, so a sudo re-run doesn't print them twice
    snapshot_config = verify_archives_from_toml(capture_dir, warn_unverified=False)

    # Only a dry run gets here without the snapshot dir (a pulled snapshot, or a missing
    # one already reported); a pulled one takes the verify step's place with a Note
    snapshot_found = capture_dir.exists()
    if not snapshot_found and is_placeholder(capture_dir):
        note("A real run would verify and restore the copied snapshot")

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
        # A config written for this run by another snap.py names the user's table
        table = restore_config.get("snap", {}).get("table", table)

    # Get archives from config
    # Semantics: None/[] = skip restore, ["*"] = restore all, [patterns...] = restore matching
    # Patterns support glob matching: ["ssh*", "dotfiles", "*-config"]
    tar_table = restore_config.get("tar", {})
    archives = tar_table.get("archives", None)
    if not archives or archives == []:
        archives = None  # Empty list in config means don't restore
    elif archives == ["*"] or "*" in archives:
        archives = []  # Wildcard means restore all available

    # Match the archives snapshot.toml lists (any other archive file in the snapshot dir
    # is ignored); without the snapshot dir there is nothing to match
    selected, available, unmatched = [], [], []
    if archives is not None and snapshot_found:
        ext, _ = COMPRESS_MAP[compress]
        available = snapshot_archives(capture_dir, snapshot_config, ext)
        selected, unmatched = archive_select(available, archives)

    # Re-run with sudo if any restore target is not writable (Unix only)
    use_rollback = bool(roll_ext) and not getattr(args, "disable_rollback", False)
    can_sudo = hasattr(os, "geteuid") and os.geteuid() != 0
    needs_root = False
    if can_sudo:
        needs_root = restore_needs_sudo(
            selected, root_map, roll_ext if use_rollback else None, snapshot_config
        )
    if needs_root:
        # Symlinks are created, archives are restored
        action = "Creating" if needs_root.startswith("symlink ") else "Restoring"
        if __dry_run__:
            note(f"{action} {needs_root} needs root; a real run would re-run with sudo")
        else:
            note(f"{action} {needs_root} needs root")
            step("Re-running the restore with sudo")
            sudo_restore(
                args, root_dir, capture_dir, restore_config, snapshot_config, table
            )

    # Past the sudo re-run, which exits after the child printed these itself
    if snapshot_found and not tarball_opts.get("checksum"):
        warn(UNVERIFIED_WARNING)
    for pattern in unmatched:
        warn(f"[{table}] archives pattern '{pattern}' matches no archive in the snapshot")

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "before", working_dir=capture_dir, phase=phase)

    # Name the selection in the step header: '4 archives' or '2 of 4 archives'
    if len(selected) == len(available):
        selection = plural(len(selected), "archive")
    else:
        selection = f"{len(selected)} of {plural(len(available), 'archive')}"

    # Restore archives; done and failed collect the archives restored and failed
    done, failed = [], []
    if archives is None:
        if "archives" in tar_table:
            step_skipped("archive restore", f"[{table}] archives is empty")
        else:
            step_skipped("archive restore", f"[{table}] has no 'archives' key")
    elif not selected:
        # A dry run without the snapshot dir already printed a Note or an Error
        if snapshot_found:
            step_skipped("archive restore", f"no archive matches [{table}] archives")
    elif getattr(args, "disable_rollback", False):
        # No rollback protection - use existing extract_archives with confirmation
        step(f"Restoring {selection} without backups")
        extract_archives(
            selected, root_map=root_map, compress=compress, restored=done, failed=failed
        )
    elif not roll_ext:
        # Transactional restore needs a rollback extension; ask per archive instead
        warn(
            f"{DEFAULT_SNAPSHOT_TOML} sets no [tarball] rollback; existing files are not "
            "backed up",
            "Each archive asks before it is restored; set 'rollback' in the capture "
            "config's [tarball] table to get backups",
        )
        step(f"Restoring {selection} without backups")
        extract_archives(
            selected, root_map=root_map, compress=compress, restored=done, failed=failed
        )
    else:
        # Transactional restore with rollback support; each root's old backup directory
        # is moved aside once in this run
        step(f"Restoring {selection}")
        rotated = set()
        for archive in selected:
            category = archive_category(archive)
            root = root_map.get(category)
            if not root:
                say(archive.name)
                say(f"skip: no 'root' key in {DEFAULT_SNAPSHOT_TOML}", 2)
                continue
            if restore_category(archive, root, roll_ext, compress, rotated):
                done.append(archive)
            else:
                failed.append(archive)

    # A failed archive stops the restore before the symlinks and after scripts. Declined
    # archives are not failures
    if failed:
        restore_stop(args, selected, done, failed, checked=bool(tarball_opts.get("checksum")))
    restored = len(done)

    # Create symlinks from link -> root, only for the archives restored
    links = create_symlinks(snapshot_config, {archive_category(archive) for archive in done})

    # Run after scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "after", working_dir=capture_dir, phase=phase)

    # Summary for the final line; no ✓ when selected archives ended with none restored
    success = True
    if archives is None:
        summary = "no archives selected"
    elif restored == 0:
        summary = "no archives restored"
        success = False
    elif restored == len(selected):
        summary = f"{plural(restored, 'archive')} restored"
    else:
        summary = f"{restored} of {plural(len(selected), 'archive')} restored"
    if links:
        summary += f", {plural(links, 'symlink')} created"
    return summary, success


def remote_pull_dir(name):
    """Return a new local temp dir for a snapshot copied from a host (a stand-in in a dry run)."""
    if __dry_run__:
        return Path(DRY_RUN_DIR) / name
    return Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))


def snapshot_pull(host, path, local_dir):
    """Copy a snapshot directory on a host, with every file in it, into local_dir."""
    step(f"Copying snapshot from {host}")
    rsync_copy(f"{host}:{path}/", f"{local_dir}/", f"the snapshot from {host}", label="snapshot")


def remote_restore(args, root_dir, source_host):
    """Copy a snapshot from a host into a local temp dir and restore it here.

    The snapshot is args.capture_dir on the host. Returns restore()'s result; the temp
    dir is removed however the restore ends.
    """
    capture_dir_path = str(args.capture_dir)
    tmpdir = remote_pull_dir("remote-restore")
    try:
        snapshot_pull(source_host, capture_dir_path, tmpdir)

        # Errors name what the user asked for, not the temp copy
        label = f"{source_host}:{capture_dir_path}"
        return restore(args, root_dir, capture_dir=tmpdir, label=label)

    finally:
        if not __dry_run__:
            shutil.rmtree(tmpdir, ignore_errors=True)


def remote_relay(args, root_dir, source_host, dest_host):
    """Restore a snapshot from one host on another, through a local temp dir.

    rsync can't copy from one host to another, so the snapshot (args.capture_dir on
    source_host) is copied here first, then to dest_host. The local copy is removed
    however the restore ends.
    """
    capture_dir_path = str(args.capture_dir)
    tmpdir = remote_pull_dir("remote-restore")
    try:
        snapshot_pull(source_host, capture_dir_path, tmpdir)
        label = f"{source_host}:{capture_dir_path}"
        remote_deploy(args, root_dir, dest_host, tmpdir, label=label)
    finally:
        if not __dry_run__:
            shutil.rmtree(tmpdir, ignore_errors=True)


def remote_deploy(args, root_dir, dest_host, local_capture, deploy_config=None, label=None):
    """Copy a local snapshot to a host and restore it there with snap.py.

    snap.py on the host restores its copy as a local restore does: it re-runs itself with
    sudo only when the restore needs root, and with --run-scripts it runs the config's
    scripts. Without deploy_config the restore config comes from -t or the default in
    root_dir (a remote snap root's local copy included); migrate passes its [restore.*]
    half, written to a temp TOML. label names the snapshot in error lines. The work
    directory on dest_host is removed however the restore ends.
    """
    if label is None:
        label = local_capture

    # A dry run reports a missing snapshot dir here, where a real run stops (and passes
    # over a stand-in for one a real run would create)
    if not __dry_run__ or not local_capture.exists():
        verify_snapshot(local_capture, label)
    elif not (local_capture / DEFAULT_SNAPSHOT_TOML).exists():
        error(
            f"{DEFAULT_SNAPSHOT_TOML} not found in {label}",
            "A real run stops before creating the work directory",
        )

    # The config is read first, so a config problem stops before the host is touched
    if deploy_config is None:
        if args.config_toml:
            config_name = args.config_toml
        elif (root_dir / DEFAULT_CONFIG_DEPLOY).exists():
            config_name = DEFAULT_CONFIG_DEPLOY
        else:
            config_name = DEFAULT_CONFIG_RESTORE
        deploy_config = load_config(root_dir, config_name)
        verify_restore_config(deploy_config, config_name)
        config_src = root_dir / config_name
    else:
        config_src = None  # migrate: written to a temp TOML below
    scripts = []
    if args.run_scripts:
        scripts = remote_scripts(root_dir, deploy_config, dest_host)

    tmp_config = None
    work_dir = None
    done = False
    try:
        if config_src is None:
            tmp_config = write_config_toml(
                restore_child_config(deploy_config, "restore.tar"),
                DEFAULT_CONFIG_RESTORE,
                f"the restore on {dest_host}",
            )
            config_src = tmp_config

        layout = remote_layout(["configs", "scripts", "snapshot"], scripts)
        work_dir = remote_workdir(dest_host, "restore", layout)

        # The whole snapshot directory, with any files after-capture scripts wrote into
        # it, then snap.py, the config and the scripts; all in parallel
        transfers = remote_copies(
            root_dir, dest_host, work_dir, config_src, DEFAULT_CONFIG_RESTORE, scripts
        )
        files = plural(len(transfers), "file")
        transfers.insert(0, (f"{local_capture}/", f"{dest_host}:{work_dir}/snapshot/", "snapshot"))
        step(f"Copying the snapshot and {files} to {dest_host}")
        success_count = rsync_parallel(transfers)
        if success_count < len(transfers):
            failed = len(transfers) - success_count
            fatal(f"Copying to {dest_host} failed for {failed} of {len(transfers)} copies")

        # snap.py on the host decides whether it needs sudo, and asks for a password (or,
        # with --disable-rollback, for each archive) on the terminal. It takes as long as
        # the archives do: no wall-clock limit
        options = ["--from", work_dir / "snapshot", "-r", work_dir, "-t", DEFAULT_CONFIG_RESTORE]
        if getattr(args, "disable_rollback", False):
            options.append("--disable-rollback")
        if args.run_scripts:
            options.append("--run-scripts")
        step(f"Running restore on {dest_host}")
        code, _, _ = ssh_run(
            dest_host,
            remote_child(work_dir, "restore", *options),
            check=False,
            tty=True,
            desc="Restore",
            timeout=None,
        )
        # With ssh -t, Ctrl-C reaches snap.py on the host, which rolls back and exits
        # 130 (or dies from SIGINT); report and exit like an interrupted local restore
        if code == 130 or code == -signal.SIGINT:
            error(f"Restore on {dest_host} was interrupted")
            sys.exit(130)
        if code == 255:
            fatal(f"Restore failed on {dest_host}: ssh error (exit status 255)")
        if code != 0:
            fatal(f"Restore failed on {dest_host} (exit status {code})")
        done = True
    finally:
        if tmp_config and tmp_config.exists():
            tmp_config.unlink()
        if work_dir is not None:
            remote_workdir_remove(dest_host, work_dir, report=done)


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
        fatal(f"{config_name}: missing required [capture.tar] table")

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
        fatal(
            f"{flag} '{value}' is not a remote host",
            "Use [user@]host or [user@]host:path (a trailing colon marks a host)",
        )


def cmd_check(args):
    """Calculate and display checksums for files."""
    files = getattr(args, "files", []) or []

    if not files:
        fatal("No files given", "Usage: snap.py check [options] file [file ...]")

    ignore_invalid = getattr(args, "ignore_invalid", False)
    short_hash = getattr(args, "short_hash", False)
    full_path = getattr(args, "full_path", False)
    no_path = getattr(args, "no_path", False)

    # Calculate and display checksums; the stdout lines are data, problems go to stderr
    for file_path in files:
        path = Path(file_path)
        missing = not (path.exists() or path.is_file())
        if missing:
            if ignore_invalid:
                continue
            checksum = "null"
        else:
            try:
                checksum = calculate_file_checksum(path, show=False)
            except OSError as e:
                fatal(f"Cannot read {file_path}: {e.strerror or e}")
        if short_hash:
            checksum = checksum[:7]
        if no_path:
            print(checksum)
        elif full_path:
            print(f"{path}: {checksum}")
        else:
            print(f"{path.name}: {checksum}")
        if missing:
            warn(f"{file_path} not found")


def cmd_capture(args):
    """Execute capture command (local or remote source, local or remote destination)."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, _ = parse_remote_arg(getattr(args, "src", None), flag="--from")
    to_host, to_path = parse_remote_arg(getattr(args, "dst", None), flag="--to")
    require_remote(args, "src", from_host)

    # A remote snap root keeps its captures on its host (root_dir is only a local copy
    # of its configs and scripts)
    if root_host and not to_host and not to_path:
        to_host = root_host

    # Determine intended destination path; a host's paths are relative to its login
    # directory unless absolute
    now = datetime.now()
    year = now.strftime("%Y")
    month_day = now.strftime("%m-%d")
    if to_host and to_path:
        intended_path = normalize_remote_path(to_path)
    elif to_host:
        intended_path = remote_snap_root(args, to_host) / "captures" / year / month_day
    elif to_path:
        intended_path = to_path.resolve()
    else:
        intended_path = root_dir / "captures" / year / month_day

    banner("capture")
    if to_host:
        context(from_host or here(), f"{to_host}:{intended_path}")
    else:
        context(from_host or here(), intended_path)

    # The staging dir is removed however the capture ends
    tmpdir = None
    try:
        if from_host:
            # Run capture on remote host, pull results back
            dst_dir, tmpdir = remote_capture(args, root_dir, from_host)
        else:
            # Local capture, from -t or the default capture config
            config_name = getattr(args, "config_toml", None) or DEFAULT_CONFIG_CAPTURE
            config = load_config(root_dir, config_name)
            verify_capture_config(config, config_name)

            if __dry_run__:
                outdir = Path(DRY_RUN_DIR) / intended_path.name
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))
                outdir = tmpdir / intended_path.name
                outdir.mkdir(parents=True, exist_ok=False)

            dst_dir = capture(args, root_dir, outdir, config, config_name=config_name)

        if __dry_run__:
            final_dst = intended_path / dst_dir.name
        elif to_host:
            # Copy snapshot to remote destination
            final_dst = intended_path / dst_dir.name
            capture_deploy(dst_dir, to_host, final_dst)
        else:
            # Move snapshot to local destination
            final_dst = intended_path / dst_dir.name
            try:
                final_dst.parent.mkdir(parents=True, exist_ok=True)
            except FileExistsError:
                fatal(f"Cannot create {final_dst.parent}: a file is already there")
            except OSError as e:
                fatal(f"Cannot create {final_dst.parent}: {e.strerror or e}")

            if final_dst.exists():
                fatal(f"Cannot save the snapshot: {final_dst} already exists")

            shutil.move(str(dst_dir), str(final_dst))
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)

    if to_host:
        if __dry_run__:
            note(f"A real run would copy the snapshot to {to_host}:{intended_path}/<checksum>")
        finish(f"Snapshot saved to {to_host}:{final_dst}")
    else:
        finish(f"Snapshot saved to {final_dst}")


def cmd_migrate(args):
    """Execute migrate command: capture on source, restore on destination."""
    root_dir = args.root

    from_host, _ = parse_remote_arg(getattr(args, "src", None), flag="--from")
    to_host, _ = parse_remote_arg(getattr(args, "dst", None), flag="--to")
    require_remote(args, "src", from_host)
    require_remote(args, "dst", to_host)

    if not from_host and not to_host:
        fatal(
            "At least one of --from or --to must be a remote host",
            "Use [user@]host or [user@]host:path",
        )

    banner("migration")
    context(from_host or here(), to_host or here())

    config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_MIGRATE
    config = load_config(root_dir, config_name)
    capture_config, restore_config = split_migrate_config(config, config_name)
    verify_capture_config(capture_config, config_name, table="capture.tar")

    # The staging dir is removed however the migration ends
    tmpdir = None
    try:
        # Phase 1: Capture on source
        if from_host:
            capture_dir, tmpdir = remote_capture(args, root_dir, from_host, capture_config)
        else:
            dir_name = datetime.now().strftime("%m-%d")
            if __dry_run__:
                outdir = Path(DRY_RUN_DIR) / dir_name
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))
                outdir = tmpdir / dir_name
                outdir.mkdir(parents=True, exist_ok=False)
            capture_dir = capture(
                args, root_dir, outdir, capture_config, config_name=config_name, phase="capture"
            )

        # Phase 2: Restore on destination
        args.capture_dir = capture_dir

        if to_host:
            # With --from too, the snapshot goes through this machine: the local copy
            # remote_capture made is copied on to the destination
            remote_deploy(args, root_dir, to_host, capture_dir, deploy_config=restore_config)
            finish(f"Migration to {to_host} finished; see the restore summary above", False)
        else:
            summary, success = restore(
                args,
                root_dir,
                capture_dir=capture_dir,
                restore_config=restore_config,
                table="restore.tar",
                phase="restore",
            )
            finish(f"Migration completed: {summary}", success)
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)


def cmd_restore(args):
    """Execute restore command (local or remote)."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, from_path = parse_remote_arg(getattr(args, "src", None), flag="--from")
    to_host, _ = parse_remote_arg(getattr(args, "dst", None), flag="--to")
    require_remote(args, "dst", to_host)

    # A remote snap root restores the latest snapshot in its own captures directory,
    # unless --from names another snapshot
    if root_host and not from_host and not from_path:
        from_host = root_host

    banner("restore")
    dest = to_host or here()

    # Determine snapshot source path; latest is set when snap.py chose the snapshot
    latest = False
    if from_host:
        # Remote source: the given path (relative to the login directory unless
        # absolute), or the latest snapshot in the host's captures directory
        if from_path:
            capture_path = normalize_remote_path(from_path)
            context(f"{from_host}:{capture_path}", dest)
        else:
            captures_dir = remote_snap_root(args, from_host) / "captures"
            context(f"{from_host}:{captures_dir} (latest)", dest)
            capture_path = resolve_remote_snapshot(from_host, captures_dir)
    elif from_path:
        capture_path = from_path.resolve()
    else:
        # Local default: find latest capture
        captures_dir = root_dir / "captures"
        capture_path = resolve_snapshot_root(captures_dir)
        latest = True

    # For local paths, auto-select latest checksum subdir if path is a date dir
    if not from_host and capture_path.exists() and capture_path.is_dir():
        toml_check = capture_path / DEFAULT_SNAPSHOT_TOML
        if not toml_check.exists():
            subdirs = [d for d in capture_path.iterdir() if d.is_dir()]
            if not subdirs:
                # A --from date dir needs a snapshot inside; otherwise there is none yet
                if getattr(args, "src", None):
                    hint = (
                        "Pass --from a snapshot directory (captures/YYYY/MM-DD/<checksum>) "
                        "or a date directory that holds one"
                    )
                else:
                    hint = "Run 'snap.py capture' first, or pass --from <snapshot>"
                fatal(f"No snapshots found in {capture_path}", hint)

            capture_path = max(subdirs, key=lambda d: d.stat().st_mtime)
            latest = True

    if not from_host:
        if latest:
            context(f"{capture_path} (latest)", dest)
        else:
            context(capture_path, dest)

    # Store capture path in args for subfunctions
    args.capture_dir = capture_path

    # Case 1: Remote to remote, through a local copy
    if from_host and to_host:
        remote_relay(args, root_dir, from_host, to_host)
        finish(f"Restore on {to_host} finished; see its summary above", False)
        return
    # Case 2: Local to remote (deploy)
    if to_host:
        remote_deploy(args, root_dir, to_host, capture_path)
        finish(f"Restore on {to_host} finished; see its summary above", False)
        return

    # Case 3: Remote to local (pull and restore)
    if from_host:
        result = remote_restore(args, root_dir, from_host)
    # Case 4: Local restore
    else:
        result = restore(args, root_dir)

    # A stubbed restore may return None
    summary, success = result or ("", True)
    if summary:
        finish(f"Restore completed: {summary}", success)
    else:
        finish("Restore completed", success)


# --- Argument Parsing --- #


def add_cli_options(parser):
    """Add common CLI options (dry-run, verbose, help)."""
    group = parser.add_argument_group("output options")
    group.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without changing anything",
    )
    group.add_argument(
        "--verbose", action="store_true",
        help="Show per-path details, each copied file and the commands run",
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
        help=(
            "Snap root with configs, scripts and captures "
            f"(default: ./.snap, then {DEFAULT_ROOT_SNAP}; on a host, "
            f"{DEFAULT_REMOTE_ROOT} in its login directory)"
        ),
    )
    group.add_argument(
        "-t", "--config-toml", dest="config_toml", metavar="config",
        help="Config file, relative to the snap root, instead of the command default",
    )
    group.add_argument(
        "--run-scripts", action="store_true",
        help="Run the config's before/after scripts (on a host, snap.py there runs them)",
    )


def add_host_options(parser, from_help, to_help):
    """Add --from and --to host options."""
    group = parser.add_argument_group(
        "host options",
        "A local path that contains '@' needs a '/' (for example ./name@tag),\n"
        "or it is read as user@host",
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
        description="Capture, restore and migrate file snapshots, locally or over SSH",
    )

    subparsers = parser.add_subparsers(
        dest="command", title="commands", metavar="<command>",
    )

    # check
    check = subparsers.add_parser(
        "check", formatter_class=formatter, add_help=False,
        help="Calculate and display file checksums",
    )
    check.add_argument(
        "files", nargs="*", metavar="file",
        help="Files to checksum",
    )
    chk = check.add_argument_group("checksum options")
    chk.add_argument(
        "--ignore-invalid", action="store_true",
        help="Skip missing files instead of printing 'null'",
    )
    chk.add_argument(
        "--short-hash", action="store_true",
        help="Show only the first 7 characters of each checksum",
    )
    chk_cli = check.add_argument_group("output options")
    chk_cli.add_argument(
        "--full-path", action="store_true",
        help="Show each path as given instead of just the file name",
    )
    chk_cli.add_argument(
        "--no-path", action="store_true",
        help="Show only the checksum (overrides --full-path)",
    )
    chk_cli.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    # capture
    cap = subparsers.add_parser(
        "capture", formatter_class=formatter, add_help=False,
        help="Capture a snapshot (on this machine or a host)",
    )
    add_snap_options(cap)
    add_host_options(
        cap,
        from_help="Host to capture on (default: this machine)",
        to_help=(
            "Where to save the snapshot, a local path or host:path "
            "(default: <root>/captures/YYYY/MM-DD); a <checksum> subdirectory is created"
        ),
    )
    add_cli_options(cap)

    # restore
    rst = subparsers.add_parser(
        "restore", formatter_class=formatter, add_help=False,
        help="Restore a snapshot (on this machine or a host)",
    )
    add_snap_options(rst)
    add_host_options(
        rst,
        from_help=(
            "Snapshot, date directory or host to restore from "
            "(default: latest in <root>/captures)"
        ),
        to_help="Host to restore on (default: this machine)",
    )
    rst_group = rst.add_argument_group("restore options")
    rst_group.add_argument(
        "--disable-rollback", action="store_true",
        help="Do not back up existing files; ask before restoring each archive",
    )
    add_cli_options(rst)

    # migrate
    mig = subparsers.add_parser(
        "migrate", formatter_class=formatter, add_help=False,
        help="Capture on one machine and restore on another (at least one must be a host)",
    )
    add_snap_options(mig)
    add_host_options(
        mig,
        from_help="Host to capture on (default: this machine)",
        to_help="Host to restore on (default: this machine)",
    )
    mig_group = mig.add_argument_group("migrate options")
    mig_group.add_argument(
        "--disable-rollback", action="store_true",
        help="Do not back up existing files; ask before restoring each archive",
    )
    add_cli_options(mig)

    # Top-level help only
    parser.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    # main() reports unknown flags with the subcommand's own usage
    parser.commands = {"check": check, "capture": cap, "restore": rst, "migrate": mig}
    return parser


def main():
    parser = setup_parser()
    args, extra = parser.parse_known_args()
    if extra:
        command_parser = parser.commands.get(args.command) or parser
        command_parser.error(f"unrecognized arguments: {' '.join(extra)}")

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
    root_host, root_path = parse_remote_arg(args.root, flag="-r/--snap-root")
    args.root_host = root_host

    # Each command prints its own banner after checking its arguments
    commands = {
        "capture": cmd_capture,
        "restore": cmd_restore,
        "migrate": cmd_migrate,
    }
    if not root_host:
        args.root = resolve_root(args.root)
        commands[args.command](args)
        return

    # A remote snap root: (root_host, root_path) name it on its host, and args.root is a
    # local copy of its configs/ and scripts/ (made in a dry run too), removed at exit
    args.root_path = normalize_remote_path(root_path or DEFAULT_REMOTE_ROOT)
    args.root = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-root-"))
    try:
        root_fetch(root_host, args.root_path, args.root)
        commands[args.command](args)
    finally:
        _root_names.pop(str(args.root), None)
        shutil.rmtree(args.root, ignore_errors=True)


if __name__ == "__main__":
    main()
