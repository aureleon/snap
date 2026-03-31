
#!/usr/bin/env bash
#
# Installation script for snap.py
#
# Usage: ./install.sh [install_dir]
#   install_dir: Optional installation directory (default: ~/.snap)
#

set -e

# Default installation directory
DEFAULT_INSTALL_DIR="$HOME/.snap"
INSTALL_DIR="${1:-$DEFAULT_INSTALL_DIR}"

# Get script directory (where install.sh is located)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "snap.py Installation"
echo "====================================="
echo

# Resolve installation directory
INSTALL_DIR=$(cd "$(dirname "$INSTALL_DIR")" 2>/dev/null && pwd)/$(basename "$INSTALL_DIR") || INSTALL_DIR="$DEFAULT_INSTALL_DIR"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

echo "Installation directory: $INSTALL_DIR"
echo

# Check and install prerequisites
echo "Checking prerequisites..."

# Check for macOS
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo "Error: This script currently only supports macOS"
    exit 1
fi

# Check Homebrew and Python - run bootstrap if missing
if ! command -v brew &> /dev/null || ! command -v python3 &> /dev/null; then
    echo "Error: Homebrew or Python not found"
    echo "Please install manually or via alternative methods"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    echo "✗ Python 3.11+ required (found $PYTHON_VERSION)"
    exit 1
else
    echo "✓ Python $PYTHON_VERSION"
fi

# Check Python dependencies
if [ ! -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "Error: requirements.txt not found in $SCRIPT_DIR"
    exit 1
fi
echo "Checking Python dependencies..."
python3 -m pip install --user -r "$SCRIPT_DIR/requirements.txt"
echo "✓ Python dependencies installed"

# Check required command-line tools
if [ ! -f "$SCRIPT_DIR/Brewfile" ]; then
    echo "Error: Brewfile not found in $SCRIPT_DIR"
    exit 1
fi
echo "Checking Homebrew dependencies..."
brew bundle install --file="$SCRIPT_DIR/Brewfile"
echo "✓ Homebrew dependencies installed"

# Check if directory already exists
if [ -d "$INSTALL_DIR" ]; then
    echo "Warning: Directory $INSTALL_DIR already exists"
    read -p "Overwrite existing files? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
	echo "Installation cancelled"
	exit 1
    fi
fi

# Create directory structure
echo "Creating directory structure..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/configs"
mkdir -p "$INSTALL_DIR/scripts"
mkdir -p "$INSTALL_DIR/backups"

# Symlink to make backups accessible as ~/Backups 
ln -sf "$INSTALL_DIR/backups" "$HOME/Backups" 

echo
echo "✓ Installation complete!"
echo
echo "Installation location: $INSTALL_DIR"
echo

# Install snap.py to PATH
echo "Where would you like to install snap.py?"
echo
echo "Available options:"
echo "  1) ~/.local/bin/snap  (user-only, no sudo required)"
echo "  2) /usr/local/bin/snap  (system-wide, requires sudo)"
echo "  3) Skip installation to PATH"
echo

read -p "Choose an option (1-3): " -n 1 -r
echo

case $REPLY in
    1)
	BIN_DIR="$HOME/.local/bin"
	mkdir -p "$BIN_DIR"
	cp "$SCRIPT_DIR/snap.py" "$BIN_DIR/snap"
	chmod +x "$BIN_DIR/snap"
	echo "✓ Installed: $BIN_DIR/snap"

	# Check if ~/.local/bin is in PATH
	if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
	    echo
	    echo "Note: $HOME/.local/bin is not in your PATH"
	    echo "Add this line to your ~/.bashrc or ~/.zshrc:"
	    echo
	    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
	    echo
	fi
	;;
    2)
	BIN_DIR="/usr/local/bin"
	sudo cp "$SCRIPT_DIR/snap.py" "$BIN_DIR/snap"
	sudo chmod +x "$BIN_DIR/snap"
	echo "✓ Installed: $BIN_DIR/snap"
	;;
    3)
	echo "Skipping installation to PATH"
	echo "You can run snap.py directly:"
	echo "  $SCRIPT_DIR/snap.py"
	;;
    *)
	echo "Invalid option, skipping installation"
	;;
esac

echo
echo "Installation complete!"
echo
echo "Next steps:"
echo "  1. Customize configs in: $INSTALL_DIR/configs/"
echo "  2. Create your first backup: snap backup"
echo "  3. For help: snap --help"
echo


