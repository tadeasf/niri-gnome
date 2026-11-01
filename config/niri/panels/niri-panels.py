#!/usr/bin/python3
"""GNOME-style top-bar menus for niri, opened from waybar:
  quick     - quick settings (status area): volume, toggles, screenshot/settings/lock/power
  calendar  - date menu (clock): notification list, calendar, weather
Runs as a daemon; waybar toggles a menu with
  gdbus call --session --dest io.github.tadeasf.NiriPanels --object-path /io/github/tadeasf/NiriPanels \
      --method org.gtk.Actions.Activate quick|calendar '[]' '{}'
"""
import ctypes.util
import json
import os
import pwd
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date

LAYER_SHELL = ctypes.util.find_library("gtk4-layer-shell") or sys.exit("niri-panels: install gtk4-layer-shell")
# gtk4-layer-shell must be loaded before libwayland-client
if LAYER_SHELL not in os.environ.get("LD_PRELOAD", ""):
    os.environ["LD_PRELOAD"] = ":".join(filter(None, [LAYER_SHELL, os.environ.get("LD_PRELOAD")]))
    os.execv(sys.executable, [sys.executable] + sys.argv)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

APP_ID = "io.github.tadeasf.NiriPanels"
HERE = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(GLib.get_user_state_dir(), "niri-panels.json")  # notifications the user cleared
WEATHER_TTL = 15 * 60

CSS = b"""
window.niri-panel { background: transparent; }
.panel { background: #1e1e1e; border-radius: 22px; padding: 16px; border: 1px solid rgba(255,255,255,0.08); }
label { font-family: "Fira Sans"; font-weight: 600; font-size: 11pt; color: #ffffff; }
image { color: #ffffff; }
.dim { color: rgba(255,255,255,0.6); font-size: 9.5pt; }
.big { font-size: 16pt; font-weight: 700; }
.heading { font-weight: 700; }
button { border: none; box-shadow: none; background-image: none; outline: none; }
.round { border-radius: 99px; min-width: 38px; min-height: 38px; padding: 0; background: #333333; }
.round:hover, .pill:hover, .menuitem:hover { background: #3d3d3d; }
.pill { border-radius: 99px; padding: 6px 16px; background: #333333; min-height: 44px; }
.pill.active { background: #ffffff; }
.pill.active label, .pill.active image { color: #000000; }
.pill.active .dim { color: rgba(0,0,0,0.6); }
.pill.active:hover { background: #e6e6e6; }
.menuitem { padding: 8px 12px; border-radius: 12px; background: transparent; }
.flat { background: transparent; border-radius: 99px; padding: 4px; min-width: 0; min-height: 0; }
.flat:hover { background: #3d3d3d; }
.card { background: #2a2a2a; border-radius: 16px; padding: 10px 12px; }
.avatar { border-radius: 99px; }
scale { padding: 0; }
scale trough { background: #3a3a3a; min-height: 6px; border-radius: 99px; border: none; }
scale highlight { background: #ffffff; border-radius: 99px; border: none; }
scale slider { background: #ffffff; min-width: 20px; min-height: 20px; border-radius: 99px; margin: -8px; border: none; box-shadow: none; }
switch { background: #3a3a3a; border: none; border-radius: 99px; }
switch:checked { background: #ffffff; }
switch slider { background: #ffffff; border-radius: 99px; border: none; box-shadow: none; }
switch:checked slider { background: #1e1e1e; }
calendar { background: transparent; border: none; color: #ffffff; padding: 0; }
calendar > header { border: none; background: transparent; }
calendar > header > button { background: transparent; color: #ffffff; border-radius: 99px; }
calendar > header > label { color: #ffffff; }
calendar > grid > label { color: #ffffff; border-radius: 99px; min-width: 32px; min-height: 30px; }
calendar > grid > label.day-name { color: rgba(255,255,255,0.5); font-size: 9pt; }
calendar > grid > label.week-number { color: rgba(255,255,255,0.4); }
calendar > grid > label.other-month { color: rgba(255,255,255,0.3); }
calendar > grid > label:selected { background: #3d3d3d; color: #ffffff; }
calendar > grid > label.today { background: #ffffff; color: #000000; }
scrolledwindow { background: transparent; }
"""

WEATHER_ICONS = {  # wttr.in weatherCode -> symbolic icon
    "weather-clear-symbolic": {113},
    "weather-few-clouds-symbolic": {116},
    "weather-overcast-symbolic": {119, 122},
    "weather-fog-symbolic": {143, 248, 260},
    "weather-storm-symbolic": {200, 386, 389, 392, 395},
    "weather-snow-symbolic": {179, 182, 185, 227, 230, 281, 284, 311, 314, 317, 320, 323, 326, 329, 332, 335,
                              338, 350, 362, 365, 368, 371, 374, 377},
}


def weather_icon(code):
    return next((icon for icon, codes in WEATHER_ICONS.items() if int(code) in codes), "weather-showers-symbolic")


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def spawn(*cmd):
    try:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def has(cmd):
    return GLib.find_program_in_path(cmd) is not None


def label(text, *classes, **kw):
    return Gtk.Label(label=text, xalign=0, css_classes=list(classes), **kw)


def icon(name, size=16):
    return Gtk.Image(icon_name=name, pixel_size=size)


def round_button(icon_name, tooltip, cb):
    b = Gtk.Button(child=icon(icon_name), css_classes=["round"], tooltip_text=tooltip)
    b.connect("clicked", lambda *_: cb())
    return b


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"cleared_upto": 0, "hidden": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


class Panels(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.weather = None  # (fetched_at, data)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = True
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.menus = {
            "quick": self.make_menu("quick", Gtk.Align.END, self.build_quick),
            "calendar": self.make_menu("calendar", Gtk.Align.CENTER, self.build_calendar),
        }
        for name in self.menus:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_, n=name: self.toggle(n))
            self.add_action(act)

    def do_activate(self):
        pass  # second launches land here; the daemon is already running

    # --- menu windows ----------------------------------------------------
    def make_menu(self, name, halign, build):
        win = Gtk.ApplicationWindow(application=self, css_classes=["niri-panel"])
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, f"niri-panels-{name}")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)
        # fill the screen below the bar (exclusive zone 0 respects waybar's) so a click outside closes the menu
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)
        outer = Gtk.Box()
        panel = Gtk.Box(css_classes=["panel"], halign=halign, valign=Gtk.Align.START, hexpand=True,
                        margin_top=6, margin_end=8, margin_start=8)
        outer.append(panel)
        win.set_child(outer)

        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, _n, x, y: self.click_outside(outer, panel, x, y))
        outer.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, *_: kv == Gdk.KEY_Escape and (self.hide_all() or True))
        win.add_controller(keys)
        return {"win": win, "panel": panel, "build": build}

    def click_outside(self, outer, panel, x, y):
        w = outer.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not panel:
            w = w.get_parent()
        if w is None:
            self.hide_all()

    def toggle(self, name):
        menu = self.menus[name]
        was_open = menu["win"].get_visible()
        self.hide_all()
        if was_open:
            return
        panel = menu["panel"]
        while c := panel.get_first_child():
            panel.remove(c)
        panel.append(menu["build"]())
        menu["win"].present()

    def hide_all(self):
        for m in self.menus.values():
            m["win"].set_visible(False)

    def run_and_hide(self, *cmd):
        self.hide_all()
        spawn(*cmd)

    # --- quick settings --------------------------------------------------
    def build_quick(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, width_request=380)

        # header: avatar, name, uptime | screenshot, settings, lock, power
        head = Gtk.Box(spacing=8)
        user = pwd.getpwuid(os.getuid())
        face = next((p for p in (os.path.expanduser("~/.face"), f"/var/lib/AccountsService/icons/{user.pw_name}")
                     if os.access(p, os.R_OK)), None)
        avatar = Gtk.Box(css_classes=["avatar"], overflow=Gtk.Overflow.HIDDEN, valign=Gtk.Align.CENTER)
        avatar.append(Gtk.Image.new_from_file(face) if face else icon("avatar-default-symbolic"))
        avatar.get_first_child().set_pixel_size(40)
        head.append(avatar)
        who = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        who.append(label(user.pw_gecos.split(",")[0] or user.pw_name, "heading"))
        who.append(label(sh("uptime", "-p").replace("up ", "Up "), "dim"))
        head.append(who)
        power_menu = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        head.append(round_button("camera-photo-symbolic", "Screenshot", self.screenshot))
        head.append(round_button("emblem-system-symbolic", "Settings",
                                 lambda: self.run_and_hide("env", "XDG_CURRENT_DESKTOP=GNOME", "gnome-control-center")))
        head.append(round_button("system-lock-screen-symbolic", "Lock",
                                 lambda: self.run_and_hide("swaylock", "-f", "-c", "000000")))
        head.append(round_button("system-shutdown-symbolic", "Power Off / Log Out",
                                 lambda: power_menu.set_reveal_child(not power_menu.get_reveal_child())))
        box.append(head)

        items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"])
        for text, cmd in (("Suspend", ("systemctl", "suspend")), ("Restart…", ("systemctl", "reboot")),
                          ("Power Off…", ("systemctl", "poweroff")),
                          ("Log Out…", ("niri", "msg", "action", "quit", "--skip-confirmation"))):
            b = Gtk.Button(child=label(text), css_classes=["menuitem"])
            b.connect("clicked", lambda *_, c=cmd: self.run_and_hide(*c))
            items.append(b)
        power_menu.set_child(items)
        box.append(power_menu)

        box.append(self.volume_row())
        if sh("brightnessctl", "--class=backlight", "-m", "info"):
            box.append(self.brightness_row())

        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        for i, pill in enumerate(p for p in (self.network_pill(), self.bluetooth_pill(), self.dnd_pill(),
                                             self.power_mode_pill()) if p):
            grid.attach(pill, i % 2, i // 2, 1, 1)
        box.append(grid)
        return box

    def screenshot(self):
        self.hide_all()
        GLib.timeout_add(300, lambda: spawn("niri", "msg", "action", "screenshot") or False)

    def volume_row(self):
        out = sh("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")  # "Volume: 0.61 [MUTED]"
        try:
            vol = round(float(out.split()[1]) * 100)
        except (IndexError, ValueError):
            vol = 0
        muted = "MUTED" in out
        row = Gtk.Box(spacing=6)
        mute = Gtk.Button(css_classes=["flat"])

        def vol_icon(v, m):
            return "audio-volume-muted-symbolic" if m or v == 0 else \
                f"audio-volume-{'low' if v < 34 else 'medium' if v < 67 else 'high'}-symbolic"

        mute.set_child(icon(vol_icon(vol, muted)))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_value(vol)
        state = {"muted": muted}

        def changed(s):
            v = round(s.get_value())
            spawn("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{v}%")
            mute.set_child(icon(vol_icon(v, state["muted"])))

        def toggle_mute(*_):
            state["muted"] = not state["muted"]
            spawn("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if state["muted"] else "0")
            mute.set_child(icon(vol_icon(round(scale.get_value()), state["muted"])))

        scale.connect("value-changed", changed)
        mute.connect("clicked", toggle_mute)
        more = Gtk.Button(child=icon("go-next-symbolic"), css_classes=["flat"], tooltip_text="Sound settings")
        more.connect("clicked", lambda *_: self.run_and_hide("pavucontrol"))
        for w in (mute, scale, more):
            row.append(w)
        return row

    def brightness_row(self):
        info = sh("brightnessctl", "--class=backlight", "-m", "info").split(",")  # name,class,cur,pct%,max
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Button(child=icon("display-brightness-symbolic"), css_classes=["flat"]))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_value(float(info[3].rstrip("%")) if len(info) > 3 else 50)
        scale.connect("value-changed", lambda s: spawn("brightnessctl", "--class=backlight", "set",
                                                       f"{round(s.get_value())}%"))
        row.append(scale)
        return row

    def pill(self, icon_name, title, subtitle, active, on_click):
        content = Gtk.Box(spacing=10)
        content.append(icon(icon_name))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        text.append(label(title, "heading", ellipsize=Pango.EllipsizeMode.END))
        sub = label(subtitle, "dim", ellipsize=Pango.EllipsizeMode.END, visible=bool(subtitle))
        text.append(sub)
        content.append(text)
        b = Gtk.Button(child=content, css_classes=["pill"] + (["active"] if active else []))

        def clicked(*_):
            result = on_click()
            if result is not None:  # (active, subtitle) after a toggle
                (b.add_css_class if result[0] else b.remove_css_class)("active")
                sub.set_label(result[1])
                sub.set_visible(bool(result[1]))

        b.connect("clicked", clicked)
        return b

    def network_pill(self):
        devs = [line.split(":") for line in sh("nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device").splitlines()]
        wifi = next((d for d in devs if d[0] == "wifi" and d[1] != "unavailable"), None)
        if wifi:
            def toggle():
                on = sh("nmcli", "radio", "wifi") != "enabled"
                sh("nmcli", "radio", "wifi", "on" if on else "off")
                return on, "" if not on else "Turning on…"
            return self.pill("network-wireless-symbolic", "Wi-Fi", wifi[2] if wifi[1] == "connected" else "",
                             sh("nmcli", "radio", "wifi") == "enabled", toggle)
        wired = next((d for d in devs if d[0] == "ethernet"), None)
        if wired:
            return self.pill("network-wired-symbolic", "Wired", wired[2] if wired[1] == "connected" else "Disconnected",
                             wired[1] == "connected", lambda: self.run_and_hide("nm-connection-editor"))
        return None

    def bluetooth_pill(self):
        show = sh("bluetoothctl", "show")
        if "Controller" not in show:
            return None

        def connected():
            names = [line.split(" ", 2)[2] for line in sh("bluetoothctl", "devices", "Connected").splitlines()
                     if line.count(" ") >= 2]
            return ", ".join(names)

        def toggle():
            on = "Powered: yes" not in sh("bluetoothctl", "show")
            sh("bluetoothctl", "power", "on" if on else "off")
            return on, connected() if on else ""
        on = "Powered: yes" in show
        return self.pill("bluetooth-active-symbolic", "Bluetooth", connected() if on else "", on, toggle)

    def dnd_pill(self):
        def toggle():
            sh("makoctl", "mode", "-t", "do-not-disturb")
            return "do-not-disturb" in sh("makoctl", "mode"), ""
        return self.pill("notifications-disabled-symbolic", "Do Not Disturb", "",
                         "do-not-disturb" in sh("makoctl", "mode"), toggle)

    def power_mode_pill(self):
        if not has("powerprofilesctl"):
            return None
        names = {"power-saver": "Power Saver", "balanced": "Balanced", "performance": "Performance"}
        order = list(names)

        def cycle():
            cur = sh("powerprofilesctl", "get")
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "balanced"
            sh("powerprofilesctl", "set", nxt)
            return nxt != "balanced", names[nxt]
        cur = sh("powerprofilesctl", "get")
        return self.pill("power-profile-balanced-symbolic", "Power Mode", names.get(cur, cur), cur != "balanced", cycle)

    # --- date menu -------------------------------------------------------
    def build_calendar(self):
        box = Gtk.Box(spacing=16)
        box.append(self.notifications_column())
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=320)
        today = date.today()
        right.append(label(today.strftime("%A"), "dim"))
        right.append(label(f"{today.strftime('%B')} {today.day} {today.year}", "big"))
        self.weather_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card"])
        right.append(self.weather_box)
        right.append(Gtk.Calendar(show_week_numbers=False))
        box.append(right)
        self.show_weather()
        return box

    def notifications(self):
        state = load_state()
        seen, out = set(), []
        for cmd in (("makoctl", "list", "-j"), ("makoctl", "history", "-j")):
            try:
                items = json.loads(sh(*cmd) or "[]")
            except ValueError:
                items = []
            for n in items:
                if n["id"] not in seen and n["id"] > state["cleared_upto"] and n["id"] not in state["hidden"]:
                    seen.add(n["id"])
                    out.append(n)
        return sorted(out, key=lambda n: n["id"], reverse=True)

    def notifications_column(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=400)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=480, vexpand=True)
        lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller.set_child(lst)
        notes = self.notifications()
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER, vexpand=True,
                        visible=not notes, margin_top=60, margin_bottom=60)
        empty.append(icon("preferences-system-notifications-symbolic", 48))
        empty.append(Gtk.Label(label="No Notifications", css_classes=["dim"]))
        col.append(empty)
        col.append(scroller)
        scroller.set_visible(bool(notes))

        def hide(n, card):
            state = load_state()
            state["hidden"] = (state["hidden"] + [n["id"]])[-200:]
            save_state(state)
            lst.remove(card)
            if not lst.get_first_child():
                scroller.set_visible(False)
                empty.set_visible(True)

        for n in notes:
            lst.append(self.notification_card(n, hide))

        bottom = Gtk.Box(spacing=10)
        bottom.append(label("Do Not Disturb", hexpand=False, valign=Gtk.Align.CENTER))
        dnd = Gtk.Switch(active="do-not-disturb" in sh("makoctl", "mode"), valign=Gtk.Align.CENTER)

        def set_dnd(_switch, on):
            sh("makoctl", "mode", "-a" if on else "-r", "do-not-disturb")
            return False  # let GTK move the switch

        dnd.connect("state-set", set_dnd)
        bottom.append(dnd)
        bottom.append(Gtk.Box(hexpand=True))
        clear = Gtk.Button(child=label("Clear"), css_classes=["menuitem"], sensitive=bool(notes))

        def clear_all(*_):
            state = load_state()
            state["cleared_upto"] = max([n["id"] for n in notes] + [state["cleared_upto"]])
            state["hidden"] = []
            save_state(state)
            sh("makoctl", "dismiss", "-a")
            while c := lst.get_first_child():
                lst.remove(c)
            scroller.set_visible(False)
            empty.set_visible(True)
            clear.set_sensitive(False)

        clear.connect("clicked", clear_all)
        bottom.append(clear)
        col.append(bottom)
        return col

    def notification_card(self, n, on_close):
        card = Gtk.Box(spacing=12, css_classes=["card"])
        src = n.get("app_icon") or ""
        path = urllib.parse.unquote(src[7:]) if src.startswith("file://") else src if src.startswith("/") else None
        if path and os.path.exists(path):
            img = Gtk.Image.new_from_file(path)
        else:
            name = src or n.get("desktop_entry") or "dialog-information"
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            # monochrome like the rest of the panel when the theme has a symbolic variant
            img = Gtk.Image(icon_name=name + "-symbolic" if theme.has_icon(name + "-symbolic") else name)
        img.set_pixel_size(32)
        img.set_valign(Gtk.Align.START)
        card.append(img)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.append(label(n.get("app_name") or "", "dim"))
        text.append(label(n.get("summary") or "", "heading", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR))
        if n.get("body"):
            text.append(label(n["body"], wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=4,
                              ellipsize=Pango.EllipsizeMode.END, max_width_chars=40))
        card.append(text)
        close = Gtk.Button(child=icon("window-close-symbolic"), css_classes=["flat"], valign=Gtk.Align.START)
        close.connect("clicked", lambda *_: on_close(n, card))
        card.append(close)
        return card

    def show_weather(self):
        box = self.weather_box
        while c := box.get_first_child():
            box.remove(c)
        if self.weather and time.time() - self.weather[0] < WEATHER_TTL:
            data = self.weather[1]
        else:
            box.append(label("Weather", "heading"))
            box.append(label("Loading…", "dim"))
            threading.Thread(target=self.fetch_weather, daemon=True).start()
            return
        if data is None:
            box.append(label("Weather", "heading"))
            box.append(label("Weather is unavailable", "dim"))
            return
        cur = data["current_condition"][0]
        area = data["nearest_area"][0]["areaName"][0]["value"]
        box.append(label(f"Weather · {area}", "heading"))
        now = Gtk.Box(spacing=12)
        now.append(icon(weather_icon(cur["weatherCode"]), 40))
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        txt.append(label(f"{cur['temp_C']}°", "big"))
        txt.append(label(cur["weatherDesc"][0]["value"], "dim"))
        now.append(txt)
        box.append(now)
        days = Gtk.Box(homogeneous=True, spacing=6)
        for d in data["weather"]:
            day = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            day.append(Gtk.Label(label=date.fromisoformat(d["date"]).strftime("%a"), css_classes=["dim"]))
            noon = d["hourly"][len(d["hourly"]) // 2]["weatherCode"] if d.get("hourly") else 116
            day.append(icon(weather_icon(noon), 20))
            day.append(Gtk.Label(label=f"{d['maxtempC']}° / {d['mintempC']}°"))
            days.append(day)
        box.append(days)

    def fetch_weather(self):
        # wttr.in locates you by IP; put a city in ~/.config/niri/panels/weather-location to pin it
        try:
            with open(os.path.join(HERE, "weather-location")) as f:
                loc = urllib.parse.quote(f.read().strip())
        except OSError:
            loc = ""
        try:
            req = urllib.request.Request(f"https://wttr.in/{loc}?format=j1", headers={"User-Agent": "niri-panels"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
        except (OSError, ValueError):
            data = None
        self.weather = (time.time() if data else time.time() - WEATHER_TTL + 60, data)  # retry failures after 1 min
        GLib.idle_add(lambda: self.menus["calendar"]["win"].get_visible() and self.show_weather() and False)


if __name__ == "__main__":
    sys.exit(Panels().run(sys.argv[:1]))
