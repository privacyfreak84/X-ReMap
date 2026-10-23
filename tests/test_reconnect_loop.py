"""Hardware-free smoke test of the daemon waiting for a reconnected controller."""
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
spec = importlib.util.spec_from_file_location("padshift_reconnect_loop_test", MODULE_PATH)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class FakeAbsInfo:
    def __init__(self, value=0, minimum=-32768, maximum=32767, *args):
        self.value, self.min, self.max = value, minimum, maximum


class FakeInputDevice:
    def __init__(self, path, instance_number):
        self.path = path
        self.instance_number = instance_number
        self.name = "Xbox Wireless Controller"
        self.fd = 432
        self.info = types.SimpleNamespace(vendor=0x045E, product=0x02FD, bustype=3)
        self.uniq = "controller-unique-id"
        self.phys = "bluetooth-input"
        self.closed = False

    def capabilities(self, absinfo=False):
        keys = [304, 305, 307, 308, 310, 311]
        axes = [(0, FakeAbsInfo()), (1, FakeAbsInfo()), (3, FakeAbsInfo()), (4, FakeAbsInfo())]
        return {FakeEcodes.EV_KEY: keys, FakeEcodes.EV_ABS: axes}

    def grab(self):
        return None

    def ungrab(self):
        return None

    def read(self):
        # The attached event7 node disappears after the first input read.
        if self.path == "/dev/input/event7" and self.instance_number == 2:
            raise OSError("simulated unplug")
        return []

    def close(self):
        self.closed = True


class FakeUInput:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.writes = []
        self.__class__.instances.append(self)

    def write(self, *args):
        self.writes.append(args)

    def syn(self):
        pass

    def close(self):
        self.closed = True


class FakeEcodes:
    EV_KEY = 1
    EV_ABS = 3
    BUS_USB = 3
    ecodes = {
        "BTN_SOUTH": 304, "BTN_EAST": 305, "BTN_NORTH": 307, "BTN_WEST": 308,
        "BTN_TL": 310, "BTN_TR": 311, "BTN_TL2": 312, "BTN_TR2": 313,
        "BTN_SELECT": 314, "BTN_START": 315, "BTN_MODE": 316,
        "BTN_THUMBL": 317, "BTN_THUMBR": 318,
        "BTN_DPAD_UP": 320, "BTN_DPAD_DOWN": 321, "BTN_DPAD_LEFT": 322, "BTN_DPAD_RIGHT": 323,
        "ABS_X": 0, "ABS_Y": 1, "ABS_Z": 2, "ABS_RX": 3, "ABS_RY": 4,
        "ABS_RZ": 5, "ABS_HAT0X": 16, "ABS_HAT0Y": 17, "ABS_GAS": 9, "ABS_BRAKE": 10,
    }
    bytype = {
        EV_KEY: {304: "BTN_SOUTH", 305: "BTN_EAST", 307: "BTN_NORTH", 308: "BTN_WEST",
                 310: "BTN_TL", 311: "BTN_TR", 312: "BTN_TL2", 313: "BTN_TR2",
                 314: "BTN_SELECT", 315: "BTN_START", 316: "BTN_MODE", 317: "BTN_THUMBL", 318: "BTN_THUMBR",
                 320: "BTN_DPAD_UP", 321: "BTN_DPAD_DOWN", 322: "BTN_DPAD_LEFT", 323: "BTN_DPAD_RIGHT"},
        EV_ABS: {0: "ABS_X", 1: "ABS_Y", 2: "ABS_Z", 3: "ABS_RX", 4: "ABS_RY", 5: "ABS_RZ",
                 16: "ABS_HAT0X", 17: "ABS_HAT0Y", 9: "ABS_GAS", 10: "ABS_BRAKE"},
    }
    AbsInfo = FakeAbsInfo


class FakeEvdev:
    UInput = FakeUInput
    AbsInfo = FakeAbsInfo

    def __init__(self):
        self.list_calls = 0
        self.open_calls = 0

    def list_devices(self, **kwargs):
        self.list_calls += 1
        # Controller is present, disappears, then returns on a different event path.
        if self.list_calls == 1:
            return ["/dev/input/event7"]
        if self.list_calls == 2:
            return []
        return ["/dev/input/event19"]

    def InputDevice(self, path):
        self.open_calls += 1
        return FakeInputDevice(path, self.open_calls)


class FakeStdin:
    @staticmethod
    def fileno():
        return 900


class ReconnectLoopTests(unittest.TestCase):
    def test_virtual_pad_waits_then_attaches_to_reconnected_controller(self):
        FakeUInput.instances.clear()
        fake_evdev = FakeEvdev()
        identity = {"path": "/dev/input/event7", "name": "Xbox Wireless Controller",
                    "vendor": 0x045E, "product": 0x02FD, "uniq": "controller-unique-id"}
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "layout.json"
            profile.write_text(json.dumps({"buttons": {"BTN_SOUTH": "BTN_WEST"}, "axes": {}}), encoding="utf-8")
            output = io.StringIO()
            ticks = iter(range(100))
            def monotonic():
                return float(next(ticks))
            select_calls = {"count": 0}
            def fake_select(readers, _writers, _errors, _timeout):
                select_calls["count"] += 1
                # First, deliver a read on the active pad (which simulates unplug); next
                # allow the scan to find no device; finally let it reappear, then stop.
                if select_calls["count"] == 1:
                    return ([432], [], [])
                if select_calls["count"] == 2:
                    return ([], [], [])
                return ([900], [], [])
            def fake_read(fd, _size):
                if fd == 900:
                    return b'{"cmd":"stop"}\n'
                raise AssertionError(f"Unexpected read from fd {fd}")
            with patch.object(ps, "evdev", fake_evdev), patch.object(ps, "ecodes", FakeEcodes), \
                 patch.object(ps.sys, "stdin", FakeStdin()), patch.object(ps.os, "set_blocking"), \
                 patch.object(ps.os, "read", side_effect=fake_read), patch.object(ps.select, "select", side_effect=fake_select), \
                 patch.object(ps.time, "monotonic", side_effect=monotonic), contextlib.redirect_stdout(output):
                code = ps.run_daemon("/dev/input/event7", str(profile), identity)

        messages = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        statuses = [item.get("message", "") for item in messages if item.get("type") == "status"]
        self.assertEqual(code, 0)
        self.assertTrue(any("Connected to Xbox Wireless Controller (/dev/input/event7)" in status for status in statuses), statuses)
        self.assertTrue(any("Controller disconnected" in status for status in statuses), statuses)
        self.assertTrue(any("Waiting for Xbox Wireless Controller" in status for status in statuses), statuses)
        self.assertTrue(any("Connected to Xbox Wireless Controller (/dev/input/event19)" in status for status in statuses), statuses)
        self.assertEqual(len(FakeUInput.instances), 1, "virtual controller should be created only once across a disconnect/reconnect cycle")
        self.assertTrue(FakeUInput.instances[0].closed)


if __name__ == "__main__":
    unittest.main()
