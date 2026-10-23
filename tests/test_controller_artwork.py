"""Regression tests for the asset-backed controller editor and HIG polish."""
import ast
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "padshift_gtk.py"
UI = UI_PATH.read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
ART = ROOT / "assets" / "xbox-one-controller.png"


def assigned_literal(name):
    tree = ast.parse(UI)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing assignment: {name}")


class ControllerArtworkTests(unittest.TestCase):
    def test_bundled_artwork_is_transparent_png_with_expected_dimensions(self):
        self.assertTrue(ART.is_file(), "The app needs to ship its controller image")
        data = ART.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (1408, 937))
        self.assertEqual(data[25], 6, "Artwork should be RGBA so it blends with Adwaita surfaces")
        self.assertGreater(len(data), 200_000)

    def test_artwork_decodes_with_pycairo_using_supported_surface_api(self):
        # The regression was calling nonexistent ImageSurface.status(), causing
        # every valid image to be rejected by the loader's broad exception block.
        import cairo
        surface = cairo.ImageSurface.create_from_png(str(ART))
        self.assertEqual((surface.get_width(), surface.get_height()), (1408, 937))
        self.assertFalse(hasattr(surface, "status"))
        self.assertNotIn("surface.status()", UI)

    def test_installer_places_artwork_next_to_the_ui(self):
        self.assertIn('"$HERE/assets/xbox-one-controller.png"', INSTALL)
        self.assertIn('"$APP_DIR/assets/xbox-one-controller.png"', INSTALL)
        self.assertIn('Path(__file__).resolve().parent / "assets" / "xbox-one-controller.png"', UI)

    def test_artwork_and_click_zones_share_one_coordinate_transform(self):
        self.assertIn("def _controller_transform", UI)
        self.assertIn("def _controller_point", UI)
        self.assertIn("ctx.set_source_surface(artwork, ox, oy)", UI)
        self.assertIn("def _paint_stage", UI)
        self.assertIn("def _hit_test_controller", UI)
        areas = assigned_literal("HIT_AREAS")
        highlights = assigned_literal("HIGHLIGHTS")
        self.assertEqual({row[0] for row in areas}, set(highlights))
        self.assertTrue(all(0 <= x1 < x2 <= 1408 and 0 <= y1 < y2 <= 937 for _, x1, y1, x2, y2 in areas))
        # Verify every target's center resolves to itself, not an overlapping earlier zone.
        for part, x1, y1, x2, y2 in areas:
            px, py = (x1 + x2) / 2, (y1 + y2) / 2
            matches = [name for name, ax1, ay1, ax2, ay2 in areas if ax1 <= px <= ax2 and ay1 <= py <= ay2]
            self.assertEqual(matches[0], part, f"Overlapping target zone around {part}: {matches}")

    def test_hover_and_click_feedback_are_present(self):
        for token in (
            "Gtk.EventControllerMotion.new()", "def _map_motion", "def _map_leave",
            'self.map_area.set_cursor_from_name("pointer" if part else None)',
            "self.hover_part", "stroke_highlight", "self._sync_destination_dropdown(part)",
        ):
            with self.subTest(token=token):
                self.assertIn(token, UI)

    def test_missing_artwork_shows_an_explicit_error_instead_of_blank_canvas(self):
        self.assertIn("def _load_controller_artwork", UI)
        self.assertIn("self.controller_artwork_error = str(exc)", UI)
        self.assertIn("Controller artwork could not be loaded", UI)
        self.assertIn("assets/xbox-one-controller.png is copied", UI)

    def test_about_dialog_is_complete_and_has_release_details(self):
        for token in (
            "Adw.AboutDialog()", "set_comments(", "set_copyright(", "set_developers(",
            "set_artists(", "set_release_notes(", "set_release_notes_version(APP_VERSION)",
            "set_license_type(Gtk.License.MIT_X11)", "set_support_url(", "set_issue_url(",
            "add_credit_section(", "set_debug_info(", "Trademark notice",
        ):
            with self.subTest(token=token):
                self.assertIn(token, UI)

    def test_popups_use_native_libadwaita_patterns_and_respect_motion_settings(self):
        self.assertIn("Adw.AlertDialog.new", UI)
        self.assertIn("Adw.PreferencesDialog()", UI)
        self.assertIn("Adw.AboutDialog()", UI)
        self.assertIn("dialog.set_license_type(Gtk.License.MIT_X11)", UI)
        self.assertNotIn("Adw.License.MIT_X11", UI)
        self.assertIn("Adw.ToastOverlay()", UI)
        self.assertIn("set_transition_duration(PAGE_TRANSITION_MS)", UI)
        self.assertIn("Gtk.StackTransitionType.SLIDE_LEFT_RIGHT", UI)
        self.assertIn("dialog.present(self.window)", UI)
        self.assertIn("Adw.get_enable_animations(self.window)", UI)
        self.assertIn("Adw.TimedAnimation", UI)
        self.assertIn("Adw.CallbackAnimationTarget", UI)
        self.assertIn("Gtk.RevealerTransitionType.SLIDE_DOWN", UI)
        self.assertIn("Follow system", UI)
        self.assertIn("Always on", UI)
        self.assertNotIn("PropertyAnimationTarget", UI)
        self.assertIn("gtk-enable-animations", UI)
        self.assertIn("dialog.present(self.window)", UI)
        self.assertNotIn("Gtk.Dialog(", UI)
        self.assertNotIn("Gtk.MessageDialog(", UI)
        self.assertIn("GTK's motion preference", README)

    def test_version_docs_and_asset_notes_are_updated(self):
        self.assertIn('APP_VERSION = "0.12.0"', UI)
        self.assertTrue(README.startswith("# PadShift 0.12.0"))
        self.assertIn("transparent, high-detail Xbox One-style product illustration", README)
        self.assertIn("not affiliated with or endorsed by Microsoft", README)


if __name__ == "__main__":
    unittest.main()
