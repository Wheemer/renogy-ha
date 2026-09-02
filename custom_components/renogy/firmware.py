"""Renogy firmware catalog and controller OTA support."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientResponse, ClientSession
from bleak import BleakClient
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

RENOGY_API_BASE = "https://gateway.renogy.com"
RENOGY_LOGIN_PATH = "/api/v1/account/app/do_login"
RENOGY_REFRESH_PATH = "/api/v1/account/app/do_refresh"
RENOGY_OTA_CATALOG_PATH = "/api/v1/device/user/me/getBleOTAFirmwareList"
ROVER_30_SKU = "RNG-CTRL-RVR30"
CONTROLLER_TYPE_ID = 14

RENOGY_READ_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
RENOGY_WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"
OTA_BLOCK_SIZE = 128
OTA_PACKET_SIZE = OTA_BLOCK_SIZE + 4
OTA_PAD_BYTE = 0xAA
OTA_COMMAND_TIMEOUT = 5.0
OTA_FINAL_TIMEOUT = 30.0
OTA_MAX_FIRMWARE_BYTES = 8 * 1024 * 1024


class RenogyFirmwareError(Exception):
    """Base error for firmware operations."""


class RenogyFirmwareAuthError(RenogyFirmwareError):
    """Renogy account authentication failed."""


class RenogyFirmwareProtocolError(RenogyFirmwareError):
    """The controller rejected or interrupted an OTA operation."""


@dataclass(slots=True)
class RenogyFirmwareAuth:
    """Persisted Renogy API tokens."""

    access_token: str
    refresh_token: str


@dataclass(slots=True)
class RenogyFirmwareRelease:
    """One firmware release returned by Renogy's catalog."""

    version: str
    url: str
    md5: str
    sku: str


class RenogyFirmwareAuthStore:
    """Store firmware credentials without putting passwords in config entries."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize token storage."""
        self._store: Store[dict[str, str]] = Store(
            hass, 1, f"renogy_firmware_auth_{entry_id}"
        )

    async def async_load(self) -> RenogyFirmwareAuth | None:
        """Load saved API tokens."""
        data = await self._store.async_load()
        if not data or not data.get("access_token") or not data.get("refresh_token"):
            return None
        return RenogyFirmwareAuth(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
        )

    async def async_save(self, auth: RenogyFirmwareAuth) -> None:
        """Persist API tokens."""
        await self._store.async_save(asdict(auth))

    async def async_clear(self) -> None:
        """Remove saved API tokens."""
        await self._store.async_remove()


class RenogyFirmwareClient:
    """Small client for Renogy's authenticated firmware API."""

    def __init__(
        self,
        session: ClientSession,
        auth: RenogyFirmwareAuth | None = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self.auth = auth

    async def async_login(self, identifier: str, password: str) -> RenogyFirmwareAuth:
        """Exchange a Renogy account login for access and refresh tokens."""
        payload = await self._async_json_request(
            "POST",
            RENOGY_LOGIN_PATH,
            json={
                "identifier": identifier,
                "credential": password,
                "loginType": 0,
            },
        )
        self.auth = self._parse_auth(payload)
        return self.auth

    async def async_refresh_auth(self) -> RenogyFirmwareAuth:
        """Refresh an expired access token."""
        if self.auth is None or not self.auth.refresh_token:
            raise RenogyFirmwareAuthError("Renogy account is not configured")
        payload = await self._async_json_request(
            "POST",
            RENOGY_REFRESH_PATH,
            json={"refreshToken": self.auth.refresh_token},
        )
        self.auth = self._parse_auth(payload)
        return self.auth

    async def async_get_latest_release(
        self, sku: str, type_id: int = CONTROLLER_TYPE_ID
    ) -> RenogyFirmwareRelease | None:
        """Return the first firmware release offered by Renogy for a SKU."""
        if self.auth is None:
            raise RenogyFirmwareAuthError("Renogy account is not configured")

        response = await self._session.get(
            f"{RENOGY_API_BASE}{RENOGY_OTA_CATALOG_PATH}",
            params={"sku": sku, "typeId": type_id},
            headers={"x-token": self.auth.access_token},
        )
        if response.status == 401:
            response.release()
            await self.async_refresh_auth()
            response = await self._session.get(
                f"{RENOGY_API_BASE}{RENOGY_OTA_CATALOG_PATH}",
                params={"sku": sku, "typeId": type_id},
                headers={"x-token": self.auth.access_token},
            )

        payload = await self._async_read_json(response)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        release = data[0]
        if not isinstance(release, dict):
            raise RenogyFirmwareError("Renogy returned an invalid firmware record")

        version = str(release.get("firmwareVersion") or "").strip()
        url = str(release.get("fileUrl") or "").strip()
        md5 = str(release.get("fileMd5") or "").strip().lower()
        release_sku = str(release.get("venderSku") or sku).strip()
        if not version or not url or len(md5) != 32:
            raise RenogyFirmwareError("Renogy returned an incomplete firmware record")
        self._validate_download_url(url)
        return RenogyFirmwareRelease(version, url, md5, release_sku)

    async def async_download(self, release: RenogyFirmwareRelease) -> bytes:
        """Download and verify a firmware image."""
        self._validate_download_url(release.url)
        async with self._session.get(release.url) as response:
            if response.status >= 400:
                raise RenogyFirmwareError(
                    f"Firmware download failed with HTTP {response.status}"
                )
            content_length = response.content_length
            if content_length and content_length > OTA_MAX_FIRMWARE_BYTES:
                raise RenogyFirmwareError("Firmware image exceeds the safety limit")
            firmware = await response.read()

        if not firmware or len(firmware) > OTA_MAX_FIRMWARE_BYTES:
            raise RenogyFirmwareError("Firmware image is empty or too large")
        digest = hashlib.md5(firmware, usedforsecurity=False).hexdigest()
        if digest.lower() != release.md5.lower():
            raise RenogyFirmwareError("Firmware MD5 does not match Renogy's catalog")
        return firmware

    async def _async_json_request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Make an API request and decode Renogy's response envelope."""
        try:
            response = await self._session.request(
                method, f"{RENOGY_API_BASE}{path}", **kwargs
            )
        except ClientError as err:
            raise RenogyFirmwareError(
                "Could not reach Renogy's firmware service"
            ) from err
        return await self._async_read_json(response)

    async def _async_read_json(self, response: ClientResponse) -> dict:
        """Decode one Renogy response and retain its useful error message."""
        try:
            payload = await response.json(content_type=None)
        except (ClientError, ValueError) as err:
            raise RenogyFirmwareError("Renogy returned an invalid response") from err
        if response.status >= 400 or not isinstance(payload, dict):
            message = payload.get("msg") if isinstance(payload, dict) else None
            if response.status in (401, 403):
                raise RenogyFirmwareAuthError(message or "Renogy login was rejected")
            raise RenogyFirmwareError(
                message or f"Renogy request failed with HTTP {response.status}"
            )
        return payload

    @staticmethod
    def _parse_auth(payload: dict) -> RenogyFirmwareAuth:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RenogyFirmwareAuthError(payload.get("msg") or "Login failed")
        access_token = str(data.get("accessToken") or "")
        refresh_token = str(data.get("refreshToken") or "")
        if not access_token or not refresh_token:
            raise RenogyFirmwareAuthError(payload.get("msg") or "Login failed")
        return RenogyFirmwareAuth(access_token, refresh_token)

    @staticmethod
    def _validate_download_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RenogyFirmwareError("Renogy returned an unsafe firmware URL")


def _modbus_crc(data: bytes) -> bytes:
    """Return Modbus CRC16 in Renogy wire order."""
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def build_bootloader_command(address: int = 0xFF) -> bytes:
    """Build the app's command that switches a controller into OTA mode."""
    command = bytes((address, 0x41, 0x00, 0x00, 0x00, 0x00))
    return command + _modbus_crc(command)


def build_ota_packet(sequence: int, block: bytes) -> bytes:
    """Build one Renogy 128-byte OTA packet recovered from the Android app."""
    if sequence < 1:
        raise ValueError("OTA sequence must be positive")
    if len(block) > OTA_BLOCK_SIZE:
        raise ValueError("OTA block is larger than 128 bytes")
    padded = block.ljust(OTA_BLOCK_SIZE, bytes((OTA_PAD_BYTE,)))
    wire_sequence = sequence & 0xFF
    header = bytes((0x01, wire_sequence, (~wire_sequence) & 0xFF))
    checksum = bytes((sum(padded) & 0xFF,))
    return header + padded + checksum


class RenogyOtaProtocol:
    """Run the Rover OTA protocol over an already connected Bleak client."""

    def __init__(self, client: BleakClient) -> None:
        """Initialize the OTA transport."""
        self._client = client
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()

    async def async_update(
        self,
        firmware: bytes,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        """Transfer and commit a verified firmware image."""
        if not firmware:
            raise RenogyFirmwareProtocolError("Firmware image is empty")
        blocks = [
            firmware[offset : offset + OTA_BLOCK_SIZE]
            for offset in range(0, len(firmware), OTA_BLOCK_SIZE)
        ]
        await self._client.start_notify(
            RENOGY_READ_CHAR_UUID, self._notification_handler
        )
        try:
            await self._ensure_packet_mtu()
            await self._write(build_bootloader_command())
            await asyncio.sleep(5)

            await self._command_expect_any(b"info")
            response = await self._command(b"updata")
            if b"\x15" not in response:
                raise RenogyFirmwareProtocolError(
                    f"Controller rejected OTA start: {response.hex()}"
                )
            await asyncio.sleep(2)

            for index, block in enumerate(blocks, start=1):
                packet = build_ota_packet(index, block)
                acknowledged = False
                for _attempt in range(3):
                    response = await self._command(packet)
                    if b"\x06" in response:
                        acknowledged = True
                        break
                if not acknowledged:
                    raise RenogyFirmwareProtocolError(
                        f"Controller did not acknowledge firmware block {index}"
                    )
                if progress_callback is not None:
                    progress_callback(index * 100 / len(blocks))

            await asyncio.sleep(0.05)
            self._drain_responses()
            await self._write(b"\x04")
            await self._wait_for_update_ok()
        finally:
            try:
                await self._client.stop_notify(RENOGY_READ_CHAR_UUID)
            except Exception:  # noqa: BLE001
                pass

    def _notification_handler(self, _sender: Any, data: bytearray) -> None:
        """Queue controller notifications for the active command."""
        self._responses.put_nowait(bytes(data))

    async def _write(self, data: bytes) -> None:
        await self._client.write_gatt_char(RENOGY_WRITE_CHAR_UUID, data, response=False)

    async def _command(self, data: bytes) -> bytes:
        self._drain_responses()
        await self._write(data)
        try:
            return await asyncio.wait_for(
                self._responses.get(), timeout=OTA_COMMAND_TIMEOUT
            )
        except TimeoutError as err:
            raise RenogyFirmwareProtocolError(
                f"Controller timed out after command {data[:8].hex()}"
            ) from err

    async def _command_expect_any(self, data: bytes) -> None:
        response = await self._command(data)
        if not response:
            raise RenogyFirmwareProtocolError("Controller did not enter OTA mode")

    async def _wait_for_update_ok(self) -> None:
        deadline = asyncio.get_running_loop().time() + OTA_FINAL_TIMEOUT
        received = b""
        while asyncio.get_running_loop().time() < deadline:
            timeout = deadline - asyncio.get_running_loop().time()
            try:
                received += await asyncio.wait_for(self._responses.get(), timeout)
            except TimeoutError as err:
                raise RenogyFirmwareProtocolError(
                    "Controller did not confirm UPDATE_OK"
                ) from err
            if b"UPDATE_OK" in received:
                return
        raise RenogyFirmwareProtocolError("Controller did not confirm UPDATE_OK")

    def _drain_responses(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    async def _ensure_packet_mtu(self) -> None:
        """Ask BlueZ for an MTU large enough for the app's 132-byte frame."""
        characteristic = self._client.services.get_characteristic(
            RENOGY_WRITE_CHAR_UUID
        )
        if characteristic is None:
            raise RenogyFirmwareProtocolError(
                "Renogy OTA write characteristic is missing"
            )
        if characteristic.max_write_without_response_size >= OTA_PACKET_SIZE:
            return

        acquire_mtu = getattr(
            getattr(self._client, "_backend", None), "_acquire_mtu", None
        )
        if callable(acquire_mtu):
            await acquire_mtu()
        if characteristic.max_write_without_response_size < OTA_PACKET_SIZE:
            raise RenogyFirmwareProtocolError(
                "Bluetooth path did not negotiate the 132-byte OTA packet size"
            )
