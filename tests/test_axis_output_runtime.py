"""Integration-level tests for real virtual-key writes from D-pad/trigger ABS events."""
import contextlib
import importlib.util
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "padshift.py"
spec = importlib.util.spec_from_file_location("padshift_axis_runtime_test", MODULE_PATH)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class FakeAbsInfo:
    def __init__(self, value=0, minimum=0, maximum=255, *args):
        self.value, self.min, self.max = value, minimum, maximum


class FakeEcodes:
    EV_KEY = 1
    EV_ABS = 3
    BUS_USB = 3
    ecodes = {
        "BTN_SOUTH": 304, "BTN_EAST": 305, "BTN_NORTH": 307, "BTN_WEST": 308,
        "BTN_TL2": 312, "BTN_TR2": 313,
        "BTN_DPAD_UP": 320, "BTN_DPAD_DOWN": 321, "BTN_DPAD_LEFT": 322, "BTN_DPAD_RIGHT": 323,
        "ABS_Z": 2, "ABS_RZ": 5, "ABS_GAS": 9, "ABS_BRAKE": 10,
        "ABS_HAT0X": 16, "ABS_HAT0Y": 17,
    }
    bytype = {
        EV_KEY: {304: "BTN_SOUTH", 305: "BTN_EAST", 307: "BTN_NORTH", 308: "BTN_WEST",
                 312: "BTN_TL2", 313: "BTN_TR2", 320: "BTN_DPAD_UP", 321: "BTN_DPAD_DOWN",
                 322: "BTN_DPAD_LEFT", 323: "BTN_DPAD_RIGHT"},
        EV_ABS: {2: "ABS_Z", 5: "ABS_RZ", 9: "ABS_GAS", 10: "ABS_BRAKE",
                 16: "ABS_HAT0X", 17: "ABS_HAT0Y"},
    }
    AbsInfo = FakeAbsInfo


class FakeUInput:
    instances = []
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.writes = []
        self.closed = False
        self.__class__.instances.append(self)
    def write(self, *args): self.writes.append(args)
    def syn(self): pass
    def close(self): self.closed = True


class FakeInputDevice:
    def __init__(self, path, axes):
        self.path, self.axes = path, axes
        self.fd = 432
        self.name = "Xbox One Controller"
        self.info = types.SimpleNamespace(vendor=0x045E, product=0x02FD, bustype=3)
        self.uniq, self.phys = "runtime-test-pad", "usb-test"
        self.events = list(axes.pop("events", []))
        self.closed = False
    def capabilities(self, absinfo=False):
        key_codes = [304, 305, 307, 308, 312, 313, 320, 321, 322, 323]
        abs_entries = [(code, FakeAbsInfo(value, minimum, maximum))
                       for code, (value, minimum, maximum) in self.axes.items() if isinstance(code, int)]
        return {FakeEcodes.EV_KEY: key_codes, FakeEcodes.EV_ABS: abs_entries}
    def grab(self): pass
    def ungrab(self): pass
    def absinfo(self, code):
        for code_, (value, minimum, maximum) in self.axes.items():
            if code_ == code:
                return FakeAbsInfo(value, minimum, maximum)
        return FakeAbsInfo()
    def read(self):
        events, self.events = self.events, []
        return events
    def close(self): self.closed = True


class FakeEvdev:
    UInput = FakeUInput
    AbsInfo = FakeAbsInfo
    def __init__(self, axis_code, axis_value, minimum, maximum, event_type=FakeEcodes.EV_ABS, events=None):
        # The discovery open gets neutral values. The daemon's second open carries
        # simulated physical event(s), for either an axis or a digital trigger.
        self.axis_code, self.axis_value = axis_code, axis_value
        self.minimum, self.maximum = minimum, maximum
        self.event_type = event_type
        self.test_events = events
        self.opens = 0
    def list_devices(self, **kwargs): return ["/dev/input/event8"]
    def InputDevice(self, path):
        self.opens += 1
        events = []
        if self.opens >= 2:
            events = self.test_events if self.test_events is not None else [
                types.SimpleNamespace(type=self.event_type, code=self.axis_code, value=self.axis_value)
            ]
        axis_dict = {self.axis_code: (0, self.minimum, self.maximum), "events": events}
        return FakeInputDevice(path, axis_dict)


class FakeStdin:
    @staticmethod
    def fileno(): return 900


class AxisOutputRuntimeTests(unittest.TestCase):
    def run_daemon_for_axis(self, axis_code, axis_value, minimum, maximum, profile_obj,
                            event_type=FakeEcodes.EV_ABS, events=None):
        FakeUInput.instances.clear()
        fake_evdev = FakeEvdev(axis_code, axis_value, minimum, maximum, event_type=event_type, events=events)
        identity = {"path": "/dev/input/event8", "name": "Xbox One Controller",
                    "vendor": 0x045E, "product": 0x02FD, "uniq": "runtime-test-pad"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile_obj), encoding="utf-8")
            output = io.StringIO()
            select_calls = {"count": 0}
            def fake_select(readers, _writers, _errors, _timeout):
                select_calls["count"] += 1
                if select_calls["count"] == 1 and 432 in readers:
                    return ([432], [], [])
                return ([900], [], [])
            def fake_read(fd, _size):
                if fd == 900:
                    return b'{"cmd":"stop"}\n'
                raise AssertionError(f"Unexpected read from fd {fd}")
            with patch.object(ps, "evdev", fake_evdev), patch.object(ps, "ecodes", FakeEcodes), \
                 patch.object(ps.sys, "stdin", FakeStdin()), patch.object(ps.os, "set_blocking"), \
                 patch.object(ps.os, "read", side_effect=fake_read), patch.object(ps.select, "select", side_effect=fake_select), \
                 contextlib.redirect_stdout(output):
                code = ps.run_daemon("/dev/input/event8", str(path), identity)
        self.assertEqual(code, 0, output.getvalue())
        self.virtual_kwargs = FakeUInput.instances[0].kwargs
        return FakeUInput.instances[0].writes

    def test_dpad_hat_direction_generates_mapped_virtual_button(self):
        profile = {
            "buttons": {},
            "axes": {"ABS_HAT0Y": {"target": "ABS_HAT0Y", "invert": False, "deadzone": 0, "curve": 1}},
            "axis_buttons": {"ABS_HAT0Y": {"negative": "BTN_SOUTH", "positive": ps.DISABLED, "threshold": .55, "invert": False}},
        }
        writes = self.run_daemon_for_axis(17, -1, -1, 1, profile)
        self.assertIn((FakeEcodes.EV_KEY, 304, 1), writes)


    def test_digital_only_left_trigger_emits_analog_axis_without_virtual_digital_button(self):
        profile = {
            "buttons": {"BTN_TL2": "BTN_TL2"},
            "axes": {},
            "axis_buttons": {},
        }
        events = [
            types.SimpleNamespace(type=FakeEcodes.EV_KEY, code=312, value=1),
            types.SimpleNamespace(type=FakeEcodes.EV_KEY, code=312, value=0),
        ]
        writes = self.run_daemon_for_axis(312, 1, 0, 1, profile,
                                          event_type=FakeEcodes.EV_KEY, events=events)
        self.assertIn((FakeEcodes.EV_ABS, 2, 255), writes)
        self.assertIn((FakeEcodes.EV_ABS, 2, 0), writes)
        self.assertNotIn((FakeEcodes.EV_KEY, 312, 1), writes)
        self.assertNotIn((FakeEcodes.EV_KEY, 312, 0), writes)

    def test_digital_only_right_trigger_emits_analog_axis_without_virtual_digital_button(self):
        profile = {
            "buttons": {"BTN_TR2": "BTN_TR2"},
            "axes": {},
            "axis_buttons": {},
        }
        events = [
            types.SimpleNamespace(type=FakeEcodes.EV_KEY, code=313, value=1),
            types.SimpleNamespace(type=FakeEcodes.EV_KEY, code=313, value=0),
        ]
        writes = self.run_daemon_for_axis(313, 1, 0, 1, profile,
                                          event_type=FakeEcodes.EV_KEY, events=events)
        self.assertIn((FakeEcodes.EV_ABS, 5, 255), writes)
        self.assertIn((FakeEcodes.EV_ABS, 5, 0), writes)
        self.assertNotIn((FakeEcodes.EV_KEY, 313, 1), writes)
        self.assertNotIn((FakeEcodes.EV_KEY, 313, 0), writes)

    def test_analog_left_trigger_routed_to_right_trigger_emits_axis_only(self):
        profile = {
            "buttons": {},
            "axes": {"ABS_Z": {"target": "ABS_RZ", "invert": False, "deadzone": 0, "curve": 1}},
            "axis_buttons": {},
        }
        writes = self.run_daemon_for_axis(2, 230, 0, 255, profile)
        self.assertIn((FakeEcodes.EV_ABS, 5, 230), writes)
        self.assertNotIn((FakeEcodes.EV_KEY, 313, 1), writes)

    def test_virtual_controller_advertises_analog_triggers_without_digital_trigger_capabilities(self):
        self.run_daemon_for_axis(2, 120, 0, 255, {"buttons": {}, "axes": {}, "axis_buttons": {}})
        advertised_keys = self.virtual_kwargs["events"][FakeEcodes.EV_KEY]
        advertised_axes = {code for code, _absinfo in self.virtual_kwargs["events"][FakeEcodes.EV_ABS]}
        self.assertNotIn(FakeEcodes.ecodes["BTN_TL2"], advertised_keys)
        self.assertNotIn(FakeEcodes.ecodes["BTN_TR2"], advertised_keys)
        self.assertIn(FakeEcodes.ecodes["ABS_Z"], advertised_axes)
        self.assertIn(FakeEcodes.ecodes["ABS_RZ"], advertised_axes)
        self.assertNotIn(FakeEcodes.ecodes["ABS_BRAKE"], advertised_axes)
        self.assertNotIn(FakeEcodes.ecodes["ABS_GAS"], advertised_axes)


if __name__ == "__main__":
    unittest.main()
