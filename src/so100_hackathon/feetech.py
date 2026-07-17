"""Minimal Feetech STS3215 bus reader for the SO-100 arm.

Uses ``scservo_sdk`` (feetech-servo-sdk) sync-read to pull the whole
``Present_*`` control-table block for all 6 motors in a single bus
transaction — the Python equivalent of the per-register reads in
rerun-io/portugal ``src/robot.rs``, but fast enough for realtime logging.
"""

from __future__ import annotations

import contextlib
import glob
import threading
import time
from dataclasses import dataclass

import scservo_sdk as scs

from so100_hackathon.calibration import software_homed, software_mechanical

BAUD_RATE = 1_000_000
PROTOCOL_END = 0  # STS/SMS series byte order


def detect_arm_ports() -> tuple[str, ...]:
    """Every SO-100 serial adapter currently on the bus (macOS device names)."""
    return tuple(sorted(glob.glob("/dev/cu.usbmodem*")))


def usb_id_from_port(port: str) -> str:
    return port.rsplit("usbmodem", 1)[-1]


# STS3215 control table: write-side registers (teleop drives the follower with these).
ADDR_MIN_POSITION_LIMIT = 9  # 2 bytes; servo-side motion limit, written from the calibration sweep
ADDR_MAX_POSITION_LIMIT = 11  # 2 bytes
ADDR_MAX_TORQUE_LIMIT = 16  # 2 bytes, 0..1000 (0.1% units)
ADDR_P_COEFFICIENT = 21  # 1 byte, position-loop P gain (servo default 32)
ADDR_D_COEFFICIENT = 22  # 1 byte
ADDR_I_COEFFICIENT = 23  # 1 byte
ADDR_PROTECTION_CURRENT = 28  # 2 bytes, 6.5 mA units
ADDR_HOMING_OFFSET = 31  # 2 bytes, sign-magnitude (bit 11); present = mechanical - offset (mod 4096)
ADDR_OVERLOAD_TORQUE = 36  # 1 byte, % of torque kept once overload protection trips
ADDR_TORQUE_ENABLE = 40  # 1 byte, 0/1
ADDR_GOAL_POSITION = 42  # 2 bytes, ticks
ADDR_LOCK = 55  # 1 byte; lerobot toggles it together with torque

GRIPPER_MOTOR_ID = 6

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


def _encode_sign_magnitude(value: int, sign_bit: int) -> int:
    return abs(value) | ((1 << sign_bit) if value < 0 else 0)


class FeetechBus:
    def __init__(self, port: str, motor_ids: tuple[int, ...] = (1, 2, 3, 4, 5, 6)) -> None:
        self.port = port
        self.motor_ids = motor_ids
        self.packet_handler = scs.PacketHandler(PROTOCOL_END)
        # Serializes bus transactions across threads: the sdk's own busy flag drops after
        # the FIRST reply of a sync read, so without this a concurrent write can interleave
        # with the remaining replies on the half-duplex line.
        self.lock = threading.Lock()
        # Software homing: raw mechanical middle per motor (same order as motor_ids), or None for
        # servo-homed / uncalibrated arms. When set, read_telemetry returns HOMED position_raw and
        # sync_write_goal takes HOMED goals — emulating a servo Homing_Offset this firmware ignores.
        self._homing_middle: list[int] | None = None
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
        self.sync_write = scs.GroupSyncWrite(self.port_handler, self.packet_handler, ADDR_GOAL_POSITION, 2)

    def reconnect(self) -> None:
        """Reopen the serial port after a USB drop (device must be plugged back in)."""
        with contextlib.suppress(OSError):  # closing a vanished device can itself fail
            self.close()
        self._open()

    def read_telemetry(self) -> list[MotorTelemetry]:
        with self.lock:
            try:
                comm = self.sync_read.txRxPacket()
            except OSError as error:  # pyserial raises SerialException (an OSError subclass) on USB drops
                raise RuntimeError(f"{self.port}: bus read failed (device disconnected?): {error}") from error
        if comm != scs.COMM_SUCCESS:
            raise RuntimeError(f"{self.port}: sync read failed: {self.packet_handler.getTxRxResult(comm)}")

        telemetry: list[MotorTelemetry] = []
        for index, motor_id in enumerate(self.motor_ids):
            if not self.sync_read.isAvailable(motor_id, BLOCK_START, BLOCK_LENGTH):
                raise RuntimeError(f"{self.port}: motor {motor_id} missing from sync read reply")
            position_raw = self.sync_read.getData(motor_id, ADDR_PRESENT_POSITION, 2)
            if self._homing_middle is not None:  # software homing: report the homed tick a servo offset would have
                position_raw = software_homed(position_raw, self._homing_middle[index])
            telemetry.append(
                MotorTelemetry(
                    position_raw=position_raw,
                    speed_ticks_s=float(_sign_magnitude(self.sync_read.getData(motor_id, ADDR_PRESENT_SPEED, 2), 15)),
                    load_pct=_sign_magnitude(self.sync_read.getData(motor_id, ADDR_PRESENT_LOAD, 2), 10) * 0.1,
                    voltage_v=self.sync_read.getData(motor_id, ADDR_PRESENT_VOLTAGE, 1) * 0.1,
                    temperature_c=float(self.sync_read.getData(motor_id, ADDR_PRESENT_TEMPERATURE, 1)),
                    current_ma=_sign_magnitude(self.sync_read.getData(motor_id, ADDR_PRESENT_CURRENT, 2), 15) * 6.5,
                )
            )
        return telemetry

    def _write_register(self, motor_id: int, address: int, value: int, size: int, *, attempts: int = 3) -> None:
        write = self.packet_handler.write1ByteTxRx if size == 1 else self.packet_handler.write2ByteTxRx
        comm, error = scs.COMM_TX_FAIL, 0
        with self.lock:
            for attempt in range(attempts):
                try:
                    comm, error = write(self.port_handler, motor_id, address, value)
                except OSError as os_error:
                    raise RuntimeError(f"{self.port}: bus write failed (device disconnected?): {os_error}") from os_error
                if comm == scs.COMM_SUCCESS and error == 0:
                    return
                # EEPROM commits (e.g. the gain registers) can delay the servo's status reply
                # past the packet timeout even though the write landed; settle and retry.
                if attempt < attempts - 1:
                    time.sleep(0.01)
        if comm != scs.COMM_SUCCESS:
            raise RuntimeError(f"{self.port}: write to motor {motor_id} addr {address} failed: {self.packet_handler.getTxRxResult(comm)}")
        raise RuntimeError(f"{self.port}: motor {motor_id} rejected write to addr {address}: {self.packet_handler.getRxPacketError(error)}")

    def _read_register(self, motor_id: int, address: int, size: int, *, attempts: int = 3) -> int:
        read = self.packet_handler.read1ByteTxRx if size == 1 else self.packet_handler.read2ByteTxRx
        value, comm, error = 0, scs.COMM_TX_FAIL, 0
        with self.lock:
            for attempt in range(attempts):
                try:
                    value, comm, error = read(self.port_handler, motor_id, address)
                except OSError as os_error:
                    raise RuntimeError(f"{self.port}: bus read failed (device disconnected?): {os_error}") from os_error
                except IndexError:  # sdk bug: a timed-out reply still gets indexed
                    comm, error = scs.COMM_RX_TIMEOUT, 0
                if comm == scs.COMM_SUCCESS and error == 0:
                    return value
                if attempt < attempts - 1:
                    time.sleep(0.01)
        raise RuntimeError(f"{self.port}: read from motor {motor_id} addr {address} failed: {self.packet_handler.getTxRxResult(comm)}")

    def _write_register_verified(self, motor_id: int, address: int, value: int, size: int) -> None:
        """Write an EEPROM register and confirm it actually landed, working around a
        re-engaging Lock.

        Homing_Offset / Position_Limit are EEPROM, gated by the Lock register (addr 55).
        The caller clears Lock once (``set_torque(False)``), but on some STS3215 firmware
        batches Lock re-engages after the first EEPROM write of a sequence, so the servo
        ACKs every write after it (protocol success) while silently discarding the value —
        exactly the "or a write was lost" case the calibration code names. Seen on real
        hardware: motor 1 took, motors 2-6 kept their old offset (~raw position).

        So read the register back; on mismatch, explicitly clear THIS motor's Lock and
        rewrite, up to 3 attempts. ``value`` is the raw register word (sign-magnitude
        already encoded by the caller), so the raw read-back compares directly. Still
        wrong after 3 tries -> raise (fail loud rather than persist a dropped write).
        """
        for attempt in range(3):
            if attempt:  # a prior write was silently dropped: Lock re-engaged, clear it for this motor
                self._write_register(motor_id, ADDR_LOCK, 0, 1)
            self._write_register(motor_id, address, value, size)
            if self._read_register(motor_id, address, size) == value:
                return
        readback = self._read_register(motor_id, address, size)
        raise RuntimeError(
            f"{self.port}: motor {motor_id} EEPROM write to addr {address} not honored after 3 attempts "
            f"(wrote {value}, reads {readback}) — servo Lock stuck engaged?"
        )

    def read_positions(self, *, attempts: int = 1) -> list[int]:
        """Raw tick positions for all motors. Extra attempts cover reads right after EEPROM
        writes, whose late status replies can desync the next transaction."""
        for _ in range(attempts - 1):
            try:
                return [t.position_raw for t in self.read_telemetry()]
            except RuntimeError:
                time.sleep(0.05)
        return [t.position_raw for t in self.read_telemetry()]

    def set_torque(self, enabled: bool) -> None:
        """Enable/disable torque on every motor (Lock toggled alongside, as lerobot does).

        Disabling is best-effort across ALL motors before raising: a transient failure on
        one servo must not leave the ones after it torqued (this runs on the exit path).
        """
        value = 1 if enabled else 0
        failures: list[str] = []
        for motor_id in self.motor_ids:
            try:
                self._write_register(motor_id, ADDR_TORQUE_ENABLE, value, 1)
                self._write_register(motor_id, ADDR_LOCK, value, 1)
            except RuntimeError as error:
                if enabled:
                    raise
                failures.append(str(error))
        if failures:
            raise RuntimeError(f"{self.port}: torque disable failed on {len(failures)} motor(s): {failures[0]}")

    def configure_follower_control(self) -> None:
        """Configure the servos the way lerobot's SO follower does. Call while torque is off.

        P=16 (vs default 32) avoids shakiness; I=0/D=32 are the servo defaults. The gripper
        additionally gets torque/current limits: closing on an object makes its goal
        unreachable, and without limits the servo stalls at full torque and can burn out.
        """
        for motor_id in self.motor_ids:
            self._write_register(motor_id, ADDR_P_COEFFICIENT, 16, 1)
            self._write_register(motor_id, ADDR_I_COEFFICIENT, 0, 1)
            self._write_register(motor_id, ADDR_D_COEFFICIENT, 32, 1)
        if GRIPPER_MOTOR_ID in self.motor_ids:
            self._write_register(GRIPPER_MOTOR_ID, ADDR_MAX_TORQUE_LIMIT, 500, 2)  # 50% max torque
            self._write_register(GRIPPER_MOTOR_ID, ADDR_PROTECTION_CURRENT, 250, 2)  # ~1.6 A
            self._write_register(GRIPPER_MOTOR_ID, ADDR_OVERLOAD_TORQUE, 25, 1)  # 25% torque when overloaded

    def write_homing_offset(self, motor_id: int, offset: int) -> None:
        """Servo-side homing: present = mechanical - offset (mod 4096), verified on hardware.

        EEPROM register — call with torque off (``set_torque(False)`` also clears Lock).
        The register is sign-magnitude with 11 magnitude bits, so |offset| caps at 2047;
        only the mechanical-position-4095 edge (offset 2048) is absorbed, as a 1-tick error.
        """
        if abs(offset) > 2048:
            raise ValueError(f"homing offset {offset} does not fit the servo's 11-bit sign-magnitude register")
        offset = min(max(offset, -2047), 2047)
        self._write_register_verified(motor_id, ADDR_HOMING_OFFSET, _encode_sign_magnitude(offset, 11), 2)

    def read_homing_offset(self, motor_id: int) -> int:
        return _sign_magnitude(self._read_register(motor_id, ADDR_HOMING_OFFSET, 2), 11)

    def enable_software_homing(self, middle: list[int]) -> None:
        """Home this arm in software instead of via the servo's Homing_Offset register.

        ``middle`` is the raw mechanical tick at the calibration middle pose, per motor (same
        order as ``motor_ids``). After this call ``read_telemetry``/``read_positions`` return
        HOMED ticks (``HOMED_CENTER`` at the middle) and ``sync_write_goal`` accepts HOMED goals,
        so every consumer sees exactly what a servo-side offset would have produced — needed for
        firmware that stores the offset register but never applies it to Present_Position.
        """
        if len(middle) != len(self.motor_ids):
            raise ValueError(f"software homing needs one middle per motor: got {len(middle)} for {len(self.motor_ids)} motors")
        self._homing_middle = list(middle)

    def write_position_limits(self, motor_id: int, range_min: int, range_max: int) -> None:
        """Servo-side motion limits (EEPROM, torque off) — lerobot writes the swept range here.

        Verified like the homing offsets: these run right after the homing writes with no
        second Lock clear, so a re-engaged Lock would drop them just as silently.
        """
        self._write_register_verified(motor_id, ADDR_MIN_POSITION_LIMIT, range_min, 2)
        self._write_register_verified(motor_id, ADDR_MAX_POSITION_LIMIT, range_max, 2)

    def sync_write_goal(self, positions: list[int]) -> None:
        """One Goal_Position sync-write for all motors (fire-and-forget, servos send no reply).

        ``positions`` are HOMED ticks when software homing is on (mirroring what ``read_telemetry``
        returns); they are mapped back to raw mechanical ticks here so the servo — which never
        applied the offset — moves to the intended pose.
        """
        if self._homing_middle is not None:
            positions = [software_mechanical(position, middle) for position, middle in zip(positions, self._homing_middle, strict=True)]
        with self.lock:
            self.sync_write.clearParam()
            for motor_id, position in zip(self.motor_ids, positions, strict=True):
                self.sync_write.addParam(motor_id, [scs.SCS_LOBYTE(position), scs.SCS_HIBYTE(position)])
            try:
                comm = self.sync_write.txPacket()
            except OSError as error:
                raise RuntimeError(f"{self.port}: goal sync write failed (device disconnected?): {error}") from error
        if comm != scs.COMM_SUCCESS:
            raise RuntimeError(f"{self.port}: goal sync write failed: {self.packet_handler.getTxRxResult(comm)}")

    def close(self) -> None:
        # A half-open handler (failed reconnect) has ser=None; closePort() would AttributeError.
        if self.port_handler.is_open:
            self.port_handler.closePort()
