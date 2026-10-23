"""Regression tests for LT/RT remapping with the normal, realistic profile."""
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
spec = importlib.util.spec_from_file_location("padshift_trigger_remap_test", MODULE_PATH)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)
PHYS_FD, STDIN_FD = 701, 702

class AbsInfo:
    def __init__(self, value=0, minimum=0, maximum=255, *_args):
        self.value, self.min, self.max = value, minimum, maximum

class Ecodes:
    EV_KEY, EV_ABS, BUS_USB = 1, 3, 3
    KEYS = {"BTN_SOUTH": 304, "BTN_EAST": 305, "BTN_NORTH": 307, "BTN_WEST": 308,
            "BTN_TL": 310, "BTN_TR": 311, "BTN_TL2": 312, "BTN_TR2": 313,
            "BTN_SELECT": 314, "BTN_START": 315, "BTN_MODE": 316,
            "BTN_THUMBL": 317, "BTN_THUMBR": 318,
            "BTN_DPAD_UP": 544, "BTN_DPAD_DOWN": 545, "BTN_DPAD_LEFT": 546, "BTN_DPAD_RIGHT": 547}
    ABS = {"ABS_X": 0, "ABS_Y": 1, "ABS_Z": 2, "ABS_RX": 3, "ABS_RY": 4, "ABS_RZ": 5,
           "ABS_GAS": 9, "ABS_BRAKE": 10, "ABS_HAT0X": 16, "ABS_HAT0Y": 17}
    ecodes = {**KEYS, **ABS}
    bytype = {EV_KEY: {v: k for k, v in KEYS.items()}, EV_ABS: {v: k for k, v in ABS.items()}}
    AbsInfo = AbsInfo
E = Ecodes

class Virtual:
    instances = []
    def __init__(self, **kwargs):
        self.keys, self.abs = {}, {}
        Virtual.instances.append(self)
    def write(self, etype, code, value):
        (self.keys if etype == E.EV_KEY else self.abs)[code] = value
    def syn(self): pass
    def close(self): pass

class Physical:
    def __init__(self, path, batches, buttons, axes):
        self.path, self.fd, self.name = path, PHYS_FD, "Xbox One Controller"
        self.info = types.SimpleNamespace(vendor=0x045E, product=0x02FD, bustype=3)
        self.uniq, self.phys = "trigger-test-pad", "usb-test"
        self.batches, self.buttons, self.axes = batches, buttons, axes
    def capabilities(self, absinfo=False):
        return {E.EV_KEY: [E.ecodes[n] for n in self.buttons],
                E.EV_ABS: [(E.ecodes[n], AbsInfo(v, lo, hi)) for n, (v, lo, hi) in self.axes.items()]}
    def grab(self): pass
    def ungrab(self): pass
    def close(self): pass
    def absinfo(self, code):
        for name, (v, lo, hi) in self.axes.items():
            if E.ecodes[name] == code: return AbsInfo(v, lo, hi)
        return AbsInfo()
    def read(self): return self.batches.pop(0) if self.batches else []

class FakeEvdev:
    UInput = Virtual
    AbsInfo = AbsInfo
    def __init__(self, batches, buttons, axes): self.batches, self.buttons, self.axes = batches, buttons, axes
    def list_devices(self, **_kwargs): return ["/dev/input/event8"]
    def InputDevice(self, path): return Physical(path, self.batches, self.buttons, self.axes)

def key(name, value): return types.SimpleNamespace(type=E.EV_KEY, code=E.ecodes[name], value=value)
def axis(name, value): return types.SimpleNamespace(type=E.EV_ABS, code=E.ecodes[name], value=value)

XBOX_BUTTONS = ["BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST", "BTN_TL", "BTN_TR",
                 "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR"]
XBOX_AXES = {"ABS_X": (0, -32768, 32767), "ABS_Y": (0, -32768, 32767),
             "ABS_RX": (0, -32768, 32767), "ABS_RY": (0, -32768, 32767),
             "ABS_Z": (0, 0, 1023), "ABS_RZ": (0, 0, 1023),
             "ABS_HAT0X": (0, -1, 1), "ABS_HAT0Y": (0, -1, 1)}
FULL, PARTIAL = 1023, 410

class TriggerRemapTests(unittest.TestCase):
    def simulate(self, overrides, steps, buttons=XBOX_BUTTONS, axes=XBOX_AXES):
        Virtual.instances.clear()
        fake = FakeEvdev([list(batch) for batch in steps], buttons, axes)
        axis_infos = {name: {"code": E.ecodes[name], "min": lo, "max": hi, "value": v}
                      for name, (v, lo, hi) in axes.items()}
        profile = ps.merge_profile(overrides, sorted(buttons), axis_infos)
        snapshots, calls = [], {"n": 0}
        def fake_select(readers, _w, _e, _t):
            n = calls["n"]; calls["n"] += 1
            if n > 0 and Virtual.instances:
                v = Virtual.instances[0]; snapshots.append((dict(v.keys), dict(v.abs)))
            return ([PHYS_FD], [], []) if n < len(steps) and PHYS_FD in readers else ([STDIN_FD], [], [])
        identity = {"path": "/dev/input/event8", "name": "Xbox One Controller", "vendor": 0x045E,
                    "product": 0x02FD, "uniq": "trigger-test-pad"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"; path.write_text(json.dumps(profile), encoding="utf-8")
            output = io.StringIO()
            with patch.object(ps, "evdev", fake), patch.object(ps, "ecodes", E), \
                 patch.object(ps, "PROFILES_DIR", Path(directory)), \
                 patch.object(ps.sys, "stdin", types.SimpleNamespace(fileno=lambda: STDIN_FD)), \
                 patch.object(ps.os, "set_blocking"), \
                 patch.object(ps.os, "read", side_effect=lambda *_a: b'{"cmd":"stop"}\n'), \
                 patch.object(ps.select, "select", side_effect=fake_select), contextlib.redirect_stdout(output):
                code = ps.run_daemon("/dev/input/event8", str(path), identity)
        self.assertEqual(code, 0, output.getvalue())
        return snapshots

    @staticmethod
    def av(s, name): return s[1].get(E.ecodes[name], 0)
    @staticmethod
    def kv(s, name): return s[0].get(E.ecodes[name], 0)

    def test_untouched_analog_trigger_keeps_its_travel(self):
        snaps = self.simulate({}, [[axis("ABS_Z", PARTIAL)], [axis("ABS_Z", FULL)], [axis("ABS_Z", 0)]])
        self.assertEqual([self.av(s, "ABS_Z") for s in snaps], [102, 255, 0])

    def test_bumper_mapped_to_left_trigger_moves_analog_axis(self):
        snaps = self.simulate({"buttons": {"BTN_TL": "BTN_TL2"}}, [[key("BTN_TL", 1)], [key("BTN_TL", 0)]])
        self.assertEqual(self.av(snaps[0], "ABS_Z"), 255); self.assertEqual(self.kv(snaps[0], "BTN_TL2"), 0)
        self.assertEqual(self.av(snaps[1], "ABS_Z"), 0); self.assertEqual(self.kv(snaps[1], "BTN_TL2"), 0)

    def test_face_button_mapped_to_right_trigger_moves_axis(self):
        snaps = self.simulate({"buttons": {"BTN_SOUTH": "BTN_TR2"}}, [[key("BTN_SOUTH", 1)], [key("BTN_SOUTH", 0)]])
        self.assertEqual(self.av(snaps[0], "ABS_RZ"), 255); self.assertEqual(self.av(snaps[0], "ABS_Z"), 0)
        self.assertEqual(self.kv(snaps[0], "BTN_TR2"), 0)
        self.assertEqual(self.av(snaps[1], "ABS_RZ"), 0)

    def test_mapped_button_is_not_cancelled_by_releasing_real_trigger(self):
        snaps = self.simulate({"buttons": {"BTN_TL": "BTN_TL2"}}, [
            [key("BTN_TL", 1)], [axis("ABS_Z", FULL)], [axis("ABS_Z", 0)], [key("BTN_TL", 0)]])
        self.assertEqual([self.av(s, "ABS_Z") for s in snaps], [255, 255, 255, 0])

    def test_physical_trigger_mapped_to_button_does_not_leak_analog(self):
        overrides = {"axis_buttons": {"ABS_Z": {"positive": "BTN_TL", "negative": ps.DISABLED,
                                                     "threshold": 0.55, "invert": False}}}
        snaps = self.simulate(overrides, [[axis("ABS_Z", PARTIAL)], [axis("ABS_Z", FULL)],
                                          [axis("ABS_Z", PARTIAL)], [axis("ABS_Z", 0)]])
        self.assertEqual([self.av(s, "ABS_Z") for s in snaps], [0, 0, 0, 0])
        self.assertEqual([self.kv(s, "BTN_TL") for s in snaps], [0, 1, 0, 0])

    def test_full_bumper_trigger_swap(self):
        overrides = {"buttons": {"BTN_TL": "BTN_TL2", "BTN_TR": "BTN_TR2"},
                     "axes": {"ABS_Z": {"target": ps.DISABLED}, "ABS_RZ": {"target": ps.DISABLED}},
                     "axis_buttons": {"ABS_Z": {"positive": "BTN_TL", "negative": ps.DISABLED, "threshold": 0.55, "invert": False},
                                      "ABS_RZ": {"positive": "BTN_TR", "negative": ps.DISABLED, "threshold": 0.55, "invert": False}}}
        snaps = self.simulate(overrides, [[key("BTN_TL", 1)], [key("BTN_TL", 0)], [key("BTN_TR", 1)], [key("BTN_TR", 0)],
                                          [axis("ABS_Z", FULL)], [axis("ABS_Z", 0)], [axis("ABS_RZ", FULL)], [axis("ABS_RZ", 0)]])
        self.assertEqual(self.av(snaps[0], "ABS_Z"), 255); self.assertEqual(self.kv(snaps[0], "BTN_TL2"), 0)
        self.assertEqual(self.av(snaps[2], "ABS_RZ"), 255); self.assertEqual(self.kv(snaps[2], "BTN_TR2"), 0)
        self.assertEqual(self.kv(snaps[4], "BTN_TL"), 1); self.assertEqual(self.av(snaps[4], "ABS_Z"), 0)
        self.assertEqual(self.kv(snaps[6], "BTN_TR"), 1); self.assertEqual(self.av(snaps[6], "ABS_RZ"), 0)

    def test_lt_rt_axis_swap_still_works(self):
        overrides = {"axes": {"ABS_Z": {"target": "ABS_RZ"}, "ABS_RZ": {"target": "ABS_Z"}}}
        snaps = self.simulate(overrides, [[axis("ABS_Z", FULL)], [axis("ABS_Z", 0)], [axis("ABS_RZ", FULL)]])
        self.assertEqual((self.av(snaps[0], "ABS_RZ"), self.av(snaps[0], "ABS_Z")), (255, 0))
        self.assertEqual((self.av(snaps[2], "ABS_Z"), self.av(snaps[2], "ABS_RZ")), (255, 0))

    def test_pad_reporting_analog_and_digital_trigger_keeps_analog_travel(self):
        buttons = XBOX_BUTTONS + ["BTN_TL2", "BTN_TR2"]
        snaps = self.simulate({}, [[axis("ABS_Z", 614)], [key("BTN_TL2", 1)], [key("BTN_TL2", 0)]], buttons=buttons)
        self.assertEqual([self.av(s, "ABS_Z") for s in snaps], [153, 153, 153])

    def test_digital_only_trigger_pad_gets_analog_trigger(self):
        buttons = XBOX_BUTTONS + ["BTN_TL2", "BTN_TR2"]
        axes = {name: spec for name, spec in XBOX_AXES.items() if name not in ("ABS_Z", "ABS_RZ")}
        snaps = self.simulate({}, [[key("BTN_TL2", 1)], [key("BTN_TL2", 0)]], buttons=buttons, axes=axes)
        self.assertEqual([self.av(s, "ABS_Z") for s in snaps], [255, 0])
        self.assertEqual([self.kv(s, "BTN_TL2") for s in snaps], [0, 0])

if __name__ == "__main__": unittest.main()
