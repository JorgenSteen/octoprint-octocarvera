# Notable Behavior

Known quirks, issues, and workarounds discovered during development.

## Table of Contents

- [OctoPrint Plugin Development Gotchas](#octoprint-plugin-development-gotchas)
- [Carvera Hardware & Firmware Quirks](#carvera-hardware--firmware-quirks)

---

# OctoPrint Plugin Development Gotchas

---

## Settings template requires `custom_bindings: False`

- **What happens**: Plugin settings form shows placeholders/defaults instead of saved values. Changing a value and clicking Save appears to work, but after restarting OctoPrint the settings revert. `ko.dataFor()` on the settings panel returns `undefined`.
- **Why**: OctoPrint unbinds `settingsViewModel` from plugin templates by default, assuming the plugin will bind its own viewmodel. Without `custom_bindings: False` in the template config, Knockout never applies to the settings form — inputs are dead HTML.
- **Fix**: In `get_template_configs()`, the settings entry must include `"custom_bindings": False`:
  ```python
  {"type": "settings", "template": "my_settings.jinja2", "custom_bindings": False}
  ```
- **How to verify**: In browser console: `ko.dataFor(document.querySelector('#settings_plugin_YOURPLUGIN'))` should return the settingsViewModel, not `undefined`.
- **Status**: fixed

## Settings save only persists values that differ from defaults

- **What happens**: After saving settings, only keys whose values differ from `get_settings_defaults()` appear in `config.yaml`. If you change a value back to the default and save, the key is **removed** from config.yaml.
- **Why**: OctoPrint's settings system is diff-based by design. `on_settings_save(data)` receives only the changed keys, and only stores values that differ from defaults.
- **Impact**: This is correct behavior, not a bug. But it means you can't distinguish "user explicitly chose the default" from "user never changed this setting".
- **Status**: by design

## OctoPrint restart is slow on Raspberry Pi 3 (~2 minutes)

- **What happens**: After `POST /api/system/commands/core/restart`, OctoPrint takes approximately 2 minutes to become responsive again on a Pi 3.
- **Why**: Pi 3 has limited CPU/RAM. OctoPrint reloads all plugins, rebuilds asset bundles, and reinitializes everything on startup.
- **Impact**: Don't assume 15-20 seconds is enough. Always note the timestamp when sending the restart command so you know when to expect it back. Pi 4/5 will be faster.
- **Status**: known — hardware limitation

## Bootstrap `.btn` overrides custom button colors in OctoPrint

- **What happens**: Custom-styled `.btn` elements appear as empty/white boxes. Color only partially shows on hover, and fully on press-and-hold.
- **Why**: Bootstrap 2's `.btn` class sets a `background-image` gradient that layers on top of `background-color`. Using `background-color` alone only sets the layer underneath — the gradient still renders on top. The `:hover` and `:active` states have their own overrides too.
- **Workaround**: Use `background` (shorthand) instead of `background-color` — this resets both color and image. Also required:
  1. `!important` on `background`, `border-color`, and `color`
  2. Include `:focus` and `:active` pseudo-selectors alongside the base rule
  3. Separate `:hover` rule with the same pattern
- **Example**: E-Stop button and jog lock button in `octocarvera.css`
- **Status**: fixed — pattern established

## OctoPrint `/api/printer` strips custom temperature keys

- **What happens**: Custom temperature keys injected via `_addTemperatureData(custom=...)` are stored correctly in `_temps.last` but don't appear in the `/api/printer` response.
- **Why**: The API endpoint in `printer.py` applies a `_keep_tools` preprocessor when both `heatedBed` and `heatedChamber` are `False` in the printer profile. This strips all keys that don't start with `"tool"`. CNC machines have neither, so all custom keys get deleted.
- **Workaround**: Set `heatedChamber: True` in the printer profile to switch the preprocessor to `_delete_bed` which keeps custom keys. Or use MQTT publishing instead (current approach — avoids the issue entirely).
- **Status**: known — using MQTT instead

## MQTT helpers require OctoPrint-MQTT plugin installed separately

- **What happens**: `get_helpers("mqtt", "mqtt_publish")` returns `None` if the OctoPrint-MQTT plugin isn't installed. No error — just silently unavailable.
- **Why**: OctoPrint's helper system returns `None` for missing plugins. Your plugin must handle this gracefully.
- **Setup**: Install OctoPrint-MQTT via `pip install --no-build-isolation "https://github.com/OctoPrint/OctoPrint-MQTT/archive/master.zip"` (PyPI name doesn't work). Configure broker in OctoPrint Settings > MQTT.
- **Tip**: Use a retry loop in `on_after_startup()` — the MQTT plugin may not be ready when your plugin starts. Wait up to ~15 seconds with retries.
- **Status**: by design — MQTT is optional

## OctoPrint API protection warning

- **What happens**: Log shows warning about `is_api_protected` default implementation.
- **Why**: OctoPrint 1.11.2+ requires plugins to explicitly declare API protection status. Will become enforced in a future version.
- **Workaround**: None needed yet. Should add `is_api_protected()` method returning `True` to the plugin before OctoPrint enforces it.
- **Status**: open

## OctoPrint communication timeout kills idle connections

- **What happens**: OctoPrint has a built-in communication timeout (`timeoutCommunication=30s`, `maxTimeoutsIdle=2`). After 2 consecutive timeouts (~60s) with no response, it kills the connection.
- **Why**: OctoPrint sends M105 (temperature query) every 5 seconds. If the plugin suppresses this (e.g. for a CNC that has no temperature sensor), OctoPrint gets no response and accumulates timeouts. Cannot be disabled — hardcoded minimum 1s interval.
- **Workaround**: Run your own keepalive thread that sends periodic commands the device will respond to. This keeps OctoPrint's timeout counter reset.
- **Status**: fixed — keepalive thread running

## Auto-connect race condition on OctoPrint restart

- **What happens**: OctoPrint's auto-connect fires before a plugin finishes loading. The plugin misses the `CONNECTED` event, so any post-connect init never runs.
- **Why**: OctoPrint initializes serial connection in parallel with plugin startup. The `Events.CONNECTED` event can fire before `on_after_startup()`.
- **Workaround**: In `on_after_startup()`, check if the printer is already connected (`self._printer.is_operational()`) and run your init code if so. Also handle the normal `Events.CONNECTED` path for when the plugin loads first.
- **Status**: fixed — both paths handled

---

# Carvera Hardware & Firmware Quirks

## G91 relative jog commands accepted but don't move

- **What happens**: `G1 G91 G21 X10.0 F1000.0` returns `ok` but the machine doesn't move. `G0 G90 X0 Y0 Z0` (absolute) works fine. Homing also works.
- **Why**: Unknown. Possibly Carvera's Smoothieware doesn't support G91 inline with G1, or relative mode needs to be set separately before the move command.
- **Workaround**: Use absolute positioning (`G90`) for jog commands. Calculate target position from current MPos + desired offset.
- **Status**: open — needs investigation

## Carvera Air work area limits — errors above ~278x192mm

- **What happens**: Commands with coordinates exceeding the work area return errors. BGS Material Framing with values like 290x190 fails because 290 exceeds X travel.
- **Why**: Carvera Air home position is MPos `-278, -192, -57` (approximately). Work area is ~278mm X, ~192mm Y, ~57mm Z from home to origin. Anything beyond these limits is out of travel.
- **Workaround**: Keep framing/jog coordinates within 278x192mm. A future Carvera-specific UI should enforce these limits.
- **Status**: known limitation of the hardware

## BGS control panel has useful layout but wrong commands for Carvera

- **What happens**: Better Grbl Support has a nice jog/control GUI with buttons for Home, Unlock, jog arrows, etc. But its commands are generic GRBL — e.g. "Home" sends `G0 G90 X0 Y0` (move to origin) instead of `$H` (real homing), and jog uses `G91` relative moves which don't work on the Carvera.
- **Decision needed (Phase 3/4)**: Either rework the commands BGS sends (override its hooks) or build a Carvera-specific control panel from scratch. A custom GUI is probably cleaner since the Carvera has unique features (ATC, calibration button, M496 navigation commands, 5-axis).
- **Status**: future consideration for Phase 3 (Full Control) / Phase 4 (UI & Polish)

## Homing requires physical calibration button on the Carvera

- **What happens**: Homing (`$H`) triggers the machine to seek all axis endstops, but the Carvera's homing sequence also involves the physical calibration button on the machine. The Carvera Controller app coordinates this.
- **Why**: The Carvera uses a probe-based homing/calibration sequence, not just simple endstop switches. The Controller GUI walks the user through the process.
- **Workaround**: Home from the Carvera Controller app. OctoPrint can observe the homing via status broadcasts (state changes to `Home`, position updates visible). BGS's "Home" button sends `G0 G90 X0 Y0` which is a move-to-origin, not real homing.
- **Status**: known limitation — Phase 3 jog controls should not include a home button unless we can replicate the full calibration flow

## DTR signal toggle on USB connect causes Alarm state

- **What happens**: When any host opens the USB serial port, the Carvera enters Alarm state ("Abort during cycle"). Must send `$X` to unlock.
- **Why**: The FTDI 232R chip toggles DTR/RTS on port open, which Carvera's Smoothieware firmware interprets as an abort signal. Common with GRBL controllers using FTDI chips. The Linux kernel asserts DTR/RTS on every CDC-ACM/FTDI open via `CDC_SET_CONTROL_LINE_STATE`; this can't be suppressed from userspace.
- **Resolution**: Plugin auto-sends `$X` immediately after the connect handshake (controlled by the `auto_unlock_on_connect` setting, default true). Cold-boot scenario where OctoPrint opens the port before the plugin loads is also covered — the on_after_startup race branch unlocks too. Manual Unlock button in the sidebar is still available for users who disable the auto behavior.
- **Cause is not gone, just absorbed**: The DTR toggle still alarms the firmware on every open; we just clear the alarm right after instead of leaving it for the user. See README "Raspberry Pi cold-boot notes" for OS-level noise reduction (ModemManager, udev) that helps avoid extra alarm triggers piling on top.
- **Status**: resolved in plugin (v0.3.x)

## Firmware 1.0.5 requires binary framing on ALL transports

- **What happens**: Plain-text GRBL commands over USB serial get zero response on firmware 1.0.5. The Carvera accepts the bytes but sends nothing back. Same over WiFi TCP.
- **Why**: Makera added a binary framing protocol in firmware 1.0.5. All commands must be wrapped in frames: `[86 68] [length] [type] [payload] [CRC-16] [55 AA]`. The GRBL commands inside are unchanged — same `?`, `version`, G-code, etc. — just wrapped.
- **Discovery**: Wireshark capture of official Makera Controller (2026-04-10), then verified by sending binary-framed commands over USB serial from the Pi. Status query and `version` command both worked perfectly.
- **Impact**: OctoCarvera must support both plain-text (community firmware / stock <= 1.0.3) and binary-framed (stock 1.0.5+) protocols. Auto-detection possible: send plain-text `version\n`, if no response within ~1.5s, retry with binary framing.
- **Community firmware**: v2.1.0c and the Community Controller still use plain text. Binary framing is Makera-specific.
- **Full protocol docs**: See `docs/carvera-serial-protocol.md` under "Transport: WiFi TCP (Firmware 1.0.5+ Binary Protocol)"
- **Status**: protocol decoded and verified, plugin implementation pending

## Firmware >1.03 pushes status automatically (no `?` needed)

- **What happens**: Newer Carvera firmware (both community and stock, versions after 1.03) automatically pushes GRBL status lines without needing `?` polling.
- **Why**: Firmware sends unsolicited `<State|...>` lines at its own interval.
- **Impact**: The `?` polling at 300ms is redundant but harmless — the `received_hook` processes any `<State|...>` line regardless of whether it was solicited. Polling is kept for backward compatibility with older firmware.
- **Status**: no action needed

## Spindle ON without a cutting tool causes firmware Halt

- **What happens**: Sending `M3 S10000` (spindle on) when no cutting tool is loaded causes the firmware to throw `ERROR: No tool or probe tool!` and enter Alarm state. The T: field shows a garbage value like `T:-1030473318` when no tool is loaded.
- **Why**: Firmware safety check — spinning a probe or empty spindle would cause damage.
- **Tool number conventions**: T0 = wireless probe, T999990 = 3D touch probe, T1–T6 (or up to T99) = cutting tools in the tool rack. Negative/garbage values = no tool loaded.
- **T: field format**: `T:number,offset,target[,unknown]` — e.g. `T:3,-16.281,-1,0`
- **Fix**: OctoCarvera blocks M3 client-side when tool number is outside T1–T100 range (backend returns 409, frontend grays out the button with tooltip).
- **Status**: fixed
