import binascii
from .helpers import crc
from enum import IntEnum
import asyncio
import logging
import json
from importlib.resources import files
from bleak import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakDBusError
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bleak import BleakClient

logger = logging.getLogger(__name__)

GARDENA_WRITE_CHAR = "98bd0002-0b0e-421a-84e5-ddbf75dc6de4"
GARDENA_READ_CHAR = "98bd0003-0b0e-421a-84e5-ddbf75dc6de4"
GARDENA_PROTOCOL_DESCRIPTOR_CHAR = "98bd0004-0b0e-421a-84e5-ddbf75dc6de4"
GATT_AUTH_ERROR_TEXT = (
    "Insufficient authentication",
    "Insufficient authorization",
    "Insufficient encryption",
)


class ModeOfOperation(IntEnum):
    # ProtocolTypes$IMowerAppMowerMode, used in modeOfOperation: 4586, 1
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-mode
    AUTO = 0
    MANUAL = 1
    HOME = 2  # Mower goes home and parks forever. Week schedule is not used. Cannot be overridden with forced mowing.
    DEMO = 3  # Same as main area, but shorter times. No blade operation
    POI = 4


class MowerState(IntEnum):
    # ProtocolTypes$IMowerAppState, used in mowerState: 4586, 2
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-state
    OFF = 0  # Mower is turned off.
    WAIT_FOR_SAFETYPIN = 1
    STOPPED = 2  # Mower is stopped requires manual action.
    FATAL_ERROR = 3
    PENDING_START = 4
    PAUSED = 5  # Mower has been paused by user.
    IN_OPERATION = 6  # See value in activity for status.
    RESTRICTED = (
        7  # Mower can currently not mow due to week calender, or override park.
    )
    ERROR = 8  # An error has occurred. Check errorCode. Mower requires manual action.


class MowerActivity(IntEnum):
    # ProtocolTypes$IMowerAppActivity, used in mowerActivity: 4586, 3
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-activity
    NONE = 0
    CHARGING = 1  # Mower is charging in station due to low battery.
    GOING_OUT = 2
    MOWING = 3  # Mower is mowing lawn. If in demo mode the blades are not in operation.
    GOING_HOME = 4  # Mower is going home to the charging station.
    PARKED = 5
    STOPPED_IN_GARDEN = 6  # Mower has stopped. Needs manual action to resume


class OverrideAction(IntEnum):
    NONE = 0
    FORCEDPARK = 1
    FORCEDMOW = 2


class ResponseResult(IntEnum):
    OK = 0
    UNKNOWN_ERROR = 1
    INVALID_VALUE = 2
    OUT_OF_RANGE = 3
    NOT_AVAILABLE = 4
    NOT_ALLOWED = 5
    INVALID_GROUP = 6
    INVALID_ID = 7
    DEVICE_BUSY = 8
    INVALID_PIN = 9
    MOWER_BLOCKED = 10


def _response_result_label(value: int) -> str:
    try:
        result = ResponseResult(value)
    except ValueError:
        return f"UNKNOWN_RESULT({value})"
    return f"{result.name}({value})"


def _is_gatt_auth_error(err: Exception) -> bool:
    return any(text in str(err) for text in GATT_AUTH_ERROR_TEXT)


# Wire encoding of TIF primitive types. All integers are little endian.
# Mirrors the official app's AutomowerUtils encoder/decoder.
FIXED_SIZES: dict[str, tuple[int, bool]] = {
    # name -> (size in bytes, signed)
    "uint8": (1, False),
    "bool": (1, False),
    "sint8": (1, True),
    "uint16": (2, False),
    "sint16": (2, True),
    "tSimpleVersion": (2, False),
    "tAngle": (2, True),
    "uint32": (4, False),
    "sint32": (4, True),
    "tUnixTime": (4, False),
    "uint64": (8, False),
    "sint64": (8, True),
}


def encode_value(dtype: str, value) -> bytes:
    """Encode one request parameter."""
    if dtype in FIXED_SIZES:
        size, signed = FIXED_SIZES[dtype]
        if dtype == "bool":
            value = 1 if value else 0
        return int(value).to_bytes(size, byteorder="little", signed=signed)
    if dtype == "ascii":
        # ISO-8859-1, null terminated
        return str(value).encode("latin-1") + b"\x00"
    if dtype == "utf16":
        # tUCS2: UTF-16LE, null terminated
        return str(value).encode("utf-16-le") + b"\x00\x00"
    if dtype == "byteArray":
        return bytes(value)
    raise ValueError("Unknown request type: " + dtype)


def decode_value(dtype: str, data: bytes, pos: int, is_last: bool):
    """Decode one response field at ``pos``; return (value, new_pos)."""
    if dtype in FIXED_SIZES:
        size, signed = FIXED_SIZES[dtype]
        if pos + size > len(data):
            raise ValueError(
                f"Response too short. Need {pos + size} bytes, got {len(data)}"
            )
        value = int.from_bytes(data[pos : pos + size], "little", signed=signed)
        return value, pos + size
    if dtype == "remaining_uint":
        return int.from_bytes(data[pos:], byteorder="little"), len(data)
    if dtype == "ascii":
        end = data.find(b"\x00", pos)
        if end == -1:
            end = len(data)
        text = bytes(data[pos:end]).decode("latin-1")
        # A trailing string owns any padding after its terminator
        return text, len(data) if is_last else min(end + 1, len(data))
    if dtype == "utf16":
        raw = bytes(data[pos:])
        end = len(raw)
        for i in range(0, len(raw) - 1, 2):
            if raw[i] == 0 and raw[i + 1] == 0:
                end = i
                break
        try:
            return raw[:end].decode("utf-16-le"), len(data)
        except UnicodeDecodeError as err:
            raise ValueError("Unable to decode UTF-16 response") from err
    if dtype == "byteArray":
        return bytes(data[pos:]), len(data)
    raise ValueError("Unknown data type: " + dtype)


class TaskInformation:
    def __init__(
        self,
        start_time_in_minutes,
        duration_in_minutes,
        on_monday,
        on_tuesday,
        on_wednesday,
        on_thursday,
        on_friday,
        on_saturday,
        on_sunday,
    ):
        self.start_time_in_minutes = start_time_in_minutes
        self.duration_in_minutes = duration_in_minutes
        self.on_monday = on_monday
        self.on_tuesday = on_tuesday
        self.on_wednesday = on_wednesday
        self.on_thursday = on_thursday
        self.on_friday = on_friday
        self.on_saturday = on_saturday
        self.on_sunday = on_sunday


class Command:
    def __init__(self, channel_id: int, parameter: dict):
        self.channel_id = channel_id

        self.major = parameter["major"]
        self.minor = parameter["minor"]

        self.request_data_type = parameter.get("requestType")

        if "responseType" not in parameter:
            parameter["responseType"] = "no_response"

        if not isinstance(parameter["responseType"], dict):  # Always wrap in list
            self.response_data_type = {"response": parameter["responseType"]}
        else:
            self.response_data_type = parameter["responseType"]
        self.request_data = bytearray()

    def generate_request(self, **kwargs) -> bytearray:
        self.request_data = bytearray(18)
        self.request_data[0] = 0x02  # Hard coded value (start of packet)
        self.request_data[1] = 0xFD  # 0xFD = LINKED_PACKET_TYPE
        self.request_data[2] = 0x00  # Length, low byte, updated later
        self.request_data[3] = 0x00  # Length, high byte, updated later

        # ChannelID
        self.request_data[4:8] = self.channel_id.to_bytes(4, byteorder="little")

        self.request_data[8] = 0x01  # is_linked (usually 0x01)

        self.request_data[9] = 0x00  # CRC, Updated later
        self.request_data[10] = (
            0x00  # Packet type (0x00 = request, 0x01 = response, 0x02 = event)
        )
        self.request_data[11] = 0xAF  # Hard coded value

        major_bytes = self.major.to_bytes(2, byteorder="little")

        self.request_data[12] = major_bytes[0]  # low byte of 'module'
        self.request_data[13] = major_bytes[1]  # high byte of 'module'
        self.request_data[14] = self.minor  # low byte of 'command'
        self.request_data[15] = 0x00  # high byte of 'command'

        # Byte 16 represents length of request data type
        request_length = 0
        request_data = bytearray()
        if self.request_data_type is not None:
            for request_name, request_type in self.request_data_type.items():
                if request_name not in kwargs:
                    raise ValueError(
                        "Missing request parameter: "
                        + request_name
                        + " for command ("
                        + str(self.major)
                        + ", "
                        + str(self.minor)
                        + ")"
                    )

                encoded = encode_value(request_type, kwargs[request_name])
                request_length += len(encoded)
                request_data += encoded
        self.request_data[16:18] = request_length.to_bytes(2, byteorder="little")
        if request_length > 0:
            self.request_data += request_data

        # Total length (excludes start byte and packet type), low byte first
        self.request_data[2:4] = (len(self.request_data) - 2).to_bytes(
            2, byteorder="little"
        )

        self.request_data[9] = crc(self.request_data, 1, 8)  # CRC

        # Two last bytes are crc and 0x03
        self.request_data.append(crc(self.request_data, 1, len(self.request_data) - 1))
        self.request_data.append(0x03)  # Hard coded value

        return self.request_data

    FIELD_SIZES = FIXED_SIZES

    def parse_response(
        self, response_data: bytearray
    ) -> dict[str, int | str | bytes] | None:
        response_length = int.from_bytes(response_data[17:19], byteorder="little")
        data = response_data[19 : 19 + response_length]
        if response_length == 0:
            # A response length of 0 indicates that no data is available (e.g. GetMessage when there are no errors)
            # This is a valid "no data available" response, not an error.
            return None
        response: dict[str, int | str | bytes] = {}
        dpos = 0  # data position
        fields = list(self.response_data_type.items())
        for index, (name, dtype) in enumerate(fields):
            if dtype == "no_response":
                return None
            is_last = index == len(fields) - 1
            response[name], dpos = decode_value(dtype, data, dpos, is_last)
        if dpos != len(data):
            raise ValueError(f"Data length mismatch. Read {dpos} bytes of {len(data)}")
        return response

    def validate_command_response(self, response_data: bytearray) -> bool:
        if response_data[0] != 0x02:
            return False

        if response_data[1] != 0xFD:
            return False

        if response_data[3] != 0x00:  # high byte of length
            return False

        if response_data[4:8] != self.channel_id.to_bytes(4, byteorder="little"):
            return False

        if response_data[8] != 0x01:
            # This is a valid config, but we don't support it
            # return m1656b(decodeState, c10786f);
            return False

        if response_data[9] != crc(response_data, 1, 8):
            return False

        if response_data[10] != 0x01:  # packet type is not 0x01 = response
            return False

        if response_data[11] != 0xAF:
            return False

        major_bytes = self.major.to_bytes(4, byteorder="little")
        if response_data[12] != major_bytes[0]:
            return False
        if response_data[13] != major_bytes[1]:
            return False
        if response_data[14] != self.minor:
            return False

        if response_data[15] != 0x00:  # high byte of 'command' (self.minor)
            return False

        if (
            response_data[16] != 0x00
        ):  # result: OK(0), UNKNOWN_ERROR(1), INVALID_VALUE(2), OUT_OF_RANGE(3), NOT_AVAILABLE(4), NOT_ALLOWED(5), INVALID_GROUP(6), INVALID_ID(7), DEVICE_BUSY(8), INVALID_PIN(9), MOWER_BLOCKED(10);
            logger.warning(
                "Command %d/%d returned %s",
                self.major,
                self.minor,
                _response_result_label(response_data[16]),
            )
            return False

        return True


class BLEClient:
    def __init__(self, channel_id: int, address, pin=None):
        self.channel_id = channel_id
        self.address = address
        self.pin = pin
        self.MTU_SIZE = 20

        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[bytearray] = asyncio.Queue()

        self.client: BleakClient | None = None
        self.protocol: dict | None = None
        self.platform: str | None = None
        self.events: dict = {}
        self.write_char: BleakGATTCharacteristic | None = None
        self.read_char: BleakGATTCharacteristic | None = None
        self._notify_started = False

    async def get_protocol(self):
        if self.protocol is None:

            def read_protocol_file():
                protocol_file = files(__package__).joinpath("protocol.json")
                logger.debug("Loading protocol from %s", protocol_file)
                with protocol_file.open("r") as f:
                    return json.load(f)

            self.protocol = await asyncio.get_running_loop().run_in_executor(
                None, read_protocol_file
            )
        return self.protocol

    async def load_model_protocol(
        self, device_type: int, device_variant: int
    ) -> str | None:
        """
        Add the full command table for a mower model.

        The tables in ``protocols/`` are generated from the official app's
        interface definitions (see tools/tif_to_protocol.py). Commands are
        added as ``"<Family>.<Command>"`` next to the existing names, so
        existing callers keep working. Returns the platform id, or None if
        the model is unknown.
        """
        protocol = await self.get_protocol()

        def read_tables():
            base = files(__package__).joinpath("protocols")
            models = json.loads(base.joinpath("models.json").read_text())
            platform = models.get(str(device_type), {}).get(str(device_variant))
            if platform is None:
                return None, None
            table = json.loads(base.joinpath(f"{platform}.json").read_text())
            return platform, table

        platform, table = await asyncio.get_running_loop().run_in_executor(
            None, read_tables
        )
        if platform is None:
            return None
        self.protocol = {**protocol, **table["commands"]}
        self.events = table.get("events", {})
        self.platform = platform
        logger.debug(
            "Loaded %d commands for platform %s", len(table["commands"]), platform
        )
        return platform

    async def available_commands(self) -> list[str]:
        """Names of all commands that can be passed to ``command()``."""
        return sorted(await self.get_protocol())

    async def _get_response(self):
        try:
            data = await asyncio.wait_for(self.queue.get(), timeout=10)

        except TimeoutError:
            logger.warning("Unable to get response from device: '%s'", self.address)
            return None

        return data

    async def _write_data(self, data):
        logger.debug("Writing: %s", str(binascii.hexlify(data)))

        chunk_size = self.MTU_SIZE - 3
        for chunk in (
            data[i : i + chunk_size] for i in range(0, len(data), chunk_size)
        ):
            await self.client.write_gatt_char(self.write_char, chunk, response=False)

        logger.debug("Finished writing")

    async def _read_data(self):
        data = await self._get_response()

        if data is None:
            return None

        while data and data[0] != 0x02:
            packet_start = data.find(b"\x02")
            if packet_start >= 0:
                logger.debug(
                    "Discarding stale response prefix: %s",
                    binascii.hexlify(data[:packet_start]),
                )
                data = data[packet_start:]
                break

            logger.debug(
                "Discarding stale response fragment: %s", binascii.hexlify(data)
            )
            data = await self._get_response()
            if data is None:
                return None

        while len(data) < 3:
            # We got such a small amount of data, let's try again.
            chunk = await self._get_response()
            if chunk is None:
                return None
            data += chunk

        length = data[2] + 4

        logger.debug("Waiting for %d bytes", length)

        while len(data) < length:
            try:
                chunk = await asyncio.wait_for(self.queue.get(), timeout=5)
            except TimeoutError:
                logger.error(
                    "Unable to get full response from device '%s', currently have %s",
                    self.address,
                    str(binascii.hexlify(data)),
                )
                logger.error("Expecting %d bytes, only have %d", length, len(data))
                return None
            if chunk is None:
                # Disconnected while waiting for the rest of the response
                return None
            data = data + chunk

        logger.debug("Final response: %s", str(binascii.hexlify(data)))

        return data

    async def _request_response(self, request_data):
        async with self.lock:
            return await self._request_response_locked(request_data)

    async def _request_response_locked(self, request_data):
        """Send a request while the caller already holds the BLE command lock."""
        try:
            # If there are previous responses, flush them out
            while not self.queue.empty():
                await self.queue.get()

            await self._write_data(request_data)

            response_data = await self._read_data()
            if response_data is None:
                logger.warning("Unable to communicate with device: '%s'", self.address)
                if self.is_connected():
                    await self.disconnect()
                return None

        except asyncio.exceptions.CancelledError:
            logger.debug("Received CancelledError")
            if self.is_connected():
                await self.disconnect()
            return None
        except BleakError as err:
            logger.warning("BLE communication failed: %s", err)
            if self.is_connected():
                await self.disconnect()
            raise

        return response_data

    async def connect(self, device) -> ResponseResult:
        """
        Connect to a device and setup the channel

        Returns a ResponseResult
        """
        if self.is_connected():
            logger.debug("Already connected")
            return ResponseResult.OK

        logger.info("starting scan...")

        if device is None:
            logger.warning("Could not find device with address '%s'", self.address)
            return ResponseResult.UNKNOWN_ERROR

        self.write_char = None
        self.read_char = None
        self._notify_started = False

        logger.info("connecting to device...")
        self.client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
        )
        logger.info("connected")

        logger.info("pairing device...")
        try:
            await self.client.pair()
            logger.info("paired")
        except BleakDBusError as err:
            if err.dbus_error == "org.bluez.Error.AuthenticationFailed":
                logger.warning(
                    "Pairing failed with AuthenticationFailed. This mower may "
                    "require a BLE agent that can confirm a passkey. Register "
                    "one before connecting, e.g. via `bluetoothctl`: "
                    "`agent DisplayYesNo`, `default-agent`, then accept the "
                    "confirmation prompt."
                )
            else:
                logger.info(
                    "Pairing failed, continuing with protocol handshake: %s", err
                )
            if not self.client.is_connected:
                return ResponseResult.UNKNOWN_ERROR
        except BleakError as err:
            logger.info("Pairing failed, continuing with protocol handshake: %s", err)
            if not self.client.is_connected:
                return ResponseResult.UNKNOWN_ERROR

        # This is not safe, _mtu_size is not defined in BaseBleakClient but may
        # be defined in subclasses.
        self.client._backend._mtu_size = self.MTU_SIZE  # type: ignore[attr-defined]

        for service in self.client.services:
            logger.info("[Service] %s", service)

            for char in service.characteristics:
                if char.uuid == GARDENA_WRITE_CHAR:
                    self.write_char = char

                if char.uuid == GARDENA_READ_CHAR:
                    self.read_char = char

                if char.uuid in (GARDENA_WRITE_CHAR, GARDENA_READ_CHAR):
                    logger.debug(
                        "  [Characteristic] %s (%s)",
                        char,
                        ",".join(char.properties),
                    )
                    continue

                if "read" in char.properties:
                    try:
                        value = await self.client.read_gatt_char(char.uuid)
                        logger.debug(
                            "  [Characteristic] %s (%s), Value: %r",
                            char,
                            ",".join(char.properties),
                            value,
                        )
                    except Exception as e:
                        logger.debug(
                            "  [Characteristic] %s (%s), Error: %s",
                            char,
                            ",".join(char.properties),
                            e,
                        )
                        if (
                            char.uuid == "98bd0002-0b0e-421a-84e5-ddbf75dc6de4"
                            or char.uuid == "98bd0003-0b0e-421a-84e5-ddbf75dc6de4"
                        ):
                            return ResponseResult.NOT_ALLOWED

                else:
                    logger.debug(
                        "  [Characteristic] %s (%s)", char, ",".join(char.properties)
                    )

        async def notification_handler(
            characteristic: BleakGATTCharacteristic, data: bytearray
        ):
            logger.debug("Received: %s", str(binascii.hexlify(data)))
            await self.queue.put(data)

        if self.write_char is None or self.read_char is None:
            logger.error("Gardena protocol characteristics not found")
            if self.is_connected():
                await self.disconnect()
            return ResponseResult.UNKNOWN_ERROR

        try:
            await self.client.start_notify(self.read_char, notification_handler)
            self._notify_started = True
        except BleakError as err:
            if _is_gatt_auth_error(err):
                logger.info(
                    "Notification subscription needs BLE authentication; "
                    "retrying pairing once"
                )
                try:
                    await self.client.pair()
                    await asyncio.sleep(1.0)
                    await self.client.start_notify(self.read_char, notification_handler)
                    self._notify_started = True
                except BleakError as retry_err:
                    logger.warning(
                        "Unable to subscribe to mower notifications after "
                        "pairing retry: %s",
                        retry_err,
                    )
                    if self.is_connected():
                        await self.disconnect()
                    return ResponseResult.NOT_ALLOWED
            else:
                logger.warning("Unable to subscribe to mower notifications: %s", err)
                if self.is_connected():
                    await self.disconnect()
                return ResponseResult.NOT_ALLOWED

        await asyncio.sleep(5.0)

        request = self.generate_request_setup_channel_id()
        response = await self._request_response(request)
        if response is None:
            return ResponseResult.UNKNOWN_ERROR

        request = self.generate_request_handshake()
        response = await self._request_response(request)
        if response is None:
            return ResponseResult.UNKNOWN_ERROR

        if self.pin is not None:
            command = Command(
                self.channel_id, (await self.get_protocol())["EnterOperatorPin"]
            )
            request = command.generate_request(code=self.pin)
            response = await self._request_response(request)
            if response is None:
                return ResponseResult.UNKNOWN_ERROR
            result = self.get_response_result(response)
            # If the result is UNKNOWN_ERROR, assume the pin was invalid
            return (
                ResponseResult.INVALID_PIN
                if result == ResponseResult.UNKNOWN_ERROR
                else result
            )

        return ResponseResult.OK

    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def probe_gatts(self, device):
        logger.info("connecting to device...")
        if device is None:
            raise BleakError(f"Could not find device with address '{self.address}'")

        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
            max_attempts=3,  # Will retry up to 3 times with backoff
        )
        logger.info("connected")

        manufacture = None
        model = None
        device_type = None

        try:
            for service in client.services:
                logger.debug("[Service] %s", service)

                if service.uuid == "98bd0001-0b0e-421a-84e5-ddbf75dc6de4":
                    manufacture = service.description

                for char in service.characteristics:
                    properties = ",".join(char.properties)
                    should_read = char.uuid in (
                        "00002a00-0000-1000-8000-00805f9b34fb",
                        GARDENA_PROTOCOL_DESCRIPTOR_CHAR,
                    )

                    if char.uuid in (GARDENA_WRITE_CHAR, GARDENA_READ_CHAR):
                        logger.debug("  [Characteristic] %s (%s)", char, properties)
                        continue

                    if "read" in char.properties and should_read:
                        try:
                            value = await client.read_gatt_char(char.uuid)
                            logger.debug(
                                "  [Characteristic] %s (%s), Value: %r",
                                char,
                                properties,
                                value,
                            )
                        except Exception as e:
                            logger.debug(
                                "  [Characteristic] %s (%s), Error: %s",
                                char,
                                properties,
                                e,
                            )
                            continue

                        if char.uuid == "00002a00-0000-1000-8000-00805f9b34fb":
                            model = value.decode()

                        if char.uuid == GARDENA_PROTOCOL_DESCRIPTOR_CHAR:
                            device_type = value.rstrip(b"\x00").decode()

                    elif "read" in char.properties:
                        logger.debug(
                            "  [Characteristic] %s (%s), Value skipped during probe",
                            char,
                            properties,
                        )
                    else:
                        logger.debug("  [Characteristic] %s (%s)", char, properties)
        finally:
            await client.disconnect()

        return (manufacture, device_type, model)

    async def disconnect(self):
        """
        Disconnect from the mower, this should be called after every
        `connect()` before the Python script exits
        """

        logger.info("disconnecting...")
        await self.client.disconnect()
        logger.info("disconnected")
        self.client = None
        self.write_char = None
        self.read_char = None
        self._notify_started = False

        await self.queue.put(None)

    def generate_request_setup_channel_id(self) -> bytearray:
        """
        Setup the channelID with an Automower, this is the first
        command that should be sent
        """
        data = bytearray.fromhex("02fd160000000000002e1400000000000000004d61696e00")

        # New ChannelID
        data[11:15] = self.channel_id.to_bytes(4, byteorder="little")

        # CRC and end byte
        data[9] = crc(data, 1, 8)
        data.append(crc(data, 1, len(data) - 1))
        data.append(0x03)

        return data

    def generate_request_handshake(self) -> bytearray:
        """
        Generate a request handshake. This should be called after
        the channel id is set up but before other commands
        """
        data = bytearray.fromhex("02fd0a000000000000d00801")

        data[4:8] = self.channel_id.to_bytes(4, byteorder="little")

        # CRCs and end byte
        data[9] = crc(data, 1, 8)
        data.append(crc(data, 1, len(data) - 1))
        data.append(0x03)

        return data

    def validate_response(self, response_data: bytearray) -> bool:
        if response_data[0] != 0x02:
            return False

        if response_data[1] != 0xFD:
            return False

        if response_data[3] != 0x00:  # high byte of length
            return False

        if response_data[4:8] != self.channel_id.to_bytes(4, byteorder="little"):
            return False

        if response_data[8] != 0x01:
            # This is a valid config, but we don't support it
            # return m1656b(decodeState, c10786f);
            return False

        if response_data[9] != crc(response_data, 1, 8):
            return False

        if response_data[10] != 0x01:  # packet type is not 0x01 = response
            return False

        if response_data[11] != 0xAF:
            return False

        if (
            response_data[16] != 0x00
        ):  # result: OK(0), UNKNOWN_ERROR(1), INVALID_VALUE(2), OUT_OF_RANGE(3), NOT_AVAILABLE(4), NOT_ALLOWED(5), INVALID_GROUP(6), INVALID_ID(7), DEVICE_BUSY(8), INVALID_PIN(9), MOWER_BLOCKED(10);
            logger.warning(
                "Protocol response returned %s",
                _response_result_label(response_data[16]),
            )
            return False

        return True

    def get_response_result(self, response_data: bytearray) -> ResponseResult:
        if self.validate_response(response_data) is False:
            # Just log if the response is invalid as this has been seen with user
            # logs from official apps. I.e. it is somewhat expected.
            logger.warning("Response failed validation")

        return ResponseResult(response_data[16])
