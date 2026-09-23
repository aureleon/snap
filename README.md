# snap.py

Snapshot and restore utility for creating system snapshots.

## Overview

snap.py creates compressed tar archives of specified directories and files, bundles them into a snapshot with checksums, and can deploy/restore to local or remote machines via SSH.

## Requirements

- Python 3.11+ (as `python3` on `PATH`)
- Optional: `tqdm` for progress bars (install with `pip install tqdm`); without it snap.py
  runs the same, with no bars
- rsync (for remote operations)
- SSH access (for remote operations)
- sudo (for restore operations that write to system locations)
- Optional: the [age](https://age-encryption.org) CLI, only for archives with
  `encrypt = true` (see [Encryption](#encryption))
- On a remote host: `python3` 3.11+ on the login `PATH` and rsync (tqdm is optional
  there too), and sudo only for restores that write where the login user can't

## Installation

### Quick Install

```bash
./install.sh            # asks where to put the command
./install.sh --yes      # asks nothing: the command goes in ~/.local/bin
```

Run it from a checkout of this repository. The installer:

1. Checks for `python3` 3.11 or later, and stops if it is missing or older.
2. Installs `tqdm` with `python3 -m pip install --user` when it is not installed yet.
   tqdm is optional: when pip fails (for example on a Python that is "externally
   managed", PEP 668), the installer prints a warning and continues. It never uses
   `--break-system-packages`; install `python3-tqdm` with your package manager instead.
3. Installs the system packages: on macOS the `Brewfile` (rsync) with
   `brew bundle install --no-upgrade`, with `HOMEBREW_NO_INSTALL_UPGRADE` and
   `HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK` set, so it installs what is missing and doesn't
   upgrade what you already have;
   on Linux the commands in `packages.txt` that are not on `PATH`, with apt-get, dnf, yum,
   pacman or zypper (with sudo when you are not root). A package manager that fails stops
   the install with an `Error:` line. On macOS without Homebrew it uses the system rsync
   and prints a warning.
4. Creates the snap root `~/.snap/` with `configs/`, `scripts/` and `captures/`. New
   directories are private (`0700`); directories that already exist keep their mode.
5. Copies `snap.py` into the snap root (a re-run updates it).
6. Installs the example configs as `configs/capture.toml`, `configs/restore.toml` and
   `configs/migrate.toml`, and the example scripts as `scripts/capture-brew.sh` and
   `scripts/restore-brew.sh` (the names the configs use). It copies each one only when
   that file does not exist, so it never overwrites your configs or scripts.
7. Links the command to `snap.py`: `~/.local/bin/snap -> ~/.snap/snap.py`. It asks
   where to put it (`~/.local/bin`, `/usr/local/bin` with sudo when that is not writable,
   or nowhere), and tells you when the directory is not on your `PATH`.

Besides these and the bin directory (when it is missing), the installer creates nothing
(older versions also linked `~/Snapshots` to
`captures/`; a re-run of those created a `captures/captures` link, which this installer
reports but leaves for you to remove).

Running it again is safe: it updates `snap.py` and the link, prints
`skip: configs/capture.toml (exists)` for each of your files, and creates no new
directories or links.

Options:

```
./install.sh [options] [install_dir]

install_dir      Snap root to install into (default: ~/.snap). It must be new, empty,
                 or a snap root already (with snap.py, configs/ or captures/)
-y, --yes        Don't ask; use the defaults (the command goes in ~/.local/bin)
--name NAME      Command name (default: snap, or snaps when another snap is on PATH)
--bin-dir DIR    Directory for the command, instead of asking
-h, --help       Show the help
```

The command is named `snap` unless another program named `snap` is on your `PATH` (for
example Ubuntu's snapd) or in the bin directory: then it is named `snaps`, and the
installer says so. `--name` picks another name. The installer never replaces a file that
is not snap.py; an older install's copy of snap.py is replaced with the link.

Without a terminal (for example in a script, or with stdin from `/dev/null`) the
installer never waits for an answer: it uses the defaults, as with `--yes`, and says so.

### Custom Installation Directory

```bash
./install.sh /path/to/custom/location
```

snap.py looks for its snap root in `./.snap`, then `~/.snap`, so with another directory
pass `-r` to each command:

```bash
snap capture -r /path/to/custom/location
snap restore -r /path/to/custom/location --dry-run
```

or add a shell function to your `~/.zshrc` or `~/.bashrc` that adds it:

```bash
snap() {
    case "$1" in
        capture|restore|migrate|list|prune) command snap "$1" -r /path/to/custom/location "${@:2}" ;;
        *) command snap "$@" ;;
    esac
}
```

### Manual Install

```bash
pip install --user -r requirements.txt   # optional: progress bars

mkdir -p -m 700 ~/.snap ~/.snap/configs ~/.snap/scripts ~/.snap/captures
cp snap.py ~/.snap/
cp configs/example-capture.toml ~/.snap/configs/capture.toml
cp configs/example-restore.toml ~/.snap/configs/restore.toml
cp configs/example-migrate.toml ~/.snap/configs/migrate.toml
cp scripts/example-capture-brew.sh ~/.snap/scripts/capture-brew.sh
cp scripts/example-restore-brew.sh ~/.snap/scripts/restore-brew.sh
chmod +x ~/.snap/snap.py

# Optional: put it on PATH
ln -s ~/.snap/snap.py ~/.local/bin/snap
```

## Directory Structure

```
~/.snap/                             # Default root directory (the snap root)
├── snap.py                          # The script (~/.local/bin/snap links to it)
├── configs/                         # Configuration files
│   ├── capture.toml                 # Capture configuration
│   ├── restore.toml                 # Restore configuration
│   └── migrate.toml                 # Migrate configuration
├── scripts/                         # Optional scripts (run with --run-scripts)
│   ├── capture-brew.sh              # Saves a Brewfile in the snapshot
│   └── restore-brew.sh              # Installs the snapshot's Brewfile
└── captures/                        # Snapshot storage
    └── 2024/01-15/                  # Date-based directories (YYYY/MM-DD)
        └── a1b2c3d/                 # Checksum prefix (first 7 chars)
            ├── category.tar.gz      # Compressed archive per category
            ├── secrets.tar.gz.age   # An archive encrypted with age (encrypt = true)
            └── snapshot.toml        # Metadata with checksums
```

In this repository, `configs/example-*.toml` and `scripts/example-*.sh` are the files the
installer copies under those names; `configs/example-snapshot.toml` shows the format of
a `snapshot.toml` and is not installed.

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

### List Snapshots and Backup Directories

```bash
snap list
```

```
Snapshots in /Users/u/.snap/captures, newest first:
  2026-09-22  fad09cc  4 archives  12.3 MB  1 encrypted
  2026-09-01  d5b6730  3 archives  11.0 MB

Backup dirs, newest first per root:
  /Users/u.bak                  2.1 MB  current
  /Users/u.bak_20260922_230644  2.0 MB  2026-09-22 23:06:44

2 snapshots, 2 backup dirs
```

`list` shows every snapshot in the snap root's `captures/` (its date, snapshot id, number
of archives, size and how many of its archives are encrypted), then the backup directories
of the roots those snapshots name: the current `<root><rollback>` and the rotated
`<root><rollback>_<timestamp>` ones. It only reads.

### Prune Old Snapshots

```bash
snap prune --keep 5                      # keep the newest 5 snapshots
snap prune --older-than 30               # delete snapshots from before the last 30 days
snap prune --keep 5 --older-than 30      # delete only what both rules let go
snap prune --keep 3 --backups            # also prune rotated backup dirs
snap prune --keep 5 --dry-run            # show the plan only
```

`prune` never deletes anything without asking: it lists what it will delete, then asks
`Delete 2 snapshots and 1 backup dir? [y/N]` (`--yes` skips the question). A snapshot is
deleted only when no rule you gave keeps it: `--keep N` keeps the newest N, and
`--older-than DAYS` keeps the snapshots dated in the last DAYS days. So `--keep 5
--older-than 30` deletes snapshots older than 30 days, but always keeps the newest 5. Date
directories left empty are removed; `captures/` itself and anything that is not a
snapshot (a directory without `snapshot.toml`) are never touched.

With `--backups`, the same rules apply to each root's rotated backup directories, by the
timestamp in their name (`--keep 3` keeps each root's 3 newest). The current
`<root><rollback>` is never deleted. A directory prune can't delete without root (for
example a backup of `/etc`) is kept and reported with the command to run yourself:

```
Warning: /etc.bak_20250101_120000 needs root to delete; it is kept
  Run: sudo rm -rf /etc.bak_20250101_120000
```

`list` and `prune` work on a local snap root only; for a snap root on a host, run them on
that host.

## Configuration Files

### capture.toml

Defines what to capture. Each `[tar.*]` section becomes a separate archive.

```toml
[tarball]
compress = "gzip"       # Compression: gzip, bzip2, xz, or "" (none)
checksum = "sha256"     # Checksums are always sha256 (the only supported value)
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
exclude = ["node_modules", "*.log", "*/Cache/*"] # Left out of the archive

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
- `exclude` (optional): Patterns for paths to leave out of the archive (see
  [Excludes](#excludes))
- `encrypt` (optional): `true` encrypts the archive with age, for the recipients in the
  `[age]` table (see [Encryption](#encryption))

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

[age]
# Needed only to restore encrypted archives (see Encryption)
# identity = "~/.config/age/key.txt"
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

For encryption, `[capture.age]` holds the recipients and `[restore.age]` the identity, as
`[age]` does in `capture.toml` and `restore.toml`.

## Encryption

An archive whose `[tar.<name>]` table sets `encrypt = true` is encrypted with the
[age](https://age-encryption.org) CLI, which must be installed (`brew install age`, or
`apt install age`). The capture config's `[age]` table names who can decrypt it, with
age public keys, a file of them, or both:

```toml
[age]
recipients = ["age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"]
recipients_file = "~/.config/age/recipients.txt"   # expanded like a root

[tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]
encrypt = true
```

The archive is created in the private staging directory, encrypted there with
`age -e -r ... -R ... -o ssh-keys.tar.gz.age ssh-keys.tar.gz`, and the plaintext is
removed; only `ssh-keys.tar.gz.age` goes into the snapshot. `snapshot.toml` marks the
archive with `encrypted = true`, and its checksum is over the `.age` file, so a restore
verifies it before decrypting. A category with `encrypt` but no recipients is a config
error, and so is a missing `age` (the capture stops before creating any archive).

To restore it, the restore config's `[age]` table (in `migrate.toml`, `[restore.age]`)
names your identity (private key) file:

```toml
[age]
identity = "~/.config/age/key.txt"
```

The selected encrypted archives are decrypted with `age -d -i <identity>` into a new
private (`0700`) temp directory, which is always removed when the restore ends. The
restore then reads the plaintext copies like any other archive; in output they are named
without `.age` (`ssh-keys.tar.gz`). Selection and globs in `archives` use the archive's
name as usual. An encrypted archive that is not selected needs no identity. A dry run
shows the `age` commands (`run: age -d ...`) and never decrypts, so it can't list an
encrypted archive's paths.

On hosts: the host that runs the capture (`capture --from`, `migrate --from`) needs `age`,
and reads `recipients_file` there. The host that runs the restore (`restore --to`,
`migrate --to`) needs `age` and the identity file at the configured path on that host;
snap.py sends only that path, never a key. When a restore re-runs itself with sudo, `age`
must be on root's `PATH` too.

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

### Excludes

A `[tar.<name>]` table's `exclude` key lists fnmatch patterns for paths to leave out. A
pattern excludes a path when it matches the path relative to `root`, or any single part
of it. An excluded directory is left out with everything below it.

```toml
[tar.projects]
root = "$HOME"
dirs = ["src"]
exclude = [
    "node_modules",    # anything named node_modules, at any depth
    "*.log",           # any name ending in .log, at any depth
    "src/build/*",     # everything below src/build (the empty dir is kept)
    "src/tmp",         # this one path
]
```

In fnmatch patterns `*` also matches `/`, so `src/build/*` matches `src/build/a/b` too.
`--verbose` lists each excluded path with the pattern that matched it
(`exclude: src/app/debug.log (pattern '*.log')`), in a dry run too.

On macOS, captures also leave out the metadata files `.DS_Store` and `._*` (AppleDouble
files) at any depth.

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

### List and Prune Options

```
-r, --snap-root <root>          Local snap root (default: ./.snap, then ~/.snap)
--keep N                        prune: keep the newest N snapshots (with --backups, also
                                the newest N rotated backup dirs of each root)
--older-than DAYS               prune: keep only what is from the last DAYS days
--backups                       prune: also delete rotated backup dirs
--yes                           prune: delete without asking
--dry-run, --verbose            prune: show the plan only; list every kept path
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

A backup is a move (a rename), not a copy, so the backed-up files keep every attribute
exactly: permissions, ownership, timestamps, xattrs, ACLs and hardlinks. A rollback moves
them back. Only when the backup directory is on another filesystem than a path (a mount
point below the root) is the path copied instead, keeping its mode, timestamps, symlinks,
ownership (as root) and, on Linux, xattrs; the original is then removed. That copy is not
exact: on macOS it drops xattrs, ACLs and file flags, and hardlinks inside the path become
separate files, so a rollback from it brings back that copy. Keep the backup directory on
the same filesystem as what you restore (or use `--disable-rollback`) where that matters.

Archives themselves store mode, ownership and timestamps, but not xattrs or ACLs, so
restored files come back without them. A move needs write
access to the path's parent directory (and to a directory itself), not read access to its
contents, so snap.py re-runs the restore with sudo only when those, or a copy, need root.

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

Snapshots are private to your user: the snapshot directory, and any `captures/YYYY` and
`MM-DD` directories a capture creates, are `0700`, and the archives, `snapshot.toml` and
any other file in the snapshot (such as a Brewfile an after script writes) are `0600`.
Directories that already exist, such as your `captures/`, keep their modes. Copies to a
host keep these modes, and the directories created there are created under `umask 077`.
Restored files get the modes stored in the archive.

### Restore Process

1. Verifies archive integrity using snapshot.toml checksums
2. Loads restore config to determine which of the archives listed in snapshot.toml to extract
3. Re-executes with sudo if needed for system paths
4. For each selected archive (with rollback enabled):
   - Moves existing files to `<root><rollback>/<archive>`
   - Extracts new files from archive
   - Rolls back on failure
5. Stops with exit status 1 if any archive failed
6. Creates symlinks for the restored archives
7. Runs scripts if enabled

### Remote Hosts

`capture --from`, `restore --to` and `migrate` run snap.py itself on the host, so the host
needs `python3` 3.11+ on its login `PATH` (this machine's interpreter path is never used
there) and rsync (and `age` for encrypted archives, see [Encryption](#encryption)). Paths on a host are relative to its login directory, and can only use
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
`<work dir>`, `<decrypted dir>`), and `Note:` lines name work it cannot preview. Queries that only read a
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
excluded paths (`exclude: ...`), the archives being verified, backup rotations, the transfer list with a `✓ <file> copied` line
per file, and the ssh and sudo commands that run.

## Security Considerations

- Archives store relative paths to prevent arbitrary file overwrites
- Extraction uses tarfile's `tar` filter (Python 3.11.4+) against path traversal, and
  refuses archives with absolute or `..` member paths
- User prompted before each archive extraction (when rollback is disabled)
- Sudo required for system file restoration, on a host too; snap.py asks for it only
  when a restore target needs root, and the root run starts Python with `-s` (no user
  site-packages)
- Snapshots and the directories a capture creates for them are private to your user
  (`0700` directories, `0600` files)
- Archives with `encrypt = true` are encrypted with age before they leave the private
  staging directory; restores decrypt them into a private temp directory that is always
  removed, and an identity (private key) is never copied to a host
- `prune` asks before deleting (unless `--yes`), never follows symlinks out of
  `captures/`, never deletes a current backup directory, and never uses sudo
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

### "Error: brew bundle install failed" (or apt-get, dnf, ...) during install
The package manager could not install rsync. Its last lines of output are shown above
the error. Fix that (or install rsync yourself), then run `./install.sh` again; a re-run
keeps everything that was already installed.

### `snap` runs another program
Ubuntu's snapd is also `snap`. The installer names the command `snaps` when it finds
another `snap`; run `./install.sh --name NAME` for another name.

## Development

Everything is checked locally (there is no hosted CI):

```bash
./check.sh          # ruff, shellcheck and all tests
./check.sh --fast   # the same without the slow end-to-end tests (a few seconds)
./check.sh --fast -k exclude   # other arguments go to pytest
```

`check.sh` runs `ruff check snap.py tests/`, `shellcheck install.sh check.sh
scripts/*.sh` and `python3 -m pytest`, runs all three even when one fails, and exits 1
when any of them failed. Their settings are in `pyproject.toml` (ruff: line length 100,
rules F, E, W and B; pytest: `testpaths`, `-p no:cacheprovider` and the `slow` marker).
It needs ruff, shellcheck and pytest (`brew install ruff shellcheck`,
`python3 -m pip install --user pytest`).

Tests marked `slow` run snap.py or install.sh in a subprocess: every test that uses the
`env` fixture in `tests/test_snap.py` (marked in `tests/conftest.py`) and all of
`tests/test_install.py`. They run in temp directories with a temp `HOME` and fake `sudo`,
`ssh`, `rsync`, `age`, `brew`, `apt-get` and `pip` (a `python3` shim), so they never use
real sudo, never contact a host and never install anything. See `AGENTS.md` for how the
code is organized.

## Snapshot Rotation

Use [`snap prune`](#prune-old-snapshots), for example `snap prune --keep 7 --backups`.

## Limitations

- No incremental snapshots (always full capture)
- Encryption is per archive (`encrypt = true`); `snapshot.toml` itself, with the roots and
  links, is not encrypted
- Remote operations require SSH and rsync

