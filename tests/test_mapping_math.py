import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "padshift.py"
spec = importlib.util.spec_from_file_location("padshift", MODULE_PATH)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

class AxisMappingTests(unittest.TestCase):
    def test_centered_stick_identity(self):
        self.assertEqual(ps.transform_axis(0, -32768, 32767, "ABS_X", "ABS_X"), 0)
        self.assertGreater(ps.transform_axis(32767, -32768, 32767, "ABS_X", "ABS_X"), 32000)
        self.assertLess(ps.transform_axis(-32768, -32768, 32767, "ABS_X", "ABS_X"), -32000)

    def test_inversion(self):
        normal = ps.transform_axis(20000, -32768, 32767, "ABS_X", "ABS_X")
        inverted = ps.transform_axis(20000, -32768, 32767, "ABS_X", "ABS_X", invert=True)
        self.assertGreater(normal, 0)
        self.assertLess(inverted, 0)

    def test_deadzone_recentres(self):
        self.assertEqual(ps.transform_axis(1000, -32768, 32767, "ABS_X", "ABS_X", deadzone=0.1), 0)

    def test_trigger_to_trigger(self):
        self.assertEqual(ps.transform_axis(0, 0, 255, "ABS_Z", "ABS_RZ"), 0)
        self.assertEqual(ps.transform_axis(255, 0, 255, "ABS_Z", "ABS_RZ"), 255)

    def test_stick_to_hat_threshold(self):
        self.assertEqual(ps.transform_axis(0, -32768, 32767, "ABS_X", "ABS_HAT0X"), 0)
        self.assertEqual(ps.transform_axis(32767, -32768, 32767, "ABS_X", "ABS_HAT0X"), 1)
        self.assertEqual(ps.transform_axis(-32768, -32768, 32767, "ABS_X", "ABS_HAT0X"), -1)


    def test_axis_to_button_dpad_directions(self):
        config = {"negative": "BTN_SOUTH", "positive": "BTN_EAST", "threshold": 0.55, "invert": False}
        self.assertEqual(ps.axis_button_for_value(-1, -1, 1, "ABS_HAT0Y", config), "BTN_SOUTH")
        self.assertEqual(ps.axis_button_for_value(1, -1, 1, "ABS_HAT0Y", config), "BTN_EAST")
        self.assertIsNone(ps.axis_button_for_value(0, -1, 1, "ABS_HAT0Y", config))

    def test_axis_to_button_trigger_press_and_reversed_trigger(self):
        config = {"negative": ps.DISABLED, "positive": "BTN_SOUTH", "threshold": 0.55, "invert": False}
        self.assertIsNone(ps.axis_button_for_value(0, 0, 255, "ABS_Z", config))
        self.assertEqual(ps.axis_button_for_value(230, 0, 255, "ABS_Z", config), "BTN_SOUTH")
        reversed_config = {"negative": ps.DISABLED, "positive": "BTN_EAST", "threshold": 0.55, "invert": True}
        self.assertIsNone(ps.axis_button_for_value(255, 0, 255, "ABS_Z", reversed_config))
        self.assertEqual(ps.axis_button_for_value(0, 0, 255, "ABS_Z", reversed_config), "BTN_EAST")

    def test_axis_output_mirrors_dpad_but_not_analog_triggers(self):
        self.assertEqual(ps.digital_button_for_axis_output("ABS_HAT0Y", -1), "BTN_DPAD_UP")
        self.assertEqual(ps.digital_button_for_axis_output("ABS_HAT0X", 1), "BTN_DPAD_RIGHT")
        self.assertIsNone(ps.digital_button_for_axis_output("ABS_Z", 255))
        self.assertIsNone(ps.digital_button_for_axis_output("ABS_RZ", 255))
        self.assertIsNone(ps.digital_button_for_axis_output("ABS_RZ", 0))

    def test_profile_round_trips_axis_button_mappings(self):
        saved = {"buttons": {}, "axes": {}, "axis_buttons": {
            "ABS_HAT0Y": {"negative": "BTN_SOUTH", "positive": ps.DISABLED, "threshold": 0.6, "invert": False}}}
        merged = ps.merge_profile(saved, [], {"ABS_HAT0Y": {"min": -1, "max": 1}})
        self.assertEqual(merged["axis_buttons"]["ABS_HAT0Y"]["negative"], "BTN_SOUTH")
        self.assertAlmostEqual(merged["axis_buttons"]["ABS_HAT0Y"]["threshold"], 0.6)

    def test_alternate_brake_gas_axes_use_standard_trigger_outputs(self):
        profile = ps.default_profile([], {"ABS_BRAKE": {"min": 0, "max": 255},
                                          "ABS_GAS": {"min": 0, "max": 255}})
        self.assertEqual(profile["axes"]["ABS_BRAKE"]["target"], "ABS_Z")
        self.assertEqual(profile["axes"]["ABS_GAS"]["target"], "ABS_RZ")
        old_profile = {"axes": {
            "ABS_BRAKE": {"target": "ABS_BRAKE", "invert": False, "deadzone": 0.0, "curve": 1.0},
            "ABS_GAS": {"target": "ABS_GAS", "invert": False, "deadzone": 0.0, "curve": 1.0},
        }}
        migrated = ps.merge_profile(old_profile, [], {"ABS_BRAKE": {"min": 0, "max": 255},
                                                        "ABS_GAS": {"min": 0, "max": 255}})
        self.assertEqual(migrated["axes"]["ABS_BRAKE"]["target"], "ABS_Z")
        self.assertEqual(migrated["axes"]["ABS_GAS"]["target"], "ABS_RZ")
        # Alternate source axes remain selectable, but are never virtual output targets.
        self.assertNotIn("ABS_BRAKE", ps.AXIS_TARGETS)
        self.assertNotIn("ABS_GAS", ps.AXIS_TARGETS)
        remapped_other_source = ps.merge_profile(
            {"axes": {"ABS_X": {"target": "ABS_BRAKE"}}},
            [], {"ABS_X": {"min": -32768, "max": 32767}})
        self.assertEqual(remapped_other_source["axes"]["ABS_X"]["target"], "ABS_Z")

    def test_profile_merge_clamps(self):
        merged = ps.merge_profile({"buttons": {"BTN_SOUTH": "NOT_A_BUTTON"},
                                   "axes": {"ABS_X": {"target": "ABS_X", "deadzone": 7, "curve": 99}}},
                                  ["BTN_SOUTH"], {"ABS_X": {"min": -32768, "max": 32767}})
        self.assertEqual(merged["buttons"]["BTN_SOUTH"], ps.DISABLED)
        self.assertEqual(merged["axes"]["ABS_X"]["deadzone"], 0.95)
        self.assertEqual(merged["axes"]["ABS_X"]["curve"], 4.0)


class VisualMappingTests(unittest.TestCase):
    def setUp(self):
        # These routing helpers can be exercised without opening a Tk window.
        self.app = object.__new__(ps.PadShiftApp)

    def test_face_button_destination(self):
        self.assertEqual(self.app._part_button_target("face_x"), "BTN_WEST")
        self.assertEqual(self.app._part_button_target("face_y"), "BTN_NORTH")
        self.assertEqual(self.app._part_button_target("face_a"), "BTN_SOUTH")

    def test_face_button_labels_follow_positional_gamepad_convention(self):
        self.assertTrue(ps.BUTTON_LABELS["BTN_WEST"].startswith("X"))
        self.assertTrue(ps.BUTTON_LABELS["BTN_NORTH"].startswith("Y"))

    def test_stick_axis_destinations(self):
        self.assertEqual(self.app._axis_target_for_part("ABS_X", "right_stick"), "ABS_RX")
        self.assertEqual(self.app._axis_target_for_part("ABS_Y", "right_stick"), "ABS_RY")

    def test_primary_trigger_destination(self):
        self.assertEqual(self.app._axis_target_for_part("ABS_Z", "rt"), "ABS_RZ")
        self.assertEqual(self.app._axis_target_for_part("ABS_RZ", "lt"), "ABS_Z")

    def test_dpad_axis_destination(self):
        self.assertEqual(self.app._axis_target_for_part("ABS_HAT0X", "dpad"), "ABS_HAT0X")
        self.assertEqual(self.app._axis_target_for_part("ABS_HAT0Y", "dpad"), "ABS_HAT0Y")

    def test_click_target_axis_directions(self):
        self.assertEqual(self.app._visual_axis_spec("left_stick_up"), ("ABS_Y", -1, "stick"))
        self.assertEqual(self.app._visual_axis_spec("right_stick_down"), ("ABS_RY", 1, "stick"))
        self.assertEqual(self.app._visual_axis_spec("dpad_left"), ("ABS_HAT0X", -1, "dpad"))

    def test_capture_auto_inverts_opposite_stick_direction(self):
        # Pulling the physical right stick down (+1) for the virtual left stick up (-1).
        self.assertTrue(self.app._captured_axis_invert("ABS_RY", "ABS_Y", 1, -1))
        self.assertFalse(self.app._captured_axis_invert("ABS_RX", "ABS_X", -1, -1))

    def test_capture_signal_detects_physical_axis_direction(self):
        info = {"min": -32768, "max": 32767}
        self.assertEqual(self.app._axis_capture_signal("ABS_RY", 30000, info, 0), 1)
        self.assertEqual(self.app._axis_capture_signal("ABS_RY", -30000, info, 0), -1)
        self.assertIsNone(self.app._axis_capture_signal("ABS_RY", 2000, info, 0))

if __name__ == "__main__":
    unittest.main()
