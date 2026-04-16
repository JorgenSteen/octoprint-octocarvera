"""XMODEM-128 file transfer for Carvera USB serial.

Implements the sender side of the XMODEM protocol with CRC-16,
matching the Carvera firmware's expected packet format for USB transfers.

Packet format: SOH + seq(1B) + ~seq(1B) + length(1B) + data(128B) + CRC16(2B)
Sequence 0 carries the MD5 hash. Sequences 1+ carry file data.
"""

import hashlib
import logging
import struct
import time

_logger = logging.getLogger("octoprint.plugins.octocarvera.xmodem")

# Protocol constants
SOH = 0x01     # Start of header (128-byte packet)
EOT = 0x04     # End of transmission
ACK = 0x06     # Acknowledge
NAK = 0x15     # Negative acknowledge
CAN = 0x16     # Cancel (Carvera firmware uses 0x16, not standard 0x18)

PACKET_DATA_SIZE = 128
MAX_RETRIES = 10
TIMEOUT = 1.0  # seconds


def crc16_xmodem(data):
    """CRC-CCITT (0x1021) used by XMODEM."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def _build_packet(seq, payload):
    """Build an XMODEM-128 packet.

    Format: SOH + seq + ~seq + length + data(padded to 128) + CRC16
    """
    length = len(payload)
    padded = payload.ljust(PACKET_DATA_SIZE, b"\x1a")
    # Length byte + padded data form the CRC payload
    crc_data = struct.pack("B", length) + padded
    crc = crc16_xmodem(crc_data)
    return (
        struct.pack("BBB", SOH, seq & 0xFF, (~seq) & 0xFF)
        + crc_data
        + struct.pack(">H", crc)
    )


def xmodem_send(serial_port, file_data, progress_callback=None, cancel_event=None):
    """Send file data via XMODEM-128 protocol.

    Builds packets on-the-fly (not pre-built) to avoid delays that cause
    the receiver's 'C' characters to accumulate in the serial buffer.

    Args:
        serial_port: pyserial port with exclusive access
        file_data: bytes to send
        progress_callback: optional fn(bytes_sent, total_bytes)
        cancel_event: optional threading.Event, set to cancel transfer

    Returns True on success, False on failure, None on cancel.
    """
    total = len(file_data)
    md5 = hashlib.md5(file_data).hexdigest().encode("ascii")
    num_data_packets = (total + PACKET_DATA_SIZE - 1) // PACKET_DATA_SIZE
    _logger.info("XMODEM: %d data packets (%d bytes, MD5=%s)", num_data_packets, total, md5.decode())

    old_timeout = serial_port.timeout
    serial_port.timeout = TIMEOUT

    try:
        # Flush any stale data in the serial buffer before handshake
        if serial_port.in_waiting:
            stale = serial_port.read(serial_port.in_waiting)
            _logger.info("XMODEM: flushed %d stale bytes", len(stale))

        # Wait for receiver to signal readiness (NAK or 'C' for CRC mode)
        _logger.info("XMODEM: waiting for receiver ready signal")
        ready = False
        for wait_i in range(60):  # Up to 60 seconds
            resp = serial_port.read(1)
            if resp:
                if resp[0] == NAK or resp[0] == ord('C'):
                    _logger.info("XMODEM: receiver ready (0x%02x) after %d reads", resp[0], wait_i)
                    # Drain any additional buffered 'C' or NAK chars
                    time.sleep(0.1)
                    if serial_port.in_waiting:
                        extra = serial_port.read(serial_port.in_waiting)
                        _logger.info("XMODEM: drained %d extra bytes after ready", len(extra))
                    ready = True
                    break
                elif resp[0] == CAN:
                    _logger.error("XMODEM: receiver cancelled before transfer (0x%02x)", resp[0])
                    return False
                else:
                    _logger.info("XMODEM: skipping byte 0x%02x during ready wait", resp[0])
            else:
                if wait_i % 10 == 0:
                    _logger.info("XMODEM: still waiting for ready (%d reads, no data)", wait_i)
        if not ready:
            _logger.error("XMODEM: receiver never signalled ready")
            return False

        # Send MD5 as sequence 0
        md5_packet = _build_packet(0, md5)
        if not _send_packet(serial_port, md5_packet, 0):
            return False

        # Send file data as sequences 1+
        offset = 0
        seq = 1
        while offset < total:
            # Check for cancellation
            if cancel_event and cancel_event.is_set():
                _logger.info("XMODEM: transfer cancelled by user")
                serial_port.write(bytes([CAN, CAN, CAN]))
                return None

            chunk = file_data[offset:offset + PACKET_DATA_SIZE]
            packet = _build_packet(seq, chunk)
            if not _send_packet(serial_port, packet, seq):
                return False

            if progress_callback:
                bytes_sent = min(offset + PACKET_DATA_SIZE, total)
                progress_callback(bytes_sent, total)

            offset += PACKET_DATA_SIZE
            seq += 1

        # Send EOT to finish (retry until ACK, like Carvera Controller)
        for eot_try in range(MAX_RETRIES):
            serial_port.write(bytes([EOT]))
            resp = serial_port.read(1)
            if resp and resp[0] == ACK:
                _logger.info("XMODEM: transfer complete")
                if progress_callback:
                    progress_callback(total, total)
                return True
            _logger.info("XMODEM: EOT not ACKed (attempt %d), retrying", eot_try + 1)

        _logger.warning("XMODEM: EOT was not ACKed after %d attempts", MAX_RETRIES)
        return False

    finally:
        serial_port.timeout = old_timeout


def _drain_noise(serial_port):
    """Read and discard non-protocol bytes (e.g. firmware 'Info: ...' text).

    Returns the first protocol byte (ACK/NAK/CAN) found, or None on timeout.
    """
    noise = bytearray()
    while True:
        b = serial_port.read(1)
        if not b:
            break  # timeout
        if b[0] in (ACK, NAK, CAN):
            if noise:
                _logger.info("XMODEM: drained %d noise bytes: %s", len(noise), noise.decode("ascii", errors="replace"))
            return b[0]
        noise.append(b[0])
    if noise:
        _logger.info("XMODEM: drained %d noise bytes (no protocol byte followed): %s", len(noise), noise.decode("ascii", errors="replace"))
    return None


def _send_packet(serial_port, packet, seq):
    """Send a single packet with retries. Returns True on ACK, False on failure."""
    for retry in range(MAX_RETRIES):
        serial_port.write(packet)

        resp = serial_port.read(1)
        if not resp:
            _logger.info("XMODEM: timeout waiting for ACK (packet %d, retry %d)", seq, retry)
            continue

        byte = resp[0]

        # If we got a non-protocol byte (e.g. 'I' from "Info: upload"),
        # drain remaining text and look for the real ACK/NAK/CAN after it.
        if byte not in (ACK, NAK, CAN):
            noise = bytearray([byte])
            found = _drain_noise(serial_port)
            if found is not None:
                _logger.info("XMODEM: skipped noise before response on packet %d: %s", seq, noise.decode("ascii", errors="replace"))
                byte = found
            else:
                _logger.info("XMODEM: unexpected noise on packet %d, retrying (%d): %s", seq, retry, noise.decode("ascii", errors="replace"))
                continue

        if byte == ACK:
            return True
        elif byte == NAK:
            _logger.info("XMODEM: NAK on packet %d, retrying (%d)", seq, retry)
        elif byte == CAN:
            _logger.error("XMODEM: transfer cancelled by receiver (0x%02x)", byte)
            return False

    _logger.error("XMODEM: max retries on packet %d", seq)
    return False
