# coding=utf-8
"""Tests for GRBL status response parsing."""

import re
import pytest

from octocarvera.carvera_protocol import GRBL_STATES, parse_carvera_status


# Real Carvera Air status line captured during hardware testing 2026-04-03
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

ALARM_STATUS = (
    "<Alarm|MPos:196.8375,119.6109,1.1125,13.6552,0.0000"
    "|WPos:393.5325,239.1459,58.1484,99.8552,0.0000"
    "|F:0.0,1000.0,100.0"
    "|S:0.0,10000.0,100.0,0,17.3,19.8"
    "|T:3,-16.281,-1"
    "|W:0.00"
    "|L:0, 0, 0, 0.0,100.0"
    "|H:1"
    "|C:2,1,0,0>"
)

# Synthetic original Carvera status (model 1, ATC-equipped)
CARVERA_ORIGINAL_STATUS = (
    "<Idle|MPos:-300.0000,-200.0000,-5.0000,-90.0000,0.0000"
    "|WPos:0.0000,0.0000,70.0000,0.0000,0.0000"
    "|F:0.0,3000.0,100.0"
    "|S:0.0,12000.0,100.0,0,22.1,25.3"
    "|T:1,-12.500"
    "|W:3.45"
    "|L:0, 0, 0, 0.0,100.0"
    "|C:1,5,0,0>"
)

# Official firmware S field with 9 values
OFFICIAL_S_9_VALUES = (
    "<Run|MPos:0.0,0.0,0.0"
    "|S:5000.0,5000.0,100.0,0,25.1,30.2,0,0,1>"
)

# Community firmware with extra fields (R, G, PWM)
COMMUNITY_STATUS = (
    "<Run|MPos:10.0,20.0,30.0,0.0,0.0"
    "|WPos:5.0,10.0,15.0,0.0,0.0"
    "|F:1000.0,3000.0,80.0"
    "|S:8000.0,10000.0,100.0,0,20.5,28.3"
    "|R:45.0000"
    "|G:2"
    "|PWM:0.750"
    "|C:2,1,0,1>"
)

# Playback status with P field (official: 3 values)
PLAYBACK_STATUS_OFFICIAL = (
    "<Run|MPos:0.0,0.0,0.0"
    "|P:1500,45,3600"
    "|C:2,1,0,0>"
)

# Playback status with P field (community: 4 values with is_playing)
PLAYBACK_STATUS_COMMUNITY = (
    "<Run|MPos:0.0,0.0,0.0"
    "|P:1500,45,3600,1"
    "|C:2,1,0,0>"
)


class TestParsCarveraStatus:
    """Test the two-stage Carvera status parser."""

    def test_parses_real_status_line(self):
        result = parse_carvera_status(REAL_STATUS)
        assert result is not None
        assert result["state"] == "Idle"

    def test_parses_alarm_status(self):
        result = parse_carvera_status(ALARM_STATUS)
        assert result is not None
        assert result["state"] == "Alarm"

    def test_5_axis_machine_pos(self):
        result = parse_carvera_status(REAL_STATUS)
        mpos = result["machine_pos"]
        assert mpos["x"] == pytest.approx(-278.195)
        assert mpos["y"] == pytest.approx(-192.035)
        assert mpos["z"] == pytest.approx(-3.0)
        assert mpos["a"] == pytest.approx(-86.2)
        assert mpos["b"] == pytest.approx(0.0)

    def test_5_axis_work_pos(self):
        result = parse_carvera_status(REAL_STATUS)
        wpos = result["work_pos"]
        assert wpos["x"] == pytest.approx(0.0)
        assert wpos["y"] == pytest.approx(0.0)
        assert wpos["z"] == pytest.approx(54.0359)
        assert wpos["a"] == pytest.approx(0.0)
        assert wpos["b"] == pytest.approx(0.0)

    def test_feed_rate(self):
        result = parse_carvera_status(REAL_STATUS)
        feed = result["feed"]
        assert feed["current"] == pytest.approx(0.0)
        assert feed["max"] == pytest.approx(3000.0)
        assert feed["override"] == pytest.approx(100.0)

    def test_spindle(self):
        result = parse_carvera_status(REAL_STATUS)
        spindle = result["spindle"]
        assert spindle["current"] == pytest.approx(0.0)
        assert spindle["max"] == pytest.approx(10000.0)
        assert spindle["override"] == pytest.approx(100.0)
        assert spindle["vacuum_mode"] == 0
        assert spindle["spindle_temp"] == pytest.approx(17.2)
        assert spindle["power_temp"] == pytest.approx(19.8)

    def test_tool(self):
        result = parse_carvera_status(REAL_STATUS)
        tool = result["tool"]
        assert tool["number"] == 3
        assert tool["offset"] == pytest.approx(-16.281)
        assert tool["target"] == -1

    def test_halt_reason(self):
        result = parse_carvera_status(REAL_STATUS)
        assert result["halt_reason"] == 1

    def test_state_only(self):
        result = parse_carvera_status("<Idle>")
        assert result is not None
        assert result["state"] == "Idle"
        assert result["machine_pos"] is None
        assert result["work_pos"] is None

    def test_simple_3_axis(self):
        """Backward compat: standard GRBL 3-axis status still works."""
        result = parse_carvera_status("<Run|MPos:10.5,-20.3,5.0|WPos:1.0,2.0,3.0>")
        assert result is not None
        assert result["state"] == "Run"
        assert result["machine_pos"]["x"] == pytest.approx(10.5)
        assert result["machine_pos"]["y"] == pytest.approx(-20.3)
        assert result["machine_pos"]["z"] == pytest.approx(5.0)
        assert result["machine_pos"]["a"] == pytest.approx(0.0)
        assert result["work_pos"]["x"] == pytest.approx(1.0)

    def test_missing_fields_returns_none(self):
        """Fields not present in the response should be None."""
        result = parse_carvera_status("<Idle|MPos:0.0,0.0,0.0>")
        assert result["feed"] is None
        assert result["spindle"] is None
        assert result["tool"] is None
        assert result["halt_reason"] is None
        assert result["playback"] is None
        assert result["rotation"] is None
        assert result["wcs"] is None
        assert result["pwm"] is None

    def test_no_match_on_ok(self):
        assert parse_carvera_status("ok") is None

    def test_no_match_on_error(self):
        assert parse_carvera_status("error:1") is None

    def test_no_match_on_empty(self):
        assert parse_carvera_status("") is None

    def test_no_match_on_alarm_text(self):
        assert parse_carvera_status("ALARM:1") is None


class TestGrblStatesSet:
    """Verify the GRBL_STATES set includes Carvera-specific states."""

    def test_contains_standard_states(self):
        for state in ["Idle", "Run", "Hold", "Jog", "Alarm", "Door", "Check", "Home", "Sleep"]:
            assert state in GRBL_STATES

    def test_contains_carvera_states(self):
        for state in ["Pause", "Wait", "Tool"]:
            assert state in GRBL_STATES

    def test_unknown_not_in_states(self):
        assert "Unknown" not in GRBL_STATES


class TestConfigParsing:
    """Test C field parsed as named dict."""

    def test_config_carvera_air(self):
        result = parse_carvera_status(REAL_STATUS)
        assert result["config"]["model"] == 2
        assert result["config"]["func_setting"] == 1
        assert result["config"]["inch_mode"] == 0
        assert result["config"]["absolute_mode"] == 0

    def test_config_original_carvera(self):
        result = parse_carvera_status(CARVERA_ORIGINAL_STATUS)
        assert result["config"]["model"] == 1
        assert result["config"]["func_setting"] == 5  # ATC bit set

    def test_wpvoltage(self):
        result = parse_carvera_status(REAL_STATUS)
        assert result["wpvoltage"] == pytest.approx(0.0)

    def test_wpvoltage_nonzero(self):
        result = parse_carvera_status(CARVERA_ORIGINAL_STATUS)
        assert result["wpvoltage"] == pytest.approx(3.45)


class TestOriginalCarvera:
    """Test parsing status from original Carvera (model 1, ATC)."""

    def test_parses_original_carvera(self):
        result = parse_carvera_status(CARVERA_ORIGINAL_STATUS)
        assert result is not None
        assert result["state"] == "Idle"

    def test_tool_2_values(self):
        """Original Carvera with ATC sends T:num,offset (2 values)."""
        result = parse_carvera_status(CARVERA_ORIGINAL_STATUS)
        assert result["tool"]["number"] == 1
        assert result["tool"]["offset"] == pytest.approx(-12.5)
        assert result["tool"]["target"] == -1  # default when absent

    def test_no_halt_reason_when_absent(self):
        """H field absent in normal operation."""
        result = parse_carvera_status(CARVERA_ORIGINAL_STATUS)
        assert result["halt_reason"] is None

    def test_spindle_12000(self):
        result = parse_carvera_status(CARVERA_ORIGINAL_STATUS)
        assert result["spindle"]["max"] == pytest.approx(12000.0)


class TestOfficialFirmwareSField:
    """Test S field with 9 values (official firmware)."""

    def test_parses_9_value_s_field(self):
        result = parse_carvera_status(OFFICIAL_S_9_VALUES)
        s = result["spindle"]
        assert s["current"] == pytest.approx(5000.0)
        assert s["max"] == pytest.approx(5000.0)
        assert s["override"] == pytest.approx(100.0)
        assert s["vacuum_mode"] == 0
        assert s["spindle_temp"] == pytest.approx(25.1)
        assert s["power_temp"] == pytest.approx(30.2)


class TestCommunityFirmware:
    """Test community firmware extra fields (R, G, PWM)."""

    def test_rotation(self):
        result = parse_carvera_status(COMMUNITY_STATUS)
        assert result["rotation"] == pytest.approx(45.0)

    def test_wcs(self):
        result = parse_carvera_status(COMMUNITY_STATUS)
        assert result["wcs"] == 2

    def test_pwm(self):
        result = parse_carvera_status(COMMUNITY_STATUS)
        assert result["pwm"] == pytest.approx(0.75)

    def test_absolute_mode_from_config(self):
        result = parse_carvera_status(COMMUNITY_STATUS)
        assert result["config"]["absolute_mode"] == 1


class TestPlayback:
    """Test P field (job playback progress)."""

    def test_playback_official_3_values(self):
        result = parse_carvera_status(PLAYBACK_STATUS_OFFICIAL)
        p = result["playback"]
        assert p["played_lines"] == 1500
        assert p["percent"] == 45
        assert p["elapsed_secs"] == 3600
        assert p["is_playing"] is False  # default when absent

    def test_playback_community_4_values(self):
        result = parse_carvera_status(PLAYBACK_STATUS_COMMUNITY)
        p = result["playback"]
        assert p["played_lines"] == 1500
        assert p["percent"] == 45
        assert p["elapsed_secs"] == 3600
        assert p["is_playing"] is True
