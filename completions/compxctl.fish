# compxctl completions for fish
#
# Mirrors the argparse CLI in compxctl.py:
#   bare `compxctl` == `rate`; subcommands: rate status dpi battery probe.
#
# Install by copying to ~/.config/fish/completions/compxctl.fish
# (or run ./install.sh).

complete -c compxctl -f

# Top-level options
complete -c compxctl -n "__fish_use_subcommand" -l version -d "show version"

# Subcommands
complete -c compxctl -n "__fish_use_subcommand" -a "rate" -d "show or set the polling rate"
complete -c compxctl -n "__fish_use_subcommand" -a "status" -d "read-only device snapshot"
complete -c compxctl -n "__fish_use_subcommand" -a "dpi" -d "list DPI slots or set a slot's DPI"
complete -c compxctl -n "__fish_use_subcommand" -a "battery" -d "read the battery level and charging state"
complete -c compxctl -n "__fish_use_subcommand" -a "probe" -d "read the mouse config memory"

# rate: where to apply, then the rate itself
complete -c compxctl -n "__fish_seen_subcommand_from rate" -a "host" -d "set the host polling interval (all USB mice, reversible, needs root)"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -a "device" -d "write the device's stored interval (needs --persist)"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -a "both" -d "apply on the host and in the device"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -a "125 500 1000"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -l persist -d "confirm the EEPROM write required by 'rate device'"

# dpi: 'list' value and target slot
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -a "list"
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -l slot -d "slot index 0..5"

# probe: config-memory window
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l start -d "start address (0x0000)"
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l length -d "bytes to read (max 0x100)"
