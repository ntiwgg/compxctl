#!/usr/bin/env bash
#
# compxctl installer — device access rules and completions, optional install.
#
# Only the udev part needs root; sudo is invoked internally, so the script
# itself can be run as a regular user.
#
# Usage:
#   ./install.sh                 install udev rules + fish completions
#   ./install.sh --with-venv P   same, plus create venv P and `pip install P[device]`
#   ./install.sh --udev-only     only install the udev rules
#   ./install.sh --uninstall     remove the udev rule and fish completions
#   ./install.sh --help          show this help
#
set -euo pipefail

# Script and repository root (works from any working directory).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

UDEV_RULES_SRC="$SCRIPT_DIR/99-compx-mouse.rules"
FISH_COMPLETION_SRC="$SCRIPT_DIR/completions/compxctl.fish"
UDEV_RULES_DEST="/etc/udev/rules.d/99-compx-mouse.rules"
FISH_COMPLETION_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions"


die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

# Run a command with root privileges: direct when already root, sudo otherwise.
run_as_root() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

install_udev() {
    [[ -f "$UDEV_RULES_SRC" ]] || die "udev rules file not found: $UDEV_RULES_SRC"
    echo "Installing udev rule for CompX / Ardor Gaming mice (VID 25a7)…"
    run_as_root cp "$UDEV_RULES_SRC" "$UDEV_RULES_DEST"
    run_as_root udevadm control --reload-rules
    # Target the vendor rather than retriggering every device on the system.
    run_as_root udevadm trigger --subsystem-match=usb --attr-match=idVendor=25a7
    echo "udev rules installed. Re-plug your mouse (or log out and back in) to apply them."
}

uninstall_udev() {
    echo "Removing udev rule $UDEV_RULES_DEST…"
    run_as_root rm -f "$UDEV_RULES_DEST"
    run_as_root udevadm control --reload-rules
    run_as_root udevadm trigger --subsystem-match=usb --attr-match=idVendor=25a7
    echo "udev rule removed. Re-plug your mouse to restore default access."
}

install_completions() {
    if ! command -v fish >/dev/null 2>&1; then
        echo "fish not found — skipping shell completions."
        if [[ "${SHELL:-}" == *bash* ]]; then
            echo "Your shell is bash; these completions target fish and would not apply."
        fi
        return 0
    fi
    [[ -f "$FISH_COMPLETION_SRC" ]] || die "completion file not found: $FISH_COMPLETION_SRC"
    mkdir -p "$FISH_COMPLETION_DIR"
    cp "$FISH_COMPLETION_SRC" "$FISH_COMPLETION_DIR/compxctl.fish"
    echo "Fish completions installed to $FISH_COMPLETION_DIR/compxctl.fish"
    echo "They apply to new fish sessions (or after running: exec fish)."
}

uninstall_completions() {
    rm -f "$FISH_COMPLETION_DIR/compxctl.fish"
    echo "Removed fish completions: $FISH_COMPLETION_DIR/compxctl.fish (if present)."
}

install_python() {
    local venv_path="${1:-}"
    if [[ -n "$venv_path" ]]; then
        echo "Creating virtual environment at $venv_path…"
        python3 -m venv "$venv_path"
        "$venv_path/bin/pip" install --upgrade pip >/dev/null
        # `[device]` pulls pyusb, which the device commands need. `rate` itself
        # has no dependencies at all.
        "$venv_path/bin/pip" install "$SCRIPT_DIR[device]"
        echo "Installed the 'compxctl' console script into $venv_path"
        echo "Activate it with: source $venv_path/bin/activate"
        return 0
    fi
    echo "Python install skipped (pass --with-venv PATH to create a venv and pip install)."
    echo "Manual setup:"
    echo "  pip install \".[device]\"   # console script + pyusb for the device commands"
    echo "  pip install .             # console script only; 'rate' needs nothing else"
    echo "Note: 'rate' reads the polling rate from sysfs and needs no packages at all."
}

print_help() {
    cat <<'EOF'
compxctl installer

Usage:
  ./install.sh [OPTIONS]

Options:
  --with-venv PATH   create a virtualenv at PATH and install compxctl[device]
  --udev-only        only install the udev rules, skip completions and Python
  --uninstall        remove the udev rule and the fish completions
  -h, --help         show this help and exit

Without options the script installs the udev rules and the fish completions,
then prints a hint for the Python setup. Only the udev steps use sudo.

Examples:
  ./install.sh                     # udev rules + fish completions
  ./install.sh --udev-only         # just the udev rules
  ./install.sh --with-venv .venv   # also create .venv and install the CLI
  ./install.sh --uninstall         # undo the udev rule and completions
EOF
}

main() {
    local mode="install"
    local udev_only=0
    local venv_path=""

    while (($# > 0)); do
        case "$1" in
            --uninstall)
                mode="uninstall"
                shift
                ;;
            --udev-only)
                udev_only=1
                shift
                ;;
            --with-venv)
                (($# >= 2)) || die "--with-venv requires a PATH argument"
                venv_path="$2"
                shift 2
                ;;
            --with-venv=*)
                venv_path="${1#*=}"
                [[ -n "$venv_path" ]] || die "--with-venv requires a PATH argument"
                shift
                ;;
            -h | --help)
                print_help
                return 0
                ;;
            *)
                die "unknown option '$1' (see --help)"
                ;;
        esac
    done

    if [[ "$mode" == "uninstall" ]]; then
        uninstall_udev
        if ((udev_only)); then
            return 0
        fi
        uninstall_completions
        return 0
    fi

    install_udev
    if ((udev_only)); then
        return 0
    fi
    install_completions
    install_python "$venv_path"
}

main "$@"
