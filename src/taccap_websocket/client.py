#!/usr/bin/env python3
"""Command-line client for a TacCap service on the local network."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any


# Robot LAN addresses should never be redirected through HTTP(S)_PROXY.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(base: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(req, timeout=8.0) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            message = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"cannot reach {base}; check the target IP, port and service ({exc.reason})"
        ) from exc


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def heartbeat_for(base: str, side: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        request(base, f"/api/grippers/{side}/heartbeat", {})
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=os.environ.get("TACCAP_BASE_URL", "http://127.0.0.1:8765"),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    sub.add_parser("status")
    sub.add_parser("cameras")

    for action in ("enable", "disable", "heartbeat"):
        p = sub.add_parser(action)
        p.add_argument("side", choices=("left", "right"))

    p = sub.add_parser(
        "mode",
        aliases=("control-mode",),
        help="show or set the command mode (position or mit)",
    )
    p.add_argument("side", choices=("left", "right"))
    p.add_argument("mode", nargs="?", choices=("position", "mit"))

    p = sub.add_parser("position")
    p.add_argument("side", choices=("left", "right"))
    p.add_argument("position", type=float)
    p.add_argument("--confirm-close", action="store_true")
    p.add_argument("--hold", type=float, default=0.0, metavar="SECONDS")

    p = sub.add_parser("open")
    p.add_argument("side", choices=("left", "right"))
    p.add_argument("--hold", type=float, default=0.0, metavar="SECONDS")

    p = sub.add_parser("close")
    p.add_argument("side", choices=("left", "right"))
    p.add_argument("--yes", action="store_true", help="confirm full closure")
    p.add_argument("--hold", type=float, default=0.0, metavar="SECONDS")

    p = sub.add_parser("snapshot")
    p.add_argument(
        "camera",
        choices=(
            "left_wrist",
            "left_tactile_left",
            "left_tactile_right",
            "right_wrist",
            "right_tactile_left",
            "right_tactile_right",
        ),
    )
    p.add_argument("output", type=pathlib.Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base = args.base
    if args.command == "health":
        print_json(request(base, "/api/health"))
    elif args.command == "status":
        print_json(request(base, "/api/grippers"))
    elif args.command == "cameras":
        print_json(request(base, "/api/cameras"))
    elif args.command in {"enable", "disable", "heartbeat"}:
        print_json(request(base, f"/api/grippers/{args.side}/{args.command}", {}))
    elif args.command in {"mode", "control-mode"}:
        if args.mode is None:
            print_json(request(base, f"/api/grippers/{args.side}"))
        else:
            print_json(
                request(
                    base,
                    f"/api/grippers/{args.side}/control_mode",
                    {"mode": args.mode},
                )
            )
    elif args.command in {"position", "open", "close"}:
        if args.command == "position":
            position = args.position
            confirm_close = args.confirm_close
        elif args.command == "open":
            position = 1.0
            confirm_close = False
        else:
            if not args.yes:
                raise RuntimeError("full closure requires: close SIDE --yes")
            position = 0.0
            confirm_close = True
        result = request(
            base,
            f"/api/grippers/{args.side}/position",
            {"position": position, "confirm_close": confirm_close},
        )
        print_json(result)
        if args.hold > 0:
            heartbeat_for(base, args.side, args.hold)
    elif args.command == "snapshot":
        url = base.rstrip("/") + f"/camera/{args.camera}.jpg"
        try:
            with _OPENER.open(url, timeout=10.0) as response:
                image = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"snapshot failed: {exc}") from exc
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(image)
        print(f"saved {len(image)} bytes to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
