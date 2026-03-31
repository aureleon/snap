# snap.py

Backup and restore utility for creating system snapshots.

## Overview

snap.py creates compressed tar archives of specified directories and files, bundles them into a single backup tarball with checksums, and can deploy/restore to local or remote machines via SSH.

## Requirements

- Python 3.11+
- Python packages: `tqdm` (install with `pip install tqdm`)
- rsync (for remote operations)
- SSH access (for remote operations)
- sudo (for restore operations that write to system locations)

## Installation

### Quick Install

```bash
# Install Python dependencies
pip install -r requirements.txt

# Run installer
./install.sh
```

The installer will:
1. Create `~/.snap/` directory structure
2. Copy snap.py and configuration files
3. Optionally create a `snap` symlink in your PATH

### Manual Install

```bash
# Install Python dependencies
pip install -r requirements.txt

# Create directory structure
mkdir -p ~/.snap/{configs,scripts,backups}

# Copy files
cp snap.py ~/.snap/
cp examples/*.toml ~/.snap/configs/
cp examples/*.sh ~/.snap/scripts/
chmod +x ~/.snap/snap.py

# Optional: Create symlink
ln -s ~/.snap/snap.py ~/.local/bin/snap
```

### Custom Installation Directory

```bash
./install.sh /path/to/custom/location
```

The installer automatically updates the default root path in snap.py, so you won't need to pass `-m` each time. You can also create a `.snap/` directory in your project to use that as the root.

## Directory Structure

```
~/.snap/                         # Default root directory
├── snap.py                      # The script
├── configs/                        # Configuration files
│   ├── backup.toml                 # Backup configuration
│   ├── restore.toml                # Restore configuration
│   └── deploy.toml                 # Deploy configuration
├── backups/2024/01-15/             # Backup storage (backups/YYYY/MM-DD)
│   └── a1b2c3d/                    # Checksum prefix (first 7 chars)
│       ├── backup.tar              # Backup archive
│       └── backup.tar.sha256       # Checksum file
└── scripts/                        # Optional scripts directory
    ├── before-backup.sh
    ├── after-backup.sh
    ├── before-restore.sh
    └── after-restore.sh
```

### Root Directory Resolution

The script searches for the `.snap` directory in this order:
1. Current working directory (`.snap/`)
2. Home directory (`~/.snap/`)
3. Exit with error if neither exists

You can override with `-m/--snap-root <path>`.

The checksum subdirectory allows multiple backups per day and provides a unique identifier for each backup.

To find your latest backup:
```bash
ls -t ~/.snap/backups/*/*/  # List all backups
ls ~/.snap/backups/2024/01-15/  # List checksums for specific date
# Use the checksum directory name with -c flag
```

## Usage

### Create Local Backup

```bash
./snap.py backup
```

Creates backup at `~/.snap/backups/YYYY/MM-DD/`

### Create Backup and Send to Remote

```bash
./snap.py backup --host user@remote.host
```

### Restore from Local Backup

```bash
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d
```

### Pull Backup from Remote and Restore

```bash
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -s user@remote.host
```

### Deploy Backup to Remote Machine

```bash
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -h user@target.host
```

### Deploy from One Remote to Another

```bash
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -s user@source.host -h user@target.host
```

### Restore Latest Backup (Auto-Select)

```bash
# Automatically selects most recent backup in date directory
./snap.py restore -b ~/.snap/backups/2024/01-15
```

### Run Scripts with Backup/Restore

```bash
# Run scripts with default config
./snap.py backup --run-scripts
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d --run-scripts

# Run scripts with custom config (--run-scripts is required)
./snap.py backup --run-scripts -t production.toml
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d --run-scripts -t production-restore.toml
```

## Configuration Files

### backup.toml

Defines what to backup. Each category becomes a separate archive.

Supports glob patterns in `files` and `dirs` arrays.

```toml
[tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]

[tar.dotfiles]
root = "$HOME"
files = [".*rc", ".vimrc", ".gitconfig"]  # Glob: matches .bashrc, .zshrc, etc.

[tar.config]
root = "$HOME"
dirs = [".config/*"]  # Glob: matches all subdirs in .config

[tar.homebrew]
root = "/usr/local"
dirs = ["Cellar", "Caskroom"]
files = ["bin/brew"]

[tar.application]
root = "/opt"
dirs = ["myapp"]

[tar.logs]
root = "/var/log"
files = ["*.log", "app-*.log"]  # Glob: matches all .log files

[scripts]
after = ["scripts/cleanup.sh"]
```

### restore.toml

Defines which archives to restore and when to run scripts.

```toml
[tar]
# Restore specific archives
archives = ["ssh-keys", "dotfiles"]

# Or use glob patterns
# archives = ["ssh*", "dot*", "*-config"]

# Or restore all available
# archives = ["*"]

# Or skip archive restoration
# archives = []

[scripts]
before = ["scripts/pre-restore.sh"]
after = ["scripts/post-restore.sh"]
```

### deploy.toml

Similar to restore.toml but for remote deployment.

```toml
[tar]
archives = ["application", "nginx-config"]

[scripts]
before = ["scripts/stop-services.sh"]
after = ["scripts/start-services.sh"]
```

## Glob Patterns

### In Backup Configuration (files/dirs)

Use glob patterns in `files` and `dirs` to match multiple paths:

```toml
[tar.dotfiles]
root = "$HOME"
files = [".*rc"]                # Matches .bashrc, .zshrc, .vimrc, etc.
dirs = [".config/*"]            # Matches all subdirs in .config

[tar.logs]
root = "/var/log"
files = ["*.log", "nginx/*.log"] # All .log files, recursively in nginx/
```

Patterns are relative to the `root` directory. Standard glob syntax:
- `*` - matches any characters
- `?` - matches single character
- `[seq]` - matches any character in seq
- `**` - recursive directory match (e.g., `**/*.log`)

Literal paths (without glob characters) work as before.

### In Restore Configuration (archives)

Select which archives to restore using patterns:

```toml
# Exact match
archives = ["ssh-keys", "dotfiles"]

# Glob patterns
archives = ["ssh*"]              # All archives starting with "ssh"
archives = ["*-config"]          # All archives ending with "-config"
archives = ["app-*", "dotfiles"] # Multiple patterns

# Special values
archives = ["*"]                 # Restore all available archives
archives = []                    # Skip archive restoration (scripts only)
```

## Command Line Options

### Global Options

```
-m, --snap-root <root>       Root directory (default: ~/.snap)
-b, --backup-path <dir>      Backup directory path
--run-scripts                Enable script execution (required to run scripts)
-t, --config-toml <config>   Specify custom TOML config (used with --run-scripts)
--dry-run                    Show what would be done without doing it
--verbose                    Show detailed progress information
--help                       Show help message and exit
```

### Backup Options

```
--host <host>                Destination host to copy backup to (user@hostname)
```

### Restore Options

```
-c, --checksum <hash>        Backup checksum prefix (first 7+ chars, auto-selects latest if omitted)
-s, --source-host <host>     Source host to pull from (user@hostname)
-h, --restore-host <host>    Destination host to restore on (user@hostname)
```

### Backup Options

```
--host <host>                Send backup to remote host (user@hostname)
```

### Restore Options

```
--source <host>              Pull backup from remote host
--host <host>                Deploy backup to remote host
```

## Example Scripts

Scripts run in the backup directory with sudo privileges (on remote if applicable). Place scripts in the `scripts/` directory.

### Backup Scripts

#### scripts/dump-database.sh

```bash
#!/bin/bash
set -e

echo "Dumping PostgreSQL database..."
pg_dump mydb > /tmp/mydb.sql

echo "Dumping MySQL database..."
mysqldump -u root mydb > /tmp/mydb-mysql.sql
```

#### scripts/stop-services.sh

```bash
#!/bin/bash
set -e

echo "Stopping application services..."
sudo systemctl stop myapp
sudo systemctl stop nginx
```

#### scripts/cleanup-temp.sh

```bash
#!/bin/bash
set -e

echo "Cleaning up temporary files..."
rm -f /tmp/*.sql
rm -rf /tmp/backup-temp
```

### Restore Scripts

#### scripts/backup-existing.sh

```bash
#!/bin/bash
set -e

BACKUP_DATE=$(date +%Y%m%d-%H%M%S)

echo "Backing up existing configuration..."
if [ -d /etc/myapp ]; then
    sudo cp -r /etc/myapp /etc/myapp.backup.$BACKUP_DATE
fi

if [ -d /opt/myapp ]; then
    sudo cp -r /opt/myapp /opt/myapp.backup.$BACKUP_DATE
fi
```

#### scripts/fix-permissions.sh

```bash
#!/bin/bash
set -e

echo "Setting ownership and permissions..."

# Application directories
sudo chown -R myapp:myapp /opt/myapp
sudo chmod -R 755 /opt/myapp

# Configuration files
sudo chown root:root /etc/myapp/*
sudo chmod 644 /etc/myapp/*.conf
sudo chmod 600 /etc/myapp/*.key

# Log directories
sudo mkdir -p /var/log/myapp
sudo chown myapp:myapp /var/log/myapp
sudo chmod 755 /var/log/myapp
```

#### scripts/reload-configs.sh

```bash
#!/bin/bash
set -e

echo "Reloading system configurations..."

# Reload systemd
sudo systemctl daemon-reload

# Reload nginx
if systemctl is-active --quiet nginx; then
    sudo nginx -t && sudo systemctl reload nginx
fi

# Update firewall rules
sudo ufw reload 2>/dev/null || true
```

#### scripts/start-services.sh

```bash
#!/bin/bash
set -e

echo "Starting services..."

# Start in dependency order
sudo systemctl start postgresql
sudo systemctl start redis
sudo systemctl start myapp
sudo systemctl start nginx

# Enable services on boot
sudo systemctl enable myapp
sudo systemctl enable nginx
```

#### scripts/health-check.sh

```bash
#!/bin/bash
set -e

echo "Running health checks..."

# Check if services are running
for service in myapp nginx postgresql; do
    if ! systemctl is-active --quiet $service; then
        echo "ERROR: $service is not running"
        exit 1
    fi
    echo "OK: $service is running"
done

# Check HTTP endpoint
if command -v curl &> /dev/null; then
    if curl -f -s http://localhost:8080/health > /dev/null; then
        echo "OK: Application health check passed"
    else
        echo "ERROR: Application health check failed"
        exit 1
    fi
fi

# Check database connection
if command -v psql &> /dev/null; then
    if psql -U myapp -d mydb -c "SELECT 1" > /dev/null 2>&1; then
        echo "OK: Database connection successful"
    else
        echo "ERROR: Database connection failed"
        exit 1
    fi
fi

echo "All health checks passed"
```

### Script Best Practices

#### Error Handling

Always use `set -e` to exit on errors:

```bash
#!/bin/bash
set -e  # Exit on any error
```

#### Conditional Execution

Check before acting:

```bash
if [ -f /etc/myapp/config ]; then
    sudo cp /etc/myapp/config /etc/myapp/config.bak
fi

if systemctl is-active --quiet nginx; then
    sudo systemctl reload nginx
fi
```

#### Logging

Add timestamps to output:

```bash
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

log "Starting backup process..."
```

#### Idempotency

Make scripts safe to run multiple times:

```bash
# Create directory only if doesn't exist
mkdir -p /opt/myapp

# Add user only if doesn't exist
id -u myapp > /dev/null 2>&1 || useradd -r myapp

# Enable service (safe to run multiple times)
systemctl enable myapp
```

## How It Works

### Backup Process

1. Reads backup.toml configuration
2. For each category, creates compressed tar archive (category.tar.gz)
3. Bundles all archives into single backup.tar
4. Generates SHA-256 checksum
5. Optionally runs after-scripts
6. Optionally copies to remote host

Archives store relative paths (leading / stripped) for portability.

### Restore Process

1. Copies backup.tar and checksum from source (local or remote)
2. Verifies checksum integrity
3. Re-executes with sudo if needed
4. Loads restore configuration
5. Optionally runs before-scripts
6. Extracts backup.tar to temporary directory
7. For each selected archive:
   - Shows destination paths
   - Prompts for confirmation
   - Extracts to root filesystem
8. Optionally runs after-scripts

### Deploy Process

1. Copies backup files, snap.py, config, and scripts to remote
2. Runs before-scripts on remote
3. Executes restore on remote via SSH
4. Runs after-scripts on remote
5. Cleans up remote temporary directory

## Security Considerations

- Archives store relative paths to prevent arbitrary file overwrites
- Python 3.12+ uses tar extraction filter to prevent path traversal attacks
- User prompted before each archive extraction
- Sudo required for system file restoration
- SSH commands use proper shell escaping
- Only specified files transferred to remote hosts

## Dry-Run and Verbose Modes

### Dry-Run Mode

Preview operations without making changes:

```bash
# Preview backup creation
./snap.py backup -n

# Preview restore
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -n

# Preview deployment
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -h user@remote -n
```

Dry-run mode shows:
- Directories that would be created
- Archives that would be created and their contents
- Files that would be transferred
- Commands that would run (local and remote)
- Archives that would be extracted

No actual operations are performed.

### Verbose Mode

Show detailed progress information:

```bash
# Verbose backup (shows each file added to archives)
./snap.py backup -v

# Verbose restore (shows rsync progress, extraction details)
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -v
```

Verbose mode shows:
- Each file added to archives during backup
- rsync transfer progress with bandwidth and ETA
- Individual files being extracted during restore

Combine with dry-run to see exactly what would happen:

```bash
./snap.py backup -nv
```

## Tips

### Custom Backup Location

```bash
# Creates /mnt/external/backup-2024-01-15/CHECKSUM/
./snap.py backup -b /mnt/external/backup-2024-01-15
```

### Using Different Configs

```bash
./snap.py backup --run-scripts -t production.toml
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d --run-scripts -t production-restore.toml
```

### Finding the Checksum

After creating a backup, the checksum directory is shown in the output:
```bash
./snap.py backup
# Output: ✓ Backup complete: /Users/you/.snap/backups/2024/01-15/a1b2c3d

# Use the checksum (a1b2c3d) with -c flag:
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d
```

### Testing Before Executing

Use dry-run mode to preview operations:

```bash
# Test backup creation
./snap.py backup -n

# Test restore
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -n

# Test deployment to remote
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -h user@remote -n
```

Or manually verify archives:

1. Extract backup.tar manually
2. Extract individual archives to temporary location
3. Verify contents before actual restore

### Backup Rotation

```bash
# Keep only last 7 date directories (may contain multiple backups each)
cd ~/.snap/backups
ls -t | tail -n +8 | xargs rm -rf

# Keep only one backup per date (remove older checksums)
for date in ~/.snap/backups/*/*/; do
  cd "$date"
  ls -t | tail -n +2 | xargs rm -rf
done
```

### Incremental Backups

Not supported. Each backup is full snapshot. Use rsync or other tools for incremental backups.

## Troubleshooting

### "Checksum verification failed"

Archive corrupted during transfer or storage. Re-create backup.

### "Command timed out"

Increase timeout constants in snap.py or check network connectivity.

### Archives not extracting to expected location

Ensure backup was created with snap.py (relative paths). Archives from other tools may have different path structure.

### Permission denied during restore

Run restore command with sudo or let script re-execute itself with sudo.

### Remote deployment fails

Verify:
- SSH access works (ssh user@host)
- python3 available on remote
- sudo privileges on remote
- rsync installed on both machines

## Examples

### Backup SSH Keys and Dotfiles

backup.toml:
```toml
[tar.ssh]
root = "$HOME"
dirs = [".ssh"]

[tar.dots]
root = "$HOME"
files = [".*rc", ".vim*", ".gitconfig"]  # Uses glob patterns
```

```bash
./snap.py backup
```

### Backup All Configuration Directories

backup.toml:
```toml
[tar.config]
root = "$HOME"
dirs = [".config/*"]  # All subdirectories in .config

[tar.local]
root = "$HOME"
dirs = [".local/share/*"]  # All subdirectories in .local/share
```

```bash
./snap.py backup
```

### Selective Restore

restore.toml:
```toml
[tar]
archives = ["ssh"]  # Only restore SSH keys
```

```bash
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d
```

### Application Deployment

deploy.toml:
```toml
[tar]
archives = ["application", "nginx-config", "systemd-units"]

[scripts]
before = ["scripts/stop-app.sh"]
after = ["scripts/start-app.sh", "scripts/reload-nginx.sh"]
```

```bash
./snap.py restore -b ~/.snap/backups/2024/01-15 -c a1b2c3d -h user@production.server
```

### Database Migration

backup.toml:
```toml
[tar.database]
root = "/var/lib"
dirs = ["postgresql/data"]

[scripts]
before = ["scripts/stop-postgres.sh"]
after = ["scripts/start-postgres.sh"]
```

deploy.toml:
```toml
[tar]
archives = ["database"]

[scripts]
before = ["scripts/stop-postgres.sh"]
after = ["scripts/fix-permissions.sh", "scripts/start-postgres.sh"]
```

## Limitations

- No incremental backups (always full snapshot)
- No encryption (use encrypted filesystem or encrypt backup.tar separately)
- No compression of final backup.tar (individual archives are gzipped)
- Remote operations require SSH and rsync

## License

See project license file.

