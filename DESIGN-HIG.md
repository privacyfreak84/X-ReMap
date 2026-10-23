# PadShift UI design crosswalk: GNOME Human Interface Guidelines

This document records the UI decisions for PadShift 0.11.3 and distinguishes implementation from testing. The design reference is the official GNOME HIG: <https://developer.gnome.org/hig/>.

## Implemented decisions

| HIG area | PadShift implementation | Verification |
|---|---|---|
| Platform integration | GTK4 + libadwaita widgets and an `Adw.ApplicationWindow`; no custom CSS theme or custom widget styling | Source inspection; syntax check |
| Navigation | `Adw.NavigationSplitView`, `Adw.NavigationPage`, `Adw.ToolbarView`, and `Adw.HeaderBar`. The content header relies on libadwaita's native page title/back-button integration when collapsed rather than drawing its own navigation controls | Source inspection; runtime check still required |
| Adaptive layout | Navigation collapses at 850sp; the controller/inspector split stacks vertically at 1180sp. The default window is 1080×740 and its minimum request is 480×420; the layout must remain usable at 1024×600 | Source inspection; manual size testing still required |
| Familiar controls | `Adw.PreferencesGroup` with action/switch/expander rows; GTK dropdowns and scales; `Adw.PreferencesDialog`; `Adw.AlertDialog` for confirmations | Source inspection; runtime check still required |
| Feedback | Capture state remains near the control being edited; toasts are for success/error feedback, and detailed events go to Activity & diagnostics | Source inspection; feedback timing still needs user testing |
| Iconography | Symbolic theme icons for in-app actions; app-specific scalable full-colour and symbolic SVGs installed under `hicolor` using the application ID as the icon name | SVG XML and desktop-entry checks |
| Appearance | System theme and accent colour are used for normal controls and the selected controller target. No stylesheet is injected. Xbox ABXY colours remain only inside the controller illustration and are labelled by letters as well as colour | Source inspection; light/dark/high-contrast testing still required |
| Keyboard and assistive technology | Standard GTK controls keep their native focus/key handling. The controller illustration is supplementary; the dropdown plus Learn input button provides a non-mouse route to every destination. Hover/click feedback is aligned to the included high-detail controller artwork. Slider labels and icon-only menu label use `Gtk.Accessible.update_property_value` when supported | Source inspection; keyboard, screen-reader and GTK Inspector test still required |
| Text and actions | Concise labels, sentence case, one profile menu for profile operations, no duplicate main menus. Changes auto-save; the Profile menu keeps an explicit Save profile action for users who want it | Source inspection; usability review still required |
| Desktop integration | Desktop entry name is `PadShift`, generic name is `Gamepad Remapper`, app icon follows the app ID, and Categories are `Game;Utility;`. Closing the window can hide the UI while the application and worker remain alive; launcher activation restores the same window, while **Quit** stops remapping | Source inspection; real desktop lifecycle test still required |
| Controller artwork | Bundled transparent PNG rendered by Cairo; asset path is installed next to the UI and missing-asset errors are visible | PNG dimensions/alpha, installer path and hit-zone geometry checked; visual run still required |
| About and release details | Full `Adw.AboutDialog` with description, source/support/issue URLs, license, developers/artists, release notes and diagnostic information | Source/API review; runtime display still required |
| Motion | 280 ms horizontal section slide, animated `Gtk.Revealer` for custom mappings, 180 ms eased controller target ring, pulse while learning, and native libadwaita dialog/popover/toast transitions. Preferences offers Follow system / Always on / Off; GTK and KDE motion settings are considered | Source review; manual test with animations enabled/disabled still required |

## Official HIG references

- Overview and principles: <https://developer.gnome.org/hig/> and <https://developer.gnome.org/hig/principles.html>
- Navigation patterns: <https://developer.gnome.org/hig/guidelines/navigation.html>
- Adaptive layouts: <https://developer.gnome.org/hig/guidelines/adaptive.html>
- Header bars: <https://developer.gnome.org/hig/patterns/containers/header-bars.html>
- Buttons: <https://developer.gnome.org/hig/patterns/controls/buttons.html>
- Accessibility: <https://developer.gnome.org/hig/guidelines/accessibility.html>
- App icon guidance: <https://developer.gnome.org/hig/guidelines/app-icons.html>
- Symbolic UI icons: <https://developer.gnome.org/hig/guidelines/ui-icons.html>
- GTK accessibility APIs: <https://docs.gtk.org/gtk4/section-accessibility.html>
- libadwaita split-view integration: <https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/adaptive-layouts.html>
- Desktop entries and icons: <https://developer.gnome.org/documentation/guidelines/maintainer/integrating.html>

## Required manual verification before claiming full conformance

The automated tests cannot establish HIG compliance by themselves. On Fedora with GTK4 and libadwaita installed:

1. Launch the installed desktop entry and verify the app icon, app name, header-bar title, menu, preferences and about dialog.
2. Resize through 1280×800, 1024×600, and narrow portrait-like widths. Confirm the sidebar collapses into native libadwaita navigation, the back action works, and no controls are clipped or require horizontal scrolling.
3. Test keyboard-only navigation through every page, dropdown, mapping target, Learn input, profile action, dialog and log action. Verify focus remains visible and input capture can be initiated without clicking the illustration.
4. Test the accessible tree and labels with GTK Inspector, then test with Orca. Check menu buttons, slider labels/values, capture feedback and the alternate dropdown workflow.
5. Test the light and dark themes, system accent colours, increased text scaling, high-contrast mode, and reduced animation settings. Verify the controller illustration stays legible and isn't the only means of operating controls.
6. Verify toasts do not obscure controls or overwhelm rapid mapping sessions; test dialogs with Escape, close button and keyboard activation.
7. Run `python3 -m unittest discover -s tests -v` on the installed package, then perform real controller learning and remapping tests.
8. With **Keep running when the window closes** enabled, close the window during active remapping and verify the virtual controller remains available; relaunch PadShift from the application menu and verify the same window returns. Confirm **Quit** stops the remapping worker. Repeat with the preference disabled and confirm close-to-exit behaviour.

## Known limits

- The central controller uses a bundled transparent product illustration drawn through Cairo, with shared artwork-space hit testing and theme-accent focus rings; it is not a collection of native per-button GTK widgets. All destinations have a keyboard-accessible dropdown/action alternative, but the drawing itself has not been independently validated as an interactive accessible control.
- This build environment does not have PyGObject/GTK4/libadwaita importable, so no live desktop screenshot, visual regression test, GTK Inspector audit or Orca audit was possible here. Source/static checks must not be mistaken for those tests.


## 0.11.3 runtime and motion fixes
- Removed `ExpanderRow.set_activatable_widget()`, an unsupported method that caused the axis page refresh to raise and could be misreported as remapping startup failure.
- Separated subprocess creation errors from UI refresh errors so a successfully started remapping process cannot be forgotten by the UI.

- The controller image loader now uses pycairo-supported APIs; loading a valid PNG no longer fails due to calling a nonexistent `ImageSurface.status()` method.
- About uses `Gtk.License.MIT_X11`, matching libadwaita’s documented `Gtk.License` parameter type.
- Section navigation uses a 280 ms `SLIDE_LEFT_RIGHT` stack transition. Dialogs use native libadwaita presentation transitions without a second opacity layer (which produced blank rendering on some backends). Custom mappings use a 250 ms `Gtk.Revealer` slide; controller target rings fade in with `Adw.TimedAnimation`, and the selected control pulses while learning. The persisted Animations preference supports Follow system / Always on / Off; changes apply immediately to in-flight animations.
