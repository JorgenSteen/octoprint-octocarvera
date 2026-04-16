# coding=utf-8
"""OctoCarvera — OctoPrint plugin for the Makera Carvera Air CNC.

Connects a Carvera Air to OctoPrint via USB serial (FTDI 232R, 115200 8N1)
using the machine's Smoothieware-based GRBL-compatible protocol. Supports
both community firmware (plain-text) and stock firmware 1.0.5 (binary framing).

Architecture:
    - carvera_protocol.py: status parsing, command constants, G-code translation
    - carvera_binary.py:   binary frame encoding/decoding for stock firmware
    - carvera_comm.py:     Communication strategy (plain-text vs binary)
    - carvera_xmodem.py:   XMODEM-128 file transfer
    - carvera_files.py:    SD card file operations (ls, upload, delete, move)
    - __init__.py:         plugin entry point, OctoPrint hooks, API, MQTT
"""
from __future__ import absolute_import

import os
import time
import threading

import octoprint.plugin
from octoprint.events import Events

from .carvera_protocol import (
    STATUS_POLL_INTERVAL,
    RT_STATUS_QUERY,
    CMD_UNLOCK, CMD_VERSION, CMD_RESTART,
    CMD_SPINDLE_ON, CMD_SPINDLE_OFF,
    CMD_AIR_ON, CMD_AIR_OFF,
    CMD_LIGHT_ON, CMD_LIGHT_OFF,
    CMD_VACUUM_ON, CMD_VACUUM_OFF,
    CMD_GOTO_CLEARANCE, CMD_GOTO_WORK_ORIGIN, CMD_GOTO_ANCHOR1, CMD_GOTO_ANCHOR2,
    GRBL_STATES, GCODE_TRANSLATIONS, SUPPRESSED_GCODES,
    OCTOPRINT_SERIAL_CONFIG, CARVERA_AIR,
    parse_carvera_status,
)
from .carvera_files import list_files, upload_file
from .carvera_comm import Communication, build_communication


class OctoCarveraPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    """Main plugin class — wires OctoPrint hooks to the Carvera.

    Responsibilities:
        - Serial hooks: translates G-code, intercepts firmware errors, gates
          motion commands based on activity state.
        - Status polling: 0.3s keepalive sends ``?`` for GRBL status, parses
          the response, pushes updates to the frontend via plugin messages.
        - API: exposes jog, goto, spindle, overrides, file ops, firmware flash.
        - MQTT: publishes machine state + HA auto-discovery for external
          automation (vacuum, air filter, etc.).
    """

    def __init__(self):
        super().__init__()

        # Machine state (updated by _parse_grbl_status from status polls)
        self._grbl_state = "Unknown"
        self._machine_pos = {"x": 0.0, "y": 0.0, "z": 0.0, "a": 0.0, "b": 0.0}
        self._work_pos = {"x": 0.0, "y": 0.0, "z": 0.0, "a": 0.0, "b": 0.0}
        self._feed = {"current": 0.0, "max": 0.0, "override": 100.0}
        self._spindle = {"current": 0.0, "max": 0.0, "override": 100.0, "vacuum_mode": 0, "spindle_temp": 0.0, "power_temp": 0.0}
        self._tool = {"number": 0, "offset": 0.0, "target": -1}
        self._work_mode = "idle"       # sticky MQTT work mode (see _compute_work_mode)
        self._halt_reason = None
        self._wpvoltage = 0.0
        self._laser = {"mode": 0, "state": 0, "testing": 0, "power": 0.0, "scale": 0.0}
        self._config = {"model": 0, "func_setting": 0, "inch_mode": 0, "absolute_mode": 0}
        self._playback = None          # None = no active SD playback
        self._atc_state = None
        self._leveling = None
        self._rotation = None
        self._wcs = None
        self._pwm = None

        # Connection and keepalive
        self._connected = False
        self._keepalive_active = False
        self._keepalive_thread = None
        self._firmware_version = None
        self._detected_firmware_type = None  # "stock" or "community"

        # MQTT / Home Assistant integration
        self._mqtt_publish = None
        self._mqtt_heartbeat_active = False
        self._mqtt_heartbeat_thread = None
        self._mqtt_retry_count = 0

        # File operation synchronization — serial is half-duplex, so file ops
        # (ls, upload, delete) must pause status polling and collect responses.
        self._file_op_event = threading.Event()
        self._file_op_event.set()      # set = polling runs; clear = file op owns serial
        self._upload_cancel = threading.Event()
        self._file_op_lines = []       # collected response lines during file ops
        self._file_op_done = threading.Event()

        # Communication strategy and jog buffering
        self._comm_mode: Communication = None  # built in on_after_startup / on_settings_save
        self._jog_pending = None       # latest-wins single-slot buffer (see _handle_jog)
        self._flash_after_upload = False

    # ~~~ StartupPlugin ~~~

    def on_after_startup(self):
        self._logger.info("OctoCarvera plugin started")
        self._configure_octoprint()
        self._rebuild_comm_mode()

        # MQTT setup
        if self._settings.get_boolean(["mqtt_publish"]):
            self._setup_mqtt()

        # Handle auto-connect race: OctoPrint may have connected before plugin loaded
        if self._printer and self._printer.is_operational():
            self._logger.info("Printer already connected on startup - starting keepalive")
            self._connected = True
            self._start_keepalive()
            self._send_command(CMD_VERSION)

    def _rebuild_comm_mode(self):
        """Build the Communication strategy for the current protocol_mode setting.

        Called at startup and whenever protocol_mode changes in settings. The
        strategy encapsulates every binary-vs-plaintext decision so the rest
        of the plugin can dispatch polymorphically without branching.
        """
        mode = self._settings.get(["protocol_mode"])
        self._comm_mode = build_communication(
            mode,
            self._send_command,
            self._send_realtime,
            self._send_raw_text,
            self._logger,
        )
        self._logger.info("Communication mode: %s", self._comm_mode.name)

    def _configure_octoprint(self):
        """Configure OctoPrint serial settings for GRBL/Smoothieware CNC."""
        for key, value in OCTOPRINT_SERIAL_CONFIG.items():
            self._settings.global_set(["serial", key], value)
        self._settings.global_set(["serial", "maxCommunicationTimeouts", "long"], 0)

        # Disable irrelevant 3D printer features
        self._settings.global_set(["feature", "modelSizeDetection"], False)
        self._settings.global_set(["feature", "sdSupport"], False)
        self._settings.global_set(["feature", "temperatureGraph"], False)
        self._settings.global_set(["gcodeAnalysis", "runAt"], "never")

        # Disable irrelevant tabs
        self._settings.global_set(
            ["appearance", "components", "disabled", "tab"],
            ["temperature", "plugin_gcodeviewer", "control"],
        )

        # Sidebar order: connection, state, carvera, files
        self._settings.global_set(
            ["appearance", "components", "order", "sidebar"],
            ["connection", "state", "plugin_octocarvera", "plugin_octocarvera_machine_status", "files"],
        )

        # Tab order — Carvera tab first
        tab_order = self._settings.global_get(
            ["appearance", "components", "order", "tab"]
        ) or []
        if "plugin_octocarvera" in tab_order:
            tab_order.remove("plugin_octocarvera")
        tab_order.insert(0, "plugin_octocarvera")
        self._settings.global_set(
            ["appearance", "components", "order", "tab"], tab_order
        )

        # Create Carvera Air printer profile if it doesn't exist
        self._create_printer_profile()

        self._settings.save()

    def _create_printer_profile(self):
        """Create a Carvera Air printer profile if not already present."""
        pm = self._printer_profile_manager
        if pm and not pm.exists("_carvera_air"):
            area = CARVERA_AIR["work_area"]
            profile = {
                "id": "_carvera_air",
                "name": "Carvera Air",
                "model": "Makera Carvera Air",
                "heatedBed": False,
                "heatedChamber": False,
                "volume": {
                    "width": float(area["x"]),
                    "depth": float(area["y"]),
                    "height": float(area["z"]),
                    "origin": "lowerleft",
                    "formFactor": "rectangular",
                },
                "axes": {
                    "x": {"speed": CARVERA_AIR["max_feed"], "inverted": False},
                    "y": {"speed": CARVERA_AIR["max_feed"], "inverted": False},
                    "z": {"speed": 200, "inverted": False},
                    "e": {"speed": 300, "inverted": False},
                },
                "extruder": {"count": 1, "nozzleDiameter": 0.4},
            }
            try:
                pm.save(profile, allow_overwrite=False, make_default=False)
                self._logger.info("Created Carvera Air printer profile")
            except Exception:
                self._logger.debug("Could not save Carvera Air printer profile")

    # ~~~ SettingsPlugin ~~~

    def get_settings_defaults(self):
        return {
            "serial_port": "/dev/ttyUSB0",
            "baud_rate": 115200,
            "send_init_on_connect": True,
            "connection_timeout": 10.0,
            "protocol_mode": "plain_text",
            "override_mode": "auto",
            "mqtt_publish": False,
            "machine_name": "Carvera",
        }

    def on_settings_save(self, data):
        """Persist settings and react to changes in protocol_mode, MQTT, or machine_name."""
        self._logger.info("on_settings_save incoming data keys: %r", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        old_mode = self._settings.get(["protocol_mode"])
        old_mqtt = self._settings.get_boolean(["mqtt_publish"])
        old_name = self._settings.get(["machine_name"])
        old_slug = self._slugify(old_name)
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        new_mode = self._settings.get(["protocol_mode"])
        new_mqtt = self._settings.get_boolean(["mqtt_publish"])
        new_name = self._settings.get(["machine_name"])
        new_slug = self._slugify(new_name)
        self._logger.info(
            "Settings saved: protocol_mode %s->%s, mqtt_publish %s->%s, machine_name %r->%r",
            old_mode, new_mode, old_mqtt, new_mqtt, old_name, new_name,
        )
        if new_mode != old_mode:
            self._rebuild_comm_mode()
        if new_mqtt:
            if not self._mqtt_publish:
                self._setup_mqtt()
            elif new_slug != old_slug:
                # Machine renamed — clear the old retained HA configs so
                # the stale device disappears, then republish under the
                # new slug.
                self._clear_ha_discovery(old_slug)
                self._publish_ha_discovery()
        else:
            self._mqtt_publish = None
            self._stop_mqtt_heartbeat()

    def _clear_ha_discovery(self, slug):
        """Clear retained HA discovery configs for a given slug by publishing
        empty retained payloads. HA treats an empty retained config as 'remove
        this entity'."""
        if not self._mqtt_publish or not slug:
            return
        discovery_prefix = "{}/octocarvera_{}".format(self._HA_DISCOVERY_BASE, slug)
        for sensor_id, *_ in self._MQTT_SENSORS:
            try:
                self._mqtt_publish(
                    "{}/{}/config".format(discovery_prefix, sensor_id),
                    "", retained=True,
                )
            except Exception:
                self._logger.exception("Failed to clear HA config for %s", sensor_id)
        self._logger.info("Cleared HA discovery for old slug=%r", slug)

    # ~~~ AssetPlugin ~~~

    def get_assets(self):
        return {
            "js": ["js/octocarvera.js"],
            "css": ["css/octocarvera.css"],
        }

    # ~~~ TemplatePlugin ~~~

    def get_template_configs(self):
        return [
            {"type": "sidebar", "name": "Carvera Status", "template": "octocarvera_sidebar.jinja2"},
            {"type": "sidebar", "name": "Machine Status", "template": "octocarvera_machine_status.jinja2",
             "suffix": "_machine_status", "custom_bindings": False},
            {"type": "tab", "name": "Carvera", "template": "octocarvera_control.jinja2"},
            {"type": "sidebar", "name": "Carvera Files", "template": "octocarvera_files.jinja2"},
            {"type": "sidebar", "name": "Transfer Files", "template": "octocarvera_transfer.jinja2"},
            {"type": "settings", "name": "OctoCarvera", "template": "octocarvera_settings.jinja2",
             "custom_bindings": False},
        ]

    # ~~~ EventHandlerPlugin ~~~

    def on_event(self, event, payload):
        if event == Events.CONNECTED:
            self._on_printer_connected()
        elif event == Events.DISCONNECTED:
            self._on_printer_disconnected()

    # ~~~ SimpleApiPlugin ~~~

    def is_api_protected(self):
        return True

    # Activity classification: one level above raw GRBL state. Separates
    # "running a streamed file" from "user is jogging/goto-ing" — both of
    # which show up as GRBL state "Run" — and drives the permission table
    # below so the frontend can distinguish them.
    _ACTIVITY_ACTIONS = {
        "idle": {
            "status", "send_command", "estop",
            "job_pause", "job_resume", "job_cancel",
            "feed_override", "spindle_override",
            "unlock",
            "goto_clearance", "goto_work_origin", "goto_anchor1", "goto_anchor2",
            "spindle_on", "spindle_off",
            "air_on", "air_off",
            "light_on", "light_off",
            "vacuum_on", "vacuum_off",
            "jog", "goto",
            "restart",
            "list_files", "upload_to_carvera",
            "delete_file", "create_folder", "move_file",
        },
        "jogging": {
            # User is mid-motion. Keep the jog control live (so the user can
            # reposition) and the safety-path peripheral "off" buttons live,
            # but disable everything else (navigation presets, goto, new
            # peripherals, etc.). If the user needs to abort, the red E-stop
            # is always there. Jogs sent while motion is in flight are
            # buffered on the plugin side (single pending slot) rather than
            # chained through the firmware's planner.
            "status", "estop",
            "feed_override", "spindle_override",
            "spindle_off", "air_off", "light_off", "vacuum_off",
            "jog",
        },
        "running_job": {
            # Active job (streamed by OctoPrint or playing from Carvera SD).
            # No ad-hoc motion, no peripheral changes.
            "status", "estop",
            "job_pause", "job_cancel",
            "feed_override", "spindle_override",
            "restart",
        },
        "paused": {
            "status", "estop",
            "job_resume", "job_cancel",
            "feed_override", "spindle_override",
        },
        "alarm": {"status", "estop", "send_command", "unlock", "restart"},
    }
    _ALWAYS_ALLOWED = {"status", "estop", "cancel_upload", "list_files"}

    # GRBL states that map to each activity. Firmware state alone doesn't
    # tell us the activity — we also need `is_printing()` / `is_paused()`
    # from OctoPrint to distinguish a streamed job from an ad-hoc move.
    _PAUSED_STATES = frozenset(("Hold", "Pause", "Wait"))
    _MOVING_STATES = frozenset(("Run", "Jog", "Home", "Tool"))

    def _compute_activity(self):
        """Classify current machine activity — what's it doing, and why.

        Precedence matters: alarm trumps everything; an active job (OctoPrint
        streamed OR Carvera SD playback) trumps ad-hoc motion; pause trumps
        plain motion.
        """
        state = self._grbl_state
        if state == "Alarm":
            return "alarm"
        # Detect active job — either OctoPrint streaming or Carvera SD playback.
        playback_active = (self._playback is not None
                           and self._playback.get("is_playing", False))
        printer = self._printer
        if printer is not None:
            try:
                if printer.is_printing():
                    return "running_job"
                if printer.is_paused():
                    return "paused"
            except Exception:
                pass
        if playback_active:
            if state in self._PAUSED_STATES:
                return "paused"
            return "running_job"
        if state in self._PAUSED_STATES:
            return "paused"
        if state in self._MOVING_STATES:
            return "jogging"
        if state == "Idle":
            return "idle"
        return "unknown"

    def _get_allowed_actions(self):
        return self._ACTIVITY_ACTIONS.get(self._compute_activity(), self._ALWAYS_ALLOWED)

    # ~~~ Work Mode — EXTERNAL ONLY (MQTT/HA automation) ~~~
    #
    # This is a separate state machine for Home Assistant automations that
    # control external peripherals (vacuum, air pressure, air filter, etc.).
    # It does NOT affect any internal plugin behavior, UI gating, or command
    # permissions.  It is purely an additional MQTT signal.
    #
    # Modes are STICKY — once entered, they persist until the job is truly
    # finished, to avoid rapid on/off cycling of external equipment.

    def _compute_work_mode(self):
        """Derive the current work mode for MQTT/HA automation.

        Modes: idle, milling, laser, probing, tool_change. Modes are sticky —
        once entered, they persist until the job is truly finished (machine
        Idle + no active playback) to avoid rapid on/off cycling of external
        equipment like vacuums and air filters.
        """
        state = self._grbl_state

        # Tool change always wins (transient, overrides sticky mode)
        if state in ("Tool", "Wait"):
            return "tool_change"

        # Read current flags
        tool_num = self._tool.get("number", 0) if self._tool else 0
        spindle_on = (self._spindle.get("current", 0) > 0) if self._spindle else False
        laser_on = (self._laser.get("mode", 0) == 1) if self._laser else False
        is_probe = tool_num == 0 or (999990 <= tool_num <= 999999)
        is_cutting_tool = 1 <= tool_num <= 100

        # Enter new modes (sticky — once set, stays until exit condition)
        if laser_on:
            self._work_mode = "laser"
        elif spindle_on and is_cutting_tool:
            self._work_mode = "milling"
        elif is_probe and state in self._MOVING_STATES:
            self._work_mode = "probing"

        # Sticky exit: only return to idle when truly done
        if self._work_mode == "milling":
            playback_done = (self._playback is None
                             or not self._playback.get("is_playing", False))
            if state == "Idle" and playback_done:
                self._work_mode = "idle"
        elif self._work_mode == "laser":
            if not laser_on and state == "Idle":
                self._work_mode = "idle"
        elif self._work_mode == "probing":
            if state == "Idle":
                self._work_mode = "idle"

        return self._work_mode

    def get_api_commands(self):
        return {
            "status": [],
            "send_command": ["command"],
            "estop": [],
            "unlock": [],
            "restart": [],
            "job_pause": [],
            "job_resume": [],
            "job_cancel": [],
            "feed_override": ["value"],
            "spindle_override": ["value"],
            "goto_clearance": [],
            "goto_work_origin": [],
            "goto_anchor1": [],
            "goto_anchor2": [],
            "spindle_on": ["rpm"],
            "spindle_off": [],
            "air_on": [],
            "air_off": [],
            "light_on": [],
            "light_off": [],
            "vacuum_on": [],
            "vacuum_off": [],
            "jog": [],
            "goto": [],
            "list_files": [],
            "upload_to_carvera": ["filename"],
            "cancel_upload": [],
            "delete_file": ["path"],
            "create_folder": ["path"],
            "move_file": ["src", "dst"],
        }

    def on_api_command(self, command, data):
        """Dispatch an API command, checking activity-based permissions first."""
        import flask

        allowed = self._get_allowed_actions()
        if command not in allowed:
            return flask.jsonify(
                ok=False, error=f"Command '{command}' not allowed in state '{self._grbl_state}'"
            ), 409

        if command == "status":
            return flask.jsonify(**self._get_status_dict())
        elif command == "send_command":
            cmd = data.get("command", "").strip()
            if cmd:
                self._send_command(cmd)
                return flask.jsonify(ok=True)
            return flask.jsonify(ok=False, error="Empty command"), 400
        elif command == "estop":
            self._comm_mode.estop()
            self._logger.warning("E-STOP triggered")
            return flask.jsonify(ok=True)
        elif command == "unlock":
            self._comm_mode.unlock()
            self._logger.info("Unlock ($X) sent via %s", self._comm_mode.name)
            return flask.jsonify(ok=True)
        elif command == "restart":
            self._send_command(CMD_RESTART)
            self._logger.warning("Machine restart sent")
            return flask.jsonify(ok=True)
        elif command == "job_pause":
            self._comm_mode.pause()
            return flask.jsonify(ok=True)
        elif command == "job_resume":
            self._comm_mode.resume()
            return flask.jsonify(ok=True)
        elif command == "job_cancel":
            self._cancel_job()
            return flask.jsonify(ok=True)
        elif command == "feed_override":
            self._send_override("feed", int(data.get("value", 100)))
            return flask.jsonify(ok=True)
        elif command == "spindle_override":
            self._send_override("spindle", int(data.get("value", 100)))
            return flask.jsonify(ok=True)
        # Navigation
        elif command == "goto_clearance":
            self._send_command(CMD_GOTO_CLEARANCE)
            return flask.jsonify(ok=True)
        elif command == "goto_work_origin":
            self._send_command(CMD_GOTO_WORK_ORIGIN)
            return flask.jsonify(ok=True)
        elif command == "goto_anchor1":
            self._send_command(CMD_GOTO_ANCHOR1)
            return flask.jsonify(ok=True)
        elif command == "goto_anchor2":
            self._send_command(CMD_GOTO_ANCHOR2)
            return flask.jsonify(ok=True)
        # Spindle
        elif command == "spindle_on":
            tool_num = self._tool.get("number", 0)
            if not (1 <= tool_num <= 100):
                return flask.make_response(
                    flask.jsonify(error="Spindle blocked: no cutting tool loaded (T{})".format(tool_num)),
                    409,
                )
            rpm = int(data.get("rpm", 10000))
            self._send_command(CMD_SPINDLE_ON.format(rpm=rpm))
            return flask.jsonify(ok=True)
        elif command == "spindle_off":
            self._send_command(CMD_SPINDLE_OFF)
            return flask.jsonify(ok=True)
        # Accessories
        elif command == "air_on":
            self._send_command(CMD_AIR_ON)
            return flask.jsonify(ok=True)
        elif command == "air_off":
            self._send_command(CMD_AIR_OFF)
            return flask.jsonify(ok=True)
        elif command == "light_on":
            self._send_command(CMD_LIGHT_ON)
            return flask.jsonify(ok=True)
        elif command == "light_off":
            self._send_command(CMD_LIGHT_OFF)
            return flask.jsonify(ok=True)
        elif command == "vacuum_on":
            self._send_command(CMD_VACUUM_ON.format(power=100))
            return flask.jsonify(ok=True)
        elif command == "vacuum_off":
            self._send_command(CMD_VACUUM_OFF)
            return flask.jsonify(ok=True)
        elif command == "jog":
            return self._handle_jog(data)
        elif command == "goto":
            return self._handle_goto(data)
        # File operations
        elif command == "list_files":
            return self._handle_list_files(data)
        elif command == "upload_to_carvera":
            return self._handle_upload_to_carvera(data)
        elif command == "cancel_upload":
            self._upload_cancel.set()
            self._logger.info("Upload cancel requested")
            return flask.jsonify(ok=True)
        elif command == "delete_file":
            return self._handle_delete_file(data)
        elif command == "create_folder":
            return self._handle_create_folder(data)
        elif command == "move_file":
            return self._handle_move_file(data)

    def _get_status_dict(self):
        """Build the full status payload sent to the frontend and API callers."""
        return {
            "state": self._grbl_state,
            "activity": self._compute_activity(),
            "machine_pos": self._machine_pos,
            "work_pos": self._work_pos,
            "feed": self._feed,
            "spindle": self._spindle,
            "tool": self._tool,
            "halt_reason": self._halt_reason,
            "wpvoltage": self._wpvoltage,
            "laser": self._laser,
            "config": self._config,
            "playback": self._playback,
            "atc_state": self._atc_state,
            "leveling": self._leveling,
            "rotation": self._rotation,
            "wcs": self._wcs,
            "pwm": self._pwm,
            "connected": self._connected,
            "firmware_version": self._firmware_version,
            "plugin_version": self._plugin_version,
            "allowed_actions": list(self._get_allowed_actions()),
        }

    def on_api_get(self, request):
        import flask
        return flask.jsonify(**self._get_status_dict())

    # ~~~ Serial communication hooks ~~~

    _MOTION_GCODES = {"G0", "G1", "G2", "G3", "G28", "G30", "G38"}

    def _is_motion_command(self, cmd, gcode):
        if gcode in self._MOTION_GCODES:
            return True
        if isinstance(cmd, str):
            s = cmd.lstrip()
            if s.startswith("$H") or s.startswith("$J"):
                return True
        return False

    @staticmethod
    def _is_file_stream_command(tags):
        """Is this outgoing command being emitted by OctoPrint's file streamer?

        OctoPrint tags commands it reads from the queued print file with
        ``source:file``. Ad-hoc commands from the API / terminal / plugin
        don't carry that tag, so we can tell them apart.
        """
        if not tags:
            return False
        try:
            return any(
                isinstance(t, str) and (t == "source:file" or t.startswith("filepos:"))
                for t in tags
            )
        except Exception:
            return False

    def sending_gcode_hook(self, comm_instance, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        """OctoPrint hook: intercept outgoing G-code before it hits serial.

        Responsibilities:
          1. Suppress all commands while a file op owns the serial line.
          2. Drop 3D-printer-only G-codes (temperature, extrusion, etc.).
          3. Translate OctoPrint G-codes to Carvera equivalents (e.g. M999 -> $X).
          4. Gate motion commands based on activity state — block ad-hoc
             moves during a running job, pause, or alarm.
        Returns the (possibly modified) command, or (None,) to suppress it.
        """
        if not cmd:
            return cmd
        # Suppress all commands during file operations (serial is busy)
        if not self._file_op_event.is_set():
            return (None,)
        if gcode in SUPPRESSED_GCODES:
            self._logger.debug("Suppressed: %s", cmd)
            return (None,)
        if gcode in GCODE_TRANSLATIONS:
            # M999 -> $X is binary-mode-only: stock fw 1.0.5 silently
            # drops M999, but community firmware 2.0.2c-RC2 supports it
            # natively and acks it with ok. Translating in plain-text
            # mode would also work, but $X in Idle state on community
            # firmware returns nothing at all (no ok), which deadlocks
            # the command queue. Let M999 through unchanged in plain
            # text mode so the firmware's own ack advances the queue.
            if gcode == "M999" and self._comm_mode and self._comm_mode.name == "plain_text":
                pass  # pass M999 through as-is
            else:
                translated = GCODE_TRANSLATIONS[gcode]
                self._logger.debug("Translated: %s -> %s", cmd, translated)
                cmd = translated
                gcode = None  # translated command isn't a motion gcode

        # Motion gating based on activity. The firmware's planner happily
        # queues back-to-back jogs, so `jogging` is a green light (chain
        # freely). During `running_job`, only the file stream's own lines
        # get through — ad-hoc API jogs/gotos are rejected with a UI
        # notification. During `paused` / `alarm` nothing moves.
        if self._is_motion_command(cmd, gcode):
            activity = self._compute_activity()
            blocked = False
            if activity == "running_job":
                blocked = not self._is_file_stream_command(tags)
            elif activity in ("paused", "alarm"):
                blocked = True
            if blocked:
                self._logger.warning(
                    "Motion blocked (activity=%s state=%s): %r",
                    activity, self._grbl_state, cmd,
                )
                try:
                    self._plugin_manager.send_plugin_message(
                        self._identifier,
                        {
                            "type": "motion_blocked",
                            "activity": activity,
                            "state": self._grbl_state,
                            "command": cmd,
                        },
                    )
                except Exception:
                    pass
                return (None,)

        return cmd

    def received_hook(self, comm_instance, line, *args, **kwargs):
        """OctoPrint hook: process lines received from the Carvera.

        Responsibilities:
          1. During file ops: collect response lines and detect terminators.
          2. Parse GRBL status responses (<Idle|MPos:...|WPos:...>).
          3. Extract firmware version from ``version =`` or ``Build version:``
             lines and auto-configure override mode.
          4. Intercept firmware error/alarm lines before OctoPrint's built-in
             handler treats them as fatal (which would kill the connection).

        Returns the line (possibly modified) for OctoPrint's further processing.
        """
        stripped = line.strip()

        # During file operations, collect response lines
        if not self._file_op_event.is_set() and not self._file_op_done.is_set():
            raw = line.encode("latin-1", errors="replace") if isinstance(line, str) else line
            self._logger.debug("File op recv: %r", raw[:80])
            # Check for terminators in raw bytes
            if b"\x04" in raw or b"\x18" in raw or b"\x16" in raw:
                self._file_op_done.set()
                return "ok\n"
            # Also check if the stripped line is empty after a non-empty response
            # (some firmware sends terminator on its own line)
            if stripped and stripped != "ok" and not stripped.startswith("<"):
                self._file_op_lines.append(stripped)
            return "ok\n"  # Fake "ok" so OctoPrint doesn't complain

        status = parse_carvera_status(stripped)
        if not status and stripped.startswith("?"):
            status = parse_carvera_status(stripped.lstrip("?"))
        if status:
            self._parse_grbl_status(status)
            return line

        # Firmware version: "version = 1.0.3" (stock) or "Build version: v1.0.5" (community)
        if stripped.startswith("version = "):
            self._firmware_version = stripped.replace("version = ", "").strip()
            self._detected_firmware_type = "stock"
            self._logger.info("Firmware version: %s (stock)", self._firmware_version)
            self._auto_set_override_mode()
            return line
        if stripped.startswith("Build version:"):
            parts = stripped.split(",")
            self._firmware_version = parts[0].replace("Build version:", "").strip()
            self._detected_firmware_type = "community"
            self._logger.info("Firmware version: %s (community)", self._firmware_version)
            self._auto_set_override_mode()
            return line

        # Intercept error/alarm lines from the firmware BEFORE OctoPrint's
        # built-in handler sees them. OctoPrint's default comm handler
        # matches "Error:" (capital E) and transitions to the Error state,
        # killing the connection. Firmware diagnostic spam like
        # "ERROR: Failed to query STA IP Addr, status: 16688" (WiFi module)
        # would otherwise tear down the whole session even though it's
        # harmless and unrelated to GRBL. We catch case-insensitively.
        stripped_lower = stripped.lower()
        if stripped_lower.startswith("error:") or stripped_lower.startswith("error "):
            self._logger.warning("Firmware error (intercepted): %s", stripped)
            if self._comm_mode and self._comm_mode.name == "binary":
                return "// " + stripped + "\n"
            return "ok\n"
        if stripped_lower.startswith("alarm:"):
            self._logger.warning("GRBL alarm (intercepted): %s", stripped)
            if self._comm_mode and self._comm_mode.name == "binary":
                return "// " + stripped + "\n"
            return "ok\n"

        return line

    # ~~~ Serial factory hook ~~~

    def serial_factory_hook(self, comm_instance, port, baudrate, connection_timeout):
        """Delegate to the active Communication strategy.

        Plain-text mode returns None (OctoPrint opens the port normally);
        binary mode returns a BinaryFrameSerial wrapper.
        """
        if self._comm_mode is None:
            self._rebuild_comm_mode()
        return self._comm_mode.serial_factory(port, baudrate, connection_timeout)

    # ~~~ Connection handling ~~~

    def _on_printer_connected(self):
        self._connected = True
        send_init = self._settings.get_boolean(["send_init_on_connect"])
        self._logger.info(
            "Carvera connected (mode=%s, send_init=%s)",
            self._comm_mode.name if self._comm_mode else "?",
            send_init,
        )
        self._comm_mode.on_connect_init(send_init)
        self._start_keepalive()

        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "connected", "state": self._grbl_state},
        )

    def _on_printer_disconnected(self):
        self._logger.info("Carvera disconnected")
        self._connected = False
        self._grbl_state = "Unknown"
        self._firmware_version = None
        self._detected_firmware_type = None
        self._jog_pending = None
        self._stop_keepalive()

        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "disconnected"},
        )

    # ~~~ Keepalive ~~~

    def _start_keepalive(self):
        if self._keepalive_active:
            return
        self._keepalive_active = True
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()
        self._logger.info("Keepalive started")

    def _stop_keepalive(self):
        self._keepalive_active = False
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=5.0)
            self._keepalive_thread = None

    def _keepalive_loop(self):
        while self._keepalive_active:
            # Wait for file operations to complete before polling
            self._file_op_event.wait(timeout=STATUS_POLL_INTERVAL)
            if self._keepalive_active and self._file_op_event.is_set():
                self._send_realtime(RT_STATUS_QUERY)
                time.sleep(STATUS_POLL_INTERVAL)

    # ~~~ GRBL response parsing ~~~

    def _parse_grbl_status(self, status):
        """Store parsed GRBL status fields and push an update to the frontend.

        Also handles the jog pending buffer: when the machine transitions from
        a moving state back to Idle, any buffered jog fires immediately.
        """
        state = status["state"]
        if state in GRBL_STATES:
            old_state = self._grbl_state
            self._grbl_state = state

            # Drain a pending jog on the Run->Idle transition. We buffer
            # at most one jog at the plugin level (latest wins) so that
            # rapid-fire clicks or knob drags don't pile motion commands
            # into the firmware planner. One jog executes at a time; the
            # next one fires the moment the machine returns to Idle.
            if (
                old_state != "Idle"
                and state == "Idle"
                and self._jog_pending is not None
            ):
                pending = self._jog_pending
                self._jog_pending = None
                try:
                    self._emit_jog(**pending)
                except Exception:
                    self._logger.exception("Failed to emit pending jog")

            if status["machine_pos"] is not None:
                self._machine_pos = status["machine_pos"]
            if status["work_pos"] is not None:
                self._work_pos = status["work_pos"]
            if status["feed"] is not None:
                self._feed = status["feed"]
            if status["spindle"] is not None:
                self._spindle = status["spindle"]
            if status["tool"] is not None:
                self._tool = status["tool"]
            if status["halt_reason"] is not None:
                self._halt_reason = status["halt_reason"]
            if status["wpvoltage"] is not None:
                self._wpvoltage = status["wpvoltage"]
            if status["laser"] is not None:
                self._laser = status["laser"]
            if status["config"] is not None:
                self._config = status["config"]
            if status["playback"] is not None:
                self._playback = status["playback"]
            if status["atc_state"] is not None:
                self._atc_state = status["atc_state"]
            if status["leveling"] is not None:
                self._leveling = status["leveling"]
            if status["rotation"] is not None:
                self._rotation = status["rotation"]
            if status["wcs"] is not None:
                self._wcs = status["wcs"]
            if status["pwm"] is not None:
                self._pwm = status["pwm"]

            self._plugin_manager.send_plugin_message(
                self._identifier,
                {
                    "type": "status",
                    "state": self._grbl_state,
                    "activity": self._compute_activity(),
                    "machine_pos": self._machine_pos,
                    "work_pos": self._work_pos,
                    "feed": self._feed,
                    "spindle": self._spindle,
                    "tool": self._tool,
                    "halt_reason": self._halt_reason,
                    "wpvoltage": self._wpvoltage,
                    "laser": self._laser,
                    "config": self._config,
                    "playback": self._playback,
                    "atc_state": self._atc_state,
                    "leveling": self._leveling,
                    "rotation": self._rotation,
                    "wcs": self._wcs,
                    "pwm": self._pwm,
                    "firmware_version": self._firmware_version,
                    "plugin_version": self._plugin_version,
                    "allowed_actions": list(self._get_allowed_actions()),
                },
            )

            self._publish_mqtt_status()

            activity = self._compute_activity()
            if old_state != state:
                self._logger.info("State: %s -> %s | activity: %s | playback: %s",
                                  old_state, state, activity, self._playback)
        else:
            self._logger.warning("Unknown GRBL state: %s", state)

    # ~~~ MQTT publishing for Home Assistant ~~~

    _MQTT_TOPIC_BASE = "octoPrint/plugin/octocarvera"
    _HA_DISCOVERY_BASE = "homeassistant/sensor"

    @staticmethod
    def _slugify(name):
        """Turn a display name into a safe MQTT topic segment / HA unique_id.

        Keeps alphanumerics and underscores, converts everything else
        (spaces, slashes, dots, accents) to underscores, lowercases the
        result, and collapses runs of underscores. Empty fallbacks to
        "carvera".
        """
        import re
        s = (name or "").strip().lower()
        s = re.sub(r"[^a-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s or "carvera"

    @property
    def _machine_slug(self):
        return self._slugify(self._settings.get(["machine_name"]))

    @property
    def _mqtt_status_topic(self):
        return "{}/{}/status".format(self._MQTT_TOPIC_BASE, self._machine_slug)

    @property
    def _mqtt_heartbeat_topic(self):
        return "{}/{}/heartbeat".format(self._MQTT_TOPIC_BASE, self._machine_slug)

    @property
    def _mqtt_work_mode_topic(self):
        return "{}/{}/work_mode".format(self._MQTT_TOPIC_BASE, self._machine_slug)

    @property
    def _ha_discovery_prefix(self):
        return "{}/octocarvera_{}".format(self._HA_DISCOVERY_BASE, self._machine_slug)
    _MQTT_SENSORS = [
        # (id, name, unit, value_template, device_class)
        ("state", "State", None, "{{ value_json.state }}", None),
        ("wpos_x", "Work Position X", "mm", "{{ value_json.work_pos.x }}", None),
        ("wpos_y", "Work Position Y", "mm", "{{ value_json.work_pos.y }}", None),
        ("wpos_z", "Work Position Z", "mm", "{{ value_json.work_pos.z }}", None),
        ("mpos_x", "Machine Position X", "mm", "{{ value_json.machine_pos.x }}", None),
        ("mpos_y", "Machine Position Y", "mm", "{{ value_json.machine_pos.y }}", None),
        ("mpos_z", "Machine Position Z", "mm", "{{ value_json.machine_pos.z }}", None),
        ("feed_rate", "Feed Rate", "mm/min", "{{ value_json.feed.current }}", None),
        ("feed_override", "Feed Override", "%", "{{ value_json.feed.override }}", None),
        ("spindle_rpm", "Spindle RPM", "RPM", "{{ value_json.spindle.current }}", None),
        ("spindle_override", "Spindle Override", "%", "{{ value_json.spindle.override }}", None),
        ("spindle_temp", "Spindle Temperature", "\u00b0C", "{{ value_json.spindle.spindle_temp }}", "temperature"),
        ("power_temp", "Power Board Temperature", "\u00b0C", "{{ value_json.spindle.power_temp }}", "temperature"),
        ("tool_number", "Tool Number", None, "{{ value_json.tool.number }}", None),
        ("job_progress", "Job Progress", "%", "{{ value_json.playback.percent if value_json.playback else 'unknown' }}", None),
        ("work_mode", "Work Mode", None, "{{ value_json.work_mode }}", None),
    ]

    def _setup_mqtt(self):
        """Grab MQTT publish helper from OctoPrint-MQTT plugin.

        If the helper isn't ready yet (OctoPrint-MQTT loaded after us, or
        broker still connecting), retry a handful of times on a background
        timer. Once the helper is live, start a heartbeat thread so we can
        easily verify publishing end-to-end.
        """
        helpers = self._plugin_manager.get_helpers("mqtt", "mqtt_publish")
        self._logger.info(
            "MQTT setup: helpers=%r keys=%r",
            bool(helpers),
            list(helpers.keys()) if helpers else None,
        )
        if helpers and "mqtt_publish" in helpers:
            self._mqtt_publish = helpers["mqtt_publish"]
            self._mqtt_retry_count = 0
            try:
                self._publish_ha_discovery()
            except Exception:
                self._logger.exception("HA discovery publish failed")
            self._start_mqtt_heartbeat()
            self._logger.info("MQTT publishing enabled (helper resolved)")
            return

        # Retry up to ~15 seconds if MQTT plugin hasn't exposed helpers yet.
        if self._mqtt_retry_count < 10:
            self._mqtt_retry_count += 1
            delay = 1.5
            self._logger.warning(
                "MQTT helper not available yet (attempt %d), retrying in %.1fs",
                self._mqtt_retry_count, delay,
            )
            threading.Timer(delay, self._setup_mqtt).start()
        else:
            self._logger.warning(
                "MQTT publish enabled in settings but OctoPrint-MQTT helper "
                "never resolved after %d retries — giving up. Is the plugin "
                "installed and the broker configured?",
                self._mqtt_retry_count,
            )

    def _start_mqtt_heartbeat(self):
        if self._mqtt_heartbeat_active:
            return
        self._mqtt_heartbeat_active = True
        self._mqtt_heartbeat_thread = threading.Thread(
            target=self._mqtt_heartbeat_loop, daemon=True, name="octocarvera-mqtt-hb"
        )
        self._mqtt_heartbeat_thread.start()
        self._logger.info("MQTT heartbeat thread started")

    def _stop_mqtt_heartbeat(self):
        self._mqtt_heartbeat_active = False
        # Daemon thread — no join needed.

    def _mqtt_heartbeat_loop(self):
        """Publish a small heartbeat every second so we can verify MQTT
        end-to-end independently of machine status polls. Always includes
        a monotonically increasing counter so it's obvious in a subscriber
        log whether messages are flowing."""
        import time
        counter = 0
        while self._mqtt_heartbeat_active:
            counter += 1
            payload = {
                "counter": counter,
                "monotonic": round(time.monotonic(), 3),
                "state": self._grbl_state,
                "connected": bool(self._connected),
                "plugin_version": getattr(self, "_plugin_version", None),
            }
            if self._mqtt_publish:
                try:
                    self._mqtt_publish(
                        self._mqtt_heartbeat_topic,
                        payload,
                        retained=False,
                    )
                    if counter % 10 == 1:
                        self._logger.info(
                            "MQTT heartbeat #%d published: %s", counter, payload
                        )
                except Exception:
                    self._logger.exception("MQTT heartbeat publish failed")
            else:
                if counter % 5 == 1:
                    self._logger.warning(
                        "MQTT heartbeat skipped — _mqtt_publish is None"
                    )
            time.sleep(1.0)

    def _publish_ha_discovery(self):
        """Publish HA MQTT auto-discovery config for all Carvera sensors.

        The display name and topic segment come from the ``machine_name``
        setting (slugified). Changing the setting and re-running discovery
        publishes fresh configs under a new device id, which lets a single
        broker host multiple Carvera machines without collision.
        """
        machine_name = self._settings.get(["machine_name"]) or "Carvera"
        slug = self._machine_slug
        device = {
            "identifiers": ["octocarvera_{}".format(slug)],
            "name": machine_name,
            "manufacturer": "Makera",
            "model": "Carvera Air",
            "sw_version": self._plugin_version,
        }
        # Availability topic needs to match OctoPrint-MQTT's actual LWT.
        # The MQTT plugin publishes "connected"/"disconnected" to
        # <baseTopic><lwTopic>, both of which are user-configurable. Read
        # them from OctoPrint's global settings so the availability topic
        # HA subscribes to actually matches what the MQTT plugin publishes
        # to — otherwise HA marks everything "unavailable" and our sensor
        # values never show even though /status is flowing fine.
        base_topic = self._settings.global_get(["plugins", "mqtt", "publish", "baseTopic"]) or "octoPrint/"
        lw_topic = self._settings.global_get(["plugins", "mqtt", "publish", "lwTopic"]) or "mqtt"
        availability_topic = base_topic + lw_topic
        self._logger.info(
            "HA discovery availability_topic=%r (baseTopic=%r + lwTopic=%r)",
            availability_topic, base_topic, lw_topic,
        )
        availability = [{"topic": availability_topic,
                         "payload_available": "connected",
                         "payload_not_available": "disconnected"}]

        status_topic = self._mqtt_status_topic
        discovery_prefix = self._ha_discovery_prefix
        for sensor_id, name, unit, template, dev_class in self._MQTT_SENSORS:
            config = {
                "name": name,
                "unique_id": "octocarvera_{}_{}".format(slug, sensor_id),
                "state_topic": status_topic,
                "value_template": template,
                "device": device,
                "availability": availability,
            }
            if unit:
                config["unit_of_measurement"] = unit
            if dev_class:
                config["device_class"] = dev_class
            self._mqtt_publish(
                "{}/{}/config".format(discovery_prefix, sensor_id),
                config, retained=True,
            )
        self._logger.info(
            "Published HA discovery config for %d sensors under slug=%r status_topic=%r",
            len(self._MQTT_SENSORS), slug, status_topic,
        )

    _mqtt_last_publish = 0

    def _publish_mqtt_status(self):
        """Publish current machine status to MQTT (throttled to 1/sec)."""
        if not self._mqtt_publish:
            return
        import time
        now = time.monotonic()
        if now - self._mqtt_last_publish < 1.0:
            return
        self._mqtt_last_publish = now
        work_mode = self._compute_work_mode()
        try:
            self._mqtt_publish(self._mqtt_status_topic, {
                "state": self._grbl_state,
                "activity": self._compute_activity(),
                "work_mode": work_mode,
                "machine_pos": self._machine_pos,
                "work_pos": self._work_pos,
                "feed": self._feed,
                "spindle": self._spindle,
                "tool": self._tool,
                "halt_reason": self._halt_reason,
                "wpvoltage": self._wpvoltage,
                "laser": self._laser,
                "config": self._config,
                "playback": self._playback,
                "firmware_version": self._firmware_version,
            }, retained=True)
            # Dedicated work_mode topic for HA automations
            self._mqtt_publish(self._mqtt_work_mode_topic, {
                "mode": work_mode,
                "tool": self._tool.get("number", 0) if self._tool else 0,
            }, retained=True)
        except Exception:
            self._logger.exception("MQTT status publish failed")

    # ~~~ Command sending ~~~

    def _send_command(self, cmd):
        if self._printer and self._printer.is_operational():
            self._printer.commands(cmd)
        else:
            self._logger.warning("Cannot send command - printer not operational")

    def _send_realtime(self, byte_val):
        if not self._printer or not self._printer.is_operational():
            self._logger.warning("Cannot send realtime command - not operational")
            return
        try:
            comm = self._printer._comm
            if comm and hasattr(comm, "_serial") and comm._serial:
                comm._serial.write(byte_val)
            else:
                self._printer.commands(byte_val.decode("latin-1"))
        except Exception:
            self._logger.exception("Failed to send realtime command")

    def _send_raw_text(self, cmd):
        """Write a text command directly to the wrapped serial, bypassing
        OctoPrint's command queue.

        OctoPrint intercepts some commands at the comm layer (notably
        ``M112``, which it treats as a kill-switch that closes the port).
        For commands we need the firmware to actually receive unmolested,
        we bypass the queue entirely. In binary mode the write still
        passes through ``BinaryFrameSerial.write`` so framing is applied;
        in plain mode it goes straight to the raw serial.
        """
        if not self._printer or not self._printer.is_operational():
            self._logger.warning("Cannot send raw text - not operational")
            return
        try:
            comm = self._printer._comm
            if comm and hasattr(comm, "_serial") and comm._serial:
                payload = (cmd.strip() + "\n").encode("latin-1")
                comm._serial.write(payload)
            else:
                self._printer.commands(cmd)
        except Exception:
            self._logger.exception("Failed to send raw text command")

    def _cancel_job(self):
        self._comm_mode.cancel()
        if self._printer:
            self._printer.cancel_print()
        threading.Timer(0.5, self._post_cancel_cleanup).start()

    def _post_cancel_cleanup(self):
        self._comm_mode.post_cancel_cleanup()

    def _emit_jog(self, dx, dy, dz, feed):
        """Actually send a jog command to the firmware.

        Uses G91 (relative) so the firmware's motion planner computes
        the target from its own authoritative live position, not our
        ~300ms-stale ``_work_pos``. We follow with a G90 restore so the
        rest of the system (goto, G-code scripts) keeps its usual
        absolute-coordinate assumption.
        """
        parts = []
        if dx != 0:
            parts.append(f"X{dx:.3f}")
        if dy != 0:
            parts.append(f"Y{dy:.3f}")
        if dz != 0:
            parts.append(f"Z{dz:.3f}")
        if not parts:
            return
        offsets = " ".join(parts)
        if feed:
            cmd = f"G91 G1 {offsets} F{int(feed)}"
        else:
            cmd = f"G91 G0 {offsets}"
        self._send_command(cmd)
        self._send_command("G90")

    def _handle_jog(self, data):
        """Dispatch a jog, honouring the single-slot pending buffer.

        If the machine is Idle and nothing is pending, send immediately.
        Otherwise store the jog in the latest-wins pending slot; it will
        fire on the next Run->Idle transition (see ``_parse_grbl_status``).
        This keeps the firmware planner from ever holding more than one
        ad-hoc move, so the queue can't pile up during rapid clicks / knob
        drags.
        """
        import flask
        jog = {
            "dx": float(data.get("x", 0)),
            "dy": float(data.get("y", 0)),
            "dz": float(data.get("z", 0)),
            "feed": data.get("feed", None),
        }
        if not (jog["dx"] or jog["dy"] or jog["dz"]):
            return flask.jsonify(ok=True)

        # Idle and nothing already pending → direct send.
        if self._grbl_state == "Idle" and self._jog_pending is None:
            self._emit_jog(**jog)
            return flask.jsonify(ok=True, queued=False)

        # Otherwise latest-wins buffering. Overwrites any prior pending
        # so the most recent user intent is what actually happens when
        # the machine returns to Idle.
        self._jog_pending = jog
        return flask.jsonify(ok=True, queued=True)

    def _handle_goto(self, data):
        """Go to absolute coordinates. Only moves axes that are provided."""
        import flask
        parts = []
        if "x" in data:
            parts.append(f"X{float(data['x']):.3f}")
        if "y" in data:
            parts.append(f"Y{float(data['y']):.3f}")
        if "z" in data:
            parts.append(f"Z{float(data['z']):.3f}")
        if parts:
            cmd = "G0 G90 " + " ".join(parts)
            self._send_command(cmd)
        return flask.jsonify(ok=True)

    def _get_serial(self):
        """Get the raw pyserial port for direct I/O (file operations)."""
        if not self._printer or not self._printer.is_operational():
            return None
        comm = self._printer._comm
        if comm and hasattr(comm, "_serial") and comm._serial:
            return comm._serial
        return None

    def _handle_list_files(self, data):
        """List files on the Carvera's SD card via the ``ls -e -s`` shell command.

        Pauses status polling, sends the command over raw serial, collects
        response lines via received_hook, then parses them into file entries.
        """
        import flask
        from .carvera_files import encode_path, parse_ls_response

        if not self._printer or not self._printer.is_operational():
            return flask.jsonify(ok=False, error="Not connected"), 503

        if not self._file_op_event.is_set():
            return flask.jsonify(ok=False, error="File operation in progress"), 409

        path = data.get("path", "/sd/gcodes")
        ls_path = path.rstrip("/")
        encoded = encode_path(ls_path)
        cmd = "ls -e -s {}".format(encoded)

        # Set up line collector
        self._file_op_lines = []
        self._file_op_done.clear()
        self._file_op_event.clear()

        try:
            # Send ls command via raw serial (bypasses sending_gcode_hook which suppresses during file ops)
            serial = self._get_serial()
            if serial:
                serial.write((cmd + "\n").encode("latin-1"))
            else:
                self._file_op_event.set()
                return flask.jsonify(ok=False, error="Serial not available"), 503

            # Wait for response collection (received_hook captures lines)
            if not self._file_op_done.wait(timeout=2.0):
                self._logger.debug("File listing completed by timeout for %s", path)
                # Still return whatever we got
                pass

            entries = parse_ls_response(self._file_op_lines)
            return flask.jsonify(ok=True, files=entries, path=path)
        except Exception as e:
            self._logger.exception("File listing failed")
            return flask.jsonify(ok=False, error=str(e)), 500
        finally:
            self._file_op_event.set()

    def _handle_delete_file(self, data):
        """Delete a file or folder on the Carvera's SD card via ``rm``."""
        import flask
        from .carvera_files import encode_path

        if not self._printer or not self._printer.is_operational():
            return flask.jsonify(ok=False, error="Not connected"), 503

        if not self._file_op_event.is_set():
            return flask.jsonify(ok=False, error="File operation in progress"), 409

        path = data.get("path", "")
        if not path:
            return flask.jsonify(ok=False, error="No path specified"), 400

        encoded = encode_path(path)
        cmd = "rm {} -e".format(encoded)

        self._file_op_lines = []
        self._file_op_done.clear()
        self._file_op_event.clear()

        try:
            serial = self._get_serial()
            if serial:
                serial.write((cmd + "\n").encode("latin-1"))
            else:
                self._file_op_event.set()
                return flask.jsonify(ok=False, error="Serial not available"), 503

            self._file_op_done.wait(timeout=2.0)
            self._logger.info("Deleted: %s", path)
            return flask.jsonify(ok=True)
        except Exception as e:
            self._logger.exception("Delete failed")
            return flask.jsonify(ok=False, error=str(e)), 500
        finally:
            self._file_op_event.set()

    def _handle_create_folder(self, data):
        """Create a folder on the Carvera's SD card via ``mkdir -p``."""
        import flask
        from .carvera_files import encode_path

        if not self._printer or not self._printer.is_operational():
            return flask.jsonify(ok=False, error="Not connected"), 503

        if not self._file_op_event.is_set():
            return flask.jsonify(ok=False, error="File operation in progress"), 409

        path = data.get("path", "")
        if not path:
            return flask.jsonify(ok=False, error="No path specified"), 400

        encoded = encode_path(path)
        cmd = "mkdir -p {}".format(encoded)

        self._file_op_lines = []
        self._file_op_done.clear()
        self._file_op_event.clear()

        try:
            serial = self._get_serial()
            if serial:
                serial.write((cmd + "\n").encode("latin-1"))
            else:
                self._file_op_event.set()
                return flask.jsonify(ok=False, error="Serial not available"), 503

            self._file_op_done.wait(timeout=2.0)
            self._logger.info("Created folder: %s", path)
            return flask.jsonify(ok=True)
        except Exception as e:
            self._logger.exception("Create folder failed")
            return flask.jsonify(ok=False, error=str(e)), 500
        finally:
            self._file_op_event.set()

    def _handle_move_file(self, data):
        """Rename or move a file on the Carvera SD card.

        Uses Smoothieware's built-in ``mv`` shell command, which works
        cross-directory on FAT32 and honors the ``-e`` extended-output
        flag like the other file commands. Works in both plain-text and
        binary protocol modes — the command goes through the same
        framed-shell path as ``ls``/``rm``/``mkdir``.
        """
        import flask
        from .carvera_files import encode_path

        if not self._printer or not self._printer.is_operational():
            return flask.jsonify(ok=False, error="Not connected"), 503

        if not self._file_op_event.is_set():
            return flask.jsonify(ok=False, error="File operation in progress"), 409

        src = (data.get("src") or "").strip()
        dst = (data.get("dst") or "").strip()
        if not src or not dst:
            return flask.jsonify(ok=False, error="Both src and dst are required"), 400
        if src == dst:
            return flask.jsonify(ok=False, error="src and dst must differ"), 400

        cmd = "mv {} {} -e".format(encode_path(src), encode_path(dst))

        self._file_op_lines = []
        self._file_op_done.clear()
        self._file_op_event.clear()

        try:
            serial = self._get_serial()
            if serial:
                serial.write((cmd + "\n").encode("latin-1"))
            else:
                self._file_op_event.set()
                return flask.jsonify(ok=False, error="Serial not available"), 503

            self._file_op_done.wait(timeout=3.0)
            # Check the collected lines for a firmware error. Smoothieware
            # replies "Could not rename X to Y" when the source is missing
            # or the destination is invalid — surface that verbatim.
            for line in self._file_op_lines:
                if "could not rename" in line.lower() or line.lower().startswith("error"):
                    self._logger.warning("Move failed: %s", line)
                    return flask.jsonify(ok=False, error=line), 400
            self._logger.info("Moved: %s -> %s", src, dst)
            return flask.jsonify(ok=True)
        except Exception as e:
            self._logger.exception("Move failed")
            return flask.jsonify(ok=False, error=str(e)), 500
        finally:
            self._file_op_event.set()

    def _handle_upload_to_carvera(self, data):
        """Start an XMODEM upload to the Carvera's SD card in a background thread.

        Disconnects OctoPrint from serial (XMODEM needs exclusive port access),
        transfers the file, then reconnects. Progress is pushed to the frontend
        via plugin messages. Also used by firmware flash (remote_path = /sd/firmware.tmp).
        """
        import flask

        if not self._printer or not self._printer.is_operational():
            return flask.jsonify(ok=False, error="Not connected"), 503

        if not self._file_op_event.is_set():
            return flask.jsonify(ok=False, error="File operation in progress"), 409

        filename = data.get("filename", "").strip()
        if not filename:
            return flask.jsonify(ok=False, error="No filename"), 400

        # Read file from OctoPrint's uploads folder
        uploads = self._settings.global_get_basefolder("uploads")
        local_path = os.path.join(uploads, filename)
        if not os.path.isfile(local_path):
            return flask.jsonify(ok=False, error="File not found: {}".format(filename)), 404

        remote_path = data.get("remote_path", "/sd/{}".format(filename))

        # Get serial port path from OctoPrint's current connection
        port = None
        try:
            conn = self._printer.get_current_connection()
            if conn and len(conn) >= 2:
                port = conn[1]
        except Exception:
            pass
        if not port:
            port = self._settings.get(["serial_port"])

        # Start upload in background thread
        thread = threading.Thread(
            target=self._do_upload,
            args=(port, local_path, remote_path, filename),
            daemon=True,
        )
        thread.start()
        return flask.jsonify(ok=True, message="Upload started")

    def _do_upload(self, port, local_path, remote_path, filename):
        """Background thread: disconnect, XMODEM transfer, reconnect.

        Sequence: disconnect OctoPrint -> open serial directly -> toggle DTR
        to reset FTDI state -> clear buffer -> XMODEM send -> close serial ->
        reconnect OctoPrint. If this is a firmware flash (_flash_after_upload),
        the staging file is renamed to firmware.bin on success.
        """
        import serial as pyserial

        self._file_op_event.clear()
        self._upload_cancel.clear()
        try:
            with open(local_path, "rb") as f:
                file_data = f.read()

            total = len(file_data)
            self._logger.info("Uploading %s (%d bytes) to %s via %s", filename, total, remote_path, port)

            upload_start = time.monotonic()

            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "upload_progress", "filename": filename, "percent": 0,
                 "bytes_sent": 0, "total_bytes": total, "elapsed_secs": 0},
            )

            # Disconnect OctoPrint from serial so we get exclusive access
            self._logger.info("Disconnecting OctoPrint for XMODEM transfer")
            self._stop_keepalive()
            self._printer.disconnect()
            time.sleep(1.0)  # Wait for port to be released

            # Open serial port directly (match Carvera Controller init)
            ser = pyserial.Serial(port, 115200, timeout=0.3)
            try:
                # Toggle DTR to reset FTDI state (same as Carvera Controller)
                try:
                    ser.setDTR(0)
                except IOError:
                    pass
                time.sleep(0.5)
                ser.flushInput()
                try:
                    ser.setDTR(1)
                except IOError:
                    pass
                time.sleep(0.5)

                # Clear machine buffer (same as Carvera Controller)
                ser.write(b"\n;\n")
                time.sleep(0.5)
                if ser.in_waiting:
                    ser.read(ser.in_waiting)

                def progress_callback(bytes_sent, total_bytes):
                    pct = int(bytes_sent * 100 / total_bytes) if total_bytes > 0 else 100
                    elapsed = time.monotonic() - upload_start
                    self._plugin_manager.send_plugin_message(
                        self._identifier,
                        {"type": "upload_progress", "filename": filename, "percent": pct,
                         "bytes_sent": bytes_sent, "total_bytes": total_bytes,
                         "elapsed_secs": round(elapsed, 1)},
                    )

                upload_result = upload_file(ser, remote_path, file_data, progress_callback=progress_callback, cancel_event=self._upload_cancel)
            finally:
                ser.close()

            # Reconnect OctoPrint
            self._logger.info("Reconnecting OctoPrint after transfer")
            time.sleep(0.5)
            self._printer.connect(port=port, baudrate=115200, profile="_carvera_air")
            time.sleep(3.0)  # Wait for connection to stabilize

            # Send result AFTER reconnect so auto-refresh works
            if upload_result is True:
                self._plugin_manager.send_plugin_message(
                    self._identifier,
                    {"type": "upload_complete", "filename": filename},
                )
                if self._flash_after_upload:
                    # Atomic firmware staging: rename firmware.tmp → firmware.bin
                    # now that the upload is verified complete. If the rename
                    # fails the staging file stays — harmless (bootloader only
                    # looks for firmware.bin). DO NOT auto-reboot: emit
                    # firmware_staged so the frontend shows the post-upload
                    # choice panel (reboot / keep / delete).
                    self._logger.info("Firmware upload complete, renaming staging file")
                    try:
                        serial = self._get_serial()
                        if serial:
                            from .carvera_files import encode_path
                            mv_cmd = "mv {} {} -e\n".format(
                                encode_path("/sd/firmware.tmp"),
                                encode_path("/sd/firmware.bin"),
                            )
                            serial.write(mv_cmd.encode("latin-1"))
                            time.sleep(0.5)
                        self._plugin_manager.send_plugin_message(
                            self._identifier,
                            {"type": "firmware_staged", "filename": filename},
                        )
                    except Exception:
                        self._logger.exception("Staging rename failed")
                        self._plugin_manager.send_plugin_message(
                            self._identifier,
                            {"type": "upload_error", "filename": filename,
                             "error": "Upload OK but rename to firmware.bin failed"},
                        )
            elif upload_result is None:
                self._logger.info("Upload cancelled: %s", filename)
                self._plugin_manager.send_plugin_message(
                    self._identifier,
                    {"type": "upload_error", "filename": filename, "error": "Cancelled"},
                )
            else:
                self._plugin_manager.send_plugin_message(
                    self._identifier,
                    {"type": "upload_error", "filename": filename, "error": "Transfer failed"},
                )

        except Exception as e:
            self._logger.exception("Upload failed: %s", filename)
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "upload_error", "filename": filename, "error": str(e)},
            )
            self._cleanup_firmware_staging()
            # Try to reconnect even on error
            try:
                self._printer.connect(port=port, baudrate=115200, profile="_carvera_air")
            except Exception:
                pass
        finally:
            self._file_op_event.set()
            self._flash_after_upload = False

    def _auto_set_override_mode(self):
        """Auto-switch override mode based on detected firmware type."""
        mode = self._settings.get(["override_mode"])
        if mode != "auto":
            return
        if self._detected_firmware_type:
            self._logger.info("Auto-detected override mode: %s", self._detected_firmware_type)

    def _get_effective_override_mode(self):
        """Return 'stock' or 'community' — resolves 'auto' using detected firmware."""
        mode = self._settings.get(["override_mode"])
        if mode == "auto":
            return self._detected_firmware_type or "stock"
        return mode

    def _send_override(self, kind, value):
        """Send a feed or spindle override. Stock uses M220/M223; community uses $F/$O."""
        mode = self._get_effective_override_mode()
        if kind == "feed":
            if mode == "community":
                self._send_command(f"$F S{value}")
            else:
                self._send_command(f"M220 S{value}")
        elif kind == "spindle":
            if mode == "community":
                self._send_command(f"$O S{value}")
            else:
                self._send_command(f"M223 S{value}")

    # ~~~ Software update hook ~~~

    def get_extension_tree(self, *args, **kwargs):
        """Register extensions OctoPrint accepts for upload.

        `cnc` covers the usual G-code file types. `firmware` adds `.bin`
        so a community firmware build can sit in OctoPrint's local files
        and be pushed to the Carvera's SD card as `firmware.bin`;
        Smoothieware's bootloader picks it up on the next reboot and
        flashes itself.
        """
        return {
            "machinecode": {
                "cnc": ["nc", "tap", "cnc", "ngc", "ncc"],
                "firmware": ["bin"],
            }
        }

    def get_update_information(self):
        return {
            "octocarvera": {
                "displayName": "OctoCarvera",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "JorgenSteen",
                "repo": "OctoCarvera",
                "current": self._plugin_version,
                "pip": "https://github.com/JorgenSteen/OctoCarvera/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "OctoCarvera"
__plugin_pythoncompat__ = ">=3,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = OctoCarveraPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.sending": __plugin_implementation__.sending_gcode_hook,
        "octoprint.comm.protocol.gcode.received": (__plugin_implementation__.received_hook, 1),
        "octoprint.filemanager.extension_tree": __plugin_implementation__.get_extension_tree,
        "octoprint.comm.transport.serial.factory": __plugin_implementation__.serial_factory_hook,
    }
