#!/usr/bin/env python3
"""One isolated xensesdk tactile-camera worker for the TacCap bridge.

The parent HTTP server deliberately does not import or call xensesdk from its
camera threads.  One instance of this program owns one Sensor object and sends
length-prefixed JPEG frames over an inherited pipe.  A pipe file descriptor is
used instead of stdout so SDK/OpenCV diagnostics can never corrupt the frame
protocol.

The sensor input remains 640x480.  xensesdk's calibrated Rectify output is
requested at (400, 700), which is returned as a NumPy image of (700, 400, 3)
on the devices in use here.
"""

from __future__ import annotations

import argparse
import os
import signal
import struct
import sys
import time
from typing import Any


RAW_SIZE = (640, 480)
RECTIFY_SIZE = (400, 700)
FPS = 30.0
JPEG_QUALITY = 85
MAX_FRAME_BYTES = 8 * 1024 * 1024


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BrokenPipeError("tactile parent pipe closed")
        view = view[written:]


def _write_frame(fd: int, frame: bytes) -> None:
    if not frame or len(frame) > MAX_FRAME_BYTES:
        raise ValueError(f"invalid JPEG frame length: {len(frame)}")
    _write_all(fd, struct.pack("!I", len(frame)))
    _write_all(fd, frame)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--fd", required=True, type=int)
    parser.add_argument("--fps", type=float, default=FPS)
    parser.add_argument("--jpeg-quality", type=int, default=JPEG_QUALITY)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    # Do not let SIGTERM interrupt an SDK call midway through a C extension;
    # the parent closes the pipe and then terminates this short-lived worker.
    # A normal default handler is still preferable to leaving a sensor alive.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    try:
        import cv2
        import numpy as np
        from xensesdk import Sensor
    except Exception as exc:
        print(f"tactile worker import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    # Each process has its own OpenCV pool.  One thread avoids oversubscription
    # when four workers run beside the bridge and the wrist ffmpeg processes.
    with np.errstate(all="ignore"):
        try:
            cv2.setNumThreads(1)
        except Exception:
            pass

    sensor: Any | None = None
    output_fd = args.fd
    try:
        sensor = Sensor.create(
            args.serial,
            disable_infer=True,
            # xensesdk takes (width, height); the returned array is normally
            # (height, width, channels).
            rectify_size=RECTIFY_SIZE,
            raw_size=RAW_SIZE,
        )
        sensor.selectSensorInfo(Sensor.OutputType.Rectify)

        period = 1.0 / max(1.0, float(args.fps))
        next_read = time.monotonic()
        quality = max(40, min(95, int(args.jpeg_quality)))
        while True:
            delay = next_read - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            image = sensor.selectSensorInfo(Sensor.OutputType.Rectify)
            array = np.asarray(image)
            # Keep the established calibrated 700x400 contract.  Accept the
            # transposed shape for SDK builds that expose width-first arrays.
            if tuple(array.shape) not in {
                (RECTIFY_SIZE[1], RECTIFY_SIZE[0], 3),
                (RECTIFY_SIZE[0], RECTIFY_SIZE[1], 3),
            }:
                raise ValueError(f"unexpected Rectify shape {array.shape}")
            ok, encoded = cv2.imencode(
                ".jpg",
                np.ascontiguousarray(array),
                [int(cv2.IMWRITE_JPEG_QUALITY), quality],
            )
            if not ok:
                raise RuntimeError("cv2.imencode returned false")
            _write_frame(output_fd, encoded.tobytes())
            next_read += period
            now = time.monotonic()
            if next_read < now - period:
                next_read = now
    except (BrokenPipeError, OSError):
        # Parent shutdown/restart is a normal lifecycle event.
        return 0
    except Exception as exc:
        print(
            f"tactile worker failed for {args.serial}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if sensor is not None:
            try:
                sensor.release()
            except Exception:
                pass
        try:
            os.close(output_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
