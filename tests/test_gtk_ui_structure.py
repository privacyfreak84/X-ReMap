"""Static HIG regression checks for the GTK frontend (runs without GI/GTK installed)."""
import ast
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "padshift_gtk.py"
UI_SOURCE = UI_PATH.read_text(encoding="utf-8")
INSTALL_SOURCE = (ROOT / "install.sh").read_text(encoding="utf-8")
README_SOURCE = (ROOT / "README.md").read_text(encoding="utf-8")


class GtkUiStructureTests(unittest.TestCase):
    def test_frontend_parses_and_has_no_duplicate_methods(self):
        tree = ast.parse(UI_SOURCE)
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PadShiftGtkApp")
        names = [node.name for node in app.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        duplicates = sorted(name for name in set(names) if names.count(name) > 1)
        self.assertEqual(duplicates, [], f"Duplicate methods shadow one another: {duplicates}")

    def test_uses_native_libadwaita_shell_and_patterns(self):
        for token in (
            "Adw.ApplicationWindow", "Adw.NavigationSplitView", "Adw.NavigationPage",
            "Adw.ToolbarView", "Adw.HeaderBar", "Adw.PreferencesDialog",
            "Adw.PreferencesGroup", "Adw.ActionRow", "Adw.SwitchRow", "Adw.AlertDialog",
        ):
            with self.subTest(token=token):
                self.assertIn(token, UI_SOURCE)

    def test_uses_responsive_breakpoints_and_native_collapsed_navigation(self):
        self.assertIn('"max-width: 850sp"', UI_SOURCE)
        self.assertIn('"max-width: 1180sp"', UI_SOURCE)
        self.assertIn("AdwHeaderBar integrates with AdwNavigationSplitView", UI_SOURCE)
        self.assertNotIn("self.back_button", UI_SOURCE)

    def test_no_custom_css_theme_is_injected(self):
        self.assertNotIn("Gtk.CssProvider", UI_SOURCE)
        self.assertNotIn("load_from_data", UI_SOURCE)
        self.assertNotIn("def _install_css", UI_SOURCE)

    def test_custom_drawing_has_keyboard_alternative(self):
        self.assertIn("Game-side control", UI_SOURCE)
        self.assertIn("self.learn_button", UI_SOURCE)
        self.assertIn("set_tooltip_text(\"Controller illustration.", UI_SOURCE)
        self.assertIn("update_property_value", UI_SOURCE)

    def test_shortcut_dialog_uses_libadwaita_when_available(self):
        self.assertIn('getattr(Adw, "ShortcutLabel", None)', UI_SOURCE)
        self.assertIn("Gtk.ShortcutLabel", UI_SOURCE)  # fallback for earlier GTK 4/libadwaita

    def test_app_icons_are_valid_hicolor_svg_assets(self):
        app_icon = ROOT / "icons/scalable/apps/io.github.privacyfreak84.PadShift.svg"
        symbolic_icon = ROOT / "icons/symbolic/apps/io.github.privacyfreak84.PadShift-symbolic.svg"
        for icon_path in (app_icon, symbolic_icon):
            with self.subTest(icon=icon_path.name):
                root = ET.parse(icon_path).getroot()
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertEqual(root.attrib.get("viewBox"), "0 0 128 128" if "scalable/apps" in str(icon_path) else "0 0 16 16")
        self.assertIn("fill=\"currentColor\"", symbolic_icon.read_text(encoding="utf-8"))

    def test_installer_uses_unique_app_identity_and_symbolic_icon_location(self):
        self.assertIn('APP_ID="io.github.privacyfreak84.PadShift"', INSTALL_SOURCE)
        self.assertIn('Icon=$APP_ID', INSTALL_SOURCE)
        self.assertIn('icons/symbolic/apps/$APP_ID-symbolic.svg', INSTALL_SOURCE)
        self.assertIn('Categories=Game;Utility;', INSTALL_SOURCE)
        self.assertNotIn('Icon=input-gaming', INSTALL_SOURCE)


    def test_expander_row_does_not_call_action_row_only_api(self):
        # Adw.ExpanderRow exposes add_suffix()/add_row(), but has no
        # set_activatable_widget() method (that method belongs to Adw.ActionRow).
        self.assertNotIn("expander.set_activatable_widget(", UI_SOURCE)
        tree = ast.parse(UI_SOURCE)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "set_activatable_widget"
                 and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "expander"]
        self.assertEqual(calls, [])

    def test_page_navigation_uses_visible_sliding_transition(self):
        self.assertIn("Gtk.StackTransitionType.SLIDE_LEFT_RIGHT", UI_SOURCE)
        self.assertIn("self.stack.set_transition_duration(PAGE_TRANSITION_MS)", UI_SOURCE)

    def test_ui_animations_are_explicit_and_motion_aware(self):
        self.assertIn("Adw.TimedAnimation", UI_SOURCE)
        self.assertIn("Adw.CallbackAnimationTarget", UI_SOURCE)
        self.assertIn("animation.play()", UI_SOURCE)
        self.assertIn("gtk-enable-animations", UI_SOURCE)
        self.assertIn("Gtk.RevealerTransitionType.SLIDE_DOWN", UI_SOURCE)
        self.assertIn("self.stack.set_transition_duration(PAGE_TRANSITION_MS)", UI_SOURCE)
        self.assertNotIn("PropertyAnimationTarget", UI_SOURCE)
        self.assertIn("Follow system", UI_SOURCE)

    def test_controller_canvas_can_shrink_on_compact_windows(self):
        self.assertIn("class ControllerCanvas(Gtk.DrawingArea)", UI_SOURCE)
        self.assertIn("MIN_HEIGHT = 220", UI_SOURCE)
        self.assertIn("NATURAL_HEIGHT = 360", UI_SOURCE)
        self.assertIn('self.window.set_size_request(420, 320)', UI_SOURCE)
        self.assertIn("self.editor_paned = Gtk.Box", UI_SOURCE)
        self.assertNotIn("self.editor_paned = Gtk.Paned", UI_SOURCE)

    def test_animation_timings_are_short_and_named(self):
        timings = {name: int(value) for name, value in re.findall(r"^([A-Z_]+_MS) = (\d+)", UI_SOURCE, re.M)}
        self.assertEqual(set(timings), {"PAGE_TRANSITION_MS", "REVEALER_TRANSITION_MS", "RING_FADE_MS", "PULSE_MS"})
        # Snappy, but still long enough to be perceived as motion.
        self.assertTrue(120 <= timings["PAGE_TRANSITION_MS"] <= 200, timings)
        self.assertTrue(100 <= timings["REVEALER_TRANSITION_MS"] <= 180, timings)
        self.assertTrue(60 <= timings["RING_FADE_MS"] <= 130, timings)
        self.assertTrue(400 <= timings["PULSE_MS"] <= 650, timings)
        for call in ("set_transition_duration(REVEALER_TRANSITION_MS)",
                     "0.0, 1.0, RING_FADE_MS, target)", "0.0, 1.0, PULSE_MS, target)"):
            self.assertIn(call, UI_SOURCE)
        self.assertNotRegex(UI_SOURCE, r"TimedAnimation\.new\([^)]*\b\d{3,}\b")

    def test_tray_icon_is_optional_installed_and_follows_background_mode(self):
        tree = ast.parse(UI_SOURCE)
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PadShiftGtkApp")
        methods = {node.name: node for node in app.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("import padshift_tray", UI_SOURCE)
        self.assertIn("padshift_tray = None", UI_SOURCE)  # a tray import failure must not stop launch
        for name in ("_sync_tray", "_tray_show", "_update_tray_state", "_on_shutdown"):
            self.assertIn(name, methods)
        self.assertIn("self._sync_tray()", ast.get_source_segment(UI_SOURCE, methods["_on_keep_background_switch"]))
        self.assertIn("self._sync_tray()", ast.get_source_segment(UI_SOURCE, methods["on_activate"]))
        self.assertIn("self.tray.stop()", ast.get_source_segment(UI_SOURCE, methods["_on_shutdown"]))
        self.assertIn('"notify::visible"', ast.get_source_segment(UI_SOURCE, methods["on_activate"]))
        self.assertIn("padshift_tray.py", INSTALL_SOURCE)

    def test_close_hides_the_window_without_stopping_background_remapping(self):
        tree = ast.parse(UI_SOURCE)
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PadShiftGtkApp")
        methods = {node.name: node for node in app.body if isinstance(node, ast.FunctionDef)}
        close_source = ast.get_source_segment(UI_SOURCE, methods["on_close_request"])
        quit_source = ast.get_source_segment(UI_SOURCE, methods["_action_quit"])
        self.assertIn('self.keep_background = bool(self.settings.get("keep_background", True))', UI_SOURCE)
        self.assertIn('title="Keep running when the window closes"', UI_SOURCE)
        self.assertIn("if self.keep_background and not self._quitting:", close_source)
        self.assertIn("self.window.set_visible(False)", close_source)
        self.assertIn('self._send_worker({"cmd": "stop"})', close_source)
        self.assertIn('"keep_background": bool(self.keep_background)', UI_SOURCE)
        self.assertIn("self._quitting = True", quit_source)
        self.assertIn("self.window.present()", UI_SOURCE)

    def test_capture_resume_scheduler_is_defined_and_used(self):
        self.assertIn("def _schedule_resume_after_capture(self, delay_ms)", UI_SOURCE)
        self.assertIn("self._schedule_resume_after_capture(500)", UI_SOURCE)
        self.assertIn("self._schedule_resume_after_capture(1500)", UI_SOURCE)

    def test_remapping_worker_is_not_disowned_by_ui_refresh_errors(self):
        tree = ast.parse(UI_SOURCE)
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PadShiftGtkApp")
        method = next(node for node in app.body if isinstance(node, ast.FunctionDef) and node.name == "start_worker")
        source = ast.get_source_segment(UI_SOURCE, method)
        self.assertLess(source.index("process = subprocess.Popen"), source.index("self.worker = process"))
        self.assertIn("Remapper started, but refreshing the interface failed", source)
        self.assertIn("return True", source)

    def test_docs_disclose_hig_and_accessibility_validation_scope(self):
        self.assertIn("official GNOME Human Interface Guidelines", README_SOURCE)
        self.assertIn("still requires a live Fedora/GTK4/libadwaita check", README_SOURCE)
        self.assertTrue((ROOT / "DESIGN-HIG.md").exists())


if __name__ == "__main__":
    unittest.main()
