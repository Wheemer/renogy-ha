"""Tests for Renogy's recovered controller OTA protocol."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


def _load_firmware_module() -> Any:
    """Load firmware.py without importing the integration package initializer."""
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "renogy"
        / "firmware.py"
    )
    name = "renogy_firmware_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


firmware_module = _load_firmware_module()


def test_bootloader_command_matches_android_protocol() -> None:
    """The broadcast boot command must include the exact Modbus CRC."""
    assert firmware_module.build_bootloader_command().hex() == "ff4100000000281b"


def test_ota_packet_padding_checksum_and_sequence_wrap() -> None:
    """Blocks are padded, checksummed, and wrap their one-byte sequence."""
    packet = firmware_module.build_ota_packet(256, b"\x01\x02")

    assert len(packet) == firmware_module.OTA_PACKET_SIZE
    assert packet[:3] == bytes((0x01, 0x00, 0xFF))
    assert packet[3:5] == b"\x01\x02"
    assert packet[5:-1] == bytes((firmware_module.OTA_PAD_BYTE,)) * 126
    assert packet[-1] == sum(packet[3:-1]) & 0xFF


class _FakeBleakClient:
    """A controller transport that responds like Renogy's bootloader."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.notification_callback: Any = None
        self.services = SimpleNamespace(
            get_characteristic=lambda _uuid: SimpleNamespace(
                max_write_without_response_size=244
            )
        )

    async def start_notify(self, _uuid: str, callback: Any) -> None:
        self.notification_callback = callback

    async def stop_notify(self, _uuid: str) -> None:
        self.notification_callback = None

    async def write_gatt_char(self, _uuid: str, data: bytes, *, response: bool) -> None:
        assert response is False
        self.writes.append(bytes(data))
        callback = self.notification_callback
        assert callback is not None
        if data == b"info":
            callback(None, bytearray(b"RVR30"))
        elif data == b"updata":
            callback(None, bytearray(b"\x15"))
        elif len(data) == firmware_module.OTA_PACKET_SIZE:
            callback(None, bytearray(b"\x06"))
        elif data == b"\x04":
            callback(None, bytearray(b"UPDATE_"))
            callback(None, bytearray(b"OK"))


class _NoAckBleakClient(_FakeBleakClient):
    """A bootloader that responds to commands but rejects every data block."""

    async def write_gatt_char(self, _uuid: str, data: bytes, *, response: bool) -> None:
        if len(data) == firmware_module.OTA_PACKET_SIZE:
            self.writes.append(bytes(data))
            self.notification_callback(None, bytearray(b"\x00"))
            return
        await super().write_gatt_char(_uuid, data, response=response)


@pytest.mark.asyncio
async def test_ota_protocol_runs_exact_command_sequence(monkeypatch: Any) -> None:
    """A transfer enters bootloader mode, sends blocks, and commits the image."""
    client = _FakeBleakClient()
    sleep = AsyncMock()
    monkeypatch.setattr(firmware_module.asyncio, "sleep", sleep)
    progress: list[float] = []

    await firmware_module.RenogyOtaProtocol(client).async_update(
        bytes(range(256)), progress.append
    )

    assert client.writes[0] == firmware_module.build_bootloader_command()
    assert client.writes[1] == b"info"
    assert client.writes[2] == b"updata"
    assert client.writes[-1] == b"\x04"
    packets = client.writes[3:-1]
    assert len(packets) == 2
    assert packets[0][1:3] == bytes((1, 0xFE))
    assert packets[1][1:3] == bytes((2, 0xFD))
    assert progress == [50.0, 100.0]
    sleep.assert_any_await(5)
    sleep.assert_any_await(2)
    sleep.assert_any_await(0.05)


@pytest.mark.asyncio
async def test_ota_protocol_rejects_missing_block_ack(monkeypatch: Any) -> None:
    """A controller that never acknowledges a block aborts before commit."""
    client = _NoAckBleakClient()
    monkeypatch.setattr(firmware_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(
        firmware_module.RenogyFirmwareProtocolError,
        match="did not acknowledge firmware block 1",
    ):
        await firmware_module.RenogyOtaProtocol(client).async_update(b"firmware")

    assert client.writes.count(client.writes[-1]) == 3
    assert b"\x04" not in client.writes
