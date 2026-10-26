#!/usr/bin/env python3
"""Convert Husqvarna TIF interface definitions into automower_ble protocol tables.

The official Automower Connect Android app ships machine-readable interface
definitions ("TIF" files) for every mower platform in ``assets/tif-models``,
plus ``assets/hash_list.json`` that maps a mower's (deviceType, deviceVariant)
to the platform definition it uses.

This script reads those files from an APK you have extracted yourself and
writes one protocol table per platform in the same format as
``automower_ble/protocol.json``:

    automower_ble/protocols/<platform>.json
    automower_ble/protocols/models.json    # deviceType -> variant -> platform

Command keys are ``"<Family>.<Command>"`` exactly as named by the TIF, e.g.
``"Headlights.SetScheme"``. Each entry also carries a ``params`` block with the
unit, range and enum values of every argument, which is what an integration
needs to build entities (number min/max, select options, ...).

Usage:
    unzip app.apk 'assets/*' -d extracted
    python tools/tif_to_protocol.py extracted/assets automower_ble/protocols
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# TIF primitive -> library wire type
PRIMITIVES = {
    "uint8": "uint8",
    "uint16": "uint16",
    "uint32": "uint32",
    "sint8": "sint8",
    "sint16": "sint16",
    "sint32": "sint32",
    "bool": "bool",
    "tUnixTime": "tUnixTime",
    "ascii": "ascii",
    "byteArray": "byteArray",
    "tUCS2": "utf16",
    "tSimpleVersion": "tSimpleVersion",
}


def _resolve(types: dict, name: str) -> tuple[str, dict]:
    """Follow type aliases down to a primitive; collect unit/range/enum info."""
    meta: dict = {"tifType": name}
    seen = 0
    while name in types and seen < 16:
        t = types[name]
        if t.get("postfix") and "unit" not in meta:
            meta["unit"] = t["postfix"]
        if "range" in t and "range" not in meta:
            r = t["range"]
            if "start" in r:
                meta["range"] = [r["start"], r["stop"]]
            elif r.get("type") == "sizeQuery":
                meta["sizeQuery"] = r["get"]
        if "enums" in t and "enum" not in meta:
            meta["enum"] = {e["key"]: int(e["value"]) for e in t["enums"]}
        name = t["type"]
        seen += 1
    if name not in PRIMITIVES:
        raise ValueError(f"Unknown primitive type {name!r}")
    return PRIMITIVES[name], meta


def convert(tif: dict) -> dict:
    types = {t["name"]: t for t in tif.get("types", [])}
    out: dict = {}
    for m in tif["methods"]:
        proto = {p["key"]: p["value"] for p in m["protocol"]}
        entry: dict = {
            "major": int(proto["msgType"]),
            "minor": int(proto["subCmd"]),
            "family": m["family"],
        }
        params: dict = {}
        for side, key in (("inParams", "requestType"), ("outParams", "responseType")):
            if not m[side]:
                continue
            fields = {}
            for p in m[side]:
                wire, meta = _resolve(types, p["type"])
                fields[p["name"]] = wire
                if len(meta) > 1:
                    params[p["name"]] = meta
            entry[key] = fields
        if params:
            entry["params"] = params
        if m.get("loginLevels"):
            entry["loginLevels"] = m["loginLevels"]
        if m.get("description"):
            entry["description"] = m["description"]
        out[f'{m["family"]}.{m["command"]}'] = entry

    events: dict = {}
    for e in tif.get("events", []):
        proto = {p["key"]: p["value"] for p in e["protocol"]}
        fields = {}
        for p in e.get("params", []):
            fields[p["name"]] = _resolve(types, p["type"])[0]
        key = f'{e["family"]}.{e["name"]}'
        if key in events:  # same name reused with a different eventId
            key = f'{key}#{proto["eventId"]}'
        events[key] = {
            "major": int(proto["family"]),
            "eventId": int(proto["eventId"]),
            "responseType": fields,
        }
    return {"commands": out, "events": events}


def _dump(table: dict) -> str:
    """Compact JSON, one command/event per line.

    Field order inside a command is the wire order and must not be sorted.
    """
    parts = []
    for section in ("commands", "events"):
        rows = [
            f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}"
            for k, v in sorted(table[section].items())
        ]
        parts.append(f' "{section}": {{\n' + ",\n".join(rows) + "\n }")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("assets", type=Path, help="extracted APK assets/ directory")
    ap.add_argument("out", type=Path, help="output directory")
    args = ap.parse_args()

    index = json.loads((args.assets / "hash_list.json").read_text())
    args.out.mkdir(parents=True, exist_ok=True)

    written = {}
    for platform, file_hash in sorted(index["latest"].items()):
        src = args.assets / "tif-models" / f"{file_hash}.json"
        if not src.exists():
            print(f"skip {platform}: {src.name} not bundled")
            continue
        table = convert(json.loads(src.read_text()))
        dst = args.out / f"{platform}.json"
        dst.write_text(_dump(table))
        written[platform] = len(table["commands"])

    models = {
        dtype: {variant: platform for variant, platform in variants.items() if platform in written}
        for dtype, variants in index["fallback"].items()
    }
    (args.out / "models.json").write_text(json.dumps(models, indent=1, sort_keys=True) + "\n")
    for p, n in written.items():
        print(f"{p:>6}: {n} commands")


if __name__ == "__main__":
    main()
