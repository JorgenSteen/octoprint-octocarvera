# coding=utf-8
"""Tests for OctoCarveraPlugin methods using mocked OctoPrint internals."""

import re
import time
from unittest.mock import MagicMock, call, patch

import pytest

from octocarvera import OctoCarveraPlugin
from octocarvera.carvera_protocol import parse_carvera_status


# Real Carvera status line for testing
REAL_STATUS = (
    "<Idle|MPos:-278.1950,-192.0350,-3.0000,-86.2000,0.0000"
    "|WPos:0.0000,0.0000,54.0359,0.0000,0.0000"
    "|F:0.0,3000.0,100.0"
    "|S:0.0,10000.0,100.0,0,17.2,19.8"
    "|T:3,-16.281,-1"
    "|W:0.00"
    "|L:0, 0, 0, 0.0,100.0"
    "|H:1"
    "|C:2,1,0,0>"
)


@pytest.fixture
def plugin():
    """Create a plugin instance with mocked OctoPrint internals."""
    p = OctoCarveraPlugin()
    p._logger = MagicMock()
    p._settings = MagicMock()
    p._settings.get_float.return_value = 1.0
    p._settings.get.return_value = "community"
    p._settings.get_boolean.return_value = True
    p._plugin_manager = MagicMock()
    p._printer = MagicMock()
    p._printer.is_operational.return_value = True
    p._identifier = "octocarvera"
    p._plugin_version = "0.3.0"
    p._printer_profile_manager = MagicMock()
    p._printer_profile_manager.exists.return_value = True
    return p


class TestParseGrblStatus:
    def test_updates_state(self, plugin):
        status = parse_carvera_status("<Idle|MPos:1.0,2.0,3.0>")
        plugin._parse_grbl_status(status)
        assert plugin._grbl_state == "Idle"

    def test_updates_machine_pos_5_axis(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        assert plugin._machine_pos["x"] == pytest.approx(-278.195)
        assert plugin._machine_pos["a"] == pytest.approx(-86.2)
        assert plugin._machine_pos["b"] == pytest.approx(0.0)

    def test_updates_work_pos_5_axis(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        assert plugin._work_pos["z"] == pytest.approx(54.0359)
        assert plugin._work_pos["a"] == pytest.approx(0.0)

    def test_updates_feed(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        assert plugin._feed["current"] == pytest.approx(0.0)
        assert plugin._feed["max"] == pytest.approx(3000.0)
        assert plugin._feed["override"] == pytest.approx(100.0)

    def test_updates_spindle(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        assert plugin._spindle["max"] == pytest.approx(10000.0)
        assert plugin._spindle["vacuum_mode"] == 0
        assert plugin._spindle["spindle_temp"] == pytest.approx(17.2)

    def test_updates_tool(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        assert plugin._tool["number"] == 3
        assert plugin._tool["offset"] == pytest.approx(-16.281)

    def test_updates_halt_reason(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        assert plugin._halt_reason == 1

    def test_sends_plugin_message_with_all_fields(self, plugin):
        status = parse_carvera_status(REAL_STATUS)
        plugin._parse_grbl_status(status)
        plugin._plugin_manager.send_plugin_message.assert_called_once()
        msg = plugin._plugin_manager.send_plugin_message.call_args[0][1]
        assert msg["type"] == "status"
        assert msg["state"] == "Idle"
        assert "a" in msg["machine_pos"]
        assert "feed" in msg
        assert "spindle" in msg
        assert "tool" in msg
        assert "halt_reason" in msg

    def test_logs_state_change(self, plugin):
        plugin._grbl_state = "Idle"
        status = parse_carvera_status("<Run|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        plugin._logger.info.assert_called()

    def test_no_log_on_same_state(self, plugin):
        plugin._grbl_state = "Idle"
        status = parse_carvera_status("<Idle|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        plugin._logger.info.assert_not_called()

    def test_warns_on_unknown_state(self, plugin):
        status = parse_carvera_status("<Bogus|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        plugin._logger.warning.assert_called()
        assert plugin._grbl_state != "Bogus"

    def test_carvera_pause_state(self, plugin):
        status = parse_carvera_status("<Pause|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        assert plugin._grbl_state == "Pause"

    def test_carvera_tool_state(self, plugin):
        status = parse_carvera_status("<Tool|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        assert plugin._grbl_state == "Tool"

    def test_missing_fields_keep_previous(self, plugin):
        """Fields not in the status line should not reset stored values."""
        plugin._feed = {"current": 500.0, "max": 3000.0, "override": 80.0}
        status = parse_carvera_status("<Idle|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        assert plugin._feed["current"] == pytest.approx(500.0)


class TestSendCommand:
    def test_sends_when_operational(self, plugin):
        plugin._send_command("G0 X10")
        plugin._printer.commands.assert_called_with("G0 X10")

    def test_warns_when_not_operational(self, plugin):
        plugin._printer.is_operational.return_value = False
        plugin._send_command("G0 X10")
        plugin._printer.commands.assert_not_called()
        plugin._logger.warning.assert_called()


class TestReceivedHook:
    def test_returns_line(self, plugin):
        result = plugin.received_hook(MagicMock(), "ok\n")
        assert result == "ok\n"

    def test_parses_status_response(self, plugin):
        result = plugin.received_hook(MagicMock(), "<Idle|MPos:0.0,0.0,0.0>\n")
        assert result == "<Idle|MPos:0.0,0.0,0.0>\n"
        assert plugin._grbl_state == "Idle"

    def test_parses_full_carvera_status(self, plugin):
        result = plugin.received_hook(MagicMock(), REAL_STATUS + "\n")
        assert result == REAL_STATUS + "\n"
        assert plugin._grbl_state == "Idle"
        assert plugin._tool["number"] == 3

    def test_logs_error_response(self, plugin):
        plugin.received_hook(MagicMock(), "error:1\n")
        plugin._logger.warning.assert_called()

    def test_logs_alarm_response(self, plugin):
        plugin.received_hook(MagicMock(), "ALARM:1\n")
        plugin._logger.warning.assert_called()


class TestConnectionEvents:
    def test_connected_sets_flag(self, plugin):
        plugin._on_printer_connected()
        assert plugin._connected is True

    def test_connected_sends_init(self, plugin):
        plugin._on_printer_connected()
        plugin._printer.commands.assert_called()
        plugin._stop_keepalive()

    def test_connected_skips_init_when_disabled(self, plugin):
        plugin._settings.get_boolean.return_value = False
        plugin._settings.get_float.return_value = 99.0  # Long interval so keepalive doesn't fire
        plugin._on_printer_connected()
        # Only the keepalive ? should be queued, not the init sequence
        calls = [str(c) for c in plugin._printer.commands.call_args_list]
        assert not any("\\n;\\n" in c for c in calls)
        plugin._stop_keepalive()

    def test_connected_starts_keepalive(self, plugin):
        plugin._on_printer_connected()
        assert plugin._keepalive_active is True
        plugin._stop_keepalive()

    def test_disconnected_clears_state(self, plugin):
        plugin._connected = True
        plugin._grbl_state = "Idle"
        plugin._keepalive_active = True
        plugin._on_printer_disconnected()
        assert plugin._connected is False
        assert plugin._grbl_state == "Unknown"
        assert plugin._keepalive_active is False

    def test_disconnected_sends_message(self, plugin):
        plugin._on_printer_disconnected()
        plugin._plugin_manager.send_plugin_message.assert_called_once()
        msg = plugin._plugin_manager.send_plugin_message.call_args[0][1]
        assert msg["type"] == "disconnected"


class TestStartup:
    """Test on_after_startup handles already-connected state."""

    def test_startup_starts_keepalive_if_connected(self, plugin):
        plugin._settings.global_get.return_value = []
        plugin._printer.is_operational.return_value = True
        plugin.on_after_startup()
        assert plugin._connected is True
        assert plugin._keepalive_active is True
        plugin._stop_keepalive()

    def test_startup_skips_keepalive_if_disconnected(self, plugin):
        plugin._settings.global_get.return_value = []
        plugin._printer.is_operational.return_value = False
        plugin.on_after_startup()
        assert plugin._connected is False
        assert plugin._keepalive_active is False


class TestSettings:
    def test_defaults(self, plugin):
        defaults = plugin.get_settings_defaults()
        assert defaults["baud_rate"] == 115200
        assert defaults["send_init_on_connect"] is True
        assert defaults["serial_port"] == "/dev/ttyUSB0"
        assert defaults["override_mode"] == "auto"
        assert defaults["protocol_mode"] == "plain_text"

    def test_assets(self, plugin):
        assets = plugin.get_assets()
        assert "js/octocarvera.js" in assets["js"]
        assert "css/octocarvera.css" in assets["css"]


class TestApiCommands:
    def test_has_status_command(self, plugin):
        commands = plugin.get_api_commands()
        assert "status" in commands
        assert "send_command" in commands

    def test_api_is_protected(self, plugin):
        assert plugin.is_api_protected() is True


class TestJobControl:
    """Test job control commands (pause/resume/cancel/estop/overrides)."""

    def test_pause_sends_feed_hold(self, plugin):
        plugin._printer._comm = MagicMock()
        plugin._printer._comm._serial = MagicMock()
        plugin._send_realtime(b"!")
        plugin._printer._comm._serial.write.assert_called_with(b"!")

    def test_resume_sends_cycle_start(self, plugin):
        plugin._printer._comm = MagicMock()
        plugin._printer._comm._serial = MagicMock()
        plugin._send_realtime(b"~")
        plugin._printer._comm._serial.write.assert_called_with(b"~")

    def test_estop_sends_reset(self, plugin):
        plugin._printer._comm = MagicMock()
        plugin._printer._comm._serial = MagicMock()
        plugin._send_realtime(b"\x18")
        plugin._printer._comm._serial.write.assert_called_with(b"\x18")

    def test_realtime_warns_when_not_operational(self, plugin):
        plugin._printer.is_operational.return_value = False
        plugin._send_realtime(b"!")
        plugin._logger.warning.assert_called()

    def test_realtime_fallback_when_no_serial(self, plugin):
        plugin._printer._comm = MagicMock(spec=[])  # No _serial attr
        plugin._send_realtime(b"!")
        plugin._printer.commands.assert_called_with("!")

    def test_cancel_job_sends_reset_and_cancels(self, plugin):
        plugin._printer._comm = MagicMock()
        plugin._printer._comm._serial = MagicMock()
        plugin._cancel_job()
        plugin._printer._comm._serial.write.assert_called_with(b"\x18")
        plugin._printer.cancel_print.assert_called_once()

    def test_feed_override_community_mode(self, plugin):
        plugin._settings.get.return_value = "community"
        plugin._send_override("feed", 150)
        plugin._printer.commands.assert_called_with("$F S150")

    def test_feed_override_stock_mode(self, plugin):
        plugin._settings.get.return_value = "stock"
        plugin._send_override("feed", 80)
        plugin._printer.commands.assert_called_with("M220 S80")

    def test_spindle_override_community_mode(self, plugin):
        plugin._settings.get.return_value = "community"
        plugin._send_override("spindle", 120)
        plugin._printer.commands.assert_called_with("$O S120")

    def test_spindle_override_stock_mode(self, plugin):
        plugin._settings.get.return_value = "stock"
        plugin._send_override("spindle", 90)
        plugin._printer.commands.assert_called_with("M223 S90")


class TestStateGating:
    """Test that commands are gated based on machine state."""

    def test_idle_allows_all_commands(self, plugin):
        plugin._grbl_state = "Idle"
        allowed = plugin._get_allowed_actions()
        assert "send_command" in allowed
        assert "job_pause" in allowed
        assert "job_cancel" in allowed
        assert "estop" in allowed
        assert "feed_override" in allowed
        assert "spindle_override" in allowed

    def test_run_allows_pause_and_overrides(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "job_pause" in allowed
        assert "job_cancel" in allowed
        assert "feed_override" in allowed
        assert "spindle_override" in allowed
        assert "estop" in allowed

    def test_run_blocks_send_command(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "send_command" not in allowed

    def test_hold_allows_resume_and_cancel(self, plugin):
        plugin._grbl_state = "Hold"
        allowed = plugin._get_allowed_actions()
        assert "job_resume" in allowed
        assert "job_cancel" in allowed
        assert "estop" in allowed

    def test_hold_blocks_pause_and_overrides(self, plugin):
        plugin._grbl_state = "Hold"
        allowed = plugin._get_allowed_actions()
        assert "job_pause" not in allowed
        assert "feed_override" not in allowed

    def test_pause_state_allows_resume(self, plugin):
        plugin._grbl_state = "Pause"
        allowed = plugin._get_allowed_actions()
        assert "job_resume" in allowed
        assert "job_cancel" in allowed

    def test_alarm_allows_send_command(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "send_command" in allowed
        assert "estop" in allowed

    def test_alarm_blocks_job_control(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "job_pause" not in allowed
        assert "job_resume" not in allowed
        assert "feed_override" not in allowed

    def test_unknown_state_allows_only_estop(self, plugin):
        plugin._grbl_state = "Bogus"
        allowed = plugin._get_allowed_actions()
        assert "estop" in allowed
        assert "status" in allowed
        assert "job_pause" not in allowed
        assert "send_command" not in allowed

    def test_allowed_actions_in_status_message(self, plugin):
        status = parse_carvera_status("<Run|MPos:0.0,0.0,0.0>")
        plugin._parse_grbl_status(status)
        msg = plugin._plugin_manager.send_plugin_message.call_args[0][1]
        assert "allowed_actions" in msg
        assert "job_pause" in msg["allowed_actions"]

    def test_api_has_new_commands(self, plugin):
        commands = plugin.get_api_commands()
        for cmd in ["job_pause", "job_resume", "job_cancel", "estop", "feed_override", "spindle_override"]:
            assert cmd in commands, f"{cmd} should be in API commands"


class TestUnlock:
    """Test unlock ($X) command for clearing Alarm state."""

    def test_unlock_api_command_exists(self, plugin):
        commands = plugin.get_api_commands()
        assert "unlock" in commands

    def test_unlock_sends_dollar_x(self, plugin):
        plugin._grbl_state = "Alarm"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("unlock", {})
        plugin._printer.commands.assert_called_with("$X")

    def test_alarm_allows_unlock(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "unlock" in allowed

    def test_idle_allows_unlock(self, plugin):
        """Unlock is harmless in Idle state and convenient after false alarms."""
        plugin._grbl_state = "Idle"
        allowed = plugin._get_allowed_actions()
        assert "unlock" in allowed


class TestGcodeTranslation:
    """Test that 3D-printer M-codes are translated or suppressed for GRBL."""

    def test_m105_becomes_status_query(self, plugin):
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "M105", None, "M105")
        assert result == "?"

    def test_m114_becomes_status_query(self, plugin):
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "M114", None, "M114")
        assert result == "?"

    def test_m115_becomes_settings_dump(self, plugin):
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "M115", None, "M115")
        assert result == "$$"

    def test_m400_becomes_dwell(self, plugin):
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "M400", None, "M400")
        assert result == "G4 P0.001"

    def test_suppressed_commands_return_none_tuple(self, plugin):
        for gcode in ["M21", "M84", "M104", "M140", "M106", "M107", "M109", "M190"]:
            result = plugin.sending_gcode_hook(MagicMock(), "sending", gcode, None, gcode)
            assert result == (None,), f"{gcode} should be suppressed"

    def test_normal_gcode_passes_through(self, plugin):
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "G0 X10", None, "G0")
        assert result == "G0 X10"

    def test_cnc_commands_pass_through(self, plugin):
        for cmd, gcode in [("M3 S12000", "M3"), ("M5", "M5"), ("G28", "G28")]:
            result = plugin.sending_gcode_hook(MagicMock(), "sending", cmd, None, gcode)
            assert result == cmd, f"{cmd} should pass through unchanged"

    def test_empty_command_passes_through(self, plugin):
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "", None, None)
        assert result == ""


class TestFirmwareDetection:
    """Test firmware version detection from serial responses."""

    def test_firmware_version_default(self, plugin):
        assert plugin._firmware_version is None

    def test_firmware_version_in_api_response(self, plugin):
        plugin._firmware_version = "v1.0.5"
        mock_flask = MagicMock()
        mock_flask.jsonify.return_value = MagicMock()
        with patch.dict("sys.modules", {"flask": mock_flask}):
            plugin.on_api_get(MagicMock())
            call_kwargs = mock_flask.jsonify.call_args[1]
            assert call_kwargs["firmware_version"] == "v1.0.5"

    def test_version_response_parsed(self, plugin):
        plugin.received_hook(MagicMock(), "Build version: v1.0.5, Build date: Dec 10 2024\n")
        assert plugin._firmware_version == "v1.0.5"

    def test_version_response_with_different_format(self, plugin):
        plugin.received_hook(MagicMock(), "Build version: v2.0.0-RC1, Build date: Jan 1 2025\n")
        assert plugin._firmware_version == "v2.0.0-RC1"

    def test_commercial_version_format_parsed(self, plugin):
        plugin.received_hook(MagicMock(), "version = 1.0.3\n")
        assert plugin._firmware_version == "1.0.3"

    def test_non_version_line_ignored(self, plugin):
        plugin.received_hook(MagicMock(), "ok\n")
        assert plugin._firmware_version is None


class TestNavigation:
    """Test M496 navigation commands."""

    def test_goto_clearance_sends_m496_1(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("goto_clearance", {})
        plugin._printer.commands.assert_called_with("M496.1")

    def test_goto_work_origin_sends_m496_2(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("goto_work_origin", {})
        plugin._printer.commands.assert_called_with("M496.2")

    def test_goto_anchor1_sends_m496_3(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("goto_anchor1", {})
        plugin._printer.commands.assert_called_with("M496.3")

    def test_goto_anchor2_sends_m496_4(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("goto_anchor2", {})
        plugin._printer.commands.assert_called_with("M496.4")

    def test_goto_blocked_in_run(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "goto_clearance" not in allowed

    def test_goto_blocked_in_alarm(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "goto_clearance" not in allowed


class TestAccessories:
    """Test spindle, air, light, vacuum commands."""

    def test_spindle_on_sends_m3(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._tool = {"number": 3, "offset": -16.281, "target": -1}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("spindle_on", {"rpm": 5000})
        plugin._printer.commands.assert_called_with("M3 S5000")

    def test_spindle_on_blocked_without_tool(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._tool = {"number": 0, "offset": 0.0, "target": -1}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("spindle_on", {"rpm": 5000})
        plugin._printer.commands.assert_not_called()

    def test_spindle_on_blocked_with_probe(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._tool = {"number": 999990, "offset": 0.0, "target": -1}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("spindle_on", {"rpm": 5000})
        plugin._printer.commands.assert_not_called()

    def test_spindle_off_sends_m5(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("spindle_off", {})
        plugin._printer.commands.assert_called_with("M5")

    def test_air_on_sends_m7(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("air_on", {})
        plugin._printer.commands.assert_called_with("M7")

    def test_air_off_sends_m9(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("air_off", {})
        plugin._printer.commands.assert_called_with("M9")

    def test_light_on_sends_m821(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("light_on", {})
        plugin._printer.commands.assert_called_with("M821")

    def test_vacuum_on_sends_m801(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("vacuum_on", {})
        plugin._printer.commands.assert_called_with("M801 S100")

    def test_accessories_blocked_in_alarm(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "spindle_on" not in allowed
        assert "air_on" not in allowed
        assert "light_on" not in allowed

    def test_spindle_off_allowed_in_run(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "spindle_off" in allowed
        assert "air_off" in allowed


class TestRestart:
    """Test machine restart command."""

    def test_restart_sends_reset(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("restart", {})
        plugin._printer.commands.assert_called_with("reset")

    def test_restart_allowed_in_alarm(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "restart" in allowed

    def test_restart_blocked_in_run(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "restart" not in allowed


class TestJogAndGoto:
    """Test jog (relative) and goto (absolute) movement commands."""

    def test_jog_x_positive(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._work_pos = {"x": 100.0, "y": 50.0, "z": 54.0, "a": 0.0, "b": 0.0}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("jog", {"x": 10, "y": 0, "z": 0})
        plugin._printer.commands.assert_called_with("G0 G90 X110.000")

    def test_jog_xy(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._work_pos = {"x": 100.0, "y": 50.0, "z": 54.0, "a": 0.0, "b": 0.0}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("jog", {"x": 5, "y": -10, "z": 0})
        plugin._printer.commands.assert_called_with("G0 G90 X105.000 Y40.000")

    def test_jog_z_only(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._work_pos = {"x": 100.0, "y": 50.0, "z": 54.0, "a": 0.0, "b": 0.0}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("jog", {"x": 0, "y": 0, "z": 1})
        plugin._printer.commands.assert_called_with("G0 G90 Z55.000")

    def test_jog_zero_no_command(self, plugin):
        plugin._grbl_state = "Idle"
        plugin._work_pos = {"x": 100.0, "y": 50.0, "z": 54.0, "a": 0.0, "b": 0.0}
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("jog", {"x": 0, "y": 0, "z": 0})
        plugin._printer.commands.assert_not_called()

    def test_goto_single_axis(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("goto", {"x": 50.0})
        plugin._printer.commands.assert_called_with("G0 G90 X50.000")

    def test_goto_all_axes(self, plugin):
        plugin._grbl_state = "Idle"
        with patch.dict("sys.modules", {"flask": MagicMock()}):
            plugin.on_api_command("goto", {"x": 100.0, "y": 50.0, "z": 54.0})
        plugin._printer.commands.assert_called_with("G0 G90 X100.000 Y50.000 Z54.000")

    def test_jog_blocked_in_run(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "jog" not in allowed
        assert "goto" not in allowed


class TestStartupConfig:
    """Test OctoPrint configuration on startup."""

    def test_startup_configures_serial(self, plugin):
        plugin._printer_profile_manager = MagicMock()
        plugin._printer_profile_manager.exists.return_value = True
        plugin.on_after_startup()
        # Should have set serial.neverSendChecksum
        plugin._settings.global_set.assert_any_call(["serial", "neverSendChecksum"], True)

    def test_startup_disables_temperature_tab(self, plugin):
        plugin._printer_profile_manager = MagicMock()
        plugin._printer_profile_manager.exists.return_value = True
        plugin.on_after_startup()
        plugin._settings.global_set.assert_any_call(
            ["appearance", "components", "disabled", "tab"],
            ["temperature", "plugin_gcodeviewer", "control"],
        )


class TestKeepalive:
    """Test keepalive thread sends periodic ? queries."""

    def test_keepalive_sends_queries(self, plugin):
        plugin._printer._comm = MagicMock()
        plugin._printer._comm._serial = MagicMock()
        plugin._start_keepalive()
        time.sleep(0.8)  # 0.3s interval -> should get at least 2 queries
        plugin._stop_keepalive()
        assert plugin._printer._comm._serial.write.call_count >= 2
        plugin._printer._comm._serial.write.assert_called_with(b"?")

    def test_keepalive_stops_cleanly(self, plugin):
        plugin._settings.get_float.return_value = 0.1
        plugin._start_keepalive()
        assert plugin._keepalive_active is True
        plugin._stop_keepalive()
        assert plugin._keepalive_active is False

    def test_keepalive_pauses_during_file_op(self, plugin):
        """Keepalive should not send queries while file_op_event is cleared."""
        plugin._printer._comm = MagicMock()
        plugin._printer._comm._serial = MagicMock()
        plugin._start_keepalive()
        time.sleep(0.5)
        initial_count = plugin._printer._comm._serial.write.call_count
        assert initial_count >= 1  # Polling is working

        # Simulate file operation — clear the event
        plugin._file_op_event.clear()
        plugin._printer._comm._serial.write.reset_mock()
        time.sleep(0.5)
        # Should NOT have sent any queries while event is cleared
        assert plugin._printer._comm._serial.write.call_count == 0

        # Resume
        plugin._file_op_event.set()
        time.sleep(0.5)
        assert plugin._printer._comm._serial.write.call_count >= 1
        plugin._stop_keepalive()


class TestFileOperations:
    """Test file operation API commands and state gating."""

    def test_list_files_api_exists(self, plugin):
        commands = plugin.get_api_commands()
        assert "list_files" in commands

    def test_upload_to_carvera_api_exists(self, plugin):
        commands = plugin.get_api_commands()
        assert "upload_to_carvera" in commands

    def test_list_files_allowed_in_idle(self, plugin):
        plugin._grbl_state = "Idle"
        allowed = plugin._get_allowed_actions()
        assert "list_files" in allowed

    def test_list_files_blocked_in_run(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "list_files" not in allowed

    def test_upload_allowed_in_idle(self, plugin):
        plugin._grbl_state = "Idle"
        allowed = plugin._get_allowed_actions()
        assert "upload_to_carvera" in allowed

    def test_upload_blocked_in_run(self, plugin):
        plugin._grbl_state = "Run"
        allowed = plugin._get_allowed_actions()
        assert "upload_to_carvera" not in allowed

    def test_upload_blocked_in_alarm(self, plugin):
        plugin._grbl_state = "Alarm"
        allowed = plugin._get_allowed_actions()
        assert "upload_to_carvera" not in allowed

    def test_file_op_event_initialized_set(self, plugin):
        """File op event should be set by default (polling runs)."""
        assert plugin._file_op_event.is_set()

    def test_sending_hook_suppresses_during_file_op(self, plugin):
        """When file_op_event is cleared, all gcode should be suppressed."""
        plugin._file_op_event.clear()
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "G0 X10", None, "G0")
        assert result == (None,)
        plugin._file_op_event.set()

    def test_sending_hook_normal_when_no_file_op(self, plugin):
        """When file_op_event is set, normal commands pass through."""
        assert plugin._file_op_event.is_set()
        result = plugin.sending_gcode_hook(MagicMock(), "sending", "G0 X10", None, "G0")
        assert result == "G0 X10"


class TestAutoUnlockOnConnect:
    """Cold-boot recovery: opening the USB serial port toggles DTR, which
    Carvera's Smoothieware firmware interprets as an abort and enters
    Alarm. Plugin auto-sends $X on connect so the user doesn't have to
    walk over and click Unlock after a power-outage reboot.
    """

    def _build_plain_text(self):
        from octocarvera.carvera_comm import build_communication

        send_command = MagicMock()
        send_realtime = MagicMock()
        send_raw_text = MagicMock()
        comm = build_communication(
            "plain_text", send_command, send_realtime, send_raw_text, MagicMock()
        )
        return comm, send_command, send_raw_text

    def _build_binary(self):
        from octocarvera.carvera_comm import build_communication

        send_command = MagicMock()
        send_realtime = MagicMock()
        send_raw_text = MagicMock()
        comm = build_communication(
            "binary", send_command, send_realtime, send_raw_text, MagicMock()
        )
        return comm, send_command, send_raw_text

    def test_plain_text_on_connect_init_sends_unlock_after_version(self):
        comm, send_command, send_raw_text = self._build_plain_text()
        comm.on_connect_init(send_init_flag=True, auto_unlock=True)
        # Order: INIT_SEQUENCE via queue, version via raw bypass, $X via raw bypass
        send_command.assert_called_once_with("\n;\n")
        assert send_raw_text.call_args_list == [call("version"), call("$X")]

    def test_binary_on_connect_init_sends_unlock_after_version(self):
        comm, send_command, send_raw_text = self._build_binary()
        comm.on_connect_init(send_init_flag=True, auto_unlock=True)
        # Binary: both version and $X go through queue (BinaryFrameSerial acks)
        assert send_command.call_args_list == [call("version"), call("$X")]
        send_raw_text.assert_not_called()

    def test_plain_text_auto_unlock_disabled_skips_unlock(self):
        comm, send_command, send_raw_text = self._build_plain_text()
        comm.on_connect_init(send_init_flag=True, auto_unlock=False)
        send_command.assert_called_once_with("\n;\n")
        # version still goes out, but no $X follows
        send_raw_text.assert_called_once_with("version")

    def test_binary_auto_unlock_disabled_skips_unlock(self):
        comm, send_command, send_raw_text = self._build_binary()
        comm.on_connect_init(send_init_flag=True, auto_unlock=False)
        send_command.assert_called_once_with("version")
        send_raw_text.assert_not_called()

    def test_send_init_disabled_still_unlocks(self):
        """auto_unlock is independent of send_init_flag. User may turn
        off the init handshake but still want the DTR alarm cleared."""
        comm, send_command, send_raw_text = self._build_plain_text()
        comm.on_connect_init(send_init_flag=False, auto_unlock=True)
        # No init/version, but $X still goes out via the raw-text bypass.
        send_command.assert_not_called()
        send_raw_text.assert_called_once_with("$X")

    def test_both_flags_disabled_is_a_noop(self):
        comm, send_command, send_raw_text = self._build_plain_text()
        comm.on_connect_init(send_init_flag=False, auto_unlock=False)
        send_command.assert_not_called()
        send_raw_text.assert_not_called()

    def test_binary_send_init_disabled_still_unlocks(self):
        comm, send_command, send_raw_text = self._build_binary()
        comm.on_connect_init(send_init_flag=False, auto_unlock=True)
        send_command.assert_called_once_with("$X")
        send_raw_text.assert_not_called()

    def test_on_printer_connected_passes_auto_unlock_flag(self, plugin):
        """Wiring check: _on_printer_connected reads the setting and forwards it."""
        plugin._comm_mode = MagicMock()
        plugin._comm_mode.name = "plain_text"
        plugin._on_printer_connected()
        plugin._comm_mode.on_connect_init.assert_called_once()
        kwargs = plugin._comm_mode.on_connect_init.call_args.kwargs
        assert kwargs.get("auto_unlock") is True
        plugin._stop_keepalive()

    def test_startup_race_branch_unlocks_via_comm_mode(self, plugin):
        """Patch unlock on the built comm_mode and assert it's called."""
        plugin._settings.global_get.return_value = []
        plugin._printer.is_operational.return_value = True
        with patch.object(
            __import__("octocarvera.carvera_comm", fromlist=["PlainTextCommunication"]).PlainTextCommunication,
            "unlock",
        ) as mock_unlock:
            plugin.on_after_startup()
            mock_unlock.assert_called_once()
        plugin._stop_keepalive()

    def test_startup_race_branch_skips_unlock_when_setting_off(self, plugin):
        """Auto-unlock setting disables the cold-boot recovery path too."""
        plugin._settings.global_get.return_value = []
        plugin._printer.is_operational.return_value = True

        # get_boolean returns False only for auto_unlock_on_connect, True otherwise
        def get_bool(keys):
            return keys != ["auto_unlock_on_connect"]
        plugin._settings.get_boolean.side_effect = get_bool

        with patch.object(
            __import__("octocarvera.carvera_comm", fromlist=["PlainTextCommunication"]).PlainTextCommunication,
            "unlock",
        ) as mock_unlock:
            plugin.on_after_startup()
            mock_unlock.assert_not_called()
        plugin._stop_keepalive()

    def test_default_setting_is_true(self, plugin):
        defaults = plugin.get_settings_defaults()
        assert defaults["auto_unlock_on_connect"] is True


class TestHandshakeWatchdog:
    """0.5.16: when the pre-handshake unlock runs but OctoPrint's $G
    handshake still times out, the Carvera's MCU is latched and needs
    a physical power cycle. Surface that instead of looping silently.
    """

    def test_arm_sets_timer(self, plugin):
        plugin._arm_handshake_watchdog("/dev/ttyUSB0")
        assert plugin._handshake_watchdog is not None
        plugin._cancel_handshake_watchdog()

    def test_cancel_clears_timer(self, plugin):
        plugin._arm_handshake_watchdog("/dev/ttyUSB0")
        plugin._cancel_handshake_watchdog()
        assert plugin._handshake_watchdog is None

    def test_arm_replaces_and_cancels_previous_timer(self, plugin):
        plugin._arm_handshake_watchdog("/dev/ttyUSB0")
        first = plugin._handshake_watchdog
        plugin._arm_handshake_watchdog("/dev/ttyUSB0")
        assert plugin._handshake_watchdog is not first
        # The previous timer must be cancelled — otherwise two watchdogs
        # are running and one will fire a false "unresponsive" alert.
        assert not first.is_alive()
        plugin._cancel_handshake_watchdog()

    def test_on_printer_connected_cancels_watchdog(self, plugin):
        plugin._arm_handshake_watchdog("/dev/ttyUSB0")
        plugin._comm_mode = MagicMock()
        plugin._comm_mode.name = "plain_text"
        plugin._on_printer_connected()
        assert plugin._handshake_watchdog is None
        plugin._stop_keepalive()

    def test_on_printer_disconnected_cancels_watchdog(self, plugin):
        plugin._arm_handshake_watchdog("/dev/ttyUSB0")
        plugin._on_printer_disconnected()
        assert plugin._handshake_watchdog is None

    def test_timeout_sends_plugin_message(self, plugin):
        plugin._on_handshake_timeout("/dev/ttyUSB0")
        calls = plugin._plugin_manager.send_plugin_message.call_args_list
        payloads = [c.args[1] for c in calls]
        assert any(p.get("type") == "carvera_unresponsive" for p in payloads)
        unresponsive = next(p for p in payloads if p.get("type") == "carvera_unresponsive")
        assert unresponsive["port"] == "/dev/ttyUSB0"

    def test_timeout_logs_warning(self, plugin):
        plugin._on_handshake_timeout("/dev/ttyUSB0")
        plugin._logger.warning.assert_called()

    def test_serial_factory_hook_arms_watchdog_for_real_port(self, plugin):
        # Build a real comm_mode and mock its serial_factory to return a
        # sentinel serial object, so the hook thinks the port is live.
        plugin._comm_mode = MagicMock()
        plugin._comm_mode.serial_factory.return_value = MagicMock()  # pretend ser
        plugin.serial_factory_hook(MagicMock(), "/dev/ttyUSB0", 115200, 10.0)
        assert plugin._handshake_watchdog is not None
        plugin._cancel_handshake_watchdog()

    def test_serial_factory_hook_does_not_arm_for_virtual(self, plugin):
        plugin._comm_mode = MagicMock()
        plugin._comm_mode.serial_factory.return_value = None
        plugin.serial_factory_hook(MagicMock(), "VIRTUAL", 115200, 10.0)
        assert plugin._handshake_watchdog is None

    def test_serial_factory_hook_does_not_arm_when_factory_returns_none(self, plugin):
        plugin._comm_mode = MagicMock()
        plugin._comm_mode.serial_factory.return_value = None
        plugin.serial_factory_hook(MagicMock(), "/dev/ttyUSB0", 115200, 10.0)
        assert plugin._handshake_watchdog is None
