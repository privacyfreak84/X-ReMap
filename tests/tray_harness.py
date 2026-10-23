"""Subprocess harness for test_tray.py. Run under `dbus-run-session`.

Acts as a desktop's tray host: it owns org.kde.StatusNotifierWatcher, lets the
real padshift_tray.TrayIcon register, then calls the item and menu over D-Bus
exactly as Plasma / the GNOME AppIndicator extension would.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gi.repository import Gio, GLib  # noqa: E402

import padshift_tray as tray  # noqa: E402

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
  <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
  <property name="ProtocolVersion" type="i" access="read"/>
</interface></node>
"""

registered, events, errors = [], [], []
loop = GLib.MainLoop()
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def watcher_call(_c, _s, _p, _i, method, params, invocation):
    registered.append(params.unpack()[0])
    invocation.return_value(None)


bus.register_object(
    tray.WATCHER_PATH, Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0], watcher_call,
    lambda *a: GLib.Variant("b", True) if a[-1] == "IsStatusNotifierHostRegistered" else GLib.Variant("i", 0), None)
Gio.bus_own_name_on_connection(bus, tray.WATCHER_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

icon = tray.TrayIcon("io.example.App", "Example", "example-icon",
                     on_show=lambda: events.append("show"), on_quit=lambda: events.append("quit"))
assert icon.start(), "TrayIcon.start() failed"


def check(cond, message):
    if not cond:
        errors.append(message)


def call(path, iface, method, params=None, reply=None):
    return bus.call_sync(icon.bus_name, path, iface, method, params, reply,
                         Gio.DBusCallFlags.NONE, 3000, None)


def prop(name):
    return bus.call_sync(icon.bus_name, tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (tray.ITEM_IFACE, name)), None,
                         Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]


def wait(predicate, seconds=3.0):
    import time
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def client():
    try:
        check(wait(lambda: registered), "item never registered with the watcher")
        check(registered == [icon.bus_name], f"registered wrong name: {registered}")
        check(wait(lambda: icon.registered), "TrayIcon.registered never became True")

        # Properties a tray host reads.
        check(prop("Id") == "io.example.App", "Id")
        check(prop("IconName") == "example-icon", "IconName")
        check(prop("Menu") == tray.MENU_PATH, "Menu path")
        check(prop("ItemIsMenu") is False, "ItemIsMenu")
        check(prop("Status") == "Active", "tray icon should be Active while the window is visible")

        # The item must remain visible in the tray both before and after hiding.
        GLib.idle_add(icon.set_window_visible, False)
        check(wait(lambda: prop("Status") == "Active"), "status not Active after hide")
        GLib.idle_add(icon.set_window_visible, True)
        check(wait(lambda: prop("Status") == "Active"), "status not Active after show")

        # Menu layout, as Plasma requests it.
        reply = call(tray.MENU_PATH, tray.MENU_IFACE, "GetLayout",
                     GLib.Variant("(iias)", (0, -1, [])), GLib.VariantType("(u(ia{sv}av))"))
        revision, (root_id, root_props, children) = reply.unpack()
        labels = [(c[0], c[1].get("label"), c[1].get("type"), c[1].get("enabled")) for c in children]
        check(root_id == 0 and root_props["children-display"] == "submenu", "root node")
        check([c[0] for c in children] == [1, 2, 3, 4], f"menu ids: {labels}")
        check(labels[0][1] == "Show Example", f"show label: {labels[0]}")
        check(labels[1][3] is False, "status line must be disabled")
        check(labels[2][2] == "separator", "separator")
        check(labels[3][1] == "Quit Example", f"quit label: {labels[3]}")

        # A status text change bumps the layout revision and appears in the menu.
        GLib.idle_add(icon.set_status_text, "Remapping active")
        check(wait(lambda: icon.status_text == "Remapping active"), "status text not set")
        reply = call(tray.MENU_PATH, tray.MENU_IFACE, "GetLayout",
                     GLib.Variant("(iias)", (0, -1, [])), GLib.VariantType("(u(ia{sv}av))"))
        new_revision, (_i, _p, children) = reply.unpack()
        check(new_revision > revision, "layout revision did not increase")
        check(children[1][1]["label"] == "Remapping active", "status label not updated")
        check(prop("ToolTip")[3] == "Remapping active", "tooltip text not updated")

        # Clicks: left-click Activate, menu "Show", menu "Quit" (via Event and EventGroup).
        call(tray.ITEM_PATH, tray.ITEM_IFACE, "Activate", GLib.Variant("(ii)", (0, 0)))
        check(wait(lambda: events == ["show"]), f"Activate -> {events}")
        call(tray.MENU_PATH, tray.MENU_IFACE, "Event",
             GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("i", 0), 0)))
        check(wait(lambda: events == ["show", "show"]), f"menu Show -> {events}")
        call(tray.MENU_PATH, tray.MENU_IFACE, "Event",
             GLib.Variant("(isvu)", (4, "hovered", GLib.Variant("i", 0), 0)))
        call(tray.MENU_PATH, tray.MENU_IFACE, "EventGroup",
             GLib.Variant("(a(isvu))", ([(4, "clicked", GLib.Variant("i", 0), 0)],)))
        check(wait(lambda: events == ["show", "show", "quit"]), f"menu Quit -> {events}")
        # Unknown ids must be rejected rather than crash the item.
        try:
            call(tray.MENU_PATH, tray.MENU_IFACE, "GetLayout", GLib.Variant("(iias)", (99, 1, [])),
                 GLib.VariantType("(u(ia{sv}av))"))
            check(False, "unknown menu id should raise a D-Bus error")
        except GLib.Error:
            pass

        # stop() must free the bus name so the icon disappears from the tray.
        done = threading.Event()
        GLib.idle_add(lambda: (icon.stop(), done.set(), GLib.SOURCE_REMOVE)[-1])
        check(done.wait(3), "stop() did not run")
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (icon.bus_name,)),
                              GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        check(wait(lambda: not Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (icon.bus_name,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]), "bus name still owned after stop()")
    except Exception as exc:  # report any unexpected failure instead of hanging
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        GLib.idle_add(loop.quit)


threading.Thread(target=client, daemon=True).start()
GLib.timeout_add_seconds(20, lambda: (errors.append("harness timed out"), loop.quit()))
loop.run()
if errors:
    print("TRAY HARNESS FAILURES:\n  " + "\n  ".join(errors))
    sys.exit(1)
print("TRAY HARNESS OK")
