from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace


SERVER_PATH = Path(__file__).parents[1] / "src" / "taccap_websocket" / "server.py"
SPEC = importlib.util.spec_from_file_location("taccap_websocket_server", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


class FakeLoop:
    def __init__(self) -> None:
        self.targets: list[float] = []
        self.gains: list[tuple[float, float, float]] = []

    def set_target(self, target: float) -> None:
        self.targets.append(target)

    def set_gains(self, kp: float, kd: float, ff: float) -> None:
        self.gains.append((kp, kd, ff))


def make_controller(*, reverse: bool = True):
    controller = object.__new__(server.GripperController)
    controller.control_mode = server.CONTROL_MODE_MIT
    controller._mode_gains = {
        server.CONTROL_MODE_POSITION: {
            "kp_nm_per_rad": 8.0,
            "kd_nm_s_per_rad": 1.0,
            "feedforward_torque_nm": 0.0,
        },
        server.CONTROL_MODE_MIT: {
            "kp_nm_per_rad": 8.0,
            "kd_nm_s_per_rad": 1.0,
            "feedforward_torque_nm": 0.0,
        },
    }
    controller.control_loop = FakeLoop()
    controller.config = SimpleNamespace(
        max_open_rad=1.22,
        min_open_rad=0.02,
        flags=0x0002 if reverse else 0,
    )
    controller.target_position = 1.0
    controller.applied_target_position = 0.5
    controller._target_update_monotonic = 10.0
    controller.target_max_velocity_rad_s = 0.6
    controller.speed_feedforward_limit_nm = 2.0
    controller.max_position_torque_nm = 0.25
    controller.speed_feedforward_torque_nm = 0.0
    controller._target_motion_active = True
    controller._target_motion_direction = 1.0
    return controller


def test_default_profile_uses_requested_gains_and_motion_limits() -> None:
    assert server.DEFAULT_TARGET_MAX_VELOCITY_RAD_S == 2.0
    assert (server.MIT_KP_NM_PER_RAD, server.MIT_KD_NM_S_PER_RAD) == (8.0, 1.0)
    assert (server.POSITION_KP_NM_PER_RAD, server.POSITION_KD_NM_S_PER_RAD) == (
        8.0,
        1.0,
    )
    assert server.MIT_FEEDFORWARD_TORQUE_NM == 0.0
    assert server.DEFAULT_SPEED_FEEDFORWARD_LIMIT_NM == 2.0
    assert server.DEFAULT_POSITION_TORQUE_NM == 1.8
    # 1.8 Nm is the startup value, not a reduction of the API safety ceiling.
    assert server.MAX_DEBUG_POSITION_TORQUE_NM == 2.0


def test_speed_controller_uses_feedback_lookahead_and_reverse_direction() -> None:
    controller = make_controller(reverse=True)

    controller._advance_target_locked(10.01, actual_position=0.5)

    expected_target = 0.5 + 0.6 / server.CONTROL_LOOP_HZ / 1.2
    assert math.isclose(controller.control_loop.targets[-1], expected_target)
    assert controller.control_loop.gains[-1] == (8.0, 1.0, -0.6)
    assert controller.speed_feedforward_torque_nm == -0.6


def test_speed_controller_scales_with_requested_speed() -> None:
    controller = make_controller(reverse=False)
    controller.target_max_velocity_rad_s = 0.1

    controller._advance_target_locked(10.01, actual_position=0.5)

    expected_target = 0.5 + 0.1 / server.CONTROL_LOOP_HZ / 1.2
    assert math.isclose(controller.control_loop.targets[-1], expected_target)
    assert controller.control_loop.gains[-1] == (8.0, 1.0, 0.1)


def test_high_speed_target_is_bounded_by_feedforward_torque() -> None:
    controller = make_controller(reverse=False)
    controller.target_max_velocity_rad_s = 4.0

    controller._advance_target_locked(10.01, actual_position=0.5)

    expected_target = 0.5 + 4.0 / server.CONTROL_LOOP_HZ / 1.2
    assert math.isclose(controller.control_loop.targets[-1], expected_target)
    assert controller.control_loop.gains[-1] == (8.0, 1.0, 2.0)
    assert controller.speed_feedforward_torque_nm == 2.0


def test_speed_feedforward_limit_is_symmetric_with_signed_base_bias() -> None:
    controller = make_controller(reverse=True)
    controller._mode_gains[server.CONTROL_MODE_MIT]["feedforward_torque_nm"] = 1.0
    controller.target_max_velocity_rad_s = 4.0
    controller.speed_feedforward_limit_nm = 1.0

    controller.target_position = 0.0
    controller._target_motion_direction = -1.0
    controller._advance_target_locked(10.01, actual_position=0.5)
    assert controller.speed_feedforward_torque_nm == 1.0
    assert controller.control_loop.gains[-1] == (8.0, 1.0, 2.0)

    controller.target_position = 1.0
    controller._target_motion_direction = 1.0
    controller._advance_target_locked(10.02, actual_position=0.5)
    assert controller.speed_feedforward_torque_nm == -1.0
    assert controller.control_loop.gains[-1] == (8.0, 1.0, 0.0)


def test_speed_controller_stops_feedforward_at_target() -> None:
    controller = make_controller(reverse=True)
    controller.speed_feedforward_torque_nm = -0.25

    controller._advance_target_locked(10.01, actual_position=0.999)

    assert controller.control_loop.targets[-1] == 1.0
    assert controller.control_loop.gains[-1] == (8.0, 1.0, 0.0)
    assert controller.speed_feedforward_torque_nm == 0.0
    assert controller._target_motion_active is False


def test_crossing_target_latches_position_hold_without_reversing() -> None:
    controller = make_controller(reverse=True)
    controller.target_position = 0.39
    controller._target_motion_direction = -1.0

    controller._advance_target_locked(10.01, actual_position=0.40)
    assert controller._target_motion_active is True
    assert controller.speed_feedforward_torque_nm == 0.6

    # One feedback tick crosses the requested target. The final position is
    # applied and velocity assistance is switched off permanently.
    controller._advance_target_locked(10.02, actual_position=0.38)
    assert controller._target_motion_active is False
    assert controller.control_loop.targets[-1] == 0.39
    assert controller.speed_feedforward_torque_nm == 0.0
    assert controller.control_loop.gains[-1] == (8.0, 1.0, 0.0)

    # Mechanical rebound/noise to the other side must not restart or reverse
    # the speed feed-forward for the same request.
    gain_count = len(controller.control_loop.gains)
    controller._advance_target_locked(10.03, actual_position=0.41)
    assert controller._target_motion_active is False
    assert controller.control_loop.targets[-1] == 0.39
    assert controller.speed_feedforward_torque_nm == 0.0
    assert len(controller.control_loop.gains) == gain_count


def test_zero_speed_keeps_position_only_behavior() -> None:
    controller = make_controller(reverse=True)
    controller.target_max_velocity_rad_s = 0.0

    controller._advance_target_locked(10.01, actual_position=0.5)

    assert controller.control_loop.targets[-1] == 1.0
    assert controller.control_loop.gains == []
