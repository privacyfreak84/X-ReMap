"""Hardware-free tests for reconnect identity matching."""
import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "padshift.py"
spec = importlib.util.spec_from_file_location("padshift_reconnect_test", MODULE_PATH)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class ControllerIdentityTests(unittest.TestCase):
    def test_reconnect_matches_same_controller_after_event_path_changes(self):
        saved = {"path": "/dev/input/event7", "name": "Xbox Wireless Controller",
                 "vendor": 0x045E, "product": 0x02FD, "uniq": "AA:BB:CC"}
        reconnected = {"path": "/dev/input/event19", "name": "Xbox Wireless Controller",
                       "vendor": 0x045E, "product": 0x02FD, "uniq": "AA:BB:CC"}
        self.assertTrue(ps.device_matches_identity(reconnected, saved))

    def test_reconnect_rejects_another_vendor_product(self):
        saved = {"path": "/dev/input/event7", "name": "Xbox Wireless Controller",
                 "vendor": 0x045E, "product": 0x02FD}
        other = {"path": "/dev/input/event7", "name": "Other Gamepad",
                 "vendor": 0x1234, "product": 0x5678}
        self.assertFalse(ps.device_matches_identity(other, saved))

    def test_uniq_disambiguates_two_same_model_controllers(self):
        saved = {"path": "/dev/input/event7", "name": "Xbox Wireless Controller",
                 "vendor": 0x045E, "product": 0x02FD, "uniq": "controller-A"}
        other = {"path": "/dev/input/event19", "name": "Xbox Wireless Controller",
                 "vendor": 0x045E, "product": 0x02FD, "uniq": "controller-B"}
        self.assertFalse(ps.device_matches_identity(other, saved))

    def test_empty_saved_identity_does_not_match_every_controller(self):
        self.assertFalse(ps.device_matches_identity({"path": "/dev/input/event7", "name": "Pad"}, {}))

    def test_identity_normalisation_keeps_path_and_device_signature(self):
        identity = ps.controller_identity({"path": "/dev/input/event7", "name": "Pad", "vendor": 7, "product": 8})
        self.assertEqual(identity["path"], "/dev/input/event7")
        self.assertEqual(identity["vendor"], 7)
        self.assertEqual(identity["product"], 8)


if __name__ == "__main__":
    unittest.main()
