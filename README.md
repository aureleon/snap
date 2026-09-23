# snap.py

Snapshot and restore utility for creating system snapshots.

## Overview

snap.py creates compressed tar archives of specified directories and files, bundles them into a snapshot with checksums, and can deploy/restore to local or remote machines via SSH.

## Requirements

- Python 3.11+
- Optional: `tqdm` for progress bars (install with `pip install tqdm`); without it snap.py
  runs the same, with no bars
- rsync (for remote operations)
- SSH access (for remote operations)
- sudo (for restore operations that write to system locations)
- On a remote host: `python3` 3.11+ on the login `PATH` and rsync (tqdm is optional
  there too), and sudo only for restores that write where the login user can't

## Installation

### Quick Install

```bash
# Run installer
./install.sh
```

The installer will:
1. Install Python and system dependencies
2. Create `~/.snap/` directory structure
3. Copy snap.py, configs, and scripts
4. Optionally install `snap` to your PATH

### Custom Installation Directory

```bash
./install.sh /path/to/custom/location
```

### Manual Install

```bash
pip install -r requirements.txt

mkdir -p ~/.snap/{configs,scripts,captures}
cp snap.py ~/.snap/
cp configs/*.toml ~/.snap/configs/
cp scripts/*.sh ~/.snap/scripts/
chmod +x ~/.snap/snap.py

# Optional: Create symlink
ln -s ~/.snap/snap.py ~/.local/bin/snap
```

## Directory Structure

```
~/.snap/                             # Default root directory
├── snap.py                          # The script
├── configs/                         # Configuration files
│   ├── capture.toml                 # Capture configuration
│   ├── restore.toml                 # Restore configuration
│   └── migrate.toml                 # Migrate configuration
├── scripts/                         # Optional scripts
│   ├── capture-brew.sh
│   └── restore-brew.sh
└── captures/                        # Snapshot storage
    └── 2024/01-15/                  # Date-based directories (YYYY/MM-DD)
        └── a1b2c3d/                 # Checksum prefix (first 7 chars)
            ├── category.tar.gz      # Compressed archive per category
            └── snapshot.toml        # Metadata with checksums
```

### Root Directory Resolution

The script searches for the `.snap` directory in this order:
1. Current working directory (`.snap/`)
2. Home directory (`~/.snap/`)
3. Exit with error if neither exists

Override with `-r/--snap-root <path>`.

With `-r [user@]host[:path]` the snap root is on a host (default `.snap` in its login
directory). snap.py copies its `configs/` and `scripts/` into a local temp directory at
startup (also in a dry run, so the dry run reads the real config) and removes the copy
at exit. Everything else runs as usual, with that host's configs and scripts:

- `capture` without `--to` saves to `host:<path>/captures/YYYY/MM-DD`.
- `restore` without `--from` restores the latest snapshot from `host:<path>/captures`
  (on this machine, or on the `--to` host).
- `--from`/`--to` still name where to capture and restore; the `-r` host is only where
  the configs, scripts and captures live. snap.py only reads its snap root, except that
  `capture` saves its snapshot in `captures/` there.

## Usage

### Create Local Snapshot

```bash
snap capture
```

Creates snapshot at `~/.snap/captures/YYYY/MM-DD/CHECKSUM/`.

### Create Snapshot and Send to Remote

```bash
snap capture --to user@remote.host          # .snap/captures/YYYY/MM-DD on the host
snap capture --to user@remote.host:~/snaps  # snaps/<checksum> in the login directory
```

The whole snapshot directory is copied, so files that after-capture scripts write into it
(for example a `Brewfile`) go with it.

### Capture on a Remote Machine

```bash
snap capture --from user@source.host                         # saved here
snap capture --from user@source.host --to user@backup.host   # saved on another host
```

snap.py runs the capture on the source host (see [Remote Hosts](#remote-hosts)) and copies
the snapshot back, named after its snapshot id. With `--to` another host, the snapshot is
copied through this machine.

### Restore Latest Snapshot

```bash
# Auto-selects most recent snapshot
snap restore
```

### Restore from Specific Path

```bash
snap restore --from ~/.snap/captures/2024/01-15
```

### Pull Snapshot from Remote and Restore

```bash
snap restore --from user@remote.host
```

Restores the host's latest snapshot: the one in `.snap/captures/YYYY/MM-DD/<checksum>`
whose `snapshot.toml` is newest. `--from user@remote.host:<path>` names a snapshot
directory instead.

### Deploy Snapshot to Remote Machine

```bash
snap restore --from ~/.snap/captures/2024/01-15 --to user@target.host
```

snap.py copies the snapshot to the target host and restores it there with snap.py (see
[Remote Hosts](#remote-hosts)).

### Deploy from One Remote to Another

```bash
snap restore --from user@source.host --to user@target.host
```

rsync can't copy from one host to another, so the snapshot is copied from the source host
into a local temp directory, then to the target host. The local copy is removed when the
restore ends, also when it fails.

### Migrate Local to Remote

```bash
snap migrate --to user@target.host
```

Captures locally, then restores on the target host.

### Migrate Remote to Local

```bash
snap migrate --from user@source.host
```

Captures on the source host, pulls the result, then restores locally.

### Migrate Remote to Remote

```bash
snap migrate --from user@source.host --to user@target.host
```

The snapshot goes through this machine, as with `restore --from A --to B`.

In a migration, `[capture.*]` goes to the capture host and `[restore.*]` to the restore
host, each written as that host's config; `[restore.tar] archives` selects the archives
there as it does locally.

### Run Scripts with Capture/Restore

```bash
snap capture --run-scripts
snap restore --run-scripts

# With custom config
snap capture --run-scripts -t production.toml
```

`-t` replaces the command's default config (`configs/capture.toml`, `configs/restore.toml`
or `configs/migrate.toml`) for everything the command reads from it: capture takes its
`[tar.*]` tables, `[tarball]` options and scripts from the `-t` file.

### Calculate File Checksums

```bash
snap check file1.tar.gz file2.tar.gz
snap check --short-hash file.tar.gz
snap check --no-path file.tar.gz
```

## Configuration Files

### capture.toml

Defines what to capture. Each `[tar.*]` section becomes a separate archive.

```toml
[tarball]
compress = "gzip"       # Compression: gzip, bzip2, xz, or "" (none)
checksum = "sha256"     # Checksum algorithm
rollback = ".bak"       # Extension for backing up existing files during restore

[tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]

[tar.dotfiles]
root = "$HOME"
files = [".*rc", ".gitconfig"]     # Glob patterns supported

[tar.config]
root = "$HOME"
dirs = [".config/*"]               # Glob: all subdirs in .config

[tar.myapp]
root = "/opt/myapp"
link = "$HOME/myapp"               # Creates symlink: ~/myapp -> /opt/myapp
dirs = ["bin", "lib", "config"]

[scripts]
# before = ["scripts/pre-capture.sh"]
after = ["scripts/capture-brew.sh"]
```

Key fields:
- `root` (required): Base directory for paths in `dirs`/`files`
- `dirs`/`files`: Paths relative to `root`, supports glob patterns
- `link` (optional): Creates a symlink pointing to `root` during restore

### restore.toml

Defines which archives to restore and when to run scripts. Names and patterns match the
`[tar.<name>]` tables in the snapshot's `snapshot.toml`; an archive file in the snapshot
directory that `snapshot.toml` doesn't list is never restored.

```toml
[tar]
# Restore specific archives (exact or glob patterns)
archives = ["ssh*", "dotfiles", "*-config"]

# Or restore all available
# archives = ["*"]

# Or skip archive restoration (scripts only)
# archives = []

[scripts]
# before = ["scripts/pre-restore.sh"]
after = ["scripts/restore-brew.sh"]
```

### migrate.toml

Uses `[capture.*]` and `[restore.*]` namespaces. Captures on the source machine, then restores on the destination. `[capture.scripts]` run on the source, `[restore.scripts]` run on the destination.

```toml
[tarball]
compress = "gzip"
checksum = "sha256"
rollback = ".bak"

[capture.tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]

[capture.tar.dotfiles]
root = "$HOME"
files = [".*rc", ".gitconfig"]

[capture.scripts]
after = ["scripts/dump-database.sh"]     # Runs on source

[restore.tar]
archives = ["*"]

[restore.scripts]
before = ["scripts/fix-permissions.sh"]  # Runs on destination
```

## Glob Patterns

### In Capture Configuration (files/dirs)

Patterns are relative to the `root` directory:

```toml
[tar.dotfiles]
root = "$HOME"
files = [".*rc"]                # Matches .bashrc, .zshrc, etc.
dirs = [".config/*"]            # All subdirs in .config

[tar.logs]
root = "/var/log"
files = ["*.log", "nginx/*.log"]
```

Standard glob syntax: `*` (any chars), `?` (single char), `[seq]` (char set), `**` (recursive).

### In Restore Configuration (archives)

```toml
archives = ["ssh*"]              # All archives starting with "ssh"
archives = ["*-config"]          # All archives ending with "-config"
archives = ["*"]                 # Restore all
archives = []                    # Skip restoration
```

## Command Line Options

### Snap and Output Options (capture, restore, migrate)

```
-r, --snap-root [[user@]host:]root  Snap root with configs, scripts and captures
                                    (default: ./.snap, then ~/.snap; on a host,
                                    .snap in its login directory)
-t, --config-toml <config>          Config file, relative to the snap root
--run-scripts                       Run the config's before/after scripts
                                    (on a host, snap.py there runs them)
--dry-run                           Show what would be done without changing anything
--verbose                           Show per-path details, each copied file and the commands run
--help                              Show help message
```

### Host Options (capture, restore, migrate)

```
--from [user@]host[:path]       capture, migrate: host to capture on (default: this machine)
                                restore: snapshot, date directory or host to restore from
                                (default: latest in <root>/captures)
--to [user@]host[:path]         capture: where to save the snapshot, a local path or host:path
                                (default: <root>/captures/YYYY/MM-DD)
                                restore, migrate: host to restore on (default: this machine)
```

`migrate` needs at least one of `--from` and `--to` to be a host.

A host is `user@host`, `host:`, or either followed by a path (`host:/path`). Anything
else is a local path; a local path containing `@` needs a `/` (e.g. `./name@tag`).
`--from` for capture/migrate and `--to` for restore/migrate must name a remote host.

A path on a host is relative to its login directory unless it is absolute (`~` and
`~/x` mean the same). A host with no path uses `.snap/captures` there, or the `-r` path's
`captures` when it is the `-r` host; the local snap root's path is never used on a host.

### Restore/Migrate Options

```
--disable-rollback              Do not back up existing files; ask before restoring each archive
```

### Check Options

```
--ignore-invalid                Skip missing files instead of printing 'null'
--short-hash                    Show only the first 7 characters of each checksum
--full-path                     Show each path as given instead of just the file name
--no-path                       Show only the checksum (overrides --full-path)
```

`check` prints one `name: checksum` line per file on stdout (`null` for a missing file),
so its output can be parsed. Problems go to stderr (`Warning: x not found`).

## Rollback Protection

By default, restore operations back up existing files before overwriting. This is controlled by the `rollback` field in `[tarball]`:

```toml
[tarball]
rollback = ".bak"    # Backs up replaced files under /opt/myapp to /opt/myapp.bak/<archive>
```

Each root gets one backup directory, `<root><rollback>`, with one subdirectory per archive
(named after the archive without its extension). For example, with root `$HOME` and
`rollback = ".bak"`, restoring `dotfiles.tar.gz` backs up `~/.zshrc` to
`/Users/me.bak/dotfiles/.zshrc`. The restore output names the directory in each archive's
header: `dotfiles.tar.gz (root: /Users/me, backup: /Users/me.bak/dotfiles)`.

At the start of each restore run, a root's existing backup directory is moved aside once, to
`<root><rollback>_<timestamp>` (with `_<n>` added when that name is taken), before the first
archive with that root is restored. Archives that share a root then write into their own
subdirectories of the fresh directory, so they never move each other's backups aside.

If an archive fails mid-restore, its backed-up files are restored automatically. The
restore tries the remaining archives, then stops with exit status 1; the symlinks and after
scripts are not run. Use `--disable-rollback` to skip backups (falls back to interactive
confirmation per archive). Without backups, an archive that can't be read or extracted also
stops the restore this way; declining a prompt does not.

## Symlinks

The `link` field in capture config creates symlinks during restore:

```toml
[tar.workspace]
root = "/Volumes/workplace"
link = "$HOME/workplace"    # Creates: ~/workplace -> /Volumes/workplace
dirs = ["projects"]
```

Symlinks are created after extraction, only for the archives restored in that run (not for
archives that were not selected or were declined at the prompt). If the symlink already
exists and points to the correct target, it's skipped. Errors if the path exists but points
elsewhere.

## How It Works

### Capture Process

1. Reads capture.toml configuration (or the `-t` config)
2. For each `[tar.*]` category, creates a compressed archive. If any archive can't be
   created, the capture stops with exit status 1 and no snapshot is saved
3. Generates snapshot.toml with per-archive SHA-256 checksums
4. Moves archives into a checksum-named subdirectory
5. Optionally runs scripts and/or sends to remote host

### Restore Process

1. Verifies archive integrity using snapshot.toml checksums
2. Loads restore config to determine which of the archives listed in snapshot.toml to extract
3. Re-executes with sudo if needed for system paths
4. For each selected archive (with rollback enabled):
   - Backs up existing files to `<root><rollback>/<archive>`
   - Extracts new files from archive
   - Rolls back on failure
5. Stops with exit status 1 if any archive failed
6. Creates symlinks for the restored archives
7. Runs scripts if enabled

### Remote Hosts

`capture --from`, `restore --to` and `migrate` run snap.py itself on the host, so the host
needs `python3` 3.11+ on its login `PATH` (this machine's interpreter path is never used
there) and rsync. Paths on a host are relative to its login directory, and can only use
letters, digits and `. _ / ~ + = , % @ -` (older rsync, including macOS's
`/usr/bin/rsync`, passes remote paths to the host's shell unescaped).

1. Creates a private work directory on the host with `mktemp -d` (under its `$TMPDIR`,
   or `/tmp`)
2. Copies what snap.py there needs into it, and nothing else:
   - `snap.py`
   - the config this command selected (`-t`; else `configs/deploy.toml` for
     `restore --to` when it exists, then the default; in a migration, the
     `[capture.*]` or `[restore.*]` half of `migrate.toml`) as `configs/capture.toml` or
     `configs/restore.toml`
   - with `--run-scripts` only, the config's scripts, at their paths in the snap root
     (`scripts/restore-brew.sh`); a script path outside the snap root stops the run
   - for a restore, the whole snapshot directory, as `snapshot/`
3. Runs snap.py there with the work directory as its snap root, passing `--verbose`,
   `--disable-rollback` and `--run-scripts` through:
   - capture: `python3 snap.py capture -r <dir> -t configs/capture.toml --to <dir>/out`,
     then copies the snapshot in `<dir>/out` back
   - restore, over `ssh -t` (so sudo and the prompts can ask on your terminal):
     `python3 snap.py restore --from <dir>/snapshot -r <dir> -t configs/restore.toml`.
     Like a local restore, it re-runs itself with sudo only when a restore target needs
     root (keeping the login user's `HOME`), and it runs the before and after scripts
     itself, as the login user or, after that re-run, as root
4. Removes the work directory, also when a step fails or on Ctrl-C

## Output

Every run prints the same shape: a `Starting <command>` line, `From:` and `To:` lines,
then one step per stage (`Creating 4 archives...`, `Restoring 4 archives...`) with its
items indented below, and one final line.

```
Starting restore
  From: /Users/u/.snap/captures/2026/09-22/cd555e5 (latest)
  To:   mymac (this machine)

Verifying 4 archives...
  ✓ 4 archives verified

Restoring 4 archives...
  dotfiles.tar.gz (root: /Users/u, backup: /Users/u.bak/dotfiles)
    replace: 1 path
    add: 1 path
  ✓ dotfiles.tar.gz restored
  ...

Creating symlinks...
  ✓ /Users/u/sysroot-link -> /sysroot

✓ Restore completed: 4 archives restored, 1 symlink created
```

- Progress, steps and results go to stdout. `Error:` and `Warning:` lines, and progress
  bars, go to stderr. Bars show only on a terminal.
- `✓` marks success. The final line has no `✓` when nothing selected was restored, or when
  an error was reported during the run (`(1 error reported above)`).
- Output from scripts and from snap.py running on a remote host is indented under the
  step that ran it. Remote restores and sudo re-runs print their own `Starting restore`
  and final lines (`Starting restore as root` when run with sudo).

## Dry-Run and Verbose Modes

```bash
# Preview operations
snap capture --dry-run
snap restore --dry-run

# Detailed progress
snap capture --verbose

# Combine both
snap capture --dry-run --verbose
```

Dry-run prints the same steps as a real run with `[DRY-RUN]` in front of every line and
makes no changes. Each command it skips is shown as `run: <command>` (ssh, rsync, bash),
paths that only a real run would create are shown by name (`<staging dir>`, `<checksum>`,
`<work dir>`), and `Note:` lines name work it cannot preview. Queries that only read a
host still run: copying a remote snap root's configs and finding the latest snapshot. It
ends with `Dry run completed; no changes were made`.

```
[DRY-RUN] Starting capture (no changes will be made)
[DRY-RUN]   From: mymac (this machine)
[DRY-RUN]   To:   user@host:.snap/captures/2026/09-22
[DRY-RUN]
[DRY-RUN] Creating 3 archives...
[DRY-RUN]   dotfiles.tar.gz (root: /Users/u)
[DRY-RUN]     include: 2 paths
[DRY-RUN]   ...
[DRY-RUN]
[DRY-RUN] Writing snapshot.toml...
[DRY-RUN]
[DRY-RUN] Note: A real run would copy the snapshot to user@host:.snap/captures/2026/09-22/<checksum>
[DRY-RUN]
[DRY-RUN] Dry run completed; no changes were made
```

Verbose only adds lines: one line per path instead of counts (`include: .zshrc`), the
archives being verified, backup rotations, the transfer list with a `✓ <file> copied` line
per file, and the ssh and sudo commands that run.

## Security Considerations

- Archives store relative paths to prevent arbitrary file overwrites
- Extraction uses tarfile's `tar` filter (Python 3.11.4+) against path traversal, and
  refuses archives with absolute or `..` member paths
- User prompted before each archive extraction (when rollback is disabled)
- Sudo required for system file restoration, on a host too; snap.py asks for it only
  when a restore target needs root
- SSH commands use proper shell escaping via `shlex.quote()`, and remote paths are
  limited to characters no remote shell treats specially
- `-r host[:path]` trusts that host fully: its configs choose which local files a capture
  sends to it, and its snapshots choose what a restore writes (with sudo, anywhere)
- The sudo re-run gets `HOME` and the variables `root`/`link` paths use, but never
  `PATH`, `IFS`, `ENV`, `BASH_ENV`, `PYTHON*`, `LD_*` or `DYLD_*`
- Only snap.py, the selected config, the snapshot and (with `--run-scripts`) the config's
  scripts are copied to a host, into a private `mktemp -d` work directory

## Troubleshooting

### "Snapshot verification failed"
Archive corrupted during transfer or storage. Capture a new snapshot, or pick another
with `--from`. `--verbose` shows the expected and actual checksums.

### "... timed out on host after 300s" or "ssh error (exit status 255)"
Check network connectivity and SSH access. Only short ssh commands (mktemp, mkdir, ls,
rm) have a wall-clock limit. The remote capture and restore, scripts and rsync copies
run as long as they need; rsync stops a copy that moves no data for 300s (its
`--timeout`), and ssh's `ServerAliveInterval` notices a dead connection. The constants
are in snap.py.

### "Note: Restoring x.tar.gz needs root"
Some restore target is not writable by you. snap.py re-runs the restore with sudo
(`Re-running the restore with sudo...`), then prints the sudo run's own output.

### Remote restore or capture fails
Verify: SSH access works, `python3 --version` on the host (over ssh) is 3.11 or later,
rsync is installed on both machines, and sudo works on the host when the restore needs
root. snap.py on the host prints its own `Error:` lines first.

### No snapshots found
Check `~/.snap/captures/` or use `--from` to specify a path directly.

## Snapshot Rotation

```bash
# Keep only last 7 date directories
cd ~/.snap/captures
ls -t | tail -n +8 | xargs rm -rf
```

## Limitations

- No incremental snapshots (always full capture)
- No encryption (use encrypted filesystem or encrypt separately)
- Remote operations require SSH and rsync

