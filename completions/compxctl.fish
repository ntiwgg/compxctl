# compxctl completions for fish
#
# Mirrors the argparse CLI in compxctl.py:
#   bare `compxctl` == `status`; subcommands: set check status dpi battery probe.
#
# Install by copying to ~/.config/fish/completions/compxctl.fish
# (or run ./install.sh).

complete -c compxctl -f

# Top-level options
complete -c compxctl -n "__fish_use_subcommand" -l version -d "show version"

# Subcommands
complete -c compxctl -n "__fish_use_subcommand" -a "set" -d "apply a polling rate (125, 500 or 1000 Hz)"
complete -c compxctl -n "__fish_use_subcommand" -a "check" -d "measure the actual polling rate"
complete -c compxctl -n "__fish_use_subcommand" -a "status" -d "print a read-only device snapshot"
complete -c compxctl -n "__fish_use_subcommand" -a "dpi" -d "list DPI slots or set a slot's DPI"
complete -c compxctl -n "__fish_use_subcommand" -a "battery" -d "read the battery level and charging state"
complete -c compxctl -n "__fish_use_subcommand" -a "probe" -d "read the mouse config memory"

# set: target polling rate
complete -c compxctl -n "__fish_seen_subcommand_from set" -a "125 500 1000"

# check: input event node to measure (default: auto-detect)
complete -c compxctl -n "__fish_seen_subcommand_from check" -l device -d "input event node"

# dpi: 'list' value and target slot
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -a "list"
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -l slot -d "slot index 0..5"

# probe: config-memory window
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l start -d "start address (hex, e.g. 0x0000)"
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l length -d "bytes to read (hex)"
