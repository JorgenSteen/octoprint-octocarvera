# OctoCarvera Design Document

Holistic design for the OctoCarvera OctoPrint plugin — a USB serial interface for the Carvera Air CNC machine. Architecture, protocol, subsystems, UI.

**Target machine**: Carvera Air (stock and community firmwares)
**Connection**: USB serial (FTDI 232R, 115200 baud, 8N1). Binary framing is also supported for stock firmware 1.0.5+.
**Coexistence**: Must work alongside the Carvera Controller app, which uses WiFi. USB and WiFi are independent links.

> See also: `docs/machine_states.md` for the raw firmware state list and the 5-state activity model that drives UI gating. Firmware wire-format notes for stock 1.0.5 live in `memory/project_firmware_105_issue.md`.

---

## 1. System Architecture

```
┌─────────────────┐     WiFi (TCP)         ┌──────────────────┐
│ Carvera          │◄─────────────────────►│ Carvera Controller│
│ Air              │                        │ (PC app)          │
│ (Smoothieware    │     USB Serial         └──────────────────┘
│  firmware)       │◄─────────────────────►┌──────────────────┐
│                  │     115200 8N1         │ Raspberry Pi      │
└─────────────────┘                         │  └─ OctoPrint     │
                                            │     └─ OctoCarvera│
                                            └──────────────────┘
```

### Standalone plugin

OctoCarvera is a standalone OctoPrint plugin — no Better Grbl Support, no other plugin dependency. It owns:

- Its own printer profile `_carvera_air`, created on startup in `octocarvera/__init__.py` if missing.
- All GRBL/Carvera protocol concerns, in `carvera_protocol.py` (status regex, GRBL state set, G-code translation and suppression tables, OctoPrint serial config).
- The communication strategy — `carvera_comm.py` picks between plain-text and binary framing based on settings and detected firmware, with `build_communication(...)` as the factory.
- Status polling, which bypasses the OctoPrint G-code queue via `_send_realtime()` / `_send_raw_serial()` so keepalive keeps working even while a job streams.

### OctoPrint hooks registered

From `__plugin_load__` in `octocarvera/__init__.py`:

| Hook | Priority | Purpose |
|------|----------|---------|
| `octoprint.comm.protocol.gcode.received` | **1** | `received_hook` parses Carvera status lines (`<State\|...>`) before anything else consumes them. |
| `octoprint.comm.protocol.gcode.sending` | default | `sending_gcode_hook` applies `GCODE_TRANSLATIONS` (e.g. `M105`→`?`) and drops `SUPPRESSED_GCODES` (3D-printer M-codes: M21, M84, M104, M140, M106/107, M109, M190, M110). |
| `octoprint.filemanager.extension_tree` | — | `get_extension_tree` registers CNC file extensions `.nc`, `.tap`, `.cnc`, `.ngc`, `.ncc`. |
| `octoprint.comm.transport.serial.factory` | — | `serial_factory_hook` lets the active communication strategy substitute its own serial-like object (binary mode installs `BinaryFrameSerial`). |

---

## 2. Connection Lifecycle

### 2.1 Connect Sequence

```
1. OctoPrint opens the serial port (FTDI at /dev/ttyUSB0).
   → DTR toggles on open → Carvera enters ALARM state.

2. OctoPrint fires Events.CONNECTED.
   → OctoCarvera._on_printer_connected()

3. The active strategy runs on_connect_init:
   → PlainTextCommunication sends INIT_SEQUENCE (the "\n;\n" buffer-clear)
     and then `version` via _send_raw_text (bypassing the queue so the
     response without a trailing `ok` doesn't block OctoPrint).
   → BinaryCommunication sends a framed init instead.

4. Keepalive thread starts. Every few hundred ms (see ~0.3s status polling
   for a responsive UI) it emits the status query:
   → Plain text: RT_STATUS_QUERY (`?`) as a realtime byte via _send_raw_serial.
   → Binary: the same intent, wrapped in the binary framing.
   This keeps OctoPrint's connection alive and drives the UI's position /
   feed / spindle / tool indicators.

5. First status response arrives:
   → received_hook parses `<Alarm|MPos:...|...>`.
   → Sidebar shows Alarm state with position data.

6. User clears alarm:
   → Carvera Controller GUI, terminal `$X`, or the Unlock button
     (which calls self._comm_mode.unlock()).
   → State transitions to Idle.
```

### 2.2 Disconnect Sequence

```
1. OctoPrint fires Events.DISCONNECTED.
   → OctoCarvera._on_printer_disconnected()
   → Stop keepalive thread.
   → Reset state to "Unknown".
   → Send "disconnected" plugin message to frontend.
```

### 2.3 Auto-Connect Race Condition

OctoPrint auto-connects before the plugin finishes loading, so `CONNECTED` fires before `on_after_startup()`. Workaround: the frontend pulls current state from the API via `onStartupComplete`.

---

## 3. Status Monitoring

### 3.1 Status Query Flow

```
Keepalive thread  → ? (or binary equivalent)
                        ↓
Carvera responds  → <State|MPos:...|WPos:...|F:...|S:...|T:...|W:...|L:...|H:...|C:...>
                        ↓
received_hook     → matches CARVERA_STATUS_RE at hook priority 1
                        ↓
_parse_grbl_status → updates internal state, pushes plugin message to frontend
                        ↓
octocarvera.js    → onDataUpdaterPluginMessage updates Knockout observables;
                    _compute_activity() decides which buttons are enabled.
```

### 3.2 Status Response Fields

| Field | Format | Example | Description |
|-------|--------|---------|-------------|
| State | `<State\|...>` | `Idle` | Machine state (see 3.3). |
| MPos | `MPos:X,Y,Z,A,B` | `-278.19,-192.03,-3.00,-86.20,0.00` | Machine position (5 axes). |
| WPos | `WPos:X,Y,Z,A,B` | `0.00,0.00,54.04,0.00,0.00` | Work position (5 axes). |
| F | `F:cur,max,ovr` | `0.0,3000.0,100.0` | Feed: current, max, override %. |
| S | `S:cur,max,ovr,on,t1,t2` | `0.0,10000.0,100.0,0,17.2,19.8` | Spindle: RPM, max, override, on/off, temps. |
| T | `T:num,off,tgt` | `3,-16.281,-1` | Tool: number, offset, target (-1=none). |
| W | `W:wear` | `0.00` | Tool wear compensation. |
| L | `L:mode,p1,p2,pwr,pct` | `0,0,0,0.0,100.0` | Laser: mode, params, power, percentage. |
| H | `H:homed` | `1` | Homed status (1=yes, 0=no). |
| C | `C:c1,c2,c3,c4` | `2,1,0,0` | Configuration flags. |

### 3.3 Machine States

| State | Description | Allowed Actions |
|-------|-------------|-----------------|
| `Idle` | Ready for commands | All commands |
| `Run` | Executing motion / job | Pause, cancel, estop, overrides |
| `Hold` | Feed hold active | Resume, cancel, estop |
| `Pause` | Job paused (Carvera-specific) | Resume, cancel, estop |
| `Wait` | Waiting (tool change) | Limited |
| `Tool` | ATC in progress | Estop only |
| `Alarm` | Alarm active | $X (unlock), $H (home), estop |
| `Home` | Homing in progress | Estop only |
| `Jog` | Jogging | Cancel jog, estop |
| `Door` | Safety door open | Estop |
| `Check` | Check mode | Limited |
| `Sleep` | Sleep mode | Wake |

These raw firmware states come from `GRBL_STATES` in `carvera_protocol.py`. See `docs/machine_states.md` for how they map to the 5 activity states (`idle`, `jogging`, `running_job`, `paused`, `alarm`) that drive UI gating.

### 3.4 Query Before/After Commands

For commands where we need to confirm the result or capture state:

```
Send: ?              → current state/position
Send: <command>      → execute
Wait for: ok/error   → receipt
Send: ?              → updated state/position
```

Used for: jog commands (verify position changed), `$X` (verify Alarm→Idle), override changes (verify override % updated), and any command whose visual feedback depends on the result.

---

## 4. Firmware Detection

### 4.1 Detect at Startup

On connect, the plugin parses the firmware identification string from the startup banner or from an explicit `version` probe:

- Stock firmware responds with `version = X.X.X`.
- Community firmware responds with `Build version: X.X.X`.

The parser sets `_detected_firmware_type` and calls `_auto_set_override_mode()`, which picks the right override commands per firmware (see §5.5).

### 4.2 Version Compatibility

| Firmware | Notes |
|----------|-------|
| Stock ≤ 1.0.4 | Plain-text ping-pong protocol. |
| Stock 1.0.5 | **Binary framing required** for reliable control (`86 68` header / CRC-16 / `55 AA` trailer). Plain text is degraded. See §8. |
| Community 2.0.x | Plain-text ping-pong, but `$X` and `version` responses skip the trailing `ok` — PlainTextCommunication uses `_send_raw_text` for these to avoid blocking OctoPrint's queue. |

**Known stock 1.0.5 quirk**: `G0` does not emit an `ok`. The plain-text mitigation is to list `G0` in `OCTOPRINT_SERIAL_CONFIG.longRunningCommands` in `carvera_protocol.py`; the real fix is to run 1.0.5 in binary mode, where `BinaryFrameSerial` synthesizes acks.

---

## 5. Command Reference

### 5.1 Movement Commands

| Command | Description | Mode | Works? |
|---------|-------------|------|--------|
| `G0 G90 X_ Y_ Z_` | Rapid move (absolute) | Absolute | Yes |
| `G1 G90 X_ Y_ Z_ F_` | Linear move (absolute) | Absolute | Yes |
| `G0 G91 X_ Y_ Z_` | Rapid move (relative) | Relative | **No — accepted but no motion** |
| `G1 G91 X_ Y_ Z_ F_` | Linear move (relative) | Relative | **No — accepted but no motion** |
| `$J=G91 G21 X_ F_` | GRBL jog command | Relative | Untested |

**Jog strategy**: absolute positioning only. Read current MPos from the last status, calculate target, send `G0 G90 X{target} Y{target}`.

### 5.2 Real-Time Commands (work during motion)

| Byte | Command | Description |
|------|---------|-------------|
| `?` (0x3F) | Status query | Returns `<State\|...>` |
| `!` (0x21) | Feed hold | Pause motion |
| `~` (0x7E) | Cycle start | Resume motion |
| `\x18` (Ctrl+X) | Soft reset | Emergency stop |
| `\x19` (Ctrl+Y) | Stop jog | Cancel continuous jog |

### 5.3 Machine Control

| Command | Description | When to use |
|---------|-------------|-------------|
| `$X` | Unlock / clear alarm | After DTR alarm, or any ALARM state |
| `$H` | Home machine | Requires physical calibration button |
| `M3 S{rpm}` | Spindle on | Set RPM with S parameter (gated: requires cutting tool T1–T100) |
| `M5` | Spindle off | |
| `M7` | Air assist on | |
| `M9` | Air assist off | |
| `M6 T{n}` | Tool change | T0=probe, T-1=drop, T1–T20=magazine |

### 5.4 Carvera Navigation (M496)

| Command | Description | Notes |
|---------|-------------|-------|
| `M496` / `M496.1` | Go to clearance position | MPos: -5, -21, -3 |
| `M496.2` | Go to work origin | WPos: 0, 0 (Z stays at clearance) |
| `M496.3` | Go to Anchor 1 | MPos: -288, -202 |
| `M496.4` | Go to Anchor 2 | MPos: -200, -157 |
| `M496.5 X_ Y_` | Go to specific position | WPos: specified X, Y |

All M496 commands keep Z at clearance height — they never plunge.

### 5.5 Override Commands

| Command | Description | Mode |
|---------|-------------|------|
| `M220 S{pct}` | Feed rate override | Stock firmware |
| `M223 S{pct}` | Spindle speed override | Stock firmware |
| `$F S{pct}` | Feed rate override | Community firmware |
| `$O S{pct}` | Spindle speed override | Community firmware |

`_auto_set_override_mode()` picks M220/M223 vs `$F`/`$O` from the detected firmware type.

### 5.6 Probing & Calibration

| Command | Description |
|---------|-------------|
| `M491` | Recalibrate tool, reset tool length offset |
| `M491.1` | Check tool integrity (compare to stored offset) |
| `M495` | Auto-leveling with parameters |
| `G30` | Z-probe calibration (**caution: physical movement**) |
| `G38.2`–`G38.5` | Standard probe commands |

### 5.7 Laser Commands

| Command | Description |
|---------|-------------|
| `M321` | Enter laser mode (drops tool, calibrates spindle) |
| `M322` | Exit laser mode |
| `M323` | Laser test mode on (for focusing) |
| `M324` | Laser test mode off |
| `M325 S{pct}` | Set laser power override |

### 5.8 System Commands

| Command | Description |
|---------|-------------|
| `version` | Get firmware version string |
| `M115` | Get firmware info (translated to `version`) |
| `$$` | Get GRBL settings |
| `$X` | Clear alarm / unlock |
| `$H` | Home all axes |
| `M500` | Save config to SD card |
| `M503` | Display current config values |

### 5.9 G-code Translation / Suppression

`carvera_protocol.py` translates 3D-printer commands OctoPrint emits:

- `M105` → `?` (temperature poll becomes status query)
- `M114` → `?` (position report becomes status query)
- `M115` → `version`
- `M400` → `G4 P0.001` (wait-for-moves becomes tiny dwell)
- `M999` → `$X` (reset-from-error becomes GRBL unlock)

And drops these entirely: `M21, M84, M104, M140, M106, M107, M109, M190, M110`.

---

## 6. Carvera Air Specifications

| Parameter | Value |
|-----------|-------|
| Work area | ~278 × 192 × 57 mm (X, Y, Z) |
| Home MPos | -278.2, -192.0, -57.0, -86.2, 0.0 |
| Axes | 5 (X, Y, Z, A, B) |
| Max feed rate | 3000 mm/min |
| Max spindle | 10000 RPM |
| Connection | FTDI 232R, 115200 baud, 8N1 |
| Firmware | Smoothieware-based; stock 1.0.5 uses binary framing |

---

## 7. Phase Plan

| Phase | Goal | Status |
|-------|------|--------|
| 1 | Connect & Monitor | Done |
| 2 | Job Control API (pause / resume / cancel / overrides) | Done |
| 3 | Standalone Carvera Control (remove BGS, navigation, spindle, restart) | Done |
| 4 | UI & Stability (custom layout, webcam, jog knob, overrides, jog lock) | Done |
| 5 | File Management & Job Monitoring (SD card browser, XMODEM upload) | Done |
| 6 | Polish & Hardening (spindle safety, settings bind fix, MQTT/HA, activity gating, binary protocol) | Done |

### Future Ideas

- External joystick / gamepad control for jogging.
- Start jobs from Carvera's SD card (M32).
- Stream jobs line-by-line as an alternative to SD playback.
- Hide / replace OctoPrint's Control tab and remaining 3D-printer UI.
- Fix the DTR-on-connect alarm; graceful restart without manual intervention.
- Air assist, work light, vacuum (hardware test).
- Tool change (ATC) integration and probing / calibration workflows.
- Jog lock visual: 3× icon, yellow when locked / red when unlocked.
- Rework spindle display ("ON" next to RPM doesn't read well).

---

## 8. Communication Strategy

All out-of-band control goes through `self._comm_mode`, an instance of the `Communication` abstract base class defined in `carvera_comm.py`. `build_communication(...)` picks the subclass from settings and firmware detection; `_rebuild_comm_mode()` runs on startup and on settings save. If `_comm_mode` is still `None` at serial-factory time, it's rebuilt lazily.

### 8.1 Strategy interface

Every strategy implements:

| Method | Purpose |
|--------|---------|
| `on_connect_init(send_init_flag)` | Wake the firmware after the port opens. |
| `estop()` | Emergency stop. |
| `pause()` | Feed hold. |
| `resume()` | Cycle start. |
| `cancel()` | Cancel a running job. |
| `post_cancel_cleanup()` | Run after a cancel settles (usually `$X` + re-init). |
| `unlock()` | Clear alarm. |
| `serial_factory(port, baudrate, timeout)` | Optional: return a serial-like object OctoPrint should use instead of opening the port directly. |
| `name` | Identifier used for logging / UI. |

The plugin's control handlers call these rather than emitting raw G-code, so the two firmwares can diverge freely.

### 8.2 PlainTextCommunication (`name = "plain_text"`)

- Realtime control bytes (`?`, `!`, `~`, `\x18`) work directly.
- `on_connect_init` sends the `\n;\n` buffer-clear followed by `version` — the `version` write goes through `_send_raw_text` to bypass OctoPrint's queue, because community firmware 2.0.2c-RC2 answers `version` without a trailing `ok\n` and would otherwise leave the queue waiting forever.
- `unlock()` sends `$X` via `_send_raw_text` for the same reason: in Idle, community firmware silently no-ops `$X` without an `ok`.
- `post_cancel_cleanup` re-runs `$X` + `INIT_SEQUENCE`.
- Hello command is `$G` (set by `OCTOPRINT_SERIAL_CONFIG.helloCommand`) — also to avoid the missing-ok pitfall during OctoPrint's handshake.

### 8.3 BinaryCommunication (`name = "binary"`)

Required for stock firmware 1.0.5. The wire format is documented in `memory/project_firmware_105_issue.md` — header `86 68`, little-endian length, type byte, payload, CRC-16, trailer `55 AA`. Implementation in `carvera_binary.py`.

- `serial_factory` returns a `BinaryFrameSerial` wrapper that the OctoPrint comm layer talks to as if it were a normal serial object. The wrapper handles framing, de-framing, and synthesizing `ok` responses so OctoPrint's ping-pong logic keeps working.
- Connection init is a framed wake sequence instead of `\n;\n`.
- `unlock()` goes through the normal queued path because the wrapper synthesizes its own ack on write.
- Fixes the stock-1.0.5 "G0 without ok" bug because the synthesized ack decouples OctoPrint's queue from what the firmware actually sends.

### 8.4 When each is used

`build_communication` picks the strategy from the `protocol` setting (`plain_text` / `binary` / `auto`). In `auto` mode, detected stock-1.0.5 flips it to binary; everything else stays plain text. Users can force either mode from the plugin settings.

---

## 9. Known Firmware Issues

| Issue | Firmware | Impact | Workaround |
|-------|----------|--------|------------|
| G0 missing `ok` reply | Stock 1.0.5 | Commands after G0 can stall in plain text | Run binary mode (§8); plain-text fallback listed G0 in `longRunningCommands`. |
| G91 no motion | All tested | Relative moves accepted, no movement | Use G90 absolute positioning. |
| DTR causes Alarm | All | Opening the serial port triggers ALARM | Send `$X` after connect. Still open — automatic clearing is on the Future Ideas list. |
| M821 work light | All | Only flashes instead of sustained on/off | No workaround. |
| 4th axis reversed | Stock 1.0.5 | A-axis operates backwards | Known bug; watch orientation. |
| Large file loading | Stock 1.0.5 | System fails on large files | Split files, or stream line-by-line. |
| `$X` / `version` without `ok` | Community 2.0.2c-RC2 | Response has no trailing `ok` and blocks OctoPrint's queue | PlainTextCommunication uses `_send_raw_text` to bypass the queue (see §8.2). |

---

## 10. File Management

### 10.1 Streaming a job from OctoPrint

Classic ping-pong G-code streaming through the OctoPrint comm layer:

```
For each G-code line:
    Send line (newline terminated)
    Wait for `ok` or `error:X`
    Never send the next line before the response
Real-time commands (?, !, ~, \x18) can be sent at any time during streaming.
Job completes when the last line is ACKed.
```

- **Pause during job**: `_comm_mode.pause()` (plain text: `!`; binary: framed equivalent). State → `Hold`.
- **Resume**: `_comm_mode.resume()` (plain text: `~`). State → `Run`.
- **Cancel**: `_comm_mode.cancel()` + `post_cancel_cleanup()` (`$X` and re-init).

### 10.2 Carvera SD card browsing

The plugin can list files on the Carvera's on-board SD card over the same serial link:

- Sends the shell command `ls -e -s` and parses the response.
- Folder navigation, file metadata (size, mtime).
- Rendered in the **Carvera Files** sidebar (template `octocarvera_files.jinja2`). The OctoPrint-side file list is renamed so the two are visually distinct.

### 10.3 XMODEM upload

Uploading a G-code file from OctoPrint to the Carvera SD card uses XMODEM via `carvera_xmodem.py`:

- Per-file progress, ETA, and cancel.
- While a file op is active, the main status loop pauses on a `file_op_event` so its ? polls don't interleave with XMODEM framing.

### 10.4 Registered file extensions

`get_extension_tree` registers `.nc`, `.tap`, `.cnc`, `.ngc`, `.ncc` so OctoPrint's file manager accepts CNC jobs alongside the default 3D-printer extensions.

---

## 11. Error Handling

| Response | Meaning | Action |
|----------|---------|--------|
| `ok` | Command accepted | Continue |
| `error:X` | Command error (X = error code) | Log, notify user |
| `ALARM:X` | Machine alarm | Stop sending, show alarm in UI |
| `[MSG:...]` | Informational message | Display to user |

Standard GRBL error codes apply. Carvera adds custom alarm messages (e.g. "Abort during cycle").

---

## 12. UI Layout

Six Jinja templates, rendered into OctoPrint's sidebar + tabs:

| Template | Where | What it renders |
|----------|-------|-----------------|
| `octocarvera_sidebar.jinja2` | Sidebar | Machine state header (raw state + activity), MPos/WPos readout, feed/spindle/tool indicators, Unlock (`$X`), Restart, job progress (percent + elapsed), M496 navigation presets. Button availability is driven by the current activity state. |
| `octocarvera_control.jinja2` | Control tab | Canvas-based XY jog knob, direction buttons, Z slider + step buttons, Go-To controls (absolute WPos), feed/spindle override sliders, jog-lock toggle (default **locked** to prevent accidental motion). |
| `octocarvera_files.jinja2` | Sidebar | Carvera SD card browser, folder navigation, XMODEM upload with progress / ETA / cancel. |
| `octocarvera_machine_status.jinja2` | — | Detailed status panel (used where a fuller readout is needed). |
| `octocarvera_settings.jinja2` | Settings | Plugin settings — requires `custom_bindings: False` so OctoPrint's `settingsViewModel` binds properly. |
| `octocarvera_transfer.jinja2` | — | Modal / panel for ongoing file transfers. |

```
┌──────────────────────────────────────────────────────────┐
│ OctoPrint Navigation                                      │
├────────────┬─────────────────────────────────────────────┤
│ SIDEBAR    │ TAB (Control)                                │
│            │                                              │
│ Carvera    │ Webcam feed                                  │
│ state      │                                              │
│ MPos/WPos  │ XY jog knob   Z slider                       │
│ F / S / T  │ Go-To buttons (WPos + M496 presets)          │
│ Unlock     │ Feed override   Spindle override             │
│ Restart    │ Jog-lock toggle                              │
│ Job %/time │                                              │
│            │                                              │
│ Carvera    │                                              │
│ Files (SD) │                                              │
│            │                                              │
│ Connection │                                              │
│ State      │                                              │
├────────────┴─────────────────────────────────────────────┤
│ Terminal / Notifications                                   │
└──────────────────────────────────────────────────────────┘
```

OctoPrint's built-in 3D-printer UI elements (temperature graph, bed visualization, extruder controls) remain visible for now — replacing them is on the Future Ideas list.

---

## 13. MQTT / Home Assistant

The plugin optionally publishes machine state to MQTT for Home Assistant automation (controlling a shop vacuum, air pressure, air filter, etc.). It does **not** run its own broker — it piggy-backs on the **OctoPrint-MQTT** plugin by grabbing its `mqtt_publish` helper, so the OctoPrint-MQTT plugin must be installed and configured with broker credentials.

### 13.1 Enabling

1. Install the **OctoPrint-MQTT** plugin and point it at your broker.
2. In OctoCarvera settings, toggle `mqtt_publish` on (default: off).
3. On save the plugin calls `_setup_mqtt()`, which resolves the helper, starts a heartbeat, and publishes Home Assistant auto-discovery configs so the sensors appear in HA automatically. If the helper isn't ready yet (load-order race), it retries up to ~15 s.

### 13.2 Topic layout

All topics hang off `octoPrint/plugin/octocarvera/<machine_slug>/…`, where `<machine_slug>` is the configured `machine_name` slugified (so multiple Carveras on one broker don't collide).

| Topic | Payload | Retained | Notes |
|-------|---------|----------|-------|
| `<base>/<slug>/status` | JSON status blob (see 13.3) | No | Published on each status update. |
| `<base>/<slug>/heartbeat` | `{"ts": ..., "alive": true}` | No | Periodic, so you can tell whether publishing is alive even when the machine is idle. |
| `<base>/<slug>/work_mode` | One of `idle`, `milling`, `laser`, `probing`, `tool_change` | **Yes (retained)** | Sticky — HA sees the current mode on reconnect without waiting for a transition. |
| `homeassistant/sensor/…/config` | HA auto-discovery configs, one per sensor | Yes | Published once on setup. |

### 13.3 Sensors exposed to Home Assistant

`_MQTT_SENSORS` in `octoprint_octocarvera/__init__.py` enumerates every sensor HA sees. Each reads a field out of the `status` topic's JSON via a template:

| Sensor | Unit | Source field |
|--------|------|--------------|
| State | — | `state` |
| Work Position X / Y / Z | mm | `work_pos.{x,y,z}` |
| Machine Position X / Y / Z | mm | `machine_pos.{x,y,z}` |
| Feed Rate | mm/min | `feed.current` |
| Feed Override | % | `feed.override` |
| Spindle RPM | RPM | `spindle.current` |
| Spindle Override | % | `spindle.override` |
| Spindle Temperature | °C | `spindle.spindle_temp` (HA device_class: temperature) |
| Power Board Temperature | °C | `spindle.power_temp` (HA device_class: temperature) |
| Tool Number | — | `tool.number` |
| Job Progress | % | `playback.percent` (or `unknown` if no playback) |
| Work Mode | — | `work_mode` |

### 13.4 Work Mode (sticky)

`work_mode` is a **separate state machine from activity gating**, computed by `_compute_work_mode()`. It exists purely as an MQTT signal for HA automations — it does not affect the plugin's UI or command gating. Values:

- `idle` — nothing running.
- `milling` — spindle on with a cutting tool loaded (T1–T100).
- `laser` — laser mode active.
- `probing` — probe tool loaded (T0 or T999990–T999999) and moving.
- `tool_change` — firmware in `Tool` or `Wait` (transient; overrides sticky mode).

Modes are sticky: once set they persist until the job is truly finished (machine `Idle` and no active playback), so HA-controlled equipment like a vacuum doesn't cycle rapidly between G-code moves. The retained publish guarantees HA has the correct mode immediately on broker reconnect.

See `memory/project_session_2026_04_13.md` for the introduction history.

---

## 14. Activity State Gating

UI buttons are enabled/disabled by activity rather than raw firmware state. The mapping is:

- `_ACTIVITY_ACTIONS` — per-activity whitelist of allowed actions.
- `_PAUSED_STATES = {"Hold", "Pause", "Wait"}`, `_MOVING_STATES = {"Run", "Jog", "Home", "Tool"}`.
- `_compute_activity()` — precedence: Alarm → OctoPrint `is_printing` → OctoPrint `is_paused` → playback-aware Hold/Pause/Wait → playback-aware Run → plain Hold/Pause/Wait → plain moving → Idle → unknown.

See `docs/machine_states.md` for the authoritative activity table and the currently-deferred expanded-activity proposal. `_compute_activity()` in `octocarvera/__init__.py` is the code of record.

---

*This document is the source of truth for OctoCarvera architecture. Update it alongside changes to the plugin.*
