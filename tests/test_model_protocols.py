import asyncio
import json
import unittest
from importlib.resources import files

from automower_ble.protocol import BLEClient, Command, decode_value, encode_value

CHANNEL = 1739453030


def _tables():
    base = files("automower_ble").joinpath("protocols")
    models = json.loads(base.joinpath("models.json").read_text())
    platforms = sorted({p for variants in models.values() for p in variants.values()})
    return base, models, platforms


def _dummy(dtype):
    return {"ascii": "x", "utf16": "x", "byteArray": b"\x01"}.get(dtype, 1)


def _response_packet(command: Command, payload: bytes) -> bytearray:
    """Build a response frame the way the mower does."""
    packet = command.generate_request(
        **{k: _dummy(v) for k, v in (command.request_data_type or {}).items()}
    )[:16]
    packet[10] = 0x01  # response
    packet += b"\x00" + len(payload).to_bytes(2, "little") + payload
    return packet


class TestTifTypes(unittest.TestCase):
    def test_encode_fixed(self):
        self.assertEqual(encode_value("sint32", -1), b"\xff\xff\xff\xff")
        self.assertEqual(encode_value("sint8", -2), b"\xfe")
        self.assertEqual(encode_value("sint16", -300), (-300).to_bytes(2, "little", signed=True))
        self.assertEqual(encode_value("bool", True), b"\x01")
        self.assertEqual(encode_value("tSimpleVersion", 0x0102), b"\x02\x01")

    def test_encode_variable(self):
        self.assertEqual(encode_value("ascii", "AB"), b"AB\x00")
        self.assertEqual(encode_value("utf16", "Hi"), b"H\x00i\x00\x00\x00")
        self.assertEqual(encode_value("byteArray", b"\x01\x02"), b"\x01\x02")

    def test_decode_ascii_in_middle(self):
        # Fota.GetAllStatus: uint8, tUnixTime, ascii, uint8
        data = b"\x02" + (1700000000).to_bytes(4, "little") + b"1.2.3\x00" + b"\x37"
        pos = 0
        state, pos = decode_value("uint8", data, pos, False)
        ts, pos = decode_value("tUnixTime", data, pos, False)
        version, pos = decode_value("ascii", data, pos, False)
        progress, pos = decode_value("uint8", data, pos, True)
        self.assertEqual((state, ts, version, progress), (2, 1700000000, "1.2.3", 0x37))
        self.assertEqual(pos, len(data))

    def test_decode_trailing_ascii_with_padding(self):
        value, pos = decode_value("ascii", b"abc\x00\x00\x00", 0, True)
        self.assertEqual((value, pos), ("abc", 6))

    def test_decode_utf16_and_signed(self):
        self.assertEqual(decode_value("utf16", "Gräs".encode("utf-16-le") + b"\0\0", 0, True)[0], "Gräs")
        self.assertEqual(decode_value("sint32", b"\x00\x00\x00\x80", 0, True)[0], -(2**31))

    def test_long_request_length_fields(self):
        command = Command(CHANNEL, {"major": 4698, "minor": 4, "requestType": {"name": "byteArray"}})
        packet = command.generate_request(name=bytes(300))
        self.assertEqual(int.from_bytes(packet[16:18], "little"), 300)
        self.assertEqual(int.from_bytes(packet[2:4], "little"), len(packet) - 4)

    def test_parse_multi_field_response(self):
        command = Command(
            CHANNEL,
            {
                "major": 4734,
                "minor": 4,
                "responseType": {"longitude": "sint32", "latitude": "sint32"},
            },
        )
        payload = (-1234567).to_bytes(4, "little", signed=True) + (59611111).to_bytes(4, "little", signed=True)
        self.assertEqual(
            command.parse_response(_response_packet(command, payload)),
            {"longitude": -1234567, "latitude": 59611111},
        )


class TestModelTables(unittest.TestCase):
    def test_mark2_maps_to_31_15(self):
        _, models, _ = _tables()
        for variant in ("2", "3"):  # 310 Mark II, 315 Mark II
            self.assertEqual(models["23"][variant], "31.15")

    def test_load_model_protocol(self):
        client = BLEClient(CHANNEL, "00:00:00:00:00:00")
        platform = asyncio.run(client.load_model_protocol(23, 3))
        self.assertEqual(platform, "31.15")
        protocol = asyncio.run(client.get_protocol())
        self.assertIn("GetModel", protocol)  # legacy names kept
        self.assertIn("Headlights.SetScheme", protocol)
        self.assertIn("MowerApp.StateUpdated", client.events)
        self.assertIsNone(asyncio.run(BLEClient(CHANNEL, "x").load_model_protocol(250, 250)))

    def test_new_and_legacy_names_build_identical_packets(self):
        base, _, _ = _tables()
        table = json.loads(base.joinpath("31.15.json").read_text())["commands"]
        legacy = json.loads(files("automower_ble").joinpath("protocol.json").read_text())
        by_id = {(v["major"], v["minor"]): v for v in table.values()}
        compared = 0
        for spec in legacy.values():
            new = by_id.get((spec["major"], spec["minor"]))
            if new is None:
                continue
            if list((spec.get("requestType") or {}).values()) != list((new.get("requestType") or {}).values()):
                continue  # library uses a known different layout (e.g. AddTask)
            args_old = {k: 1 for k in (spec.get("requestType") or {})}
            args_new = {k: 1 for k in (new.get("requestType") or {})}
            self.assertEqual(
                Command(CHANNEL, dict(spec)).generate_request(**args_old),
                Command(CHANNEL, dict(new)).generate_request(**args_new),
            )
            compared += 1
        self.assertGreater(compared, 50)

    def test_every_command_encodes(self):
        base, _, platforms = _tables()
        total = 0
        for platform in platforms:
            table = json.loads(base.joinpath(f"{platform}.json").read_text())["commands"]
            for name, spec in table.items():
                kwargs = {k: _dummy(v) for k, v in (spec.get("requestType") or {}).items()}
                packet = Command(CHANNEL, dict(spec)).generate_request(**kwargs)
                self.assertEqual(packet[0], 0x02, name)
                self.assertEqual(packet[-1], 0x03, name)
                total += 1
        self.assertGreater(total, 10000)


if __name__ == "__main__":
    unittest.main()


class TestFieldOrderAndValues(unittest.TestCase):
    def _table(self):
        base = files("automower_ble").joinpath("protocols")
        return json.loads(base.joinpath("31.15.json").read_text())["commands"]

    def test_wire_order_is_preserved(self):
        table = self._table()
        self.assertEqual(
            list(table["GeoFence.GetCenterPosition"]["responseType"]),
            ["longitude", "latitude"],
        )
        self.assertEqual(
            list(table["LeaveChargingStation.GetAllSettings"]["responseType"]),
            [
                "reversingDistance",
                "minAngleSector1",
                "maxAngleSector1",
                "minAngleSector2",
                "maxAngleSector2",
                "sector1Proportion",
            ],
        )
        self.assertEqual(
            list(table["Calendar.SetTask"]["requestType"])[:3],
            ["index", "start", "duration"],
        )

    def test_single_value_returned_for_named_field(self):
        from automower_ble.mower import Mower

        class CannedMower(Mower):
            async def _request_response(self, request):
                response = bytearray(request[:16])
                response[10] = 0x01
                response += b"\x00\x01\x00\x02"  # result OK, length 1, scheme=2
                return response

        async def run():
            mower = CannedMower(CHANNEL, "00:00:00:00:00:00")
            await mower.load_model_protocol(23, 3)
            value = await mower.command("Headlights.GetScheme")
            result, value2 = await mower.command_response("Headlights.GetScheme")
            return value, result.name, value2

        self.assertEqual(asyncio.run(run()), (2, "OK", 2))
