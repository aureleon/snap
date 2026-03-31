#!/bin/bash
# Backup Homebrew packages to text files
# This script expects to run with the backup directory as PWD

set -euo pipefail

if ! command -v brew &>/dev/null; then
    echo "Warning: Homebrew not found, skipping brew backup" >&2
    exit 0
fi

echo "Backing up Homebrew packages..."

# Save to current directory (the backup directory)
brew list --formulae > brew-formulae.txt
brew list --casks > brew-casks.txt

echo "  Saved to $PWD/brew-formulae.txt and $PWD/brew-casks.txt"
echo "✓ Homebrew backup complete"
