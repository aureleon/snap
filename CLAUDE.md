# snap.py - Code Style & Design Guidelines

This document captures the established patterns and conventions in snap.py to maintain consistency.

## Architecture Principles

### 1. **Single-File Design**
- Everything in one `snap.py` file (~1800 lines)
- No external modules or package structure
- Self-contained and portable

### 2. **Global State Pattern**
- Global flags set once at runtime: `DRY_RUN`, `VERBOSE`
- Constants in `UPPER_SNAKE_CASE` at module level
- Flags control behavior throughout without passing parameters

### 3. **Functional Organization**
- Related functions grouped with `# --- Section Name --- #` headers
- Sections: Subprocess Runners, Snap Configuration, Archive Handlers, Checksum Handlers, Capture Logic, Restore Logic, Main Execution

## Naming Conventions

### Functions
- **Pattern**: `verb_noun` or `noun_verb`
  - `archive_create`, `archive_extract`, `archive_select`
  - `run_scripts`, `load_config`, `verify_capture`
  - `remote_capture`, `remote_restore`, `remote_deploy`
  - `create_symlinks`, `restore_category`
- **Helpers**: `calculate_checksum`, `generate_snapshot_toml`, `archive_entries`, `archive_category`, `expand_path`
- **Commands**: `cmd_capture`, `cmd_restore`, `cmd_migrate` (entry points from CLI)

### Variables
- **Constants**: `UPPER_SNAKE_CASE`
  - `COMPRESS_MAP`, `SSH_CONNECT_TIMEOUT`, `MIN_RSYNC_TIMEOUT`
- **Locals**: `snake_case`
  - `capture_dir`, `root_dir`, `tarball`, `dest_host`
- **Temp vars**: Short names OK in small scopes (`tar`, `f`, `e`)

### Parameters
- Descriptive names: `capture_dir`, `dest_host`, `config_name`
- Avoid abbreviations except standard ones: `chkfile`, `tmpdir`

## Code Style

### 0. **Code Cleanliness & Readability**
- **Priority**: Human readability over clever one-liners
- **Line length**: Keep lines under 100 characters when practical
  - Break long lines at logical points
  - Use intermediate variables for clarity
  - Multi-line function calls should align arguments
- **Avoid nasty constructs**:
  ```python
  # Bad - too long and hard to read
  result = some_function(arg1, arg2, arg3) if condition else other_function(arg4, arg5) if other_condition else default_value

  # Good - clear and readable
  if condition:
      result = some_function(arg1, arg2, arg3)
  elif other_condition:
      result = other_function(arg4, arg5)
  else:
      result = default_value
  ```
- **Whitespace**: Use blank lines to separate logical sections within functions
- **Variable names**: Descriptive over short - `capture_directory` not `cap_dir`

### 1. **No Type Hints**
- Target: Python 3.11+
- No `typing` imports, no annotations
- Docstrings document types in Args/Returns sections

### 2. **Docstrings**
```python
def function_name(arg1, arg2):
    """Short description on first line."""
```
- Keep it simple - just describe what the function does
- No Args/Returns sections
- Multi-line if needed, but concise

### 3. **Error Handling**
- **Fatal errors**: Print to stderr, `sys.exit(1)`
  ```python
  if not something:
      print("Error: descriptive message", file=sys.stderr)
      sys.exit(1)
  ```
- **Non-fatal warnings**: Print to stderr, continue
  ```python
  print(f"Warning: {message}", file=sys.stderr)
  ```
- **Try/except**: Only where recovery is possible
  ```python
  try:
      subprocess.run(..., timeout=TIMEOUT)
  except subprocess.TimeoutExpired:
      print("Error: Command timed out", file=sys.stderr)
      sys.exit(1)
  ```

### 4. **User Feedback**
- Status messages without emojis by default
- Use `✓` checkmark for success (at end of operations)
- Progress bars with tqdm for long operations
- Verbose mode shows detailed progress with `tqdm.write()`

### 5. **Dry-Run Support**
- Check `DRY_RUN` flag in every mutating function
- Print `[DRY-RUN]` prefix for all dry-run output
- **Use `dtqdm()` helper**: Automatically handles dry-run mode
  ```python
  # Write loop once, works in both modes
  with dtqdm(len(items), "Processing", "item") as pbar:
      for item in items:
          process(item)
          pbar.update(1)  # No-op in DRY_RUN
  ```
  - In DRY_RUN: `pbar.update()` does nothing, no progress bar shown
  - In normal mode: Full tqdm progress bar displayed
  - `pbar.write()` works in both modes for warnings/errors
- **Show actual commands**: Use `shlex.join()` to display the full command with arguments
  ```python
  cmd = ["ssh", "-o", "ConnectTimeout=30", host, command]
  if DRY_RUN:
      print(f"[DRY-RUN] {shlex.join(cmd)}")
      return None
  ```
- Show relevant context: file sizes, timeouts, working directories
- Return mock/placeholder values when needed
- Dedicated dry-run functions for complex cases: `dry_run_create_archive`
- File listings in capture dry-run only shown in `--verbose` mode

## Progress Bar Helper

Use `dtqdm()` instead of directly using tqdm. This centralizes dry-run handling:

```python
def dtqdm(total, desc="", unit="item", **kwargs):
    """Return tqdm progress bar or no-op context in DRY_RUN mode.

    For unknown totals (total=None), starts a background thread to refresh
    the display every second so elapsed time updates even during long operations.
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
        """Wrapper that auto-refreshes progress bar when total is unknown."""
        # Starts background thread that calls pbar.refresh() every second
        # Ensures timer keeps ticking even during 30s+ file operations

    if DRY_RUN:
        return _dtqdm()

    pbar = tqdm(...)

    # Auto-refresh for unknown totals
    if total is None:
        return _AutoRefreshProgress(pbar)

    return pbar
```

**Design**:
- Nested class `_dtqdm` for dry-run no-op behavior
- `_AutoRefreshProgress` wrapper for `total=None` cases - starts background refresh thread
- No module-level pollution

**Usage**: Replace `tqdm(...)` with `dtqdm(...)`
- All tqdm kwargs supported: `leave`, `unit_scale`, `unit_divisor`, etc.
- `pbar.update(n)` works in both modes (no-op in dry-run)
- `pbar.write(msg)` prints to stdout in both modes

**leave parameter**:
- **Outer/summary bars**: `leave=True` or default - persist to show overall operation summary
  - All use custom `bar_format` to remove visual bar, show only: `N/TOTAL [elapsed, rate]`
  - `create_archives` "Overall progress" (with `autorefresh=True` for live timer)
  - `extract_archives` "Extraction progress" (with `autorefresh=True`)
  - `rsync_parallel` "Transfer progress" (with `autorefresh=True`)
- **Nested/sequential bars**: `leave=False` - show during operation, clear when done
  - `calculate_checksum` (nested under capture) - uses byte format with custom bar_format
- **No progress bars in parallel workers**: `archive_create` and `archive_extract` don't show individual bars when running in parallel to avoid terminal corruption and empty lines

**Custom bar format**: Use `bar_format='{desc}: {n}/{total} [{elapsed}, {rate_fmt}]'` to remove visual progress bar while keeping timing info. Add `autorefresh=True` for operations where individual items may take a long time.

**Subprocess output buffering**:
- All subprocess calls (`ssh_run`, `rsync_run`, `run_scripts`) capture stdout/stderr
- Output printed after subprocess completes (not during)
- Prevents output from interleaving with progress bars
- For parallel operations, output goes through `pbar.write()` for thread-safety

**Byte-based progress** (for large file operations):
```python
# Unknown total (no upfront cost) - shows rate but no ETA
# Auto-refreshes every second to show elapsed time even during long operations
with dtqdm(total=None, desc="Processing", unit="B", unit_scale=True, unit_divisor=1024) as pbar:
    for item in items:
        process(item)  # Even if this takes 30s, timer keeps updating
        pbar.update(item.size)  # Update by bytes

# Known total (calculate from metadata) - shows rate AND ETA
total_size = sum(item.size for item in items)
with dtqdm(total_size, "Processing", "B", unit_scale=True, unit_divisor=1024) as pbar:
    for item in items:
        process(item)
        pbar.update(item.size)
```

**Archive operations**:
- **Creation**: `total=None` to avoid walking entire tree upfront
  - Shows elapsed/rate, no ETA
  - Auto-refreshes every second so timer keeps running even during large file operations
- **Extraction**: Known total from `tar.getmembers()` metadata
  - Shows elapsed/rate/ETA
  - No auto-refresh needed (progress updates frequently)

## Parallel Execution Pattern

### ThreadPoolExecutor Template
```python
def parallel_operation(tasks):
    """Process multiple items in parallel."""
    if not tasks:
        return []

    print(f"Processing {len(tasks)} item(s)...")
    results = []

    with ccft.ThreadPoolExecutor(max_workers=len(tasks)) as exc:
        futures = {exc.submit(worker_func, *task): task for task in tasks}
        with tqdm(total=len(tasks), desc="Progress", unit="item") as pbar:
            for future in ccft.as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"\nError processing {task}: {e}", file=sys.stderr)
                pbar.update(1)

    print()
    return results
```
- Always print newline after tqdm progress bar
- max_workers = len(tasks) for I/O-bound operations

### Current Parallelized Operations
1. **Archive creation**: Multiple categories processed simultaneously
2. **Archive extraction**: Multiple archives extracted after user confirmation

### Sequential Operations (by design)
1. **User confirmations**: Must be sequential for interaction
2. **Scripts**: May have dependencies, run in order

## Security Patterns

### 1. **Relative Path Archives**
```python
# Store paths relative to root (not absolute)
arcname = str(path)  # path already relative to root

# Extract to root directory (not /)
try:
    tar.extract(member, root_path, filter='tar')
except TypeError:
    tar.extract(member, root_path)  # Fallback for < 3.12
```

### 2. **Shell Command Safety**
```python
# Always use shlex.quote for remote commands
escaped_path = shlex.quote(str(path))
ssh_run(host, f"mkdir -p {escaped_path}")
```

### 3. **Sudo Elevation**
```python
# Re-execute with sudo when needed
if hasattr(os, 'geteuid') and os.geteuid() != 0:
    print("Note: This script needs sudo privileges...")
    cmd = ["sudo", sys.executable] + sys.argv
    os.execvp("sudo", cmd)
```

## Remote Operations Pattern

### SSH/Rsync Structure
1. **Create remote directory** (if needed)
2. **Transfer files** (in parallel using `rsync_parallel`)
3. **Execute remote commands** via SSH
4. **Cleanup** remote temp directory

### Timeout Handling
- SSH: `SSH_CONNECT_TIMEOUT`, `SSH_ALIVE_INTERVAL`
- Rsync: Dynamic timeout based on file size
- Commands: `COMMAND_TIMEOUT` with `TIMEOUT_BUFFER`

### Subprocess Return Values
- `ssh_run()` returns: `(returncode, stdout, stderr)` tuple (or None in DRY_RUN)
- `rsync_run()` returns: `(returncode, stdout, stderr)` tuple (or None in DRY_RUN)
- Output is captured and printed after completion (not streamed)
- For TTY operations (interactive), output is not captured

## Configuration Handling

### TOML Structure
**capture.toml** (input config for creating snapshots):
- `[tarball]`: `compress`, `checksum`, `backups` (rollback extension)
- `[tar.category]`: `root` (required), `link` (optional), `dirs`, `files`
- `[scripts]`: `before`, `after`

**snapshot.toml** (generated metadata for each snapshot):
- `[tarball]`: `compress`, `checksum`, `backups`
- `[tar.category]`: `root`, `link` (if set), `checksum` (array of chunks)

**Validation**: Separate `verify_capture_config`, `verify_restore_config`
**Loading**: `load_config` with proper error messages

### Path Resolution
```python
# Environment variables expanded
root_path = Path(os.path.expandvars(root)).expanduser()

# Root directory resolution order:
# 1. Explicit --snap-root (if not default)
# 2. PWD/.snap
# 3. ~/.snap
# 4. Error
```

## CLI Design

### Argparse Structure
- Main parser with subcommands (`capture`, `restore`, `migrate`)
- Argument groups: `configuration`, `capture options`, `restore options`, `cli options`
- No `-h` short option (used for `--restore-host`)
- `--help` explicitly added to show help
- Restore options: `--disable-backups` (backups are on by default)

### Command Flow
```
main() → setup_parser() → args parsed
  → Set global flags
  → Resolve root directory
  → cmd_capture() or cmd_restore() or cmd_migrate()
```

## File Organization Conventions

### Capture Structure
```
~/.snap/captures/YYYY/MM-DD/CHECKSUM/
    category1.tar.gz     # Individual compressed archives
    category2.tar.gz
    categoryN.tar.gz
    snapshot.toml        # Metadata with per-archive checksums
```
- `CHECKSUM`: First 7 chars of snapshot.toml SHA-256 hash
- snapshot.toml contains checksums for each individual archive

### Temp Directory Pattern
```python
tmpdir = Path(tempfile.mkdtemp(prefix=f"{SCRIPT.stem}-"))
try:
    # Work in tmpdir
    pass
finally:
    shutil.rmtree(tmpdir)
```

## Compression Handling

### COMPRESS_MAP Constant
Single source of truth for compression mappings:
```python
COMPRESS_MAP = {
    "gzip":  (".tar.gz",  "w:gz"),
    "bzip2": (".tar.bz2", "w:bz2"),
    "xz":    (".tar.xz",  "w:xz"),
    "":      (".tar",     "w"),
}
```

### Usage Pattern
- **Creating archives**: Use write mode from `COMPRESS_MAP`
  ```python
  ext, mode = COMPRESS_MAP[compress]
  with tarfile.open(archive_path, mode) as tar:
      # Create archive
  ```
- **Reading archives**: Always use `'r:*'` to auto-detect compression
  ```python
  with tarfile.open(archive_path, 'r:*') as tar:
      # Read archive - handles any compression type
  ```
- **Never guess** compression type - always read from config
- Flow: `capture.toml` → `snapshot.toml` → all restore functions
- Glob specific type only: `capture_dir.glob(f"*{ext}")` not `*.tar.*`

## Restore Flow

### Transactional Restore with Rollback (Default)
When `backups` extension is specified in snapshot.toml (e.g., `.bak`):
1. Load `tarball_config` from `verify_archives_from_toml()`
2. Build `root_map`: `{category: root}` from snapshot.toml
3. For each category, call `restore_category(archive, root, backup_ext, compress)`:
   - **Per-entry interleaving**: backup → remove → extract → next entry
   - **Backup**: `shutil.copytree()` (dirs) or `shutil.copy2()` (files) to `{root}.bak`
   - **Extract**: only that entry's members from the archive
   - **Rollback on error**: delete extracted, restore from backup
4. If existing `.bak` exists, rename to `.bak_YYYYMMDD_HHMMSS` first
5. Sequential per-category (not parallel) for transactional safety
6. Call `create_symlinks(tarball_config)` to create `link` → `root` symlinks

### With --disable-backups
- Falls back to parallel extraction with interactive confirmation
- Calls `extract_archives()` with `skip_confirm=False`
- No backup copies, no rollback protection
- Still creates symlinks if `link` fields present

### If no backups extension in snapshot.toml
- Warning printed, falls back to interactive confirmation mode
- Same behavior as `--disable-backups`

### root_map
Dictionary mapping category name to extraction root:
```python
root_map = {
    "workplace": "/Volumes/workplace",
    "configs": "$HOME",
    "apps": "/Applications"
}
```
Passed through: `restore()` → `restore_category()` (transactional) or `extract_archives()` (interactive)

## Symlink Pattern

### Configuration
In `[tar.category]` section of capture.toml:
```toml
[tar.workplace]
root = "/Volumes/workplace"
link = "$HOME/workplace"  # Optional: creates symlink during restore
dirs = ["projects", "repos"]
```

### Behavior
- `link` propagated to snapshot.toml during capture
- During restore, `create_symlinks()` creates: `$HOME/workplace` → `/Volumes/workplace`
- Skips if symlink exists and points to correct target
- Errors if path exists but isn't a symlink or points elsewhere
- Environment variables expanded in both `root` and `link`
- Created after extraction, regardless of `--disable-backups`

## Testing Patterns

### Dry-Run Mode
- Test all operations without side effects
- Must work for: capture, restore, deploy, migrate
- Shows what would happen with `[DRY-RUN]` prefix

### Verbose Mode
- Shows detailed progress during operations
- Uses `tqdm.write()` to avoid corrupting progress bars
- Can combine with dry-run: `--dry-run --verbose`

## Don'ts

1. **Don't add type hints** - Keep consistent with existing code
2. **Don't add emojis** - Only `✓` for success messages
3. **Don't over-engineer** - Keep functions focused and simple
4. **Don't use external libraries** - Only `tqdm` beyond stdlib
5. **Don't break single-file design** - No imports from other files
6. **Don't use async/await** - ThreadPoolExecutor is sufficient

## Do's

1. **Do add progress bars** for operations > 2 seconds
2. **Do support dry-run** for all mutating operations
3. **Do validate inputs** with clear error messages
4. **Do escape shell commands** with `shlex.quote`
5. **Do handle timeouts** for network operations
6. **Do clean up temp files** in finally blocks
7. **Do parallelize** independent I/O operations when beneficial
