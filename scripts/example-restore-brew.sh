#!/bin/bash
# Restore Homebrew packages from backup
# This script expects to run from the backup directory

set -euo pipefail

BREWFILE="$PWD/Brewfile"

echo "Restoring Homebrew packages ..."

brew bundle --file="$BREWFILE"

# Remove quarantine attributes on macOS
if [[ "$(uname)" == "Darwin" ]] && [[ -d /opt/homebrew/bin ]]; then
    find /opt/homebrew/bin -type f -exec xattr -d com.apple.quarantine "{}" \; 2>/dev/null || true
fi

echo "✓ Homebrew packages restored"

