"""File operations for the Carvera SD card.

Handles file listing, path encoding, and upload coordination.
Uses shell commands (ls, upload) with XMODEM transfer — not standard M-codes.
"""

import logging
import time

from .carvera_xmodem import xmodem_send

_logger = logging.getLogger("octoprint.plugins.octocarvera.files")

# ~~~ Path encoding ~~~
# Characters that conflict with GRBL real-time commands must be escaped.

_ENCODE_MAP = {
    " ": "\x01",
    "?": "\x02",
    "&": "\x03",
    "!": "\x04",
    "~": "\x05",
}

_DECODE_MAP = {v: k for k, v in _ENCODE_MAP.items()}

# Directories to hide from file listings (firmware internals)
_HIDDEN_DIRS = {".md5", ".lz"}

# Response terminators
_EOT = 0x04  # Success
_CAN = 0x18  # Error (also 0x16 in some firmware versions)
_CAN_ALT = 0x16


def encode_path(path):
    """Encode a file path for sending to the Carvera."""
    result = path
    for char, encoded in _ENCODE_MAP.items():
        result = result.replace(char, encoded)
    return result


def decode_path(encoded):
    """Decode a path received from the Carvera."""
    result = encoded
    for char, decoded in _DECODE_MAP.items():
        result = result.replace(char, decoded)
    return result


def parse_ls_response(lines):
    """Parse ls -e -s output lines into structured entries.

    Each line: "<name>[/] <size> <YYYYMMDDHHmmSS>"
    Trailing / on name = directory. Size=0 for dirs.

    Returns list of dicts: {name, is_dir, size, date}
    """
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip command echo (firmware echoes back the ls command)
        if line.startswith("ls "):
            continue
        # Skip firmware completion/status messages
        if "finished" in line.lower():
            continue

        parts = line.rsplit(" ", 2)
        if len(parts) < 3:
            continue

        raw_name, size_str, date_str = parts

        is_dir = raw_name.endswith("/")
        name = decode_path(raw_name.rstrip("/"))

        # Skip hidden directories
        if name in _HIDDEN_DIRS:
            continue

        try:
            size = int(size_str)
        except ValueError:
            size = 0

        # Format date: "20240315143022" -> "2024-03-15 14:30"
        date = date_str
        if len(date_str) >= 12:
            date = "{}-{}-{} {}:{}".format(
                date_str[0:4], date_str[4:6], date_str[6:8],
                date_str[8:10], date_str[10:12],
            )

        entries.append({
            "name": name,
            "is_dir": is_dir,
            "size": size,
            "date": date,
        })

    # Sort: directories first, then alphabetically
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def list_files(serial_port, path="/sd/", timeout=10.0):
    """List files on the Carvera SD card.

    Sends 'ls -e -s <path>' and reads lines until EOT/CAN/timeout.
    Must be called with exclusive serial access (keepalive paused).

    Returns list of dicts: {name, is_dir, size, date}
    """
    # Flush any stale data
    if serial_port.in_waiting:
        serial_port.read(serial_port.in_waiting)

    # Send ls command
    encoded = encode_path(path)
    cmd = "ls -e -s {}\n".format(encoded)
    serial_port.write(cmd.encode("latin-1"))
    _logger.debug("Sent: %s", cmd.strip())

    lines = []
    deadline = time.monotonic() + timeout
    old_timeout = serial_port.timeout
    serial_port.timeout = 1.0

    try:
        while time.monotonic() < deadline:
            raw = serial_port.readline()
            if not raw:
                continue

            # Check for terminators
            if _EOT in raw or _CAN in raw or _CAN_ALT in raw:
                break

            line = raw.decode("latin-1", errors="replace").strip()
            if line and line != "ok":
                lines.append(line)
    finally:
        serial_port.timeout = old_timeout

    _logger.debug("ls returned %d entries", len(lines))
    return parse_ls_response(lines)


def upload_file(serial_port, remote_path, file_data, progress_callback=None, cancel_event=None):
    """Upload a file to the Carvera SD card via XMODEM.

    Sends 'upload <path>' then performs XMODEM-128 transfer.
    Must be called with exclusive serial access (keepalive paused).

    Returns True on success, False on failure.
    """
    # Flush stale data
    if serial_port.in_waiting:
        serial_port.read(serial_port.in_waiting)

    # Send upload command
    encoded = encode_path(remote_path)
    cmd = "upload {}\n".format(encoded)
    serial_port.write(cmd.encode("latin-1"))
    _logger.info("Upload starting: %s (%d bytes)", remote_path, len(file_data))

    # Brief pause for firmware to enter XMODEM receive mode
    time.sleep(0.5)

    # Perform XMODEM transfer
    success = xmodem_send(serial_port, file_data, progress_callback=progress_callback, cancel_event=cancel_event)

    if success:
        _logger.info("Upload complete: %s", remote_path)
    else:
        _logger.error("Upload failed: %s", remote_path)

    return success
