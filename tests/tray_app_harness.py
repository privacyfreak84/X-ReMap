"""End-to-end harness for test_tray.py: the real PadShift GTK app plus a fake tray host.

Run as:  dbus-run-session -- xvfb-run -a python3 tests/tray_app_harness.py
Uses a throwaway HOME so the user's profiles and settings are never touched.
"""
import faulthandler
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

faulthandler.dump_traceback_later(45, exit=True)  # never hang: print where we are stuck, then exit
HOME = tempfile.mkdtemp(prefix="padshift-home-")
os.environ["HOME"] = HOME
os.environ["XDG_CONFIG_HOME"] = str(Path(HOME) / ".config")
os.environ.setdefault("GSK_RENDERER", "cairo")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gi.repository import Gio, GLib  # noqa: E402

import padshift_gtk  # noqa: E402

# Hermetic: pretend no controller is connected so the app can never scan /dev/input, grab a
# real gamepad, or start the remapping daemon during this test.
padshift_gtk.core.discover_controllers = lambda: []
import padshift_tray as tray  # noqa: E402

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>
"""
registered, errors, notes = [], [], []
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def watcher_call(_c, _s, _p, _i, _m, params, invocation):
    registered.append(params.unpack()[0])
    invocation.return_value(None)


bus.register_object(tray.WATCHER_PATH, Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0],
                    watcher_call, None, None)
Gio.bus_own_name_on_connection(bus, tray.WATCHER_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

state = {"exit": None}


def check(cond, message):
    if not cond:
        errors.append(message)


def wait(predicate, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def on_main(fn):
    """Run fn on the GTK main thread and return its result."""
    box, done = [], threading.Event()
    def run():
        try:
            box.append(fn())
        finally:
            done.set()
        return GLib.SOURCE_REMOVE
    GLib.idle_add(run)
    done.wait(5)
    return box[0] if box else None


def item_prop(name):
    name_owner = registered[0]
    return bus.call_sync(name_owner, tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (tray.ITEM_IFACE, name)), None,
                         Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]


def name_has_owner(name):
    return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                         "NameHasOwner", GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
                         Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]


def drive():
    try:
        check(wait(lambda: registered and app.window is not None), "tray never registered / window never built")
        item = registered[0]
        check(app.current_device is None and app.worker is None, "test must not touch real controllers")
        check(wait(lambda: app.tray is not None and app.tray.registered), "app tray not registered")
        check(item_prop("Status") == "Passive", "icon should be Passive while the window is visible")
        check(item_prop("IconName") == padshift_gtk.APP_ID, "tray icon name should be the app id")

        # 1. Closing the window hides it, keeps the app alive, and the icon becomes Active.
        on_main(lambda: app.window.close())
        check(wait(lambda: item_prop("Status") == "Active"), "icon not Active after the window was closed")
        check(on_main(lambda: app.window.get_visible()) is False, "window should be hidden after close")
        time.sleep(1.5)
        check(state["exit"] is None, "app exited when the window was closed (should keep running)")
        check(name_has_owner(item), "tray bus name vanished while app still running")

        # 2. Clicking the tray icon restores the window and the icon goes Passive again.
        bus.call_sync(item, tray.ITEM_PATH, tray.ITEM_IFACE, "Activate", GLib.Variant("(ii)", (0, 0)),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
        check(wait(lambda: on_main(lambda: app.window.get_visible()) is True), "tray click did not show the window")
        check(wait(lambda: item_prop("Status") == "Passive"), "icon not Passive after the window re-opened")

        # 3. Hide again, then Quit from the tray menu while the window is hidden.
        on_main(lambda: app.window.close())
        check(wait(lambda: item_prop("Status") == "Active"), "icon not Active after second close")
        bus.call_sync(item, tray.MENU_PATH, tray.MENU_IFACE, "Event",
                      GLib.Variant("(isvu)", (tray.MENU_QUIT, "clicked", GLib.Variant("i", 0), 0)),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
        check(wait(lambda: state["exit"] is not None, 6), "tray Quit did not stop the app")
        check(wait(lambda: not name_has_owner(item), 3), "tray icon still on the bus after quitting")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        GLib.idle_add(app.quit)


# Settings: background mode on (the default) in the throwaway profile.
Path(HOME, ".config", "padshift").mkdir(parents=True, exist_ok=True)
Path(HOME, ".config", "padshift", "settings.json").write_text(
    json.dumps({"keep_background": True, "auto_activate": False}))
app = padshift_gtk.PadShiftGtkApp()
threading.Thread(target=drive, daemon=True).start()
GLib.timeout_add_seconds(40, lambda: (errors.append("harness timed out"), app.quit()))
state["exit"] = app.run([])
if errors:
    print("TRAY APP HARNESS FAILURES:\n  " + "\n  ".join(errors))
    sys.exit(1)
faulthandler.cancel_dump_traceback_later()
print("TRAY APP HARNESS OK")
