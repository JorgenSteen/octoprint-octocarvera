# Machine States

## Raw Firmware States (GRBL + Carvera extensions)

These are the states the Carvera reports in its status response (`<State|...>`).

### Standard GRBL v1.1

| State | Meaning |
|-------|---------|
| `Idle` | Stationary, ready for commands |
| `Run` | Motion executing (G-code cycle or job) |
| `Hold` | Feed hold in progress (decelerating to stop) |
| `Jog` | Manual jog motion active |
| `Home` | Homing cycle ($H) in progress |
| `Alarm` | Safety violation or position unknown, locked out |
| `Door` | Safety door open (feed hold triggered) |
| `Check` | Dry-run mode (interprets G-code, no motion) |
| `Sleep` | Low-power idle |

GRBL also supports sub-states: `Hold:0` (complete), `Hold:1` (decelerating), `Door:0`-`Door:3` (various door states).

### Carvera Extensions

| State | Meaning |
|-------|---------|
| `Pause` | Job paused via M600 or pause button |
| `Wait` | Waiting for user action (e.g., manual tool change) |
| `Tool` | ATC tool change in progress |

## OctoCarvera Activity States (current: 5 states)

The plugin maps raw firmware states to higher-level "activity" states that drive UI gating (which buttons are enabled/disabled).

| Activity | Firmware States | Detection | Controls Enabled |
|----------|----------------|-----------|-----------------|
| **idle** | `Idle` | GRBL state | Everything |
| **jogging** | `Run`, `Jog`, `Home`, `Tool` (no active job) | GRBL state, no playback | Jog, estop, safety-off buttons |
| **running_job** | `Run` (with playback) | `playback.is_playing = true` OR OctoPrint `is_printing()` | Pause, cancel, overrides, restart |
| **paused** | `Hold`, `Pause`, `Wait` | GRBL state (with or without playback) | Resume, cancel, overrides |
| **alarm** | `Alarm` | GRBL state (highest priority) | Unlock, restart, estop |

### Detection Priority (highest to lowest)

1. `Alarm` -> **alarm**
2. OctoPrint `is_printing()` -> **running_job**
3. OctoPrint `is_paused()` -> **paused**
4. `playback.is_playing` + paused GRBL state -> **paused**
5. `playback.is_playing` + any other state -> **running_job**
6. `Hold` / `Pause` / `Wait` -> **paused**
7. `Run` / `Jog` / `Home` / `Tool` -> **jogging**
8. `Idle` -> **idle**
9. Anything else -> **unknown**

## Future: Expanded Activity States (not implemented)

The current plugin implements only the 5-state model above — `_compute_activity()` in `octocarvera/__init__.py` is the authoritative source. The table below is an aspirational design for splitting out distinct CNC operations that are currently lumped into `jogging`; none of these extra states exist in code yet.

| Activity | Firmware States | What user sees | Controls Enabled |
|----------|----------------|---------------|-----------------|
| **idle** | `Idle` | Ready | Everything |
| **jogging** | `Jog` | Manual motion | Jog, estop, safety-off buttons |
| **homing** | `Home` | Homing... | Estop only |
| **tool_change** | `Tool`, `Wait` | Tool Change | Estop, confirm/resume |
| **running_job** | `Run` + playback | Running Job (%) | Pause, cancel, overrides, restart |
| **paused** | `Pause`, `Hold` + playback | Paused | Resume, cancel, overrides |
| **probing** | `Run` + probe context | Probing... | Estop only |
| **alarm** | `Alarm` | ALARM | Unlock, restart, estop |

### Open questions

- Is `Door` relevant for Carvera? (Does it have a safety door switch?)
- Is `Check` (dry-run) useful to expose?
- How to detect probing vs. normal Run? (No distinct firmware state for probing)
- Should `Wait` map to tool_change or its own state?

## Tool Number Mapping

| Number | Display Name | Spindle Allowed |
|--------|-------------|----------------|
| < 0 or NaN | Empty | No |
| 0 | Probe | No |
| 1-100 | Tool 1 - Tool 100 | Yes |
| 8888 | Laser | No |
| 999990-999999 | 3D Probe | No |

## References

- GRBL v1.1 Interface: https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface
- Carvera Firmware: https://github.com/MakeraInc/CarveraFirmware
- Carvera Community Controller: https://github.com/Carvera-Community/Carvera_Controller
- Smoothieware: http://smoothieware.org/console-commands
