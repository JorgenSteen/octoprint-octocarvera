# coding=utf-8
"""Carvera CNC protocol constants, commands, and status parsing.

This file contains everything specific to the Carvera firmware protocol.
If Makera changes the command format or status response structure, only
this file needs updating.
"""

import re

# ~~~ Connection ~~~

INIT_SEQUENCE = "\n;\n"
BAUD_RATE = 115200
STATUS_POLL_INTERVAL = 0.3  # Carvera Controller polls at 0.3s

# ~~~ Realtime commands (work during motion, bypass command queue) ~~~

RT_STATUS_QUERY = b"?"
RT_FEED_HOLD = b"!"
RT_CYCLE_START = b"~"
RT_SOFT_RESET = b"\x18"
RT_STOP_JOG = b"\x19"

# ~~~ Machine control ~~~

CMD_UNLOCK = "$X"
CMD_HOME = "$H"
CMD_SETTINGS = "$$"
CMD_VERSION = "version"
CMD_RESTART = "reset"

# ~~~ Spindle ~~~

CMD_SPINDLE_ON = "M3 S{rpm}"
CMD_SPINDLE_OFF = "M5"

# ~~~ Air assist ~~~

CMD_AIR_ON = "M7"
CMD_AIR_OFF = "M9"

# ~~~ Tool change ~~~

CMD_TOOL_CHANGE = "M6 T{tool}"
CMD_TOOL_DROP = "M6 T-1"

# ~~~ Navigation (Carvera-specific M496) ~~~

CMD_GOTO_CLEARANCE = "M496.1"
CMD_GOTO_WORK_ORIGIN = "M496.2"
CMD_GOTO_ANCHOR1 = "M496.3"
CMD_GOTO_ANCHOR2 = "M496.4"
CMD_GOTO_POSITION = "M496.5 X{x} Y{y}"

# ~~~ Overrides ~~~

CMD_FEED_OVERRIDE_STOCK = "M220 S{pct}"
CMD_SPINDLE_OVERRIDE_STOCK = "M223 S{pct}"
CMD_FEED_OVERRIDE_COMMUNITY = "$F S{pct}"
CMD_SPINDLE_OVERRIDE_COMMUNITY = "$O S{pct}"

# ~~~ Movement (absolute only — G91 relative does not work on Carvera) ~~~

CMD_RAPID_ABSOLUTE = "G0 G90 X{x} Y{y} Z{z}"
CMD_LINEAR_ABSOLUTE = "G1 G90 X{x} Y{y} Z{z} F{feed}"

# ~~~ Probing / Calibration ~~~

CMD_CALIBRATE_TOOL = "M491"
CMD_CHECK_TOOL = "M491.1"
CMD_AUTO_LEVEL = "M495"

# ~~~ Laser ~~~

CMD_LASER_MODE_ON = "M321"
CMD_LASER_MODE_OFF = "M322"
CMD_LASER_TEST_ON = "M323"
CMD_LASER_TEST_OFF = "M324"
CMD_LASER_POWER = "M325 S{pct}"

# ~~~ Accessories ~~~

CMD_LIGHT_ON = "M821"
CMD_LIGHT_OFF = "M822"
CMD_VACUUM_ON = "M801 S{power}"
CMD_VACUUM_OFF = "M802"

# File operations (shell commands, not G-code)
CMD_LS = "ls -e -s {path}"
CMD_UPLOAD = "upload {path}"
CMD_PLAY = "play {path}"
CMD_DELETE = "rm {path} -e"

# ~~~ Machine specifications (keyed by C field model number) ~~~

CARVERA_SPECS = {
    1: {  # Original Carvera
        "name": "Carvera",
        "work_area": {"x": 300, "y": 200, "z": 75},
        "max_feed": 3000,
        "max_spindle": 12000,
        "has_atc": True,
    },
    2: {  # Carvera Air
        "name": "Carvera Air",
        "work_area": {"x": 278, "y": 192, "z": 57},
        "max_feed": 3000,
        "max_spindle": 10000,
        "has_atc": False,
    },
}

# Default to Carvera Air for backward compatibility
CARVERA_AIR = {
    "work_area": {"x": 278, "y": 192, "z": 57},
    "home_mpos": {"x": -278.2, "y": -192.0, "z": -57.0, "a": -86.2, "b": 0.0},
    "max_feed": 3000,
    "max_spindle": 10000,
    "clearance_mpos": {"x": -5.0, "y": -21.0, "z": -3.0},
    "anchor1_mpos": {"x": -291.0, "y": -203.0},
    "anchor2_offset": {"x": 88.5, "y": 45.0},
}

# ~~~ Status parsing ~~~

# Outer regex: extract state and optional pipe-delimited fields
CARVERA_STATUS_RE = re.compile(r"<(\w+)(?:\|(.+))?>")

# Valid GRBL states (includes Carvera-specific: Pause, Wait, Tool)
GRBL_STATES = {
    "Idle", "Run", "Hold", "Jog", "Alarm", "Door", "Check",
    "Home", "Sleep", "Pause", "Wait", "Tool",
}

# Axis labels for position parsing
_AXES = ("x", "y", "z", "a", "b")


def _parse_position(value_str):
    """Parse a comma-separated position string into a 5-axis dict."""
    parts = value_str.split(",")
    pos = {"x": 0.0, "y": 0.0, "z": 0.0, "a": 0.0, "b": 0.0}
    for i, val in enumerate(parts):
        if i < len(_AXES):
            pos[_AXES[i]] = float(val)
    return pos


def parse_carvera_status(line):
    """Parse a Carvera status response into a structured dict.

    Returns None if the line is not a valid status response.
    Returns a dict with: state, machine_pos, work_pos, feed, spindle, tool, homed.
    Missing fields are set to None.
    """
    match = CARVERA_STATUS_RE.match(line.strip() if line else "")
    if not match:
        return None

    result = {
        "state": match.group(1),
        "machine_pos": None,
        "work_pos": None,
        "feed": None,
        "spindle": None,
        "tool": None,
        "halt_reason": None,
        "wpvoltage": None,
        "laser": None,
        "config": None,
        "playback": None,
        "atc_state": None,
        "leveling": None,
        "rotation": None,
        "wcs": None,
        "pwm": None,
    }

    fields_str = match.group(2)
    if not fields_str:
        return result

    for segment in fields_str.split("|"):
        colon = segment.find(":")
        if colon == -1:
            continue
        key = segment[:colon].strip()
        val = segment[colon + 1:].strip()

        try:
            if key == "MPos":
                result["machine_pos"] = _parse_position(val)
            elif key == "WPos":
                result["work_pos"] = _parse_position(val)
            elif key == "F":
                parts = val.split(",")
                result["feed"] = {
                    "current": float(parts[0]),
                    "max": float(parts[1]),
                    "override": float(parts[2]),
                }
            elif key == "S":
                parts = val.split(",")
                result["spindle"] = {
                    "current": float(parts[0]),
                    "max": float(parts[1]),
                    "override": float(parts[2]),
                    "vacuum_mode": int(parts[3]) if len(parts) > 3 else 0,
                    "spindle_temp": float(parts[4]) if len(parts) > 4 else 0.0,
                    "power_temp": float(parts[5]) if len(parts) > 5 else 0.0,
                }
            elif key == "T":
                parts = val.split(",")
                result["tool"] = {
                    "number": int(parts[0]),
                    "offset": float(parts[1]),
                    "target": int(parts[2]) if len(parts) > 2 else -1,
                }
            elif key == "H":
                result["halt_reason"] = int(val)
            elif key == "W":
                result["wpvoltage"] = float(val)
            elif key == "L":
                parts = val.split(",")
                result["laser"] = {
                    "mode": int(parts[0]),
                    "state": int(parts[1]) if len(parts) > 1 else 0,
                    "testing": int(parts[2]) if len(parts) > 2 else 0,
                    "power": float(parts[3]) if len(parts) > 3 else 0.0,
                    "scale": float(parts[4]) if len(parts) > 4 else 0.0,
                }
            elif key == "C":
                parts = val.split(",")
                result["config"] = {
                    "model": int(parts[0]) if len(parts) > 0 else 0,
                    "func_setting": int(parts[1]) if len(parts) > 1 else 0,
                    "inch_mode": int(parts[2]) if len(parts) > 2 else 0,
                    "absolute_mode": int(parts[3]) if len(parts) > 3 else 0,
                }
            elif key == "P":
                parts = val.split(",")
                result["playback"] = {
                    "played_lines": int(parts[0]),
                    "percent": int(parts[1]) if len(parts) > 1 else 0,
                    "elapsed_secs": int(parts[2]) if len(parts) > 2 else 0,
                    "is_playing": int(parts[3]) != 0 if len(parts) > 3 else False,
                }
            elif key == "A":
                result["atc_state"] = int(val)
            elif key == "O":
                result["leveling"] = float(val)
            elif key == "R":
                result["rotation"] = float(val)
            elif key == "G":
                result["wcs"] = int(val)
            elif key == "PWM":
                result["pwm"] = float(val)
        except (ValueError, IndexError):
            pass  # Skip malformed fields

    return result


# ~~~ G-code translation/suppression ~~~

# OctoPrint sends 3D-printer M-codes that don't exist in GRBL.
# Translate to GRBL equivalents.
GCODE_TRANSLATIONS = {
    "M105": "?",          # Temperature query -> status query
    "M114": "?",          # Position report -> status query
    "M115": "version",    # Firmware info -> version probe ($$ errors on fw 1.0.5)
    "M400": "G4 P0.001",  # Wait for moves -> dwell
    "M999": "$X",         # Reset-from-error -> GRBL unlock (fw 1.0.5 silently drops M999)
}

# Suppress entirely (3D printer only, meaningless for CNC)
SUPPRESSED_GCODES = {
    "M21", "M84", "M104", "M140", "M106", "M107", "M109", "M190", "M110",
}

# ~~~ OctoPrint serial config (replaces what BGS used to set) ~~~

OCTOPRINT_SERIAL_CONFIG = {
    "neverSendChecksum": True,
    "checksumRequiringCommands": [],
    # `$G` instead of `version` because community firmware 2.0.2c-RC2
    # replies to `version` without a trailing `ok\n`, which hangs
    # OctoPrint's hello handshake in "Connecting" until it times out.
    # `$G` (view parser state) is standard GRBL in both modes and
    # always follows with `ok` — binary mode via the wrapper's
    # synthesized ack, plain text via the firmware's real ack.
    "helloCommand": "$G",
    "encoding": "latin_1",
    "sanityCheckTools": False,
    "unknownCommandsNeedAck": False,
    "sendChecksumWithUnknownCommands": False,
    "emergencyCommands": [],
    "blockedCommands": [],
    "ackMax": 1,
    "longRunningCommands": [
        "G4", "G28", "G29", "G30", "G32", "M400", "M226", "M600",
        "$H", "G92", "G53", "G54", "G20", "G21", "G90", "G91",
        "G38.1", "G38.2", "G38.3", "G38.4", "G38.5",
        "G0", "G1", "G2", "G3", "M3", "M4", "M5", "M7", "M8", "M9", "M30",
    ],
}
