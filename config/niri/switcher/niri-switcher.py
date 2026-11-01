#!/usr/bin/python3
"""GNOME-style Alt+Tab for niri: app icons grouped by app_id, MRU order,
Down shows the app's windows as previews. Runs as a daemon; niri binds trigger
it with gdbus (org.gtk.Actions.Activate next|prev|window|prev-window).

Previews come from niri's org.gnome.Mutter.ScreenCast API (the one the portal
uses): one-frame window cast -> gst pipewiresrc -> PNG. Works for off-screen
windows and never touches the clipboard.
"""
import ctypes.util
import json
import os
import shutil
import socket
import sys

LAYER_SHELL = ctypes.util.find_library("gtk4-layer-shell") or sys.exit("niri-switcher: install gtk4-layer-shell")
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

APP_ID = "io.github.tadeasf.NiriSwitcher"
CACHE = os.path.join(GLib.get_user_cache_dir(), "niri-switcher")
SC = "org.gnome.Mutter.ScreenCast"
HOLD = os.environ.get("NIRI_SWITCHER_HOLD") == "1"  # debug: don't activate on modifier release
MODS = Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
MOD_KEYS = {Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R}

CSS = b"""
window { background: transparent; }
.panel { background: rgba(30,30,30,0.97); border-radius: 18px; padding: 12px;
         border: 1px solid rgba(255,255,255,0.08); }
.item { border-radius: 12px; padding: 10px; }
.item.selected { background: rgba(255,255,255,0.15); }
label { color: #ffffff; font-family: "Fira Sans"; font-weight: 600; font-size: 11pt; }
.arrow { color: rgba(255,255,255,0.55); font-size: 8pt; min-height: 10px; }
.title { font-size: 10pt; }
"""


def niri(request):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(os.environ["NIRI_SOCKET"])
        s.sendall((json.dumps(request) + "\n").encode())
        reply = json.loads(s.makefile().readline())
    ok = reply.get("Ok")
    return next(iter(ok.values())) if isinstance(ok, dict) else ok  # actions reply "Handled"


def app_info(app_id):
    if not app_id:
        return None
    for cand in (app_id, app_id.lower()):
        try:
            return Gio.DesktopAppInfo.new(cand + ".desktop")
        except TypeError:
            pass
    low = app_id.lower()
    for info in Gio.AppInfo.get_all():
        wm = info.get_startup_wm_class() if isinstance(info, Gio.DesktopAppInfo) else None
        if (wm and wm.lower() == low) or info.get_id().lower().removesuffix(".desktop").endswith(low):
            return info
    return None


def ts(win):
    t = win.get("focus_timestamp") or {}
    return (t.get("secs", 0), t.get("nanos", 0))


def snap_path(win_id):
    return os.path.join(CACHE, f"{win_id}.png")


class Switcher(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.apps = []  # [(app_id, info, [windows MRU])]
        self.sel = 0
        self.win_sel = None  # index in selected app's windows when Down list is open
        self.casting = set()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        shutil.rmtree(CACHE, ignore_errors=True)  # window ids restart with niri
        os.makedirs(CACHE, exist_ok=True)
        for name, fn in (("next", lambda: self.step(1)), ("prev", lambda: self.step(-1)),
                         ("window", lambda: self.step_window(1)), ("prev-window", lambda: self.step_window(-1))):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_, f=fn: f())
            self.add_action(act)

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())

        win = self.window = Gtk.ApplicationWindow(application=self)
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "niri-switcher")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["panel"])
        self.app_row = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER)
        self.name = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
        self.app_panel.append(self.app_row)
        self.app_panel.append(self.name)
        self.win_panel = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER, css_classes=["panel"], visible=False)
        box.append(self.app_panel)
        box.append(self.win_panel)
        win.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        keys.connect("key-released", self.on_release)
        win.add_controller(keys)
        win.connect("notify::is-active", self.on_active)
        # pause niri binds (Super+arrows etc.) while open; Tab/` are then handled in on_key
        win.connect("map", lambda w: w.get_surface().inhibit_system_shortcuts(None))

    def do_activate(self):
        pass  # second launches land here; the daemon is already running

    # --- previews --------------------------------------------------------
    def cast(self, win):
        """Grab one frame of a window via niri's ScreenCast API, then refresh the list."""
        win_id = win["id"]
        ww, wh = win["layout"]["window_size"]
        if win_id in self.casting:
            return
        bus = self.get_dbus_connection()

        def call(path, iface, method, args=None, rtype=None):
            return bus.call_sync(SC, path, iface, method, args, rtype, Gio.DBusCallFlags.NONE, 2000, None)

        try:
            sess = call("/org/gnome/Mutter/ScreenCast", SC, "CreateSession",
                        GLib.Variant("(a{sv})", ({},)), GLib.VariantType("(o)"))[0]
            stream = call(sess, SC + ".Session", "RecordWindow",
                          GLib.Variant("(a{sv})", ({"window-id": GLib.Variant("t", win_id),
                                                    "cursor-mode": GLib.Variant("u", 0)},)),
                          GLib.VariantType("(o)"))[0]
        except GLib.Error as e:
            print("cast failed:", e.message, file=sys.stderr)
            return
        self.casting.add(win_id)
        tmp = snap_path(win_id) + ".tmp.png"
        state = {}

        def stop():
            self.casting.discard(win_id)
            if "sub" in state:
                bus.signal_unsubscribe(state.pop("sub"))
            bus.call(SC, sess, SC + ".Session", "Stop", None, None, Gio.DBusCallFlags.NONE, 2000, None, None)

        def finished(proc, res):
            try:
                proc.wait_check_finish(res)
                os.replace(tmp, snap_path(win_id))
                if self.window.get_visible() and self.win_sel is not None:
                    self.build_windows()
            except (GLib.Error, OSError):
                pass
            if "timeout" in state:
                GLib.source_remove(state.pop("timeout"))
            stop()

        def on_stream(_conn, _sender, _path, _iface, _signal, params):
            bus.signal_unsubscribe(state.pop("sub"))
            proc = Gio.Subprocess.new(
                # niri only offers DMA-BUF, so download through GL; RGB drops translucent backgrounds to black
                ["gst-launch-1.0", "-q", "pipewiresrc", f"path={params[0]}", "num-buffers=1", "!",
                 "glupload", "!", "glcolorconvert", "!", "gldownload", "!", "videoconvert", "!",
                 "videoscale", "!", f"video/x-raw,format=RGB,width=520,height={max(1, round(520 * wh / ww))},pixel-aspect-ratio=1/1", "!",
                 "pngenc", "!", "filesink", f"location={tmp}"],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
            state["proc"] = proc
            proc.wait_check_async(None, finished)

        def give_up():  # no frame (e.g. a minimised client that never redraws)
            state.pop("timeout", None)
            if "proc" in state:
                state["proc"].force_exit()  # finished() then stops the session
            else:
                stop()
            return False

        state["sub"] = bus.signal_subscribe(None, SC + ".Stream", "PipeWireStreamAdded", stream, None,
                                            Gio.DBusSignalFlags.NONE, on_stream)
        state["timeout"] = GLib.timeout_add(3000, give_up)
        call(sess, SC + ".Session", "Start")

    # --- model -----------------------------------------------------------
    def load(self):
        wins = sorted(niri("Windows") or [], key=ts, reverse=True)
        groups = {}
        for w in wins:
            groups.setdefault(w["app_id"] or f"window-{w['id']}", []).append(w)
        self.apps = [(app_id, app_info(app_id), ws) for app_id, ws in groups.items()]

    def icon(self, app_id, info, size):
        img = Gtk.Image(pixel_size=size)
        gicon = info.get_icon() if info else None
        steam = "steam_icon_" + app_id.removeprefix("steam_app_")  # Steam games: app_id steam_app_<appid>
        if gicon and (isinstance(gicon, Gio.FileIcon) or self.icon_theme.has_gicon(gicon)):
            img.set_from_gicon(gicon)
        elif app_id.startswith("steam_app_") and self.icon_theme.has_icon(steam):
            img.set_from_icon_name(steam)
        else:
            img.set_from_icon_name("application-x-executable")
        return img

    def build_apps(self):
        while c := self.app_row.get_first_child():
            self.app_row.remove(c)
        self.app_items = []
        for i, (app_id, info, ws) in enumerate(self.apps):
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["item"])
            item.append(self.icon(app_id, info, 96))
            item.append(Gtk.Label(label="▾" if len(ws) > 1 else "", css_classes=["arrow"]))
            click = Gtk.GestureClick()
            click.connect("released", lambda *_, i=i: (self.select(i), self.commit()))
            item.add_controller(click)
            self.app_row.append(item)
            self.app_items.append(item)

    def build_windows(self):
        while c := self.win_panel.get_first_child():
            self.win_panel.remove(c)
        self.win_items = []
        if self.win_sel is None:
            self.win_panel.set_visible(False)
            return
        app_id, info, ws = self.apps[self.sel]
        for i, w in enumerate(ws):
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["item"])
            frame = Gtk.Box(width_request=260, height_request=160)
            if os.path.exists(snap_path(w["id"])):
                pic = Gtk.Picture.new_for_filename(snap_path(w["id"]))
                pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                pic.set_hexpand(True)
                frame.append(pic)
            else:
                img = self.icon(app_id, info, 64)
                img.set_hexpand(True)
                frame.append(img)
            item.append(frame)
            item.append(Gtk.Label(label=w["title"] or "", ellipsize=Pango.EllipsizeMode.END,
                                  max_width_chars=26, css_classes=["title"]))
            click = Gtk.GestureClick()
            click.connect("released", lambda *_, i=i: (self.select_window(i), self.commit()))
            item.add_controller(click)
            self.win_panel.append(item)
            self.win_items.append(item)
        self.win_panel.set_visible(True)
        self.select_window(self.win_sel)

    def open_windows(self, sel):
        self.win_sel = sel
        self.build_windows()
        for w in self.apps[self.sel][2]:
            self.cast(w)

    def select(self, i):
        self.sel = i % len(self.apps)
        for j, item in enumerate(self.app_items):
            (item.add_css_class if j == self.sel else item.remove_css_class)("selected")
        app_id, info, ws = self.apps[self.sel]
        self.name.set_label(info.get_display_name() if info else (ws[0]["title"] or app_id))

    def select_window(self, i):
        self.win_sel = i % len(self.win_items)
        for j, item in enumerate(self.win_items):
            (item.add_css_class if j == self.win_sel else item.remove_css_class)("selected")

    # --- actions ---------------------------------------------------------
    def open(self):
        self.load()
        if not self.apps:
            return False
        self.win_sel = None
        self.build_apps()
        self.build_windows()
        self.window.set_opacity(0)  # GNOME-like delay: a quick tap switches without a flash
        self.window.present()
        GLib.timeout_add(120, lambda: self.window.set_opacity(1) or False)
        return True

    def step(self, d):
        if not self.window.get_visible():
            if not self.open():
                return
            self.select(1 if d > 0 and len(self.apps) > 1 else d)
        else:
            self.win_sel = None
            self.select(self.sel + d)
            self.build_windows()

    def step_window(self, d):
        if not self.window.get_visible():
            if not self.open():
                return
            self.select(0)
            self.open_windows(1 if d > 0 else -1)
        elif self.win_sel is None:
            self.open_windows(0)
        else:
            self.select_window(self.win_sel + d)

    def commit(self):
        if not self.window.get_visible():
            return
        ws = self.apps[self.sel][2]
        target = ws[self.win_sel if self.win_sel is not None else 0]
        self.hide()
        niri({"Action": {"FocusWindow": {"id": target["id"]}}})

    def hide(self):
        self.window.get_surface().restore_system_shortcuts()
        self.window.set_visible(False)

    def on_key(self, _ctl, keyval, _code, state):
        back = -1 if state & Gdk.ModifierType.SHIFT_MASK else 1
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self.step(back)
        elif keyval in (Gdk.KEY_grave, Gdk.KEY_asciitilde, Gdk.KEY_dead_grave):
            self.step_window(back)
        elif keyval == Gdk.KEY_Escape:
            self.hide()
        elif keyval in (Gdk.KEY_Right, Gdk.KEY_Left):
            d = 1 if keyval == Gdk.KEY_Right else -1
            if self.win_sel is None:
                self.select(self.sel + d)
            else:
                self.select_window(self.win_sel + d)
        elif keyval == Gdk.KEY_Down and self.win_sel is None:
            self.open_windows(0)
        elif keyval == Gdk.KEY_Up and self.win_sel is not None:
            self.win_sel = None
            self.build_windows()
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.commit()
        else:
            return False
        return True

    def on_release(self, _ctl, keyval, _code, _state):
        if keyval in MOD_KEYS and not HOLD:
            self.commit()

    def on_active(self, win, _pspec):
        # Alt may already be up by the time we get keyboard focus (quick tap)
        if win.is_active() and not HOLD:
            state = Gdk.Display.get_default().get_default_seat().get_keyboard().get_modifier_state()
            if not state & MODS:
                self.commit()


if __name__ == "__main__":
    sys.exit(Switcher().run(sys.argv[:1]))
