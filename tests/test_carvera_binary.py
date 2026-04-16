# coding=utf-8
"""Tests for the Carvera binary framing protocol (firmware 1.0.5+).

Verified against Wireshark capture of official Makera Controller and
live hardware test on Carvera Air fw 1.0.5 (2026-04-10).
"""

import pytest
from octocarvera.carvera_binary import (
    crc16_xmodem, build_frame, build_command_frame, parse_frame,
    BinaryFrameSerial,
    TYPE_STATUS_QUERY, TYPE_TEXT_CMD, TYPE_STATUS_RESP, TYPE_TEXT_RESP,
    HEADER, TRAILER,
)


class TestCRC16:
    """CRC-16/XMODEM verified against captured packets."""

    def test_status_query(self):
        # From Wireshark: 86 68 00 04 a1 3f [35 33] 55 aa
        data = bytes([0x00, 0x04, 0xa1, 0x3f])
        assert crc16_xmodem(data) == 0x3533

    def test_time_command(self):
        data = bytes([0x00, 0x07, 0xa2]) + b'time'
        assert crc16_xmodem(data) == 0x6aad

    def test_model_command(self):
        data = bytes([0x00, 0x08, 0xa2]) + b'model'
        assert crc16_xmodem(data) == 0x5201

    def test_version_command(self):
        data = bytes([0x00, 0x0a, 0xa2]) + b'version'
        assert crc16_xmodem(data) == 0xcca0

    def test_version_response(self):
        data = bytes([0x00, 0x13, 0x90]) + b'version = 1.0.5\n'
        assert crc16_xmodem(data) == 0xbd72

    def test_time_response(self):
        data = bytes([0x00, 0x15, 0x90]) + b'time = 1775838090\n'
        assert crc16_xmodem(data) == 0x2c46


class TestBuildFrame:
    """Frame construction matches captured packets byte-for-byte."""

    def test_status_query_frame(self):
        frame = build_frame(TYPE_STATUS_QUERY, b'?')
        assert frame == bytes.fromhex('86680004a13f353355aa')

    def test_version_command_frame(self):
        frame = build_frame(TYPE_TEXT_CMD, b'version')
        assert frame == bytes.fromhex('8668000aa276657273696f6ecca055aa')

    def test_time_command_frame(self):
        frame = build_frame(TYPE_TEXT_CMD, b'time')
        assert frame == bytes.fromhex('86680007a274696d656aad55aa')

    def test_model_command_frame(self):
        frame = build_frame(TYPE_TEXT_CMD, b'model')
        assert frame == bytes.fromhex('86680008a26d6f64656c520155aa')


class TestBuildCommandFrame:
    """build_command_frame auto-selects the correct message type."""

    def test_status_query_type(self):
        frame = build_command_frame('?')
        assert frame == build_frame(TYPE_STATUS_QUERY, b'?')

    def test_text_command_type(self):
        frame = build_command_frame('version')
        assert frame == build_frame(TYPE_TEXT_CMD, b'version')

    def test_gcode_type(self):
        frame = build_command_frame('G0 X10 Y20')
        assert frame == build_frame(TYPE_TEXT_CMD, b'G0 X10 Y20')

    def test_strips_whitespace(self):
        frame = build_command_frame('  version  \n')
        assert frame == build_frame(TYPE_TEXT_CMD, b'version')


class TestParseFrame:
    """Frame parsing extracts type and payload correctly."""

    def test_parse_status_response(self):
        # Build a status response frame
        payload = b'<Idle|MPos:0,0,0,0,0|WPos:0,0,0,0,0>'
        frame = build_frame(TYPE_STATUS_RESP, payload)
        result = parse_frame(frame)
        assert result is not None
        typ, data, next_offset = result
        assert typ == TYPE_STATUS_RESP
        assert data == payload
        assert next_offset == len(frame)

    def test_parse_version_response(self):
        # Exact bytes from Wireshark capture
        raw = bytes.fromhex('866800139076657273696f6e203d20312e302e350abd7255aa')
        result = parse_frame(raw)
        assert result is not None
        typ, data, next_offset = result
        assert typ == TYPE_TEXT_RESP
        assert data == b'version = 1.0.5\n'

    def test_parse_status_query(self):
        raw = bytes.fromhex('86680004a13f353355aa')
        result = parse_frame(raw)
        assert result is not None
        typ, data, _ = result
        assert typ == TYPE_STATUS_QUERY
        assert data == b'?'

    def test_parse_with_offset(self):
        garbage = b'\x00\x01\x02'
        frame = build_frame(TYPE_TEXT_RESP, b'ok\n')
        raw = garbage + frame
        result = parse_frame(raw, offset=0)
        assert result is not None
        typ, data, _ = result
        assert data == b'ok\n'

    def test_parse_incomplete_returns_none(self):
        frame = build_frame(TYPE_STATUS_QUERY, b'?')
        assert parse_frame(frame[:5]) is None

    def test_parse_bad_crc_returns_none(self):
        frame = bytearray(build_frame(TYPE_STATUS_QUERY, b'?'))
        frame[-3] ^= 0xFF  # Corrupt CRC
        assert parse_frame(bytes(frame)) is None

    def test_parse_bad_trailer_returns_none(self):
        frame = bytearray(build_frame(TYPE_STATUS_QUERY, b'?'))
        frame[-1] = 0x00  # Corrupt trailer
        assert parse_frame(bytes(frame)) is None


class TestBinaryFrameSerial:
    """Tests for the serial port wrapper."""

    class FakeSerial:
        """Minimal fake serial port for testing."""

        def __init__(self, responses=None):
            self.written = bytearray()
            self._responses = responses or []
            self._resp_idx = 0
            self.timeout = 1.0
            self.baudrate = 115200
            self.port = '/dev/ttyFAKE'
            self.is_open = True
            self.write_timeout = 10

        @property
        def in_waiting(self):
            if self._resp_idx < len(self._responses):
                return len(self._responses[self._resp_idx])
            return 0

        def write(self, data):
            self.written.extend(data)
            return len(data)

        def read(self, size=1):
            if self._resp_idx < len(self._responses):
                data = self._responses[self._resp_idx][:size]
                self._responses[self._resp_idx] = self._responses[self._resp_idx][size:]
                if not self._responses[self._resp_idx]:
                    self._resp_idx += 1
                return data
            return b''

        def reset_input_buffer(self):
            pass

        def reset_output_buffer(self):
            pass

        def flush(self):
            pass

        def close(self):
            self.is_open = False

    def test_write_wraps_in_binary_frame(self):
        fake = self.FakeSerial()
        wrapper = BinaryFrameSerial(fake)
        wrapper.write(b'version\n')
        expected = build_command_frame('version')
        assert bytes(fake.written) == expected

    def test_write_status_query_framed(self):
        """'?' is framed (not raw) because the response needs binary framing too."""
        fake = self.FakeSerial()
        wrapper = BinaryFrameSerial(fake)
        wrapper.write(b'?')
        expected = build_command_frame('?')
        assert bytes(fake.written) == expected

    def test_readline_unwraps_binary_frame(self):
        # Prepare a binary-framed response
        response_frame = build_frame(TYPE_TEXT_RESP, b'version = 1.0.5\n')
        fake = self.FakeSerial(responses=[response_frame])
        wrapper = BinaryFrameSerial(fake)
        line = wrapper.readline()
        assert line == b'version = 1.0.5\n'

    def test_readline_unwraps_status_response(self):
        status = b'<Idle|MPos:0,0,0,0,0|WPos:0,0,0,0,0>'
        response_frame = build_frame(TYPE_STATUS_RESP, status)
        fake = self.FakeSerial(responses=[response_frame])
        wrapper = BinaryFrameSerial(fake)
        line = wrapper.readline()
        assert line == b'<Idle|MPos:0,0,0,0,0|WPos:0,0,0,0,0>\n'

    def test_readline_timeout_returns_empty(self):
        fake = self.FakeSerial()
        fake.timeout = 0.1
        wrapper = BinaryFrameSerial(fake)
        line = wrapper.readline()
        assert line == b''

    def test_multiple_frames_queued(self):
        frame1 = build_frame(TYPE_STATUS_RESP, b'<Idle>')
        frame2 = build_frame(TYPE_STATUS_RESP, b'<Run>')
        fake = self.FakeSerial(responses=[frame1 + frame2])
        wrapper = BinaryFrameSerial(fake)
        line1 = wrapper.readline()
        line2 = wrapper.readline()
        assert line1 == b'<Idle>\n'
        assert line2 == b'<Run>\n'

    def test_text_response_injects_ok(self):
        """Text command responses (type 0x90) should be followed by ok."""
        frame = build_frame(TYPE_TEXT_RESP, b'version = 1.0.5\n')
        fake = self.FakeSerial(responses=[frame])
        wrapper = BinaryFrameSerial(fake)
        line1 = wrapper.readline()
        line2 = wrapper.readline()
        assert line1 == b'version = 1.0.5\n'
        assert line2 == b'ok\n'

    def test_status_response_no_ok(self):
        """Status responses (type 0x81) should NOT inject ok."""
        frame = build_frame(TYPE_STATUS_RESP, b'<Idle|MPos:0,0,0,0,0>')
        fake = self.FakeSerial(responses=[frame])
        fake.timeout = 0.1
        wrapper = BinaryFrameSerial(fake)
        line1 = wrapper.readline()
        line2 = wrapper.readline()
        assert line1 == b'<Idle|MPos:0,0,0,0,0>\n'
        assert line2 == b''  # timeout, no ok injected

    def test_multiline_payload_split(self):
        """Multi-line payloads (e.g. ls output) should be split into individual lines."""
        ls_output = b'file1.nc 1234 20260101120000\r\nfile2.nc 5678 20260102120000\r\nsubdir/ 0 20260103120000\n'
        frame = build_frame(TYPE_TEXT_RESP, ls_output)
        fake = self.FakeSerial(responses=[frame])
        wrapper = BinaryFrameSerial(fake)
        line1 = wrapper.readline()
        line2 = wrapper.readline()
        line3 = wrapper.readline()
        line4 = wrapper.readline()  # injected ok
        assert line1 == b'file1.nc 1234 20260101120000\n'
        assert line2 == b'file2.nc 5678 20260102120000\n'
        assert line3 == b'subdir/ 0 20260103120000\n'
        assert line4 == b'ok\n'

    def test_realtime_commands_bypass_framing(self):
        """Realtime bytes (!, ~, \\x18, \\x19) must go raw, not framed."""
        fake = self.FakeSerial()
        wrapper = BinaryFrameSerial(fake)

        for byte_val in [b'!', b'~', b'\x18', b'\x19']:
            fake.written.clear()
            wrapper.write(byte_val)
            # Should be sent as raw single byte, NOT wrapped in a frame
            assert bytes(fake.written) == byte_val, f"Realtime byte {byte_val!r} should bypass framing"

    def test_multi_byte_question_mark_gets_framed(self):
        """A '?' inside a longer command should still be framed."""
        fake = self.FakeSerial()
        wrapper = BinaryFrameSerial(fake)
        wrapper.write(b'G28?\n')
        # Should be framed, not raw
        assert fake.written[:2] == b'\x86\x68'

    def test_properties_delegated(self):
        fake = self.FakeSerial()
        wrapper = BinaryFrameSerial(fake)
        assert wrapper.baudrate == 115200
        assert wrapper.port == '/dev/ttyFAKE'
        assert wrapper.is_open is True
        wrapper.close()
        assert fake.is_open is False
