# Configuration

[Project README](../README.md) · [Usage and options](usage.md)

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
