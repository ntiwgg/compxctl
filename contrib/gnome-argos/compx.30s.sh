#!/usr/bin/env bash
# GNOME Argos / Guillotine / Executor widget for a CompX mouse.
#
# Copy to ~/.config/argos/compx.30s.sh (the name says: refresh every 30 s) and
# make it executable — the extension picks it up by itself. 10s/1m/… also work.
#
# The panel is refreshed by the extension; the dropdown is rebuilt on every
# refresh too, which is why "Обновить" is there and cheap.
#
# The formatting lives in contrib/compx-status.sh so that the GNOME, Waybar and
# Polybar widgets cannot drift apart. Put that file in PATH (e.g.
# ~/.local/bin/compx-status) or point COMPX_STATUS at it.

set -u
exec "${COMPX_STATUS:-compx-status}" --format argos
