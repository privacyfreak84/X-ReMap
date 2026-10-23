#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v dnf >/dev/null 2>&1; then
  echo "This installer expects Fedora/dnf. Install GTK4, libadwaita, PyGObject and python-evdev with your distribution's package manager." >&2
  exit 1
fi

# PadShift's frontend is GTK4/libadwaita. python3-tkinter is retained for the
# compatibility backend module, which still contains the tested daemon engine.
sudo dnf install -y python3-evdev python3-tkinter python3-gobject python3-cairo gtk4 libadwaita

# /dev/uinput access is granted to the active local desktop session; no broad
# membership in the input group and no root-run Python GUI are used.
RULE_TMP="$(mktemp)"
MODULE_TMP="$(mktemp)"
trap 'rm -f "$RULE_TMP" "$MODULE_TMP"' EXIT
cat > "$RULE_TMP" <<'RULE'
# Give the active local desktop session access to the virtual gamepad device.
SUBSYSTEM=="misc", KERNEL=="uinput", OPTIONS+="static_node=uinput", TAG+="uaccess"
RULE
printf 'uinput\n' > "$MODULE_TMP"
sudo install -D -m 0644 "$RULE_TMP" /etc/udev/rules.d/70-padshift-uinput.rules
sudo install -D -m 0644 "$MODULE_TMP" /etc/modules-load.d/padshift.conf
sudo modprobe uinput
if command -v udevadm >/dev/null 2>&1; then
  sudo udevadm control --reload-rules
  sudo udevadm trigger --action=change --subsystem-match=misc --sysname-match=uinput || true
fi

APP_ID="io.github.privacyfreak84.PadShift"
APP_DIR="$HOME/.local/share/padshift"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor"
mkdir -p "$APP_DIR" "$BIN_DIR" "$APPS_DIR"
install -m 0644 "$HERE/padshift.py" "$APP_DIR/padshift.py"
install -m 0644 "$HERE/padshift_gtk.py" "$APP_DIR/padshift_gtk.py"
install -D -m 0644 "$HERE/assets/xbox-one-controller.png" "$APP_DIR/assets/xbox-one-controller.png"
install -D -m 0644 "$HERE/icons/scalable/apps/$APP_ID.svg" "$ICON_DIR/scalable/apps/$APP_ID.svg"
install -D -m 0644 "$HERE/icons/symbolic/apps/$APP_ID-symbolic.svg" "$ICON_DIR/symbolic/apps/$APP_ID-symbolic.svg"
cat > "$BIN_DIR/padshift" <<'LAUNCH'
#!/usr/bin/env bash
exec /usr/bin/python3 "$HOME/.local/share/padshift/padshift_gtk.py" "$@"
LAUNCH
chmod 0755 "$BIN_DIR/padshift"
DESKTOP_FILE="$APPS_DIR/$APP_ID.desktop"
# Remove the previous generic entry so upgrades do not leave duplicate launchers.
rm -f "$APPS_DIR/padshift.desktop"
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PadShift
GenericName=Gamepad Remapper
Comment=Remap a game controller for Linux games
Exec=$BIN_DIR/padshift
Icon=$APP_ID
Terminal=false
Categories=Game;Utility;
Keywords=Gamepad;Controller;Joystick;Remapping;
StartupNotify=true
StartupWMClass=$APP_ID
DESKTOP
chmod 0644 "$DESKTOP_FILE"
# The hicolor theme is cache-optional, but refresh it when a cache tool is present.
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
  gtk4-update-icon-cache -f "$ICON_DIR" >/dev/null 2>&1 || true
elif command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
fi

cat <<'DONE'

PadShift GTK4/libadwaita installed.
Launch it from the application menu or run: ~/.local/bin/padshift
Your existing profiles under ~/.config/padshift/ are preserved.
The GTK UI automatically pauses remapping while learning a physical control,
then resumes it after capture. The virtual controller stays active while playing.
If /dev/uinput reports Permission denied after install, log out of KDE and back in once.
DONE
