# snap.py - Code Style & Design Guidelines

This document captures the established patterns and conventions in snap.py to maintain consistency.

## Architecture Principles

- Everything in one `snap.py` file — no external modules or package structure
- Global flags set once at runtime: `__dry_run__`, `__verbose__`
- Constants in `UPPER_SNAKE_CASE` at module level
- Related functions grouped with `# --- Section Name --- #` headers

## Naming Conventions

### Functions
- **Pattern**: `verb_noun` or `noun_verb` — e.g. `archive_create`, `run_scripts`, `remote_deploy`
- **Helpers**: `calculate_checksum`, `expand_path`, `archive_entries`
- **Commands**: `cmd_capture`, `cmd_restore`, `cmd_migrate` (CLI entry points)

### Variables
- **Constants**: `UPPER_SNAKE_CASE` — e.g. `COMPRESS_MAP`, `SSH_CONNECT_TIMEOUT`
- **Locals**: `snake_case` — e.g. `capture_dir`, `root_dir`, `dest_host`
- **Temp vars**: Short names OK in small scopes (`tar`, `f`, `e`)
- **Parameters**: Descriptive names, avoid abbreviations except standard ones (`tmpdir`)

## Code Style

### Readability
- Human readability over clever one-liners
- Keep lines under 100 characters when practical
- Use blank lines to separate logical sections within functions
- Prefer descriptive variable names over short ones
- No nested ternaries — use if/elif/else

### No Type Hints
- Target: Python 3.11+
- No `typing` imports, no annotations

### Docstrings
```python
def function_name(arg1, arg2):
    """Short description on first line."""
```
- Keep it simple — just describe what the function does
- No Args/Returns sections
- Multi-line if needed, but concise

### Error Handling
- **Fatal errors**: Print to stderr, `sys.exit(1)`
- **Non-fatal warnings**: Print to stderr, continue
- **Try/except**: Only where recovery is possible

### User Feedback
- No emojis — only `✓` for success messages
- Progress bars with `dtqdm()` helper for long operations (handles dry-run automatically)
- Verbose mode shows detailed progress with `pbar.write()`

### Dry-Run Support
- Check `__dry_run__` flag in every mutating function
- Use `printd` for all output for `[DRY-RUN] ` support
- Use `shlex.join()` to display commands that would run
- Return mock/placeholder values when needed

## Key Patterns

### Parallel Execution
- Use `ThreadPoolExecutor` with `max_workers=len(tasks)` for I/O-bound operations
- No progress bars in parallel workers (avoids terminal corruption)
- User confirmations and scripts run sequentially

### Security
- Archives store relative paths (no absolute)
- Use `shlex.quote()` for all remote shell commands
- Python 3.12+ tar extraction filter for path traversal protection
- Sudo re-execution when needed for system file restoration

### Subprocess Output
- All subprocess calls capture stdout/stderr
- Output printed after completion (not streamed) to avoid interleaving with progress bars
- Parallel operations use `pbar.write()` for thread-safety

### Temp Directory Cleanup
- Always use try/finally with `shutil.rmtree()` for temp directories

## Don'ts

1. **Don't add type hints** — keep consistent with existing code
2. **Don't add emojis** — only `✓` for success messages
3. **Don't over-engineer** — keep functions focused and simple
4. **Don't use external libraries** — only `tqdm` beyond stdlib
5. **Don't break single-file design** — no imports from other files
6. **Don't use async/await** — ThreadPoolExecutor is sufficient

## Do's

1. **Do add progress bars** for operations > 2 seconds
2. **Do support dry-run** for all mutating operations
3. **Do validate inputs** with clear error messages
4. **Do escape shell commands** with `shlex.quote`
5. **Do handle timeouts** for network operations
6. **Do clean up temp files** in finally blocks
7. **Do parallelize** independent I/O operations when beneficial
