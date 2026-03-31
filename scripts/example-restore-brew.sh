#!/bin/bash
# Restore Homebrew packages from backup
# This script expects to run with the backup directory as PWD

set -euo pipefail

FORMULAE_FILE="brew-formulae.txt"
CASKS_FILE="brew-casks.txt"

# Install Homebrew if not present
if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

echo "Restoring Homebrew packages from $PWD..."

# Install casks
if [[ -f "$CASKS_FILE" ]]; then
    while IFS= read -r cask; do
        [[ -z "$cask" ]] && continue
        echo "  Installing cask: $cask"
        brew install --cask "$cask" || true
    done < "$CASKS_FILE"
fi

# Install formulae
if [[ -f "$FORMULAE_FILE" ]]; then
    while IFS= read -r formula; do
        [[ -z "$formula" ]] && continue
        echo "  Installing formula: $formula"
        brew install "$formula" || true
    done < "$FORMULAE_FILE"
fi

# Remove quarantine attributes on macOS
if [[ "$(uname)" == "Darwin" ]] && [[ -d /opt/homebrew/bin ]]; then
    find /opt/homebrew/bin -type f -exec xattr -d com.apple.quarantine "{}" \; 2>/dev/null || true
fi

echo "✓ Homebrew packages restored"
