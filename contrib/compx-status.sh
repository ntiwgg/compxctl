#!/usr/bin/env bash
# compx-status — turn `compxctl status --json` into what a desktop widget wants.
#
# One implementation for three consumers:
#   compx-status                  -> one plain line (Polybar, xsetroot, tmux, …)
#   compx-status --format waybar  -> the JSON object a Waybar custom module reads
#   compx-status --format argos   -> the line/dropdown format of GNOME Argos,
#                                    Guillotine and Executor
#
# No `jq`: the parsing is done by the python3 that compxctl itself needs, so the
# only dependency is the tool.
#
# Environment:
#   COMPXCTL     the tool to run, as ONE executable word (default: compxctl).
#                For a copy that is not in PATH, point it at the script itself —
#                compxctl.py is executable and starts with a shebang, so
#                COMPXCTL=/path/to/compxctl.py works, spaces in the path
#                included. Need an interpreter? Make a two-line wrapper.
#   COMPX_TRIES  attempts per readout (default 1: a bar would rather see "asleep")
#   COMPX_MS     wait per attempt in ms (default 150)

set -u

COMPXCTL="${COMPXCTL:-compxctl}"
COMPX_TRIES="${COMPX_TRIES:-1}"
COMPX_MS="${COMPX_MS:-150}"

format="plain"
while [ $# -gt 0 ]; do
    case "$1" in
        --format=*) format="${1#--format=}" ;;
        --format) shift; format="${1:-plain}" ;;
        plain|waybar|argos) format="$1" ;;
        *) echo "compx-status: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

json="$("$COMPXCTL" status --json --tries "$COMPX_TRIES" --timeout-ms "$COMPX_MS" 2>/dev/null)"

# A mouse that is asleep, absent or locked out is normal for a bar: not an error,
# a state to show. Each format says so in its own way.
if [ -z "$json" ]; then
    case "$format" in
        waybar) echo '{"text": "🖱️ —", "tooltip": "compxctl did not answer", "class": "asleep"}' ;;
        argos)  printf '🖱️ — | color=#888888\n---\ncompxctl did not answer\nОбновить | refresh=true\n' ;;
        *)      printf '🖱️ —' ;;
    esac
    exit 0
fi

COMPX_FORMAT="$format" python3 - "$json" <<'PY'
import json
import os
import sys

try:
    doc = json.loads(sys.argv[1])
except ValueError:
    doc = {"error": {"message": "unreadable JSON from compxctl"}}

fmt = os.environ.get("COMPX_FORMAT", "plain")
error = (doc.get("error") or {}).get("message")
battery = doc.get("battery") or {}
dpi = (doc.get("active_dpi") or {}).get("dpi")
rate = (doc.get("polling") or {}).get("effective_rate_hz")
stored = doc.get("stored_rate_hz")
percent = battery.get("percent")
charging = bool(battery.get("charging"))
asleep = error is not None or percent is None

icon = "⚡" if charging else "🖱️"
state = "charging" if charging else ("asleep" if asleep else "discharging")
panel = f"{icon} спит" if asleep else f"{icon} {percent}% · {dpi} DPI"

tooltip = [
    error or f"Батарея: {percent}% ({'заряжается' if charging else '2.4G'})",
    f"Активный DPI: {dpi if dpi is not None else '?'}",
    f"Частота: {rate if rate is not None else '?'} Гц (хост) / "
    f"{stored if stored is not None else 'н/д'} Гц (EEPROM)",
]

if fmt == "waybar":
    print(json.dumps({
        "text": panel,
        "tooltip": "\n".join(tooltip),
        "class": state,
        "percentage": percent if percent is not None else 0,
    }, ensure_ascii=False))
elif fmt == "argos":
    print(f"{panel} | font=monospace")
    print("---")
    print("\n".join(tooltip))
    print("---")
    for value in (400, 800, 1600, 2400):
        print(f"DPI: {value} | bash=compxctl dpi {value} terminal=false refresh=true")
    print("---")
    print("Открыть TUI | bash=compxctl tui terminal=true")
    print("Обновить | refresh=true")
else:
    print(panel, end="")
PY
