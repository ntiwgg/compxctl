#!/usr/bin/env python3
"""compx-tray — the CompX mouse in the GNOME tray.

GNOME has no tray of its own any more: the icons come from the AppIndicator
extension (`appindicatorsupport`), which renders StatusNotifierItem services.
This is one such service — it asks `compxctl status --json` on a timer and shows
the answer as an icon, a tooltip and a menu.

The battery percentage is *drawn into the icon* rather than set as a label,
because the extension's support for labels is broken (Ubuntu bug 2059818:
`set_label` stopped working after Jammy). The icon is the part that always shows.

The badge has its own background on purpose: a light shell theme would swallow
white text drawn straight onto a transparent icon.

Requires python-gobject with Gtk 3 and AyatanaAppIndicator3 — both ship with a
GNOME desktop. Nothing else, and no root.

Environment:
  COMPXCTL       the tool to run (default: compxctl, which must be in PATH)
  COMPX_INTERVAL seconds between polls (default 10)
  COMPX_TERMINAL terminal for the interactive screen (default: first one found)
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

import cairo  # noqa: E402  (after gi.require_version, like every PyGObject app)
from gi.repository import AyatanaAppIndicator3, Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

TOOL = os.environ.get("COMPXCTL", "compxctl")
INTERVAL = max(2, int(os.environ.get("COMPX_INTERVAL", "10")))
TIMEOUT_MS = 150  # an awake mouse answers in single-digit milliseconds
RUN_TIMEOUT = 10  # a subprocess may never outlive its usefulness
BADGE_PX = 44  # drawn large and scaled down by the shell: crisper on HiDPI
ICON_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "compx-tray"
PRESETS = (400, 800, 1600, 2400)

# A panel has no terminal of its own, so the full-screen view needs one.
TERMINALS = (
    ("ptyxis", "--"),
    ("gnome-terminal", "--"),
    ("foot", "-e"),
    ("alacritty", "-e"),
    ("kitty",),
    ("xterm", "-e"),
)

CHARGING = (0.16, 0.65, 0.45)
DISCHARGING = (0.18, 0.24, 0.36)
ASLEEP = (0.45, 0.45, 0.48)


def run_tool(*arguments: str) -> str:
    """Run the tool and return its stdout, empty when it could not be run."""
    try:
        finished = subprocess.run(
            [TOOL, *arguments], capture_output=True, text=True, timeout=RUN_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return finished.stdout


def read_status() -> dict:
    """One status document, or a document-shaped error when there is none.

    A sleeping mouse is the normal case, not an exception: the tool still answers,
    with `battery: null` and a warning explaining itself. This only has to cover
    the tool being absent or its output unreadable.
    """
    try:
        return json.loads(run_tool("status", "--json", "--tries", "1", "--timeout-ms", str(TIMEOUT_MS)))
    except ValueError:
        return {"error": {"message": f"{TOOL} did not answer"}}


def describe(document: dict) -> dict:
    """The few facts the icon, the tooltip and the menu are built from."""
    battery = document.get("battery") or {}
    polling = document.get("polling") or {}
    error = (document.get("error") or {}).get("message")
    if error is None and (document.get("warnings") or []):
        error = document["warnings"][0]
    percent = battery.get("percent")
    return {
        "name": (document.get("device") or {}).get("name") or "CompX mouse",
        "percent": percent,
        "charging": bool(battery.get("charging")),
        "state": battery.get("state_label") or "?",
        "dpi": (document.get("active_dpi") or {}).get("dpi"),
        "rate": polling.get("effective_rate_hz"),
        "stored": document.get("stored_rate_hz"),
        "asleep": percent is None,
        "error": error,
    }


def badge_text(facts: dict) -> str:
    if facts["asleep"]:
        return "—"
    return str(facts["percent"])


def badge_colour(facts: dict) -> tuple[float, float, float]:
    if facts["asleep"]:
        return ASLEEP
    return CHARGING if facts["charging"] else DISCHARGING


def render_badge(text: str, colour: tuple[float, float, float], size: int = BADGE_PX):
    """A rounded badge with the text centred on its own background.

    Drawn instead of a stock icon so the number is legible in any panel theme:
    the background travels with the text.
    """
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    context = cairo.Context(surface)
    radius = size * 0.22
    context.new_sub_path()
    context.arc(size - radius, radius, radius, -0.5 * 3.14159, 0)
    context.arc(size - radius, size - radius, radius, 0, 0.5 * 3.14159)
    context.arc(radius, size - radius, radius, 0.5 * 3.14159, 3.14159)
    context.arc(radius, radius, radius, 3.14159, 1.5 * 3.14159)
    context.close_path()
    context.set_source_rgb(*colour)
    context.fill_preserve()
    context.set_line_width(size * 0.03)
    context.set_source_rgba(1, 1, 1, 0.25)
    context.stroke()

    # The text is fitted to the badge instead of the badge to the text: "100"
    # needs a smaller face than "64", and the shell scales the whole thing down
    # to ~16 px, where a cramped number turns to mush.
    layout = PangoCairo.create_layout(context)
    layout.set_text(text, -1)
    room = size * 0.76
    points = int(size * 0.46)
    while points > 6:
        layout.set_font_description(Pango.FontDescription(f"Sans Bold {points}"))
        width, height = layout.get_pixel_size()
        if width <= room and height <= room:
            break
        points -= 1
    context.set_source_rgb(1, 1, 1)
    context.move_to((size - width) / 2, (size - height) / 2)
    PangoCairo.show_layout(context, layout)
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)


def tooltip(facts: dict) -> str:
    lines = [facts["name"]]
    if facts["asleep"]:
        lines.append(facts["error"] or "мышь спит или не отвечает")
    else:
        lines.append(f"Батарея: {facts['percent']}% ({'заряжается' if facts['charging'] else facts['state']})")
    lines.append(f"Активный DPI: {facts['dpi'] if facts['dpi'] is not None else '?'}")
    lines.append(
        f"Частота: {facts['rate'] if facts['rate'] is not None else '?'} Гц (хост) / "
        f"{facts['stored'] if facts['stored'] is not None else 'н/д'} Гц (EEPROM)"
    )
    return "\n".join(lines)


def terminal_command(arguments: list[str]) -> list[str] | None:
    for name, *prefix in TERMINALS:
        path = shutil.which(name)
        if path:
            return [path, *prefix, *arguments]
    return None


class Tray:
    """The indicator, its menu, and the timer that keeps them honest."""

    def __init__(self) -> None:
        self.facts = describe(read_status())
        self.serial = 0
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "compx-mouse",
            "input-mouse",
            AyatanaAppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("CompX mouse")
        self.indicator.set_icon_theme_path(str(ICON_DIR))
        self.refresh()
        GLib.timeout_add_seconds(INTERVAL, self.refresh)

    def publish_icon(self, pixbuf) -> str:
        """Write the badge out and return the file name to hand the indicator.

        The binding takes an icon *name*, not a pixbuf, so the drawing goes to a
        file in the runtime directory and its path is used as the name. The serial
        in the file name is what makes the host reload it: an icon is cached by
        name, so a fixed name would show the first badge for the rest of the
        session. Only the last few are kept.
        """
        ICON_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.serial += 1
        path = ICON_DIR / f"badge-{self.serial}.png"
        pixbuf.savev(str(path), "png", [], [])
        for stale in sorted(ICON_DIR.glob("badge-*.png"))[:-3]:
            stale.unlink(missing_ok=True)
        return str(path)

    def refresh(self) -> bool:
        """Poll once and redraw. Always returns True: the timer keeps running."""
        self.facts = describe(read_status())
        badge = render_badge(badge_text(self.facts), badge_colour(self.facts))
        details = tooltip(self.facts)
        self.indicator.set_icon_full(self.publish_icon(badge), details)
        # The tooltip a panel shows is built from Title (the second half of
        # set_icon_full is an accessible description, not a tooltip — measured:
        # the ToolTip property stayed empty), so the summary goes there, with the
        # full text as a one-line-ish title only when it fits a tooltip.
        self.indicator.set_title(details.replace("\n", " · ") if not self.facts["asleep"] else details.splitlines()[-1])
        # Harmless where labels are ignored, useful where they are not.
        self.indicator.set_label(
            "" if self.facts["asleep"] else f"{self.facts['percent']}%", "100%"
        )
        self.indicator.set_menu(self.build_menu())
        return True

    def build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        for line in tooltip(self.facts).splitlines():
            item = Gtk.MenuItem(label=line)
            item.set_sensitive(False)
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())

        current = self.facts["dpi"]
        group: list[Gtk.RadioMenuItem] = []
        for value in PRESETS:
            item = Gtk.RadioMenuItem(label=f"{value} DPI", group=group[0] if group else None)
            item.set_active(value == current)
            item.connect("activate", self.on_preset, value)
            group.append(item)
            menu.append(item)
        if current is not None and current not in PRESETS:
            item = Gtk.RadioMenuItem(label=f"{current} DPI (сейчас)", group=group[0])
            item.set_active(True)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        screen = Gtk.MenuItem(label="Открыть экран (tui)")
        screen.connect("activate", self.on_screen)
        menu.append(screen)
        refresh = Gtk.MenuItem(label="Обновить")
        refresh.connect("activate", lambda _item: self.refresh())
        menu.append(refresh)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Выход")
        quit_item.connect("activate", lambda _item: Gtk.main_quit())
        menu.append(quit_item)
        menu.show_all()
        return menu

    def on_preset(self, _item: Gtk.RadioMenuItem, value: int) -> None:
        run_tool("dpi", str(value))
        self.refresh()

    def on_screen(self, _item: Gtk.MenuItem) -> None:
        command = terminal_command([TOOL, "tui"])
        if command is None:
            self.notify("Не нашёл терминал для полноэкранного режима")
            return
        try:
            subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            self.notify(f"Не удалось запустить терминал: {exc}")

    def notify(self, message: str) -> None:
        """Say something without a dialog: the tray has no room for one."""
        print(f"compx-tray: {message}", file=sys.stderr, flush=True)


def claim_single_instance():
    """Only one indicator per session, or the tray fills up with copies."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    handle = open(runtime / "compx-tray.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return handle


def main() -> int:
    if shutil.which(TOOL) is None:
        print(
            f"compx-tray: `{TOOL}` is not in PATH. Install the tool first "
            "(see the README) or point COMPXCTL at it.",
            file=sys.stderr,
        )
        return 1
    lock = claim_single_instance()
    if lock is None:
        print("compx-tray: already running, nothing to do.", file=sys.stderr)
        return 0
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
