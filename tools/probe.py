#!/usr/bin/env python3
"""Probe an Automower over BLE using the model specific command table.

Read-only sweep (safe, nothing is changed on the mower):
    python tools/probe.py --address AA:BB:CC:DD:EE:FF --pin 1234 --read-all

List the commands known for the connected model:
    python tools/probe.py --address ... --list

Send one command (may change settings, requires --yes):
    python tools/probe.py --address ... --call Headlights.SetScheme scheme=2 --yes
    python tools/probe.py --address ... --call LeaveChargingStation.GetAllSettings

Results are printed and written to probe-<platform>.json so they can be shared.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bleak import BleakScanner

from automower_ble.mower import Mower

READ_PREFIXES = ("Get", "Is")


def _is_safe_read(name: str, spec: dict) -> bool:
    command = name.split(".", 1)[-1]
    return (
        "." in name
        and command.startswith(READ_PREFIXES)
        and not spec.get("requestType")
    )


def _jsonable(value):
    if isinstance(value, bytes | bytearray):
        return value.hex()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _parse_args(pairs: list[str], spec: dict) -> dict:
    kwargs = {}
    types = spec.get("requestType") or {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        dtype = types.get(key)
        if dtype is None:
            raise SystemExit(f"{key!r} is not a parameter; expected {list(types)}")
        if dtype in ("ascii", "utf16"):
            kwargs[key] = raw
        elif dtype == "byteArray":
            kwargs[key] = bytes.fromhex(raw)
        elif dtype == "bool":
            kwargs[key] = raw.lower() in ("1", "true", "yes", "on")
        else:
            kwargs[key] = int(raw, 0)
    missing = set(types) - set(kwargs)
    if missing:
        raise SystemExit(f"missing parameters: {sorted(missing)}")
    return kwargs


async def run(args) -> None:
    mower = Mower(1197489078, args.address, args.pin)
    device = await BleakScanner.find_device_by_address(args.address, timeout=20)
    if device is None:
        raise SystemExit(f"Mower {args.address} not found, is it in range and awake?")

    result = await mower.connect(device)
    print(
        f"connect: {result.name}  model: {await mower.get_model()}  platform: {mower.platform}"
    )
    protocol = await mower.get_protocol()
    report: dict = {
        "platform": mower.platform,
        "model": await mower.command("GetModel"),
    }

    try:
        if args.list:
            for name in sorted(n for n in protocol if "." in n):
                spec = protocol[name]
                req = ", ".join(
                    f"{k}:{v}" for k, v in (spec.get("requestType") or {}).items()
                )
                print(f"{name}({req})")

        if args.read_all:
            reads = {}
            for name in sorted(n for n, s in protocol.items() if _is_safe_read(n, s)):
                status, value = await mower.command_response(name, warn_on_error=False)
                reads[name] = {"result": status.name, "value": _jsonable(value)}
                shown = value if status.name == "OK" else status.name
                print(f"{name:55s} {shown}")
            report["reads"] = reads

        if args.call:
            spec = protocol.get(args.call)
            if spec is None:
                raise SystemExit(f"Unknown command {args.call}; use --list")
            command = args.call.split(".", 1)[-1]
            if not command.startswith(READ_PREFIXES) and not args.yes:
                raise SystemExit(
                    f"{args.call} may change the mower; add --yes to send it"
                )
            kwargs = _parse_args(args.params, spec)
            status, value = await mower.command_response(args.call, **kwargs)
            print(f"{args.call}: {status.name} {value}")
            report["call"] = {
                "command": args.call,
                "args": _jsonable(kwargs),
                "result": status.name,
                "value": _jsonable(value),
            }
    finally:
        await mower.disconnect()

    out = Path(f"probe-{mower.platform or 'unknown'}.json")
    out.write_text(json.dumps(report, indent=1))
    print(f"\nWrote {out.resolve()}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--address", required=True, help="mower Bluetooth address")
    ap.add_argument("--pin", type=int, default=None, help="mower PIN code")
    ap.add_argument("--list", action="store_true", help="list commands for this model")
    ap.add_argument(
        "--read-all", action="store_true", help="run every parameterless getter"
    )
    ap.add_argument("--call", metavar="Family.Command", help="send a single command")
    ap.add_argument("params", nargs="*", help="key=value arguments for --call")
    ap.add_argument(
        "--yes", action="store_true", help="allow commands that change the mower"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
