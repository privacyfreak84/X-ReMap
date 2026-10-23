# PadShift 0.12.0 · GTK4 + libadwaita

PadShift is a destination-first Linux gamepad remapper for Fedora KDE and GNOME. This release refines the GTK interface against the **official GNOME Human Interface Guidelines (HIG)** while retaining the tested evdev/uinput remapping engine.

## Interface polish in 0.12.0

- Closing the main window now hides PadShift without stopping the remapping worker by default. Launch PadShift again from the application menu to restore the same running instance; choose **Quit** in the main menu to stop remapping and exit. The **Keep running when the window closes** preference restores close-to-exit behaviour when switched off. No tray extension is required.
- The controller view uses a bundled transparent, high-detail Xbox One-style product illustration instead of the old hand-drawn schematic. The artwork is installed next to the UI, so it does not depend on network access at runtime.
- The image and click targets share one aspect-preserving transform. Hovering a target shows a pointer and highlights the corresponding control; triggers, bumpers, face buttons, D-pad directions, stick directions and stick clicks have separate hit zones.
- If the art is missing or cannot be decoded, the controller area shows an explicit installation error instead of an empty canvas.
- The About dialog includes the version, release notes, source/support links, MIT license, framework/artwork credits, diagnostic details and a Microsoft trademark disclaimer.
- Navigation uses a 280 ms horizontal slide. Dialogs use libadwaita's native presentation transitions; the earlier extra opacity layer was removed because it could make dialogs render blank on some graphics backends. Custom mappings slide open with `Gtk.Revealer`, controller target rings fade in, and the selected control pulses gently while learning an input.
- The Animations preference offers **Follow system**, **Always on**, and **Off**. Follow system respects GTK's motion preference and checks Plasma's configured animation speed when GTK reports animations off. No custom theme stylesheet is injected.

The controller illustration was created specifically for PadShift; it is not copied from a third-party image. Xbox and Xbox One are trademarks of Microsoft Corporation. PadShift is independent and is not affiliated with or endorsed by Microsoft.

## 0.11.3 UI reliability and motion fixes

- Fixed an invalid `Adw.ExpanderRow.set_activatable_widget()` call. `ExpanderRow` supports `add_suffix()` and `add_row()`, but does not implement the ActionRow-only activatable-widget method; axis mapping pages can now populate without crashing.
- Hardened remapping startup so a successful daemon process remains tracked even if a separate UI refresh fails. This avoids reporting that remapping failed and dropping the process reference after it already started.
- Replaced the shrink-prone `Gtk.Paned` editor split with an adaptive `Gtk.Box` layout and a measured-height canvas, preventing the controller drawing area from collapsing inside the scroller.
- Added cached high-quality artwork scaling, a theme-aware neutral backdrop, a visible error fallback, and animated selection rings using libadwaita animation targets.
- Added a `Gtk.Revealer` transition for custom mappings and a persisted Animations preference with system/on/off choices.
- Removed the additional dialog opacity layer; native libadwaita presentation remains in charge to avoid blank dialogs on graphics backends that do not handle the extra offscreen opacity pass correctly.
- Defined the missing `_schedule_resume_after_capture` callback and isolated the periodic poll loop so remapping resumes after learning and UI exceptions do not stop future polls.

## Design approach

The interface uses standard GTK4 and libadwaita widgets instead of a custom CSS theme: `Adw.ApplicationWindow`, `Adw.HeaderBar`, `Adw.ToolbarView`, `Adw.NavigationSplitView`, `Adw.PreferencesGroup`, `Adw.ActionRow`, `Adw.SwitchRow`, `Adw.ExpanderRow`, `Adw.PreferencesDialog`, and `Adw.AlertDialog`. It follows the active system theme and accent colour; the only custom drawing is the controller illustration, which is the central task-specific visual and is not used to replace ordinary widgets.

- The navigation sidebar collapses through libadwaita's navigation split view. The controller and inspector switch from side-by-side to a vertical stack at narrow widths, without a draggable split that can collapse the artwork.
- Common controls use familiar labelled GTK/Adwaita controls. Profile actions live together in a profile menu; routine edits are saved automatically.
- UI icons use symbolic theme icons. The app has its own scalable full-colour and symbolic app icons installed under the HIG/Freedesktop `hicolor` icon-theme paths.
- The controller illustration is supplementary. Every destination is also available from the keyboard-accessible **Game-side control** dropdown and **Learn input** button.
- Native preferences, about and alert-dialog patterns are used. The app does not install a custom stylesheet or override the system's widget styling.
- See [`DESIGN-HIG.md`](DESIGN-HIG.md) for the design crosswalk and remaining manual validation checklist.

## Install on Fedora

From the repository root (or the root of GitHub's **Download ZIP**), open a terminal and run:

```bash
./install.sh
```

The installer installs `python3-evdev`, `python3-tkinter` (required by the existing backend module), `python3-gobject`, `python3-cairo`, GTK4, and libadwaita; configures active-session `/dev/uinput` access; installs the app and its icons; and creates the desktop entry. It may ask for your password for the one-time system rule and package setup. The remapping GUI itself runs as your regular user, not as root.

Launch **PadShift** from the application menu or run:

```bash
~/.local/bin/padshift
```

Your profiles and preferences remain in:

- `~/.config/padshift/profiles/`
- `~/.config/padshift/settings.json`

Re-running the installer updates the app without deleting those files.

## Use PadShift

1. Connect the Xbox controller and open PadShift. Select the controller if it was not found automatically.
2. Choose or create a profile. **Keep remapping active** is available in Preferences; when enabled, the selected profile starts automatically and reconnects after a physical disconnect. **Keep running when the window closes** lets the interface stay hidden while remapping continues; launch PadShift from the application menu to reopen it, or choose **Quit** to stop the worker.
3. In **Visual remapper**, click the desired game-side control on the controller illustration, then press or move the physical input you want to use. If using a keyboard or screen reader, choose the **Game-side control** from the dropdown and activate **Learn input**.
4. PadShift confirms successful mappings and saves them automatically. The **Profile** menu also includes Save profile, Save profile as, and Reset profile.
5. For fine tuning, open **Sticks & axes** or inspect the last learned input. Axis destination, inversion, deadzone, response curve and axis-to-button thresholds are available where applicable.

If remapping is active, PadShift pauses the remapping daemon briefly to learn an input and resumes it after capture. This is necessary because the daemon grabs the physical event device while remapping is active.

## Pages

- **Visual remapper**: clickable controller illustration, destination-first learning, profile/device selection, mapping inspector and current custom mappings.
- **Button mappings**: exact physical-button to virtual-button edits.
- **Sticks & axes**: axis routing and tuning.
- **Activity & diagnostics**: live button/axis activity, helper status, activity log, and a Copy diagnostics action.
- **Games & Bottles**: Flatpak Bottles status, confirmation-protected input permission and troubleshooting.

## Controller behaviour

PadShift creates a virtual Xbox 360-compatible Linux gamepad. Native Linux games, Steam, Wine/Proton and Bottles can use it when their runtime can access host input devices. The virtual controller is kept alive across physical-controller disconnects; the selected profile is remembered and reapplied after reconnect. Games that enumerate controllers only at launch may need to be restarted after the virtual controller first appears.

LT/RT are exposed as standard analogue `ABS_Z` / `ABS_RZ` axes, without redundant digital-trigger capabilities that can confuse some older SDL/joydev input paths. Button mappings to LT/RT still generate the corresponding analogue trigger values. Generic physical `ABS_BRAKE` / `ABS_GAS` axes can be routed to the standard outputs.

For Flatpak Bottles, open **Games & Bottles** and use **Enable Flatpak input access…** only if the game cannot see the virtual controller. This applies the per-user override:

```bash
flatpak override --user --device=input com.usebottles.bottles
```

Fully quit and reopen Bottles after changing that permission. Bottles' per-bottle **Dedicated sandbox** is separate and may need to be disabled for the affected bottle.

## Troubleshooting

- **GTK won't start:** run `./install.sh` again, then launch `~/.local/bin/padshift` from a terminal to see the dependency error.
- **Permission denied for `/dev/uinput`:** log out of KDE and back in once after initial installation, then relaunch the app.
- **No physical controller found:** connect it and choose **Scan devices**. Close other remappers if input capture fails.
- **Mapping times out:** move a stick clearly to its edge or press and release a button. The capture feedback and Activity & diagnostics page show what PadShift received.
- **A game does not see the virtual controller:** start PadShift before launching the game; fully close and reopen the game; check Steam Input and sandbox permissions if applicable.
- **LT/RT do not work:** PadShift exposes standard analogue trigger axes. Check Activity & diagnostics for physical axis movement and relaunch the game after changing the virtual controller setup.

## Developer checks

Run the input-engine and UI-structure regression suite from the extracted directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile padshift_gtk.py padshift.py
bash -n install.sh uninstall.sh
```

**Validation note:** the 0.12.0 source passes the full automated suite (68 tests) and the syntax checks above. The GTK UI was also started once under a headless X server (Xvfb) with GTK 4 and libadwaita 1.5 on Ubuntu; the window built and the app kept running. That check had no physical controller, no `/dev/uinput`, and no Fedora/KDE/GNOME session, so remapping, input learning and the visual layout were not exercised. The GTK UI still requires a live Fedora/GTK4/libadwaita check; follow the checklist in `DESIGN-HIG.md` before describing the interface as fully accessibility-verified.

## Uninstall

```bash
./uninstall.sh
```

The uninstaller removes the app, app icons and PadShift's `/dev/uinput` setup. Profiles under `~/.config/padshift/` are preserved.
