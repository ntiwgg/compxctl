# compxctl completions for fish
#
# Mirrors the argparse CLI in compxctl.py:
#   bare `compxctl` == `rate`; subcommands: rate status dpi battery probe.
#   Every command that talks to the device takes --timeout-ms and --tries;
#   the read commands also take --json (one document, schema 2).
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
complete -c compxctl -n "__fish_seen_subcommand_from rate" -l reset-port -d "after --persist, re-enumerate the port so the host re-reads the descriptors"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -l json -d "print one JSON document (schema 2)"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -l timeout-ms -d "wait this long for one answer (default 500)"
complete -c compxctl -n "__fish_seen_subcommand_from rate" -l tries -d "send a command at most this many times (default 4)"

# dpi: 'list' value and target slot
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -a "list"
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -l slot -d "slot index 0..5"
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -l json -d "print one JSON document (schema 2)"
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -l timeout-ms -d "wait this long for one answer (default 500)"
complete -c compxctl -n "__fish_seen_subcommand_from dpi" -l tries -d "send a command at most this many times (default 4)"

# status and battery: JSON for status bars, and the same patience flags
complete -c compxctl -n "__fish_seen_subcommand_from status" -l json -d "print one JSON document (schema 2)"
complete -c compxctl -n "__fish_seen_subcommand_from status" -l timeout-ms -d "wait this long for one answer (default 500)"
complete -c compxctl -n "__fish_seen_subcommand_from status" -l tries -d "send a command at most this many times (default 4)"
complete -c compxctl -n "__fish_seen_subcommand_from battery" -l json -d "print one JSON document (schema 2)"
complete -c compxctl -n "__fish_seen_subcommand_from battery" -l timeout-ms -d "wait this long for one answer (default 500)"
complete -c compxctl -n "__fish_seen_subcommand_from battery" -l tries -d "send a command at most this many times (default 4)"

# probe: a window of config memory, up to the whole 64 KiB of it
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l start -d "start address (0x0000)"
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l length -d "bytes to read (max 0x10000)"
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l timeout-ms -d "wait this long for one answer (default 500)"
complete -c compxctl -n "__fish_seen_subcommand_from probe" -l tries -d "send a command at most this many times (default 4)"
