"""Hardware-free tests for PadShift's event-capture thread."""
import importlib.util
import queue
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "padshift.py"
spec = importlib.util.spec_from_file_location("padshift_capture_test", MODULE_PATH)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class FakeEcodes:
    EV_KEY = 1
    EV_ABS = 3
    ecodes = {
        "BTN_SOUTH": 304,
        "BTN_EAST": 305,
        "ABS_Y": 1,
        "ABS_Z": 2,
        "ABS_RY": 4,
        "ABS_HAT0Y": 17,
    }
    bytype = {
        EV_KEY: {304: "BTN_SOUTH", 305: "BTN_EAST"},
        EV_ABS: {1: "ABS_Y", 2: "ABS_Z", 4: "ABS_RY", 17: "ABS_HAT0Y"},
    }


class FakeAbsInfo:
    def __init__(self, value=0, minimum=-32768, maximum=32767):
        self.value = value
        self.min = minimum
        self.max = maximum


class FakeInputDevice:
    def __init__(self, events):
        self.name = "Fake Xbox One Controller"
        self.fd = 123
        self.events = list(events)
        self.closed = False

    def absinfo(self, _code):
        return FakeAbsInfo()

    def read(self):
        events, self.events = self.events, []
        return events

    def close(self):
        self.closed = True


class CaptureThreadTests(unittest.TestCase):
    def run_capture(self, event, device_snapshot, button_target=None, axis_target=None, direction=None, mode="button"):
        fake_input = FakeInputDevice([event])
        ps.evdev = types.SimpleNamespace(InputDevice=lambda _path: fake_input)
        ps.ecodes = FakeEcodes
        app = object.__new__(ps.PadShiftApp)
        app.capture_queue = queue.Queue()
        cancel = threading.Event()
        spec = {"button": button_target, "axis": axis_target, "direction": direction, "mode": mode}

        def fake_select(readers, _writers, _errors, _timeout):
            return (readers, [], []) if fake_input.events else ([], [], [])

        with patch.object(ps.select, "select", side_effect=fake_select), patch.object(ps.os, "set_blocking") as set_blocking:
            app._capture_input_thread(device_snapshot, "face_x", spec, cancel, 7)
        set_blocking.assert_called_once_with(fake_input.fd, False)
        messages = []
        while not app.capture_queue.empty():
            messages.append(app.capture_queue.get_nowait())
        self.assertTrue(fake_input.closed)
        complete = next(item for item in messages if item["event"] == "complete")
        return messages, complete

    def test_button_press_is_captured_and_reported(self):
        event = types.SimpleNamespace(type=FakeEcodes.EV_KEY, code=304, value=1)
        snapshot = {"path": "/dev/input/event0", "buttons": {"BTN_SOUTH"}, "axes": {}}
        messages, complete = self.run_capture(event, snapshot, button_target="BTN_WEST")
        self.assertTrue(any(item["event"] == "opened" for item in messages))
        self.assertEqual(complete["result"]["source"], "BTN_SOUTH")
        self.assertEqual(complete["result"]["target"], "BTN_WEST")
        self.assertFalse(complete["timed_out"])


    def test_hat_dpad_can_be_learned_as_a_digital_button(self):
        event = types.SimpleNamespace(type=FakeEcodes.EV_ABS, code=17, value=-1)
        snapshot = {"path": "/dev/input/event0", "buttons": set(),
                    "axes": {"ABS_HAT0Y": {"code": 17, "value": 0, "min": -1, "max": 1}}}
        messages, complete = self.run_capture(event, snapshot, button_target="BTN_SOUTH")
        self.assertEqual(complete["result"]["kind"], "axis_button")
        self.assertEqual(complete["result"]["source"], "ABS_HAT0Y")
        self.assertEqual(complete["result"]["target"], "BTN_SOUTH")
        self.assertEqual(complete["result"]["source_sign"], -1)

    def test_analog_trigger_can_be_learned_as_a_digital_button(self):
        event = types.SimpleNamespace(type=FakeEcodes.EV_ABS, code=2, value=220)
        snapshot = {"path": "/dev/input/event0", "buttons": set(),
                    "axes": {"ABS_Z": {"code": 2, "value": 0, "min": 0, "max": 255}}}
        messages, complete = self.run_capture(event, snapshot, button_target="BTN_EAST")
        self.assertEqual(complete["result"]["kind"], "axis_button")
        self.assertEqual(complete["result"]["source"], "ABS_Z")
        self.assertEqual(complete["result"]["target"], "BTN_EAST")
        self.assertEqual(complete["result"]["source_sign"], 1)

    def test_stick_direction_is_captured_and_reported(self):
        event = types.SimpleNamespace(type=FakeEcodes.EV_ABS, code=4, value=30000)
        snapshot = {
            "path": "/dev/input/event0", "buttons": set(),
            "axes": {"ABS_RY": {"code": 4, "value": 0, "min": -32768, "max": 32767}},
        }
        messages, complete = self.run_capture(event, snapshot, axis_target="ABS_Y", direction=-1, mode="stick")
        self.assertTrue(any(item["event"] == "opened" for item in messages))
        self.assertTrue(any(item["event"] == "activity" for item in messages))
        self.assertEqual(complete["result"]["source"], "ABS_RY")
        self.assertEqual(complete["result"]["source_sign"], 1)
        self.assertEqual(complete["result"]["target_sign"], -1)


if __name__ == "__main__":
    unittest.main()
