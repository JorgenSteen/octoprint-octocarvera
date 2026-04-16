"""Tests for carvera_xmodem.py — CRC, packet building, and transfer logic."""

import hashlib
import struct
from unittest.mock import MagicMock

import pytest

from octocarvera.carvera_xmodem import (
    ACK,
    CAN,
    EOT,
    NAK,
    PACKET_DATA_SIZE,
    SOH,
    _build_packet,
    crc16_xmodem,
    xmodem_send,
)


class TestCRC16:
    def test_empty(self):
        assert crc16_xmodem(b"") == 0

    def test_known_value(self):
        # "123456789" -> CRC-CCITT = 0x31C3
        assert crc16_xmodem(b"123456789") == 0x31C3

    def test_single_byte(self):
        result = crc16_xmodem(b"\x00")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF


class TestBuildPacket:
    def test_packet_structure(self):
        payload = b"hello"
        packet = _build_packet(1, payload)

        # SOH + seq + ~seq + length + data(128) + CRC(2)
        assert len(packet) == 3 + 1 + PACKET_DATA_SIZE + 2
        assert packet[0] == SOH
        assert packet[1] == 1  # seq
        assert packet[2] == 0xFE  # ~seq

    def test_sequence_wrapping(self):
        packet = _build_packet(256, b"data")
        assert packet[1] == 0  # 256 & 0xFF
        assert packet[2] == 0xFF  # ~0 & 0xFF

    def test_length_byte(self):
        payload = b"x" * 50
        packet = _build_packet(0, payload)
        assert packet[3] == 50  # length byte

    def test_padding(self):
        payload = b"short"
        packet = _build_packet(0, payload)
        # Data section starts at byte 4, padded to 128
        data_section = packet[4:4 + PACKET_DATA_SIZE]
        assert data_section[:5] == b"short"
        assert data_section[5:] == b"\x1a" * (PACKET_DATA_SIZE - 5)

    def test_crc_at_end(self):
        payload = b"test"
        packet = _build_packet(0, payload)
        crc_bytes = packet[-2:]
        crc_val = struct.unpack(">H", crc_bytes)[0]
        # Verify CRC matches the length+data section
        crc_data = packet[3:-2]  # length + padded data
        assert crc_val == crc16_xmodem(crc_data)


class TestXmodemSend:
    def _make_serial(self, responses):
        """Create a mock serial port that returns given bytes for read()."""
        serial = MagicMock()
        serial.timeout = 1.0
        serial.in_waiting = 0
        resp_iter = iter(responses)
        serial.read = MagicMock(side_effect=lambda n: next(resp_iter, b""))
        return serial

    def test_small_file_success(self):
        data = b"hello world"
        # Ready signal + 2 packets ACK + EOT ACK
        serial = self._make_serial([
            bytes([NAK]),  # receiver ready
            bytes([ACK]),  # seq 0
            bytes([ACK]),  # seq 1
            bytes([ACK]),  # EOT
        ])

        result = xmodem_send(serial, data)
        assert result is True

    def test_progress_callback(self):
        data = b"x" * 200  # 2 data packets
        # Ready + seq 0 + seq 1 + seq 2 + EOT
        serial = self._make_serial([
            bytes([NAK]),  # receiver ready
            bytes([ACK]),  # seq 0
            bytes([ACK]),  # seq 1
            bytes([ACK]),  # seq 2
            bytes([ACK]),  # EOT
        ])

        progress = []
        xmodem_send(serial, data, progress_callback=lambda sent, total: progress.append((sent, total)))

        assert len(progress) >= 2
        # Last progress should be total
        assert progress[-1] == (200, 200)

    def test_nak_retry(self):
        data = b"test"
        # Ready, then NAK on seq 0, then ACKs
        serial = self._make_serial([
            bytes([NAK]),  # receiver ready
            bytes([NAK]),  # NAK on seq 0
            bytes([ACK]),  # ACK on retry
            bytes([ACK]),  # ACK on seq 1
            bytes([ACK]),  # ACK on EOT
        ])

        result = xmodem_send(serial, data)
        assert result is True

    def test_cancel_by_receiver(self):
        data = b"test"
        serial = self._make_serial([bytes([CAN])])

        result = xmodem_send(serial, data)
        assert result is False

    def test_timeout_failure(self):
        data = b"test"
        # Return empty bytes (timeout) for all reads — never ready
        serial = self._make_serial([b""] * 100)

        result = xmodem_send(serial, data)
        assert result is False

    def test_md5_in_seq_zero(self):
        data = b"file content here"
        expected_md5 = hashlib.md5(data).hexdigest().encode("ascii")

        serial = self._make_serial([bytes([NAK])] + [bytes([ACK])] * 10)
        packets_written = []

        def capture_write(pkt):
            packets_written.append(pkt)

        serial.write = capture_write

        xmodem_send(serial, data)

        # First packet should be seq 0 with MD5
        first = packets_written[0]
        assert first[0] == SOH
        assert first[1] == 0  # seq 0
        # MD5 is in the data section (after length byte at offset 3)
        length = first[3]
        assert length == len(expected_md5)
        md5_data = first[4:4 + length]
        assert md5_data == expected_md5

    def test_eot_retry_then_ack(self):
        data = b"test"
        # Ready + ACK for packets, then empty on first EOT, ACK on retry
        serial = self._make_serial([
            bytes([NAK]),  # receiver ready
            bytes([ACK]),  # seq 0
            bytes([ACK]),  # seq 1
            b"",           # no ACK for first EOT
            bytes([ACK]),  # ACK on EOT retry
        ])

        result = xmodem_send(serial, data)
        assert result is True

    def test_info_text_noise_before_ack(self):
        """Firmware sends 'Info: upload\\n' before ACK on some packets."""
        data = b"test"
        info_bytes = list(b"Info: upload\n")
        serial = self._make_serial([
            bytes([NAK]),                               # receiver ready
            bytes([ACK]),                               # seq 0
            # seq 1: firmware sends "Info: upload\n" then ACK
            *[bytes([b]) for b in info_bytes],
            bytes([ACK]),
            bytes([ACK]),                               # EOT
        ])

        result = xmodem_send(serial, data)
        assert result is True

    def test_noise_without_protocol_byte_retries(self):
        """Non-protocol noise with no ACK/NAK after it triggers retry."""
        data = b"test"
        serial = self._make_serial([
            bytes([NAK]),                               # receiver ready
            bytes([ACK]),                               # seq 0
            # seq 1: noise then timeout (no protocol byte)
            b"X", b"Y", b"",
            # retry succeeds
            bytes([ACK]),
            bytes([ACK]),                               # EOT
        ])

        result = xmodem_send(serial, data)
        assert result is True

    def test_eot_no_ack(self):
        data = b"test"
        # Ready + ACK for packets, then empty for all EOT retries
        serial = self._make_serial([
            bytes([NAK]),  # receiver ready
            bytes([ACK]),  # seq 0
            bytes([ACK]),  # seq 1
        ] + [b""] * 20)   # no ACK for any EOT attempt

        result = xmodem_send(serial, data)
        assert result is False
