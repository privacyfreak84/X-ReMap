#!/usr/bin/python3
"""PadShift GTK4/libadwaita desktop UI. The input engine remains in padshift.py."""
from __future__ import annotations

import configparser
import json
import math
import os
import queue
import select
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import cairo

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
        raise RuntimeError("libadwaita 1.5 or newer is required for the native dialog API")
except (ImportError, ValueError, RuntimeError) as exc:  # pragma: no cover - depends on the host desktop
    print("PadShift requires GTK4, libadwaita and PyGObject. On Fedora run: sudo dnf install gtk4 libadwaita python3-gobject python3-cairo", file=sys.stderr)
    print(f"GTK startup error: {exc}", file=sys.stderr)
    raise SystemExit(2)

import padshift as core

APP_VERSION = "0.12.0"
APP_ID = "io.github.privacyfreak84.PadShift"
ANIMATION_MODES = ("system", "on", "off")
ANIMATION_LABELS = ["Follow system", "Always on", "Off"]



PART_INFO = {
    "lt": {"label": "Left trigger (LT)", "buttons": ["BTN_TL2"], "axes": ["ABS_Z", "ABS_BRAKE"]},
    "lb": {"label": "Left bumper (LB)", "buttons": ["BTN_TL"], "axes": []},
    "left_stick": {"label": "Left stick", "buttons": [], "axes": ["ABS_X", "ABS_Y"]},
    "left_stick_up": {"label": "Left stick up / forward", "buttons": [], "axes": ["ABS_Y"]},
    "left_stick_down": {"label": "Left stick down / back", "buttons": [], "axes": ["ABS_Y"]},
    "left_stick_left": {"label": "Left stick left", "buttons": [], "axes": ["ABS_X"]},
    "left_stick_right": {"label": "Left stick right", "buttons": [], "axes": ["ABS_X"]},
    "left_stick_click": {"label": "Left stick click (L3)", "buttons": ["BTN_THUMBL"], "axes": []},
    "dpad": {"label": "D-pad", "buttons": ["BTN_DPAD_UP", "BTN_DPAD_DOWN", "BTN_DPAD_LEFT", "BTN_DPAD_RIGHT"], "axes": ["ABS_HAT0X", "ABS_HAT0Y"]},
    "dpad_up": {"label": "D-pad up", "buttons": ["BTN_DPAD_UP"], "axes": ["ABS_HAT0Y"]},
    "dpad_down": {"label": "D-pad down", "buttons": ["BTN_DPAD_DOWN"], "axes": ["ABS_HAT0Y"]},
    "dpad_left": {"label": "D-pad left", "buttons": ["BTN_DPAD_LEFT"], "axes": ["ABS_HAT0X"]},
    "dpad_right": {"label": "D-pad right", "buttons": ["BTN_DPAD_RIGHT"], "axes": ["ABS_HAT0X"]},
    "view": {"label": "View / Back", "buttons": ["BTN_SELECT"], "axes": []},
    "menu": {"label": "Menu / Start", "buttons": ["BTN_START"], "axes": []},
    "guide": {"label": "Xbox / Guide button", "buttons": ["BTN_MODE"], "axes": []},
    "right_stick": {"label": "Right stick", "buttons": [], "axes": ["ABS_RX", "ABS_RY"]},
    "right_stick_up": {"label": "Right stick up / forward", "buttons": [], "axes": ["ABS_RY"]},
    "right_stick_down": {"label": "Right stick down / back", "buttons": [], "axes": ["ABS_RY"]},
    "right_stick_left": {"label": "Right stick left", "buttons": [], "axes": ["ABS_RX"]},
    "right_stick_right": {"label": "Right stick right", "buttons": [], "axes": ["ABS_RX"]},
    "right_stick_click": {"label": "Right stick click (R3)", "buttons": ["BTN_THUMBR"], "axes": []},
    "rb": {"label": "Right bumper (RB)", "buttons": ["BTN_TR"], "axes": []},
    "rt": {"label": "Right trigger (RT)", "buttons": ["BTN_TR2"], "axes": ["ABS_RZ", "ABS_GAS"]},
    "face_y": {"label": "Y button", "buttons": ["BTN_NORTH"], "axes": []},
    "face_x": {"label": "X button", "buttons": ["BTN_WEST"], "axes": []},
    "face_a": {"label": "A button", "buttons": ["BTN_SOUTH"], "axes": []},
    "face_b": {"label": "B button", "buttons": ["BTN_EAST"], "axes": []},
}
PART_BUTTON = {part: info["buttons"][0] for part, info in PART_INFO.items() if info["buttons"] and part not in {"dpad", "left_stick", "right_stick"}}
PART_AXIS_SPEC = {
    "left_stick_up": ("ABS_Y", -1, "stick"), "left_stick_down": ("ABS_Y", 1, "stick"),
    "left_stick_left": ("ABS_X", -1, "stick"), "left_stick_right": ("ABS_X", 1, "stick"),
    "right_stick_up": ("ABS_RY", -1, "stick"), "right_stick_down": ("ABS_RY", 1, "stick"),
    "right_stick_left": ("ABS_RX", -1, "stick"), "right_stick_right": ("ABS_RX", 1, "stick"),
    "dpad_up": ("ABS_HAT0Y", -1, "dpad"), "dpad_down": ("ABS_HAT0Y", 1, "dpad"),
    "dpad_left": ("ABS_HAT0X", -1, "dpad"), "dpad_right": ("ABS_HAT0X", 1, "dpad"),
    "lt": ("ABS_Z", 1, "trigger"), "rt": ("ABS_RZ", 1, "trigger"),
}

# Click zones are in the tightly-cropped controller artwork's 1408 × 937 space.
# Painting and hit-testing share a single transform, so the clickable regions stay
# aligned when the image is resized or the window adapts to a narrower layout.
CONTROLLER_ART_WIDTH = 1408
CONTROLLER_ART_HEIGHT = 937
HIT_AREAS = [
    ("lt", 245, 0, 385, 62), ("lb", 390, 16, 535, 88),
    ("rt", 1030, 0, 1170, 62), ("rb", 872, 16, 1019, 88),
    ("dpad_up", 468, 390, 558, 468), ("dpad_down", 468, 530, 560, 588),
    ("dpad_left", 423, 464, 475, 534), ("dpad_right", 554, 464, 609, 534),
    ("left_stick_up", 305, 177, 391, 222), ("left_stick_down", 305, 310, 391, 357),
    ("left_stick_left", 250, 225, 304, 309), ("left_stick_right", 392, 225, 446, 309),
    ("left_stick_click", 312, 225, 387, 304),
    ("right_stick_up", 819, 385, 900, 418), ("right_stick_down", 819, 495, 900, 532),
    ("right_stick_left", 772, 418, 817, 496), ("right_stick_right", 902, 418, 949, 496),
    ("right_stick_click", 820, 419, 900, 494),
    ("view", 575, 249, 626, 300), ("menu", 779, 249, 831, 300),
    ("guide", 667, 78, 741, 152),
    ("face_y", 1017, 146, 1102, 231), ("face_x", 908, 234, 991, 320),
    ("face_b", 1087, 236, 1173, 321), ("face_a", 1007, 327, 1095, 417),
]
# Shape, center and radii for unobtrusive selected/hover rings on the pictured hardware.
HIGHLIGHTS = {
    "lt": ("roundrect", 315, 31, 62, 24), "lb": ("roundrect", 458, 49, 64, 24),
    "rt": ("roundrect", 1100, 31, 62, 24), "rb": ("roundrect", 945, 49, 64, 24),
    "face_y": ("ellipse", 1060, 190, 43, 43), "face_x": ("ellipse", 950, 277, 43, 43),
    "face_b": ("ellipse", 1130, 278, 43, 43), "face_a": ("ellipse", 1050, 371, 43, 43),
    "view": ("ellipse", 600, 274, 28, 28), "menu": ("ellipse", 804, 274, 28, 28),
    "guide": ("ellipse", 703, 116, 38, 38),
    "dpad_up": ("roundrect", 513, 430, 38, 22), "dpad_down": ("roundrect", 513, 554, 38, 22),
    "dpad_left": ("roundrect", 457, 500, 22, 38), "dpad_right": ("roundrect", 574, 500, 22, 38),
    "left_stick_up": ("ellipse", 348, 201, 37, 25), "left_stick_down": ("ellipse", 348, 331, 37, 25),
    "left_stick_left": ("ellipse", 277, 267, 25, 38), "left_stick_right": ("ellipse", 419, 267, 25, 38),
    "left_stick_click": ("ellipse", 348, 265, 44, 44),
    "right_stick_up": ("ellipse", 861, 400, 36, 24), "right_stick_down": ("ellipse", 861, 514, 36, 24),
    "right_stick_left": ("ellipse", 795, 457, 24, 37), "right_stick_right": ("ellipse", 926, 457, 24, 37),
    "right_stick_click": ("ellipse", 861, 457, 43, 43),
}


def label_for_button(code: str) -> str:
    return core.BUTTON_LABELS.get(code, code.replace("BTN_", "").replace("_", " ").title())


def label_for_axis(code: str) -> str:
    return core.AXIS_LABELS.get(code, code)


def short_button(code: str) -> str:
    return label_for_button(code).split(" · ", 1)[0].replace("View / Back", "View").replace("Menu / Start", "Menu")


def short_axis(code: str) -> str:
    replacements = {
        "ABS_X": "Left X", "ABS_Y": "Left Y", "ABS_RX": "Right X", "ABS_RY": "Right Y",
        "ABS_Z": "LT", "ABS_RZ": "RT", "ABS_HAT0X": "D-pad X", "ABS_HAT0Y": "D-pad Y",
        "ABS_GAS": "Gas", "ABS_BRAKE": "Brake",
    }
    return replacements.get(code, label_for_axis(code))


def rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


# --- theme and animation helpers -----------------------------------------------
def _icon_theme():
    display = Gdk.Display.get_default()
    return Gtk.IconTheme.get_for_display(display) if display else None


def set_image_icon(image, name):
    """Use icons from the active theme so they adapt to Adwaita, Breeze and Papirus."""
    theme = _icon_theme()
    if theme is not None and theme.has_icon(name):
        image.set_from_icon_name(name)
        return
    fallback = {
        "padshift-nav-remapper-symbolic": "input-gaming-symbolic",
        "padshift-nav-buttons-symbolic": "input-gaming-symbolic",
        "padshift-nav-axes-symbolic": "preferences-system-symbolic",
        "padshift-nav-activity-symbolic": "utilities-system-monitor-symbolic",
        "padshift-nav-games-symbolic": "applications-games-symbolic",
        "padshift-status-success-symbolic": "emblem-ok-symbolic",
        "padshift-status-warning-symbolic": "dialog-warning-symbolic",
        "padshift-status-error-symbolic": "dialog-error-symbolic",
        "padshift-status-info-symbolic": "dialog-information-symbolic",
    }
    fallback_name = fallback.get(name, "applications-games-symbolic")
    if theme is not None and not theme.has_icon(fallback_name):
        fallback_name = "applications-system-symbolic"
    image.set_from_icon_name(fallback_name)


def icon_image(name, pixel_size=16):
    image = Gtk.Image()
    set_image_icon(image, name)
    image.set_pixel_size(pixel_size)
    return image


def style_is_dark():
    try:
        return bool(Adw.StyleManager.get_default().get_dark())
    except Exception:
        return False


def style_is_high_contrast():
    try:
        return bool(Adw.StyleManager.get_default().get_high_contrast())
    except Exception:
        return False


def accent_rgba(widget):
    """Use the active theme's selection/accent colour, with a restrained fallback."""
    try:
        context = widget.get_style_context()
        for name in ("accent_bg_color", "accent_color", "theme_selected_bg_color"):
            found, color = context.lookup_color(name)
            if found:
                return (color.red, color.green, color.blue, 1.0)
    except Exception:
        pass
    return (0.21, 0.52, 0.90, 1.0)


def kde_animations_enabled():
    """Read Plasma's animation speed when GTK's setting does not reflect KDE motion."""
    if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return None
    try:
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read(Path.home() / ".config" / "kdeglobals", encoding="utf-8")
        factor = parser.get("KDE", "AnimationDurationFactor", fallback=None)
        return True if factor is None else float(factor) > 0.0
    except Exception:
        return None


class ControllerCanvas(Gtk.DrawingArea):
    """Drawing area that keeps enough vertical space for the controller to remain visible."""
    __gtype_name__ = "PadShiftControllerCanvas"
    # Allow the controller canvas to shrink in short windows, retaining its natural size.
    MIN_HEIGHT = 220
    NATURAL_HEIGHT = 360

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (280, 560, -1, -1)
        return (self.MIN_HEIGHT, self.NATURAL_HEIGHT, -1, -1)


class PadShiftGtkApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self.on_activate)
        self._quitting = False
        self.window = None
        self.devices: list[dict] = []
        self.current_device: dict | None = None
        self.settings = core.load_settings_file()
        self.keep_background = bool(self.settings.get("keep_background", True))
        self.saved_device_identity = core.controller_identity(self.settings.get("device_identity", {})) if isinstance(self.settings.get("device_identity"), dict) else {}
        self.profile_name = str(self.settings.get("active_profile", "") or "")
        if not self.profile_name:
            older = [p for p in core.PROFILES_DIR.glob("*.json") if p.stem != "default"] if core.PROFILES_DIR.exists() else []
            self.profile_name = max(older, key=lambda p: p.stat().st_mtime).stem if older else "default"
        self.auto_activate = bool(self.settings.get("auto_activate", True))
        self.profile: dict = {"format": 2, "buttons": {}, "axes": {}, "axis_buttons": {}}
        self.profile_dirty = False
        self.worker = None
        self.worker_buffer = ""
        self.log_lines: list[str] = []
        self.last_input = "No physical input yet"
        self.last_axis = "No stick or trigger movement yet"
        self.capture_queue = queue.Queue()
        self.capture_cancel_event = None
        self.capture_thread = None
        self.capture_serial = 0
        self.capture_target_part = None
        self.last_target_part = None
        self.pending_capture_part = None
        self.resume_after_capture = False
        self.resume_source = 0
        self.user_requested_stop = False
        self.active_input: tuple[str, str] | None = None
        self.selected_part: str | None = None
        self.last_axis_button_direction = "positive"
        self._updating = False
        self._profile_save_timeout = 0
        self._drop_maps: dict[int, list[tuple[str, str]]] = {}
        self.controller_surface = None
        self.controller_artwork_error = None
        self.hover_part = None
        self.animation_mode = str(self.settings.get("animations", "system"))
        if self.animation_mode not in ANIMATION_MODES:
            self.animation_mode = "system"
        self._system_animation_flag = None
        self._animation_note = ""
        self._ring_key = None
        self._ring_progress = 1.0
        self._ring_anim = None
        self._pulse_value = 1.0
        self._pulse_anim = None
        self._pulse_listening_state = False
        self._art_cache = None
        self._draw_error_logged = False
        core.PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # --- application shell -------------------------------------------------
    def on_activate(self, _app):
        if self.window:
            self.window.present()
            return
        self._apply_animation_preference()
        self._load_controller_artwork()
        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("PadShift")
        self.window.set_default_size(1080, 740)
        self.window.set_size_request(420, 320)
        self._build_shell()
        # Repaint the custom canvas when dark mode, contrast or accent changes.
        manager = Adw.StyleManager.get_default()
        for signal_name in ("notify::dark", "notify::high-contrast", "notify::accent-color"):
            try:
                manager.connect(signal_name, self._on_style_changed)
            except (TypeError, ValueError):
                pass
        self.refresh_profiles(load=False)
        self.refresh_devices(initial=True)
        self.load_profile(self.profile_name)
        self._refresh_all()
        self.log(self._animation_note, "info" if self._animations_enabled() else "warning")
        GLib.timeout_add(120, self.poll_worker)
        GLib.timeout_add(350, self.maybe_autostart)
        GLib.timeout_add(1800, self.auto_device_watch)
        self.window.connect("close-request", self.on_close_request)
        self.window.present()

    def _build_shell(self):
        """Build a standard Adwaita navigation shell with adaptive content."""
        self._install_app_actions()
        self.toast_overlay = Adw.ToastOverlay()
        self.split_view = Adw.NavigationSplitView()
        self.split_view.set_sidebar_width_fraction(0.22)
        self.split_view.set_min_sidebar_width(190)
        self.split_view.set_max_sidebar_width(260)

        sidebar_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.nav_list = Gtk.ListBox()
        self.nav_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav_list.set_activate_on_single_click(True)
        self.nav_list.add_css_class("navigation-sidebar")
        self.nav_rows = {}
        self.nav_specs = [
            ("remapper", "padshift-nav-remapper-symbolic", "Visual remapper"),
            ("buttons", "padshift-nav-buttons-symbolic", "Button mappings"),
            ("axes", "padshift-nav-axes-symbolic", "Sticks & axes"),
            ("activity", "padshift-nav-activity-symbolic", "Activity & diagnostics"),
            ("compat", "padshift-nav-games-symbolic", "Games & Bottles"),
        ]
        for key, icon_name, title in self.nav_specs:
            row = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            image = icon_image(icon_name, 16)
            image.set_can_focus(False)
            label = Gtk.Label(label=title)
            label.set_xalign(0)
            label.set_hexpand(True)
            line.append(image)
            line.append(label)
            row.set_child(line)
            row.set_tooltip_text(title)
            self.nav_list.append(row)
            self.nav_rows[key] = row
        nav_scroll = Gtk.ScrolledWindow()
        nav_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        nav_scroll.set_child(self.nav_list)
        nav_scroll.set_vexpand(True)
        sidebar_content.append(nav_scroll)

        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle.new("PadShift", "Controller remapper"))
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_toolbar.set_content(sidebar_content)
        self.sidebar_page = Adw.NavigationPage.new(sidebar_toolbar, "PadShift")

        self.content_toolbar = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()
        # AdwHeaderBar integrates with AdwNavigationSplitView: page titles and
        # the collapsed-layout Back button are supplied by libadwaita itself.
        self.content_menu_button = self._make_main_menu_button()
        self.content_header.pack_end(self.content_menu_button)
        self.content_toolbar.add_top_bar(self.content_header)
        self.stack = Gtk.Stack()
        # A deliberate horizontal slide makes section changes perceptible while
        # staying within standard GTK Stack transitions and system motion settings.
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(280)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.stack.add_named(self._build_remapper_page(), "remapper")
        self.stack.add_named(self._build_buttons_page(), "buttons")
        self.stack.add_named(self._build_axes_page(), "axes")
        self.stack.add_named(self._build_activity_page(), "activity")
        self.stack.add_named(self._build_compat_page(), "compat")
        self.content_toolbar.set_content(self.stack)
        self.content_page = Adw.NavigationPage.new(self.content_toolbar, "Visual remapper")

        self.split_view.set_sidebar(self.sidebar_page)
        self.split_view.set_content(self.content_page)
        self.toast_overlay.set_child(self.split_view)
        self.window.set_content(self.toast_overlay)

        # Keep the navigation sidebar and editor usable at desktop minimum sizes.
        try:
            nav_breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 850sp"))
            nav_breakpoint.add_setter(self.split_view, "collapsed", True)
            self.window.add_breakpoint(nav_breakpoint)
            editor_breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1180sp"))
            editor_breakpoint.add_setter(self.editor_paned, "orientation", Gtk.Orientation.VERTICAL)
            editor_breakpoint.add_setter(self.inspector_panel, "hexpand", True)
            self.window.add_breakpoint(editor_breakpoint)
        except (AttributeError, TypeError) as exc:
            self.log(f"Adaptive breakpoint support unavailable: {exc}", "warning")
            self.editor_paned.set_orientation(Gtk.Orientation.VERTICAL)
            self.inspector_panel.set_hexpand(True)

        self.nav_list.connect("row-activated", self._on_nav_row_activated)
        self.nav_list.select_row(self.nav_rows["remapper"])
        self.stack.set_visible_child_name("remapper")

    def _make_main_menu_button(self):
        menu = Gio.Menu.new()
        menu.append("Preferences", "app.preferences")
        menu.append("Keyboard Shortcuts", "app.shortcuts")
        menu.append("Help", "app.help")
        menu.append("About PadShift", "app.about")
        menu.append("Quit", "app.quit")
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_menu_model(menu)
        button.set_tooltip_text("Main menu")
        try:
            button.update_property_value([Gtk.AccessibleProperty.LABEL], ["Main menu"])
        except (AttributeError, TypeError, ValueError):
            pass
        return button

    def _install_app_actions(self):
        actions = [
            ("save", self._action_save),
            ("save-as", self._action_save_as),
            ("scan", self._action_scan),
            ("preferences", self._action_preferences),
            ("shortcuts", self._action_shortcuts),
            ("help", self._action_help),
            ("about", self._action_about),
            ("reset-profile", self._action_reset_profile),
            ("quit", self._action_quit),
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.save", ["<Primary>s"])
        self.set_accels_for_action("app.save-as", ["<Primary><Shift>s"])
        self.set_accels_for_action("app.scan", ["<Primary>r"])
        self.set_accels_for_action("app.preferences", ["<Primary>comma"])
        self.set_accels_for_action("app.shortcuts", ["<Primary>question"])
        self.set_accels_for_action("app.help", ["F1"])
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def _action_save(self, *_args):
        self.save_profile()

    def _action_save_as(self, *_args):
        self.save_as_profile()

    def _action_quit(self, *_args):
        # Explicit Quit always exits; the title-bar close button may only hide.
        self._quitting = True
        if self.window:
            self.window.close()
        else:
            self.quit()

    def _action_scan(self, *_args):
        self.refresh_devices()

    def _action_reset_profile(self, *_args):
        self.confirm_reset()

    def _action_preferences(self, *_args):
        dialog = Adw.PreferencesDialog()
        dialog.set_title("Preferences")
        page = Adw.PreferencesPage(title="General")
        group = Adw.PreferencesGroup(
            title="Remapping",
            description="Choose whether PadShift should start your selected profile automatically and reconnect after the controller returns.",
        )
        auto = Adw.SwitchRow(title="Keep remapping active", subtitle="Start automatically and reconnect when the controller returns.")
        auto.set_active(self.auto_activate)
        self.preferences_auto_switch = auto
        auto.connect("notify::active", self._on_auto_switch)
        group.add(auto)
        page.add(group)

        background = Adw.PreferencesGroup(
            title="Background operation",
            description="Keep the remapping engine available while the main window is hidden.",
        )
        keep_background = Adw.SwitchRow(
            title="Keep running when the window closes",
            subtitle="Hide PadShift without stopping remapping. Reopen it from the application menu; choose Quit to stop it.",
        )
        keep_background.set_active(self.keep_background)
        keep_background.connect("notify::active", self._on_keep_background_switch)
        background.add(keep_background)
        page.add(background)

        interface = Adw.PreferencesGroup(
            title="Interface",
            description="Page changes, dialogs, and controller highlights use GTK/libadwaita animations.",
        )
        animations = Adw.ComboRow(title="Animations", subtitle=self._animation_note)
        animations.set_model(Gtk.StringList.new(ANIMATION_LABELS))
        animations.set_selected(ANIMATION_MODES.index(self.animation_mode))
        animations.connect("notify::selected", self._on_animation_mode_selected)
        interface.add(animations)
        page.add(interface)
        dialog.add(page)
        dialog.connect("closed", self._on_preferences_closed)
        self._present_dialog(dialog)

    def _on_preferences_closed(self, *_args):
        self.preferences_auto_switch = None

    def _action_shortcuts(self, *_args):
        dialog = Adw.Dialog()
        dialog.set_title("Keyboard Shortcuts")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle.new("Keyboard Shortcuts", ""))
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: dialog.close())
        header.pack_end(close_button)
        toolbar.add_top_bar(header)
        sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        sections.set_margin_top(18)
        sections.set_margin_bottom(18)
        sections.set_margin_start(24)
        sections.set_margin_end(24)
        for section_title, rowspec in [
            ("General", [("Save profile", "<Control>s"), ("Save profile as…", "<Control><Shift>s"),
                          ("Open preferences", "<Control>comma"), ("Show keyboard shortcuts", "<Control>question"),
                          ("Open help", "F1"), ("Quit PadShift", "<Control>q")]),
            ("Controller", [("Scan for controllers", "<Control>r"), ("Open the main menu", "F10")]),
        ]:
            group = Adw.PreferencesGroup(title=section_title)
            for title, accelerator in rowspec:
                row = Adw.ActionRow(title=title)
                shortcut_label = getattr(Adw, "ShortcutLabel", None)
                if shortcut_label is None:
                    shortcut_label = Gtk.ShortcutLabel  # GTK 4 fallback on older libadwaita
                keycap = shortcut_label(accelerator=accelerator)
                keycap.set_valign(Gtk.Align.CENTER)
                row.add_suffix(keycap)
                group.add(row)
            sections.append(group)
        clamp = Adw.Clamp(maximum_size=600)
        clamp.set_child(sections)
        toolbar.set_content(clamp)
        dialog.set_child(toolbar)
        dialog.set_content_width(520)
        dialog.set_content_height(400)
        self._present_dialog(dialog)

    def _action_help(self, *_args):
        try:
            Gio.AppInfo.launch_default_for_uri("https://github.com/privacyfreak84/X-ReMap", None)
        except Exception as exc:
            self.set_status(f"Could not open help: {exc}", "error")

    def _animations_enabled(self):
        """Return GTK/libadwaita's effective animation preference."""
        if self.window is not None:
            try:
                return bool(Adw.get_enable_animations(self.window))
            except (AttributeError, TypeError):
                pass
        settings = Gtk.Settings.get_default()
        try:
            return bool(settings.get_property("gtk-enable-animations")) if settings else False
        except (AttributeError, TypeError):
            return False

    def _apply_animation_preference(self):
        """Respect the desktop motion setting unless the user explicitly overrides it."""
        settings = Gtk.Settings.get_default()
        if settings is None:
            self._animation_note = "Animations: no GTK settings object is available."
            return
        if self._system_animation_flag is None:
            self._system_animation_flag = bool(settings.get_property("gtk-enable-animations"))
        system = self._system_animation_flag
        if self.animation_mode == "on":
            effective, note = True, "Always on (chosen in Preferences)."
        elif self.animation_mode == "off":
            effective, note = False, "Off (chosen in Preferences)."
        elif system:
            effective, note = True, "On - your desktop allows animations."
        elif kde_animations_enabled():
            effective = True
            note = "On - Plasma's animation speed is not instant, even though GTK reported animations off."
        else:
            effective = False
            note = "Off - your desktop disables animations. Choose Always on in Preferences to override."
        if bool(settings.get_property("gtk-enable-animations")) != effective:
            settings.set_property("gtk-enable-animations", effective)
        self._animation_note = note
        row = getattr(self, "_anim_activity_row", None)
        if row is not None:
            row.set_subtitle(note)

    def _on_animation_mode_selected(self, row, _pspec):
        index = row.get_selected()
        if not 0 <= index < len(ANIMATION_MODES):
            return
        self.animation_mode = ANIMATION_MODES[index]
        self._apply_animation_preference()
        row.set_subtitle(self._animation_note)
        # Apply a changed motion preference immediately to any animation already running.
        listening = self.capture_target_part is not None
        self._pulse_listening_state = not listening
        GLib.idle_add(self._sync_pulse_animation, listening)
        if not self._animations_enabled():
            if self._ring_anim is not None:
                try:
                    self._ring_anim.pause()
                except (AttributeError, TypeError):
                    pass
                self._ring_anim = None
            self._ring_progress = 1.0
        elif self._selected_highlight():
            self._ring_progress = 0.0
            GLib.idle_add(self._start_ring_animation)
        self.map_area.queue_draw()
        self._persist_settings()
        self.log(f"Animations: {self._animation_note}")

    def _on_style_changed(self, *_args):
        self._art_cache = None
        if hasattr(self, "map_area"):
            self.map_area.queue_draw()

    def _present_dialog(self, dialog):
        """Present an Adwaita dialog with native animated sheet/dialog transitions.

        Avoid forcing opacity or layering a second fade over the dialog: the previous
        offscreen-opacity approach could make dialog content render blank on some backends.
        """
        try:
            dialog.present(self.window)
        except Exception as exc:
            self.log(f"Dialog presentation failed: {type(exc).__name__}: {exc}", "error")
            self.set_status(f"Could not open dialog: {exc}", "error")

    def _action_about(self, *_args):
        """Present a complete, native libadwaita About dialog."""
        try:
            self._show_about_dialog()
        except Exception as exc:
            # Do not silently fail if a host's libadwaita version differs.
            self.log(f"Could not build the standard About dialog ({type(exc).__name__}: {exc}); trying a simple fallback: {exc}", "error")
            try:
                self._show_simple_about_dialog()
            except Exception as inner:
                self.set_status(f"Could not open About: {inner}", "error")

    def _show_simple_about_dialog(self):
        """Fallback About page using basic widgets if an optional About API is missing."""
        dialog = Adw.Dialog()
        dialog.set_title("About PadShift")
        dialog.set_content_width(420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="PadShift", description="A visual Linux gamepad remapper.")
        for title, value in (
            ("Version", APP_VERSION),
            ("Website", "https://github.com/privacyfreak84/X-ReMap"),
            ("License", "MIT"),
            ("Notice", "Xbox and Xbox One are trademarks of Microsoft Corporation. PadShift is independent and is not affiliated with Microsoft."),
        ):
            row = Adw.ActionRow(title=title, subtitle=value)
            row.set_subtitle_selectable(True)
            group.add(row)
        page.add(group)
        toolbar.set_content(page)
        dialog.set_child(toolbar)
        self._present_dialog(dialog)

    def _show_about_dialog(self):
        """Build and present libadwaita's standard, fully populated About dialog."""
        dialog = Adw.AboutDialog()
        dialog.set_application_name("PadShift")
        dialog.set_application_icon(APP_ID)
        dialog.set_version(APP_VERSION)
        dialog.set_developer_name("PadShift contributors")
        dialog.set_comments(
            "A visual Linux gamepad remapper. Build a controller layout by choosing a "
            "game-side control and learning the physical input that should drive it."
        )
        dialog.set_copyright("Copyright © 2026 PadShift contributors")
        dialog.set_website("https://github.com/privacyfreak84/X-ReMap")
        dialog.set_issue_url("https://github.com/privacyfreak84/X-ReMap/issues")
        dialog.set_support_url("https://github.com/privacyfreak84/X-ReMap/issues")
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.set_developers(["PadShift contributors"])
        dialog.set_artists(["PadShift contributors · custom controller artwork"])
        dialog.set_release_notes_version(APP_VERSION)
        dialog.set_release_notes(
            "<p>Interface and reliability improvements.</p>"
            "<ul>"
            "<li>Controller canvas layout now reserves enough height to remain visible in the adaptive editor.</li>"
            "<li>Controller highlight rings animate on hover/selection and gently pulse while learning an input.</li>"
            "<li>Closing the main window can leave remapping running in the background; use Quit to stop it.</li>"
            "<li>Custom mappings expand and collapse with a native GTK revealer transition.</li>"
            "<li>Animations can follow the desktop, be explicitly enabled, or be disabled in Preferences.</li>"
            "<li>Dialog presentation uses native libadwaita transitions rather than a second opacity layer.</li>"
            "</ul>"
        )
        try:
            dialog.add_credit_section("Built with", ["GTK 4", "libadwaita", "python-evdev"])
            dialog.add_credit_section(
                "Trademark notice",
                ["Xbox and Xbox One are trademarks of Microsoft Corporation. PadShift is an independent project and is not affiliated with or endorsed by Microsoft."],
            )
        except (AttributeError, TypeError):
            # Keep About usable with older libadwaita versions that lack credit sections.
            pass
        debug_info = (
            f"PadShift {APP_VERSION}\n"
            f"GTK 4 / libadwaita\n"
            f"Profile: {self.profile_name}\n"
            f"Controller: {(self.current_device or {}).get('name', 'disconnected')}\n"
            f"Remapping active: {bool(self.worker and self.worker.poll() is None)}\n"
            f"GTK animations enabled: {self._animations_enabled()}"
        )
        try:
            dialog.set_debug_info(debug_info)
        except (AttributeError, TypeError):
            pass
        self._present_dialog(dialog)

    def _on_nav_row_activated(self, _listbox, row):
        for key, nav_row in self.nav_rows.items():
            if nav_row is row:
                self._on_nav_clicked(nav_row, key)
                return

    def _on_nav_clicked(self, _row, key):
        titles = dict((k, title) for k, _icon, title in self.nav_specs)
        title = titles.get(key, "PadShift")
        self.stack.set_visible_child_name(key)
        self.content_page.set_title(title)
        if self.split_view.get_collapsed():
            self.split_view.set_show_content(True)
        for nav_key, row in self.nav_rows.items():
            if row is not self.nav_list.get_selected_row() and nav_key == key:
                self.nav_list.select_row(row)
        if key == "remapper":
            self.map_area.queue_draw()
            self._refresh_inspector()
        elif key == "buttons":
            self._populate_button_rows()
        elif key == "axes":
            self._populate_axis_rows()
        elif key == "compat":
            self.refresh_bottles_compatibility()

    def _box(self, orientation=Gtk.Orientation.VERTICAL, spacing=10):
        return Gtk.Box(orientation=orientation, spacing=spacing)

    def _label(self, text, css=None, xalign=0.0, wrap=False):
        label = Gtk.Label(label=text)
        label.set_xalign(xalign)
        label.set_wrap(wrap)
        label.set_selectable(False)
        style_aliases = {
            "page-title": "title-1",
            "page-subtitle": "body",
            "section-title": "heading",
            "eyebrow": "caption",
            "muted": "dim-label",
            "numeric": "numeric",
        }
        if css:
            label.add_css_class(style_aliases.get(css, css))
        return label

    def _section(self, title=None, subtitle=None):
        """An unframed content section using standard Adwaita type styles and spacing."""
        outer = self._box(spacing=12)
        if title:
            outer.append(self._label(title, "heading", wrap=True))
        if subtitle:
            desc = self._label(subtitle, "dim-label", wrap=True)
            desc.set_max_width_chars(90)
            outer.append(desc)
        return outer

    def _scroller(self, child=None):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(child)
        scroll.set_vexpand(True)
        return scroll

    def _page(self, subtitle):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        if subtitle:
            intro = self._label(subtitle, "body", wrap=True)
            intro.set_max_width_chars(100)
            content.append(intro)
        clamp = Adw.Clamp(maximum_size=1040, tightening_threshold=760)
        clamp.set_child(content)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(clamp)
        scroll.set_vexpand(True)
        return scroll, content

    def _make_dropdown(self, pairs: list[tuple[str, str]], selected_value: str, callback=None, width=-1):
        names = [name for name, _value in pairs]
        drop = Gtk.DropDown.new_from_strings(names)
        selected_index = next((i for i, pair in enumerate(pairs) if pair[1] == selected_value), 0)
        drop.set_selected(selected_index)
        drop.set_hexpand(width == -1)
        if width > 0:
            drop.set_size_request(width, -1)
        self._drop_maps[id(drop)] = pairs
        if callback:
            drop.connect("notify::selected", callback)
        return drop

    def _dropdown_value(self, drop) -> str:
        pairs = self._drop_maps.get(id(drop), [])
        index = drop.get_selected()
        return pairs[index][1] if 0 <= index < len(pairs) else core.DISABLED

    def _button_pairs(self):
        return [("Disabled", core.DISABLED)] + [(f"{label_for_button(k)}  ·  {k}", k) for k in core.BUTTON_TARGETS]

    def _axis_pairs(self):
        return [("Disabled", core.DISABLED)] + [(f"{label_for_axis(k)}  ·  {k}", k) for k in core.AXIS_TARGETS]

    def _build_remapper_page(self):
        page, content = self._page("Choose a game-side control, then press or move the physical control you want to use. Changes are saved automatically.")

        device_group = Adw.PreferencesGroup(title="Controller and profile")
        device_row = Adw.ActionRow(title="Physical controller", subtitle="The controller PadShift reads")
        self.device_drop = Gtk.DropDown.new_from_strings(["Scanning for controllers…"])
        self.device_drop.set_hexpand(False)
        self.device_drop.set_valign(Gtk.Align.CENTER)
        self.device_drop.set_size_request(190, -1)
        self.device_drop.set_tooltip_text("Choose the physical controller")
        self.device_drop.connect("notify::selected", self._on_device_drop_changed)
        device_row.add_suffix(self.device_drop)
        device_row.set_activatable_widget(self.device_drop)
        device_group.add(device_row)

        profile_row = Adw.ActionRow(title="Profile", subtitle="The layout sent to games")
        self.profile_drop = Gtk.DropDown.new_from_strings(["default"])
        self.profile_drop.set_valign(Gtk.Align.CENTER)
        self.profile_drop.set_size_request(190, -1)
        self.profile_drop.set_tooltip_text("Choose a saved controller layout")
        self.profile_drop.connect("notify::selected", self._on_profile_drop_changed)
        profile_row.add_suffix(self.profile_drop)
        profile_row.set_activatable_widget(self.profile_drop)
        device_group.add(profile_row)
        self.scan_button = Gtk.Button(label="Scan devices")
        self.scan_button.set_valign(Gtk.Align.CENTER)
        self.scan_button.set_tooltip_text("Scan for connected controllers (Ctrl+R)")
        self.scan_button.connect("clicked", lambda *_: self.refresh_devices())
        device_group.set_header_suffix(self.scan_button)
        content.append(device_group)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        action_row.set_halign(Gtk.Align.START)
        self.run_button = Gtk.Button(label="Start remapping")
        self.run_button.set_tooltip_text("Start or stop the virtual controller")
        self.run_button.add_css_class("suggested-action")
        self.run_button.connect("clicked", lambda *_: self.toggle_worker())
        action_row.append(self.run_button)
        profile_menu = Gio.Menu.new()
        profile_menu.append("Save profile", "app.save")
        profile_menu.append("Save profile as…", "app.save-as")
        profile_menu.append("Reset profile…", "app.reset-profile")
        profile_menu_button = Gtk.MenuButton(label="Profile")
        profile_menu_button.set_menu_model(profile_menu)
        profile_menu_button.set_tooltip_text("Profile actions")
        action_row.append(profile_menu_button)
        content.append(action_row)

        # A box is more robust here than Gtk.Paned: both panes previously shrank
        # together, which could collapse the controller card inside the scroller.
        self.editor_paned = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)

        map_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        map_column.set_hexpand(True)
        map_column.append(self._label("Controller layout", "heading"))
        self.map_state_label = self._label("Ready to map", "dim-label", wrap=True)
        self.map_state_label.set_xalign(0)
        map_column.append(self.map_state_label)
        self.map_area = ControllerCanvas()
        self.map_area.set_content_width(560)
        self.map_area.set_content_height(373)
        self.map_area.set_hexpand(True)
        self.map_area.set_vexpand(True)
        self.map_area.set_size_request(280, 187)
        self.map_area.set_focusable(False)
        self.map_area.set_tooltip_text("Controller illustration. Use the Game-side control list and Learn input button for keyboard and assistive-technology access.")
        try:
            self.map_area.update_property_value(
                [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                ["Controller layout", "Illustrated controller destinations. Every destination is also available in the Game-side control list below."],
            )
        except (AttributeError, TypeError, ValueError):
            pass
        self.map_area.set_draw_func(self._draw_controller)
        gesture = Gtk.GestureClick.new()
        gesture.set_button(1)
        gesture.connect("pressed", self._map_pressed)
        self.map_area.add_controller(gesture)
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._map_motion)
        motion.connect("leave", self._map_leave)
        self.map_area.add_controller(motion)
        # The canvas paints a theme-aware stage under the transparent controller; the card
        # provides rounded corners without constraining the artwork's aspect ratio.
        artwork_frame = Gtk.Frame()
        artwork_frame.add_css_class("card")
        artwork_frame.set_overflow(Gtk.Overflow.HIDDEN)
        artwork_frame.set_child(self.map_area)
        map_column.append(artwork_frame)
        map_column.append(self._label("Point to a control to highlight it. Click to learn its physical input.", "dim-label", wrap=True))

        destination_group = Adw.PreferencesGroup(title="Choose a destination")
        destination_row = Adw.ActionRow(title="Game-side control", subtitle="Keyboard-accessible alternative to the drawing")
        self.destination_parts = [(info["label"], part) for part, info in PART_INFO.items()
                                  if part not in {"dpad", "left_stick", "right_stick"}]
        self.target_drop = Gtk.DropDown.new_from_strings([name for name, _part in self.destination_parts])
        self.target_drop.set_valign(Gtk.Align.CENTER)
        self.target_drop.set_size_request(190, -1)
        self.target_drop.set_tooltip_text("Choose which game-side control to configure")
        self.target_drop.connect("notify::selected", self._on_destination_drop_changed)
        if self.destination_parts:
            self.selected_part = self.destination_parts[0][1]
        destination_row.add_suffix(self.target_drop)
        destination_row.set_activatable_widget(self.target_drop)
        destination_group.add(destination_row)
        destination_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.learn_button = Gtk.Button(label="Learn input")
        self.learn_button.connect("clicked", self._on_learn_button_clicked)
        destination_actions.append(self.learn_button)
        destination_group.set_header_suffix(destination_actions)
        map_column.append(destination_group)

        self.capture_feedback_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.capture_feedback_icon = icon_image("padshift-status-info-symbolic", 16)
        self.capture_feedback_icon.set_valign(Gtk.Align.START)
        self.capture_feedback_label = self._label("Ready. Select a destination to begin.", "body", wrap=True)
        self.capture_feedback_label.set_hexpand(True)
        self.capture_feedback_box.append(self.capture_feedback_icon)
        self.capture_feedback_box.append(self.capture_feedback_label)
        map_column.append(self.capture_feedback_box)
        self.editor_paned.append(map_column)

        inspector = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.inspector_panel = inspector
        inspector.set_hexpand(False)
        inspector.set_size_request(300, -1)
        self.inspector_title = self._label("Mapping options", "heading", wrap=True)
        inspector.append(self.inspector_title)
        self.inspector_description = self._label("Learn an input to review its route and tuning options.", "dim-label", wrap=True)
        inspector.append(self.inspector_description)
        self.inspector_controls = Adw.PreferencesGroup()
        inspector.append(self.inspector_controls)

        mapping_section = self._section("Custom mappings", "Only changes from the standard layout are shown.")
        self.mapping_count = Gtk.Label(label="0")
        self.mapping_count.add_css_class("numeric")
        self.mapping_count.set_xalign(0)
        mapping_section.append(self.mapping_count)
        self.mapping_list = Gtk.ListBox()
        self.mapping_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.mapping_list.add_css_class("boxed-list")
        map_scroll = Gtk.ScrolledWindow()
        map_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        map_scroll.set_min_content_height(90)
        map_scroll.set_max_content_height(220)
        map_scroll.set_child(self.mapping_list)
        mapping_section.append(map_scroll)
        # Gtk.Expander changes instantly; Revealer gives this frequently-used section motion.
        self.custom_mappings_revealer = Gtk.Revealer()
        self.custom_mappings_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.custom_mappings_revealer.set_transition_duration(250)
        self.custom_mappings_revealer.set_child(mapping_section)
        self.custom_mappings_toggle = Gtk.ToggleButton(label="Show custom mappings")
        self.custom_mappings_toggle.add_css_class("flat")
        self.custom_mappings_toggle.set_halign(Gtk.Align.START)
        self.custom_mappings_toggle.connect("toggled", self._on_custom_mappings_toggled)
        inspector.append(self.custom_mappings_toggle)
        inspector.append(self.custom_mappings_revealer)
        self.editor_paned.append(inspector)
        content.append(self.editor_paned)
        return page

    def _on_custom_mappings_toggled(self, button):
        revealed = button.get_active()
        self.custom_mappings_revealer.set_reveal_child(revealed)
        button.set_label("Hide custom mappings" if revealed else "Show custom mappings")

    def _on_destination_drop_changed(self, dropdown, _pspec):
        if getattr(self, "_updating", False):
            return
        index = dropdown.get_selected()
        if 0 <= index < len(self.destination_parts):
            self.selected_part = self.destination_parts[index][1]
            self.active_input = None
            self.map_area.queue_draw()
            self._refresh_inspector()

    def _on_learn_button_clicked(self, *_args):
        index = self.target_drop.get_selected()
        if 0 <= index < len(self.destination_parts):
            self.begin_capture(self.destination_parts[index][1])

    def _build_buttons_page(self):
        page, content = self._page("Change which game button each physical button sends. These controls are also available through the visual editor.")
        self.button_rows = Adw.PreferencesGroup(title="Physical button → game button", description="Choose a destination for each detected button.")
        content.append(self.button_rows)
        return page

    def _build_axes_page(self):
        page, content = self._page("Route analogue sticks, triggers and D-pad axes. Expand an axis for inversion and response tuning.")
        explanation = self._label("Deadzone filters small unwanted movement. A response curve of 1.00 is linear.", "dim-label", wrap=True)
        content.append(explanation)
        self.axis_rows = Adw.PreferencesGroup(title="Physical axis → game axis", description="Each axis has its own destination and tuning controls.")
        content.append(self.axis_rows)
        return page

    def _build_activity_page(self):
        page, content = self._page("Inspect controller input and copy diagnostics when something is not working as expected.")
        self.activity_group = Adw.PreferencesGroup(title="Live activity")
        self._input_activity_row = Adw.ActionRow(title="Last button event", subtitle="No physical input yet")
        self.activity_group.add(self._input_activity_row)
        self._axis_activity_row = Adw.ActionRow(title="Last axis movement", subtitle="No stick or trigger movement yet")
        self.activity_group.add(self._axis_activity_row)
        self._engine_activity_row = Adw.ActionRow(title="Virtual controller", subtitle="Waiting to start")
        self.engine_status_label = Gtk.Label(label="Stopped")
        self.engine_status_label.set_valign(Gtk.Align.CENTER)
        self._engine_activity_row.add_suffix(self.engine_status_label)
        self.activity_group.add(self._engine_activity_row)
        self._anim_activity_row = Adw.ActionRow(title="Interface animations", subtitle=self._animation_note)
        self.activity_group.add(self._anim_activity_row)
        content.append(self.activity_group)

        log_section = self._section("Activity log", "Recent controller, mapping and virtual-device messages.")
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("monospace")
        self.log_view.set_left_margin(12)
        self.log_view.set_right_margin(12)
        self.log_view.set_top_margin(12)
        self.log_view.set_bottom_margin(12)
        self.log_view.set_tooltip_text("Read-only controller activity log")
        self.log_buffer = self.log_view.get_buffer()
        logscroll = Gtk.ScrolledWindow()
        logscroll.set_min_content_height(220)
        logscroll.set_child(self.log_view)
        log_section.append(logscroll)
        log_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clear = Gtk.Button(label="Clear log")
        clear.set_tooltip_text("Clear the visible activity log")
        clear.connect("clicked", self._clear_log)
        copy = Gtk.Button(label="Copy diagnostics")
        copy.connect("clicked", self._copy_diagnostics)
        log_actions.append(clear)
        log_actions.append(copy)
        log_section.append(log_actions)
        content.append(log_section)
        return page

    def _build_compat_page(self):
        page, content = self._page("PadShift publishes a host-level virtual controller. Sandboxed apps may need permission to access it.")
        status_section = self._section("Bottles compatibility", "Change Flatpak permissions only if a game cannot see PadShift’s virtual controller.")
        self.bottles_status = Gtk.Label(label="Checking Bottles installation…")
        self.bottles_status.set_xalign(0)
        self.bottles_status.set_wrap(True)
        self.bottles_status.set_selectable(True)
        status_section.append(self.bottles_status)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh = Gtk.Button(label="Refresh status")
        refresh.connect("clicked", lambda *_: self.refresh_bottles_compatibility())
        grant = Gtk.Button(label="Enable Flatpak input access…")
        grant.connect("clicked", lambda *_: self.confirm_bottles_access())
        actions.append(refresh)
        actions.append(grant)
        status_section.append(actions)
        content.append(status_section)

        advice = Adw.PreferencesGroup(title="Troubleshooting")
        instructions = [
            ("Start PadShift first", "Some games only detect controllers when they start. Activate remapping before launching the game."),
            ("Restart the game", "If the virtual controller appeared after the game launched, fully quit and reopen the game."),
            ("Check the Bottles sandbox", "For Flatpak Bottles, enable input access above and restart Bottles. The bottle’s Dedicated sandbox may also need to be disabled."),
            ("Avoid double remapping", "Disable Steam Input or other remapping layers for this virtual controller if they change its inputs again."),
        ]
        for title, desc in instructions:
            advice.add(Adw.ActionRow(title=title, subtitle=desc))
        content.append(advice)
        return page

    def refresh_profiles(self, load=True):
        names = sorted(p.stem for p in core.PROFILES_DIR.glob("*.json"))
        if "default" not in names:
            names.insert(0, "default")
        if self.profile_name not in names:
            self.profile_name = names[0] if names else "default"
        self.profile_names = names
        if hasattr(self, "profile_drop"):
            self._updating = True
            self.profile_drop.set_model(Gtk.StringList.new(names))
            self.profile_drop.set_selected(names.index(self.profile_name) if self.profile_name in names else 0)
            self._updating = False
        if load:
            self.load_profile(self.profile_name)

    def profile_path(self, name=None):
        raw_name = name or self.profile_name or "default"
        clean = "".join(ch for ch in raw_name if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_") or "default"
        return core.PROFILES_DIR / f"{clean}.json"

    def load_profile(self, name):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
            self.capture_cancel_event = None
            self.capture_target_part = None
            self.capture_serial += 1
        path = self.profile_path(name)
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"format": 1, "buttons": {}, "axes": {}}
        except Exception as exc:
            raw = {"format": 1, "buttons": {}, "axes": {}}
            self.log(f"Could not load profile {name}: {exc}", "error")
        if self.current_device:
            self.profile = core.merge_profile(raw, self.current_device["buttons"], self.current_device["axes"])
            if not path.exists():
                self.profile = core.default_profile(self.current_device["buttons"], self.current_device["axes"])
                self.profile_dirty = True
                self.save_profile(silent=True)
        else:
            self.profile = raw
        self.profile_name = name
        self.profile_dirty = False if path.exists() else self.profile_dirty
        self.active_input = None
        self._persist_settings()
        self._refresh_all()
        self.log(f"Loaded profile '{name}'.")

    def refresh_devices(self, initial=False):
        if not core.evdev:
            self.set_status("Missing python-evdev dependency. Run the installer again.", "error")
            return
        previous_identity = core.controller_identity(self.current_device) if self.current_device else dict(self.saved_device_identity or {})
        previous_path = self.current_device.get("path") if self.current_device else ""
        self.devices = core.discover_controllers()
        # Avoid adopting our own virtual pad if the remembered physical pad is disconnected.
        physicals = [d for d in self.devices if "padshift virtual" not in d.get("name", "").lower()]
        chosen = None
        matches = [d for d in physicals if core.device_matches_identity(d, previous_identity)] if previous_identity else []
        if matches:
            chosen = max(matches, key=lambda d: (d.get("path") == previous_identity.get("path"), len(d.get("buttons", [])) + len(d.get("axes", {}))))
        elif previous_path:
            chosen = next((d for d in physicals if d.get("path") == previous_path), None)
        elif not previous_identity and physicals:
            xbox = [d for d in physicals if "xbox" in d.get("name", "").lower()]
            chosen = max(xbox or physicals, key=lambda d: len(d.get("buttons", [])) + len(d.get("axes", {})))
        if chosen:
            changed = not self.current_device or self.current_device.get("path") != chosen.get("path")
            self.current_device = chosen
            self.saved_device_identity = core.controller_identity(chosen)
            try:
                raw = json.loads(self.profile_path().read_text(encoding="utf-8")) if self.profile_path().exists() else self.profile
            except Exception:
                raw = self.profile
            self.profile = core.merge_profile(raw, chosen["buttons"], chosen["axes"])
            if self.profile_name == "default" and not self.profile_path().exists():
                self.profile = core.default_profile(chosen["buttons"], chosen["axes"])
                self.save_profile(silent=True)
            if changed:
                self.log(f"Selected {chosen['name']} ({chosen['path']}).")
                if self.worker and self.worker.poll() is None:
                    self._send_worker({"cmd": "set_device", "identity": self.saved_device_identity})
                elif self.auto_activate:
                    GLib.idle_add(self._start_worker_idle, True)
        else:
            # Preserve identity so an unplugged controller can reappear under a new eventN path.
            self.current_device = None
            if previous_identity:
                self.saved_device_identity = previous_identity
        self._update_device_dropdown()
        self._persist_settings()
        self._refresh_all()
        if chosen:
            self.set_status(f"{chosen['name']} connected", "success")
        elif self.saved_device_identity:
            self.set_status(f"Waiting for {self.saved_device_identity.get('name') or 'your controller'} to reconnect…", "warning")
        else:
            self.set_status("Connect your controller and select Scan devices.", "warning")
        self.log(f"Device scan complete: {len(physicals)} controller-like device(s) found.")

    def _update_device_dropdown(self):
        labels = [d["label"] for d in self.devices if "padshift virtual" not in d.get("name", "").lower()]
        physicals = [d for d in self.devices if "padshift virtual" not in d.get("name", "").lower()]
        if not labels:
            labels = ["Waiting for controller…"]
        self._updating = True
        self.device_drop.set_model(Gtk.StringList.new(labels))
        selected = 0
        if self.current_device:
            for i, d in enumerate(physicals):
                if d["path"] == self.current_device["path"]:
                    selected = i; break
        self.device_drop.set_selected(min(selected, len(labels) - 1))
        self._updating = False
        self._device_dropdown_devices = physicals
        self.device_drop.set_sensitive(bool(physicals))

    def _on_device_drop_changed(self, dropdown, _pspec):
        if self._updating:
            return
        items = getattr(self, "_device_dropdown_devices", [])
        i = dropdown.get_selected()
        if not 0 <= i < len(items):
            return
        device = items[i]
        if self.current_device and self.current_device.get("path") == device.get("path"):
            return
        self.current_device = device
        self.saved_device_identity = core.controller_identity(device)
        try:
            raw = json.loads(self.profile_path().read_text(encoding="utf-8")) if self.profile_path().exists() else self.profile
        except Exception:
            raw = self.profile
        self.profile = core.merge_profile(raw, device["buttons"], device["axes"])
        self._persist_settings()
        self._refresh_all()
        self.log(f"Selected physical controller {device['name']} at {device['path']}.")
        if self.worker and self.worker.poll() is None:
            self._send_worker({"cmd": "set_device", "identity": self.saved_device_identity})
        elif self.auto_activate:
            GLib.idle_add(self._start_worker_idle, True)

    def _on_profile_drop_changed(self, dropdown, _pspec):
        if self._updating:
            return
        names = getattr(self, "profile_names", ["default"])
        i = dropdown.get_selected()
        if not 0 <= i < len(names):
            return
        name = names[i]
        if name == self.profile_name:
            return
        self.profile_name = name
        self.load_profile(name)
        if self.worker and self.worker.poll() is None:
            self.save_profile(silent=True)
            self._send_worker({"cmd": "set_profile", "path": str(self.profile_path())})
            self.log(f"Switched active remapper to profile '{name}'.")
        elif self.auto_activate:
            GLib.idle_add(self._start_worker_idle, True)

    def _on_auto_switch(self, switch, _pspec):
        if self._updating:
            return
        self.auto_activate = switch.get_active()
        self._persist_settings()
        if self.auto_activate:
            GLib.idle_add(self._start_worker_idle, True)
        elif self.worker and self.worker.poll() is None:
            self.stop_worker(persist_toggle=False)
        else:
            self.resume_after_capture = False
            if self.resume_source:
                GLib.source_remove(self.resume_source)
                self.resume_source = 0
        state = "enabled" if self.auto_activate else "disabled"
        self.set_status(f"Automatic activation {state}.", "success" if self.auto_activate else "info")

    def _on_keep_background_switch(self, switch, _pspec):
        if self._updating:
            return
        self.keep_background = switch.get_active()
        self._persist_settings()
        if self.keep_background:
            self.set_status(
                "Closing the window will keep PadShift running in the background. "
                "Launch PadShift again from the application menu to reopen it; choose Quit to stop remapping.",
                "success",
            )
        else:
            self.set_status("Closing the window will stop remapping and exit PadShift.", "info")

    def _persist_settings(self):
        try:
            core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            identity = core.controller_identity(self.current_device) if self.current_device else self.saved_device_identity
            data = {"active_profile": self.profile_name or "default", "auto_activate": bool(self.auto_activate), "keep_background": bool(self.keep_background), "animations": self.animation_mode, "device_identity": identity or {}}
            core.SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.settings = data
            if identity:
                self.saved_device_identity = dict(identity)
        except OSError as exc:
            self.log(f"Could not save settings: {exc}", "error")

    # --- profile edits ------------------------------------------------------
    def save_profile(self, silent=False):
        try:
            if self.current_device:
                self.profile = core.merge_profile(self.profile, self.current_device["buttons"], self.current_device["axes"])
            core.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            path = self.profile_path()
            path.write_text(json.dumps(self.profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.profile_dirty = False
            self.refresh_profiles(load=False)
            if not silent:
                self.set_status(f"Saved profile '{self.profile_name}'.", "success")
                self.log(f"Saved profile to {path}.")
            if self.worker and self.worker.poll() is None:
                self._send_worker({"cmd": "reload"})
            self._refresh_mapping_list()
            return True
        except Exception as exc:
            self.set_status(f"Could not save profile: {exc}", "error")
            self.log(f"Profile save failed: {exc}", "error")
            return False

    def save_as_profile(self):
        dialog = Adw.AlertDialog.new("Save profile as", "Choose a name for this controller layout.")
        entry = Gtk.Entry()
        entry.set_placeholder_text("Profile name")
        entry.set_text(self.profile_name if self.profile_name != "default" else "Custom layout")
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("save", "_Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_save_as_response, entry)
        self._present_dialog(dialog)

    def _on_save_as_response(self, dialog, response, entry):
        dialog.close()
        if response != "save":
            return
        clean = "".join(ch for ch in entry.get_text() if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_")
        if not clean:
            self.set_status("Enter a profile name.", "warning")
            return
        self.profile_name = clean
        self._persist_settings()
        if self.save_profile():
            self.refresh_profiles(load=False)
            if self.worker and self.worker.poll() is None:
                self._send_worker({"cmd": "set_profile", "path": str(self.profile_path())})

    def confirm_reset(self):
        dialog = Adw.AlertDialog.new(
            "Reset this profile?",
            "This restores the detected controls to their standard one-to-one layout. Other saved profiles will not change.",
        )
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("reset", "Reset profile")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, response: self.reset_mappings() if response == "reset" else None)
        self._present_dialog(dialog)

    def reset_mappings(self):
        if not self.current_device:
            self.set_status("Connect a controller before resetting mappings.", "warning")
            return
        self.profile = core.default_profile(self.current_device["buttons"], self.current_device["axes"])
        self.profile_dirty = True
        self.save_profile()
        self._refresh_all()
        self.set_status("Mappings reset to standard controls.", "success")

    def _changed(self, save=False):
        self.profile_dirty = True
        if save:
            self._schedule_profile_save()
        else:
            self.set_status("Unsaved changes · Save to apply this layout", "warning")

    def _schedule_profile_save(self):
        if self._profile_save_timeout:
            GLib.source_remove(self._profile_save_timeout)
        self._profile_save_timeout = GLib.timeout_add(350, self._debounced_save)

    def _debounced_save(self):
        self._profile_save_timeout = 0
        self.save_profile(silent=True)
        self.map_area.queue_draw()
        self._refresh_mapping_list()
        self._populate_axis_rows()
        return False

    # --- click-to-learn capture --------------------------------------------
    def _part_info(self, part):
        return PART_INFO.get(part, {"label": part.replace("_", " ").title(), "buttons": [], "axes": []})

    def _part_button_target(self, part):
        return PART_BUTTON.get(part)

    def _visual_axis_spec(self, part):
        return PART_AXIS_SPEC.get(part)

    def _axis_capture_signal(self, source, raw, info, baseline):
        return core.PadShiftApp._axis_capture_signal(self, source, raw, info, baseline)

    def _capture_input_thread(self, device, part, spec, cancel, serial):
        return core.PadShiftApp._capture_input_thread(self, device, part, spec, cancel, serial)

    def _captured_axis_invert(self, source, target, source_sign, target_sign):
        return core.PadShiftApp._captured_axis_invert(self, source, target, source_sign, target_sign)

    def _load_controller_artwork(self):
        """Load the bundled transparent controller artwork before first paint."""
        image_path = Path(__file__).resolve().parent / "assets" / "xbox-one-controller.png"
        try:
            if not image_path.is_file():
                raise FileNotFoundError(f"Controller artwork was not installed: {image_path}")
            # Pycairo ImageSurface has no .status() method. create_from_png()
            # raises cairo.Error itself if decoding fails, so calling .status()
            # here raises AttributeError on every successful load and leaves the
            # controller canvas empty. Validate using the supported dimension API.
            surface = cairo.ImageSurface.create_from_png(str(image_path))
            width, height = surface.get_width(), surface.get_height()
            if width < 100 or height < 100:
                raise RuntimeError(f"Controller artwork has unexpected dimensions: {width}×{height}")
            self.controller_surface = surface
            self.controller_artwork_error = None
        except Exception as exc:
            self.controller_surface = None
            self.controller_artwork_error = str(exc)
            print(f"PadShift artwork warning: {exc}", file=sys.stderr)

    def _controller_transform(self, width, height):
        """Return the exact image-to-widget scale and offset shared by paint and hit testing."""
        image_width = self.controller_surface.get_width() if self.controller_surface else CONTROLLER_ART_WIDTH
        image_height = self.controller_surface.get_height() if self.controller_surface else CONTROLLER_ART_HEIGHT
        available_width = max(1.0, float(width) - 24.0)
        available_height = max(1.0, float(height) - 24.0)
        scale = min(available_width / image_width, available_height / image_height)
        return scale, (width - image_width * scale) / 2.0, (height - image_height * scale) / 2.0

    def _controller_point(self, x, y):
        width, height = self.map_area.get_width(), self.map_area.get_height()
        scale, ox, oy = self._controller_transform(width, height)
        if scale <= 0 or x < ox or y < oy:
            return None
        px, py = (x - ox) / scale, (y - oy) / scale
        if px < 0 or py < 0 or px > CONTROLLER_ART_WIDTH or py > CONTROLLER_ART_HEIGHT:
            return None
        return px, py

    @staticmethod
    def _hit_test_controller(px, py):
        for part, x1, y1, x2, y2 in HIT_AREAS:
            if x1 <= px <= x2 and y1 <= py <= y2:
                return part
        # The rest of the stick/D-pad body selects the group and gives hints;
        # discrete actions live in more precise click zones above.
        if 420 <= px <= 610 and 390 <= py <= 595:
            return "dpad"
        if 245 <= px <= 452 and 176 <= py <= 360:
            return "left_stick"
        if 758 <= px <= 960 and 384 <= py <= 540:
            return "right_stick"
        return None

    def _sync_destination_dropdown(self, part):
        if not hasattr(self, "target_drop"):
            return
        index = next((i for i, (_label, candidate) in enumerate(self.destination_parts) if candidate == part), None)
        if index is not None and self.target_drop.get_selected() != index:
            self._updating = True
            self.target_drop.set_selected(index)
            self._updating = False

    def _map_pressed(self, _gesture, _n_press, x, y):
        point = self._controller_point(x, y)
        if point is None:
            return
        part = self._hit_test_controller(*point)
        if part in {"dpad", "left_stick", "right_stick"}:
            self.selected_part = part
            hint = {
                "dpad": "Choose one of the four D-pad directions to learn it.",
                "left_stick": "Choose a direction on the left stick, or press its centre to map L3.",
                "right_stick": "Choose a direction on the right stick, or press its centre to map R3.",
            }[part]
            self.set_feedback(hint, "info")
            self.map_area.queue_draw()
            return
        if part:
            self._sync_destination_dropdown(part)
            self.begin_capture(part)

    def _map_motion(self, _controller, x, y):
        point = self._controller_point(x, y)
        part = self._hit_test_controller(*point) if point else None
        if part != self.hover_part:
            self.hover_part = part
            self.map_area.set_cursor_from_name("pointer" if part else None)
            self.map_area.queue_draw()
            if part:
                label = self._part_info(part).get("label", part.replace("_", " ").title())
                self.map_area.set_tooltip_text(f"{label} · click to learn physical input")
            else:
                self.map_area.set_tooltip_text("Choose a game-side control, then learn its physical input.")

    def _map_leave(self, *_args):
        if self.hover_part is not None:
            self.hover_part = None
            self.map_area.set_cursor_from_name(None)
            self.map_area.queue_draw()

    def begin_capture(self, part):
        # If the last successful capture scheduled a quiet-period resume, cancel it
        # so a rapid sequence of mappings cannot restart the daemon mid-capture.
        if self.resume_source:
            GLib.source_remove(self.resume_source)
            self.resume_source = 0
        if not self.current_device:
            self.set_feedback("Connect your controller and choose Scan devices first.", "warning")
            return
        # Learning needs exclusive access to the physical event node. Pause the live
        # remapper automatically, learn one input, then resume it without menu hopping.
        if self.worker and self.worker.poll() is None:
            self.pending_capture_part = part
            self.resume_after_capture = True
            self.user_requested_stop = True
            self._send_worker({"cmd": "stop"})
            self.set_feedback("Pausing the live remapper so PadShift can learn this control…", "warning")
            return
        self._begin_capture_now(part)

    def _begin_capture_now(self, part):
        if not self.current_device:
            self.set_feedback("Connect your controller and choose Scan devices first.", "warning")
            return
        spec = {"button": self._part_button_target(part), "axis": None, "direction": None, "mode": "button"}
        axis_spec = self._visual_axis_spec(part)
        if axis_spec:
            spec["axis"], spec["direction"], spec["mode"] = axis_spec
        if not spec["button"] and not spec["axis"]:
            self.set_feedback("Choose a button, trigger, D-pad direction, or stick arrow.", "warning")
            return
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
        self.capture_serial += 1
        serial = self.capture_serial
        cancel = threading.Event()
        self.capture_cancel_event = cancel
        self.capture_target_part = part
        self.selected_part = part
        self.active_input = None
        self.set_feedback(f"Listening for {self._part_info(part)['label']}. Press a physical button or move a stick / trigger…", "warning")
        self.map_state_label.set_text("Listening…")
        # Listening state is conveyed by the adjacent status text.
        self.map_state_label.add_css_class("heading")
        snap = {"path": self.current_device["path"], "buttons": set(self.current_device.get("buttons", [])), "axes": {k: dict(v) for k, v in self.current_device.get("axes", {}).items()}}
        self.capture_thread = threading.Thread(target=self._capture_input_thread, args=(snap, part, spec, cancel, serial), daemon=True, name="PadShift-input-capture")
        self.capture_thread.start()
        self.map_area.queue_draw()
        self._refresh_inspector()

    def cancel_capture(self):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
            self.capture_cancel_event = None
            self.capture_serial += 1
        self.capture_target_part = None
        self.map_state_label.set_text("Ready to map")
        self.set_feedback("Capture cancelled. No mapping was changed.")
        self.map_area.queue_draw()
        self._refresh_inspector()
        if self.resume_after_capture:
            self._schedule_resume_after_capture(500)

    def _poll_capture_queue(self):
        while True:
            try:
                item = self.capture_queue.get_nowait()
            except queue.Empty:
                break
            if item.get("serial") != self.capture_serial:
                continue
            event = item.get("event")
            if event == "opened":
                self.set_feedback(f"Listening to {item.get('device', 'controller')}. Press or move one control now.", "warning")
                self.log("Physical input listener opened successfully.")
            elif event == "activity":
                self.capture_feedback_label.set_text(item.get("message", "Physical movement detected."))
            elif event == "complete":
                self.finish_capture(item)
                # Resume even if learning timed out or failed. A failed capture should
                # never leave the controller paused with no explanation.
                if self.resume_after_capture:
                    self._schedule_resume_after_capture(1500)

    def finish_capture(self, item):
        result, error = item.get("result"), item.get("error")
        timed_out, opened = bool(item.get("timed_out")), bool(item.get("opened"))
        self.capture_cancel_event = None
        self.capture_target_part = None
        self.map_state_label.set_text("Ready to map")
        if error:
            message = f"Input learning failed: {error}"
            self.set_feedback(message, "error")
            self.log(message, "error")
            self._refresh_inspector(); self.map_area.queue_draw(); return
        if timed_out:
            message = "No mappable input within 15 seconds. Move a stick fully or press a button, then try again."
            if item.get("last_activity"):
                message += " Last activity: " + item["last_activity"]
            self.set_feedback(message, "warning")
            self._refresh_inspector(); self.map_area.queue_draw(); return
        if item.get("cancelled") or not result:
            self.set_feedback("Capture cancelled. No mapping changed.")
            self._refresh_inspector(); self.map_area.queue_draw(); return
        if result["kind"] == "button":
            source, target = result["source"], result["target"]
            if self.current_device and source not in self.current_device["buttons"]:
                self.current_device["buttons"].append(source)
                self.current_device["buttons"].sort()
                self.profile = core.merge_profile(self.profile, self.current_device["buttons"], self.current_device["axes"])
            self.profile.setdefault("buttons", {})[source] = target
            self.active_input = ("button", source)
            self.last_target_part = result.get("part")
            self.set_feedback(f"Mapped physical {label_for_button(source)} → game receives {label_for_button(target)}. Saved automatically.", "success")
            self.log(f"Mapped button {source} → {target}.")
        elif result["kind"] == "axis_button":
            source, target = result["source"], result["target"]
            source_sign = int(result["source_sign"])
            conf = self.profile.setdefault("axis_buttons", {}).setdefault(source, {"negative": core.DISABLED, "positive": core.DISABLED, "threshold": 0.55, "invert": False})
            if source in core.TRIGGER_AXES:
                direction = "positive"
                conf["invert"] = bool(result.get("invert", False))
            else:
                direction = "negative" if source_sign < 0 else "positive"
                conf["invert"] = False
            conf[direction] = target
            self.active_input = ("axis_button", source)
            self.last_axis_button_direction = direction
            self.last_target_part = result.get("part")
            self.set_feedback(f"Mapped {label_for_axis(source)} ({direction}) → game receives {label_for_button(target)}. Saved automatically.", "success")
            self.log(f"Mapped axis direction {source}/{direction} → {target}.")
        else:
            source, target = result["source"], result["target"]
            source_sign, target_sign = int(result["source_sign"]), int(result["target_sign"])
            if target in core.STICK_AXES and source not in core.STICK_AXES and source not in core.HAT_AXES:
                self.set_feedback("That input is not a stick axis. Choose the target again and move a stick or D-pad.", "error")
                self._refresh_inspector(); self.map_area.queue_draw(); return
            invert = self._captured_axis_invert(source, target, source_sign, target_sign)
            for other, conf in self.profile.setdefault("axes", {}).items():
                if other != source and conf.get("target") == target:
                    conf["target"] = core.DISABLED
            conf = self.profile.setdefault("axes", {}).setdefault(source, {"target": source, "invert": False, "deadzone": 0.08 if source in core.STICK_AXES else 0.0, "curve": 1.0})
            conf.update({"target": target, "invert": bool(invert)})
            conf.setdefault("deadzone", 0.08 if source in core.STICK_AXES else 0.0)
            conf.setdefault("curve", 1.0)
            self.active_input = ("axis", source)
            self.last_target_part = result.get("part")
            suffix = " Direction automatically inverted." if invert else ""
            self.set_feedback(f"Mapped {label_for_axis(source)} → game receives {label_for_axis(target)}.{suffix} Saved automatically.", "success")
            self.log(f"Mapped axis {source} → {target}; invert={invert}.")
        self.profile_dirty = True
        self.save_profile(silent=True)
        self._refresh_all()
        self.map_area.queue_draw()

    def _schedule_resume_after_capture(self, delay_ms):
        """Restart remapping shortly after learning ends (it is paused while learning)."""
        if self.resume_source:
            GLib.source_remove(self.resume_source)
        self.resume_source = GLib.timeout_add(int(delay_ms), self._resume_remapping_after_capture)

    def _resume_remapping_after_capture(self):
        self.resume_source = 0
        should_resume = self.resume_after_capture
        self.resume_after_capture = False
        if should_resume and not (self.worker and self.worker.poll() is None):
            self.start_worker(automatic=True)
        return GLib.SOURCE_REMOVE

    # --- map drawing --------------------------------------------------------
    @staticmethod
    def _theme_color(area, name, fallback):
        try:
            found, color = area.get_style_context().lookup_color(name)
            if found:
                return (color.red, color.green, color.blue, color.alpha)
        except (AttributeError, TypeError):
            pass
        return fallback

    # --- map drawing --------------------------------------------------------
    def _scaled_artwork(self, scale, device_scale):
        """Cache high-quality artwork scaling so mouse movement stays smooth."""
        key = (round(scale, 3), max(1, int(device_scale)))
        if self._art_cache and self._art_cache[0] == key:
            return self._art_cache[1]
        source = self.controller_surface
        dscale = max(1, int(device_scale))
        px_w = max(1, round(source.get_width() * scale * dscale))
        px_h = max(1, round(source.get_height() * scale * dscale))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, px_w, px_h)
        surface.set_device_scale(dscale, dscale)
        scaler = cairo.Context(surface)
        scaler.scale(px_w / source.get_width() / dscale,
                     px_h / source.get_height() / dscale)
        pattern = cairo.SurfacePattern(source)
        pattern.set_filter(cairo.FILTER_BEST)
        scaler.set_source(pattern)
        scaler.paint()
        self._art_cache = (key, surface)
        return surface

    @staticmethod
    def _paint_stage(ctx, width, height):
        """Paint a theme-aware neutral stage behind the black controller."""
        if style_is_high_contrast():
            inner, outer = (1.0, 1.0, 1.0), (0.91, 0.92, 0.94)
        elif style_is_dark():
            inner, outer = (0.92, 0.93, 0.95), (0.74, 0.77, 0.82)
        else:
            inner, outer = (1.0, 1.0, 1.0), (0.91, 0.92, 0.94)
        gradient = cairo.RadialGradient(
            width / 2, height * 0.45, min(width, height) * 0.1,
            width / 2, height / 2, max(width, height) * 0.75,
        )
        gradient.add_color_stop_rgb(0, *inner)
        gradient.add_color_stop_rgb(1, *outer)
        ctx.save()
        ctx.set_source(gradient)
        ctx.paint()
        ctx.restore()

    @staticmethod
    def _foreground_rgba(widget):
        try:
            color = widget.get_color()
        except (AttributeError, TypeError):
            color = widget.get_style_context().get_color()
        return color.red, color.green, color.blue, color.alpha

    def _selected_highlight(self):
        return self.hover_part or self.capture_target_part or self.selected_part or self.last_target_part

    def _start_ring_animation(self):
        if not self._animations_enabled():
            self._ring_progress = 1.0
            self.map_area.queue_draw()
            return GLib.SOURCE_REMOVE
        if self._ring_anim is not None:
            try:
                self._ring_anim.skip()
            except (AttributeError, TypeError):
                pass
        target = Adw.CallbackAnimationTarget.new(self._on_ring_progress)
        animation = Adw.TimedAnimation.new(self.map_area, 0.0, 1.0, 180, target)
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._ring_anim = animation
        animation.play()
        return GLib.SOURCE_REMOVE

    def _on_ring_progress(self, value, *_args):
        self._ring_progress = value
        self.map_area.queue_draw()

    def _sync_pulse_animation(self, listening):
        """Animate the selected control while waiting for physical input."""
        self._pulse_listening_state = bool(listening)
        if listening and self._animations_enabled() and self._pulse_anim is None:
            target = Adw.CallbackAnimationTarget.new(self._on_pulse_progress)
            animation = Adw.TimedAnimation.new(self.map_area, 0.0, 1.0, 800, target)
            animation.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
            animation.set_repeat_count(0)
            animation.set_alternate(True)
            self._pulse_anim = animation
            animation.play()
        elif (not listening or not self._animations_enabled()) and self._pulse_anim is not None:
            try:
                self._pulse_anim.pause()
            except (AttributeError, TypeError):
                pass
            self._pulse_anim = None
            self._pulse_value = 1.0
            self.map_area.queue_draw()
        return GLib.SOURCE_REMOVE

    def _on_pulse_progress(self, value, *_args):
        self._pulse_value = value
        self.map_area.queue_draw()

    def _draw_message(self, area, ctx, width, height, title, detail):
        ctx.save()
        red, green, blue, _alpha = self._foreground_rgba(area)
        ctx.set_source_rgba(red, green, blue, 0.78)
        ctx.select_font_face("Sans", 0, 0)
        ctx.set_font_size(15)
        ext = ctx.text_extents(title)
        ctx.move_to(max(8, (width - ext.width) / 2), height / 2)
        ctx.show_text(title)
        ctx.set_font_size(11)
        ext = ctx.text_extents(detail)
        ctx.move_to(max(8, (width - ext.width) / 2), height / 2 + 24)
        ctx.show_text(detail)
        ctx.restore()

    def _draw_controller(self, area, ctx, width, height, _data=None):
        """Never let a paint exception turn the controller area into a silent blank."""
        try:
            self._draw_controller_inner(area, ctx, width, height)
        except Exception as exc:
            if not self._draw_error_logged:
                self._draw_error_logged = True
                GLib.idle_add(
                    self.log,
                    f"Controller illustration failed to draw: {type(exc).__name__}: {exc}",
                    "error",
                )
            self._paint_stage(ctx, width, height)
            self._draw_message(area, ctx, width, height,
                               "Controller illustration failed to draw", str(exc))

    def _draw_controller_inner(self, area, ctx, width, height):
        """Draw the bundled Xbox One controller and animated target highlights."""
        scale, ox, oy = self._controller_transform(width, height)
        self._paint_stage(ctx, width, height)
        if scale <= 0:
            return
        if self.controller_surface is None:
            self._draw_message(
                area, ctx, width, height, "Controller artwork could not be loaded",
                getattr(self, "controller_artwork_error", None)
                or "Reinstall PadShift so assets/xbox-one-controller.png is copied.",
            )
            return

        ctx.save()
        artwork = self._scaled_artwork(scale, max(1, area.get_scale_factor()))
        ctx.set_source_surface(artwork, ox, oy)
        ctx.paint()
        ctx.translate(ox, oy)
        ctx.scale(scale, scale)

        selected = self._selected_highlight()
        if selected != self._ring_key:
            self._ring_key = selected
            if self._animations_enabled():
                GLib.idle_add(self._start_ring_animation)
            else:
                self._ring_progress = 1.0
        listening = self.capture_target_part is not None
        if listening != self._pulse_listening_state:
            GLib.idle_add(self._sync_pulse_animation, listening)

        if selected == "dpad":
            selected_parts = ("dpad_up", "dpad_down", "dpad_left", "dpad_right")
        elif selected == "left_stick":
            selected_parts = (
                "left_stick_up", "left_stick_down", "left_stick_left",
                "left_stick_right", "left_stick_click",
            )
        elif selected == "right_stick":
            selected_parts = (
                "right_stick_up", "right_stick_down", "right_stick_left",
                "right_stick_right", "right_stick_click",
            )
        else:
            selected_parts = (selected,) if selected else ()

        accent = accent_rgba(area)
        fade = self._ring_progress
        pulse = self._pulse_value if listening and self._animations_enabled() else 0.0

        def stroke_highlight(shape_type, cx, cy, rx, ry, line_width):
            if shape_type == "roundrect":
                ctx.set_line_width(line_width)
                rounded_rect(ctx, cx - rx, cy - ry, rx * 2, ry * 2, min(12, ry * 0.8))
                ctx.stroke()
            else:
                ctx.save()
                ctx.translate(cx, cy)
                ctx.scale(rx, ry)
                ctx.arc(0, 0, 1, 0, 2 * math.pi)
                ctx.set_line_width(line_width / max(1.0, min(rx, ry)))
                ctx.stroke()
                ctx.restore()

        for part in selected_parts:
            shape = HIGHLIGHTS.get(part)
            if not shape:
                continue
            shape_type, cx, cy, rx, ry = shape
            ctx.save()
            hovering = part == self.hover_part
            halo_alpha = (0.18 if hovering else 0.26) * fade + 0.22 * pulse
            halo_width = (10 if hovering else 13) + 6 * pulse
            ctx.set_source_rgba(accent[0], accent[1], accent[2], min(halo_alpha, 0.6))
            stroke_highlight(shape_type, cx, cy, rx, ry, halo_width)
            ring_alpha = (0.98 if part == selected else 0.72) * fade
            ctx.set_source_rgba(accent[0], accent[1], accent[2], ring_alpha)
            stroke_highlight(shape_type, cx, cy, rx, ry,
                             3.5 if part == selected else 2.5)
            ctx.restore()
        ctx.restore()

    @staticmethod
    def _draw_text(ctx, x, y, text, size=10, color=(1, 1, 1, 1), bold=False):
        ctx.select_font_face("Sans", 0, 1 if bold else 0)
        ctx.set_font_size(size)
        ctx.set_source_rgba(*color)
        ext = ctx.text_extents(text)
        ctx.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        ctx.show_text(text)

    def _refresh_all(self):
        if hasattr(self, "map_area"):
            self.map_area.queue_draw()
            self._refresh_inspector()
            self._refresh_mapping_list()
        if hasattr(self, "button_rows"):
            self._populate_button_rows()
        if hasattr(self, "axis_rows"):
            self._populate_axis_rows()
        if hasattr(self, "engine_status_label"):
            self.engine_status_label.set_text("Active" if self.worker and self.worker.poll() is None else "Stopped")
        if hasattr(self, "_input_activity_row"):
            self._input_activity_row.set_subtitle(self.last_input)
        if hasattr(self, "_axis_activity_row"):
            self._axis_activity_row.set_subtitle(self.last_axis)

    def _on_scale_value(self, scale, label, callback, suffix, multiplier, formatter):
        value = scale.get_value()
        label.set_text(formatter(value) if formatter else f"{value * multiplier:.0f}{suffix}")
        callback(value)

    def _refresh_inspector(self):
        """Show settings for the last learned physical input using Adwaita rows."""
        if not hasattr(self, "inspector_controls"):
            return
        self._clear_preference_group(self.inspector_controls)
        active = self.active_input
        if not active:
            part = self.capture_target_part or self.selected_part
            info = self._part_info(part) if part else {"label": "Mapping options"}
            self.inspector_title.set_text(info.get("label", "Mapping options"))
            self.inspector_description.set_text(
                "Click a game-side control and then use the physical control you want to assign. "
                "After learning an input, its routing and tuning options appear here."
            )
            row = Adw.ActionRow(
                title="No input selected",
                subtitle="Choose a destination, then activate Learn input. Routing options appear here after a control is learned.",
            )
            self._add_preference_row(self.inspector_controls, row)
            return

        kind, source = active
        if kind == "button":
            current = self.profile.get("buttons", {}).get(source, core.DISABLED)
            self.inspector_title.set_text(label_for_button(source))
            self.inspector_description.set_text("Choose which game button this physical button sends.")
            row = Adw.ActionRow(title="Game button", subtitle="Output when this button is pressed")
            drop = self._make_dropdown(self._button_pairs(), current, callback=lambda w, p, src=source: self._on_inspector_button_changed(w, src, p))
            drop.set_valign(Gtk.Align.CENTER)
            row.add_suffix(drop)
            row.set_activatable_widget(drop)
            self._add_preference_row(self.inspector_controls, row)
            return

        if kind == "axis_button":
            conf = self.profile.get("axis_buttons", {}).get(source, {})
            self.inspector_title.set_text(label_for_axis(source))
            self.inspector_description.set_text("This axis is learned as a digital button in the selected direction.")
            for direction, title in (("negative", "Negative direction"), ("positive", "Positive direction")):
                row = Adw.ActionRow(title=title, subtitle="Game button generated by this movement")
                drop = self._make_dropdown(self._button_pairs(), conf.get(direction, core.DISABLED),
                    callback=lambda w, p, src=source, dr=direction: self._on_axis_button_changed(w, src, dr, p))
                drop.set_valign(Gtk.Align.CENTER)
                row.add_suffix(drop)
                row.set_activatable_widget(drop)
                self._add_preference_row(self.inspector_controls, row)
            threshold = float(conf.get("threshold", 0.55))
            threshold_row = Adw.ActionRow(title="Activation threshold", subtitle="How far to move before the button activates")
            threshold_control = self._scale_control("Activation threshold", threshold, 0.15, 0.95, 0.05,
                lambda value, src=source: self._on_axis_button_threshold(src, value), suffix="%", multiplier=100)
            threshold_row.add_suffix(threshold_control)
            self._add_preference_row(self.inspector_controls, threshold_row)
            return

        if kind == "axis":
            conf = self.profile.get("axes", {}).get(source, {})
            target = conf.get("target", core.DISABLED)
            self.inspector_title.set_text(label_for_axis(source))
            self.inspector_description.set_text("Route this physical axis to a game-side axis and tune its response.")
            target_row = Adw.ActionRow(title="Game axis", subtitle="Destination receiving this movement")
            drop = self._make_dropdown(self._axis_pairs(), target, callback=lambda w, p, src=source: self._on_axis_target_changed(w, src, p))
            drop.set_valign(Gtk.Align.CENTER)
            target_row.add_suffix(drop)
            target_row.set_activatable_widget(drop)
            self._add_preference_row(self.inspector_controls, target_row)
            invert_row = Adw.SwitchRow(title="Invert axis", subtitle="Reverse the direction of movement")
            invert_row.set_active(bool(conf.get("invert", False)))
            invert_row.connect("notify::active", lambda w, p, src=source: self._on_axis_invert(src, w.get_active()))
            self._add_preference_row(self.inspector_controls, invert_row)
            deadzone_row = Adw.ActionRow(title="Deadzone", subtitle="Ignore small movements around the centre")
            deadzone = self._scale_control("Deadzone", float(conf.get("deadzone", 0.08 if source in core.STICK_AXES else 0)),
                0.0, 0.75, 0.01, lambda value, src=source: self._on_axis_tune(src, "deadzone", value), suffix="%", multiplier=100)
            deadzone_row.add_suffix(deadzone)
            self._add_preference_row(self.inspector_controls, deadzone_row)
            curve_row = Adw.ActionRow(title="Response curve", subtitle="1.00 is linear")
            curve = self._scale_control("Response curve", float(conf.get("curve", 1.0)), 0.25, 4.0, 0.05,
                lambda value, src=source: self._on_axis_tune(src, "curve", value), formatter=lambda value: f"{value:.2f}")
            curve_row.add_suffix(curve)
            self._add_preference_row(self.inspector_controls, curve_row)
            return

        self.inspector_title.set_text("Mapping options")
        self.inspector_description.set_text("Select a physical input to see its settings.")

    def _scale_control(self, accessible_name, value, minimum, maximum, step, callback,
                       suffix="", multiplier=1.0, formatter=None):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimum, maximum, step)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_value(float(value))
        scale.set_digits(2 if formatter else 0)
        scale.set_tooltip_text(accessible_name)
        try:
            scale.update_property_value([Gtk.AccessibleProperty.LABEL], [accessible_name])
            scale.update_property_value([Gtk.AccessibleProperty.DESCRIPTION], [accessible_name])
        except (AttributeError, TypeError, ValueError):
            pass
        value_label = Gtk.Label(label=formatter(value) if formatter else f"{value * multiplier:.0f}{suffix}")
        value_label.set_xalign(1)
        value_label.set_width_chars(5)
        value_label.add_css_class("numeric")
        value_label.set_valign(Gtk.Align.CENTER)
        scale.connect("value-changed", self._on_scale_value, value_label, callback, suffix, multiplier, formatter)
        box.append(scale)
        box.append(value_label)
        return box

    def _on_inspector_button_changed(self, dropdown, source, _pspec):
        self.profile.setdefault("buttons", {})[source] = self._dropdown_value(dropdown)
        self._changed(save=True)
        self._refresh_mapping_list()
        self.map_area.queue_draw()

    def _on_axis_button_changed(self, dropdown, source, direction, _pspec):
        conf = self.profile.setdefault("axis_buttons", {}).setdefault(source, {"negative": core.DISABLED, "positive": core.DISABLED, "threshold": 0.55, "invert": False})
        conf[direction] = self._dropdown_value(dropdown)
        self._changed(save=True)
        self._refresh_mapping_list()
        self.map_area.queue_draw()

    def _on_axis_button_threshold(self, source, value):
        conf = self.profile.setdefault("axis_buttons", {}).setdefault(
            source, {"negative": core.DISABLED, "positive": core.DISABLED, "threshold": 0.55, "invert": False}
        )
        conf["threshold"] = round(float(value), 3)
        self._changed(save=True)

    def _on_axis_target_changed(self, dropdown, source, _pspec):
        target = self._dropdown_value(dropdown)
        for other, config in self.profile.setdefault("axes", {}).items():
            if other != source and config.get("target") == target and target != core.DISABLED:
                config["target"] = core.DISABLED
        self.profile.setdefault("axes", {}).setdefault(source, {})["target"] = target
        self._changed(save=True)

    def _on_axis_invert(self, source, value):
        self.profile.setdefault("axes", {}).setdefault(source, {})["invert"] = bool(value)
        self._changed(save=True)

    def _on_axis_tune(self, source, key, value):
        conf = self.profile.setdefault("axes", {}).setdefault(source, {})
        conf[key] = round(float(value), 3)
        self._changed(save=True)

    def _refresh_mapping_list(self):
        if not hasattr(self, "mapping_list"):
            return
        child = self.mapping_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.mapping_list.remove(child)
            child = nxt
        mappings = []
        for src, target in self.profile.get("buttons", {}).items():
            if target != src and target != core.DISABLED:
                mappings.append((f"Game {short_button(target)}", f"Physical {short_button(src)}"))
        for src, conf in self.profile.get("axes", {}).items():
            target = conf.get("target", core.DISABLED)
            if target != src and target != core.DISABLED:
                extra = " · inverted" if conf.get("invert") else ""
                mappings.append((f"Game {short_axis(target)}", f"Physical {short_axis(src)}{extra}"))
            elif conf.get("invert"):
                mappings.append((f"Game {short_axis(target)}", f"Physical {short_axis(src)} · inverted"))
        for src, conf in self.profile.get("axis_buttons", {}).items():
            for direction in ("negative", "positive"):
                target = conf.get(direction, core.DISABLED)
                if target != core.DISABLED:
                    mappings.append((f"Game {short_button(target)}", f"Physical {short_axis(src)} · {direction}"))
        self.mapping_count.set_text(str(len(mappings)))
        if not mappings:
            row = Gtk.ListBoxRow()
            empty = self._label("No custom mappings yet. The standard layout is unchanged.", "dim-label", wrap=True)
            empty.set_margin_top(8); empty.set_margin_bottom(8); empty.set_margin_start(12); empty.set_margin_end(12)
            row.set_child(empty)
            row.set_activatable(False)
            self.mapping_list.append(row)
        for output, source in mappings[:48]:
            rowbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            rowbox.set_margin_top(7); rowbox.set_margin_bottom(7); rowbox.set_margin_start(12); rowbox.set_margin_end(12)
            words = self._box(spacing=3)
            words.append(self._label(output, "heading"))
            words.append(self._label("← " + source, "dim-label", wrap=True))
            words.set_hexpand(True)
            rowbox.append(words)
            row = Gtk.ListBoxRow()
            row.set_child(rowbox)
            row.set_activatable(False)
            self.mapping_list.append(row)

    def _populate_button_rows(self):
        if not hasattr(self, "button_rows"):
            return
        self._clear_preference_group(self.button_rows)
        if not self.current_device:
            self._add_preference_row(self.button_rows, Adw.ActionRow(title="No controller detected", subtitle="Connect the Xbox controller and choose Scan devices."))
            return
        for source in self.current_device.get("buttons", []):
            current = self.profile.get("buttons", {}).get(source, core.DISABLED)
            row = Adw.ActionRow(title=label_for_button(source), subtitle=source)
            drop = self._make_dropdown(self._button_pairs(), current, callback=lambda w, p, src=source: self._advanced_button_changed(w, src, p))
            drop.set_valign(Gtk.Align.CENTER)
            row.add_suffix(drop)
            row.set_activatable_widget(drop)
            self._add_preference_row(self.button_rows, row)

    def _advanced_button_changed(self, dropdown, source, _pspec):
        self.profile.setdefault("buttons", {})[source] = self._dropdown_value(dropdown)
        self._changed(save=True)

    def _populate_axis_rows(self):
        if not hasattr(self, "axis_rows"):
            return
        self._clear_preference_group(self.axis_rows)
        if not self.current_device:
            self._add_preference_row(self.axis_rows, Adw.ActionRow(title="No controller detected", subtitle="Connect the Xbox controller and choose Scan devices."))
            return
        for source in self.current_device.get("axes", {}):
            conf = self.profile.get("axes", {}).get(source, {})
            target = self._make_dropdown(self._axis_pairs(), conf.get("target", core.DISABLED), callback=lambda w, p, src=source: self._advanced_axis_target(w, src, p), width=210)
            target.set_valign(Gtk.Align.CENTER)
            expander = Adw.ExpanderRow(title=label_for_axis(source), subtitle=source)
            # Adw.ExpanderRow has add_suffix(), but unlike Adw.ActionRow it does
            # not implement set_activatable_widget(). The dropdown remains an
            # independently focusable, directly clickable suffix.
            expander.add_suffix(target)
            invert = Adw.SwitchRow(title="Invert axis", subtitle="Reverse the axis direction")
            invert.set_active(bool(conf.get("invert", False)))
            invert.connect("notify::active", lambda w, p, src=source: self._advanced_axis_invert(src, w.get_active()))
            expander.add_row(invert)
            deadzone_row = Adw.ActionRow(title="Deadzone", subtitle="Ignore movement near the centre")
            deadzone_control = self._scale_control("Deadzone", float(conf.get("deadzone", 0.08 if source in core.STICK_AXES else 0)), 0, 0.75, 0.01, lambda v, src=source: self._on_axis_tune(src, "deadzone", v), suffix="%", multiplier=100)
            deadzone_row.add_suffix(deadzone_control)
            expander.add_row(deadzone_row)
            curve_row = Adw.ActionRow(title="Response curve", subtitle="1.00 is linear")
            curve_control = self._scale_control("Response curve", float(conf.get("curve", 1)), 0.25, 4, 0.05, lambda v, src=source: self._on_axis_tune(src, "curve", v), formatter=lambda v: f"{v:.2f}")
            curve_row.add_suffix(curve_control)
            expander.add_row(curve_row)
            self._add_preference_row(self.axis_rows, expander)

    def _preference_rows_attr(self, group):
        if group is getattr(self, "button_rows", None):
            return "_button_preference_rows"
        if group is getattr(self, "axis_rows", None):
            return "_axis_preference_rows"
        if group is getattr(self, "inspector_controls", None):
            return "_inspector_preference_rows"
        raise ValueError("This preferences group is not a dynamically populated group")

    def _clear_preference_group(self, group):
        attr = self._preference_rows_attr(group)
        for row in getattr(self, attr, []):
            group.remove(row)
        setattr(self, attr, [])

    def _add_preference_row(self, group, row):
        group.add(row)
        attr = self._preference_rows_attr(group)
        getattr(self, attr, []).append(row)

    def _advanced_axis_target(self, dropdown, source, _pspec):
        self._on_axis_target_changed(dropdown, source, _pspec)
        GLib.idle_add(self._populate_axis_rows)

    def _advanced_axis_invert(self, source, value):
        self.profile.setdefault("axes", {}).setdefault(source, {})["invert"] = bool(value)
        self._changed(save=True)

    @staticmethod
    def _clear_list(listbox):
        child = listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling(); listbox.remove(child); child = nxt

    def _empty_row(self, message):
        row = Gtk.ListBoxRow()
        label = self._label(message, "dim-label", wrap=True)
        label.set_margin_top(8); label.set_margin_bottom(8); label.set_margin_start(12); label.set_margin_end(12)
        row.set_child(label)
        row.set_activatable(False)
        return row

    def swap_stick_axes(self, part):
        pair = ["ABS_X", "ABS_Y"] if part == "left_stick" else ["ABS_RX", "ABS_RY"]
        if not self.current_device or not all(n in self.current_device.get("axes", {}) for n in pair):
            self.set_status("Both axes for that stick must be detected to swap them.", "warning"); return
        first = self.profile["axes"].get(pair[0], {}).get("target", pair[0])
        second = self.profile["axes"].get(pair[1], {}).get("target", pair[1])
        self.profile["axes"][pair[0]]["target"] = second
        self.profile["axes"][pair[1]]["target"] = first
        self.profile_dirty = True
        self.save_profile(silent=True); self._refresh_all()
        self.set_status("Swapped the stick's horizontal and vertical routes.", "success")

    def reset_stick_axes(self, part):
        pair = ["ABS_X", "ABS_Y"] if part == "left_stick" else ["ABS_RX", "ABS_RY"]
        if not self.current_device: return
        for source in pair:
            if source in self.current_device.get("axes", {}):
                self.profile["axes"][source] = {"target": source, "invert": False, "deadzone": 0.08, "curve": 1.0}
        self.profile_dirty = True; self.save_profile(silent=True); self._refresh_all()
        self.set_status("Stick routing restored to default.", "success")

    # --- engine process / logs ---------------------------------------------
    def _start_worker_idle(self, automatic=True):
        self.start_worker(automatic=automatic)
        return GLib.SOURCE_REMOVE

    def start_worker(self, automatic=False):
        if not core.evdev:
            self.set_status("python-evdev is missing. Re-run ./install.sh.", "error"); return False
        if self.worker and self.worker.poll() is None:
            return True
        identity = core.controller_identity(self.current_device) if self.current_device else dict(self.saved_device_identity or {})
        if not identity or not (identity.get("name") or identity.get("vendor") or identity.get("path")):
            if not automatic:
                self.set_status("Connect your Xbox controller once so PadShift can remember it.", "warning")
            return False
        if self.profile_dirty or not self.profile_path().exists():
            if not self.save_profile(silent=True): return False
        self._persist_settings()
        script = str(Path(core.__file__).resolve())
        command = [sys.executable, script, "--daemon", "--device", str(identity.get("path", "")), "--profile", str(self.profile_path()), "--identity", json.dumps(identity, separators=(",", ":"))]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except Exception as exc:
            self.worker = None
            self.set_status(f"Could not start remapping: {exc}", "error")
            self.log(str(exc), "error")
            return False

        # Keep ownership of a successfully started daemon even if refreshing a UI
        # panel encounters a separate widget/API error. Previously the broad try
        # block could clear self.worker after Popen succeeded, orphaning the daemon
        # and incorrectly reporting that remapping never started.
        self.worker = process
        self.worker_buffer = ""
        self.set_status(f"Starting remapping with '{self.profile_name}'…", "warning")
        self.log(f"Started virtual controller using profile '{self.profile_name}'.")
        try:
            self._refresh_all()
        except Exception as exc:
            self.log(f"Remapper started, but refreshing the interface failed: {type(exc).__name__}: {exc}", "error")
            self.set_status(f"Remapping started, but the interface needs refreshing: {exc}", "warning")
        return True

    def _send_worker(self, obj):
        if not self.worker or self.worker.poll() is not None or not self.worker.stdin: return
        try:
            self.worker.stdin.write(json.dumps(obj) + "\n"); self.worker.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def toggle_worker(self):
        if self.worker and self.worker.poll() is None:
            self.stop_worker()
        else:
            self.start_worker()

    def stop_worker(self, persist_toggle=True):
        self.user_requested_stop = True
        if persist_toggle and self.auto_activate:
            self.auto_activate = False
            self._updating = True
            preference_switch = getattr(self, "preferences_auto_switch", None)
            if preference_switch is not None:
                preference_switch.set_active(False)
            self._updating = False
            self._persist_settings()
        if not self.worker:
            self.set_status("Remapping is stopped.", "warning"); return
        if self.worker.poll() is None:
            self._send_worker({"cmd": "stop"})
            self.set_status("Stopping remapping and releasing the controller…", "warning")
        else:
            self.worker = None
            self._refresh_all()

    def poll_worker(self):
        """Keep the GLib timer alive when a UI update encounters an exceptional state."""
        try:
            self._poll_worker_once()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != getattr(self, "_last_poll_error", None):
                self._last_poll_error = message
                self.log(f"Background poll error (continuing): {message}", "error")
        return GLib.SOURCE_CONTINUE

    def _poll_worker_once(self):
        self._poll_capture_queue()
        process = self.worker
        if process:
            if process.stdout:
                fd = process.stdout.fileno()
                try:
                    while select.select([fd], [], [], 0)[0]:
                        chunk = os.read(fd, 8192).decode("utf-8", "replace")
                        if not chunk: break
                        self.worker_buffer += chunk
                        while "\n" in self.worker_buffer:
                            line, self.worker_buffer = self.worker_buffer.split("\n", 1)
                            line = line.strip()
                            if not line: continue
                            try:
                                payload = json.loads(line); kind = payload.get("type")
                                if kind == "status": self.set_status(payload.get("message", ""), "success"); self.log(payload.get("message", ""))
                                elif kind == "input":
                                    self.last_input = f"{payload.get('name', 'Input')} · {'pressed' if payload.get('down') else 'released'}"
                                    self._input_activity_row.set_subtitle(self.last_input)
                                elif kind == "axis":
                                    self.last_axis = f"{payload.get('name', 'Axis')} → {payload.get('target', 'output')}: {payload.get('value', '')}"
                                    self._axis_activity_row.set_subtitle(self.last_axis)
                                elif kind == "error": self.set_status(payload.get("message", "Remapping error"), "error"); self.log(payload.get("message", ""), "error")
                                elif kind == "debug": self.log(payload.get("message", ""))
                            except json.JSONDecodeError: self.log(line)
                except (OSError, ValueError): pass
            if process.poll() is not None:
                code = process.returncode
                self.log(f"Remapper process exited with code {code}.", "error" if code else "info")
                self.worker = None
                self._refresh_all()
                pending = self.pending_capture_part
                if pending:
                    self.pending_capture_part = None
                    self.user_requested_stop = False
                    GLib.idle_add(self._begin_capture_now, pending)
                elif self.user_requested_stop:
                    self.user_requested_stop = False
                    self.set_status("Remapping stopped. The physical controller is available for mapping.", "warning")
                elif self.auto_activate:
                    self.set_status("Remapping stopped unexpectedly. Check Activity & diagnostics.", "error")
        self._refresh_engine_status()
    def _refresh_engine_status(self):
        active = bool(self.worker and self.worker.poll() is None)
        if hasattr(self, "engine_status_label"):
            self.engine_status_label.set_text("Running" if active else "Stopped")
        if hasattr(self, "_engine_activity_row"):
            self._engine_activity_row.set_subtitle("Virtual controller is available to games" if active else "Virtual controller is stopped")
        if hasattr(self, "map_state_label"):
            if self.capture_target_part:
                self.map_state_label.set_text(f"Listening for {self._part_info(self.capture_target_part)['label']}…")
            else:
                self.map_state_label.set_text("Remapping active" if active else "Ready to map")
        if hasattr(self, "run_button"):
            self.run_button.set_label("Stop remapping" if active else "Start remapping")
            if active:
                self.run_button.remove_css_class("suggested-action")
            else:
                self.run_button.add_css_class("suggested-action")

    def maybe_autostart(self):
        if self.auto_activate and not (self.worker and self.worker.poll() is None) and (self.current_device or self.saved_device_identity):
            self.start_worker(automatic=True)
        return GLib.SOURCE_REMOVE

    def auto_device_watch(self):
        try:
            if self.auto_activate:
                # Re-scan also detects a reconnect that appears at a new eventN path.
                current_path = self.current_device.get("path") if self.current_device else None
                known = core.discover_controllers()
                match = next((d for d in known if core.device_matches_identity(d, self.saved_device_identity)), None) if self.saved_device_identity else None
                if match and (not current_path or match.get("path") != current_path):
                    self.refresh_devices()
                    if not (self.worker and self.worker.poll() is None): self.start_worker(automatic=True)
                elif not self.current_device and known and not self.saved_device_identity:
                    self.refresh_devices()
        except Exception as exc:
            self.log(f"Controller auto-watch: {exc}", "error")
        return GLib.SOURCE_CONTINUE

    # --- Bottles integration -----------------------------------------------
    def refresh_bottles_compatibility(self):
        flatpak = shutil.which("flatpak")
        if not flatpak:
            self.bottles_status.set_text("Flatpak was not found. Native RPM/AppImage Bottles does not need a Flatpak permission override.")
            return
        try:
            result = subprocess.run([flatpak, "info", core.BOTTLES_FLATPAK_ID], capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.bottles_status.set_text(f"Could not check Bottles Flatpak: {exc}"); return
        if result.returncode != 0:
            self.bottles_status.set_text("Bottles Flatpak was not detected. Native Bottles does not need this permission change.")
        else:
            self.bottles_status.set_text("Bottles Flatpak is installed. If your game cannot see PadShift, enable the per-user input-device permission below, then fully quit and reopen Bottles. Check the bottle's Dedicated sandbox setting if the game still cannot see the controller.")

    def confirm_bottles_access(self):
        flatpak = shutil.which("flatpak")
        if not flatpak:
            self.set_status("Flatpak is not installed. Native Bottles needs no Flatpak override.", "warning")
            return
        try:
            check = subprocess.run([flatpak, "info", core.BOTTLES_FLATPAK_ID], capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.set_status(f"Could not check Bottles: {exc}", "error")
            return
        if check.returncode != 0:
            self.set_status("Bottles Flatpak was not detected.", "warning")
            return
        dialog = Adw.AlertDialog.new(
            "Allow controller access in Bottles?",
            "This adds a per-user Flatpak override that allows Bottles to access host input devices under /dev/input. The permission applies to Bottles, not just one bottle.",
        )
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("allow", "Allow input access")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_bottles_access_response)
        self._present_dialog(dialog)

    def _on_bottles_access_response(self, _dialog, response):
        if response != "allow": return
        try:
            result = subprocess.run([shutil.which("flatpak"), "override", "--user", "--device=input", core.BOTTLES_FLATPAK_ID], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.set_status(f"Could not update Bottles permissions: {exc}", "error"); return
        if result.returncode == 0:
            self.set_status("Bottles input access enabled. Fully quit and reopen Bottles.", "success")
            self.log("Granted Bottles Flatpak input-device permission.")
            self.refresh_bottles_compatibility()
        else:
            self.set_status((result.stderr or result.stdout or "Flatpak override failed.").strip(), "error")

    # --- state, status, shutdown --------------------------------------------
    def set_feedback(self, message, kind="info"):
        self.capture_feedback_label.set_text(message)
        icon_name = {
            "success": "padshift-status-success-symbolic",
            "warning": "padshift-status-warning-symbolic",
            "error": "padshift-status-error-symbolic",
            "info": "padshift-status-info-symbolic",
        }.get(kind, "padshift-status-info-symbolic")
        set_image_icon(self.capture_feedback_icon, icon_name)
        for css in ("success", "warning", "error"):
            self.capture_feedback_icon.remove_css_class(css)
        if kind in ("success", "warning", "error"):
            self.capture_feedback_icon.add_css_class(kind)
        self.set_status(message, kind)

    def set_status(self, message, kind="info"):
        if hasattr(self, "capture_feedback_label") and message:
            # Status text remains in the work area; toasts are reserved for success/error.
            if kind in ("warning", "error"):
                self.capture_feedback_label.set_text(message)
        if hasattr(self, "toast_overlay") and kind in ("success", "error"):
            try:
                toast = Adw.Toast.new(message)
                toast.set_timeout(4)
                self.toast_overlay.add_toast(toast)
            except Exception:
                pass
        if kind == "error":
            self.log(message, "error")

    def log(self, message, kind="info"):
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.log_lines.append(line); self.log_lines = self.log_lines[-500:]
        if hasattr(self, "log_buffer"):
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, line + "\n")
            mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
            self.log_view.scroll_mark_onscreen(mark)
        print(line, file=sys.stderr if kind == "error" else sys.stdout)

    def _clear_log(self, *_):
        self.log_lines.clear(); self.log_buffer.set_text("")

    def _copy_diagnostics(self, *_):
        display = Gdk.Display.get_default()
        if not display: return
        clipboard = display.get_clipboard()
        state = f"PadShift {APP_VERSION}\nProfile: {self.profile_name}\nController: {(self.current_device or {}).get('name', 'disconnected')}\nWorker active: {bool(self.worker and self.worker.poll() is None)}\nLast input: {self.last_input}\nLast axis: {self.last_axis}\n\n" + "\n".join(self.log_lines[-150:])
        clipboard.set_text(state)
        self.set_status("Diagnostics copied to clipboard.", "success")

    def on_close_request(self, *_args):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
        if self.keep_background and not self._quitting:
            # Keep the worker alive: it owns the virtual controller. A fresh launcher
            # activation presents this same ApplicationWindow again.
            if self.window:
                self.window.set_visible(False)
            self.log(
                "Window hidden; PadShift is still running. Launch PadShift again from the application menu to reopen it.",
                "info",
            )
            return True
        if self.worker and self.worker.poll() is None:
            self._send_worker({"cmd": "stop"})
        return False


def main():
    app = PadShiftGtkApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
