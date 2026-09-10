#!/usr/bin/env python3
"""Portable TacCap remote bridge for a gripper host.

The server listens on the target device and exposes camera, tactile and motor
control APIs to trusted LAN clients.
"""

from __future__ import annotations

import argparse
from collections import deque
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import struct
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


LOG = logging.getLogger("taccap-bridge")

LEASE_TIMEOUT_S = 5.0
MAX_VELOCITY_RAD_S = 0.60
MAX_TORQUE_NM = 0.25
CLOSE_CONFIRM_THRESHOLD = 0.05
CONTROL_MODE_POSITION = "position"
CONTROL_MODE_MIT = "mit"
CONTROL_MODES = (CONTROL_MODE_POSITION, CONTROL_MODE_MIT)


def _configured_float(name: str, default: float) -> float:
    value = os.environ.get(name, str(default)).strip()
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return result


MIT_KP_NM_PER_RAD = _configured_float("TACCAP_MIT_KP", 4.0)
MIT_KD_NM_S_PER_RAD = _configured_float("TACCAP_MIT_KD", 2.0)
MIT_FEEDFORWARD_TORQUE_NM = _configured_float("TACCAP_MIT_FEEDFORWARD_TORQUE", 0.0)
POSITION_KP_NM_PER_RAD = _configured_float("TACCAP_POSITION_KP", 4.0)
POSITION_KD_NM_S_PER_RAD = _configured_float("TACCAP_POSITION_KD", 2.0)
CONTROL_LOOP_HZ = 100
MOTOR_STREAM_HZ = 100
# Optional host-side target slew limit.  The SDK ControlLoop accepts a
# normalized target, not a velocity argument; the bridge advances that target
# at the requested raw-radian speed before handing it to ControlLoop.
DEFAULT_TARGET_MAX_VELOCITY_RAD_S = _configured_float(
    # A non-zero speed feed-forward is an optional tuning aid.  It is off by
    # default because a position/impedance step already has its own damping;
    # adding a signed torque bias is exactly what can make a jaw overshoot and
    # ring when the operator releases the trigger.  It remains available from
    # the web API (or TACCAP_TARGET_MAX_VELOCITY_RAD_S) for deliberate tuning.
    "TACCAP_TARGET_MAX_VELOCITY_RAD_S", 0.0
)
# Debugging ceiling for the requested MIT approach speed.  The effective
# speed is still bounded by the +/-2 Nm feed-forward range and the independent
# SDK/firmware torque envelope.  With kd=1 this starts saturating near 2 rad/s;
# values up to 4 rad/s are useful only when deliberately tuning a lower kd.
MAX_TARGET_MAX_VELOCITY_RAD_S = 4.0
MAX_DEBUG_KP_NM_PER_RAD = 100.0
MAX_DEBUG_KD_NM_S_PER_RAD = 50.0
MAX_DEBUG_FEEDFORWARD_TORQUE_NM = 2.0
MAX_DEBUG_POSITION_TORQUE_NM = 2.0
DEFAULT_SPEED_FEEDFORWARD_LIMIT_NM = _configured_float(
    "TACCAP_SPEED_FEEDFORWARD_LIMIT_NM", MAX_DEBUG_FEEDFORWARD_TORQUE_NM
)
if not 0.0 <= DEFAULT_SPEED_FEEDFORWARD_LIMIT_NM <= MAX_DEBUG_FEEDFORWARD_TORQUE_NM:
    raise ValueError(
        "TACCAP_SPEED_FEEDFORWARD_LIMIT_NM must be between 0 and "
        f"{MAX_DEBUG_FEEDFORWARD_TORQUE_NM} Nm"
    )
# A trigger/encoder sample can easily move by a few thousandths of the
# normalized travel while the operator is holding still. Treat a target inside
# this window as arrived so the velocity-assist phase is latched off instead of
# repeatedly hunting around the requested position.
SPEED_CONTROL_POSITION_TOLERANCE = 0.01
# Ignore smaller command changes at the HTTP boundary as well. This is a
# second line of defence for clients that do not smooth their trigger input.
POSITION_COMMAND_DEADBAND = 0.005

MOTOR_MODE_NAMES = {
    0: "idle",
    1: CONTROL_MODE_POSITION,
    2: "velocity",
    3: "torque",
    4: "impedance (MIT)",
}
# Read, rectify and encode tactile samples at the same 30 Hz cadence used by
# LeRobot and the wrist-camera streams.  Running four 700x400 Rectify encoders
# at 120 Hz wastes CPU and network bandwidth without benefiting a 30 FPS
# consumer.
SDK_TACTILE_FPS = 30.0
# ``xensesdk`` takes rectify_size in (width, height) order.  The returned
# NumPy image is normally (height, width, channels), i.e. (700, 400, 3).
# Keep these constants explicit so the bridge cannot silently fall back to the
# old 640x480 Raw frame contract.
TACTILE_RECTIFY_WIDTH = 400
TACTILE_RECTIFY_HEIGHT = 700
TACTILE_RECTIFY_SHAPES = {
    (TACTILE_RECTIFY_HEIGHT, TACTILE_RECTIFY_WIDTH, 3),
    (TACTILE_RECTIFY_WIDTH, TACTILE_RECTIFY_HEIGHT, 3),
}
CAMERA_STREAM_FPS = 30.0
TACTILE_STREAM_FPS = 30.0
CAMERA_JPEG_QUALITY = max(
    60,
    min(95, int(os.environ.get("TACCAP_CAMERA_JPEG_QUALITY", "85"))),
)
FRAME_STALE_AFTER_S = 3.0
USB_BANDWIDTH_ERROR = (
    "USB isochronous bandwidth exhausted (VIDIOC_STREAMON/ENOSPC); "
    "move one gripper USB hub to another root controller"
)

MOTOR_STATUS_BITS = {
    0x0001: "enabled",
    0x0002: "fault",
    0x0004: "stalled",
    0x0008: "over_temp",
    0x0010: "over_current",
    0x0020: "over_voltage",
    0x0040: "under_voltage",
    0x0080: "encoder_error",
}


@dataclass(frozen=True)
class GripperSpec:
    side: str
    serial_number: str
    mcu_device: str
    firmware_serial: str | None = None


@dataclass(frozen=True)
class CameraSpec:
    name: str
    side: str
    kind: str
    label: str
    device: str
    sdk_serial: str | None = None


# These generic placeholders preserve the public two-side/six-camera schema
# while hardware is absent or being replugged.  Real paths are discovered at
# startup or supplied through TACCAP_DEVICE_CONFIG; no machine-specific serial
# number belongs in source control.
FALLBACK_GRIPPER_SPECS = {
    "left": GripperSpec(
        side="left",
        serial_number="unavailable-left",
        mcu_device="/dev/taccap-unavailable/left-mcu",
    ),
    "right": GripperSpec(
        side="right",
        serial_number="unavailable-right",
        mcu_device="/dev/taccap-unavailable/right-mcu",
    ),
}

FALLBACK_CAMERA_SPECS = {
    "left_wrist": CameraSpec(
        "left_wrist",
        "left",
        "wrist",
        "左夹爪相机",
        "/dev/taccap-unavailable/left-wrist",
    ),
    "left_tactile_left": CameraSpec(
        "left_tactile_left",
        "left",
        "tactile_raw",
        "左夹爪 · 左指触觉（标定矫正）",
        "/dev/taccap-unavailable/left-tactile-left",
    ),
    "left_tactile_right": CameraSpec(
        "left_tactile_right",
        "left",
        "tactile_raw",
        "左夹爪 · 右指触觉（标定矫正）",
        "/dev/taccap-unavailable/left-tactile-right",
    ),
    "right_wrist": CameraSpec(
        "right_wrist",
        "right",
        "wrist",
        "右夹爪相机",
        "/dev/taccap-unavailable/right-wrist",
    ),
    "right_tactile_left": CameraSpec(
        "right_tactile_left",
        "right",
        "tactile_raw",
        "右夹爪 · 左指触觉（标定矫正）",
        "/dev/taccap-unavailable/right-tactile-left",
    ),
    "right_tactile_right": CameraSpec(
        "right_tactile_right",
        "right",
        "tactile_raw",
        "右夹爪 · 右指触觉（标定矫正）",
        "/dev/taccap-unavailable/right-tactile-right",
    ),
}


_USB_DEVICE_RE = re.compile(r"\d+-\d+(?:\.\d+)*")
_WRIST_SERIAL_RE = re.compile(r"(XCA[A-Za-z0-9]+)", re.IGNORECASE)
_TACTILE_SERIAL_RE = re.compile(r"(GSPS[A-Za-z0-9]+)", re.IGNORECASE)
_TRAILING_DIGITS_RE = re.compile(r"(\d+)(?:[^0-9]*)$")


def _class_device_path(path: str, class_name: str) -> str | None:
    """Resolve a V4L2/TTY node to its physical USB sysfs device path."""

    try:
        node = Path(os.path.realpath(path)).name
        target = Path(f"/sys/class/{class_name}/{node}/device")
        if not target.exists():
            return None
        return str(target.resolve())
    except (OSError, RuntimeError):
        return None


def _usb_hub_key(path: str, class_name: str) -> tuple[str, ...] | None:
    """Return the USB parent chain shared by a gripper and its cameras.

    The final token identifies the individual device (MCU or camera), while
    all preceding tokens identify the external hub chain.  For example the
    current machine reports ``3-1/3-1.1`` for the left MCU and
    ``3-1/3-1.3`` for its wrist camera, hence both resolve to ``("3-1",)``.
    """

    target = _class_device_path(path, class_name)
    if target is None:
        return None
    # A class device path ends in ``<usb-device>:<interface>``; without
    # removing that suffix the regex sees the same USB token twice and treats
    # the individual camera/MCU as part of the shared hub key.
    target = re.sub(r"/[^/]+:\d+\.\d+$", "", target)
    tokens = tuple(_USB_DEVICE_RE.findall(target))
    return tokens[:-1] if tokens else None


def _by_id_video_entries() -> list[tuple[str, str, tuple[str, ...] | None]]:
    """Enumerate stable V4L2 ``by-id`` index-0 entries.

    Returns ``(basename, path, hub_key)``.  Index 1 is the same physical UVC
    device's metadata node and must not be opened as a second stream.
    """

    result: list[tuple[str, str, tuple[str, ...] | None]] = []
    root = Path("/dev/v4l/by-id")
    try:
        paths = sorted(root.glob("*video-index0"))
    except OSError:
        return result
    for path in paths:
        result.append((path.name, str(path), _usb_hub_key(str(path), "video4linux")))
    return result


def _serial_from_name(pattern: re.Pattern[str], name: str) -> str | None:
    match = pattern.search(name)
    return match.group(1) if match else None


def _finger_from_serial(serial: str) -> str | None:
    """Fleet convention: odd trailing sensor number is the left jaw."""

    match = _TRAILING_DIGITS_RE.search(serial)
    if not match:
        return None
    return "left" if int(match.group(1)) % 2 else "right"


def _firmware_wrist_serial(firmware_serial: str | None) -> str | None:
    """Convert ``TCGU01A28Z0017s`` to the UVC wrist serial ``XCA28Z0017s``."""

    if not firmware_serial:
        return None
    value = str(firmware_serial)
    # Current firmware starts with TCGU01; retain a conservative fallback for
    # future revisions by taking the suffix after the first six characters.
    suffix = value[7:] if value.upper().startswith("TCGU01A") else value
    return f"XCA{suffix}" if suffix else None


def discover_specs(taccap_module: Any | None) -> tuple[dict[str, GripperSpec], dict[str, CameraSpec]]:
    """Discover both grippers and their six cameras on the current USB tree.

    The SDK is authoritative for left/right assignment of the MCU.  V4L2
    ``by-id`` names identify each camera, and the sysfs hub chain associates it
    with the corresponding gripper.  This avoids stale device numbers after a
    reboot or replug while retaining deterministic ``<side>_tactile_left`` /
    ``right`` names.
    """

    grippers: dict[str, GripperSpec] = {}
    if taccap_module is not None:
        for side, finder_name in (("left", "find_left"), ("right", "find_right")):
            try:
                endpoint = getattr(taccap_module, finder_name)()
                mcu = str(endpoint.mcu_device)
                mcu_serial = str(getattr(endpoint, "mcu_serial", "") or "")
                firmware_serial = str(
                    getattr(endpoint, "firmware_sn", getattr(endpoint, "firmware_serial", "")) or ""
                )
                serial = mcu_serial or str(getattr(endpoint, "ch343_sn", "") or "") or firmware_serial
                grippers[side] = GripperSpec(
                    side=side,
                    serial_number=serial,
                    mcu_device=mcu,
                    firmware_serial=firmware_serial or None,
                )
                LOG.info(
                    "discovered %s gripper: mcu=%s mcu_serial=%s firmware=%s",
                    side,
                    mcu,
                    serial,
                    firmware_serial or "?",
                )
            except Exception as exc:
                LOG.warning("could not discover %s gripper through SDK: %s", side, exc)

    # Preserve the public two-side schema while a side is absent/replugging.
    for side, fallback in FALLBACK_GRIPPER_SPECS.items():
        grippers.setdefault(side, fallback)

    entries = _by_id_video_entries()
    cameras: dict[str, CameraSpec] = {}
    for side in ("left", "right"):
        gripper = grippers[side]
        hub = _usb_hub_key(gripper.mcu_device, "tty")
        side_entries = [entry for entry in entries if hub is not None and entry[2] == hub]
        wrist_target = _firmware_wrist_serial(gripper.firmware_serial)
        wrist_entry = None
        if wrist_target:
            wrist_entry = next(
                (
                    entry
                    for entry in side_entries
                    if (serial := _serial_from_name(_WRIST_SERIAL_RE, entry[0]))
                    and serial.lower() == wrist_target.lower()
                ),
                None,
            )
        if wrist_entry is None:
            wrist_entry = next(
                (entry for entry in side_entries if "LRCP_imx385" in entry[0]),
                None,
            )
        if wrist_entry is not None:
            cameras[f"{side}_wrist"] = CameraSpec(
                f"{side}_wrist",
                side,
                "wrist",
                f"{'左' if side == 'left' else '右'}夹爪相机",
                wrist_entry[1],
            )

        tactile_entries: list[tuple[str, str, tuple[str, ...] | None, str]] = []
        for entry in side_entries:
            serial = _serial_from_name(_TACTILE_SERIAL_RE, entry[0])
            if serial:
                tactile_entries.append((*entry, serial))
        # Sort by serial as a deterministic fallback; the finger assignment is
        # still based on parity, never enumeration order.
        for serial, finger in sorted(
            ((item[3], _finger_from_serial(item[3])) for item in tactile_entries),
            key=lambda item: item[0],
        ):
            if finger is None:
                continue
            item = next(item for item in tactile_entries if item[3] == serial)
            name = f"{side}_tactile_{finger}"
            cameras[name] = CameraSpec(
                name,
                side,
                "tactile_raw",
                f"{'左' if side == 'left' else '右'}夹爪 · {'左' if finger == 'left' else '右'}指触觉（标定矫正）",
                item[1],
                serial,
            )

        LOG.info(
            "camera discovery for %s: hub=%s wrist=%s tactile=%s",
            side,
            hub,
            cameras.get(f"{side}_wrist", CameraSpec("", "", "", "", "")).device
            if f"{side}_wrist" in cameras
            else "missing",
            sorted(name for name in cameras if name.startswith(f"{side}_tactile_")),
        )

    # Fill only missing logical names with the compatibility map.  Existing
    # discovered entries always win, including after a device-number change.
    for name, fallback in FALLBACK_CAMERA_SPECS.items():
        cameras.setdefault(name, fallback)
    return grippers, cameras


def apply_device_config(
    grippers: dict[str, GripperSpec],
    cameras: dict[str, CameraSpec],
) -> tuple[dict[str, GripperSpec], dict[str, CameraSpec]]:
    """Apply optional per-host device paths without changing source code."""

    config_value = os.environ.get("TACCAP_DEVICE_CONFIG", "").strip()
    if not config_value:
        return grippers, cameras
    config_path = Path(config_value).expanduser()
    with config_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"device config must contain a JSON object: {config_path}")

    for side, values in payload.get("grippers", {}).items():
        if side not in FALLBACK_GRIPPER_SPECS or not isinstance(values, dict):
            LOG.warning("ignoring unknown gripper config entry: %s", side)
            continue
        current = grippers.get(side, FALLBACK_GRIPPER_SPECS[side])
        grippers[side] = GripperSpec(
            side=side,
            serial_number=str(values.get("serial_number") or current.serial_number),
            mcu_device=str(values.get("mcu_device") or current.mcu_device),
            firmware_serial=str(values.get("firmware_serial") or current.firmware_serial or "") or None,
        )

    for name, values in payload.get("cameras", {}).items():
        if name not in FALLBACK_CAMERA_SPECS or not isinstance(values, dict):
            LOG.warning("ignoring unknown camera config entry: %s", name)
            continue
        current = cameras.get(name, FALLBACK_CAMERA_SPECS[name])
        cameras[name] = CameraSpec(
            name=name,
            side=current.side,
            kind=current.kind,
            label=str(values.get("label") or current.label),
            device=str(values.get("device") or current.device),
            sdk_serial=str(values.get("sdk_serial") or current.sdk_serial or "") or None,
        )

    LOG.info("applied device configuration: %s", config_path)
    return grippers, cameras


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GripperController:
    """Own one serial transport and serialize all commands sent over it."""

    def __init__(
        self,
        spec: GripperSpec,
        taccap_module: Any,
        control_mode: str | None = None,
    ):
        self.spec = spec
        self.taccap = taccap_module
        self.lock = threading.RLock()
        self.gripper: Any | None = None
        self.config: Any | None = None
        self.control_loop: Any | None = None
        self.armed = False
        self.last_lease = 0.0
        # Firmware may return the pre-enable status for a short interval after
        # the enable ACK.  Do not let the status poller revoke the service
        # lease during that transition.
        self._enable_grace_until = 0.0
        self.target_position: float | None = None
        self.last_error: str | None = None
        if control_mode is None:
            side_mode = os.environ.get(
                f"TACCAP_{spec.side.upper()}_GRIPPER_CONTROL_MODE", ""
            ).strip()
            control_mode = side_mode or os.environ.get(
                "TACCAP_GRIPPER_CONTROL_MODE", CONTROL_MODE_POSITION
            )
        self.control_mode = self._validate_control_mode(control_mode)
        self._mode_gains: dict[str, dict[str, float]] = {
            CONTROL_MODE_POSITION: {
                "kp_nm_per_rad": POSITION_KP_NM_PER_RAD,
                "kd_nm_s_per_rad": POSITION_KD_NM_S_PER_RAD,
                "feedforward_torque_nm": 0.0,
            },
            CONTROL_MODE_MIT: {
                "kp_nm_per_rad": MIT_KP_NM_PER_RAD,
                "kd_nm_s_per_rad": MIT_KD_NM_S_PER_RAD,
                "feedforward_torque_nm": MIT_FEEDFORWARD_TORQUE_NM,
            },
        }
        self.max_position_torque_nm = MAX_TORQUE_NM
        self.target_max_velocity_rad_s = DEFAULT_TARGET_MAX_VELOCITY_RAD_S
        self.speed_feedforward_limit_nm = DEFAULT_SPEED_FEEDFORWARD_LIMIT_NM
        # Default SDK limits are intentionally kept separate from the old
        # position-command constants; this is the constructor safety clamp.
        self.last_connect_attempt = 0.0
        self.status_condition = threading.Condition()
        self.latest_status: dict[str, Any] | None = None
        # Wall-clock timestamp of the most recent MCU status sample.  It is
        # exported on the status stream so remote clients can distinguish a
        # stale `.4` cache from transport delay on the way to the client.
        self.latest_status_updated_at_s: float | None = None
        self.applied_target_position: float | None = None
        self._target_update_monotonic = time.monotonic()
        # A position request has two phases: one velocity-assisted approach,
        # followed by a latched position hold.  The approach direction is
        # fixed when a new target arrives so feedback noise cannot reverse the
        # velocity feed-forward on alternate sides of the target.
        self._target_motion_active = False
        self._target_motion_direction = 0.0
        # The SDK ControlLoop accepts a position target but no velocity target.
        # Keep the velocity-derived torque contribution visible so the web UI
        # can distinguish the requested speed from the torque actually used to
        # follow it.
        self.speed_feedforward_torque_nm = 0.0
        self.status_sequence = 0
        self.status_stop = threading.Event()
        self.status_thread: threading.Thread | None = None
        if self.connect():
            self._start_status_reader()

    @staticmethod
    def _validate_control_mode(value: str) -> str:
        mode = str(value).strip().lower()
        if mode not in CONTROL_MODES:
            choices = ", ".join(CONTROL_MODES)
            raise ValueError(f"unsupported gripper control mode {value!r}; choose {choices}")
        return mode

    def set_control_mode(self, mode: str) -> dict[str, Any]:
        """Select the command primitive used by subsequent position requests.

        Current TacCap SDKs intentionally remove raw motor ``submit_*`` and
        ``set_position`` methods from Python.  Both web modes therefore use
        the SDK ``ControlLoop``; ``position`` selects conservative gains and
        ``mit`` selects the configured impedance gains.
        """

        mode = self._validate_control_mode(mode)
        with self.lock:
            previous = self.control_mode
            self.control_mode = mode
            self.speed_feedforward_torque_nm = 0.0
            self._apply_control_gains_locked()
            LOG.info(
                "%s gripper command mode changed: %s -> %s",
                self.spec.side,
                previous,
                mode,
            )
        return self.status()

    def _control_gains(self, mode: str | None = None) -> tuple[float, float, float]:
        selected = mode or self.control_mode
        values = self._mode_gains[selected]
        return (
            values["kp_nm_per_rad"],
            values["kd_nm_s_per_rad"],
            values["feedforward_torque_nm"],
        )

    def _control_parameters_locked(self, mode: str | None = None) -> dict[str, Any]:
        selected = mode or self.control_mode
        kp, kd, ff = self._control_gains(selected)
        return {
            "mode": selected,
            "kp_nm_per_rad": kp,
            "kd_nm_s_per_rad": kd,
            "feedforward_torque_nm": ff,
            "max_position_torque_nm": self.max_position_torque_nm,
            "target_max_velocity_rad_s": self.target_max_velocity_rad_s,
            "target_control_state": (
                "moving"
                if self._target_motion_active
                else "holding"
                if self.target_position is not None
                else "idle"
            ),
            "speed_feedforward_limit_nm": self.speed_feedforward_limit_nm,
            "speed_feedforward_torque_nm": self.speed_feedforward_torque_nm,
            "applied_feedforward_torque_nm": min(
                MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
                max(
                    -MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
                    ff + self.speed_feedforward_torque_nm,
                ),
            ),
            "control_loop_hz": CONTROL_LOOP_HZ,
            "motor_stream_hz": MOTOR_STREAM_HZ,
            "submit_phase": "STREAM_LOCKED",
            "limits": {
                "kp_nm_per_rad": [0.0, MAX_DEBUG_KP_NM_PER_RAD],
                "kd_nm_s_per_rad": [0.0, MAX_DEBUG_KD_NM_S_PER_RAD],
                "feedforward_torque_nm": [
                    -MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
                    MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
                ],
                "max_position_torque_nm": [0.0, MAX_DEBUG_POSITION_TORQUE_NM],
                "target_max_velocity_rad_s": [0.0, MAX_TARGET_MAX_VELOCITY_RAD_S],
                "speed_feedforward_limit_nm": [0.0, MAX_DEBUG_FEEDFORWARD_TORQUE_NM],
            },
        }

    @staticmethod
    def _parameter_float(body: dict[str, Any], *names: str) -> float | None:
        for name in names:
            if name in body:
                value = body[name]
                if isinstance(value, bool):
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be numeric")
                try:
                    parsed = float(value)
                except (TypeError, ValueError) as exc:
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be numeric") from exc
                if not math.isfinite(parsed):
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be finite")
                return parsed
        return None

    def set_control_parameters(self, body: dict[str, Any]) -> dict[str, Any]:
        """Update debug gains and host-side target speed for one mode.

        The native SDK exposes gains through ``ControlLoop.set_gains`` only;
        safety limits that belong to the constructor are applied by rebuilding
        the loop while holding the controller lock.  The endpoint never
        exposes an unbounded raw motor command.
        """

        mode_value = body.get("mode", body.get("control_mode", self.control_mode))
        if not isinstance(mode_value, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be one of: position, mit")
        mode = self._validate_control_mode(mode_value)
        updates = {
            "kp_nm_per_rad": self._parameter_float(body, "kp_nm_per_rad", "kp"),
            "kd_nm_s_per_rad": self._parameter_float(body, "kd_nm_s_per_rad", "kd"),
            "feedforward_torque_nm": self._parameter_float(
                body, "feedforward_torque_nm", "feedforward_torque", "ff"
            ),
        }
        max_position_torque = self._parameter_float(
            body, "max_position_torque_nm", "position_torque_limit_nm"
        )
        target_velocity = self._parameter_float(
            body,
            "target_max_velocity_rad_s",
            "max_velocity_rad_s",
            "velocity_rad_s",
        )
        speed_feedforward_limit = self._parameter_float(
            body,
            "speed_feedforward_limit_nm",
            "velocity_feedforward_limit_nm",
        )
        for name, value in updates.items():
            if value is None:
                continue
            upper = {
                "kp_nm_per_rad": MAX_DEBUG_KP_NM_PER_RAD,
                "kd_nm_s_per_rad": MAX_DEBUG_KD_NM_S_PER_RAD,
                "feedforward_torque_nm": MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
            }[name]
            if value < (-upper if name == "feedforward_torque_nm" else 0.0) or value > upper:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be within its safety range")
        if max_position_torque is not None and not 0.0 <= max_position_torque <= MAX_DEBUG_POSITION_TORQUE_NM:
            raise ApiError(HTTPStatus.BAD_REQUEST, "max_position_torque_nm is outside the safety range")
        if target_velocity is not None and not 0.0 <= target_velocity <= MAX_TARGET_MAX_VELOCITY_RAD_S:
            raise ApiError(HTTPStatus.BAD_REQUEST, "target_max_velocity_rad_s is outside the safety range")
        if (
            speed_feedforward_limit is not None
            and not 0.0
            <= speed_feedforward_limit
            <= MAX_DEBUG_FEEDFORWARD_TORQUE_NM
        ):
            raise ApiError(HTTPStatus.BAD_REQUEST, "speed_feedforward_limit_nm is outside the safety range")

        with self.lock:
            # A speed target is an optional approach aid, not a persistent
            # motor command.  Changing gains/ff from the web UI must not leave
            # a previous approach phase active with a new torque law.
            if target_velocity is not None or any(value is not None for value in updates.values()):
                self._target_motion_active = False
                self._target_motion_direction = 0.0
                self.speed_feedforward_torque_nm = 0.0
            previous_mode = self.control_mode
            self.control_mode = mode
            for name, value in updates.items():
                if value is not None:
                    self._mode_gains[mode][name] = value
            if target_velocity is not None:
                self.target_max_velocity_rad_s = target_velocity
                # Remove the contribution calculated for the previous speed;
                # the next status frame will derive a fresh value from the
                # measured direction and velocity.
                self.speed_feedforward_torque_nm = 0.0
            if speed_feedforward_limit is not None:
                self.speed_feedforward_limit_nm = speed_feedforward_limit
                self.speed_feedforward_torque_nm = 0.0
            if previous_mode != mode:
                self.speed_feedforward_torque_nm = 0.0
            rebuild = (
                max_position_torque is not None
                and max_position_torque != self.max_position_torque_nm
            )
            if max_position_torque is not None:
                self.max_position_torque_nm = max_position_torque
            if rebuild and self.control_loop is not None:
                self._rebuild_control_loop_locked()
            else:
                self._apply_control_gains_locked()
            LOG.info(
                "%s control parameters updated: mode=%s kp=%.3f kd=%.3f "
                "ff=%.3f torque_limit=%.3f target_speed=%.3f speed_ff_limit=%.3f",
                self.spec.side,
                mode,
                self._mode_gains[mode]["kp_nm_per_rad"],
                self._mode_gains[mode]["kd_nm_s_per_rad"],
                self._mode_gains[mode]["feedforward_torque_nm"],
                self.max_position_torque_nm,
                self.target_max_velocity_rad_s,
                self.speed_feedforward_limit_nm,
            )
            if previous_mode != mode:
                LOG.info(
                    "%s gripper command mode changed by tuning request: %s -> %s",
                    self.spec.side,
                    previous_mode,
                    mode,
                )
            return self._control_parameters_locked(mode)

    def _rebuild_control_loop_locked(self) -> None:
        old_loop = self.control_loop
        if old_loop is None or self.gripper is None:
            return
        was_running = bool(old_loop.running)
        if was_running:
            old_loop.stop()
        self.speed_feedforward_torque_nm = 0.0
        kp, kd, ff = self._control_gains()
        self.control_loop = self.taccap.ControlLoop(
            self.gripper,
            hz=CONTROL_LOOP_HZ,
            kp=kp,
            kd=kd,
            feedforward_torque=ff,
            motor_stream_hz=MOTOR_STREAM_HZ,
            max_position_torque_nm=self.max_position_torque_nm,
        )
        if was_running:
            self.control_loop.start()
            if self.applied_target_position is not None:
                self.control_loop.set_target(self.applied_target_position)

    def _apply_control_gains_locked(self) -> None:
        if self.control_loop is None:
            return
        kp, kd, ff = self._control_gains()
        self.control_loop.set_gains(kp, kd, ff + self.speed_feedforward_torque_nm)

    def _update_speed_feedforward_locked(
        self,
        actual_position: float | None = None,
    ) -> None:
        """Add a bounded MIT torque feed-forward for the requested speed.

        ``ControlLoop.set_target`` is position-only.  A moving target by itself
        therefore does not constrain the motor's physical velocity.  The MIT
        law already damps measured velocity with ``kd``; adding a signed
        velocity-bias torque to the feed-forward term makes the
        steady-state velocity follow the requested speed when torque headroom
        is available.  The combined user and speed feed-forward is bounded by
        the public feed-forward safety range, while ControlLoop and firmware
        retain their independent torque and stall guards.
        """

        speed = self.target_max_velocity_rad_s
        requested = self.target_position
        cfg = self.config
        loop = self.control_loop
        if (
            loop is None
            or requested is None
            or cfg is None
            or not self._target_motion_active
            or speed <= 0.0
            or actual_position is None
            or not math.isfinite(actual_position)
        ):
            speed_ff = 0.0
        else:
            _, kd, base_ff = self._control_gains()
            # Reverse maps normalized opening direction to raw motor
            # direction. Use the direction captured for this motion instead
            # of recomputing sign(target-actual) every tick: after arrival the
            # phase is latched to HOLD, so noise or rebound cannot command a
            # full-strength reversal.
            raw_open_direction = -1.0 if int(cfg.flags) & 0x0002 else 1.0
            raw_direction = raw_open_direction * self._target_motion_direction
            requested_speed_ff = raw_direction * min(
                self.speed_feedforward_limit_nm,
                abs(kd) * speed,
            )
            total_ff = min(
                MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
                max(
                    -MAX_DEBUG_FEEDFORWARD_TORQUE_NM,
                    base_ff + requested_speed_ff,
                ),
            )
            # Report only the contribution that is actually left for speed
            # after the signed base bias and total +/-2 Nm clamp.
            speed_ff = total_ff - base_ff

        if abs(speed_ff - self.speed_feedforward_torque_nm) <= 1e-6:
            return
        self.speed_feedforward_torque_nm = speed_ff
        kp, kd, base_ff = self._control_gains()
        loop.set_gains(kp, kd, base_ff + speed_ff)

    def _advance_target_locked(
        self,
        now: float | None = None,
        *,
        actual_position: float | None = None,
    ) -> None:
        """Apply the requested target, optionally respecting a raw-rad/s slew limit."""

        loop = self.control_loop
        requested = self.target_position
        if loop is None or requested is None:
            return
        now = time.monotonic() if now is None else now
        speed = self.target_max_velocity_rad_s
        if (
            self._target_motion_active
            and speed > 0.0
            and self.config is not None
            and actual_position is not None
        ):
            travel_rad = abs(float(self.config.max_open_rad) - float(self.config.min_open_rad))
            if travel_rad > 1e-6:
                # A one-tick position look-ahead supplies a small proportional
                # term while kd*(desired-actual velocity) determines the
                # physical approach speed.  Deriving it from feedback avoids
                # an accumulated target getting far ahead of a loaded jaw.
                lookahead = speed / CONTROL_LOOP_HZ / travel_rad
                delta = requested - actual_position
                reached = (
                    abs(delta) <= SPEED_CONTROL_POSITION_TOLERANCE
                    or self._target_motion_direction * delta <= 0.0
                )
                if reached:
                    # Arrival (or a one-tick overshoot) permanently ends this
                    # approach. Keep the requested position as the final
                    # impedance target; only the velocity-assist term is
                    # removed. The conservative default gains (kp=4, kd=2)
                    # then provide a damped position hold, while feedback
                    # noise cannot restart speed control. Only a different
                    # position request may start another approach.
                    self._target_motion_active = False
                    self._target_motion_direction = 0.0
                    applied = requested
                else:
                    applied = actual_position + self._target_motion_direction * min(
                        abs(delta), lookahead
                    )
            else:
                self._target_motion_active = False
                self._target_motion_direction = 0.0
                applied = requested
        else:
            if speed <= 0.0:
                self._target_motion_active = False
                self._target_motion_direction = 0.0
            applied = requested
        applied = min(1.0, max(0.0, applied))
        if self.applied_target_position is None or abs(applied - self.applied_target_position) > 1e-7:
            loop.set_target(applied)
            self.applied_target_position = applied
        self._target_update_monotonic = now
        self._update_speed_feedforward_locked(actual_position)

    def _start_control_loop_locked(self) -> None:
        if self.control_loop is None or self.control_loop.running:
            return
        self.speed_feedforward_torque_nm = 0.0
        self._apply_control_gains_locked()
        self.control_loop.start()
        self.applied_target_position = float(self.control_loop.target)
        self._target_update_monotonic = time.monotonic()

    def _stop_control_loop_locked(self) -> None:
        if self.control_loop is None or not self.control_loop.running:
            return
        self.control_loop.stop()
        self.speed_feedforward_torque_nm = 0.0

    def _stop_transport_locked(self) -> None:
        if self.gripper is None:
            return
        self._stop_control_loop_locked()
        try:
            self.gripper.transport.stop()
        except Exception:
            LOG.exception("%s transport stop failed", self.spec.side)
        self.gripper = None
        self.config = None
        self.control_loop = None
        self.armed = False

    @staticmethod
    def _is_serial_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "serialbus" in text
            or "input/output error" in text
            or "broken pipe" in text
            or "device disappeared" in text
        )

    def _invalidate_transport_locked(self, reason: Exception) -> None:
        """Drop a stale USB handle so the next operation can reconnect.

        A USB serial adapter can be re-enumerated while the process remains
        alive.  The SDK object then keeps the old file descriptor: status
        reads may continue to show its last cached sample, while the first
        command fails with ``SerialBus::write: Input/output error``.  Closing
        that object and reconnecting by-id is safe because the watchdog state
        is cleared before a new motor object is exposed.
        """
        self.last_error = f"{type(reason).__name__}: {reason}"
        self.armed = False
        self.last_lease = 0.0
        old_gripper = self.gripper
        self._stop_control_loop_locked()
        self.gripper = None
        self.config = None
        self.control_loop = None
        self.last_connect_attempt = time.monotonic()
        if old_gripper is not None:
            try:
                old_gripper.transport.stop()
            except Exception:
                LOG.debug("%s stale transport stop failed", self.spec.side, exc_info=True)

    def _reconnect_after_serial_error_locked(self, reason: Exception) -> bool:
        self._invalidate_transport_locked(reason)
        # The status loop may reconnect in the background.  A control request
        # should also recover immediately when the cable has already settled.
        self.last_connect_attempt = 0.0
        connected = self._connect_locked()
        if connected:
            self._start_status_reader()
        return connected

    def _connect_locked(self) -> bool:
        self.last_connect_attempt = time.monotonic()
        self._stop_transport_locked()
        try:
            gripper = self.taccap.FollowerGripper(
                self.spec.mcu_device,
                baudrate=3_000_000,
                ack_timeout_ms=1000,
                max_retries=2,
                open_cameras=False,
            )
            config = gripper.get_gripper_config(500)
            # A bridge restart must never inherit an enabled actuator.
            gripper.motor.disable()
            self.gripper = gripper
            self.config = config
            kp, kd, ff = self._control_gains()
            self.control_loop = self.taccap.ControlLoop(
                gripper,
                hz=CONTROL_LOOP_HZ,
                kp=kp,
                kd=kd,
                feedforward_torque=ff,
                motor_stream_hz=MOTOR_STREAM_HZ,
                max_position_torque_nm=self.max_position_torque_nm,
            )
            # The current SDK's ControlLoop owns the motor-status stream and
            # is the only Python-safe path for MIT/position commands.
            self.control_loop.start()
            self.applied_target_position = float(self.control_loop.target)
            self._target_update_monotonic = time.monotonic()
            self.armed = False
            self._enable_grace_until = 0.0
            self.target_position = None
            self.applied_target_position = None
            self._target_motion_active = False
            self._target_motion_direction = 0.0
            self._target_update_monotonic = time.monotonic()
            self.last_error = None
            LOG.info(
                "%s gripper connected: serial=%s config=%r",
                self.spec.side,
                self.spec.serial_number,
                config,
            )
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.exception("%s gripper connection failed", self.spec.side)
            try:
                if "gripper" in locals():
                    gripper.transport.stop()
            except Exception:
                pass
            self.gripper = None
            self.config = None
            self.control_loop = None
            self.armed = False
            return False

    def connect(self) -> bool:
        with self.lock:
            if self.gripper is not None:
                return True
            connected = self._connect_locked()
        if connected:
            self._start_status_reader()
        return connected

    def _start_status_reader(self) -> None:
        if self.status_thread is not None and self.status_thread.is_alive():
            return
        self.status_stop.clear()
        self.status_thread = threading.Thread(
            target=self._status_reader_loop,
            name=f"status-{self.spec.side}",
            daemon=True,
        )
        self.status_thread.start()

    def _status_reader_loop(self) -> None:
        """Read motor feedback once and fan the cached state out to clients.

        Both LeRobot side clients poll at 30 Hz.  Performing a serial request in
        every HTTP handler would serialize those requests behind the MCU and
        make observation latency depend on network fan-out.  One producer per
        motor keeps serial ownership local and makes status GETs non-blocking.
        """

        period = 1.0 / 100.0
        next_read = time.monotonic()
        while not self.status_stop.is_set():
            value: dict[str, Any] | None = None
            try:
                with self.lock:
                    gripper = self.gripper
                    if gripper is None:
                        if time.monotonic() - self.last_connect_attempt >= 2.0:
                            self._connect_locked()
                        gripper = self.gripper
                    if gripper is None:
                        # Keep the producer alive while a re-enumerated USB
                        # adapter settles; status() will report the last error.
                        pass
                    else:
                        loop = self.control_loop
                        if loop is None or not loop.running:
                            raise RuntimeError("TacCap ControlLoop is not running")
                        observation = loop.observation()
                        if not observation.valid:
                            raise RuntimeError("waiting for TacCap motor-status stream")
                        self._advance_target_locked(
                            actual_position=float(observation.position),
                        )
                        # The speed feed-forward may have changed the gains;
                        # read the cached observation again only for the
                        # already-updated public state, not by polling serial.
                        observation = loop.observation()
                        raw_status = int(observation.status)
                        hardware_enabled = bool(raw_status & 0x0001)
                        if (
                            self.armed
                            and not hardware_enabled
                            and time.monotonic() >= self._enable_grace_until
                        ):
                            self.armed = False
                            self.last_lease = 0.0
                        normalized = min(1.0, max(0.0, float(observation.position)))
                        cfg = self.config
                        remaining = (
                            max(0.0, LEASE_TIMEOUT_S - (time.monotonic() - self.last_lease))
                            if self.armed
                            else 0.0
                        )
                        value = {
                            "side": self.spec.side,
                            "available": True,
                            "serial_number": self.spec.serial_number,
                            "firmware_serial": self.spec.firmware_serial,
                            "mcu_device": self.spec.mcu_device,
                            "armed": self.armed,
                            "enabled": hardware_enabled,
                            "lease_remaining_s": round(remaining, 3),
                            "position": normalized,
                            "raw_position_rad": float(observation.raw_pos),
                            "velocity_rad_s": float(observation.velocity),
                            "torque_nm": float(observation.torque),
                            "motor_temp_c": float(observation.motor_temp_c),
                            "status": raw_status,
                            "status_flags": [
                                name for bit, name in MOTOR_STATUS_BITS.items() if raw_status & bit
                            ],
                            "target_position": self.target_position,
                            # ControlLoop owns the command frame and the
                            # current observation type does not expose the
                            # firmware target/control-mode fields.
                            "firmware_target_rad": None,
                            "control_mode": 4,
                            "control_mode_name": MOTOR_MODE_NAMES[4],
                            "command_mode": self.control_mode,
                            "speed_feedforward_torque_nm": self.speed_feedforward_torque_nm,
                            "control_parameters": self._control_parameters_locked(),
                            # Compatibility field retained for older clients.
                            "mit_gains": dict(self._mode_gains[CONTROL_MODE_MIT]),
                            "config": {
                                "flags": int(cfg.flags),
                                "max_open_rad": float(cfg.max_open_rad),
                                "min_open_rad": float(cfg.min_open_rad),
                                "reverse": bool(int(cfg.flags) & 0x0002),
                            },
                            "limits": {
                                "max_velocity_rad_s": MAX_VELOCITY_RAD_S,
                                "max_torque_nm": MAX_TORQUE_NM,
                            },
                            "last_error": None,
                        }
                        self.last_error = None
                if value is not None:
                    with self.status_condition:
                        self.latest_status = value
                        self.latest_status_updated_at_s = time.time()
                        self.status_sequence += 1
                        self.status_condition.notify_all()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._is_serial_error(exc):
                    with self.lock:
                        if self.gripper is not None:
                            self._invalidate_transport_locked(exc)
                if not self.status_stop.wait(0.05):
                    LOG.debug("%s status read failed: %s", self.spec.side, self.last_error)
            next_read += period
            delay = next_read - time.monotonic()
            if delay > 0:
                self.status_stop.wait(delay)
            elif delay < -period:
                next_read = time.monotonic()

    def _require_gripper_locked(self) -> Any:
        if self.gripper is None:
            if time.monotonic() - self.last_connect_attempt >= 2.0:
                connected = self._connect_locked()
                if connected:
                    # ``enable``/``position`` can be the first request after a
                    # transient USB replug.  That path calls this helper
                    # directly (rather than ``connect``), so explicitly
                    # restart the cached status producer here as well.
                    self._start_status_reader()
        if self.gripper is None:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"{self.spec.side} gripper unavailable: {self.last_error}",
            )
        return self.gripper

    def enable(self) -> dict[str, Any]:
        with self.lock:
            gripper = self._require_gripper_locked()
            try:
                # Enabling starts a fresh command session. Do not resume a
                # stale approach target left over from before disable/watchdog.
                self.target_position = None
                self._target_motion_active = False
                self._target_motion_direction = 0.0
                self.speed_feedforward_torque_nm = 0.0
                self._stop_control_loop_locked()
                gripper.motor.clear_fault()
                gripper.motor.enable()
                self._start_control_loop_locked()
                self.armed = True
                self.last_lease = time.monotonic()
                self._enable_grace_until = self.last_lease + 0.75
                self.last_error = None
            except Exception as exc:
                if self._is_serial_error(exc) and self._reconnect_after_serial_error_locked(exc):
                    try:
                        self._stop_control_loop_locked()
                        self.gripper.motor.clear_fault()
                        self.gripper.motor.enable()
                        self._start_control_loop_locked()
                        self.armed = True
                        self.last_lease = time.monotonic()
                        self._enable_grace_until = self.last_lease + 0.75
                        self.last_error = None
                        return self.status()
                    except Exception as retry_exc:
                        exc = retry_exc
                self.armed = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._enable_grace_until = 0.0
                try:
                    self._start_control_loop_locked()
                except Exception:
                    LOG.exception("%s could not restart control loop after enable failure", self.spec.side)
                raise ApiError(HTTPStatus.CONFLICT, self.last_error) from exc
        return self.status()

    def disable(self, reason: str = "operator") -> dict[str, Any]:
        with self.lock:
            gripper = self._require_gripper_locked()
            try:
                self.target_position = None
                self._target_motion_active = False
                self._target_motion_direction = 0.0
                self.speed_feedforward_torque_nm = 0.0
                self._stop_control_loop_locked()
                gripper.motor.disable()
                self._start_control_loop_locked()
                LOG.info("%s gripper disabled (%s)", self.spec.side, reason)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise ApiError(HTTPStatus.CONFLICT, self.last_error) from exc
            finally:
                self.armed = False
                self.last_lease = 0.0
                self._enable_grace_until = 0.0
        return self.status()

    def heartbeat(self) -> dict[str, Any]:
        with self.lock:
            if not self.armed:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    f"{self.spec.side} gripper is not enabled by this service",
                )
            self.last_lease = time.monotonic()
        return {"side": self.spec.side, "lease_remaining_s": LEASE_TIMEOUT_S}

    def set_position(self, position: float, confirm_close: bool = False) -> dict[str, Any]:
        if not math.isfinite(position) or position < 0.0 or position > 1.0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "position must be within [0, 1]")
        if position <= CLOSE_CONFIRM_THRESHOLD and not confirm_close:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "fully closing requires JSON field confirm_close=true",
            )

        with self.lock:
            gripper = self._require_gripper_locked()
            if not self.armed:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    f"enable the {self.spec.side} gripper before commanding it",
                )
            try:
                loop = self.control_loop
                if loop is None:
                    raise RuntimeError("TacCap ControlLoop is not available")
                self._start_control_loop_locked()
                # Current SDKs intentionally do not expose raw Motor
                # submit_* / set_position methods to Python.  ControlLoop is
                # the safe normalized [0,1] command surface for both modes.
                observation = loop.observation()
                actual_position = (
                    float(observation.position) if observation.valid else None
                )
                target_changed = (
                    self.target_position is None
                    or abs(position - self.target_position) > POSITION_COMMAND_DEADBAND
                )
                if not target_changed:
                    # Keep the service lease alive, but do not restart the
                    # approach phase or rewrite the ControlLoop target for a
                    # sub-deadband trigger fluctuation.
                    self.last_lease = time.monotonic()
                    self.last_error = None
                    return self.status()
                self.target_position = position
                if target_changed:
                    delta = (
                        position - actual_position
                        if actual_position is not None
                        else 0.0
                    )
                    self._target_motion_active = (
                        self.target_max_velocity_rad_s > 0.0
                        and actual_position is not None
                        and abs(delta) > SPEED_CONTROL_POSITION_TOLERANCE
                    )
                    self._target_motion_direction = (
                        math.copysign(1.0, delta)
                        if self._target_motion_active
                        else 0.0
                    )
                self._advance_target_locked(
                    time.monotonic(),
                    actual_position=actual_position,
                )
                self.last_lease = time.monotonic()
                self.last_error = None
                LOG.info(
                    "%s target position=%.3f applied=%.3f mode=%s state=%s",
                    self.spec.side,
                    position,
                    self.applied_target_position,
                    self.control_mode,
                    "moving" if self._target_motion_active else "holding",
                )
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise ApiError(HTTPStatus.CONFLICT, self.last_error) from exc
        return self.status()

    def watchdog(self, now: float) -> None:
        with self.lock:
            if not self.armed or now - self.last_lease <= LEASE_TIMEOUT_S:
                return
            try:
                self.target_position = None
                self._target_motion_active = False
                self._target_motion_direction = 0.0
                self.speed_feedforward_torque_nm = 0.0
                if self.gripper is not None:
                    self._stop_control_loop_locked()
                    self.gripper.motor.disable()
                    self._start_control_loop_locked()
                LOG.warning("%s watchdog expired; motor disabled", self.spec.side)
            except Exception as exc:
                self.last_error = f"watchdog disable failed: {type(exc).__name__}: {exc}"
                LOG.exception("%s", self.last_error)
            finally:
                self.armed = False
                self.last_lease = 0.0
                self._enable_grace_until = 0.0

    def status(self) -> dict[str, Any]:
        with self.status_condition:
            value = dict(self.latest_status) if self.latest_status is not None else None
            updated_at_s = self.latest_status_updated_at_s
        if value is not None:
            if updated_at_s is not None:
                value["server_status_updated_at_s"] = updated_at_s
                value["server_cache_age_ms"] = round(
                    max(0.0, (time.time() - updated_at_s) * 1000.0), 3
                )
            with self.lock:
                value["armed"] = self.armed
                value["target_position"] = self.target_position
                value["command_mode"] = self.control_mode
                value["applied_target_position"] = self.applied_target_position
                value["target_control_state"] = (
                    "moving"
                    if self._target_motion_active
                    else "holding"
                    if self.target_position is not None
                    else "idle"
                )
                value["speed_feedforward_torque_nm"] = self.speed_feedforward_torque_nm
                value["control_parameters"] = self._control_parameters_locked()
                value["mit_gains"] = dict(self._mode_gains[CONTROL_MODE_MIT])
                value["lease_remaining_s"] = round(
                    max(0.0, LEASE_TIMEOUT_S - (time.monotonic() - self.last_lease))
                    if self.armed
                    else 0.0,
                    3,
                )
                value["last_error"] = self.last_error
            return value
        with self.lock:
            if self.gripper is None and time.monotonic() - self.last_connect_attempt >= 2.0:
                connected = self._connect_locked()
            else:
                connected = self.gripper is not None
        if connected:
            self._start_status_reader()
        return self._unavailable(self.last_error or "waiting for first motor status")

    def _unavailable(self, error: str) -> dict[str, Any]:
        return {
            "side": self.spec.side,
            "available": False,
            "serial_number": self.spec.serial_number,
            "mcu_device": self.spec.mcu_device,
            "armed": self.armed,
            "enabled": False,
            "lease_remaining_s": 0.0,
            "command_mode": self.control_mode,
            "speed_feedforward_torque_nm": self.speed_feedforward_torque_nm,
            "control_parameters": self._control_parameters_locked(),
            "mit_gains": dict(self._mode_gains[CONTROL_MODE_MIT]),
            "last_error": error,
        }

    def close(self) -> None:
        self.status_stop.set()
        thread = self.status_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self.status_thread = None
        with self.lock:
            if self.gripper is not None:
                try:
                    self._stop_control_loop_locked()
                    self.gripper.motor.disable()
                except Exception:
                    LOG.exception("%s shutdown disable failed", self.spec.side)
            self.armed = False
            self._stop_transport_locked()


class FrameSource:
    """Thread-safe latest-frame cache shared by all HTTP clients.

    A camera is captured exactly once by its producer thread.  HTTP handlers
    only wait for a sequence number and copy the newest JPEG; a slow browser
    can therefore drop old frames without back-pressuring the USB reader.
    """

    def __init__(self, spec: CameraSpec):
        self.spec = spec
        self.condition = threading.Condition()
        self.latest_frame: bytes | None = None
        self.sequence = 0
        self.frame_count = 0
        self.last_frame_monotonic = 0.0
        self.last_frame_wallclock = 0.0
        self.last_error: str | None = None
        self.stop_event = threading.Event()
        self.client_count = 0
        self._publish_times: deque[float] = deque(maxlen=90)

    def _publish(self, frame: bytes) -> None:
        now = time.monotonic()
        with self.condition:
            self.latest_frame = frame
            self.sequence += 1
            self.frame_count += 1
            self.last_frame_monotonic = now
            self.last_frame_wallclock = time.time()
            self._publish_times.append(now)
            self.last_error = None
            self.condition.notify_all()

    def latest(self) -> tuple[int, bytes] | None:
        with self.condition:
            if self.latest_frame is None:
                return None
            return self.sequence, self.latest_frame

    def published_at(self, sequence: int) -> float | None:
        """Return the wall-clock time at which the current frame was cached."""

        with self.condition:
            if self.latest_frame is None or sequence != self.sequence:
                return None
            return self.last_frame_wallclock

    def wait_for_frame(
        self, timeout: float = 5.0, after_sequence: int | None = None
    ) -> tuple[int, bytes] | None:
        deadline = time.monotonic() + max(0.05, timeout)
        with self.condition:
            while (
                self.latest_frame is None
                or (after_sequence is not None and self.sequence <= after_sequence)
            ) and not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            if self.latest_frame is None:
                return None
            return self.sequence, self.latest_frame

    # Keep the old method name for the CLI and any external scripts.
    def capture_once(
        self, timeout: float = 5.0, after_sequence: int | None = None
    ) -> tuple[int, bytes] | None:
        return self.wait_for_frame(timeout, after_sequence)

    def _source_fps(self) -> float | None:
        with self.condition:
            if len(self._publish_times) < 2:
                return None
            elapsed = self._publish_times[-1] - self._publish_times[0]
            if elapsed <= 0:
                return None
            return (len(self._publish_times) - 1) / elapsed

    def _base_status(self) -> dict[str, Any]:
        with self.condition:
            age = (
                time.monotonic() - self.last_frame_monotonic
                if self.last_frame_monotonic
                else None
            )
            frame_count = self.frame_count
            client_count = self.client_count
            last_error = self.last_error
            source_fps = None
            if len(self._publish_times) >= 2:
                elapsed = self._publish_times[-1] - self._publish_times[0]
                if elapsed > 0 and age is not None and age < FRAME_STALE_AFTER_S:
                    source_fps = round((len(self._publish_times) - 1) / elapsed, 2)
        stream_fps = TACTILE_STREAM_FPS if self.spec.kind == "tactile_raw" else CAMERA_STREAM_FPS
        return {
            "name": self.spec.name,
            "side": self.spec.side,
            "kind": self.spec.kind,
            "label": self.spec.label,
            "device": self.spec.device,
            "available": (
                self.latest_frame is not None
                and age is not None
                and age < FRAME_STALE_AFTER_S
            ),
            "frame_count": frame_count,
            "source_fps": source_fps,
            "last_frame_age_s": round(age, 3) if age is not None else None,
            "clients": client_count,
            "last_error": last_error,
            "snapshot_url": f"/camera/{self.spec.name}.jpg",
            "stream_url": f"/camera/{self.spec.name}.mjpg?fps={stream_fps:g}",
        }

    def register_client(self) -> None:
        with self.condition:
            self.client_count += 1

    def unregister_client(self) -> None:
        with self.condition:
            self.client_count = max(0, self.client_count - 1)


class CameraSource(FrameSource):
    """Persistent UVC MJPEG reader for a wrist camera.

    The previous implementation launched and destroyed ffmpeg for every HTTP
    frame.  That repeatedly negotiated an isochronous endpoint and made the
    SDK tactile streams pause/resume (often permanently).  One long-lived
    reader keeps the endpoint stable and lets all HTTP clients fan out from a
    single latest-frame cache.  If the USB controller rejects the endpoint,
    the reader backs off and retries without disturbing the tactile readers.
    """

    def __init__(self, spec: CameraSpec, ffmpeg: str, open_lock: threading.Lock):
        super().__init__(spec)
        self.ffmpeg = ffmpeg
        self.open_lock = open_lock
        self.process: subprocess.Popen[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.process_lock = threading.RLock()
        self._buffer = bytearray()
        self._last_success = 0.0
        self._retry_backoff_s = 0.0

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._reader_loop,
            name=f"uvc-{self.spec.name}",
            daemon=True,
        )
        self.thread.start()

    def _command(self) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "v4l2",
            "-input_format",
            "mjpeg",
            "-video_size",
            "640x480",
            "-framerate",
            "30",
            "-i",
            self.spec.device,
            "-an",
            "-c:v",
            "copy",
            "-f",
            "mjpeg",
            "pipe:1",
        ]

    def _publish_buffered_frames(self) -> int:
        published = 0
        while True:
            start = self._buffer.find(b"\xff\xd8")
            if start < 0:
                # Keep a possible SOI prefix split across read chunks.
                if self._buffer and self._buffer[-1] == 0xFF:
                    self._buffer[:] = b"\xff"
                else:
                    self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            end = self._buffer.find(b"\xff\xd9", 2)
            if end < 0:
                # A corrupt/stalled stream must not grow without bound.
                if len(self._buffer) > 4 * 1024 * 1024:
                    del self._buffer[:-2]
                break
            frame = bytes(self._buffer[: end + 2])
            del self._buffer[: end + 2]
            self._publish(frame)
            self._last_success = time.monotonic()
            self._retry_backoff_s = 0.0
            published += 1
        return published

    def _terminate_process(self) -> None:
        with self.process_lock:
            proc = self.process
            if proc is None:
                return
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=1.0)
            self.process = None

    def _reader_loop(self) -> None:
        backoff = 0.5
        while not self.stop_event.is_set():
            if not os.path.exists(self.spec.device):
                self.last_error = f"device not found: {self.spec.device}"
                self.stop_event.wait(min(30.0, backoff))
                backoff = min(30.0, backoff * 2.0)
                continue

            proc: subprocess.Popen[bytes] | None = None
            open_lock_held = False
            self._buffer.clear()
            try:
                # Serialize UVC endpoint negotiation with SDK opens.  The
                # kernel's USB2 bandwidth allocator is order-dependent when
                # several cameras call STREAMON at the same time.
                open_lock_held = self.open_lock.acquire(timeout=5.0)
                if not open_lock_held:
                    self.last_error = "camera open bus is busy"
                    self.stop_event.wait(0.1)
                    continue
                proc = subprocess.Popen(
                    self._command(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                with self.process_lock:
                    self.process = proc
                if proc.stdout is None:
                    raise RuntimeError("ffmpeg stdout is unavailable")
                startup_deadline = time.monotonic() + 5.0
                while not self.stop_event.is_set():
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self._buffer.extend(chunk)
                    published = self._publish_buffered_frames()
                    if open_lock_held and published:
                        self.open_lock.release()
                        open_lock_held = False
                    if open_lock_held and time.monotonic() >= startup_deadline:
                        self.last_error = "UVC stream produced no frame during startup"
                        break
                return_code = proc.poll()
                if return_code is None:
                    return_code = proc.wait(timeout=1.0)
                if self._last_success:
                    backoff = 0.5
                if return_code != 0 or self.latest() is None:
                    self.last_error = (
                        f"{USB_BANDWIDTH_ERROR} (ffmpeg exit={return_code})"
                    )
                elif not self.stop_event.is_set():
                    self.last_error = f"ffmpeg stream stopped (exit={return_code})"
            except Exception as exc:
                self.last_error = f"UVC reader failed: {type(exc).__name__}: {exc}"
                LOG.warning("camera %s: %s", self.spec.name, self.last_error)
            finally:
                if open_lock_held:
                    with contextlib.suppress(Exception):
                        self.open_lock.release()
                self._terminate_process()

            if not self.stop_event.is_set():
                self._retry_backoff_s = min(30.0, backoff)
                self.stop_event.wait(min(30.0, backoff))
                backoff = min(30.0, backoff * 2.0)

    def status(self) -> dict[str, Any]:
        result = self._base_status()
        result.update(
            {
                "stream_fps_max": 30,
                "capture_mode": "persistent ffmpeg UVC MJPEG; latest-frame fanout",
                "process_running": bool(
                    self.process is not None and self.process.poll() is None
                ),
                "target_fps": CAMERA_STREAM_FPS,
                "retry_backoff_s": self._retry_backoff_s,
                "bandwidth_note": USB_BANDWIDTH_ERROR,
            }
        )
        return result

    def close(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self._terminate_process()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        self.thread = None


class SdkTactileSource(FrameSource):
    """One isolated xensesdk process per calibrated tactile camera.

    xensesdk and OpenCV are intentionally kept out of the bridge's Python
    process.  Each source launches ``tactile_worker.py`` and receives
    length-prefixed JPEGs through a private pipe.  This isolates SDK state,
    the GIL, allocator failures, and OpenCV thread pools between all four
    sensors.  The parent still exposes the same latest-frame HTTP contract.

    ``raw_size=(640, 480)`` is fixed in the worker.  Rectify output remains
    ``rectify_size=(400, 700)`` (normally an array of shape ``(700, 400, 3)``).
    Each worker reads, rectifies and encodes at a 30 Hz cadence.  The input
    remains the original 640x480 frame and the HTTP stream still carries
    calibrated Rectify output.
    """

    _MAX_FRAME_BYTES = 8 * 1024 * 1024

    def __init__(self, spec: CameraSpec, open_lock: threading.Lock | None = None):
        super().__init__(spec)
        # Retain the lock argument for compatibility with the old source and
        # CameraSource constructor.  Worker startup is serialized by
        # BridgeState's start-and-wait sequence, while each worker owns its
        # actual UVC negotiation in a separate process.
        self.open_lock = open_lock
        self.worker_path = Path(__file__).with_name("tactile_worker.py")
        self.process: subprocess.Popen[bytes] | None = None
        self.process_lock = threading.RLock()
        self.pipe_fd: int | None = None
        self.thread: threading.Thread | None = None
        self.first_frame = threading.Event()
        self._retry_backoff_s = 0.0

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._reader_loop,
            name=f"sdk-proc-{self.spec.name}",
            daemon=True,
        )
        self.thread.start()

    @staticmethod
    def _read_exact(fd: int, size: int, stop_event: threading.Event) -> bytes | None:
        data = bytearray()
        while len(data) < size and not stop_event.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                return None
            if not ready:
                continue
            try:
                chunk = os.read(fd, size - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data) if len(data) == size else None

    def _spawn_worker(self) -> bool:
        if self.stop_event.is_set():
            return False
        if not self.worker_path.exists():
            self.last_error = f"tactile worker not found: {self.worker_path}"
            return False
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                return True
            read_fd, write_fd = os.pipe()
            try:
                os.set_inheritable(write_fd, True)
                command = [
                    sys.executable,
                    str(self.worker_path),
                    "--serial",
                    self.spec.sdk_serial or self.spec.device,
                    "--fd",
                    str(write_fd),
                    "--fps",
                    str(SDK_TACTILE_FPS),
                    "--jpeg-quality",
                    str(CAMERA_JPEG_QUALITY),
                ]
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(write_fd,),
                )
            except Exception as exc:
                with contextlib.suppress(OSError):
                    os.close(read_fd)
                with contextlib.suppress(OSError):
                    os.close(write_fd)
                self.last_error = f"tactile worker start failed: {type(exc).__name__}: {exc}"
                return False
            with contextlib.suppress(OSError):
                os.close(write_fd)
            self.pipe_fd = read_fd
            self.process = process
            self.first_frame.clear()
            self.last_error = None
            LOG.info(
                "tactile %s worker started (pid=%s, raw_size=(640, 480), "
                "rectify_size=(%d, %d), target=%g Hz)",
                self.spec.name,
                process.pid,
                TACTILE_RECTIFY_WIDTH,
                TACTILE_RECTIFY_HEIGHT,
                SDK_TACTILE_FPS,
            )
            return True

    def _terminate_worker(self) -> None:
        with self.process_lock:
            fd = self.pipe_fd
            self.pipe_fd = None
            process = self.process
            self.process = None
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if process is None:
                return
            if process.poll() is None:
                with contextlib.suppress(Exception):
                    process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        process.kill()
                    with contextlib.suppress(Exception):
                        process.wait(timeout=1.0)

    def wait_until_open(self, timeout: float = 5.0) -> bool:
        """Wait until the worker has negotiated the sensor and sent a frame."""

        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if self.first_frame.is_set():
                return True
            process = self.process
            if process is not None and process.poll() is not None:
                return False
            time.sleep(0.02)
        return self.first_frame.is_set()

    def _reader_loop(self) -> None:
        backoff = 0.5
        while not self.stop_event.is_set():
            if not self._spawn_worker():
                self._retry_backoff_s = backoff
                self.stop_event.wait(min(30.0, backoff))
                backoff = min(30.0, backoff * 2.0)
                continue
            fd = self.pipe_fd
            if fd is None:
                continue
            clean_exit = False
            try:
                while not self.stop_event.is_set():
                    header = self._read_exact(fd, 4, self.stop_event)
                    if header is None:
                        clean_exit = self.stop_event.is_set()
                        break
                    frame_len = struct.unpack("!I", header)[0]
                    if frame_len <= 0 or frame_len > self._MAX_FRAME_BYTES:
                        self.last_error = f"invalid tactile worker frame length: {frame_len}"
                        break
                    frame = self._read_exact(fd, frame_len, self.stop_event)
                    if frame is None:
                        clean_exit = self.stop_event.is_set()
                        break
                    self._publish(frame)
                    self.first_frame.set()
                    backoff = 0.5
                    self._retry_backoff_s = 0.0
            except Exception as exc:
                self.last_error = f"tactile worker pipe failed: {type(exc).__name__}: {exc}"
            finally:
                self._terminate_worker()

            if not self.stop_event.is_set():
                if not clean_exit:
                    self.last_error = self.last_error or "tactile worker stopped"
                self._retry_backoff_s = backoff
                self.stop_event.wait(min(30.0, backoff))
                backoff = min(30.0, backoff * 2.0)

    def status(self) -> dict[str, Any]:
        result = self._base_status()
        with self.process_lock:
            process = self.process
            pid = process.pid if process is not None else None
            running = bool(process is not None and process.poll() is None)
        result.update(
            {
                "sdk_serial": self.spec.sdk_serial,
                "capture_mode": (
                    "one isolated xensesdk worker process per tactile camera; "
                    "raw_size=(640, 480); Rectify (400, 700); "
                    "latest-frame pipe fanout"
                ),
                "sdk_output_type": "Rectify",
                "sdk_rectify_size": [TACTILE_RECTIFY_WIDTH, TACTILE_RECTIFY_HEIGHT],
                "sdk_raw_size": [640, 480],
                "expected_sdk_array_shapes": [
                    [TACTILE_RECTIFY_HEIGHT, TACTILE_RECTIFY_WIDTH, 3],
                    [TACTILE_RECTIFY_WIDTH, TACTILE_RECTIFY_HEIGHT, 3],
                ],
                "stream_fps_max": TACTILE_STREAM_FPS,
                "suspended": False,
                "sdk_open": running and self.first_frame.is_set(),
                "worker_process_running": running,
                "worker_pid": pid,
                "worker_retry_backoff_s": self._retry_backoff_s,
            }
        )
        return result

    def close(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self._terminate_worker()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        self.thread = None


class BridgeState:
    def __init__(
        self,
        ffmpeg: str,
        gripper_specs: dict[str, GripperSpec],
        camera_specs: dict[str, CameraSpec],
        taccap_module: Any | None,
    ):
        """Own the hardware workers for one discovered device snapshot.

        Discovery is deliberately performed before this constructor.  That
        keeps the object graph deterministic and makes a replug recoverable by
        restarting the service (the startup scripts do not retain stale paths).
        """

        try:
            from xensesdk import Sensor
        except Exception as exc:
            Sensor = None
            LOG.warning("xensesdk unavailable; tactile endpoints will use UVC fallback: %s", exc)

        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.camera_open_lock = threading.Lock()
        # OpenCV's default worker pool can create one pool per SDK reader and
        # starve the HTTP threads.  The SDK readers already run concurrently;
        # one encoder thread per source is sufficient and has lower jitter.
        with contextlib.suppress(Exception):
            import cv2

            cv2.setNumThreads(1)
        self.tactile_mode = (
            "xensesdk Sensor.OutputType.Rectify "
            "(rectify_size=(400, 700), one worker process per source, 30 Hz target)"
            if Sensor is not None
            else "raw UVC MJPEG fallback (xensesdk unavailable; bandwidth-limited)"
        )
        self.grippers = {
            side: GripperController(spec, taccap_module)
            for side, spec in gripper_specs.items()
        }
        self.cameras: dict[str, Any] = {}
        for name, spec in camera_specs.items():
            if spec.kind == "tactile_raw" and Sensor is not None:
                camera = SdkTactileSource(spec, self.camera_open_lock)
            else:
                camera = CameraSource(spec, ffmpeg, self.camera_open_lock)
            self.cameras[name] = camera

        # Give the isolated SDK tactile workers first chance to reserve their
        # UVC endpoints.  Starting one source and waiting for its first frame
        # keeps STREAMON ordering deterministic on the shared USB2 controller;
        # the SDK object itself remains isolated in its child process.
        for name, camera in self.cameras.items():
            if name.endswith("_tactile_left") or name.endswith("_tactile_right"):
                camera.start()
                if isinstance(camera, SdkTactileSource):
                    # Let endpoint negotiation settle before opening the next
                    # sensor; simultaneous STREAMON calls are order-sensitive
                    # on the shared USB2 controller.
                    camera.wait_until_open(timeout=4.0)
        for name, camera in self.cameras.items():
            if name.endswith("_wrist"):
                camera.start()
        self.watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="motor-watchdog",
            daemon=True,
        )
        self.watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self.stop_event.wait(0.25):
            now = time.monotonic()
            for controller in self.grippers.values():
                controller.watchdog(now)

    def close(self) -> None:
        self.stop_event.set()
        self.watchdog_thread.join(timeout=2.0)
        for controller in self.grippers.values():
            controller.close()
        for camera in self.cameras.values():
            camera.close()


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TacCap 远程控制</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; }
body { max-width: 1500px; margin: auto; padding: 18px; background:#10141b; color:#e8edf5; }
h1 { margin: 0 0 8px; } .hint { color:#aeb9ca; margin-bottom:18px; }
.controls,.cameras { display:grid; gap:14px; }
.controls { grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); margin-bottom:18px; }
.cameras { grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); }
.card { background:#19212d; border:1px solid #303b4b; border-radius:12px; padding:14px; }
.camera img { display:block; width:100%; aspect-ratio:4/3; object-fit:contain; background:#05070a; border-radius:8px; }
.row { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:10px 0; }
.tuning { display:grid; grid-template-columns:repeat(2,minmax(120px,1fr)); gap:7px; margin:10px 0; }
.tuning label { display:flex; flex-direction:column; gap:3px; color:#aeb9ca; font-size:12px; }
.tuning input { min-width:0; width:100%; box-sizing:border-box; }
.tune { background:#d5a84c; color:#17130a; }
button { border:0; border-radius:7px; padding:9px 13px; cursor:pointer; font-weight:650; }
.enable { background:#48bd79; } .disable { background:#8893a3; }
.open { background:#55a9f3; } .close { background:#f06c64; }
input[type=range] { flex:1; min-width:180px; }
.status { white-space:pre-wrap; font:13px/1.45 ui-monospace,monospace; color:#c9d6e7; min-height:90px; }
.ok { color:#68d391; } .bad { color:#fc8181; }
</style>
</head>
<body>
<h1>TacCap 远程控制</h1>
<div class="hint">0 = 完全闭合，1 = 完全张开。电机使能后需要持续心跳；连接中断约 5 秒会自动断使能。四路触觉由设备端的四个独立采集进程持续读取，触觉和腕部相机均以 30 Hz 发布；网页只接收最新帧，不会因某一路浏览器卡住而拖慢其它路。触觉采集输入为 640×480，使用 xensesdk Sensor.OutputType.Rectify 输出 700×400 标定矫正图。</div>
<section class="controls" id="controls"></section>
<section class="cameras" id="cameras"></section>
<script>
const armed = {left:false, right:false};
const labels = {left:'左夹爪', right:'右夹爪'};
async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  const data = await response.json().catch(()=>({error:response.statusText}));
  if (!response.ok) throw new Error(data.error || JSON.stringify(data));
  return data;
}
function controlCard(side) {
  return `<article class="card"><h2>${labels[side]}</h2>
    <div class="row"><label for="mode-${side}">控制模式</label>
      <select id="mode-${side}" onchange="changeMode('${side}',this.value)">
        <option value="position">Position 位置模式</option>
        <option value="mit">MIT 阻抗模式</option>
      </select></div>
    <div class="row">
      <button class="enable" onclick="enableSide('${side}')">使能</button>
      <button class="disable" onclick="disableSide('${side}')">断使能</button>
      <button class="open" onclick="moveSide('${side}',1,false)">完全张开</button>
      <button class="close" onclick="moveSide('${side}',0,true)">完全闭合</button>
    </div>
    <div class="row"><span>位置</span><input id="slider-${side}" type="range" min="0" max="1" step="0.01" value="1">
      <output id="value-${side}">1.00</output><button onclick="sendSlider('${side}')">发送</button></div>
    <div class="tuning" title="新版 SDK 的 position 和 mit 都通过 ControlLoop 发送阻抗帧">
      <label>kp (Nm/rad)<input id="kp-${side}" type="number" min="0" max="100" step="0.1"></label>
      <label>kd (Nm·s/rad)<input id="kd-${side}" type="number" min="0" max="50" step="0.1"></label>
      <label>基础前馈力矩 (Nm，有符号)<input id="ff-${side}" type="number" min="-2" max="2" step="0.01"></label>
      <label>速度前馈上限 (Nm)<input id="speed-ff-limit-${side}" type="number" min="0" max="2" step="0.01"></label>
      <label>位置误差力矩上限 (Nm)<input id="limit-${side}" type="number" min="0" max="2" step="0.01"></label>
      <label>目标速度 (rad/s)<input id="speed-${side}" type="number" min="0" max="4" step="0.01"></label>
      <label>状态流频率 (Hz)<input value="100" disabled></label>
    </div>
    <div class="row"><button class="tune" onclick="applyTuning('${side}')">应用调参</button><small>速度为主机目标斜坡限制；速度前馈上限是无符号幅值</small></div>
    <div class="status" id="status-${side}">读取中…</div></article>`;
}
document.querySelector('#controls').innerHTML = controlCard('left') + controlCard('right');
for (const side of ['left','right']) {
  const slider=document.querySelector(`#slider-${side}`), out=document.querySelector(`#value-${side}`);
  slider.addEventListener('input',()=>out.textContent=Number(slider.value).toFixed(2));
}
async function enableSide(side) { try { await api(`/api/grippers/${side}/enable`,{method:'POST',body:'{}'}); armed[side]=true; await refresh(); } catch(e){alert(e.message);} }
async function disableSide(side) { try { await api(`/api/grippers/${side}/disable`,{method:'POST',body:'{}'}); armed[side]=false; await refresh(); } catch(e){alert(e.message);} }
async function changeMode(side, mode) { try { await api(`/api/grippers/${side}/control_mode`,{method:'POST',body:JSON.stringify({mode})}); await refresh(); } catch(e){alert(e.message); await refresh();} }
async function moveSide(side, position, confirm_close) { try { await api(`/api/grippers/${side}/position`,{method:'POST',body:JSON.stringify({position,confirm_close})}); await refresh(); } catch(e){alert(e.message);} }
async function applyTuning(side) {
  const n=id=>Number(document.querySelector(`#${id}-${side}`).value);
  const body={mode:document.querySelector(`#mode-${side}`).value,kp_nm_per_rad:n('kp'),kd_nm_s_per_rad:n('kd'),feedforward_torque_nm:n('ff'),speed_feedforward_limit_nm:n('speed-ff-limit'),max_position_torque_nm:n('limit'),target_max_velocity_rad_s:n('speed')};
  try { await api(`/api/grippers/${side}/control_parameters`,{method:'POST',body:JSON.stringify(body)}); await refresh(); }
  catch(e) { alert(e.message); }
}
function sendSlider(side) { const p=Number(document.querySelector(`#slider-${side}`).value); moveSide(side,p,p<=0.05); }
async function refresh() {
  try {
    const all=await api('/api/grippers');
    for(const side of ['left','right']) {
      const s=all.grippers[side];
      armed[side]=Boolean(s.armed);
      const cls=s.available?'ok':'bad';
      const mode=document.querySelector(`#mode-${side}`);
      if (mode && document.activeElement !== mode && s.command_mode) mode.value=s.command_mode;
      const params=s.control_parameters||{};
      for (const [id,key] of [['kp','kp_nm_per_rad'],['kd','kd_nm_s_per_rad'],['ff','feedforward_torque_nm'],['speed-ff-limit','speed_feedforward_limit_nm'],['limit','max_position_torque_nm'],['speed','target_max_velocity_rad_s']]) {
        const input=document.querySelector(`#${id}-${side}`);
        if (input && document.activeElement !== input && params[key] !== undefined) input.value=Number(params[key]);
      }
      document.querySelector(`#status-${side}`).innerHTML=`<span class="${cls}">${s.available?'在线':'不可用'}</span>\n`+
        `位置: ${s.position===undefined?'--':s.position.toFixed(3)}  原始: ${s.raw_position_rad===undefined?'--':s.raw_position_rad.toFixed(4)} rad\n`+
        `命令模式: ${(s.command_mode||'--').toUpperCase()}  电机模式: ${s.control_mode_name||'--'} (${s.control_mode===undefined?'--':s.control_mode})\n`+
        `使能: ${s.enabled?'是':'否'}  服务已授权: ${s.armed?'是':'否'}  剩余: ${Number(s.lease_remaining_s||0).toFixed(1)} s\n`+
        `速度: ${s.velocity_rad_s===undefined?'--':s.velocity_rad_s.toFixed(3)} rad/s  目标: ${s.control_parameters?.target_max_velocity_rad_s===undefined?'--':Number(s.control_parameters.target_max_velocity_rad_s).toFixed(3)} rad/s\n`+
        `速度前馈力矩: ${s.speed_feedforward_torque_nm===undefined?'--':s.speed_feedforward_torque_nm.toFixed(3)} Nm  合计前馈: ${s.control_parameters?.applied_feedforward_torque_nm===undefined?'--':Number(s.control_parameters.applied_feedforward_torque_nm).toFixed(3)} Nm\n`+
        `实际力矩: ${s.torque_nm===undefined?'--':s.torque_nm.toFixed(3)} Nm\n`+
        `温度: ${s.motor_temp_c===undefined?'--':s.motor_temp_c.toFixed(1)} °C  状态: ${(s.status_flags||[]).join(', ')||'--'}\n`+
        `目标: ${s.target_position===undefined?'--':Number(s.target_position).toFixed(3)}  已应用: ${s.applied_target_position===undefined?'--':Number(s.applied_target_position).toFixed(3)}  状态: ${s.target_control_state||'--'}\n`+
        `${s.last_error||''}`;
    }
  } catch(e) { console.error(e); }
}
async function loadCameras() {
  const data=await api('/api/cameras');
  document.querySelector('#cameras').innerHTML=data.cameras.map(c=>`<article class="card camera"><h3>${c.label}</h3><img data-camera="${c.name}" data-stream-fps="30" src="${c.stream_url}" alt="${c.label}" decoding="async"><small id="camera-meta-${c.name}">${c.name} · 采集 ${c.source_fps===null?'--':c.source_fps} Hz</small></article>`).join('');
  for (const img of document.querySelectorAll('img[data-camera]')) {
    attachStreamRetry(img);
  }
}
function attachStreamRetry(img) {
  img.addEventListener('error',()=>setTimeout(()=>{
    const base=img.dataset.camera;
    const fps=img.dataset.streamFps||30;
    img.src=`/camera/${base}.mjpg?fps=${fps}&retry=${Date.now()}`;
    attachStreamRetry(img);
  },2000),{once:true});
}
async function refreshCameras() {
  try {
    const data=await api('/api/cameras');
    for (const c of data.cameras) {
      const el=document.querySelector(`#camera-meta-${c.name}`);
      if (el) el.textContent=`${c.name} · 采集 ${c.source_fps===null?'--':Number(c.source_fps).toFixed(1)} Hz · ${c.available?'在线':'不可用'} · 客户端 ${c.clients||0}`;
    }
  } catch(e) { console.error(e); }
}
setInterval(()=>{ for(const side of ['left','right']) if(armed[side]) api(`/api/grippers/${side}/heartbeat`,{method:'POST',body:'{}'}).catch(()=>armed[side]=false); },1000);
setInterval(refresh,1000); setInterval(refreshCameras,1000); refresh(); loadCameras().then(refreshCameras).catch(e=>alert(e.message));
</script>
</body></html>"""


def make_handler(state: BridgeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "TacCapBridge/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # The LeRobot remote follower polls each side at 30 Hz and sends a
            # heartbeat roughly once per second.  Logging every status GET at
            # INFO would grow the service log by several megabytes per minute;
            # retain the entries at DEBUG while keeping snapshots, streams,
            # control commands, and errors visible at INFO.
            path = urllib.parse.urlsplit(self.path).path
            high_rate_status = (
                (self.command == "GET" and path.startswith("/api/grippers/"))
                or (self.command == "POST" and path.endswith("/heartbeat"))
            )
            level = LOG.debug if high_rate_status else LOG.info
            level("http %s - %s", self.address_string(), fmt % args)

        def _send_bytes(
            self,
            status: int,
            content: bytes,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            if headers:
                for key, value in headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

        def _json(self, status: int, value: Any) -> None:
            content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            self._send_bytes(status, content, "application/json; charset=utf-8")

        def _gripper_status_stream(self, side: str) -> None:
            """Push each new cached MCU status to one long-lived client.

            The serial reader remains the sole producer.  This endpoint only
            fans out the already cached status and never touches the MCU, so a
            slow client cannot change the hardware sampling cadence.  New
            clients receive the current snapshot immediately and then wait on
            the controller's condition variable for newer sequence numbers.
            """

            controller = state.grippers[side]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            with contextlib.suppress(Exception):
                self.connection.settimeout(2.0)
            with contextlib.suppress(Exception):
                # Avoid Nagle coalescing several tiny NDJSON records into a
                # burst.  The client consumes the newest cache value, so
                # prompt delivery matters more than packet efficiency here.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            last_sequence = -1
            last_emit_monotonic = 0.0
            # A duplicate snapshot is a lightweight health heartbeat.  It is
            # needed when the MCU reader stops producing new sequence numbers:
            # without it, the client would not learn about a stale `.4` cache
            # until the HTTP socket's much longer read timeout expires.
            heartbeat_period_s = 0.02
            try:
                while not state.stop_event.is_set():
                    timed_out = False
                    with controller.status_condition:
                        while (
                            controller.status_sequence <= last_sequence
                            and not state.stop_event.is_set()
                        ):
                            if controller.status_condition.wait(timeout=heartbeat_period_s):
                                continue
                            timed_out = True
                            break
                        if state.stop_event.is_set():
                            break
                        sequence = controller.status_sequence

                    now_monotonic = time.monotonic()
                    if (
                        timed_out
                        and sequence == last_sequence
                        and now_monotonic - last_emit_monotonic < heartbeat_period_s
                    ):
                        continue

                    value = controller.status()
                    value["server_status_sequence"] = sequence
                    value["server_sent_at_s"] = time.time()
                    updated_at_s = value.get("server_status_updated_at_s")
                    if isinstance(updated_at_s, (int, float)):
                        value["server_cache_age_ms"] = round(
                            max(
                                0.0,
                                (value["server_sent_at_s"] - float(updated_at_s)) * 1000.0,
                            ),
                            3,
                        )
                    line = (
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                    ).encode()
                    self.wfile.write(line)
                    self.wfile.flush()
                    last_sequence = sequence
                    last_emit_monotonic = now_monotonic
            except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
                # A client disappearing is normal for a long-lived stream;
                # do not attempt to write a second HTTP error response after
                # the 200 headers have already been sent.
                pass

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
            if length > 4096:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            if not length:
                return {}
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return value

        def do_GET(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/":
                    self._send_bytes(
                        HTTPStatus.OK,
                        INDEX_HTML.encode(),
                        "text/html; charset=utf-8",
                    )
                    return
                if path == "/api/health":
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "uptime_s": round(time.time() - state.started_at, 3),
                            "bind_policy": "direct LAN access; no built-in authentication",
                            "motor_lease_timeout_s": LEASE_TIMEOUT_S,
                            "tactile_mode": state.tactile_mode,
                            "camera_target_fps": CAMERA_STREAM_FPS,
                            "tactile_target_fps": TACTILE_STREAM_FPS,
                            "camera_transport": "persistent producers + latest-frame fanout",
                            "gripper_status_stream": True,
                        },
                    )
                    return
                if path.startswith("/api/grippers/") and path.endswith("/stream"):
                    side = path[len("/api/grippers/") : -len("/stream")].strip("/")
                    if side in state.grippers:
                        self._gripper_status_stream(side)
                        return
                if path.startswith("/api/grippers/") and path.endswith("/control_parameters"):
                    side = path[len("/api/grippers/") : -len("/control_parameters")].strip("/")
                    if side in state.grippers:
                        controller = state.grippers[side]
                        with controller.lock:
                            self._json(HTTPStatus.OK, controller._control_parameters_locked())
                        return
                if path == "/api/grippers":
                    self._json(
                        HTTPStatus.OK,
                        {"grippers": {k: v.status() for k, v in state.grippers.items()}},
                    )
                    return
                if path.startswith("/api/grippers/"):
                    side = path[len("/api/grippers/") :].strip("/")
                    if side in state.grippers:
                        self._json(HTTPStatus.OK, state.grippers[side].status())
                        return
                if path == "/api/cameras":
                    cameras = [c.status() for c in state.cameras.values()]
                    self._json(
                        HTTPStatus.OK,
                        {
                            "cameras": cameras,
                            "all_six_fresh_30hz": all(
                                c.get("available")
                                and isinstance(c.get("source_fps"), (int, float))
                                and c["source_fps"] >= 27.0
                                for c in cameras
                            ),
                            "all_four_tactile_fresh_30hz": all(
                                c.get("available")
                                and c.get("kind") == "tactile_raw"
                                and isinstance(c.get("source_fps"), (int, float))
                                and c["source_fps"] >= 27.0
                                for c in cameras
                                if c.get("kind") == "tactile_raw"
                            ),
                        },
                    )
                    return
                if path.startswith("/camera/") and path.endswith(".jpg"):
                    name = path[len("/camera/") : -len(".jpg")]
                    self._snapshot(name)
                    return
                if path.startswith("/camera/") and path.endswith(".mjpg"):
                    name = path[len("/camera/") : -len(".mjpg")]
                    camera = self._camera(name)
                    query = urllib.parse.parse_qs(parsed.query)
                    max_fps = (
                        TACTILE_STREAM_FPS
                        if camera.spec.kind == "tactile_raw"
                        else CAMERA_STREAM_FPS
                    )
                    try:
                        fps = float(query.get("fps", [str(max_fps)])[0])
                    except ValueError:
                        fps = max_fps
                    self._mjpeg(name, min(max_fps, max(0.2, fps)))
                    return
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                LOG.exception("GET %s failed", self.path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def _camera(self, name: str) -> Any:
            camera = state.cameras.get(name)
            if camera is None:
                raise ApiError(HTTPStatus.NOT_FOUND, f"unknown camera: {name}")
            return camera

        def _snapshot(self, name: str) -> None:
            camera = self._camera(name)
            item = camera.wait_for_frame(timeout=10.0)
            if item is None:
                raise ApiError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    camera.last_error or f"no frame from {name}",
                )
            _, frame = item
            self._send_bytes(
                HTTPStatus.OK,
                frame,
                "image/jpeg",
                {"X-Camera-Name": name},
            )

        def _mjpeg(self, name: str, fps: float) -> None:
            camera = self._camera(name)
            # Acquisition is performed by one producer thread per camera.  Do
            # not start a capture here: every browser/client must fan out from
            # the same cache, otherwise a slow HTTP socket can stall USB.
            first = camera.wait_for_frame(timeout=12.0)
            if first is None:
                raise ApiError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    camera.last_error or f"no frame from {name}",
                )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            # A dead/slow browser must not leave a handler blocked forever.
            # The producer is independent, so timing out this socket only
            # disconnects this client and never affects another stream.
            with contextlib.suppress(Exception):
                self.connection.settimeout(2.0)
            camera.register_client()
            sequence, frame = first
            interval = 1.0 / fps
            next_send = time.monotonic()
            try:
                while not state.stop_event.is_set() and not camera.stop_event.is_set():
                    header = (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode()
                        + b"\r\nX-Source-Sequence: "
                        + str(sequence).encode()
                        + b"\r\nX-Source-Published-At: "
                        + f"{camera.published_at(sequence) or time.time():.6f}".encode()
                        + b"\r\nX-Server-Sent-At: "
                        + f"{time.time():.6f}".encode()
                        + b"\r\n\r\n"
                    )
                    self.wfile.write(header)
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

                    next_send += interval
                    delay = next_send - time.monotonic()
                    if delay > 0 and state.stop_event.wait(delay):
                        break
                    # Skip stale frames when the source is ahead, and repeat
                    # the latest frame if a source momentarily misses a tick.
                    item = camera.wait_for_frame(
                        timeout=max(0.05, min(1.0, interval * 2.5)),
                        after_sequence=sequence,
                    )
                    if item is not None:
                        sequence, frame = item
                    else:
                        latest = camera.latest()
                        if latest is not None:
                            sequence, frame = latest
            except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
                pass
            finally:
                camera.unregister_client()

        def do_POST(self) -> None:
            try:
                path = urllib.parse.urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                if len(parts) != 4 or parts[:2] != ["api", "grippers"]:
                    raise ApiError(HTTPStatus.NOT_FOUND, "not found")
                side, action = parts[2], parts[3]
                controller = state.grippers.get(side)
                if controller is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, f"unknown side: {side}")
                body = self._read_json()
                if action == "enable":
                    result = controller.enable()
                elif action == "disable":
                    result = controller.disable()
                elif action == "heartbeat":
                    result = controller.heartbeat()
                elif action in {"control_mode", "mode"}:
                    mode = body.get("mode", body.get("control_mode"))
                    if not isinstance(mode, str):
                        raise ApiError(
                            HTTPStatus.BAD_REQUEST,
                            "mode must be one of: position, mit",
                        )
                    result = controller.set_control_mode(mode)
                elif action in {"control_parameters", "parameters", "tuning"}:
                    result = controller.set_control_parameters(body)
                elif action == "position":
                    if "position" not in body:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "missing position")
                    try:
                        position = float(body["position"])
                    except (TypeError, ValueError) as exc:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "position must be numeric") from exc
                    result = controller.set_position(
                        position,
                        confirm_close=body.get("confirm_close") is True,
                    )
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, f"unknown action: {action}")
                self._json(HTTPStatus.OK, result)
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                LOG.exception("POST %s failed", self.path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    if not os.path.isfile(args.ffmpeg):
        raise SystemExit(f"ffmpeg not found: {args.ffmpeg}")

    try:
        from xense import taccap
    except Exception as exc:
        # UVC wrist streams can still be diagnosed without the native motor
        # package.  Gripper endpoints remain visible as unavailable until the
        # SDK environment is repaired and the service is restarted.
        taccap = None
        LOG.exception("xense.taccap import failed; motor control is unavailable: %s", exc)

    gripper_specs, camera_specs = discover_specs(taccap)
    gripper_specs, camera_specs = apply_device_config(gripper_specs, camera_specs)
    LOG.info(
        "startup discovery: grippers=%s cameras=%s",
        sorted(gripper_specs),
        sorted(camera_specs),
    )
    state = BridgeState(args.ffmpeg, gripper_specs, camera_specs, taccap)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    server.daemon_threads = True

    def shutdown(signum: int, _frame: Any) -> None:
        LOG.info("received signal %s; shutting down", signum)
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    LOG.info("listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
