"""System-tray tests. They run the tray over a private, throwaway D-Bus session.

  * test_tray_protocol  - padshift_tray.TrayIcon against a fake StatusNotifierWatcher.
  * test_app_tray_lifecycle - the real GTK app under Xvfb: close hides it and shows the
    icon, clicking the icon restores it, the tray menu's Quit exits.

Both are skipped (not failed) when the needed tools are not installed.
"""
import os
import shutil
import signal
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def _have_gio():
    try:
        import gi  # noqa: F401
        from gi.repository import Gio, GLib  # noqa: F401
        return True
    except Exception:
        return False


def _have_gtk_app_deps():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        import cairo  # noqa: F401
        import evdev  # noqa: F401
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def _run(cmd, extra_env=None, timeout=60):
    env = dict(os.environ)
    # Never let a test window appear on the developer's real desktop or touch their session:
    # force X11 (inside Xvfb), a software renderer, and no desktop-portal / accessibility buses.
    env.pop("WAYLAND_DISPLAY", None)
    env.update({"GDK_BACKEND": "x11", "GSK_RENDERER": "cairo", "GTK_A11Y": "none",
                "GTK_USE_PORTAL": "0", "GDK_DEBUG": "no-portals", "ADW_DISABLE_PORTAL": "1"})
    env.update(extra_env or {})
    # Own process group so a hang can be cleaned up completely (Xvfb, the private bus, helpers).
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(cmd, -signal.SIGKILL, out, f"{err}\n[timed out after {timeout}s]")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


@unittest.skipUnless(shutil.which("dbus-run-session") and _have_gio(), "needs dbus-run-session and PyGObject")
class TrayProtocolTests(unittest.TestCase):
    def test_tray_protocol(self):
        result = _run(["dbus-run-session", "--", sys.executable, str(TESTS / "tray_harness.py")])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TRAY HARNESS OK", result.stdout)


@unittest.skipUnless(shutil.which("dbus-run-session") and shutil.which("xvfb-run") and _have_gtk_app_deps(),
                     "needs dbus-run-session, xvfb-run, GTK 4, libadwaita, evdev and tkinter")
class TrayAppLifecycleTests(unittest.TestCase):
    def test_app_tray_lifecycle(self):
        result = _run(["dbus-run-session", "--", "xvfb-run", "-a", sys.executable, str(TESTS / "tray_app_harness.py")])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TRAY APP HARNESS OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
