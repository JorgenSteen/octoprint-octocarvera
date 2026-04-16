# coding=utf-8
"""Carvera binary framing protocol for firmware 1.0.5+.

Firmware 1.0.5 requires all commands (both WiFi TCP and USB serial) to be
wrapped in binary frames. The GRBL commands inside are unchanged — same ?,
version, G-code, etc. — just wrapped in a frame with header, length, type,
CRC-16, and trailer.

Frame format:
    [86 68] [len_hi len_lo] [type] [payload...] [crc_hi crc_lo] [55 AA]

Where:
    - Header: always 0x86 0x68
    - Length: big-endian uint16 = 1 (type) + len(payload) + 2 (CRC)
    - Type: message type identifier
    - CRC-16: XMODEM (poly 0x1021, init 0x0000) over [len_hi, len_lo, type, payload]
    - Trailer: always 0x55 0xAA

Decoded from Wireshark capture of official Makera Controller (2026-04-10).
Verified working over USB serial on Carvera Air fw 1.0.5 (2026-04-10).
"""

import struct
import logging
import threading
import time

_logger = logging.getLogger("octoprint.plugins.octocarvera.binary")

# Frame constants
HEADER = b'\x86\x68'
TRAILER = b'\x55\xAA'

# Message types — Controller to Carvera
TYPE_STATUS_QUERY = 0xa1
TYPE_TEXT_CMD = 0xa2
TYPE_DOWNLOAD = 0xb0
TYPE_CANCEL_DOWNLOAD = 0xb5

# Message types — Carvera to Controller
TYPE_STATUS_RESP = 0x81
TYPE_TEXT_RESP = 0x90
TYPE_FILE_HASH = 0xb1

# GRBL realtime command characters that must bypass binary framing.
# Note: '?' (status query) is NOT here — it works via binary frame type 0xa1,
# and sending it raw would produce a plain-text response that the wrapper can't parse.
_REALTIME_CHARS = frozenset(b'!~\x18\x19')


def crc16_xmodem(data):
    """CRC-16/XMODEM: polynomial 0x1021, initial value 0x0000."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return crc


def build_frame(type_byte, payload=b''):
    """Build a binary-framed message.

    Args:
        type_byte: Message type (e.g. TYPE_STATUS_QUERY, TYPE_TEXT_CMD)
        payload: Command bytes (e.g. b'?' or b'version')

    Returns:
        Complete frame bytes ready to send.
    """
    length = 1 + len(payload) + 2  # type + payload + crc
    len_bytes = struct.pack('>H', length)
    crc_input = len_bytes + bytes([type_byte]) + payload
    crc = crc16_xmodem(crc_input)
    crc_bytes = struct.pack('>H', crc)
    return HEADER + len_bytes + bytes([type_byte]) + payload + crc_bytes + TRAILER


def build_command_frame(text):
    """Build a binary frame for a text command.

    Automatically selects the correct message type:
    - '?' -> TYPE_STATUS_QUERY (0xa1)
    - Everything else -> TYPE_TEXT_CMD (0xa2)

    Args:
        text: Command string (e.g. '?', 'version', 'G0 X10')

    Returns:
        Complete frame bytes.
    """
    text = text.strip()
    if text == '?':
        return build_frame(TYPE_STATUS_QUERY, b'?')
    return build_frame(TYPE_TEXT_CMD, text.encode('latin-1'))


def parse_frame(data, offset=0):
    """Try to parse one binary frame starting at offset.

    Args:
        data: bytes or bytearray buffer
        offset: position to start looking for a frame

    Returns:
        (type_byte, payload_bytes, next_offset) on success,
        or None if no complete valid frame found.
    """
    # Find header
    idx = data.find(HEADER, offset)
    if idx == -1 or idx + 8 > len(data):  # minimum frame = 9 bytes
        return None

    # Read length
    length = struct.unpack('>H', data[idx+2:idx+4])[0]
    frame_end = idx + 4 + length + 2  # header(2) + len(2) + data(length) + trailer(2)

    if frame_end > len(data):
        return None  # incomplete frame

    # Check trailer
    if data[frame_end-2:frame_end] != TRAILER:
        return None

    # Verify CRC
    crc_input = data[idx+2:frame_end-4]  # len(2) + type(1) + payload
    expected_crc = struct.unpack('>H', data[frame_end-4:frame_end-2])[0]
    actual_crc = crc16_xmodem(crc_input)
    if actual_crc != expected_crc:
        _logger.warning("CRC mismatch: expected 0x%04x, got 0x%04x", expected_crc, actual_crc)
        return None

    type_byte = data[idx+4]
    payload = data[idx+5:frame_end-4]
    return (type_byte, bytes(payload), frame_end)


class BinaryFrameSerial:
    """Serial port wrapper that transparently handles Carvera binary framing.

    Wraps a real pyserial.Serial object. Intercepts write() to wrap outgoing
    commands in binary frames, and intercepts readline() to unwrap incoming
    binary frames back into text lines.

    OctoPrint sees plain text lines; the Carvera sees binary frames.
    """

    def __init__(self, real_serial):
        self._serial = real_serial
        self._buffer = bytearray()
        self._line_queue = []  # Parsed text lines ready to return
        self._logger = logging.getLogger("octoprint.plugins.octocarvera.binary.serial")
        # PySerial's write() is NOT thread-safe. OctoPrint's comm loop and
        # our own keepalive thread both call write() on this wrapper, and
        # interleaved bytes corrupt the binary frame → firmware drops it
        # silently. Serialize every write through this lock so frames go
        # out atomically. Also guards _line_queue against readline/write
        # races (the queue is mutated from both).
        self._write_lock = threading.RLock()

    def write(self, data):
        """Wrap outgoing data in a binary frame and send.

        Thread-safe: the whole write (including frame construction and
        synthetic-ok enqueue) happens under ``_write_lock`` so that
        OctoPrint's comm loop and our keepalive thread can't interleave
        bytes on the underlying pyserial port.
        """
        if not data:
            return 0

        with self._write_lock:
            # Realtime commands (!, ~, \x18, \x19) must be sent as raw bytes,
            # not wrapped in binary frames. The firmware processes these
            # immediately via its interrupt handler — framing them would
            # break that. None come from OctoPrint's command queue, so no
            # ok synthesis.
            if isinstance(data, (bytes, bytearray)) and len(data) == 1 and data[0] in _REALTIME_CHARS:
                self._logger.debug("TX realtime: 0x%02x", data[0])
                return self._serial.write(data)

            # Out-of-band status query: keepalive sends `?` as a single raw
            # byte via comm._serial.write(b"?"), bypassing OctoPrint's
            # command queue. Binary fw 1.0.5 needs it wrapped as a 0xa1
            # frame to get a response, but we must NOT synthesize an ok —
            # no command is waiting on one. OctoPrint-side `?` arrives as
            # multi-byte (with newline) and falls through to the text path.
            if isinstance(data, (bytes, bytearray)) and len(data) == 1 and data[0] == 0x3f:
                frame = build_command_frame('?')
                self._logger.debug("TX status query (no ok): %s", frame.hex())
                return self._serial.write(frame)

            # Decode to text for processing
            if isinstance(data, (bytes, bytearray)):
                text = data.decode('latin-1', errors='replace')
            else:
                text = data

            text = text.strip()
            if not text:
                return len(data)

            # Build and send the binary frame
            frame = build_command_frame(text)
            self._logger.debug("TX frame (%d bytes): %s -> %s", len(frame), repr(text), frame.hex())
            result = self._serial.write(frame)

            # Firmware 1.0.5 binary protocol has no "ok" ack:
            #   - G0/G1/etc. get ZERO response frames (only state transitions)
            #   - version/$G/echo get one 0x90 with the response text (no ok)
            #   - errors get multiple 0x90 frames (error: + message)
            #   - $X response embeds "ok" inside its payload
            # So we synthesize one ok per successful write() and never ack
            # from response frames. Responses flow through as plain lines.
            self._line_queue.append(b'ok\n')
            return result

    def readline(self):
        """Read a binary frame, extract text payload, return as a line.

        Returns the payload text with newline appended (matching OctoPrint's
        expectation from readline()). Returns empty bytes on timeout.
        """
        # Return queued lines first
        if self._line_queue:
            return self._line_queue.pop(0)

        # Read bytes and try to parse frames
        deadline = time.monotonic() + (self._serial.timeout or 5.0)

        while time.monotonic() < deadline:
            # Read available bytes
            waiting = self._serial.in_waiting
            if waiting > 0:
                chunk = self._serial.read(waiting)
                if chunk:
                    self._buffer.extend(chunk)
            else:
                # Brief read with timeout to avoid busy-waiting
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                old_timeout = self._serial.timeout
                self._serial.timeout = min(0.1, remaining)
                chunk = self._serial.read(1)
                self._serial.timeout = old_timeout
                if chunk:
                    self._buffer.extend(chunk)

            # Try to parse all complete frames from buffer
            self._parse_buffer()

            if self._line_queue:
                return self._line_queue.pop(0)

        # Timeout — return empty
        return b''

    def _parse_buffer(self):
        """Parse all complete frames from the internal buffer."""
        offset = 0
        while True:
            result = parse_frame(self._buffer, offset)
            if result is None:
                break
            type_byte, payload, next_offset = result

            # Extract text from payload
            try:
                text = payload.decode('latin-1')
            except Exception:
                text = payload.decode('ascii', errors='replace')

            # Split multi-line payloads into individual lines.
            # Binary frames can contain multiple \r\n-separated lines
            # (e.g. ls responses, or $X's "[Caution: Unlocked]\nok\n").
            # Strip any line that is exactly "ok" — we synthesize acks at
            # write time, so a bare "ok" inside a response would desync
            # OctoPrint's command counter.
            queued_any = False
            for subline in text.split('\n'):
                subline = subline.rstrip('\r')
                if not subline:
                    continue
                if subline.strip().lower() == 'ok':
                    continue
                encoded = (subline + '\n').encode('latin-1')
                self._line_queue.append(encoded)
                queued_any = True

            if queued_any:
                self._logger.debug("RX frame type=0x%02x: %s", type_byte, text[:100].replace('\n', '\\n'))

            offset = next_offset

        # Remove consumed bytes from buffer
        if offset > 0:
            del self._buffer[:offset]

        # Safety: prevent buffer from growing unbounded if we can't parse anything
        # (e.g. corrupted data). Keep last 1024 bytes.
        if len(self._buffer) > 4096:
            self._logger.warning("Buffer overflow (%d bytes), trimming", len(self._buffer))
            del self._buffer[:-1024]

    def close(self):
        """Close the underlying serial port."""
        return self._serial.close()

    @property
    def timeout(self):
        return self._serial.timeout

    @timeout.setter
    def timeout(self, value):
        self._serial.timeout = value

    @property
    def baudrate(self):
        return self._serial.baudrate

    @baudrate.setter
    def baudrate(self, value):
        self._serial.baudrate = value

    @property
    def port(self):
        return self._serial.port

    @property
    def is_open(self):
        return self._serial.is_open

    @property
    def in_waiting(self):
        return self._serial.in_waiting or len(self._line_queue)

    @property
    def write_timeout(self):
        return getattr(self._serial, 'write_timeout', None)

    @write_timeout.setter
    def write_timeout(self, value):
        self._serial.write_timeout = value

    @property
    def name(self):
        return getattr(self._serial, 'name', str(self._serial.port))

    def reset_input_buffer(self):
        self._serial.reset_input_buffer()
        self._buffer.clear()
        self._line_queue.clear()

    def reset_output_buffer(self):
        self._serial.reset_output_buffer()

    def flush(self):
        self._serial.flush()

    def flushInput(self):
        self.reset_input_buffer()

    def flushOutput(self):
        self.reset_output_buffer()

    def __getattr__(self, name):
        """Delegate any other attribute access to the real serial port."""
        return getattr(self._serial, name)
