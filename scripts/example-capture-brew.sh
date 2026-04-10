#!/bin/bash
# Backup Homebrew packages to text files
# This script expects to run from the backup directory

set -euo pipefail

if ! command -v brew &>/dev/null; then
    echo "Warning: Homebrew not found, skipping brew backup" >&2
    exit 0
fi

echo "Backing up Homebrew packages..."

# Save to current directory (the backup directory)
brew bundle dump

echo "✓ Homebrew backup saved to Brewfile"

