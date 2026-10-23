#!/usr/bin/python3
"""System-tray icon for PadShift using the StatusNotifierItem D-Bus protocol.

GTK 4 has no tray API, so PadShift talks to the desktop's StatusNotifierWatcher
directly through Gio. KDE Plasma supports this natively; GNOME needs the
"AppIndicator and KStatusNotifierItem Support" extension. No extra Python
packages are required, and if no watcher is running nothing breaks: the app
simply behaves as it did without a tray icon.

The icon's Status is "Active" (always shown) while the main window is hidden and
"Passive" (tucked away by the desktop) while the window is visible.

All D-Bus callbacks run on the GLib main loop of the thread that called start(),
so the callbacks passed in by the application are safe to use with GTK.
"""
from __future__ import annotations

import os

from gi.repository import Gio, GLib

ITEM_IFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
MENU_PATH = "/StatusNotifierMenu"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

# Menu item ids for the dbusmenu layout.
MENU_ROOT, MENU_SHOW, MENU_STATUS, MENU_SEPARATOR, MENU_QUIT = 0, 1, 2, 3, 4

ITEM_XML = f"""
<node>
  <interface name="{ITEM_IFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = f"""
<node>
  <interface name="{MENU_IFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{{sv}}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{{sv}})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{{sv}})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""


def _s(value):
    return GLib.Variant("s", value)


def _b(value):
    return GLib.Variant("b", bool(value))


class TrayIcon:
    """A StatusNotifierItem with a small dbusmenu (Show, status line, Quit)."""

    def __init__(self, app_id, title, icon_name, on_show, on_quit):
        self.app_id = app_id
        self.title = title
        self.icon_name = icon_name
        self._on_show = on_show
        self._on_quit = on_quit
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.status = "Passive"
        self.status_text = ""
        self.registered = False  # True once a StatusNotifierWatcher accepted the item
        self._revision = 1
        self._connection = None
        self._item_reg = self._menu_reg = 0
        self._owner_id = self._watch_id = 0
        self._name_acquired = False
        self._watcher_present = False

    # --- lifecycle -------------------------------------------------------
    @property
    def started(self):
        return self._connection is not None

    def start(self):
        """Export the item on the session bus. Returns False if D-Bus is unavailable."""
        if self._connection is not None:
            return True
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self._item_reg = connection.register_object(
                ITEM_PATH, item_info, self._on_item_call, self._on_item_get, None)
            self._menu_reg = connection.register_object(
                MENU_PATH, menu_info, self._on_menu_call, self._on_menu_get, None)
        except (GLib.Error, TypeError, ValueError):
            self._unregister()
            return False
        self._connection = connection
        self._owner_id = Gio.bus_own_name_on_connection(
            connection, self.bus_name, Gio.BusNameOwnerFlags.NONE,
            self._on_name_acquired, self._on_name_lost)
        self._watch_id = Gio.bus_watch_name_on_connection(
            connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared, self._on_watcher_vanished)
        return True

    def stop(self):
        """Remove the icon from the tray."""
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
        self._watch_id = self._owner_id = 0
        self._unregister()
        self._connection = None
        self.registered = self._name_acquired = self._watcher_present = False

    def _unregister(self):
        connection = self._connection or None
        if connection is None:
            try:
                connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error:
                return
        for attr in ("_item_reg", "_menu_reg"):
            reg = getattr(self, attr)
            if reg:
                connection.unregister_object(reg)
                setattr(self, attr, 0)

    # --- state pushed in by the application ---------------------------------
    def set_window_visible(self, visible):
        """Show the icon prominently only while the main window is hidden."""
        status = "Passive" if visible else "Active"
        if status == self.status:
            return
        self.status = status
        self._emit(ITEM_PATH, ITEM_IFACE, "NewStatus", GLib.Variant("(s)", (status,)))

    def set_status_text(self, text):
        """Update the tooltip and the disabled status line in the menu."""
        if text == self.status_text:
            return
        self.status_text = text
        self._emit(ITEM_PATH, ITEM_IFACE, "NewToolTip", None)
        self._revision += 1
        self._emit(MENU_PATH, MENU_IFACE, "LayoutUpdated",
                   GLib.Variant("(ui)", (self._revision, MENU_ROOT)))

    def _emit(self, path, iface, name, params):
        if self._connection is None:
            return
        try:
            self._connection.emit_signal(None, path, iface, name, params)
        except GLib.Error:
            pass

    # --- watcher registration ---------------------------------------------
    def _on_name_acquired(self, *_args):
        self._name_acquired = True
        self._try_register()

    def _on_name_lost(self, *_args):
        self._name_acquired = False
        self.registered = False

    def _on_watcher_appeared(self, *_args):
        self._watcher_present = True
        self._try_register()

    def _on_watcher_vanished(self, *_args):
        # Plasma/GNOME shell restarted or no tray host exists; re-register when it returns.
        self._watcher_present = False
        self.registered = False

    def _try_register(self):
        if not (self._connection and self._name_acquired and self._watcher_present):
            return
        self._connection.call(
            WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE,
            3000, None, self._on_register_done)

    def _on_register_done(self, connection, result):
        try:
            connection.call_finish(result)
            self.registered = True
        except GLib.Error:
            self.registered = False

    # --- org.kde.StatusNotifierItem ---------------------------------------
    def _item_properties(self):
        empty_pixmap = GLib.Variant("a(iiay)", [])
        return {
            "Category": _s("ApplicationStatus"),
            "Id": _s(self.app_id),
            "Title": _s(self.title),
            "Status": _s(self.status),
            "WindowId": GLib.Variant("u", 0),
            "IconName": _s(self.icon_name),
            "IconPixmap": empty_pixmap,
            "OverlayIconName": _s(""),
            "OverlayIconPixmap": empty_pixmap,
            "AttentionIconName": _s(""),
            "AttentionIconPixmap": empty_pixmap,
            "AttentionMovieName": _s(""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], self.title, self.status_text)),
            "ItemIsMenu": _b(False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }

    def _on_item_get(self, _connection, _sender, _path, _iface, prop):
        return self._item_properties().get(prop)

    def _on_item_call(self, _connection, _sender, _path, _iface, method, _params, invocation):
        invocation.return_value(None)
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._run_callback, self._on_show)

    @staticmethod
    def _run_callback(callback):
        try:
            callback()
        except Exception as exc:  # never let a UI error kill the tray
            print(f"PadShift tray callback failed: {type(exc).__name__}: {exc}")
        return GLib.SOURCE_REMOVE

    # --- com.canonical.dbusmenu ---------------------------------------------
    def _menu_properties(self, item_id):
        if item_id == MENU_ROOT:
            return {"children-display": _s("submenu")}
        if item_id == MENU_SHOW:
            return {"label": _s(f"Show {self.title}"), "enabled": _b(True), "visible": _b(True)}
        if item_id == MENU_STATUS:
            label = self.status_text or "Remapping stopped"
            return {"label": _s(label), "enabled": _b(False), "visible": _b(True)}
        if item_id == MENU_SEPARATOR:
            return {"type": _s("separator"), "visible": _b(True)}
        if item_id == MENU_QUIT:
            return {"label": _s(f"Quit {self.title}"), "enabled": _b(True), "visible": _b(True),
                    "icon-name": _s("application-exit-symbolic")}
        return None

    def _menu_node(self, item_id, depth):
        props = self._menu_properties(item_id)
        if props is None:
            return None
        children = []
        if item_id == MENU_ROOT and depth != 0:
            for child_id in (MENU_SHOW, MENU_STATUS, MENU_SEPARATOR, MENU_QUIT):
                children.append(GLib.Variant("(ia{sv}av)", self._menu_node(child_id, depth - 1 if depth > 0 else depth)))
        return (item_id, props, children)

    def _on_menu_get(self, _connection, _sender, _path, _iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": _s("ltr"),
            "Status": _s("normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _on_menu_call(self, _connection, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            parent_id, depth, _names = params.unpack()
            node = self._menu_node(parent_id, depth)
            if node is None:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(), Gio.DBusError.INVALID_ARGS, f"Unknown menu id {parent_id}")
                return
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, node)))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            ids = ids or [MENU_SHOW, MENU_STATUS, MENU_SEPARATOR, MENU_QUIT]
            props = [(i, self._menu_properties(i)) for i in ids if self._menu_properties(i) is not None]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (props,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            props = self._menu_properties(item_id) or {}
            if name in props:
                invocation.return_value(GLib.Variant("(v)", (props[name],)))
            else:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(), Gio.DBusError.INVALID_ARGS, f"No property {name}")
        elif method == "Event":
            item_id, event_id, _data, _timestamp = params.unpack()
            invocation.return_value(None)
            self._handle_menu_event(item_id, event_id)
        elif method == "EventGroup":
            (events,) = params.unpack()
            invocation.return_value(GLib.Variant("(ai)", ([],)))
            for item_id, event_id, _data, _timestamp in events:
                self._handle_menu_event(item_id, event_id)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, f"Unknown method {method}")

    def _handle_menu_event(self, item_id, event_id):
        if event_id != "clicked":
            return
        if item_id == MENU_SHOW:
            GLib.idle_add(self._run_callback, self._on_show)
        elif item_id == MENU_QUIT:
            GLib.idle_add(self._run_callback, self._on_quit)
