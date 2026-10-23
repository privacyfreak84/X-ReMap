#!/usr/bin/env python3
"""PadShift: a per-profile Linux gamepad remapper built on evdev/uinput."""
from __future__ import annotations

import argparse
import json
import math
import os
import select
import signal
import shutil
import subprocess
import sys
import time
import traceback
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    import evdev
    from evdev import ecodes
except ImportError:  # GUI can still show a useful setup message.
    evdev = None
    ecodes = None

APP_NAME = "PadShift"
APP_VERSION = "0.8.4"
BOTTLES_FLATPAK_ID = "com.usebottles.bottles"
CONFIG_DIR = Path.home() / ".config" / "padshift"
PROFILES_DIR = CONFIG_DIR / "profiles"
SETTINGS_PATH = CONFIG_DIR / "settings.json"

# Linux's standard gamepad button names, annotated with their usual Xbox positions.
BUTTON_LABELS = {
    "BTN_SOUTH": "A · bottom face button",
    "BTN_EAST": "B · right face button",
    "BTN_WEST": "X · left face button",
    "BTN_NORTH": "Y · top face button",
    "BTN_TL": "LB · left bumper",
    "BTN_TR": "RB · right bumper",
    "BTN_TL2": "LT · left trigger (digital)",
    "BTN_TR2": "RT · right trigger (digital)",
    "BTN_SELECT": "View / Back",
    "BTN_START": "Menu / Start",
    "BTN_MODE": "Xbox / Guide",
    "BTN_THUMBL": "L3 · press left stick",
    "BTN_THUMBR": "R3 · press right stick",
    "BTN_DPAD_UP": "D-pad up",
    "BTN_DPAD_DOWN": "D-pad down",
    "BTN_DPAD_LEFT": "D-pad left",
    "BTN_DPAD_RIGHT": "D-pad right",
}
BUTTON_TARGETS = list(BUTTON_LABELS)
AXIS_LABELS = {
    "ABS_X": "Left stick · horizontal",
    "ABS_Y": "Left stick · vertical",
    "ABS_RX": "Right stick · horizontal",
    "ABS_RY": "Right stick · vertical",
    "ABS_Z": "Left trigger · analog",
    "ABS_RZ": "Right trigger · analog",
    "ABS_HAT0X": "D-pad · horizontal axis",
    "ABS_HAT0Y": "D-pad · vertical axis",
    "ABS_GAS": "Gas / alternate trigger axis",
    "ABS_BRAKE": "Brake / alternate trigger axis",
}
# The two generic axes ABS_BRAKE / ABS_GAS are useful *physical input aliases*
# on Android-style/Bluetooth pads, but must not be advertised by the virtual Xbox
# device. SDL's Linux fallback mapping prefers BRAKE/GAS over ABS_Z/ABS_RZ, which
# can make a game read never-updated axes instead of the Xbox trigger axes.
AXIS_TARGETS = [name for name in AXIS_LABELS if name not in {"ABS_BRAKE", "ABS_GAS"}]
SOURCE_AXIS_NAMES = list(AXIS_LABELS)
STICK_AXES = {"ABS_X", "ABS_Y", "ABS_RX", "ABS_RY"}
HAT_AXES = {"ABS_HAT0X", "ABS_HAT0Y"}
TRIGGER_AXES = {"ABS_Z", "ABS_RZ", "ABS_GAS", "ABS_BRAKE"}  # physical/source trigger aliases included
VIRTUAL_TRIGGER_AXES = {"ABS_Z", "ABS_RZ"}
ALTERNATE_TRIGGER_TARGETS = {"ABS_BRAKE": "ABS_Z", "ABS_GAS": "ABS_RZ"}
STANDARD_BUTTON_CODES = list(BUTTON_TARGETS)
STANDARD_AXIS_CODES = list(AXIS_TARGETS)
DISABLED = "(Disabled)"
DISCOVERY_ERRORS: list[str] = []


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalise_axis(raw: int, minimum: int, maximum: int, source_name: str) -> tuple[float, bool]:
    """Return an axis in [-1,1] for centred axes or [0,1] for unipolar axes."""
    if maximum <= minimum:
        return 0.0, source_name in STICK_AXES or source_name in HAT_AXES
    centered = source_name in STICK_AXES or source_name in HAT_AXES
    if centered:
        midpoint = (minimum + maximum) / 2.0
        if raw >= midpoint:
            span = max(1.0, maximum - midpoint)
            value = (raw - midpoint) / span
        else:
            span = max(1.0, midpoint - minimum)
            value = (raw - midpoint) / span
        return _clamp(value, -1.0, 1.0), True
    return _clamp((raw - minimum) / float(maximum - minimum), 0.0, 1.0), False


def transform_axis(raw: int, minimum: int, maximum: int, source_name: str,
                   target_name: str, invert: bool = False, deadzone: float = 0.0,
                   curve: float = 1.0) -> int | None:
    """Convert a physical absolute axis value into the virtual axis's range."""
    if target_name not in AXIS_TARGETS:
        return None
    normalised, centered = normalise_axis(raw, minimum, maximum, source_name)
    deadzone = _clamp(float(deadzone), 0.0, 0.95)
    curve = _clamp(float(curve), 0.25, 4.0)
    if invert:
        normalised = -normalised if centered else 1.0 - normalised
    if centered:
        magnitude = abs(normalised)
        if magnitude <= deadzone:
            normalised = 0.0
        else:
            normalised = math.copysign((magnitude - deadzone) / max(0.0001, 1.0 - deadzone), normalised)
    else:
        if normalised <= deadzone:
            normalised = 0.0
        else:
            normalised = (normalised - deadzone) / max(0.0001, 1.0 - deadzone)
    normalised = math.copysign(abs(normalised) ** curve, normalised) if centered else normalised ** curve

    if target_name in STICK_AXES:
        # A unipolar source (e.g. a trigger) pushes from centre in one direction.
        return int(round(_clamp(normalised, -1.0, 1.0) * 32767))
    if target_name in TRIGGER_AXES:
        # A stick mapped to a trigger uses its positive half; a trigger maps 0..full.
        if centered:
            normalised = max(0.0, normalised)
        return int(round(_clamp(normalised, 0.0, 1.0) * 255))
    if target_name in HAT_AXES:
        # Hat switches are digital in the evdev protocol. Sticks need a firm threshold.
        threshold = max(0.55, deadzone)
        if centered:
            return -1 if normalised <= -threshold else (1 if normalised >= threshold else 0)
        return 1 if normalised >= threshold else 0
    return None


def default_profile(button_names: list[str], axis_infos: dict[str, dict]) -> dict:
    buttons = {name: (name if name in BUTTON_TARGETS else DISABLED) for name in button_names}
    axes = {}
    axis_buttons = {}
    for name in axis_infos:
        target = name if name in AXIS_TARGETS else DISABLED
        # Some Linux drivers expose gamepad triggers using the generic ABS_BRAKE /
        # ABS_GAS names. Route those to the standard Xbox/Linux trigger axes when
        # the canonical ABS_Z / ABS_RZ axes are not present on the physical device.
        if name == "ABS_BRAKE" and "ABS_Z" not in axis_infos:
            target = "ABS_Z"
        elif name == "ABS_GAS" and "ABS_RZ" not in axis_infos:
            target = "ABS_RZ"
        default_deadzone = 0.08 if name in STICK_AXES else 0.0
        axes[name] = {"target": target, "invert": False, "deadzone": default_deadzone, "curve": 1.0}
        # Directional analog inputs (especially D-pad hats and analog triggers) may
        # also be learned as digital button presses.
        axis_buttons[name] = {"negative": DISABLED, "positive": DISABLED,
                              "threshold": 0.55, "invert": False}
    return {"format": 2, "buttons": buttons, "axes": axes, "axis_buttons": axis_buttons}


def merge_profile(profile: dict, button_names: list[str], axis_infos: dict[str, dict]) -> dict:
    profile = profile if isinstance(profile, dict) else {}
    base = default_profile(button_names, axis_infos)
    user_buttons = profile.get("buttons", {})
    user_axes = profile.get("axes", {})
    user_axis_buttons = profile.get("axis_buttons", {})
    for name in button_names:
        value = user_buttons.get(name, base["buttons"][name])
        base["buttons"][name] = value if value in BUTTON_TARGETS or value == DISABLED else DISABLED
    for name in axis_infos:
        saved = user_axes.get(name, {})
        default_target = base["axes"][name]["target"]
        target = saved.get("target", default_target)
        # Older profiles could target generic brake/gas axes directly. Keep these
        # usable, but normalize the destination to the virtual Xbox trigger axes.
        target = ALTERNATE_TRIGGER_TARGETS.get(target, target)
        if name in ("ABS_BRAKE", "ABS_GAS") and target == name and default_target != name:
            target = default_target
        base["axes"][name] = {
            "target": target if target in AXIS_TARGETS or target == DISABLED else DISABLED,
            "invert": bool(saved.get("invert", False)),
            "deadzone": _clamp(float(saved.get("deadzone", base["axes"][name]["deadzone"])), 0.0, 0.95),
            "curve": _clamp(float(saved.get("curve", 1.0)), 0.25, 4.0),
        }
        saved_buttons = user_axis_buttons.get(name, {})
        base["axis_buttons"][name] = {
            "negative": saved_buttons.get("negative", DISABLED) if saved_buttons.get("negative", DISABLED) in BUTTON_TARGETS or saved_buttons.get("negative", DISABLED) == DISABLED else DISABLED,
            "positive": saved_buttons.get("positive", DISABLED) if saved_buttons.get("positive", DISABLED) in BUTTON_TARGETS or saved_buttons.get("positive", DISABLED) == DISABLED else DISABLED,
            "threshold": _clamp(float(saved_buttons.get("threshold", 0.55)), 0.15, 0.95),
            "invert": bool(saved_buttons.get("invert", False)),
        }
    return base


def axis_button_for_value(raw: int, minimum: int, maximum: int, source_name: str,
                          config: dict | None) -> str | None:
    """Return a configured digital target while a physical axis is pushed far enough."""
    if not isinstance(config, dict):
        return None
    value, centered = normalise_axis(raw, minimum, maximum, source_name)
    if config.get("invert", False):
        value = -value if centered else 1.0 - value
    threshold = _clamp(float(config.get("threshold", 0.55)), 0.15, 0.95)
    if centered:
        if value <= -threshold:
            direction = "negative"
        elif value >= threshold:
            direction = "positive"
        else:
            return None
    else:
        if value >= threshold:
            direction = "positive"
        else:
            return None
    target = config.get(direction, DISABLED)
    return target if target in BUTTON_TARGETS else None


def digital_button_for_axis_output(target_name: str, value: int) -> str | None:
    """Return digital D-pad equivalents; keep analogue triggers axis-only for compatibility."""
    if target_name == "ABS_HAT0X":
        return "BTN_DPAD_LEFT" if value < 0 else "BTN_DPAD_RIGHT" if value > 0 else None
    if target_name == "ABS_HAT0Y":
        return "BTN_DPAD_UP" if value < 0 else "BTN_DPAD_DOWN" if value > 0 else None
    # Do not mirror ABS_Z / ABS_RZ as BTN_TL2 / BTN_TR2. Some older
    # joydev/SDL/gamepad paths interpret the advertised digital trigger keys as
    # digital-only trigger style and stop consuming the analogue axes. Digital
    # inputs *mapped by the user* to LT/RT are still converted internally into
    # full-range analogue axis values by refresh_trigger_outputs().
    return None


def _code_name(event_type: int, code: int) -> str:
    """Return a stable, canonical evdev name for a code."""
    if not ecodes:
        return f"CODE_{code}"
    preferred = BUTTON_TARGETS if event_type == ecodes.EV_KEY else AXIS_TARGETS
    byname = ecodes.ecodes
    for name in preferred:
        if byname.get(name) == code:
            return name
    table = ecodes.bytype.get(event_type, {})
    value = table.get(code, f"CODE_{code}")
    if isinstance(value, (tuple, list)):
        candidates = list(value)
    else:
        candidates = [value]
    if event_type == ecodes.EV_KEY:
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.startswith("BTN_"):
                return candidate
    for candidate in candidates:
        if isinstance(candidate, str) and (candidate.startswith("ABS_") or candidate.startswith("BTN_")):
            return candidate
    return str(candidates[0])


def _code_value(name: str) -> int | None:
    if not ecodes:
        return None
    value = ecodes.ecodes.get(name)
    return int(value) if isinstance(value, int) else None


def _friendly_button(name: str) -> str:
    return f"{BUTTON_LABELS.get(name, name)}  [{name}]"


def _friendly_axis(name: str) -> str:
    return f"{AXIS_LABELS.get(name, name)}  [{name}]"



def controller_identity(device: dict) -> dict:
    """Small stable identity payload used to find a controller after its eventN changes."""
    if not isinstance(device, dict):
        return {}
    result = {
        "path": str(device.get("path", "")),
        "name": str(device.get("name", "")),
        "vendor": int(device.get("vendor", 0) or 0),
        "product": int(device.get("product", 0) or 0),
        "bustype": int(device.get("bustype", 0) or 0),
        "uniq": str(device.get("uniq", "") or ""),
        "phys": str(device.get("phys", "") or ""),
    }
    return result if any((result["path"], result["name"], result["vendor"], result["product"], result["uniq"])) else {}


def device_matches_identity(candidate: dict, identity: dict | None) -> bool:
    """Match a reconnected event node to a saved physical controller identity."""
    if not identity:
        return False
    wanted_vendor = int(identity.get("vendor", 0) or 0)
    wanted_product = int(identity.get("product", 0) or 0)
    candidate_vendor = int(candidate.get("vendor", 0) or 0)
    candidate_product = int(candidate.get("product", 0) or 0)
    wanted_name = str(identity.get("name", "")).strip().casefold()
    candidate_name = str(candidate.get("name", "")).strip().casefold()
    wanted_uniq = str(identity.get("uniq", "") or "").strip()
    candidate_uniq = str(candidate.get("uniq", "") or "").strip()

    # Vendor/product IDs are stronger than eventN, which can change on reconnect.
    if wanted_vendor and wanted_product and candidate_vendor and candidate_product:
        if (wanted_vendor, wanted_product) != (candidate_vendor, candidate_product):
            return False
        if wanted_uniq and candidate_uniq and wanted_uniq != candidate_uniq:
            return False
        return True
    # Some Bluetooth/HID nodes don't report IDs consistently. Fall back to name.
    if wanted_name and candidate_name and wanted_name == candidate_name:
        if wanted_uniq and candidate_uniq and wanted_uniq != candidate_uniq:
            return False
        return True
    # Last resort, permit the original event path only if available.
    return bool(identity.get("path")) and str(candidate.get("path", "")) == str(identity.get("path", ""))


def load_settings_file() -> dict:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def discover_controllers() -> list[dict]:
    """Find controller event nodes and retain useful diagnostics for skipped nodes."""
    global DISCOVERY_ERRORS
    DISCOVERY_ERRORS = []
    if not evdev:
        DISCOVERY_ERRORS.append("python-evdev is not installed")
        return []
    devices = []
    try:
        paths = evdev.list_devices(writable=False)
    except TypeError:  # python-evdev versions before the writable keyword
        try:
            paths = evdev.list_devices()
        except Exception as exc:
            DISCOVERY_ERRORS.append(f"Could not list /dev/input devices: {exc}")
            return []
    except Exception as exc:
        DISCOVERY_ERRORS.append(f"Could not list /dev/input devices: {exc}")
        return []
    for path in paths:
        dev = None
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities(absinfo=True)
            key_codes = list(caps.get(ecodes.EV_KEY, []))
            key_codes = [int(c) for c in key_codes if isinstance(c, int)]
            abs_entries = caps.get(ecodes.EV_ABS, [])
            axis_infos = {}
            for entry in abs_entries:
                if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                    continue
                code, info = int(entry[0]), entry[1]
                name = _code_name(ecodes.EV_ABS, code)
                if not name.startswith("ABS_"):
                    continue
                axis_infos[name] = {
                    "code": code,
                    "value": int(getattr(info, "value", 0)),
                    "min": int(getattr(info, "min", -32768)),
                    "max": int(getattr(info, "max", 32767)),
                }
            button_names = sorted({_code_name(ecodes.EV_KEY, c) for c in key_codes
                                   if 0x100 <= c <= 0x2FF and _code_name(ecodes.EV_KEY, c).startswith("BTN_")})
            name_lower = (dev.name or "").lower()
            gamepad_signature = any(n in button_names for n in ("BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST"))
            has_stick = any(axis in axis_infos for axis in STICK_AXES)
            named_like_controller = any(word in name_lower for word in ("xbox", "gamepad", "controller", "joystick"))
            controller_controls = any(n in button_names for n in ("BTN_TL", "BTN_TR", "BTN_TL2", "BTN_TR2", "BTN_THUMBL", "BTN_THUMBR", "BTN_DPAD_UP", "BTN_DPAD_DOWN", "BTN_DPAD_LEFT", "BTN_DPAD_RIGHT"))
            if not (gamepad_signature or has_stick or (named_like_controller and controller_controls)):
                continue
            if not button_names and not axis_infos:
                DISCOVERY_ERRORS.append(f"{path}: named like a controller but exposes no readable buttons or axes")
                continue
            info = getattr(dev, "info", None)
            devices.append({
                "path": path, "name": dev.name or path, "buttons": button_names,
                "axes": axis_infos,
                "vendor": int(getattr(info, "vendor", 0) or 0),
                "product": int(getattr(info, "product", 0) or 0),
                "bustype": int(getattr(info, "bustype", 0) or 0),
                "uniq": str(getattr(dev, "uniq", "") or ""),
                "phys": str(getattr(dev, "phys", "") or ""),
                "label": f"{dev.name or 'Controller'}  ({len(button_names)} buttons, {len(axis_infos)} axes)  ·  {path}"
            })
        except Exception as exc:
            DISCOVERY_ERRORS.append(f"{path}: {type(exc).__name__}: {exc}")
        finally:
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass
    return devices


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def run_daemon(device_path: str, profile_path: str, identity: dict | None = None) -> int:
    """Run a user-session remapper that keeps the virtual pad alive across disconnects."""
    if not evdev:
        _emit({"type": "error", "message": "PadShift needs python3-evdev. Install python3-evdev."})
        return 2

    try:
        stdin_fd = sys.stdin.fileno()
    except Exception:
        stdin_fd = -1
    stdin_buffer = ""
    stop_requested = False
    input_device = None
    virtual_device = None
    input_name = ""
    target_identity = controller_identity(identity or {"path": device_path})
    current_path = str(device_path or target_identity.get("path", ""))
    held_sources: set[str] = set()
    held_targets: set[int] = set()
    # Holds created by analog axes can coexist with physical button holds. Keys are
    # source axis names or synthetic output-axis tags, values are evdev button codes.
    axis_button_held: dict[str, int] = {}
    profile: dict = {"buttons": {}, "axes": {}, "axis_buttons": {}}
    source_axes: dict[str, dict] = {}
    source_buttons: set[str] = set()
    last_axis_bucket: dict[str, int] = {}
    waiting_message_sent = False

    def profile_is_allowed(path: str) -> bool:
        try:
            resolved = Path(path).expanduser().resolve()
            return resolved.parent == PROFILES_DIR.resolve() and resolved.suffix == ".json"
        except (OSError, RuntimeError):
            return False

    def read_profile(path: str | None = None) -> dict:
        source_path = path or profile_path
        try:
            with open(source_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            raise RuntimeError(f"Could not load profile {source_path}: {exc}") from exc
        # Keep buttons and axes as separate namespaces even for untrusted/corrupt profile JSON.
        cleaned = {"buttons": {}, "axes": {}, "axis_buttons": {}}
        for name, target in raw.get("buttons", {}).items():
            cleaned["buttons"][str(name)] = target if target in BUTTON_TARGETS or target == DISABLED else DISABLED
        for name, config in raw.get("axes", {}).items():
            if not isinstance(config, dict):
                continue
            target = config.get("target", DISABLED)
            target = ALTERNATE_TRIGGER_TARGETS.get(target, target)
            cleaned["axes"][str(name)] = {
                "target": target if target in AXIS_TARGETS or target == DISABLED else DISABLED,
                "invert": bool(config.get("invert", False)),
                "deadzone": _clamp(float(config.get("deadzone", 0.0)), 0.0, 0.95),
                "curve": _clamp(float(config.get("curve", 1.0)), 0.25, 4.0),
            }
        for name, config in raw.get("axis_buttons", {}).items():
            if not isinstance(config, dict):
                continue
            cleaned["axis_buttons"][str(name)] = {
                "negative": config.get("negative", DISABLED) if config.get("negative", DISABLED) in BUTTON_TARGETS or config.get("negative", DISABLED) == DISABLED else DISABLED,
                "positive": config.get("positive", DISABLED) if config.get("positive", DISABLED) in BUTTON_TARGETS or config.get("positive", DISABLED) == DISABLED else DISABLED,
                "threshold": _clamp(float(config.get("threshold", 0.55)), 0.15, 0.95),
                "invert": bool(config.get("invert", False)),
            }
        return cleaned

    def send(etype: int, code: int, value: int, sync: bool = True):
        if virtual_device is None:
            return
        virtual_device.write(etype, code, value)
        if sync:
            virtual_device.syn()

    # ---- Virtual triggers -------------------------------------------------------
    # Games (SDL / XInput / Wine) read LT and RT from the analog axes ABS_Z / ABS_RZ,
    # not from BTN_TL2 / BTN_TR2. A virtual trigger can be driven by:
    #   * analog sources: any physical axis routed to it (the real LT/RT, a stick, ...)
    #   * digital sources: a button, or an analog direction, that the profile maps to
    #     LT/RT (for example LB -> LT). These press the trigger fully.
    # The axis therefore always reports the strongest of its drivers, so a remapped
    # button works even though the physical pad's own analog trigger is also routed
    # to the same output, and releasing one driver can't zero a trigger another is holding.
    trigger_analog: dict[str, dict[str, int]] = {}  # virtual axis -> {source axis: 0..255}
    trigger_last_sent: dict[str, int] = {}
    digital_trigger_axis = {"ABS_Z": "BTN_TL2", "ABS_RZ": "BTN_TR2"}
    # These names are retained as *internal mapping targets* so the GUI can assign a
    # physical button to LT/RT. They are never advertised or emitted as virtual EV_KEYs;
    # refresh_trigger_outputs converts the internal held state into ABS_Z / ABS_RZ.
    twin_trigger_axes = {"BTN_TL2": ("ABS_Z", "ABS_BRAKE"), "BTN_TR2": ("ABS_RZ", "ABS_GAS")}

    def is_redundant_trigger_twin(source_button: str) -> bool:
        for axis_name in twin_trigger_axes.get(source_button, ()):
            if axis_name in source_axes:
                route = profile.get("axes", {}).get(axis_name, {}).get("target", DISABLED)
                if route in TRIGGER_AXES:
                    return True
        return False

    def digital_press_codes() -> set[int]:
        """Virtual button codes pressed on purpose by a button or analog-direction mapping."""
        codes: set[int] = set()
        for src in held_sources:
            target = profile.get("buttons", {}).get(src, DISABLED)
            code = _code_value(target) if target in BUTTON_TARGETS else None
            if code is not None and not is_redundant_trigger_twin(src):
                codes.add(code)
        for key, code in axis_button_held.items():
            # Only user-selected source mappings drive trigger axes. D-pad output holds
            # are kept separate so they cannot feed back into trigger processing.
            if key.startswith("source:"):
                codes.add(code)
        return codes

    def refresh_trigger_outputs() -> bool:
        """Write every virtual trigger axis; returns True if any value changed."""
        if virtual_device is None:
            return False
        pressed = digital_press_codes()
        changed = False
        for axis_name in sorted(VIRTUAL_TRIGGER_AXES):
            axis_code = _code_value(axis_name)
            if axis_code is None:
                continue
            analog = max(trigger_analog.get(axis_name, {}).values(), default=0)
            digital = 0
            button_name = digital_trigger_axis.get(axis_name)
            if button_name and _code_value(button_name) in pressed:
                digital = 255
            value = max(analog, digital)
            if trigger_last_sent.get(axis_name, 0) != value:
                virtual_device.write(ecodes.EV_ABS, axis_code, value)
                trigger_last_sent[axis_name] = value
                changed = True
            # Deliberately do not emit a matching BTN_TL2 / BTN_TR2 EV_KEY event.
            # The virtual device is analogue-trigger-only for better compatibility.
        return changed

    def sync_key_targets():
        """Reconcile held virtual keys and trigger axes across buttons and analog directions."""
        if virtual_device is None:
            return
        axes_changed = refresh_trigger_outputs()
        button_names = {profile.get("buttons", {}).get(src, DISABLED) for src in held_sources}
        physical_targets = {code for name in button_names if name in BUTTON_TARGETS
                            for code in [_code_value(name)] if code is not None}
        new_targets = physical_targets | set(axis_button_held.values())
        # LT/RT remain selectable as mapping destinations, but the virtual device
        # must expose them as analogue axes only. Never publish BTN_TL2 / BTN_TR2
        # in the virtual EV_KEY stream; some legacy consumers then classify the
        # controller as having digital-only triggers and ignore ABS_Z / ABS_RZ.
        suppressed_trigger_codes = {
            code for name in ("BTN_TL2", "BTN_TR2")
            for code in [_code_value(name)] if code is not None
        }
        new_targets.difference_update(suppressed_trigger_codes)
        for output_code in held_targets - new_targets:
            send(ecodes.EV_KEY, output_code, 0, sync=False)
        for output_code in new_targets - held_targets:
            send(ecodes.EV_KEY, output_code, 1, sync=False)
        if axes_changed or new_targets != held_targets:
            virtual_device.syn()
        held_targets.clear()
        held_targets.update(new_targets)

    def set_axis_button_hold(key: str, target_name: str | None):
        code = _code_value(target_name) if target_name in BUTTON_TARGETS else None
        old = axis_button_held.get(key)
        if code is None:
            axis_button_held.pop(key, None)
        else:
            axis_button_held[key] = code
        if old != code:
            sync_key_targets()

    def neutralize_outputs():
        if virtual_device is None:
            return
        for output_code in list(held_targets):
            try:
                virtual_device.write(ecodes.EV_KEY, output_code, 0)
            except Exception:
                pass
        held_targets.clear()
        held_sources.clear()
        axis_button_held.clear()
        trigger_analog.clear()
        trigger_last_sent.clear()
        for name in STICK_AXES | HAT_AXES | TRIGGER_AXES:
            code = _code_value(name)
            if code is not None:
                try:
                    virtual_device.write(ecodes.EV_ABS, code, 0)
                except Exception:
                    pass
        try:
            virtual_device.syn()
        except Exception:
            pass
        last_axis_bucket.clear()

    def process_button(code: int, value: int):
        name = _code_name(ecodes.EV_KEY, code)
        if name not in source_buttons:
            return
        if value == 0:
            held_sources.discard(name)
        elif value in (1, 2):
            held_sources.add(name)
        else:
            return
        sync_key_targets()
        _emit({"type": "input", "name": BUTTON_LABELS.get(name, name), "down": bool(value)})

    def process_axis(code: int, value: int):
        source_name = _code_name(ecodes.EV_ABS, code)
        info = source_axes.get(source_name)
        if not info:
            return
        info["value"] = int(value)

        # Digital button targets learned from analog axes. This is critical for Xbox
        # D-pads reported as ABS_HAT0X/Y and triggers reported only as ABS_Z/RZ etc.
        axis_button_config = profile.get("axis_buttons", {}).get(source_name, {})
        mapped_button = axis_button_for_value(
            int(value), info["min"], info["max"], source_name, axis_button_config
        )
        set_axis_button_hold(f"source:{source_name}", mapped_button)
        mapped_axis_button_active = mapped_button is not None

        config = profile.get("axes", {}).get(source_name, {})
        target_name = config.get("target", DISABLED)
        if target_name not in AXIS_TARGETS:
            return
        output_code = _code_value(target_name)
        if output_code is None:
            return
        result = transform_axis(value, info["min"], info["max"], source_name, target_name,
                                config.get("invert", False), config.get("deadzone", 0.0),
                                config.get("curve", 1.0))
        if result is None:
            return
        # A learned axis-to-button direction takes over that direction, while the
        # opposite/unmapped direction can continue to pass through as an axis.
        # A trigger only has one direction (squeeze), so once a physical trigger is
        # mapped to a button it stops being an analog trigger altogether. Otherwise
        # the first half of the pull would still leak through as partial LT/RT.
        trigger_became_button = (
            source_name in TRIGGER_AXES
            and axis_button_config.get("positive", DISABLED) in BUTTON_TARGETS
        )
        if mapped_axis_button_active or trigger_became_button:
            result = 0

        if target_name in TRIGGER_AXES:
            # Resolve all analog and digital sources together. This prevents a real
            # trigger axis from cancelling a mapped bumper/button, and vice versa.
            trigger_analog.setdefault(target_name, {})[source_name] = result
            sync_key_targets()
        else:
            send(ecodes.EV_ABS, output_code, result)
            # Emit the equivalent digital controls alongside hats. Some games consume
            # D-pad BUTTON events, while others consume their ABS axes.
            derived_button = digital_button_for_axis_output(target_name, result)
            set_axis_button_hold(f"axis-output:{target_name}", derived_button)

        if target_name in STICK_AXES:
            bucket = int(round(result / 32767.0 * 10))
            detail = f"{int(round(result / 32767.0 * 100)):+d}%"
        elif target_name in HAT_AXES:
            bucket = int(result)
            detail = {-1: "negative", 0: "center", 1: "positive"}.get(result, str(result))
        else:
            bucket = int(round(result / 255.0 * 10))
            detail = f"{int(round(result / 255.0 * 100))}%"
        if last_axis_bucket.get(source_name) != bucket:
            last_axis_bucket[source_name] = bucket
            _emit({"type": "axis", "name": AXIS_LABELS.get(source_name, source_name),
                   "target": AXIS_LABELS.get(target_name, target_name), "value": detail})

    def process_command(line: str):
        nonlocal stop_requested, profile, profile_path, target_identity, current_path
        try:
            obj = json.loads(line)
        except Exception:
            return
        cmd = obj.get("cmd")
        if cmd == "stop":
            stop_requested = True
            return
        if cmd == "set_device":
            new_identity = obj.get("identity")
            if isinstance(new_identity, dict):
                target_identity = controller_identity(new_identity)
                current_path = str(target_identity.get("path", ""))
                if input_device is not None:
                    disconnect("Selected controller changed; reconnecting to the newly selected device.")
                else:
                    _emit({"type": "status", "message": "Controller selection updated. Searching for the selected controller…"})
            return
        if cmd == "set_profile":
            proposed = str(obj.get("path", ""))
            if not profile_is_allowed(proposed):
                _emit({"type": "error", "message": "Rejected profile switch: profile must be a JSON file inside ~/.config/padshift/profiles/."})
                return
            profile_path = proposed
        elif cmd != "reload":
            return
        try:
            profile = read_profile(profile_path)
            neutralize_outputs()
            _emit({"type": "status", "message": f"Profile '{Path(profile_path).stem}' active. Changes are live."})
            if input_device is not None:
                for name, info in source_axes.items():
                    process_axis(info["code"], info["value"])
        except Exception as exc:
            _emit({"type": "error", "message": str(exc)})

    def disconnect(reason: str):
        nonlocal input_device, input_name, source_axes, source_buttons
        if input_device is not None:
            try:
                input_device.ungrab()
            except Exception:
                pass
            try:
                input_device.close()
            except Exception:
                pass
            input_device = None
        source_axes = {}
        source_buttons = set()
        input_name = ""
        neutralize_outputs()
        _emit({"type": "status", "message": reason})

    def matching_candidates() -> list[dict]:
        candidates = discover_controllers()
        matches = [item for item in candidates if device_matches_identity(item, target_identity)]
        # Same controller may expose several event nodes; prefer prior node, then the fullest gamepad node.
        matches.sort(key=lambda item: (
            item.get("path") == current_path,
            bool(target_identity.get("uniq")) and item.get("uniq") == target_identity.get("uniq"),
            len(item.get("buttons", [])) + len(item.get("axes", {})),
        ), reverse=True)
        return matches

    def try_attach() -> bool:
        nonlocal input_device, input_name, source_axes, source_buttons, current_path, waiting_message_sent
        matches = matching_candidates()
        if not matches:
            if not waiting_message_sent:
                label = target_identity.get("name") or "the selected controller"
                _emit({"type": "status", "message": f"Waiting for {label} to connect. Your virtual controller and selected profile remain active."})
                waiting_message_sent = True
            return False
        last_error = None
        for candidate in matches:
            candidate_device = None
            try:
                candidate_device = evdev.InputDevice(candidate["path"])
                os.set_blocking(candidate_device.fd, False)
                candidate_device.grab()
                caps = candidate_device.capabilities(absinfo=True)
                buttons = {
                    _code_name(ecodes.EV_KEY, int(code)) for code in caps.get(ecodes.EV_KEY, [])
                    if isinstance(code, int) and 0x100 <= code <= 0x2FF
                    and _code_name(ecodes.EV_KEY, int(code)).startswith("BTN_")
                }
                axes = {}
                for entry in caps.get(ecodes.EV_ABS, []):
                    if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                        continue
                    code, absinfo = int(entry[0]), entry[1]
                    name = _code_name(ecodes.EV_ABS, code)
                    if name.startswith("ABS_"):
                        axes[name] = {"code": code, "min": int(absinfo.min), "max": int(absinfo.max), "value": int(absinfo.value)}
                input_device = candidate_device
                candidate_device = None
                source_buttons = buttons
                source_axes = axes
                input_name = input_device.name or candidate["name"]
                current_path = candidate["path"]
                held_sources.clear()
                held_targets.clear()
                waiting_message_sent = False
                neutralize_outputs()
                _emit({"type": "status", "message": f"Connected to {input_name} ({current_path}). Profile '{Path(profile_path).stem}' is active automatically."})
                # Re-emit current physical axis positions after attach, if any.
                for name, info in source_axes.items():
                    process_axis(info["code"], info["value"])
                return True
            except Exception as exc:
                last_error = exc
                if candidate_device is not None:
                    try:
                        candidate_device.close()
                    except Exception:
                        pass
        if not waiting_message_sent:
            _emit({"type": "status", "message": f"Found the selected controller but could not grab it: {last_error}. Will retry automatically."})
            waiting_message_sent = True
        return False

    try:
        profile = read_profile(profile_path)
        # Build one stable virtual controller for the lifetime of the helper, even while the
        # physical pad is unplugged. Games therefore don't lose the virtual device on reconnect.
        button_codes = []
        for name in BUTTON_TARGETS:
            # Virtual LT / RT are deliberately axis-only. We still keep BTN_TL2 /
            # BTN_TR2 as internal profile targets so button-to-trigger mappings can
            # drive ABS_Z / ABS_RZ, but do not advertise them as EV_KEY capabilities.
            if name in ("BTN_TL2", "BTN_TR2"):
                continue
            code = _code_value(name)
            if code is not None and code not in button_codes:
                button_codes.append(code)
        output_abs = {}
        # Publish only canonical Xbox axes. In particular, do not expose ABS_BRAKE
        # or ABS_GAS here: SDL can prefer those generic axes over ABS_Z / ABS_RZ,
        # while PadShift sends trigger values to the latter. Source aliases are
        # normalized to canonical trigger destinations during profile loading.
        for name in AXIS_TARGETS:
            code = _code_value(name)
            if code is None:
                continue
            if name in STICK_AXES:
                info = evdev.AbsInfo(0, -32768, 32767, 0, 0, 0)
            elif name in HAT_AXES:
                info = evdev.AbsInfo(0, -1, 1, 0, 0, 0)
            else:
                info = evdev.AbsInfo(0, 0, 255, 0, 0, 0)
            output_abs[code] = info
        virtual_device = evdev.UInput(
            events={ecodes.EV_KEY: button_codes, ecodes.EV_ABS: list(output_abs.items())},
            name="PadShift Virtual Xbox Controller", vendor=0x045E, product=0x028E,
            version=0x0110, bustype=ecodes.BUS_USB, phys="padshift/virtual-gamepad")
        neutralize_outputs()
        _emit({"type": "status", "message": f"Virtual controller ready. Profile '{Path(profile_path).stem}' is selected; waiting for the physical controller."})
        try_attach()
        next_scan = time.monotonic() + 0.65

        while not stop_requested:
            readers = []
            if stdin_fd >= 0:
                readers.append(stdin_fd)
            if input_device is not None:
                readers.append(input_device.fd)
            timeout = 0.25 if input_device is not None else 0.4
            try:
                readable, _, _ = select.select(readers, [], [], timeout) if readers else ([], [], [])
            except (OSError, ValueError) as exc:
                if input_device is not None:
                    disconnect(f"Controller connection changed ({exc}). Waiting to reconnect…")
                readable = []

            if stdin_fd >= 0 and stdin_fd in readable:
                try:
                    chunk = os.read(stdin_fd, 4096).decode("utf-8", "replace")
                except OSError:
                    chunk = ""
                if not chunk:
                    stop_requested = True
                else:
                    stdin_buffer += chunk
                    while "\n" in stdin_buffer:
                        line, stdin_buffer = stdin_buffer.split("\n", 1)
                        process_command(line)

            if input_device is not None and input_device.fd in readable and not stop_requested:
                try:
                    events = input_device.read()
                    for event in events:
                        if event.type == ecodes.EV_KEY:
                            process_button(event.code, event.value)
                        elif event.type == ecodes.EV_ABS:
                            process_axis(event.code, event.value)
                except BlockingIOError:
                    pass
                except OSError as exc:
                    disconnect(f"Controller disconnected ({exc}). Waiting for it to reconnect; the virtual controller stays active.")
                    next_scan = 0.0

            if input_device is None and not stop_requested and time.monotonic() >= next_scan:
                try_attach()
                next_scan = time.monotonic() + 0.8

        _emit({"type": "status", "message": "Stopping PadShift and releasing the physical controller…"})
    except Exception as exc:
        _emit({"type": "error", "message": str(exc)})
        _emit({"type": "debug", "message": traceback.format_exc()})
        return 1
    finally:
        if input_device is not None:
            try:
                input_device.ungrab()
            except Exception:
                pass
            try:
                input_device.close()
            except Exception:
                pass
        if virtual_device:
            try:
                neutralize_outputs()
                virtual_device.close()
            except Exception:
                pass
        _emit({"type": "status", "message": "Stopped. The physical controller is released."})
    return 0


class ScrollFrame(ttk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._resize_inner)
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _update_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_inner(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _wheel(self, event):
        if self.winfo_ismapped():
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class PadShiftApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION} · Controller Remapper")
        self.root.geometry("1160x800")
        self.root.minsize(960, 650)
        self.devices: list[dict] = []
        self.device_by_label: dict[str, dict] = {}
        self.settings = load_settings_file()
        saved_profile = str(self.settings.get("active_profile", "") or "")
        if not saved_profile:
            # Older PadShift versions did not remember the active profile. Prefer the most
            # recently edited non-default profile instead of silently activating Default.
            older_profiles = [p for p in PROFILES_DIR.glob("*.json") if p.stem != "default"]
            if older_profiles:
                try:
                    saved_profile = max(older_profiles, key=lambda item: item.stat().st_mtime).stem
                except OSError:
                    saved_profile = older_profiles[0].stem
            else:
                saved_profile = "default"
        self.profile_name = tk.StringVar(value=saved_profile)
        self.auto_activate_var = tk.BooleanVar(value=bool(self.settings.get("auto_activate", True)))
        self.device_label = tk.StringVar()
        self.status_var = tk.StringVar(value="Connect your Xbox controller, then scan for it.")
        self.last_input_var = tk.StringVar(value="No input yet")
        self.last_axis_var = tk.StringVar(value="No axis movement yet")
        self.worker_output_buffer = ""
        self.button_vars: dict[str, tk.StringVar] = {}
        self.axis_vars: dict[str, dict] = {}
        self.worker = None
        self.log_lines: list[str] = []
        self.current_device: dict | None = None
        self.saved_device_identity: dict = controller_identity(self.settings.get("device_identity", {})) if isinstance(self.settings.get("device_identity"), dict) else {}
        self.profile: dict = {"format": 2, "buttons": {}, "axes": {}, "axis_buttons": {}}
        self.profile_dirty = False
        self.selected_part = None
        self.last_target_part: str | None = None
        self.capture_target_part: str | None = None
        self.capture_cancel_event = None
        self.capture_serial = 0
        self.capture_thread = None
        self.capture_queue = queue.Queue()
        self.capture_feedback_var = tk.StringVar(value="Ready. Click a game-side target, then use your physical controller.")
        self.mapping_listbox = None
        self.active_input: tuple[str, str] | None = None
        self.last_captured_input: tuple[str, str] | None = None
        self.active_input_var = tk.StringVar(value="")
        self.controller_axis_vars: dict[str, dict] = {}
        self.controller_button_vars: dict[str, tk.StringVar] = {}
        self.controller_canvases: dict[str, tk.Canvas] = {}
        self.controller_detail_widgets: list[tk.Widget] = []
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self.refresh_devices()
        self.refresh_profiles()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self.poll_worker)
        self.root.after(350, self._maybe_autostart)
        self.root.after(1800, self._auto_device_watch)

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.configure("Hero.TLabel", font=("TkDefaultFont", 18, "bold"))
            style.configure("Subhero.TLabel", font=("TkDefaultFont", 10))
            style.configure("Section.TLabel", font=("TkDefaultFont", 11, "bold"))
            style.configure("ControllerTitle.TLabel", font=("TkDefaultFont", 10, "bold"))
        except Exception:
            pass
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="PadShift", style="Hero.TLabel").pack(side="left")
        ttk.Label(header, text="Your controller. Your rules.", style="Subhero.TLabel").pack(side="left", padx=(14, 0), pady=(7, 0))
        ttk.Label(outer, text="Click a game-side target on the controller map, then press or move the matching physical control.").pack(anchor="w", pady=(3, 10))

        device_row = ttk.Frame(outer)
        device_row.pack(fill="x", pady=(0, 7))
        ttk.Label(device_row, text="Physical controller:").pack(side="left")
        self.device_combo = ttk.Combobox(device_row, textvariable=self.device_label, state="readonly", width=62)
        self.device_combo.pack(side="left", fill="x", expand=True, padx=8)
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)
        self.refresh_button = ttk.Button(device_row, text="Scan", command=self.refresh_devices)
        self.refresh_button.pack(side="left")

        profile_row = ttk.Frame(outer)
        profile_row.pack(fill="x", pady=(0, 9))
        ttk.Label(profile_row, text="Profile:").pack(side="left")
        self.profile_combo = ttk.Combobox(profile_row, textvariable=self.profile_name, state="readonly", width=24)
        self.profile_combo.pack(side="left", padx=(8, 6))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        ttk.Button(profile_row, text="Save changes", command=self.save_profile).pack(side="left", padx=3)
        ttk.Button(profile_row, text="Save as…", command=self.save_as_profile).pack(side="left", padx=3)
        ttk.Button(profile_row, text="Reset mappings", command=self.reset_mappings).pack(side="left", padx=3)
        ttk.Checkbutton(
            profile_row, text="Keep remapping active automatically", variable=self.auto_activate_var,
            command=self.on_auto_activate_changed
        ).pack(side="right", padx=(10, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.controller_tab = ttk.Frame(self.notebook, padding=8)
        self.buttons_tab = ttk.Frame(self.notebook, padding=10)
        self.axes_tab = ttk.Frame(self.notebook, padding=10)
        self.run_tab = ttk.Frame(self.notebook, padding=10)
        self.compat_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.controller_tab, text="  Visual remapper  ")
        self.notebook.add(self.buttons_tab, text="  Advanced: Buttons  ")
        self.notebook.add(self.axes_tab, text="  Advanced: Axes  ")
        self.notebook.add(self.run_tab, text="  Run & monitor  ")
        self.notebook.add(self.compat_tab, text="  Games & Bottles  ")
        self._build_controller_tab()
        self._build_buttons_tab()
        self._build_axes_tab()
        self._build_run_tab()
        self._build_compat_tab()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, textvariable=self.status_var, wraplength=850).pack(side="left", fill="x", expand=True)
        ttk.Label(footer, text=f"v{APP_VERSION}").pack(side="right")

    def _build_controller_tab(self):
        ttk.Label(
            self.controller_tab,
            text="Click the action you want on the pictured controller, then press or move that control on your real controller.",
            style="Section.TLabel", wraplength=1080
        ).pack(anchor="w", pady=(0, 3))
        ttk.Label(
            self.controller_tab,
            text="Example: click the UP zone on the left stick, then pull your physical right stick DOWN. PadShift learns that axis and reverses it automatically.",
            wraplength=1080
        ).pack(anchor="w", pady=(0, 7))

        map_frame = ttk.LabelFrame(self.controller_tab, text="WHAT THE GAME SHOULD RECEIVE · CLICK A TARGET", padding=5)
        map_frame.pack(fill="x", expand=False)
        self.controller_canvases["virtual"] = tk.Canvas(
            map_frame, width=560, height=275, bg="#171a20", highlightthickness=0, borderwidth=0
        )
        self.controller_canvases["virtual"].pack(anchor="center")
        ttk.Label(map_frame, textvariable=self.capture_feedback_var, wraplength=900, justify="center").pack(fill="x", pady=(2, 1))

        details = ttk.LabelFrame(self.controller_tab, text="Mapping and control feel", padding=7)
        details.pack(fill="both", expand=True, pady=(6, 0))
        details.columnconfigure(0, weight=1, minsize=250)
        details.columnconfigure(1, weight=3)
        details.rowconfigure(0, weight=1)
        self.controller_inputs_frame = ttk.Frame(details)
        self.controller_inputs_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.controller_settings_frame = ttk.Frame(details)
        self.controller_settings_frame.grid(row=0, column=1, sticky="nsew")
        self.controller_settings_scroll = ScrollFrame(self.controller_settings_frame)
        self.controller_settings_scroll.canvas.configure(height=115)
        self.controller_settings_scroll.pack(fill="both", expand=True)
        self.controller_settings_content = self.controller_settings_scroll.inner
        self._draw_controller("virtual")
        self._render_controller_panel()

    def _on_notebook_tab_changed(self, _event=None):
        try:
            if self.notebook.index(self.notebook.select()) == 0:
                self._draw_controller("virtual")
                self._render_controller_panel()
        except Exception:
            pass

    def _part_info(self, part: str) -> dict:
        return {
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
        }.get(part, {"label": (str(part).replace("_", " ").title() if part else "No control selected"), "buttons": [], "axes": []})

    def _part_button_target(self, part: str) -> str | None:
        return {
            "lt": "BTN_TL2", "lb": "BTN_TL", "left_stick_click": "BTN_THUMBL",
            "dpad_up": "BTN_DPAD_UP", "dpad_down": "BTN_DPAD_DOWN",
            "dpad_left": "BTN_DPAD_LEFT", "dpad_right": "BTN_DPAD_RIGHT",
            "view": "BTN_SELECT", "menu": "BTN_START", "guide": "BTN_MODE",
            "right_stick_click": "BTN_THUMBR", "rb": "BTN_TR", "rt": "BTN_TR2",
            "face_y": "BTN_NORTH", "face_x": "BTN_WEST", "face_a": "BTN_SOUTH", "face_b": "BTN_EAST",
        }.get(part)

    def _part_axis_targets(self, part: str) -> list[str]:
        direct = self._visual_axis_spec(part)
        if direct:
            return [direct[0]]
        return {
            "lt": ["ABS_Z"], "left_stick": ["ABS_X", "ABS_Y"],
            "dpad": ["ABS_HAT0X", "ABS_HAT0Y"], "right_stick": ["ABS_RX", "ABS_RY"],
            "rt": ["ABS_RZ"],
        }.get(part, [])

    def _visual_axis_spec(self, part: str) -> tuple[str, int, str] | None:
        """Return (virtual axis, desired sign, type) for a directional target on the drawing."""
        specs = {
            "left_stick_up": ("ABS_Y", -1, "stick"),
            "left_stick_down": ("ABS_Y", 1, "stick"),
            "left_stick_left": ("ABS_X", -1, "stick"),
            "left_stick_right": ("ABS_X", 1, "stick"),
            "right_stick_up": ("ABS_RY", -1, "stick"),
            "right_stick_down": ("ABS_RY", 1, "stick"),
            "right_stick_left": ("ABS_RX", -1, "stick"),
            "right_stick_right": ("ABS_RX", 1, "stick"),
            "dpad_up": ("ABS_HAT0Y", -1, "dpad"),
            "dpad_down": ("ABS_HAT0Y", 1, "dpad"),
            "dpad_left": ("ABS_HAT0X", -1, "dpad"),
            "dpad_right": ("ABS_HAT0X", 1, "dpad"),
            "lt": ("ABS_Z", 1, "trigger"),
            "rt": ("ABS_RZ", 1, "trigger"),
        }
        return specs.get(part)

    def _axis_target_for_part(self, source: str, target_part: str) -> str | None:
        """Legacy helper retained for the advanced list editor and regression tests."""
        targets = self._part_axis_targets(target_part)
        if not targets:
            return None
        horizontal = source in ("ABS_X", "ABS_RX", "ABS_HAT0X")
        vertical = source in ("ABS_Y", "ABS_RY", "ABS_HAT0Y")
        if horizontal and len(targets) > 1:
            return targets[0]
        if vertical and len(targets) > 1:
            return targets[1]
        if source in ("ABS_Z", "ABS_RZ", "ABS_GAS", "ABS_BRAKE"):
            if source in ("ABS_GAS", "ABS_BRAKE") and len(targets) > 1:
                return targets[1]
            return targets[0]
        return targets[0]

    def _draw_controller(self, role: str):
        canvas = self.controller_canvases.get(role)
        if not canvas:
            return
        canvas.delete("all")
        selected = self.capture_target_part or self.last_target_part
        canvas.create_text(80, 12, anchor="w", text="CLICK A TARGET, THEN USE YOUR REAL CONTROLLER", fill="#aeb7c4", font=("TkDefaultFont", 8, "bold"))
        canvas.create_polygon(
            98, 72, 119, 48, 158, 39, 196, 45, 228, 60, 282, 60, 314, 45, 352, 39,
            391, 48, 412, 72, 443, 113, 445, 145, 432, 177, 410, 197, 388, 197,
            365, 180, 346, 151, 323, 139, 285, 135, 225, 135, 187, 139, 164, 151,
            145, 180, 122, 197, 100, 197, 78, 177, 65, 145, 67, 113,
            smooth=True, splinesteps=24, fill="#2b3038", outline="#626c7a", width=2
        )

        def color_for(part: str, ordinary="#48515e"):
            if selected == part:
                return "#e39b32" if self.capture_target_part else "#239b79"
            return ordinary

        def add_item(part: str, kind: str, coords, text=None, ordinary="#48515e", outline="#8490a0", **kwargs):
            fill = color_for(part, ordinary)
            tags = ("part_" + part, "control")
            if kind == "oval":
                canvas.create_oval(*coords, fill=fill, outline=outline, width=2, tags=tags, **kwargs)
            elif kind == "rect":
                canvas.create_rectangle(*coords, fill=fill, outline=outline, width=2, tags=tags, **kwargs)
            elif kind == "line":
                canvas.create_line(*coords, fill=fill, width=2, tags=tags, **kwargs)
            if text:
                canvas.create_text(text[0], text[1], text=text[2], fill=text[3] if len(text) > 3 else "#f5f7fa", font=("TkDefaultFont", text[4] if len(text) > 4 else 9, "bold"), tags=tags)
            canvas.tag_bind("part_" + part, "<Button-1>", lambda _e, p=part, r=role: self._controller_part_clicked(r, p))
            canvas.tag_bind("part_" + part, "<Enter>", lambda _e, c=canvas: c.configure(cursor="hand2"))
            canvas.tag_bind("part_" + part, "<Leave>", lambda _e, c=canvas: c.configure(cursor=""))

        # Triggers and bumpers.
        add_item("lt", "rect", (111, 32, 207, 57), text=(159, 44, "LT", "#f5f7fa", 9), ordinary="#3c4652")
        add_item("lb", "rect", (119, 61, 209, 79), text=(164, 70, "LB", "#f5f7fa", 8), ordinary="#515b68")
        add_item("rt", "rect", (303, 32, 399, 57), text=(351, 44, "RT", "#f5f7fa", 9), ordinary="#3c4652")
        add_item("rb", "rect", (301, 61, 391, 79), text=(346, 70, "RB", "#f5f7fa", 8), ordinary="#515b68")

        # D-pad arrows are separate targets. If the physical D-pad exposes a hat axis
        # instead of four buttons, PadShift can learn that axis too.
        add_item("dpad", "oval", (105, 117, 137, 149), text=(121, 133, "D", "#dce3ec", 8), ordinary="#252a31", outline="#a0a8b5")
        add_item("dpad_up", "rect", (108, 94, 134, 122), text=(121, 108, "▲", "#f5f7fa", 8), ordinary="#454e5b")
        add_item("dpad_down", "rect", (108, 144, 134, 172), text=(121, 158, "▼", "#f5f7fa", 8), ordinary="#454e5b")
        add_item("dpad_left", "rect", (84, 120, 112, 146), text=(98, 133, "◀", "#f5f7fa", 8), ordinary="#454e5b")
        add_item("dpad_right", "rect", (130, 120, 158, 146), text=(144, 133, "▶", "#f5f7fa", 8), ordinary="#454e5b")

        # Stick rings, plus four individually clickable directions around each stick.
        add_item("left_stick", "oval", (169, 91, 219, 141), ordinary="#252a31", outline="#a0a8b5")
        add_item("left_stick", "oval", (177, 99, 211, 133), ordinary="#4c5663", outline="#8f9aa8")
        add_item("left_stick_up", "rect", (185, 89, 203, 104), text=(194, 96, "↑", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("left_stick_down", "rect", (185, 128, 203, 143), text=(194, 136, "↓", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("left_stick_left", "rect", (168, 107, 183, 125), text=(175, 116, "←", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("left_stick_right", "rect", (205, 107, 220, 125), text=(212, 116, "→", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("left_stick_click", "oval", (184, 106, 204, 126), text=(194, 116, "L3", "#f5f7fa", 7), ordinary="#323943", outline="#a0a8b5")

        add_item("right_stick", "oval", (287, 139, 337, 189), ordinary="#252a31", outline="#a0a8b5")
        add_item("right_stick", "oval", (295, 147, 329, 181), ordinary="#4c5663", outline="#8f9aa8")
        add_item("right_stick_up", "rect", (303, 137, 321, 152), text=(312, 144, "↑", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("right_stick_down", "rect", (303, 176, 321, 191), text=(312, 184, "↓", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("right_stick_left", "rect", (286, 155, 301, 173), text=(293, 164, "←", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("right_stick_right", "rect", (323, 155, 338, 173), text=(330, 164, "→", "#f5f7fa", 8), ordinary="#4b657a", outline="#a0a8b5")
        add_item("right_stick_click", "oval", (302, 154, 322, 174), text=(312, 164, "R3", "#f5f7fa", 7), ordinary="#323943", outline="#a0a8b5")

        add_item("view", "oval", (223, 91, 244, 107), text=(233, 99, "▤", "#f5f7fa", 8), ordinary="#4b5562")
        add_item("menu", "oval", (259, 91, 280, 107), text=(269, 99, "☰", "#f5f7fa", 8), ordinary="#4b5562")
        add_item("guide", "oval", (239, 65, 263, 87), text=(251, 76, "◎", "#f5f7fa", 8), ordinary="#4b5562")

        face = [
            ("face_y", (355, 81, 379, 105), (367, 93, "Y", "#f4d34e", 10), "#514f3f"),
            ("face_x", (332, 104, 356, 128), (344, 116, "X", "#62a9f6", 10), "#394e61"),
            ("face_b", (378, 104, 402, 128), (390, 116, "B", "#ef7777", 10), "#603f45"),
            ("face_a", (355, 127, 379, 151), (367, 139, "A", "#65d88b", 10), "#355444"),
        ]
        for part, coords, txt, ordinary in face:
            add_item(part, "oval", coords, text=txt, ordinary=ordinary)

        canvas.create_text(171, 218, text="LEFT STICK", fill="#adb6c3", font=("TkDefaultFont", 8, "bold"))
        canvas.create_text(315, 218, text="RIGHT STICK", fill="#adb6c3", font=("TkDefaultFont", 8, "bold"))
        canvas.create_text(400, 165, text="ABXY", fill="#adb6c3", font=("TkDefaultFont", 7, "bold"))
        canvas.create_text(121, 183, text="D-PAD", fill="#adb6c3", font=("TkDefaultFont", 8, "bold"))
        if not self.current_device:
            canvas.create_text(255, 255, text="Connect your controller and press Scan", fill="#e0a35c", font=("TkDefaultFont", 9, "bold"))
        elif self.capture_target_part:
            label = self._part_info(self.capture_target_part)["label"]
            canvas.create_text(255, 255, text=f"Waiting for physical input: {label}", fill="#e6ae5f", font=("TkDefaultFont", 9, "bold"))
        else:
            canvas.create_text(255, 255, text="Click a target above, then press/move the control on your real controller", fill="#aeb7c4", font=("TkDefaultFont", 8))
        canvas.scale("all", 255, 140, 1.12, 1.05)

    def _short_button_name(self, name: str) -> str:
        label = BUTTON_LABELS.get(name, name)
        return label.split(" · ", 1)[0].replace("View / Back", "View").replace("Menu / Start", "Menu")

    def _short_axis_name(self, name: str) -> str:
        label = AXIS_LABELS.get(name, name)
        return label.replace("Left stick · horizontal", "Left X").replace("Left stick · vertical", "Left Y") \
            .replace("Right stick · horizontal", "Right X").replace("Right stick · vertical", "Right Y") \
            .replace("Left trigger · analog", "LT axis").replace("Right trigger · analog", "RT axis") \
            .replace("D-pad · horizontal axis", "D-pad X").replace("D-pad · vertical axis", "D-pad Y")

    def _refresh_mapping_summary(self):
        box = getattr(self, "mapping_listbox", None)
        if not box:
            return
        box.delete(0, "end")
        mappings = []
        for source, target in sorted(self.profile.get("buttons", {}).items()):
            if target != DISABLED and source != target:
                mappings.append(f"Game {self._short_button_name(target)}  ←  {self._short_button_name(source)}")
        for source, config in sorted(self.profile.get("axes", {}).items()):
            target = config.get("target", DISABLED)
            if target != DISABLED and (source != target or config.get("invert", False)):
                suffix = " (inverted)" if config.get("invert", False) else ""
                mappings.append(f"Game {self._short_axis_name(target)} ← {self._short_axis_name(source)}{suffix}")
        for source, config in sorted(self.profile.get("axis_buttons", {}).items()):
            for direction in ("negative", "positive"):
                target = config.get(direction, DISABLED)
                if target != DISABLED:
                    direction_label = "− / left / up" if direction == "negative" else "+ / right / down"
                    if config.get("invert", False) and source in TRIGGER_AXES:
                        direction_label += " (axis reversed)"
                    mappings.append(f"Game {self._short_button_name(target)} ← {self._short_axis_name(source)} {direction_label}")
        if not mappings:
            box.insert("end", "No custom mappings yet.")
            box.insert("end", "Default controls stay unchanged.")
        else:
            for line in mappings[:40]:
                box.insert("end", line)

    def _controller_part_clicked(self, role: str, part: str):
        if role != "virtual":
            return
        if part in ("left_stick", "right_stick", "dpad"):
            self.last_target_part = part
            self.status_var.set("Choose an arrow on the stick or D-pad to map a specific direction. Stick centre maps L3/R3 clicks.")
            self._draw_controller("virtual")
            self._render_controller_panel()
            return
        self._begin_visual_capture(part)

    def _axis_capture_signal(self, source: str, raw: int, info: dict, baseline: int) -> int | None:
        low, high = int(info.get("min", -32768)), int(info.get("max", 32767))
        if high <= low:
            return None
        if source in STICK_AXES or source in HAT_AXES:
            midpoint = (low + high) / 2.0
            span = max(1.0, (high - low) / 2.0)
            value = (float(raw) - midpoint) / span
            threshold = 0.48 if source in HAT_AXES else 0.52
            if abs(value) >= threshold:
                return -1 if value < 0 else 1
            return None
        if source in TRIGGER_AXES:
            span = max(1.0, float(high - low))
            delta = (float(raw) - float(baseline)) / span
            if abs(delta) >= 0.20:
                return -1 if delta < 0 else 1
        return None

    def _begin_visual_capture(self, part: str):
        if not self.current_device:
            message = "Connect your Xbox controller and press Scan first."
            self.status_var.set(message)
            self.capture_feedback_var.set(message)
            return
        if self.worker and self.worker.poll() is None:
            message = "Stop the virtual controller before learning a new mapping, then click the target again."
            self.status_var.set(message)
            self.capture_feedback_var.set(message)
            return
        spec = {"button": self._part_button_target(part), "axis": None, "direction": None, "mode": "button"}
        axis_spec = self._visual_axis_spec(part)
        if axis_spec:
            spec["axis"], spec["direction"], spec["mode"] = axis_spec
        if not spec["button"] and not spec["axis"]:
            message = "Click a button, trigger, D-pad direction, or one of the arrows around a stick."
            self.status_var.set(message)
            self.capture_feedback_var.set(message)
            return
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
        self.capture_serial += 1
        serial = self.capture_serial
        cancel = threading.Event()
        self.capture_cancel_event = cancel
        self.capture_target_part = part
        self.capture_feedback_var.set(f"Listening for {self._part_info(part)['label']}. Press one physical button, or move a stick/trigger clearly in one direction.")
        self.status_var.set(f"Listening for {self._part_info(part)['label']}… now press a physical button or move a stick/trigger.")
        self._draw_controller("virtual")
        self._render_controller_panel()
        device = self.current_device
        device_snapshot = {
            "path": device["path"], "buttons": set(device.get("buttons", [])),
            "axes": {name: dict(info) for name, info in device.get("axes", {}).items()},
        }
        self.capture_thread = threading.Thread(
            target=self._capture_input_thread,
            args=(device_snapshot, part, spec, cancel, serial),
            daemon=True,
            name="PadShift-input-capture",
        )
        self.capture_thread.start()

    def _capture_input_thread(self, device: dict, part: str, spec: dict, cancel, serial: int):
        """Read physical inputs on a worker thread; report all UI changes through a queue."""
        result = None
        error = None
        timed_out = False
        opened = False
        last_activity = ""
        input_device = None
        try:
            if not evdev:
                raise RuntimeError("python-evdev is not installed; install it with sudo dnf install python3-evdev")
            input_device = evdev.InputDevice(device["path"])
            # python-evdev InputDevice does not expose set_blocking() in many versions.
            # Configure the underlying file descriptor with Python's portable fd API instead.
            os.set_blocking(input_device.fd, False)
            opened = True
            self.capture_queue.put({"event": "opened", "serial": serial, "device": input_device.name or device["path"]})
            baselines = {}
            for name, info in device["axes"].items():
                baseline = int(info.get("value", 0))
                try:
                    baseline = int(input_device.absinfo(int(info["code"])).value)
                except Exception:
                    pass
                baselines[name] = baseline
            deadline = time.monotonic() + 15.0
            last_activity_sent = 0.0
            last_activity_token = None
            while not cancel.is_set() and time.monotonic() < deadline:
                try:
                    ready, _, _ = select.select([input_device.fd], [], [], 0.12)
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"The selected controller stopped responding: {exc}") from exc
                if not ready:
                    continue
                try:
                    events = input_device.read()
                except BlockingIOError:
                    continue
                # Prefer analog events when one physical control emits both ABS and KEY.
                # This preserves analog trigger travel and supports hat-based D-pads.
                events = sorted(events, key=lambda ev: 0 if ev.type == ecodes.EV_ABS else 1)
                for event in events:
                    if cancel.is_set():
                        break
                    if event.type == ecodes.EV_KEY and int(event.value) in (1, 2):
                        source = _code_name(ecodes.EV_KEY, int(event.code))
                        # The event node can reveal buttons that did not fit our initial capability
                        # filter. Do not silently discard them during learning.
                        is_button = source.startswith("BTN_")
                        if is_button:
                            human = BUTTON_LABELS.get(source, source.replace("BTN_", "").replace("_", " ").title())
                            last_activity = f"Detected physical button: {human}."
                            if spec.get("button") and int(event.value) == 1:
                                result = {"kind": "button", "source": source, "target": spec["button"], "part": part}
                                break
                            now = time.monotonic()
                            token = ("button", source)
                            if token != last_activity_token or now - last_activity_sent > 0.8:
                                self.capture_queue.put({"event": "activity", "serial": serial, "message": last_activity})
                                last_activity_token, last_activity_sent = token, now
                    if event.type != ecodes.EV_ABS:
                        continue
                    # Button destinations must also listen for analog/hat sources.
                    # Many Xbox D-pads are ABS_HAT axes and LT/RT are analog ABS axes.
                    if not spec.get("axis") and not spec.get("button"):
                        continue
                    source = _code_name(ecodes.EV_ABS, int(event.code))
                    info = device["axes"].get(source)
                    if not info:
                        continue
                    raw_value = int(event.value)
                    baseline = baselines.get(source, int(info.get("value", 0)))
                    # Show sub-threshold movement too. Previously the app looked dead until a
                    # stick crossed the capture threshold, even when input events were arriving.
                    if source in STICK_AXES or source in HAT_AXES:
                        progress_value, _centered = normalise_axis(raw_value, int(info.get("min", -32768)), int(info.get("max", 32767)), source)
                        progress_percent = int(round(progress_value * 100))
                    else:
                        span = max(1.0, float(int(info.get("max", 255)) - int(info.get("min", 0))))
                        progress_percent = int(round((raw_value - baseline) / span * 100))
                    if abs(progress_percent) >= 4:
                        now = time.monotonic()
                        progress_token = ("axis-progress", source, int(progress_percent / 10))
                        if progress_token != last_activity_token or now - last_activity_sent > 0.45:
                            self.capture_queue.put({"event": "activity", "serial": serial,
                                "message": f"Seeing movement: {AXIS_LABELS.get(source, source)} {progress_percent:+d}% (move farther to capture)."})
                            last_activity_token, last_activity_sent = progress_token, now
                    source_sign = self._axis_capture_signal(source, raw_value, info, baseline)
                    if source_sign is None:
                        continue
                    dir_label = "negative direction" if source_sign < 0 else "positive direction"
                    if source in ("ABS_Y", "ABS_RY", "ABS_HAT0Y"):
                        dir_label = "up" if source_sign < 0 else "down"
                    elif source in ("ABS_X", "ABS_RX", "ABS_HAT0X"):
                        dir_label = "left" if source_sign < 0 else "right"
                    elif source in TRIGGER_AXES:
                        dir_label = "squeezed" if source_sign > 0 else "released/reversed"
                    last_activity = f"Detected physical movement: {AXIS_LABELS.get(source, source)} · {dir_label}."
                    now = time.monotonic()
                    token = ("axis", source, source_sign)
                    if token != last_activity_token or now - last_activity_sent > 0.8:
                        self.capture_queue.put({"event": "activity", "serial": serial, "message": last_activity})
                        last_activity_token, last_activity_sent = token, now
                    target_axis = spec.get("axis")
                    mode = spec.get("mode", "button")
                    compatible_axis = bool(target_axis)
                    if mode == "stick" and source not in STICK_AXES and source not in HAT_AXES:
                        compatible_axis = False
                    if mode == "dpad" and source not in STICK_AXES and source not in HAT_AXES:
                        compatible_axis = False
                    if mode == "trigger" and source not in TRIGGER_AXES and source not in STICK_AXES and source not in HAT_AXES:
                        compatible_axis = False
                    if compatible_axis:
                        result = {"kind": "axis", "source": source, "target": target_axis,
                                  "source_sign": source_sign, "target_sign": int(spec["direction"]),
                                  "mode": mode, "part": part}
                    elif spec.get("button"):
                        # Axis-to-button learning: record which side of this axis
                        # should activate the clicked game-side button.
                        trigger_invert = source in TRIGGER_AXES and source_sign < 0
                        result = {"kind": "axis_button", "source": source,
                                  "target": spec["button"], "source_sign": source_sign,
                                  "invert": trigger_invert, "part": part}
                    else:
                        continue
                    break
                if result:
                    break
            if not result and not cancel.is_set():
                timed_out = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if input_device:
                try:
                    input_device.close()
                except Exception:
                    pass
            # Tkinter is updated only by the main thread. Calling root.after from this worker
            # can fail on some Tcl builds, which previously swallowed the callback and left the
            # UI looking as if nothing had happened.
            self.capture_queue.put({"event": "complete", "serial": serial, "result": result,
                                    "error": error, "timed_out": timed_out, "opened": opened,
                                    "last_activity": last_activity, "cancelled": cancel.is_set()})

    def _captured_axis_invert(self, source: str, target: str, source_sign: int, target_sign: int) -> bool:
        """Compute the axis flip needed to make the learned physical direction match the clicked target."""
        if target in TRIGGER_AXES:
            # For centred axes this chooses which half activates the trigger; for a
            # reversed trigger axis it also makes rest=0 and pressed=full scale.
            return source_sign < 0
        if target in STICK_AXES | HAT_AXES and source in STICK_AXES | HAT_AXES:
            return source_sign != target_sign
        return False

    def _finish_visual_capture(self, result: dict | None, error: str | None, timed_out: bool,
                               serial: int, opened: bool = False, last_activity: str = "", cancelled: bool = False):
        if serial != self.capture_serial:
            return
        self.capture_cancel_event = None
        self.capture_target_part = None
        if error:
            lower = error.lower()
            if "permission denied" in lower or "eacces" in lower:
                message = f"Cannot read the controller ({error}). Check /dev/input permissions, and close Steam or other controller tools before trying again."
            elif "no such file" in lower or "not found" in lower:
                message = f"The controller disappeared while PadShift was listening ({error}). Reconnect it and press Scan."
            else:
                message = f"Could not listen to the controller: {error}. See the activity log for details."
            self.status_var.set(message)
            self.capture_feedback_var.set("✕ Input learning failed. " + message)
            self._log("ERROR during physical-input learning: " + message)
        elif timed_out:
            if opened:
                message = "No mappable input was received in 15 seconds. Move a stick all the way to an edge or press/release a button. If Steam or another controller tool is open, close it and retry."
                if last_activity:
                    message += " Last detected activity: " + last_activity
            else:
                message = "PadShift could not open the controller to listen. Re-scan and check device permissions."
            self.status_var.set(message)
            self.capture_feedback_var.set("⌛ No mapping made. " + message)
            self._log("Input learning timed out. " + message)
        elif cancelled or not result:
            self.status_var.set("Input learning cancelled. No mapping was changed.")
            self.capture_feedback_var.set("Cancelled. No mapping was changed.")
        elif result["kind"] == "button":
            source, target = result["source"], result["target"]
            if self.current_device and source not in self.current_device["buttons"]:
                self.current_device["buttons"].append(source)
                self.current_device["buttons"].sort()
                self.profile = merge_profile(self.profile, self.current_device["buttons"], self.current_device["axes"])
                self._rebuild_mappings(keep_profile=True)
            self._apply_controller_button(source, target)
            self.active_input = ("button", source)
            self.last_captured_input = self.active_input
            self.last_target_part = result.get("part")
            if source in self.button_vars:
                self.button_vars[source].set(self._button_choice(target))
            self.profile_dirty = True
            saved = self.save_profile(silent=True)
            destination = BUTTON_LABELS.get(target, target)
            input_name = BUTTON_LABELS.get(source, source)
            suffix = "Profile saved automatically." if saved else "Press Save changes to keep it."
            self.status_var.set(f"Mapped {input_name} → game receives {destination}.")
            self.capture_feedback_var.set(f"✓ MAPPED: Physical {input_name}  →  Game receives {destination}. {suffix}")
            self._log(f"Mapped physical {source} to virtual {target}. " + ("Saved." if saved else "Save pending."))
        elif result["kind"] == "axis_button":
            source, target = result["source"], result["target"]
            source_sign = int(result["source_sign"])
            config = self.profile.setdefault("axis_buttons", {}).setdefault(
                source, {"negative": DISABLED, "positive": DISABLED, "threshold": 0.55, "invert": False}
            )
            if source in TRIGGER_AXES:
                direction = "positive"
                config["invert"] = bool(result.get("invert", False))
            else:
                direction = "negative" if source_sign < 0 else "positive"
                config["invert"] = False
            config[direction] = target
            # The clicked target is now driven by the chosen axis direction, not by
            # the controller's old passthrough for that same direction.
            self.profile_dirty = True
            self.active_input = ("axis_button", source)
            self.last_captured_input = self.active_input
            self.last_axis_button_direction = direction
            self.last_target_part = result.get("part")
            saved = self.save_profile(silent=True)
            direction_label = {"negative": "negative / left / up", "positive": "positive / right / down"}[direction]
            input_name = AXIS_LABELS.get(source, source)
            destination = BUTTON_LABELS.get(target, target)
            suffix = " Profile saved automatically." if saved else " Press Save changes to keep it."
            self.status_var.set(f"Mapped {input_name} ({direction_label}) → game receives {destination}.")
            self.capture_feedback_var.set(f"✓ MAPPED: Physical {input_name} ({direction_label}) → Game receives {destination}.{suffix}")
            self._log(f"Mapped axis direction {source}/{direction} to virtual button {target}; invert={config['invert']}. " + ("Saved." if saved else "Save pending."))
        else:
            source, target = result["source"], result["target"]
            source_sign, target_sign = int(result["source_sign"]), int(result["target_sign"])
            if target in STICK_AXES and source not in STICK_AXES and source not in HAT_AXES:
                message = "That input is not a stick axis. Click the target again and move a physical stick or D-pad."
                self.status_var.set(message)
                self.capture_feedback_var.set("✕ " + message)
                self._draw_controller("virtual")
                self._render_controller_panel()
                return
            invert = self._captured_axis_invert(source, target, source_sign, target_sign)
            # One physical axis per virtual axis avoids two controls fighting over the same output.
            for other_source, config in self.profile.setdefault("axes", {}).items():
                if other_source != source and config.get("target") == target:
                    config["target"] = DISABLED
                    self._sync_advanced_axis_vars(other_source)
            config = self.profile.setdefault("axes", {}).setdefault(
                source, {"target": source, "invert": False, "deadzone": 0.08 if source in STICK_AXES else 0.0, "curve": 1.0}
            )
            config["target"] = target
            config["invert"] = bool(invert)
            if source in STICK_AXES and "deadzone" not in config:
                config["deadzone"] = 0.08
            config.setdefault("deadzone", 0.08 if source in STICK_AXES else 0.0)
            config.setdefault("curve", 1.0)
            self.profile_dirty = True
            self.active_input = ("axis", source)
            self.last_captured_input = self.active_input
            self.last_target_part = result.get("part")
            saved = self.save_profile(silent=True)
            self._sync_controller_axis_vars(source)
            label = AXIS_LABELS.get(target, target)
            suffix = " Direction automatically inverted." if invert else ""
            saved_suffix = " Profile saved automatically." if saved else " Press Save changes to keep it."
            self.status_var.set(f"Mapped {AXIS_LABELS.get(source, source)} → game receives {label}.{suffix}")
            self.capture_feedback_var.set(f"✓ MAPPED: Physical {AXIS_LABELS.get(source, source)} → Game receives {label}.{suffix}{saved_suffix}")
            self._log(f"Mapped physical axis {source} to virtual axis {target}; invert={invert}. " + ("Saved." if saved else "Save pending."))
        self._draw_controller("virtual")
        self._render_controller_panel()

    def _cancel_visual_capture(self):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
        self.capture_cancel_event = None
        self.capture_serial += 1
        self.capture_target_part = None
        self._draw_controller("virtual")
        self._render_controller_panel()
        self.status_var.set("Input learning cancelled. No mapping was changed.")
        self.capture_feedback_var.set("Cancelled. No mapping was changed.")

    def _apply_controller_button(self, source: str, target: str):
        self.profile.setdefault("buttons", {})[source] = target if target in BUTTON_TARGETS or target == DISABLED else DISABLED
        self.profile_dirty = True
        if source in self.button_vars:
            self.button_vars[source].set(self._button_choice(self.profile["buttons"][source]))
        self._notify_reload()

    def _apply_controller_axis_target(self, source: str, target: str):
        config = self.profile.setdefault("axes", {}).setdefault(source, {"target": source, "invert": False, "deadzone": 0.08 if source in STICK_AXES else 0.0, "curve": 1.0})
        config["target"] = target if target in AXIS_TARGETS or target == DISABLED else DISABLED
        self.profile_dirty = True
        self._sync_controller_axis_vars(source)
        self._sync_advanced_axis_vars(source)
        self._notify_reload()

    def _sync_controller_axis_vars(self, source: str):
        vars_ = self.controller_axis_vars.get(source)
        config = self.profile.get("axes", {}).get(source, {})
        if vars_ and config:
            vars_["target"].set(self._axis_choice(config.get("target", DISABLED)))
            vars_["invert"].set(bool(config.get("invert", False)))
            vars_["deadzone"].set(float(config.get("deadzone", 0.0)))
            vars_["curve"].set(float(config.get("curve", 1.0)))

    def _sync_advanced_axis_vars(self, source: str):
        widgets = self.axis_vars.get(source)
        config = self.profile.get("axes", {}).get(source, {})
        if widgets and config:
            widgets["target"].set(self._axis_choice(config.get("target", DISABLED)))
            widgets["invert"].set(bool(config.get("invert", False)))
            widgets["deadzone"].set(f"{float(config.get('deadzone', 0.0)):.2f}")
            widgets["curve"].set(f"{float(config.get('curve', 1.0)):.2f}")

    def _controller_axis_changed(self, source: str):
        vars_ = self.controller_axis_vars.get(source)
        if not vars_:
            return
        config = self.profile.setdefault("axes", {}).setdefault(source, {})
        config["target"] = self._parse_choice(vars_["target"].get())
        config["invert"] = bool(vars_["invert"].get())
        try:
            config["deadzone"] = round(_clamp(float(vars_["deadzone"].get()), 0.0, 0.95), 2)
            config["curve"] = round(_clamp(float(vars_["curve"].get()), 0.25, 4.0), 2)
        except (TypeError, ValueError):
            config["deadzone"] = 0.08 if source in STICK_AXES else 0.0
            config["curve"] = 1.0
        self.profile_dirty = True
        self._sync_advanced_axis_vars(source)
        self._draw_controller("virtual")
        self._notify_reload()

    def _current_output_part(self) -> str | None:
        if not self.active_input:
            # For a selected group, show where its first axis is currently routed.
            axes = [n for n in self._part_info(self.selected_part)["axes"] if self.current_device and n in self.current_device.get("axes", {})]
            for source in axes:
                target = self.profile.get("axes", {}).get(source, {}).get("target", source)
                part = self._part_for_axis_target(target)
                if part:
                    return part
            return None
        kind, source = self.active_input
        if kind == "button":
            target = self.profile.get("buttons", {}).get(source, DISABLED)
            for part in ("lt", "lb", "left_stick_click", "dpad_up", "dpad_down", "dpad_left", "dpad_right", "view", "menu", "guide", "right_stick_click", "rb", "rt", "face_y", "face_x", "face_a", "face_b"):
                if self._part_button_target(part) == target:
                    return part
        elif kind == "axis_button":
            config = self.profile.get("axis_buttons", {}).get(source, {})
            direction = getattr(self, "last_axis_button_direction", "positive")
            target = config.get(direction, DISABLED)
            for part in ("lt", "lb", "left_stick_click", "dpad_up", "dpad_down", "dpad_left", "dpad_right", "view", "menu", "guide", "right_stick_click", "rb", "rt", "face_y", "face_x", "face_a", "face_b"):
                if self._part_button_target(part) == target:
                    return part
        else:
            target = self.profile.get("axes", {}).get(source, {}).get("target", source)
            return self._part_for_axis_target(target)
        return None

    def _part_for_axis_target(self, target: str) -> str | None:
        if target in ("ABS_X", "ABS_Y"):
            return "left_stick"
        if target in ("ABS_RX", "ABS_RY"):
            return "right_stick"
        if target in ("ABS_Z", "ABS_BRAKE"):
            return "lt"
        if target in ("ABS_RZ", "ABS_GAS"):
            return "rt"
        if target in ("ABS_HAT0X", "ABS_HAT0Y"):
            return "dpad"
        return None

    def _render_controller_panel(self):
        if not hasattr(self, "controller_inputs_frame"):
            return
        for child in self.controller_inputs_frame.winfo_children():
            child.destroy()
        for child in self.controller_settings_content.winfo_children():
            child.destroy()
        self.controller_axis_vars = {}
        self.controller_button_vars = {}

        ttk.Label(self.controller_inputs_frame, text="HOW TO MAP", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        steps = (
            "1. Click a button, trigger, D-pad arrow, or a stick-direction arrow in the drawing above.",
            "2. Press the physical button, or push the physical stick in the direction you want to use.",
            "3. PadShift learns the input and saves it to your current profile automatically.",
        )
        for step in steps:
            ttk.Label(self.controller_inputs_frame, text=step, wraplength=260, justify="left").pack(anchor="w", pady=(0, 5))

        ttk.Label(self.controller_inputs_frame, text="CURRENT CUSTOM MAPPINGS", style="Section.TLabel").pack(anchor="w", pady=(5, 2))
        self.mapping_listbox = tk.Listbox(self.controller_inputs_frame, height=4, width=37, activestyle="none", exportselection=False)
        self.mapping_listbox.pack(fill="x", expand=False)
        self._refresh_mapping_summary()

        if self.capture_target_part:
            ttk.Label(self.controller_inputs_frame, text=f"LISTENING FOR\n{self._part_info(self.capture_target_part)['label']}", style="Section.TLabel").pack(anchor="w", pady=(4, 3))
            ttk.Button(self.controller_inputs_frame, text="Cancel listening", command=self._cancel_visual_capture).pack(anchor="w", pady=(0, 4))
        elif not self.current_device:
            ttk.Label(self.controller_inputs_frame, text="Connect the controller and press Scan.", wraplength=260).pack(anchor="w", pady=(4, 0))
        elif self.last_captured_input:
            kind, source = self.last_captured_input
            if kind == "button":
                target = self.profile.get("buttons", {}).get(source, DISABLED)
                desc = f"{BUTTON_LABELS.get(source, source)}\n→ {BUTTON_LABELS.get(target, target)}"
            elif kind == "axis_button":
                config = self.profile.get("axis_buttons", {}).get(source, {})
                direction = getattr(self, "last_axis_button_direction", "positive")
                target = config.get(direction, DISABLED)
                direction_label = "negative / left / up" if direction == "negative" else "positive / right / down"
                desc = f"{AXIS_LABELS.get(source, source)} ({direction_label})\n→ {BUTTON_LABELS.get(target, target)}"
                if config.get("invert", False):
                    desc += "\nTrigger axis reversed"
            else:
                config = self.profile.get("axes", {}).get(source, {})
                target = config.get("target", DISABLED)
                desc = f"{AXIS_LABELS.get(source, source)}\n→ {AXIS_LABELS.get(target, target)}"
                if config.get("invert"):
                    desc += "\nDirection inverted"
            ttk.Label(self.controller_inputs_frame, text="LAST MAPPING", style="Section.TLabel").pack(anchor="w", pady=(4, 2))
            ttk.Label(self.controller_inputs_frame, text=desc, wraplength=260).pack(anchor="w")
        else:
            ttk.Label(self.controller_inputs_frame, text="Nothing learned yet. Click a target on the controller above to begin.", wraplength=260).pack(anchor="w", pady=(4, 0))

        ttk.Label(self.controller_settings_content, text="Adjust the selected physical input", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        active = self.active_input
        if active and active[0] == "button":
            source = active[1]
            current_target = self.profile.get("buttons", {}).get(source, DISABLED)
            ttk.Label(self.controller_settings_content, text=f"Physical button: {BUTTON_LABELS.get(source, source)}").pack(anchor="w", pady=(0, 4))
            ttk.Label(self.controller_settings_content, text="Game receives:").pack(anchor="w")
            target_var = tk.StringVar(value=self._button_choice(current_target))
            self.controller_button_vars[source] = target_var
            combo = ttk.Combobox(
                self.controller_settings_content, textvariable=target_var,
                values=[DISABLED] + [f"{BUTTON_LABELS.get(n, n)} · {n}" for n in BUTTON_TARGETS],
                state="readonly", width=34
            )
            combo.pack(anchor="w", fill="x", pady=(2, 5))
            combo.bind("<<ComboboxSelected>>", lambda _e, src=source, var=target_var: self._set_controller_button_from_combo(src, var.get()))
            ttk.Label(self.controller_settings_content, text="You can click another button on the drawing any time to learn a new physical input.", wraplength=680).pack(anchor="w")
        elif active and active[0] == "axis_button":
            source = active[1]
            config = self.profile.get("axis_buttons", {}).get(source, {})
            direction = getattr(self, "last_axis_button_direction", "positive")
            target = config.get(direction, DISABLED)
            direction_label = "negative / left / up" if direction == "negative" else "positive / right / down"
            ttk.Label(self.controller_settings_content, text=f"Physical axis: {AXIS_LABELS.get(source, source)} ({direction_label})").pack(anchor="w", pady=(0, 4))
            ttk.Label(self.controller_settings_content, text=f"Game receives: {BUTTON_LABELS.get(target, target)}").pack(anchor="w", pady=(0, 5))
            ttk.Label(self.controller_settings_content, text="Click a different game-side button and move this direction again to replace the mapping. Axis-to-button mappings activate when the input crosses the configured threshold.", wraplength=680).pack(anchor="w")
        elif active and active[0] == "axis":
            source = active[1]
            config = self.profile.get("axes", {}).get(source, {"target": source, "invert": False, "deadzone": 0.08 if source in STICK_AXES else 0.0, "curve": 1.0})
            ttk.Label(self.controller_settings_content, text=f"Physical axis: {AXIS_LABELS.get(source, source)}").pack(anchor="w", pady=(0, 4))
            ttk.Label(self.controller_settings_content, text="Game receives:").pack(anchor="w")
            target_var = tk.StringVar(value=self._axis_choice(config.get("target", DISABLED)))
            invert_var = tk.BooleanVar(value=bool(config.get("invert", False)))
            deadzone_var = tk.DoubleVar(value=float(config.get("deadzone", 0.08 if source in STICK_AXES else 0.0)))
            curve_var = tk.DoubleVar(value=float(config.get("curve", 1.0)))
            self.controller_axis_vars[source] = {"target": target_var, "invert": invert_var, "deadzone": deadzone_var, "curve": curve_var}
            target_combo = ttk.Combobox(
                self.controller_settings_content, textvariable=target_var,
                values=[DISABLED] + [f"{AXIS_LABELS.get(n, n)} · {n}" for n in AXIS_TARGETS],
                state="readonly", width=35
            )
            target_combo.pack(anchor="w", fill="x", pady=(2, 5))
            target_combo.bind("<<ComboboxSelected>>", lambda _e, src=source: self._controller_axis_changed(src))
            ttk.Checkbutton(self.controller_settings_content, text="Invert direction", variable=invert_var,
                            command=lambda src=source: self._controller_axis_changed(src)).pack(anchor="w", pady=(0, 4))
            row = ttk.Frame(self.controller_settings_content)
            row.pack(anchor="w", fill="x", pady=2)
            ttk.Label(row, text="Deadzone", width=10).pack(side="left")
            tk.Scale(row, from_=0.0, to=0.50, resolution=0.01, orient="horizontal", showvalue=True,
                     variable=deadzone_var, length=165, sliderlength=14, highlightthickness=0, bd=0,
                     command=lambda _v, src=source: self._controller_axis_changed(src)).pack(side="left", padx=(0, 12))
            ttk.Label(row, text="Response", width=9).pack(side="left")
            tk.Scale(row, from_=0.25, to=2.5, resolution=0.05, orient="horizontal", showvalue=True,
                     variable=curve_var, length=165, sliderlength=14, highlightthickness=0, bd=0,
                     command=lambda _v, src=source: self._controller_axis_changed(src)).pack(side="left")
            pair = None
            if source in ("ABS_X", "ABS_Y"):
                pair = ("left_stick", ["ABS_X", "ABS_Y"])
            elif source in ("ABS_RX", "ABS_RY"):
                pair = ("right_stick", ["ABS_RX", "ABS_RY"])
            if pair:
                actions = ttk.Frame(self.controller_settings_content)
                actions.pack(anchor="w", pady=(5, 0))
                ttk.Button(actions, text="Swap this stick's X ↔ Y", command=lambda p=pair[0]: self._swap_stick_axes(p)).pack(side="left")
                ttk.Button(actions, text="Reset this stick", command=lambda p=pair[0]: self._reset_stick_axes(p)).pack(side="left", padx=(6, 0))
        else:
            ttk.Label(
                self.controller_settings_content,
                text="After you learn an input, its tuning options appear here. Axis options include destination, direction inversion, deadzone and response curve.",
                wraplength=680, justify="left"
            ).pack(anchor="w", pady=(0, 5))
            ttk.Label(
                self.controller_settings_content,
                text="Tip: to map a stick direction, click one of the four small arrows around that stick, then move the real stick clearly to one side and hold it briefly.",
                wraplength=680, justify="left"
            ).pack(anchor="w")

    def _set_controller_button_from_combo(self, source: str, choice: str):
        self._apply_controller_button(source, self._parse_choice(choice))
        self._draw_controller("virtual")
        self._notify_reload()

    def _swap_stick_axes(self, part: str):
        pair = ["ABS_X", "ABS_Y"] if part == "left_stick" else ["ABS_RX", "ABS_RY"]
        if not self.current_device or not all(n in self.current_device.get("axes", {}) for n in pair):
            self.status_var.set("Both stick axes must be detected to swap them.")
            return
        first = self.profile["axes"].get(pair[0], {}).get("target", pair[0])
        second = self.profile["axes"].get(pair[1], {}).get("target", pair[1])
        self.profile["axes"][pair[0]]["target"] = second
        self.profile["axes"][pair[1]]["target"] = first
        for source in pair:
            self._sync_controller_axis_vars(source)
            self._sync_advanced_axis_vars(source)
        self.profile_dirty = True
        self._draw_controller("virtual")
        self._notify_reload()
        self.status_var.set("Swapped the stick's horizontal and vertical routes. Press Save to keep it.")

    def _reset_stick_axes(self, part: str):
        pair = ["ABS_X", "ABS_Y"] if part == "left_stick" else ["ABS_RX", "ABS_RY"]
        if not self.current_device:
            return
        for source in pair:
            if source in self.current_device.get("axes", {}):
                self.profile["axes"][source] = {"target": source, "invert": False, "deadzone": 0.08, "curve": 1.0}
                self._sync_controller_axis_vars(source)
                self._sync_advanced_axis_vars(source)
        self.profile_dirty = True
        self._draw_controller("virtual")
        self._notify_reload()
        self.status_var.set("Stick settings reset. Press Save to keep it.")

    def _build_buttons_tab(self):
        ttk.Label(self.buttons_tab, text="Button routing", style="Section.TLabel").pack(anchor="w")
        ttk.Label(self.buttons_tab, text="Each physical button can send any virtual button, or be disabled. Multiple physical buttons may share one target.", wraplength=850).pack(anchor="w", pady=(3, 10))
        header = ttk.Frame(self.buttons_tab)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="PHYSICAL INPUT", width=46).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="VIRTUAL OUTPUT", width=37).grid(row=0, column=1, sticky="w")
        self.button_scroll = ScrollFrame(self.buttons_tab)
        self.button_scroll.pack(fill="both", expand=True)

    def _build_axes_tab(self):
        ttk.Label(self.axes_tab, text="Analogue routing", style="Section.TLabel").pack(anchor="w")
        ttk.Label(self.axes_tab, text="Route sticks, triggers and the D-pad axes to a different virtual axis. Deadzone is a fraction (0.08 = 8%); curve 1.0 is linear. Invert flips direction.", wraplength=850).pack(anchor="w", pady=(3, 10))
        header = ttk.Frame(self.axes_tab)
        header.pack(fill="x", pady=(0, 4))
        for col, (text, width) in enumerate((("PHYSICAL AXIS", 35), ("VIRTUAL AXIS", 30), ("INVERT", 8), ("DEADZONE", 11), ("CURVE", 9))):
            ttk.Label(header, text=text, width=width).grid(row=0, column=col, sticky="w", padx=(0, 3))
        self.axis_scroll = ScrollFrame(self.axes_tab)
        self.axis_scroll.pack(fill="both", expand=True)

    def _build_run_tab(self):
        ttk.Label(self.run_tab, text="Activation & live status", style="Section.TLabel").pack(anchor="w")
        ttk.Label(self.run_tab, text="PadShift normally starts this profile automatically and keeps one virtual gamepad alive across physical disconnects. You can still stop it here; use the checkbox above to control automatic activation.", wraplength=850).pack(anchor="w", pady=(3, 12))
        control = ttk.Frame(self.run_tab)
        control.pack(fill="x")
        self.start_button = ttk.Button(control, text="▶  Start virtual controller", command=self.start_worker)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(control, text="■  Stop remapping", command=self.stop_worker, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(self.run_tab, text="Last physical button event:").pack(anchor="w", pady=(18, 3))
        ttk.Label(self.run_tab, textvariable=self.last_input_var, font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        ttk.Label(self.run_tab, text="Latest stick / trigger movement:").pack(anchor="w", pady=(10, 3))
        ttk.Label(self.run_tab, textvariable=self.last_axis_var, font=("TkDefaultFont", 11)).pack(anchor="w")
        ttk.Label(self.run_tab, text="Activity log").pack(anchor="w", pady=(16, 3))
        log_frame = ttk.Frame(self.run_tab)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled", font=("TkFixedFont", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(self.run_tab, text="Tip: some games need to be restarted after the virtual controller appears. Avoid enabling Steam Input for this controller if it double-processes the mapping.", wraplength=850).pack(anchor="w", pady=(10, 0))

    def _build_compat_tab(self):
        ttk.Label(self.compat_tab, text="Native games, Wine and Bottles", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            self.compat_tab,
            text=("PadShift publishes a normal Linux virtual gamepad. Native Linux games and most Wine/Proton games "
                  "can read it directly; Steam is not required. Keep remapping active while playing. If a game "
                  "doesn't notice a hot-plugged controller, start PadShift first and then launch the game."),
            wraplength=850, justify="left"
        ).pack(anchor="w", pady=(4, 10))

        status = ttk.LabelFrame(self.compat_tab, text="Bottles Flatpak compatibility", padding=10)
        status.pack(fill="x", pady=(0, 10))
        self.bottles_status_var = tk.StringVar(value="Checking for Bottles Flatpak…")
        ttk.Label(status, textvariable=self.bottles_status_var, wraplength=820, justify="left").pack(anchor="w")
        actions = ttk.Frame(status)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Refresh status", command=self.refresh_bottles_compatibility).pack(side="left")
        ttk.Button(actions, text="Enable Flatpak input access…", command=self.enable_bottles_input_access).pack(side="left", padx=(8, 0))

        ttk.Label(self.compat_tab, text="If a Bottles game still cannot see the controller", style="Section.TLabel").pack(anchor="w", pady=(2, 4))
        instructions = (
            "1. In PadShift, select your Xbox controller and press Start virtual controller.\n"
            "2. Leave PadShift running. Launch Bottles and start the game. Enable its controller/gamepad option if it has one.\n"
            "3. If Bottles is the Flatpak build, grant its input-device permission using the button above, then fully quit and reopen Bottles.\n"
            "4. If the game is in a bottle/prefix with ‘Dedicated sandbox’ enabled, turn that option off for that bottle and relaunch the game. The extra sandbox can hide /dev/input even when Flatpak itself allows input devices.\n"
            "5. If the game was already running before PadShift created the virtual pad, close and relaunch the game.\n\n"
            "Permission note: the Flatpak permission grants Bottles access to host input devices exposed under /dev/input. It applies to the Bottles app, not just one game. The button asks first and does not use sudo."
        )
        info_frame = ttk.Frame(self.compat_tab)
        info_frame.pack(fill="both", expand=True)
        self.compat_text = tk.Text(info_frame, height=12, wrap="word", state="normal", font=("TkDefaultFont", 10),
                                   relief="flat", borderwidth=0, padx=3, pady=3)
        compat_scroll = ttk.Scrollbar(info_frame, command=self.compat_text.yview)
        self.compat_text.configure(yscrollcommand=compat_scroll.set)
        self.compat_text.insert("1.0", instructions)
        self.compat_text.configure(state="disabled")
        self.compat_text.pack(side="left", fill="both", expand=True)
        compat_scroll.pack(side="right", fill="y")
        self.refresh_bottles_compatibility()

    def refresh_bottles_compatibility(self):
        flatpak = shutil.which("flatpak")
        if not flatpak:
            self.bottles_status_var.set(
                "The Flatpak command was not found. If Bottles is installed as a native RPM, no Flatpak permission change is needed."
            )
            return
        try:
            result = subprocess.run(
                [flatpak, "info", BOTTLES_FLATPAK_ID], capture_output=True, text=True, timeout=8
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.bottles_status_var.set(f"Could not check Flatpak Bottles: {exc}")
            return
        if result.returncode != 0:
            self.bottles_status_var.set(
                "Bottles Flatpak was not detected. If you use the native RPM/AppImage build, the host virtual controller should be available without a Flatpak permission change."
            )
            return

        self.bottles_status_var.set(
            "Bottles Flatpak is installed. PadShift cannot reliably infer effective access from layered Flatpak permissions. "
            "If the virtual controller is missing inside a game, use ‘Enable Flatpak input access…’, then fully quit and reopen Bottles. "
            "If it is still missing, check the bottle's Dedicated sandbox setting."
        )

    def enable_bottles_input_access(self):
        flatpak = shutil.which("flatpak")
        if not flatpak:
            messagebox.showinfo(
                "Flatpak not found",
                "No flatpak command is available. Native RPM/AppImage Bottles does not need this Flatpak-specific permission."
            )
            return
        try:
            check = subprocess.run(
                [flatpak, "info", BOTTLES_FLATPAK_ID], capture_output=True, text=True, timeout=8
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror("Could not check Bottles Flatpak", str(exc))
            return
        if check.returncode != 0:
            messagebox.showinfo(
                "Bottles Flatpak not found",
                "The Bottles Flatpak (com.usebottles.bottles) was not detected. If you use a native Bottles package, no change is needed."
            )
            return
        accepted = messagebox.askyesno(
            "Allow gamepad input in Bottles?",
            "This will run:\n\n"
            "flatpak override --user --device=input com.usebottles.bottles\n\n"
            "It grants Bottles access to host input devices under /dev/input, which includes game controllers. "
            "The permission applies to Bottles as a whole, not only one bottle. It changes your per-user Flatpak override and can be reviewed later with:\n\n"
            "flatpak override --user --show com.usebottles.bottles\n\n"
            "Apply this permission?"
        )
        if not accepted:
            return
        try:
            result = subprocess.run(
                [flatpak, "override", "--user", "--device=input", BOTTLES_FLATPAK_ID],
                capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror("Could not change Flatpak permission", str(exc))
            return
        if result.returncode == 0:
            messagebox.showinfo(
                "Bottles input permission updated",
                "The Flatpak input-device override was applied. Fully quit and reopen Bottles before testing.\n\n"
                "If the game still cannot see the controller, disable ‘Dedicated sandbox’ for that bottle and relaunch it."
            )
            self._log("Granted Bottles Flatpak the per-user input-device permission.")
            self.refresh_bottles_compatibility()
        else:
            detail = (result.stderr or result.stdout or "Flatpak returned an error.").strip()
            messagebox.showerror("Could not change Flatpak permission", detail)
            self._log("ERROR granting Bottles input access: " + detail)

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-400:]
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_devices(self):
        if not evdev:
            self.status_var.set("Missing dependency: install python3-evdev and python3-tkinter, then relaunch PadShift.")
            messagebox.showerror("PadShift setup needed", "Python package 'evdev' is not installed. On Fedora run:\n\nsudo dnf install python3-evdev python3-tkinter\n\nThen relaunch PadShift.")
            return
        previous = self.device_label.get()
        self.devices = discover_controllers()
        self.device_by_label = {d["label"]: d for d in self.devices}
        labels = list(self.device_by_label)
        self.device_combo["values"] = labels
        if previous in labels:
            self.device_label.set(previous)
            self.on_device_selected()
            return

        saved_identity = self.saved_device_identity or {}
        matches = [d for d in self.devices if device_matches_identity(d, saved_identity)] if saved_identity else []
        if matches:
            chosen = max(matches, key=lambda d: (d.get("path") == saved_identity.get("path"), len(d.get("buttons", [])) + len(d.get("axes", {}))))
            self.device_label.set(chosen["label"])
            self.on_device_selected()
        elif saved_identity:
            # Do not silently switch the saved profile to a different controller during a disconnect.
            self.device_label.set("")
            self.current_device = None
            self._log(f"Saved controller '{saved_identity.get('name', 'controller')}' is not currently connected.")
            self.status_var.set("Waiting for your saved controller. PadShift will reconnect automatically when it returns.")
            if not labels:
                self.capture_feedback_var.set("Saved controller is disconnected. Reconnect it; the selected profile is remembered.")
        elif labels:
            xbox_devices = [d for d in self.devices if "xbox" in d["name"].lower()]
            candidates = xbox_devices or self.devices
            chosen = max(candidates, key=lambda d: len(d.get("buttons", [])) + len(d.get("axes", {})))
            self.device_label.set(chosen["label"])
            self.on_device_selected()
        else:
            self.device_label.set("")
            self.current_device = None
            details = " ".join(DISCOVERY_ERRORS[:3])
            message = "No accessible gamepad found. Connect the controller and PadShift will scan again automatically."
            if details:
                message += " Device scan details: " + details
            self.status_var.set(message)
            self.capture_feedback_var.set("No controller found. Connect it, then press Scan if you want to map new controls.")
        self._log(f"Scan complete: {len(labels)} controller-like device(s) found.")

    def _persist_settings(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        current_identity = controller_identity(self.current_device) if self.current_device else self.saved_device_identity
        payload = {
            "active_profile": self.profile_name.get() or "default",
            "auto_activate": bool(self.auto_activate_var.get()),
            "device_identity": current_identity or {},
        }
        SETTINGS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.settings = payload
        if current_identity:
            self.saved_device_identity = dict(current_identity)

    def _maybe_autostart(self):
        if not self.auto_activate_var.get() or (self.worker and self.worker.poll() is None):
            return
        if self.current_device or self.saved_device_identity:
            self.start_worker(automatic=True)

    def _auto_device_watch(self):
        # If PadShift opened before the controller, keep looking and auto-select it on arrival.
        try:
            if self.auto_activate_var.get() and not (self.worker and self.worker.poll() is None):
                if not self.current_device and not self.saved_device_identity:
                    if discover_controllers():
                        self.refresh_devices()
                        if self.current_device:
                            self._maybe_autostart()
        finally:
            try:
                self.root.after(1800, self._auto_device_watch)
            except tk.TclError:
                pass

    def on_auto_activate_changed(self):
        try:
            self._persist_settings()
        except OSError as exc:
            messagebox.showerror("Could not save PadShift settings", str(exc))
            return
        if self.auto_activate_var.get():
            self._maybe_autostart()
        else:
            self.stop_worker(persist_toggle=False)
            self.status_var.set("Automatic remapping is off. The selected profile remains saved.")

    def on_device_selected(self, _event=None):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
            self.capture_cancel_event = None
            self.capture_serial += 1
            self.capture_target_part = None
        label = self.device_label.get()
        device = self.device_by_label.get(label)
        if not device:
            return
        self.current_device = device
        self.saved_device_identity = controller_identity(device)
        self._rebuild_mappings()
        self.status_var.set(f"Selected {device['name']} ({device['path']}).")
        try:
            self._persist_settings()
        except OSError as exc:
            self._log(f"Could not save device/profile preferences: {exc}")
        if self.worker and self.worker.poll() is None:
            self._send_worker_command({"cmd": "set_device", "identity": self.saved_device_identity})
        elif self.auto_activate_var.get() and hasattr(self, "start_button"):
            self.root.after(100, self._maybe_autostart)

    def refresh_profiles(self):
        names = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        if "default" not in names:
            names.insert(0, "default")
            if not (PROFILES_DIR / "default.json").exists():
                self.profile_name.set("default")
                if self.current_device:
                    self.reset_mappings(save=False)
                    self.save_profile(silent=True)
        self.profile_combo["values"] = names
        if self.profile_name.get() not in names:
            self.profile_name.set(names[0] if names else "default")
        self.load_profile(self.profile_name.get())

    def profile_path(self, name=None) -> Path:
        safe = "".join(ch for ch in (name or self.profile_name.get()) if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_")
        if not safe:
            safe = "default"
        return PROFILES_DIR / f"{safe}.json"

    def load_profile(self, name: str):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
            self.capture_cancel_event = None
            self.capture_serial += 1
            self.capture_target_part = None
        self.active_input = None
        self.last_captured_input = None
        path = self.profile_path(name)
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"format": 1, "buttons": {}, "axes": {}}
        except Exception as exc:
            messagebox.showerror("Could not load profile", f"{path}\n\n{exc}")
            raw = {"format": 1, "buttons": {}, "axes": {}}
        if self.current_device:
            self.profile = merge_profile(raw, self.current_device["buttons"], self.current_device["axes"])
        else:
            self.profile = raw
        self._rebuild_mappings(keep_profile=True)
        self.profile_dirty = False
        self._log(f"Loaded profile '{name}'.")

    def on_profile_selected(self, _event=None):
        self.load_profile(self.profile_name.get())
        try:
            self._persist_settings()
        except OSError as exc:
            self._log(f"Could not save selected profile preference: {exc}")
        if self.worker and self.worker.poll() is None:
            self.save_profile(silent=True)
            self._send_worker_command({"cmd": "set_profile", "path": str(self.profile_path())})
            self._log(f"Switched the active remapper to profile '{self.profile_name.get()}'.")
        elif self.auto_activate_var.get():
            self.root.after(100, self._maybe_autostart)

    def _rebuild_mappings(self, keep_profile=True):
        if not self.current_device:
            return
        device = self.current_device
        raw = self.profile if keep_profile else {"format": 1, "buttons": {}, "axes": {}}
        self.profile = merge_profile(raw, device["buttons"], device["axes"])
        for child in self.button_scroll.inner.winfo_children():
            child.destroy()
        self.button_vars = {}
        targets = [DISABLED] + [f"{BUTTON_LABELS.get(name, name)} · {name}" for name in BUTTON_TARGETS]
        for row, name in enumerate(device["buttons"]):
            ttk.Label(self.button_scroll.inner, text=_friendly_button(name), width=46).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
            value = self.profile["buttons"].get(name, DISABLED)
            var = tk.StringVar(value=self._button_choice(value))
            combo = ttk.Combobox(self.button_scroll.inner, textvariable=var, values=targets, state="readonly", width=45)
            combo.grid(row=row, column=1, sticky="ew", pady=2)
            combo.bind("<<ComboboxSelected>>", lambda _e, source=name, v=var: self._set_button(source, v.get()))
            self.button_vars[name] = var
        self.button_scroll.inner.columnconfigure(1, weight=1)
        for child in self.axis_scroll.inner.winfo_children():
            child.destroy()
        self.axis_vars = {}
        axis_targets = [DISABLED] + [f"{AXIS_LABELS.get(name, name)} · {name}" for name in AXIS_TARGETS]
        for row, name in enumerate(device["axes"]):
            config = self.profile["axes"].get(name, {})
            ttk.Label(self.axis_scroll.inner, text=_friendly_axis(name), width=35).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 4))
            target_var = tk.StringVar(value=self._axis_choice(config.get("target", DISABLED)))
            target_combo = ttk.Combobox(self.axis_scroll.inner, textvariable=target_var, values=axis_targets, state="readonly", width=30)
            target_combo.grid(row=row, column=1, sticky="ew", pady=3, padx=(0, 7))
            target_combo.bind("<<ComboboxSelected>>", lambda _e, source=name: self._set_axis(source))
            invert_var = tk.BooleanVar(value=bool(config.get("invert", False)))
            ttk.Checkbutton(self.axis_scroll.inner, variable=invert_var, command=lambda source=name: self._set_axis(source)).grid(row=row, column=2, sticky="w", pady=3)
            deadzone_var = tk.StringVar(value=f"{float(config.get('deadzone', 0.0)):.2f}")
            deadzone_entry = ttk.Spinbox(self.axis_scroll.inner, from_=0.0, to=0.95, increment=0.01, textvariable=deadzone_var, width=9, command=lambda source=name: self._set_axis(source))
            deadzone_entry.grid(row=row, column=3, sticky="w", pady=3, padx=(0, 7))
            deadzone_entry.bind("<FocusOut>", lambda _e, source=name: self._set_axis(source))
            deadzone_entry.bind("<Return>", lambda _e, source=name: self._set_axis(source))
            curve_var = tk.StringVar(value=f"{float(config.get('curve', 1.0)):.2f}")
            curve_entry = ttk.Spinbox(self.axis_scroll.inner, from_=0.25, to=4.0, increment=0.05, textvariable=curve_var, width=8, command=lambda source=name: self._set_axis(source))
            curve_entry.grid(row=row, column=4, sticky="w", pady=3)
            curve_entry.bind("<FocusOut>", lambda _e, source=name: self._set_axis(source))
            curve_entry.bind("<Return>", lambda _e, source=name: self._set_axis(source))
            self.axis_vars[name] = {"target": target_var, "invert": invert_var, "deadzone": deadzone_var, "curve": curve_var}
        self.axis_scroll.inner.columnconfigure(1, weight=1)
        if hasattr(self, "controller_canvases"):
            self._draw_controller("virtual")
            self._render_controller_panel()

    def _button_choice(self, code_name: str) -> str:
        if code_name not in BUTTON_TARGETS:
            return DISABLED
        return f"{BUTTON_LABELS.get(code_name, code_name)} · {code_name}"

    def _axis_choice(self, code_name: str) -> str:
        if code_name not in AXIS_TARGETS:
            return DISABLED
        return f"{AXIS_LABELS.get(code_name, code_name)} · {code_name}"

    def _parse_choice(self, value: str) -> str:
        if value == DISABLED or value.startswith(DISABLED):
            return DISABLED
        return value.rsplit(" · ", 1)[-1]

    def _set_button(self, source: str, choice: str):
        self.profile["buttons"][source] = self._parse_choice(choice)
        self.profile_dirty = True
        if source in self.controller_button_vars:
            self.controller_button_vars[source].set(self._button_choice(self.profile["buttons"][source]))
        self._draw_controller("virtual")
        self._refresh_mapping_summary()
        self._notify_reload()

    def _set_axis(self, source: str):
        widgets = self.axis_vars.get(source)
        if not widgets:
            return
        target = self._parse_choice(widgets["target"].get())
        try:
            deadzone = float(widgets["deadzone"].get())
            if not math.isfinite(deadzone):
                deadzone = 0.0
        except Exception:
            deadzone = 0.0
        try:
            curve = float(widgets["curve"].get())
            if not math.isfinite(curve):
                curve = 1.0
        except Exception:
            curve = 1.0
        self.profile["axes"][source] = {
            "target": target, "invert": bool(widgets["invert"].get()),
            "deadzone": round(_clamp(deadzone, 0.0, 0.95), 2),
            "curve": round(_clamp(curve, 0.25, 4.0), 2),
        }
        self.profile_dirty = True
        self._sync_controller_axis_vars(source)
        self._draw_controller("virtual")
        self._refresh_mapping_summary()
        self._notify_reload()

    def _notify_reload(self):
        # Edits are saved by Save/Start; a running worker reloads from disk after Save.
        if self.profile_dirty:
            if self.worker and self.worker.poll() is None:
                self.status_var.set("Unsaved changes. Press Save to apply them live.")
            else:
                self.status_var.set("Unsaved changes. Press Save to keep this layout.")

    def save_profile(self, silent=False):
        if self.current_device:
            self.profile = merge_profile(self.profile, self.current_device["buttons"], self.current_device["axes"])
        path = self.profile_path()
        try:
            PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.profile_dirty = False
            if not silent:
                self.status_var.set(f"Saved '{self.profile_name.get()}'.")
                self._log(f"Saved profile to {path}")
            if self.worker and self.worker.poll() is None:
                self._send_worker_command({"cmd": "reload"})
        except Exception as exc:
            messagebox.showerror("Could not save profile", str(exc))
            return False
        self.refresh_profiles_only()
        return True

    def refresh_profiles_only(self):
        names = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        if "default" not in names:
            names.insert(0, "default")
        self.profile_combo["values"] = names
        self.profile_combo.set(self.profile_name.get())

    def save_as_profile(self):
        name = simpledialog.askstring("Save profile as", "New profile name (for example, FPS or Racing):", parent=self.root)
        if not name:
            return
        clean = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_")
        if not clean:
            messagebox.showwarning("Invalid name", "Choose a name with letters or numbers.")
            return
        self.profile_name.set(clean)
        if self.save_profile():
            self.refresh_profiles_only()
            self.profile_combo.set(clean)
            try:
                self._persist_settings()
            except OSError as exc:
                self._log(f"Could not save selected profile preference: {exc}")
            if self.worker and self.worker.poll() is None:
                self._send_worker_command({"cmd": "set_profile", "path": str(self.profile_path())})
                self._log(f"Switched the active remapper to profile '{clean}'.")

    def reset_mappings(self, save=True):
        if not self.current_device:
            return
        self.profile = default_profile(self.current_device["buttons"], self.current_device["axes"])
        self._rebuild_mappings(keep_profile=True)
        self.profile_dirty = True
        self.status_var.set("Mappings reset to identity routing.")
        if save:
            self.save_profile()

    def start_worker(self, automatic=False):
        if not evdev:
            return
        if self.worker and self.worker.poll() is None:
            return
        identity = controller_identity(self.current_device) if self.current_device else dict(self.saved_device_identity or {})
        if not identity or not (identity.get("name") or identity.get("vendor") or identity.get("path")):
            if not automatic:
                messagebox.showwarning("No controller selected", "Connect the Xbox controller once so PadShift can remember which one to follow.")
            return
        if self.profile_dirty or not self.profile_path().exists():
            if not self.save_profile(silent=True):
                return
        try:
            self._persist_settings()
        except OSError as exc:
            self._log(f"Could not save startup settings: {exc}")
        script = str(Path(__file__).resolve())
        command = [sys.executable, script, "--daemon", "--device", str(identity.get("path", "")),
                   "--profile", str(self.profile_path()), "--identity", json.dumps(identity, separators=(",", ":"))]
        try:
            self.worker_output_buffer = ""
            self.worker = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as exc:
            messagebox.showerror("Could not start PadShift", str(exc))
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.refresh_button.configure(state="disabled")
        self.device_combo.configure(state="disabled")
        label = "automatically" if automatic else ""
        self.status_var.set(f"Starting {label} with profile '{self.profile_name.get()}'…".replace("  ", " "))
        self._log(f"Started the user-session remapper with profile '{self.profile_name.get()}'; no authentication prompt is required.")

    def _send_worker_command(self, obj: dict):
        if not self.worker or self.worker.poll() is not None or not self.worker.stdin:
            return
        try:
            self.worker.stdin.write(json.dumps(obj) + "\n")
            self.worker.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def stop_worker(self, persist_toggle=True):
        if persist_toggle and self.auto_activate_var.get():
            self.auto_activate_var.set(False)
            try:
                self._persist_settings()
            except OSError:
                pass
        if not self.worker:
            self.status_var.set("Remapping is stopped.")
            return
        if self.worker.poll() is None:
            self._send_worker_command({"cmd": "stop"})
            self.status_var.set("Stopping remapping and releasing the controller…")
            self._log("Stop requested.")
        else:
            self._finish_worker()

    def _finish_worker(self):
        self.worker = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.refresh_button.configure(state="normal")
        self.device_combo.configure(state="readonly")
        if self.auto_activate_var.get():
            self.status_var.set("Remapping stopped unexpectedly. Check the activity log for the error; automatic retry is paused to avoid a loop.")
        else:
            self.status_var.set("Remapping is stopped. The physical controller is available normally.")

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
                name = item.get("device", "selected controller")
                message = f"Listening to {name}. Device opened successfully. Now press a button or move a stick/trigger."
                self.capture_feedback_var.set(message)
                self.status_var.set("Controller input listener is active. Perform one physical input now.")
                self._log("Input learning opened device: " + name)
            elif event == "activity":
                self.capture_feedback_var.set(item.get("message", "Physical input detected."))
            elif event == "complete":
                self._finish_visual_capture(
                    item.get("result"), item.get("error"), bool(item.get("timed_out")),
                    int(item.get("serial", -1)), opened=bool(item.get("opened")),
                    last_activity=item.get("last_activity", ""), cancelled=bool(item.get("cancelled"))
                )

    def poll_worker(self):
        self._poll_capture_queue()
        process = self.worker
        if process:
            if process.stdout:
                fd = process.stdout.fileno()
                try:
                    while select.select([fd], [], [], 0)[0]:
                        chunk = os.read(fd, 8192).decode("utf-8", "replace")
                        if not chunk:
                            break
                        self.worker_output_buffer += chunk
                        while "\n" in self.worker_output_buffer:
                            line, self.worker_output_buffer = self.worker_output_buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                payload = json.loads(line)
                                kind = payload.get("type")
                                if kind == "status":
                                    self.status_var.set(payload.get("message", ""))
                                    self._log(payload.get("message", ""))
                                elif kind == "input":
                                    label = payload.get("name", "Input")
                                    self.last_input_var.set(f"{label}  ·  {'pressed' if payload.get('down') else 'released'}")
                                elif kind == "axis":
                                    self.last_axis_var.set(f"{payload.get('name', 'Axis')} → {payload.get('target', 'output')}: {payload.get('value', '')}")
                                elif kind == "error":
                                    self.status_var.set("Remapping error. See the activity log.")
                                    self._log("ERROR: " + payload.get("message", "unknown error"))
                                elif kind == "debug":
                                    self._log(payload.get("message", ""))
                            except json.JSONDecodeError:
                                self._log(line)
                except (OSError, ValueError):
                    pass
            if process.poll() is not None:
                returncode = process.returncode
                self._log(f"Remapper helper exited with code {returncode}.")
                self._finish_worker()
        self.root.after(120, self.poll_worker)

    def on_close(self):
        if self.capture_cancel_event:
            self.capture_cancel_event.set()
        if self.worker and self.worker.poll() is None:
            self._send_worker_command({"cmd": "stop"})
            self.root.after(250, self.root.destroy)
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="PadShift Linux gamepad remapper")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--device", help=argparse.SUPPRESS)
    parser.add_argument("--profile", help=argparse.SUPPRESS)
    parser.add_argument("--identity", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.daemon:
        if not args.profile:
            parser.error("--daemon requires --profile")
        try:
            identity = json.loads(args.identity) if args.identity else {}
            if not isinstance(identity, dict):
                identity = {}
        except json.JSONDecodeError:
            identity = {}
        return run_daemon(args.device or "", args.profile, identity)
    root = tk.Tk()
    if not evdev:
        # Show the app's dependency prompt rather than failing at import time.
        app = PadShiftApp(root)
    else:
        app = PadShiftApp(root)
    root.mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
