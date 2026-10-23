#!/usr/bin/env bash
set -euo pipefail
APP_ID="io.github.privacyfreak84.PadShift"
rm -f "$HOME/.local/bin/padshift"   "$HOME/.local/share/applications/padshift.desktop"   "$HOME/.local/share/applications/$APP_ID.desktop"   "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"   "$HOME/.local/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
rm -rf "$HOME/.local/share/padshift"
if command -v sudo >/dev/null 2>&1; then
  sudo rm -f /etc/udev/rules.d/70-padshift-uinput.rules /etc/modules-load.d/padshift.conf
  if command -v udevadm >/dev/null 2>&1; then
    sudo udevadm control --reload-rules || true
  fi
fi
echo "PadShift application and system udev setup removed. Saved profiles in ~/.config/padshift/ were preserved."
