
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

# Detect OS
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *)
        echo "Error: Unsupported operating system: $OS"
        exit 1
        ;;
esac

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found"
    echo "Please install Python 3.11+ before running this script"
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

# Install system dependencies
echo "Checking system dependencies..."
if [ "$PLATFORM" = "macos" ]; then
    if ! command -v brew &> /dev/null; then
        echo "Error: Homebrew not found"
        echo "Install from https://brew.sh or install rsync manually"
        exit 1
    fi
    if [ ! -f "$SCRIPT_DIR/Brewfile" ]; then
        echo "Error: Brewfile not found in $SCRIPT_DIR"
        exit 1
    fi
    brew bundle install --file="$SCRIPT_DIR/Brewfile"
else
    if [ ! -f "$SCRIPT_DIR/packages.txt" ]; then
        echo "Error: packages.txt not found in $SCRIPT_DIR"
        exit 1
    fi
    while IFS= read -r pkg || [ -n "$pkg" ]; do
        [ -z "$pkg" ] && continue
        if ! command -v "$pkg" &> /dev/null; then
            if command -v apt-get &> /dev/null; then
                sudo apt-get update && sudo apt-get install -y "$pkg"
            elif command -v dnf &> /dev/null; then
                sudo dnf install -y "$pkg"
            elif command -v yum &> /dev/null; then
                sudo yum install -y "$pkg"
            elif command -v pacman &> /dev/null; then
                sudo pacman -S --noconfirm "$pkg"
            elif command -v zypper &> /dev/null; then
                sudo zypper install -y "$pkg"
            else
                echo "Error: $pkg not found and no supported package manager detected"
                echo "Please install $pkg manually"
                exit 1
            fi
        fi
    done < "$SCRIPT_DIR/packages.txt"
fi
echo "✓ System dependencies installed"

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
mkdir -p "$INSTALL_DIR/captures"

# Symlink to make captures accessible as ~/Snapshots
ln -sf "$INSTALL_DIR/captures" "$HOME/Snapshots" 

# Copy configuration files
echo "Installing configuration files..."
if [ -d "$SCRIPT_DIR/configs" ]; then
    cp "$SCRIPT_DIR/configs/"*.toml "$INSTALL_DIR/configs/" 2>/dev/null || true
fi

# Copy scripts
echo "Installing scripts..."
if [ -d "$SCRIPT_DIR/scripts" ]; then
    cp "$SCRIPT_DIR/scripts/"*.sh "$INSTALL_DIR/scripts/" 2>/dev/null || true
fi

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
echo "  1. Review and customize configs in: $INSTALL_DIR/configs/"
echo "  2. Create your first snapshot: snap capture"
echo "  3. For help: snap --help"
echo


