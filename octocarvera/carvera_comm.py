# coding=utf-8
"""Communication strategy classes for Carvera wire protocols.

OctoCarvera supports two transport formats:

    * Plain-text GRBL — used by the community firmware and stock
      firmware <= 1.0.3. Realtime control characters (0x18, !, ~) work.
    * Binary framing — required by Makera stock firmware 1.0.5+. Every
      command must be wrapped in an 86 68 / type / CRC / 55 AA frame,
      and the firmware silently drops realtime control bytes.

The ``Communication`` class hierarchy centralises every decision that
differs between these formats so the rest of the plugin can invoke
polymorphic methods instead of branching on ``protocol_mode``.

A Communication instance is a thin strategy object. It is handed the
plugin's low-level send primitives at construction time and uses them
to dispatch the appropriate byte sequence for each out-of-band action.
The instance is rebuilt whenever ``protocol_mode`` changes in settings.

Note on file operations
-----------------------
File listing, delete, mkdir and upload commands in ``carvera_files.py``
currently bypass OctoPrint's command queue and write directly to
``comm._serial``. In binary mode that underlying object IS the
``BinaryFrameSerial`` wrapper, so text commands like ``ls -e -s`` are
transparently framed on the way out — there is no extra plumbing to
add here. XMODEM upload is the one exception: it fully disconnects
OctoPrint and opens its own raw ``pyserial.Serial``, so framing is NOT
applied to the ``upload`` handshake. That is a pre-existing concern
tracked separately from this refactor.
"""

from abc import ABC, abstractmethod

from .carvera_protocol import (
    CMD_UNLOCK,
    CMD_VERSION,
    INIT_SEQUENCE,
    RT_CYCLE_START,
    RT_FEED_HOLD,
    RT_SOFT_RESET,
)


class Communication(ABC):
    """Strategy object: what bytes to send for each out-of-band action."""

    name = "base"

    def __init__(self, send_command_fn, send_realtime_fn, send_raw_text_fn, logger):
        self._send_command = send_command_fn          # via printer.commands() queue
        self._send_realtime = send_realtime_fn        # raw bytes to comm._serial
        self._send_raw_text = send_raw_text_fn        # text bytes to comm._serial, bypass queue
        self._logger = logger

    # Alarm recovery. Plain-text community firmware doesn't send `ok\n`
    # after `$X` when no alarm is active (silently no-op), which would
    # leave the OctoPrint command queue waiting forever. Plain-text
    # override uses _send_raw_text to bypass the queue. Binary mode
    # keeps the default (queued) path because BinaryFrameSerial
    # synthesizes its own ack on write.
    def unlock(self):
        self._send_command(CMD_UNLOCK)

    # ~~~ Serial factory ~~~

    def serial_factory(self, port, baudrate, connection_timeout):
        """Return a serial-like object for OctoPrint's comm layer.

        Default: return None so OctoPrint opens the port normally.
        Binary framing overrides this to install ``BinaryFrameSerial``.
        """
        return None

    # ~~~ Out-of-band control commands ~~~

    @abstractmethod
    def estop(self):
        """Emergency stop."""

    @abstractmethod
    def pause(self):
        """Pause a running job (feed hold)."""

    @abstractmethod
    def resume(self):
        """Resume a paused job (cycle start)."""

    @abstractmethod
    def cancel(self):
        """Cancel a running job (same intent as estop, called from _cancel_job)."""

    # ~~~ Connection lifecycle ~~~

    def on_connect_init(self, send_init_flag):
        """Run connection-time init. Override in subclasses to send wake bytes."""
        if send_init_flag:
            self._send_command(CMD_VERSION)

    def post_cancel_cleanup(self):
        """Run after a cancel settles. Base: unlock."""
        self._send_command(CMD_UNLOCK)


class PlainTextCommunication(Communication):
    """Community firmware / stock firmware <= 1.0.3.

    Realtime control bytes work. Connection init needs the classic
    buffer-clear sequence (``\\n;\\n``) to wake the firmware.
    """

    name = "plain_text"

    def estop(self):
        self._send_realtime(RT_SOFT_RESET)

    def pause(self):
        self._send_realtime(RT_FEED_HOLD)

    def resume(self):
        self._send_realtime(RT_CYCLE_START)

    def cancel(self):
        self._send_realtime(RT_SOFT_RESET)

    def on_connect_init(self, send_init_flag):
        if send_init_flag:
            self._send_command(INIT_SEQUENCE)
            # Community firmware 2.0.2c-RC2 replies to `version` without a
            # trailing `ok\n`, which would leave a phantom pending command
            # in OctoPrint's queue and eventually trigger a comm timeout.
            # Write it directly to the serial so there's no outstanding
            # command waiting for an ack — the received_hook still parses
            # the `version = ...` response and populates firmware state.
            self._send_raw_text(CMD_VERSION)

    def unlock(self):
        # $X in Idle state on community firmware 2.0.2c-RC2 returns
        # nothing — no ok. Sending it via the command queue would
        # deadlock OctoPrint waiting for an ack forever. Bypass the
        # queue.
        self._send_raw_text(CMD_UNLOCK)

    def post_cancel_cleanup(self):
        self._send_raw_text(CMD_UNLOCK)
        self._send_command(INIT_SEQUENCE)


class BinaryCommunication(Communication):
    """Makera stock firmware 1.0.5+.

    Every command must be wrapped in a binary frame. The firmware
    silently drops raw realtime bytes (``!``, ``~``, ``0x18``) — use
    the corresponding text commands (``suspend``, ``resume``, ``M112``,
    ``abort``) instead. Connection init needs no buffer clear; framing
    is enough.

    Note on ``abort`` vs ``M112``: Smoothieware's ``abort`` finishes the
    *current* move then stops the queue — it's a graceful cancel, not an
    emergency halt. ``M112`` halts motion mid-flight and puts the machine
    into Halt state (recovery via ``M999`` or ``$X``). We use ``M112``
    for estop and ``abort`` for cancel so each button matches the user's
    intent. Verified on fw 1.0.5: ``M112`` stopped a 50 mm move at 1.9 mm.
    """

    name = "binary"

    def serial_factory(self, port, baudrate, connection_timeout):
        import serial as pyserial

        from .carvera_binary import BinaryFrameSerial

        if port is None or port == "AUTO":
            # AUTO port detection doesn't work with the binary wrapper.
            self._logger.warning(
                "Binary protocol mode requires an explicit serial port (got %r)", port
            )
            return None

        try:
            ser = pyserial.Serial(
                str(port),
                baudrate,
                timeout=connection_timeout,
                writeTimeout=10000,
            )
        except Exception:
            self._logger.exception(
                "Failed to open serial port %s for binary framing", port
            )
            return None

        self._logger.info(
            "Binary protocol mode: wrapping serial port %s @ %d", port, baudrate
        )
        return BinaryFrameSerial(ser)

    def estop(self):
        # M112 = true emergency halt on Smoothieware. We must bypass
        # OctoPrint's command queue to send it: OctoPrint intercepts
        # M112 at the comm layer and closes the serial port on top of
        # sending the command. Writing directly to comm._serial keeps
        # the binary wrapper in the loop (frames the command) without
        # triggering OctoPrint's kill-switch. Recovery is via M999/$X;
        # we translate OctoPrint's M999 to $X in sending_gcode_hook.
        self._send_raw_text("M112")

    def pause(self):
        self._send_command("suspend")

    def resume(self):
        self._send_command("resume")

    def cancel(self):
        # abort is the gentle "finish current move then stop queue" —
        # the right semantic for a job cancel, distinct from estop.
        self._send_command("abort")

    def on_connect_init(self, send_init_flag):
        if send_init_flag:
            self._send_command(CMD_VERSION)

    def post_cancel_cleanup(self):
        self._send_command(CMD_UNLOCK)


def build_communication(
    protocol_mode, send_command_fn, send_realtime_fn, send_raw_text_fn, logger
):
    """Factory: pick the right subclass for the current protocol_mode setting."""
    args = (send_command_fn, send_realtime_fn, send_raw_text_fn, logger)
    if protocol_mode == "binary":
        return BinaryCommunication(*args)
    return PlainTextCommunication(*args)
