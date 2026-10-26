"""Developer services for exploring every mower command over BLE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN, LOGGER

ATTR_DEVICE_ID = "device_id"
ATTR_COMMAND = "command"
ATTR_PARAMETERS = "parameters"
ATTR_FAMILY = "family"
ATTR_ALLOW_WRITE = "allow_write"

READ_PREFIXES = ("Get", "Is")

DEVICE_SCHEMA = {vol.Optional(ATTR_DEVICE_ID): cv.string}

READ_ALL_SCHEMA = vol.Schema({**DEVICE_SCHEMA, vol.Optional(ATTR_FAMILY): cv.string})
LIST_SCHEMA = vol.Schema({**DEVICE_SCHEMA, vol.Optional(ATTR_FAMILY): cv.string})
SEND_SCHEMA = vol.Schema(
    {
        **DEVICE_SCHEMA,
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Optional(ATTR_PARAMETERS, default={}): dict,
        vol.Optional(ATTR_ALLOW_WRITE, default=False): cv.boolean,
    }
)


def _coordinator(hass: HomeAssistant, call: ServiceCall):
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    if device_id := call.data.get(ATTR_DEVICE_ID):
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"Unknown device {device_id}")
        entries = [e for e in entries if e.entry_id in device.config_entries]
    if len(entries) != 1:
        raise ServiceValidationError(
            "Select the mower device" if entries else "No connected mower"
        )
    return entries[0].runtime_data


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes | bytearray):
        return value.hex()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if hasattr(value, "name") and hasattr(value, "value"):  # enums
        return value.value
    return value


def _is_read(name: str, spec: dict) -> bool:
    return (
        "." in name
        and name.split(".", 1)[1].startswith(READ_PREFIXES)
        and not spec.get("requestType")
    )


def _coerce(dtype: str, value: Any) -> Any:
    if dtype in ("ascii", "utf16"):
        return str(value)
    if dtype == "byteArray":
        return bytes.fromhex(value) if isinstance(value, str) else bytes(value)
    if dtype == "bool":
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    return int(value, 0) if isinstance(value, str) else int(value)


def _enum_names(spec: dict, field: str, value: Any) -> Any:
    """Add the symbolic enum name next to a raw value, when known."""
    enum = spec.get("params", {}).get(field, {}).get("enum")
    if not enum or not isinstance(value, int):
        return value
    for key, number in enum.items():
        if number == value:
            return f"{value} ({key})"
    return value


def _describe(spec: dict, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _enum_names(spec, k, v) for k, v in value.items()}
    fields = list((spec.get("responseType") or {}).keys())
    if len(fields) == 1:
        return _enum_names(spec, fields[0], value)
    return value


def async_setup_services(hass: HomeAssistant) -> None:
    """Register developer services."""

    async def list_commands(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        protocol = await coordinator.mower.get_protocol()
        family = call.data.get(ATTR_FAMILY)
        commands = {}
        for name, spec in sorted(protocol.items()):
            if "." not in name or (family and spec.get("family") != family):
                continue
            commands[name] = {
                "parameters": spec.get("requestType") or {},
                "returns": spec.get("responseType") or {},
                **({"details": spec["params"]} if "params" in spec else {}),
            }
        return {"platform": coordinator.mower.platform, "commands": commands}

    async def read_all(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        mower = coordinator.mower
        protocol = await mower.get_protocol()
        family = call.data.get(ATTR_FAMILY)
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, spec in sorted(protocol.items()):
            if not _is_read(name, spec):
                continue
            if family and spec.get("family") != family:
                continue
            if not mower.is_connected():
                try:
                    await coordinator._async_find_device()
                except Exception as err:
                    errors["_connection"] = f"lost connection before {name}: {err}"
                    break
            try:
                result, value = await mower.command_response(name, warn_on_error=False)
            except Exception as err:
                errors[name] = f"{type(err).__name__}: {err}"
                continue
            if result.name == "UNKNOWN_ERROR" and not mower.is_connected():
                errors[name] = "no response (mower dropped the connection)"
                continue
            if result.name == "OK":
                values[name] = _jsonable(_describe(spec, value))
            else:
                errors[name] = result.name
        LOGGER.info("read_all: %d values, %d errors", len(values), len(errors))
        return {
            "platform": mower.platform,
            "model": coordinator.model,
            "values": values,
            "errors": errors,
        }

    async def send_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        mower = coordinator.mower
        name = call.data[ATTR_COMMAND]
        protocol = await mower.get_protocol()
        spec = protocol.get(name)
        if spec is None:
            raise ServiceValidationError(
                f"Unknown command {name}; use list_commands to see what is available"
            )
        is_read = name.split(".", 1)[-1].startswith(READ_PREFIXES)
        if not is_read and not call.data[ATTR_ALLOW_WRITE]:
            raise ServiceValidationError(
                f"{name} may change the mower; set allow_write: true to send it"
            )
        types = spec.get("requestType") or {}
        given = call.data[ATTR_PARAMETERS]
        missing = set(types) - set(given)
        unknown = set(given) - set(types)
        if missing or unknown:
            raise ServiceValidationError(
                f"{name} takes parameters {dict(types)}"
                + (f"; missing {sorted(missing)}" if missing else "")
                + (f"; unknown {sorted(unknown)}" if unknown else "")
            )
        kwargs = {k: _coerce(types[k], v) for k, v in given.items()}
        LOGGER.info("send_command %s %s", name, kwargs)
        result, value = await mower.command_response(name, **kwargs)
        if not is_read:
            await coordinator.async_request_refresh()
        return {
            "command": name,
            "result": result.name,
            "value": _jsonable(_describe(spec, value)),
        }

    hass.services.async_register(
        DOMAIN, "list_commands", list_commands, LIST_SCHEMA, SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, "read_all", read_all, READ_ALL_SCHEMA, SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, "send_command", send_command, SEND_SCHEMA, SupportsResponse.OPTIONAL
    )
