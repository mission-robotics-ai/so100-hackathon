"""Minimal Feetech STS3215 bus reader for the SO-100 arm.

Uses ``scservo_sdk`` (feetech-servo-sdk) sync-read to pull the whole
``Present_*`` control-table block for all 6 motors in a single bus
transaction — the Python equivalent of the per-register reads in
rerun-io/portugal ``src/robot.rs``, but fast enough for realtime logging.
"""

from __future__ import annotations

import contextlib
import glob
from dataclasses import dataclass

import scservo_sdk as scs

BAUD_RATE = 1_000_000
PROTOCOL_END = 0  # STS/SMS series byte order
TICKS_PER_REV = 4096


def detect_arm_ports() -> tuple[str, ...]:
    """Every SO-100 serial adapter currently on the bus (macOS device names)."""
    return tuple(sorted(glob.glob("/dev/cu.usbmodem*")))


def usb_id_from_port(port: str) -> str:
    return port.rsplit("usbmodem", 1)[-1]

# STS3215 control table: contiguous block covering every Present_* register.
ADDR_PRESENT_POSITION = 56  # 2 bytes, ticks (0..4095)
ADDR_PRESENT_SPEED = 58  # 2 bytes, sign-magnitude (bit 15), ticks/s
ADDR_PRESENT_LOAD = 60  # 2 bytes, sign-magnitude (bit 10), 0.1% units
ADDR_PRESENT_VOLTAGE = 62  # 1 byte, 0.1 V units
ADDR_PRESENT_TEMPERATURE = 63  # 1 byte, celsius
ADDR_PRESENT_CURRENT = 69  # 2 bytes, sign-magnitude (bit 15), 6.5 mA units
BLOCK_START = ADDR_PRESENT_POSITION
BLOCK_LENGTH = ADDR_PRESENT_CURRENT + 2 - BLOCK_START  # 15 bytes


@dataclass(frozen=True)
class MotorTelemetry:
    position_raw: int
    speed_ticks_s: float
    load_pct: float
    voltage_v: float
    temperature_c: float
    current_ma: float


def _sign_magnitude(value: int, sign_bit: int) -> int:
    magnitude = value & ((1 << sign_bit) - 1)
    return -magnitude if value & (1 << sign_bit) else magnitude


class FeetechBus:
    def __init__(self, port: str, motor_ids: tuple[int, ...] = (1, 2, 3, 4, 5, 6)) -> None:
        self.port = port
        self.motor_ids = motor_ids
        self.packet_handler = scs.PacketHandler(PROTOCOL_END)
        self._open()

    def _open(self) -> None:
        self.port_handler = scs.PortHandler(self.port)
        if not self.port_handler.openPort():
            raise RuntimeError(f"failed to open serial port {self.port}")
        if not self.port_handler.setBaudRate(BAUD_RATE):
            raise RuntimeError(f"failed to set baud rate {BAUD_RATE} on {self.port}")
        self.sync_read = scs.GroupSyncRead(self.port_handler, self.packet_handler, BLOCK_START, BLOCK_LENGTH)
        for motor_id in self.motor_ids:
            self.sync_read.addParam(motor_id)

    def reconnect(self) -> None:
        """Reopen the serial port after a USB drop (device must be plugged back in)."""
        with contextlib.suppress(OSError):  # closing a vanished device can itself fail
            self.close()
        self._open()

    def read_telemetry(self) -> list[MotorTelemetry]:
        try:
            comm = self.sync_read.txRxPacket()
        except OSError as error:  # pyserial raises SerialException (an OSError subclass) on USB drops
            raise RuntimeError(f"{self.port}: bus read failed (device disconnected?): {error}") from error
        if comm != scs.COMM_SUCCESS:
            raise RuntimeError(f"{self.port}: sync read failed: {self.packet_handler.getTxRxResult(comm)}")

        telemetry: list[MotorTelemetry] = []
        for motor_id in self.motor_ids:
            if not self.sync_read.isAvailable(motor_id, BLOCK_START, BLOCK_LENGTH):
                raise RuntimeError(f"{self.port}: motor {motor_id} missing from sync read reply")
            telemetry.append(
                MotorTelemetry(
                    position_raw=self.sync_read.getData(motor_id, ADDR_PRESENT_POSITION, 2),
                    speed_ticks_s=float(_sign_magnitude(self.sync_read.getData(motor_id, ADDR_PRESENT_SPEED, 2), 15)),
                    load_pct=_sign_magnitude(self.sync_read.getData(motor_id, ADDR_PRESENT_LOAD, 2), 10) * 0.1,
                    voltage_v=self.sync_read.getData(motor_id, ADDR_PRESENT_VOLTAGE, 1) * 0.1,
                    temperature_c=float(self.sync_read.getData(motor_id, ADDR_PRESENT_TEMPERATURE, 1)),
                    current_ma=_sign_magnitude(self.sync_read.getData(motor_id, ADDR_PRESENT_CURRENT, 2), 15) * 6.5,
                )
            )
        return telemetry

    def close(self) -> None:
        self.port_handler.closePort()
