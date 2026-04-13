# snap.py

Snapshot and restore utility for creating system snapshots.

## Overview

snap.py creates compressed tar archives of specified directories and files, bundles them into a snapshot with checksums, and can deploy/restore to local or remote machines via SSH.

## Requirements

- Python 3.11+
- Python packages: `tqdm` (install with `pip install tqdm`)
- rsync (for remote operations)
- SSH access (for remote operations)
- sudo (for restore operations that write to system locations)

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

## Usage

### Create Local Snapshot

```bash
snap capture
```

Creates snapshot at `~/.snap/captures/YYYY/MM-DD/CHECKSUM/`.

### Create Snapshot and Send to Remote

```bash
snap capture --to user@remote.host
```

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

### Deploy Snapshot to Remote Machine

```bash
snap restore --from ~/.snap/captures/2024/01-15 --to user@target.host
```

### Deploy from One Remote to Another

```bash
snap restore --from user@source.host --to user@target.host
```

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

### Run Scripts with Capture/Restore

```bash
snap capture --run-scripts
snap restore --run-scripts

# With custom config
snap capture --run-scripts -t production.toml
```

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

Defines which archives to restore and when to run scripts.

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

### Global Options (all subcommands)

```
-r, --snap-root [host:]root    Root directory (default: ~/.snap)
-t, --config-toml <config>     Custom TOML config file (used with --run-scripts)
--run-scripts                   Enable script execution
--dry-run                       Show what would be done without doing it
--verbose                       Show detailed progress information
--help                          Show help message
```

### Host Options (capture, restore, migrate)

```
--from [host:]path              Source host/path
--to [host:]path                Destination host/path
```

### Restore/Migrate Options

```
--disable-rollback              Disable backup/rollback of existing files
```

### Check Options

```
--ignore-invalid                Skip invalid or missing files
--short-hash                    Show 7-character hash
--full-path                     Show full file path
--no-path                       Show only the checksum
```

## Rollback Protection

By default, restore operations back up existing files before overwriting. This is controlled by the `rollback` field in `[tarball]`:

```toml
[tarball]
rollback = ".bak"    # Backs up /opt/myapp to /opt/myapp.bak before restoring
```

If restore fails mid-operation, backed-up files are automatically restored. Use `--disable-rollback` to skip this (falls back to interactive confirmation per archive).

## Symlinks

The `link` field in capture config creates symlinks during restore:

```toml
[tar.workspace]
root = "/Volumes/workplace"
link = "$HOME/workplace"    # Creates: ~/workplace -> /Volumes/workplace
dirs = ["projects"]
```

Symlinks are created after extraction. If the symlink already exists and points to the correct target, it's skipped. Errors if the path exists but points elsewhere.

## How It Works

### Capture Process

1. Reads capture.toml configuration
2. For each `[tar.*]` category, creates a compressed archive
3. Generates snapshot.toml with per-archive SHA-256 checksums
4. Moves archives into a checksum-named subdirectory
5. Optionally runs scripts and/or sends to remote host

### Restore Process

1. Verifies archive integrity using snapshot.toml checksums
2. Re-executes with sudo if needed for system paths
3. Loads restore config to determine which archives to extract
4. For each selected archive (with rollback enabled):
   - Backs up existing files
   - Extracts new files from archive
   - Rolls back on failure
5. Creates symlinks if configured
6. Runs scripts if enabled

### Deploy Process (restore --to)

1. Transfers snapshot, snap.py, config, and scripts to remote
2. Runs before-scripts on remote
3. Executes restore on remote via SSH
4. Runs after-scripts on remote
5. Cleans up remote temporary directory

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

Dry-run shows directories, archives, files, and commands that would be executed without making changes. Verbose shows individual files added to archives and rsync transfer progress.

## Security Considerations

- Archives store relative paths to prevent arbitrary file overwrites
- Python 3.12+ uses tar extraction filter to prevent path traversal attacks
- User prompted before each archive extraction (when rollback is disabled)
- Sudo required for system file restoration
- SSH commands use proper shell escaping via `shlex.quote()`
- Only specified files transferred to remote hosts

## Troubleshooting

### "Checksum verification failed"
Archive corrupted during transfer or storage. Re-create snapshot.

### "Command timed out"
Check network connectivity. Timeout constants are in snap.py.

### Permission denied during restore
The script will attempt to re-execute with sudo automatically.

### Remote deployment fails
Verify: SSH access works, python3 available on remote, sudo privileges on remote, rsync installed on both machines.

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

