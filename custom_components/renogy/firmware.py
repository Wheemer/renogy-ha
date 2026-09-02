"""Renogy firmware catalog and controller OTA support."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import re
import uuid
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
RENOGY_EXPIRED_TOKEN_CODES = {"SEC002", "SEC003"}
RENOGY_APP_VERSION = "1.10.80"

RENOGY_READ_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
RENOGY_WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"
OTA_BLOCK_SIZE = 128
OTA_PACKET_SIZE = OTA_BLOCK_SIZE + 4
OTA_PAD_BYTE = 0xAA
RENOGY_ANDROID_REQUESTED_MTU = 251
OTA_COMMAND_TIMEOUT = 5.0
OTA_FINAL_TIMEOUT = 30.0
OTA_MAX_FIRMWARE_BYTES = 8 * 1024 * 1024
FIRMWARE_VERSION_PATTERN = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)$")


class RenogyFirmwareError(Exception):
    """Base error for firmware operations."""


class RenogyFirmwareAuthError(RenogyFirmwareError):
    """Renogy account authentication failed."""


class RenogyFirmwareProtocolError(RenogyFirmwareError):
    """The controller rejected or interrupted an OTA operation."""


class RenogyFirmwareTimeoutError(RenogyFirmwareProtocolError):
    """The controller did not answer an OTA command in time."""


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
    md5: str | None
    sku: str


def normalized_firmware_version(version: str | None) -> str:
    """Return Renogy's version without its optional display prefix."""
    return (version or "").strip().lower().removeprefix("v")


def parsed_firmware_version(version: str | None) -> tuple[int, int, int] | None:
    """Parse the exact three-part controller version format."""
    match = FIRMWARE_VERSION_PATTERN.fullmatch((version or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


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
        identity_uuid: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self.auth = auth
        self._identity_uuid = identity_uuid or str(uuid.uuid4())

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
            auth_error=True,
        )
        self.auth = self._parse_auth(payload)
        return self.auth

    async def async_refresh_auth(self) -> RenogyFirmwareAuth:
        """Refresh an expired access token."""
        if self.auth is None or not self.auth.refresh_token:
            raise RenogyFirmwareAuthError("Renogy account is not configured")
        previous_refresh_token = self.auth.refresh_token
        payload = await self._async_json_request(
            "POST",
            RENOGY_REFRESH_PATH,
            json={"refreshToken": self.auth.refresh_token},
            auth_error=True,
        )
        self.auth = self._parse_auth(
            payload, fallback_refresh_token=previous_refresh_token
        )
        return self.auth

    async def async_get_latest_release(
        self, sku: str, type_id: int = CONTROLLER_TYPE_ID
    ) -> RenogyFirmwareRelease | None:
        """Return the first firmware release offered by Renogy for a SKU."""
        if self.auth is None:
            raise RenogyFirmwareAuthError("Renogy account is not configured")

        refreshed_auth = False
        response = await self._async_catalog_request(sku, type_id)
        if response.status == 401:
            response.release()
            await self.async_refresh_auth()
            refreshed_auth = True
            response = await self._async_catalog_request(sku, type_id)

        try:
            payload = await self._async_read_json(response)
        except RenogyFirmwareAuthError:
            if refreshed_auth:
                raise
            await self.async_refresh_auth()
            response = await self._async_catalog_request(sku, type_id)
            payload = await self._async_read_json(response)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        release = data[0]
        if not isinstance(release, dict):
            raise RenogyFirmwareError("Renogy returned an invalid firmware record")

        version = str(release.get("firmwareVersion") or "").strip()
        url = str(release.get("fileUrl") or "").strip()
        md5 = str(release.get("fileMd5") or "").strip().lower() or None
        release_sku = str(release.get("venderSku") or "").strip().upper()
        if (
            parsed_firmware_version(version) is None
            or not url
            or release_sku != sku.upper()
            or (md5 is not None and re.fullmatch(r"[0-9a-f]{32}", md5) is None)
        ):
            raise RenogyFirmwareError("Renogy returned an incomplete firmware record")
        self._validate_download_url(url)
        return RenogyFirmwareRelease(version, url, md5, release_sku)

    async def async_download(self, release: RenogyFirmwareRelease) -> bytes:
        """Download and verify a firmware image."""
        self._validate_download_url(release.url)
        firmware = await self._async_download_once(release.url)

        if release.md5 is not None:
            digest = hashlib.md5(firmware, usedforsecurity=False).hexdigest()
            if not hmac.compare_digest(digest.lower(), release.md5.lower()):
                raise RenogyFirmwareError(
                    "Firmware MD5 does not match Renogy's catalog"
                )
            self._validate_firmware_image(firmware, release)
            return firmware

        # Renogy's live Rover catalog currently omits fileMd5, and its Android
        # BLE OTA path does not consume that field. Fetch twice over separate
        # HTTPS requests and require identical bytes before allowing a flash.
        confirmation = await self._async_download_once(release.url)
        first_sha256 = hashlib.sha256(firmware).digest()
        second_sha256 = hashlib.sha256(confirmation).digest()
        if not hmac.compare_digest(first_sha256, second_sha256):
            raise RenogyFirmwareError(
                "Repeated firmware downloads did not match; refusing to install"
            )
        self._validate_firmware_image(firmware, release)
        return firmware

    async def _async_download_once(self, url: str) -> bytes:
        """Download one bounded firmware image over a validated HTTPS path."""
        try:
            async with self._session.get(url) as response:
                final_url = str(getattr(response, "url", url))
                self._validate_download_url(final_url)
                if not 200 <= response.status < 300:
                    raise RenogyFirmwareError(
                        f"Firmware download failed with HTTP {response.status}"
                    )
                content_length = response.content_length
                if content_length and content_length > OTA_MAX_FIRMWARE_BYTES:
                    raise RenogyFirmwareError("Firmware image exceeds the safety limit")
                firmware_buffer = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    firmware_buffer.extend(chunk)
                    if len(firmware_buffer) > OTA_MAX_FIRMWARE_BYTES:
                        raise RenogyFirmwareError(
                            "Firmware image exceeds the safety limit"
                        )
        except ClientError as err:
            raise RenogyFirmwareError(
                "Could not download firmware from Renogy"
            ) from err

        firmware = bytes(firmware_buffer)

        if not firmware or len(firmware) > OTA_MAX_FIRMWARE_BYTES:
            raise RenogyFirmwareError("Firmware image is empty or too large")
        return firmware

    async def _async_catalog_request(self, sku: str, type_id: int) -> ClientResponse:
        """Start one authenticated catalog request with normalized network errors."""
        assert self.auth is not None
        try:
            return await self._session.get(
                f"{RENOGY_API_BASE}{RENOGY_OTA_CATALOG_PATH}",
                params={"sku": sku, "typeId": type_id},
                headers=self._api_headers(include_token=True),
            )
        except ClientError as err:
            raise RenogyFirmwareError(
                "Could not reach Renogy's firmware service"
            ) from err

    async def _async_json_request(
        self,
        method: str,
        path: str,
        *,
        auth_error: bool = False,
        **kwargs: Any,
    ) -> dict:
        """Make an API request and decode Renogy's response envelope."""
        try:
            request_headers = self._api_headers(include_token=False)
            supplied_headers = kwargs.pop("headers", None)
            if supplied_headers:
                request_headers.update(supplied_headers)
            response = await self._session.request(
                method,
                f"{RENOGY_API_BASE}{path}",
                headers=request_headers,
                **kwargs,
            )
        except ClientError as err:
            raise RenogyFirmwareError(
                "Could not reach Renogy's firmware service"
            ) from err
        return await self._async_read_json(response, auth_error=auth_error)

    async def _async_read_json(
        self, response: ClientResponse, *, auth_error: bool = False
    ) -> dict:
        """Decode one Renogy response and retain its useful error message."""
        try:
            payload = await response.json(content_type=None)
        except (ClientError, ValueError) as err:
            raise RenogyFirmwareError("Renogy returned an invalid response") from err
        if response.status >= 400 or not isinstance(payload, dict):
            message = payload.get("msg") if isinstance(payload, dict) else None
            if auth_error or response.status in (401, 403):
                raise RenogyFirmwareAuthError(message or "Renogy login was rejected")
            raise RenogyFirmwareError(
                message or f"Renogy request failed with HTTP {response.status}"
            )
        api_code = payload.get("code")
        if api_code is not None and str(api_code) != "000000":
            message = str(payload.get("msg") or payload.get("message") or api_code)
            if auth_error or str(api_code) in RENOGY_EXPIRED_TOKEN_CODES:
                raise RenogyFirmwareAuthError(message)
            raise RenogyFirmwareError(message)
        return payload

    def _api_headers(self, *, include_token: bool) -> dict[str, str]:
        """Return the device headers required by Renogy's Android API."""
        headers = {
            "Accept-Language": "en-CA",
            "User-Agent": "Renogy Home Assistant Integration/0.9.0",
            "app-version": RENOGY_APP_VERSION,
            "device-mode": "Home Assistant",
            "device-version": "2026.9",
            "device-manufacturer": "Home Assistant",
            "identity-uuid": self._identity_uuid,
            "request-channel": "android",
        }
        if include_token and self.auth is not None:
            headers["x-token"] = self.auth.access_token
        return headers

    @staticmethod
    def _parse_auth(
        payload: dict, fallback_refresh_token: str | None = None
    ) -> RenogyFirmwareAuth:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RenogyFirmwareAuthError(payload.get("msg") or "Login failed")
        access_token = str(data.get("accessToken") or "")
        refresh_token = str(data.get("refreshToken") or fallback_refresh_token or "")
        if not access_token or not refresh_token:
            raise RenogyFirmwareAuthError(payload.get("msg") or "Login failed")
        return RenogyFirmwareAuth(access_token, refresh_token)

    @staticmethod
    def _validate_download_url(url: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.lower().endswith(".bin")
        ):
            raise RenogyFirmwareError("Renogy returned an unsafe firmware URL")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise RenogyFirmwareError("Renogy returned an unsafe firmware URL")

    @staticmethod
    def _validate_firmware_image(
        firmware: bytes, release: RenogyFirmwareRelease
    ) -> None:
        """Validate the Rover image header recovered from Renogy's catalog file."""
        if len(firmware) < 128:
            raise RenogyFirmwareError("Firmware image is too small for a Rover image")
        declared_size = int.from_bytes(firmware[:4], "little")
        if declared_size != len(firmware):
            raise RenogyFirmwareError(
                "Firmware image length does not match its Rover header"
            )
        if release.sku.encode("ascii") not in firmware[:128]:
            raise RenogyFirmwareError(
                "Firmware image header is not for the expected Rover SKU"
            )


def firmware_identity_uuid(entry_id: str) -> str:
    """Return a stable API device identity for one Home Assistant entry."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"home-assistant://renogy/{entry_id}"))


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

    def __init__(
        self, client: BleakClient, controller_address: int | None = None
    ) -> None:
        """Initialize the OTA transport."""
        self._client = client
        self._controller_address = (
            controller_address
            if controller_address is not None and 1 <= controller_address <= 247
            else None
        )
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
        await self._ensure_packet_mtu()
        await self._client.start_notify(
            RENOGY_READ_CHAR_UUID, self._notification_handler
        )
        try:
            used_addressed_boot = await self._enter_bootloader()
            response = await self._command_with_timeout_retries(b"updata", attempts=3)
            if response != b"\x15":
                raise RenogyFirmwareProtocolError(
                    f"Controller rejected OTA start: {response.hex()}"
                )
            await asyncio.sleep(2)

            for index, block in enumerate(blocks, start=1):
                packet = build_ota_packet(index, block)
                response = await self._command_with_timeout_retries(packet, attempts=3)
                if response != b"\x06":
                    raise RenogyFirmwareProtocolError(
                        f"Controller did not acknowledge firmware block {index}"
                    )
                if progress_callback is not None:
                    progress_callback(index * 100 / len(blocks))

            await asyncio.sleep(0.05)
            response = await self._command_with_timeout_retries(b"\x04", attempts=3)
            if not response:
                raise RenogyFirmwareProtocolError(
                    "Controller did not acknowledge firmware completion"
                )
            if used_addressed_boot:
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
            raise RenogyFirmwareTimeoutError(
                f"Controller timed out after command {data[:8].hex()}"
            ) from err

    async def _enter_bootloader(self) -> bool:
        """Enter OTA mode using Renogy's broadcast then addressed fallback."""
        await self._write(build_bootloader_command())
        await asyncio.sleep(5)
        try:
            await self._command_expect_any(b"info", attempts=3)
            return False
        except RenogyFirmwareTimeoutError:
            if self._controller_address is None:
                raise

        await self._write(build_bootloader_command(self._controller_address))
        await asyncio.sleep(5)
        await self._command_expect_any(b"info", attempts=1)
        return True

    async def _command_with_timeout_retries(
        self, data: bytes, *, attempts: int
    ) -> bytes:
        """Retry only command timeouts, matching Renogy's Android updater."""
        for attempt in range(1, attempts + 1):
            try:
                return await self._command(data)
            except RenogyFirmwareTimeoutError:
                if attempt == attempts:
                    raise
        raise AssertionError("unreachable")

    async def _command_expect_any(self, data: bytes, *, attempts: int) -> None:
        response = await self._command_with_timeout_retries(data, attempts=attempts)
        if not response:
            raise RenogyFirmwareProtocolError("Controller did not enter OTA mode")

    async def _wait_for_update_ok(self) -> None:
        """Wait for the extra completion message used by the addressed fallback."""
        deadline = asyncio.get_running_loop().time() + OTA_FINAL_TIMEOUT
        received = b""
        while asyncio.get_running_loop().time() < deadline:
            timeout = deadline - asyncio.get_running_loop().time()
            try:
                received += await asyncio.wait_for(self._responses.get(), timeout)
            except TimeoutError as err:
                raise RenogyFirmwareTimeoutError(
                    "Controller did not confirm UPDATE_OK"
                ) from err
            if b"UPDATE_OK" in received:
                return
        raise RenogyFirmwareTimeoutError("Controller did not confirm UPDATE_OK")

    def _drain_responses(self) -> None:
        while not self._responses.empty():
            self._responses.get_nowait()

    async def _ensure_packet_mtu(self) -> None:
        """Confirm the negotiated connection can carry the app's 132-byte frame."""
        characteristic = self._client.services.get_characteristic(
            RENOGY_WRITE_CHAR_UUID
        )
        if characteristic is None:
            raise RenogyFirmwareProtocolError(
                "Renogy OTA write characteristic is missing"
            )
        characteristic_size = characteristic.max_write_without_response_size
        if characteristic_size >= OTA_PACKET_SIZE:
            return

        negotiated_size = 0
        acquire_mtu = getattr(
            getattr(self._client, "_backend", None), "_acquire_mtu", None
        )
        if callable(acquire_mtu):
            await acquire_mtu()
            mtu_size = getattr(self._client, "mtu_size", 0)
            if isinstance(mtu_size, int):
                negotiated_size = max(0, mtu_size - 3)

        characteristic_size = characteristic.max_write_without_response_size
        if max(characteristic_size, negotiated_size) < OTA_PACKET_SIZE:
            raise RenogyFirmwareProtocolError(
                "Bluetooth path cannot carry Renogy's 132-byte OTA packet "
                f"(characteristic={characteristic_size}, negotiated={negotiated_size}, "
                f"Android requests MTU {RENOGY_ANDROID_REQUESTED_MTU})"
            )
