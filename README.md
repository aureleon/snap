# snap.py

Capture files in tar archives, then restore them locally or on another machine over SSH.
Each snapshot includes metadata and SHA-256 checksums.

snap.py supports per-archive encryption, backups of replaced files, and optional scripts.
Snapshots are full captures, not incremental backups.

## Requirements

- Python 3.11+ (`python3` on `PATH`). Restore requires Python 3.11.4+ for the tar extraction filter.
- SSH and rsync for remote operations.
- sudo for restore targets that need root access.
- Optional: `tqdm` for progress bars and [age](https://age-encryption.org) for encryption.

Remote hosts need Python and rsync too.

## Installation

From a checkout of this repository, run:

```bash
./install.sh
```

For an install without prompts, run:

```bash
./install.sh --yes
```

The installer creates `~/.snap/` and installs the command in `~/.local/bin` by default.
It installs rsync and optional progress bars, but keeps existing configs and scripts.
If another program uses the name `snap`, the installer names the command `snaps`.

### Custom Installation Directory

```bash
./install.sh /path/to/custom/location
```

With a custom snap root, pass `-r /path/to/custom/location` to each command.
See the [installation details](docs/usage.md#installation) for installer options and manual installation.

## Quick start

The installer adds example configs to `~/.snap/configs/`. For a small first capture,
set `~/.snap/configs/capture.toml` to:

```toml
[tarball]
compress = "gzip"
checksum = "sha256"
rollback = ".bak"

[tar.dotfiles]
root = "$HOME"
files = [".bashrc", ".gitconfig"]
```

This creates one archive named `dotfiles`. Use paths that exist on your machine.
See [configuration](docs/configuration.md) for directories, excludes, and other options.

1. Preview the capture:

   ```bash
   snap capture --dry-run
   ```

2. Create the snapshot:

   ```bash
   snap capture
   ```

Snapshots go in `~/.snap/captures/YYYY/MM-DD/<id>/`.
Each snapshot contains the archives and `snapshot.toml`.
A local `./.snap/` takes priority over `~/.snap/`. Use `-r` to select another snap root.

### Restore

**CAUTION: Restore replaces files at the roots recorded in the snapshot. Only restore snapshots that you trust.**

Set `~/.snap/configs/restore.toml` to select that archive:

```toml
[tar]
archives = ["dotfiles"]
```

1. Preview the latest snapshot:

   ```bash
   snap restore --dry-run
   ```

2. Restore the snapshot:

   ```bash
   snap restore
   ```

By default, restore moves existing files into backup directories before extraction.
See [rollback protection](docs/usage.md#rollback-protection) for backup locations and recovery behavior.

## Remote use

Use `--to` to send a capture to a host or to restore on a host:

```bash
snap capture --to user@host
snap restore --to user@host --dry-run
```

Use `--from` to capture on a host or restore a snapshot from one:

```bash
snap capture --from user@host
snap restore --from user@host --dry-run
```

Remote operations require SSH access, Python 3.11+, and rsync on the host.
See [remote usage](docs/usage.md#usage) for migration, host paths, and other examples.

## Common commands

| Task | Command |
| --- | --- |
| List snapshots and backup directories | `snap list` |
| Restore a specific snapshot | `snap restore --from /path/to/snapshot` |
| Capture on a remote host | `snap capture --from user@host` |
| Send a local capture to a remote host | `snap capture --to user@host` |
| Restore on a remote host | `snap restore --to user@host` |
| Capture locally, then restore remotely | `snap migrate --to user@host` |
| Preview deletion of older snapshots | `snap prune --keep 5 --dry-run` |
| Run the scripts in the config | `snap capture --run-scripts` |
| Show command options | `snap restore --help` |

Restore and migrate can replace files. Preview them with `--dry-run` before a real run.
Scripts run only with `--run-scripts`.

## Encryption

Set `encrypt = true` for an archive and add age recipients to the capture config.
Restore needs an age identity file. Snapshot metadata remains unencrypted.
See [encryption](docs/configuration.md#encryption) for examples and remote requirements.

## Documentation

- [Usage and options](docs/usage.md): installation details, local and remote use, flags, backups, troubleshooting, and development.
- [Configuration](docs/configuration.md): TOML examples, globs, excludes, encryption, and symlinks.
