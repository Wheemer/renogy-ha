"""Tests for Renogy's recovered controller OTA protocol."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


def _rover_firmware_image(size: int = 256) -> bytes:
    """Build the minimum Rover image envelope observed in Renogy's live file."""
    image = bytearray(b"\x00" * size)
    image[:4] = size.to_bytes(4, "little")
    sku = firmware_module.ROVER_30_SKU.encode("ascii")
    image[0x20 : 0x20 + len(sku)] = sku
    return bytes(image)


class _FakeJsonResponse:
    """Minimal aiohttp response for API-envelope tests."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def json(self, *, content_type: Any = None) -> Any:
        del content_type
        return self.payload

    def release(self) -> None:
        """Mirror the response cleanup used for HTTP authentication failures."""


class _FakeContent:
    """Stream a fixed list of firmware chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int) -> Any:
        for chunk in self.chunks:
            yield chunk


class _FakeDownloadResponse:
    """Minimal async context manager returned by ClientSession.get."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        content_length: int | None = None,
        url: str = "https://example.com/controller.bin",
    ) -> None:
        self.status = status
        self.content_length = content_length
        self.content = _FakeContent(chunks)
        self.url = url

    async def __aenter__(self) -> _FakeDownloadResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeDownloadSession:
    """Return one prepared firmware response."""

    def __init__(self, response: _FakeDownloadResponse) -> None:
        self.response = response

    def get(self, _url: str) -> _FakeDownloadResponse:
        return self.response


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


@pytest.mark.asyncio
async def test_api_envelope_error_is_not_treated_as_empty_catalog() -> None:
    """Renogy often reports service errors inside an HTTP 200 response."""
    client = firmware_module.RenogyFirmwareClient(SimpleNamespace())
    response = _FakeJsonResponse({"code": "TOKEN_IS_REQUIRED", "data": None})

    with pytest.raises(firmware_module.RenogyFirmwareError, match="TOKEN_IS_REQUIRED"):
        await client._async_read_json(response)


@pytest.mark.asyncio
async def test_login_sends_required_android_api_headers() -> None:
    """Renogy rejects otherwise valid login requests without device headers."""
    response = _FakeJsonResponse(
        {"code": "UAC102", "msg": "Incorrect account or password."}, status=400
    )
    session = SimpleNamespace(request=AsyncMock(return_value=response))
    client = firmware_module.RenogyFirmwareClient(
        session, identity_uuid="58e1b1cc-4edf-4ac9-8361-137fdc193f5b"
    )

    with pytest.raises(
        firmware_module.RenogyFirmwareAuthError,
        match="Incorrect account or password",
    ):
        await client.async_login("owner@example.com", "incorrect")

    headers = session.request.await_args.kwargs["headers"]
    assert headers["app-version"] == firmware_module.RENOGY_APP_VERSION
    assert headers["identity-uuid"] == "58e1b1cc-4edf-4ac9-8361-137fdc193f5b"
    assert headers["request-channel"] == "android"
    assert headers["device-mode"] == "Home Assistant"


def test_firmware_identity_is_stable_per_config_entry() -> None:
    """Login and later catalog checks must present the same device identity."""
    first = firmware_module.firmware_identity_uuid("entry-1")

    assert first == firmware_module.firmware_identity_uuid("entry-1")
    assert first != firmware_module.firmware_identity_uuid("entry-2")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["SEC002", "SEC003"])
async def test_catalog_refreshes_app_reported_expired_token(code: str) -> None:
    """Match DC Home's token interceptor for HTTP-200 expiry responses."""
    expired = _FakeJsonResponse({"code": code, "msg": "expired", "data": None})
    refreshed = _FakeJsonResponse({"code": "000000", "data": []})
    session = SimpleNamespace(get=AsyncMock(side_effect=[expired, refreshed]))
    client = firmware_module.RenogyFirmwareClient(
        session,
        firmware_module.RenogyFirmwareAuth("old-access", "refresh-token"),
    )
    client.async_refresh_auth = AsyncMock()

    release = await client.async_get_latest_release(firmware_module.ROVER_30_SKU)

    assert release is None
    client.async_refresh_auth.assert_awaited_once()
    assert session.get.await_count == 2


@pytest.mark.asyncio
async def test_catalog_does_not_loop_when_refreshed_token_is_rejected() -> None:
    """A failed replacement token stops after the app's single retry."""
    unauthorized = _FakeJsonResponse({"code": "SEC003", "data": None}, status=401)
    session = SimpleNamespace(get=AsyncMock(side_effect=[unauthorized, unauthorized]))
    client = firmware_module.RenogyFirmwareClient(
        session,
        firmware_module.RenogyFirmwareAuth("old-access", "refresh-token"),
    )
    client.async_refresh_auth = AsyncMock()

    with pytest.raises(firmware_module.RenogyFirmwareAuthError):
        await client.async_get_latest_release(firmware_module.ROVER_30_SKU)

    client.async_refresh_auth.assert_awaited_once()
    assert session.get.await_count == 2


@pytest.mark.asyncio
async def test_catalog_accepts_release_without_optional_md5() -> None:
    """The live Rover catalog may omit MD5 while still offering a release."""
    response = _FakeJsonResponse(
        {
            "code": "000000",
            "data": [
                {
                    "firmwareVersion": "2.0.1",
                    "fileUrl": "https://example.com/RVR30.bin",
                    "fileMd5": None,
                    "venderSku": firmware_module.ROVER_30_SKU,
                }
            ],
        }
    )
    session = SimpleNamespace(get=AsyncMock(return_value=response))
    client = firmware_module.RenogyFirmwareClient(
        session,
        firmware_module.RenogyFirmwareAuth("access", "refresh"),
    )

    release = await client.async_get_latest_release(firmware_module.ROVER_30_SKU)

    assert release is not None
    assert release.version == "2.0.1"
    assert release.md5 is None


@pytest.mark.asyncio
async def test_catalog_rejects_release_for_another_sku() -> None:
    """A catalog response cannot redirect a Rover entry to another controller."""
    response = _FakeJsonResponse(
        {
            "code": "000000",
            "data": [
                {
                    "firmwareVersion": "2.0.1",
                    "fileUrl": "https://example.com/RVR40.bin",
                    "fileMd5": "0" * 32,
                    "venderSku": "RNG-CTRL-RVR40",
                }
            ],
        }
    )
    client = firmware_module.RenogyFirmwareClient(
        SimpleNamespace(get=AsyncMock(return_value=response)),
        firmware_module.RenogyFirmwareAuth("access", "refresh"),
    )

    with pytest.raises(firmware_module.RenogyFirmwareError, match="incomplete"):
        await client.async_get_latest_release(firmware_module.ROVER_30_SKU)


def test_refresh_auth_keeps_existing_refresh_token() -> None:
    """The app preserves the old refresh token when refresh returns only access."""
    auth = firmware_module.RenogyFirmwareClient._parse_auth(
        {"data": {"accessToken": "new-access"}},
        fallback_refresh_token="existing-refresh",
    )

    assert auth.access_token == "new-access"
    assert auth.refresh_token == "existing-refresh"


@pytest.mark.asyncio
async def test_download_stream_is_verified() -> None:
    """A streamed image is accepted only when it matches the catalog MD5."""
    firmware = _rover_firmware_image()
    release = firmware_module.RenogyFirmwareRelease(
        version="1.2.3",
        url="https://example.com/controller.bin",
        md5=hashlib.md5(firmware, usedforsecurity=False).hexdigest(),
        sku=firmware_module.ROVER_30_SKU,
    )
    session = _FakeDownloadSession(_FakeDownloadResponse([firmware[:5], firmware[5:]]))

    result = await firmware_module.RenogyFirmwareClient(session).async_download(release)

    assert result == firmware


@pytest.mark.asyncio
async def test_download_is_accepted_when_catalog_omits_md5() -> None:
    """Two identical HTTPS downloads replace Renogy's omitted checksum."""
    firmware = _rover_firmware_image()
    release = firmware_module.RenogyFirmwareRelease(
        version="2.0.1",
        url="https://example.com/controller.bin",
        md5=None,
        sku=firmware_module.ROVER_30_SKU,
    )
    session = _FakeDownloadSession(_FakeDownloadResponse([firmware]))

    result = await firmware_module.RenogyFirmwareClient(session).async_download(release)

    assert result == firmware


def test_firmware_image_requires_matching_length_and_sku() -> None:
    """A downloaded binary must identify itself as this exact Rover model."""
    release = firmware_module.RenogyFirmwareRelease(
        version="2.0.1",
        url="https://example.com/controller.bin",
        md5=None,
        sku=firmware_module.ROVER_30_SKU,
    )
    wrong_size = bytearray(_rover_firmware_image())
    wrong_size[:4] = (len(wrong_size) + 1).to_bytes(4, "little")
    wrong_sku = bytearray(_rover_firmware_image())
    wrong_sku[0x20:0x30] = b"WRONG-CONTROLLER"

    with pytest.raises(firmware_module.RenogyFirmwareError, match="length"):
        firmware_module.RenogyFirmwareClient._validate_firmware_image(
            bytes(wrong_size), release
        )
    with pytest.raises(firmware_module.RenogyFirmwareError, match="expected Rover SKU"):
        firmware_module.RenogyFirmwareClient._validate_firmware_image(
            bytes(wrong_sku), release
        )


@pytest.mark.asyncio
async def test_download_without_md5_rejects_changed_second_copy() -> None:
    """A checksum-free release fails closed when repeated downloads differ."""
    release = firmware_module.RenogyFirmwareRelease(
        version="2.0.1",
        url="https://example.com/controller.bin",
        md5=None,
        sku=firmware_module.ROVER_30_SKU,
    )
    session = SimpleNamespace(
        get=MagicMock(
            side_effect=[
                _FakeDownloadResponse([b"first"]),
                _FakeDownloadResponse([b"second"]),
            ]
        )
    )

    with pytest.raises(firmware_module.RenogyFirmwareError, match="did not match"):
        await firmware_module.RenogyFirmwareClient(session).async_download(release)


def test_download_url_rejects_local_redirect_and_non_binary_path() -> None:
    """Firmware downloads cannot redirect into the local network or non-bin data."""
    for url in (
        "https://127.0.0.1/controller.bin",
        "https://192.168.1.40/controller.bin",
        "https://example.com/controller.json",
    ):
        with pytest.raises(firmware_module.RenogyFirmwareError, match="unsafe"):
            firmware_module.RenogyFirmwareClient._validate_download_url(url)


@pytest.mark.asyncio
async def test_download_without_content_length_still_enforces_limit(
    monkeypatch: Any,
) -> None:
    """Chunked downloads cannot bypass the firmware size safety limit."""
    monkeypatch.setattr(firmware_module, "OTA_MAX_FIRMWARE_BYTES", 4)
    release = firmware_module.RenogyFirmwareRelease(
        version="1.2.3",
        url="https://example.com/controller.bin",
        md5="0" * 32,
        sku=firmware_module.ROVER_30_SKU,
    )
    session = _FakeDownloadSession(_FakeDownloadResponse([b"123", b"45"]))

    with pytest.raises(
        firmware_module.RenogyFirmwareError, match="exceeds the safety limit"
    ):
        await firmware_module.RenogyFirmwareClient(session).async_download(release)


class _FakeBleakClient:
    """A controller transport that responds like Renogy's bootloader."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.events: list[str] = []
        self.notification_callback: Any = None
        self.services = SimpleNamespace(
            get_characteristic=lambda _uuid: SimpleNamespace(
                max_write_without_response_size=244
            )
        )

    async def start_notify(self, _uuid: str, callback: Any) -> None:
        self.events.append("notify")
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


class _EmbeddedAckBleakClient(_FakeBleakClient):
    """A bootloader returning extra bytes around ACK must not be accepted."""

    async def write_gatt_char(self, _uuid: str, data: bytes, *, response: bool) -> None:
        if len(data) == firmware_module.OTA_PACKET_SIZE:
            self.writes.append(bytes(data))
            self.notification_callback(None, bytearray(b"noise\x06noise"))
            return
        await super().write_gatt_char(_uuid, data, response=response)


class _StaleMtuBleakClient(_FakeBleakClient):
    """BlueZ can negotiate a large MTU while its characteristic cache says 20."""

    def __init__(self) -> None:
        super().__init__()
        self.mtu_size = 23
        self.services = SimpleNamespace(
            get_characteristic=lambda _uuid: SimpleNamespace(
                max_write_without_response_size=20
            )
        )

        async def _acquire_mtu() -> None:
            self.events.append("mtu")
            self.mtu_size = 251

        self._backend = SimpleNamespace(_acquire_mtu=_acquire_mtu)


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
    assert client.events[0] == "notify"
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

    assert client.writes.count(client.writes[-1]) == 1
    assert b"\x04" not in client.writes


@pytest.mark.asyncio
async def test_ota_protocol_requires_exact_block_ack(monkeypatch: Any) -> None:
    """Incidental ACK bytes in unrelated responses cannot advance the transfer."""
    client = _EmbeddedAckBleakClient()
    monkeypatch.setattr(firmware_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(
        firmware_module.RenogyFirmwareProtocolError,
        match="did not acknowledge firmware block 1",
    ):
        await firmware_module.RenogyOtaProtocol(client).async_update(b"firmware")


@pytest.mark.asyncio
async def test_ota_uses_negotiated_bluez_mtu_when_characteristic_cache_is_stale(
    monkeypatch: Any,
) -> None:
    """The acquired ATT MTU is authoritative when BlueZ still reports 20."""
    client = _StaleMtuBleakClient()
    monkeypatch.setattr(firmware_module.asyncio, "sleep", AsyncMock())

    await firmware_module.RenogyOtaProtocol(client).async_update(b"firmware")

    assert client.events[:2] == ["mtu", "notify"]


@pytest.mark.asyncio
async def test_ota_retries_timeouts_but_not_negative_acknowledgements() -> None:
    """Match the app's two retries for silence without repeating a NACK."""
    protocol = firmware_module.RenogyOtaProtocol(_FakeBleakClient())
    protocol._command = AsyncMock(
        side_effect=[
            firmware_module.RenogyFirmwareTimeoutError("timed out"),
            b"\x06",
        ]
    )

    response = await protocol._command_with_timeout_retries(b"packet", attempts=3)

    assert response == b"\x06"
    assert protocol._command.await_count == 2


@pytest.mark.asyncio
async def test_ota_uses_controller_address_only_after_broadcast_timeout(
    monkeypatch: Any,
) -> None:
    """Mirror the APK's addressed boot fallback after three silent info reads."""
    protocol = firmware_module.RenogyOtaProtocol(
        _FakeBleakClient(), controller_address=1
    )
    protocol._write = AsyncMock()
    protocol._command_expect_any = AsyncMock(
        side_effect=[firmware_module.RenogyFirmwareTimeoutError("timed out"), None]
    )
    monkeypatch.setattr(firmware_module.asyncio, "sleep", AsyncMock())

    used_addressed_boot = await protocol._enter_bootloader()

    assert used_addressed_boot is True
    assert protocol._write.await_args_list == [
        ((firmware_module.build_bootloader_command(),), {}),
        ((firmware_module.build_bootloader_command(1),), {}),
    ]
    assert protocol._command_expect_any.await_args_list == [
        ((b"info",), {"attempts": 3}),
        ((b"info",), {"attempts": 1}),
    ]
