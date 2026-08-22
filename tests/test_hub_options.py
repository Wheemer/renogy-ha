"""Tests for Communication Hub options-flow behavior."""

from __future__ import annotations

import asyncio
import importlib
import json
import types
from pathlib import Path
from typing import Any

from tests.test_config_flow import _load_config_flow_module, _schema_default


def _entry(
    const_module: Any,
    *,
    device_type: str,
    options: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal config entry for options-flow tests."""
    return types.SimpleNamespace(
        data={const_module.CONF_DEVICE_TYPE: device_type},
        options=options or {},
    )


def _schema_fields(schema: Any) -> set[str]:
    """Return field names from a voluptuous schema."""
    return {str(key.schema) for key in schema.schema}


def test_non_shunt_hub_option_defaults_to_disabled() -> None:
    """Existing non-shunt entries should show Hub polling disabled by default."""
    config_flow_module = _load_config_flow_module()
    const_module = importlib.import_module("custom_components.renogy.const")
    handler = config_flow_module.RenogyOptionsFlowHandler(
        _entry(const_module, device_type=const_module.DeviceType.INVERTER.value)
    )

    result = asyncio.run(handler.async_step_init())

    assert result["type"] == "form"
    assert (
        _schema_default(
            result["data_schema"],
            const_module.CONF_COMMUNICATION_HUB_ENABLED,
        )
        is False
    )
    assert (
        _schema_default(
            result["data_schema"],
            const_module.CONF_NON_SHUNT_CONNECTION_MODE,
        )
        == const_module.DEFAULT_NON_SHUNT_CONNECTION_MODE
    )


def test_non_shunt_hub_option_preserves_enabled_value() -> None:
    """The options form should preserve an already enabled Hub setting."""
    config_flow_module = _load_config_flow_module()
    const_module = importlib.import_module("custom_components.renogy.const")
    handler = config_flow_module.RenogyOptionsFlowHandler(
        _entry(
            const_module,
            device_type=const_module.DeviceType.INVERTER.value,
            options={
                const_module.CONF_NON_SHUNT_CONNECTION_MODE: "persistent_session",
                const_module.CONF_COMMUNICATION_HUB_ENABLED: True,
            },
        )
    )

    result = asyncio.run(handler.async_step_init())

    assert (
        _schema_default(
            result["data_schema"],
            const_module.CONF_COMMUNICATION_HUB_ENABLED,
        )
        is True
    )
    assert (
        _schema_default(
            result["data_schema"],
            const_module.CONF_NON_SHUNT_CONNECTION_MODE,
        )
        == "persistent_session"
    )


def test_non_shunt_hub_option_submission_is_saved() -> None:
    """Submitting the Hub checkbox should save it with the connection mode."""
    config_flow_module = _load_config_flow_module()
    const_module = importlib.import_module("custom_components.renogy.const")
    handler = config_flow_module.RenogyOptionsFlowHandler(
        _entry(const_module, device_type=const_module.DeviceType.INVERTER.value)
    )
    user_input = {
        const_module.CONF_NON_SHUNT_CONNECTION_MODE: "persistent_session",
        const_module.CONF_COMMUNICATION_HUB_ENABLED: True,
    }

    result = asyncio.run(handler.async_step_init(user_input))

    assert result == {
        "type": "create_entry",
        "title": "",
        "data": user_input,
    }


def test_shunt_options_do_not_expose_hub_checkbox() -> None:
    """Smart Shunt entries should keep their existing options unchanged."""
    config_flow_module = _load_config_flow_module()
    const_module = importlib.import_module("custom_components.renogy.const")
    handler = config_flow_module.RenogyOptionsFlowHandler(
        _entry(const_module, device_type=const_module.DeviceType.SHUNT300.value)
    )

    result = asyncio.run(handler.async_step_init())
    fields = _schema_fields(result["data_schema"])

    assert const_module.CONF_SHUNT_CONNECTION_MODE in fields
    assert const_module.CONF_COMMUNICATION_HUB_ENABLED not in fields


def test_hub_option_has_user_facing_label() -> None:
    """English strings should label the Hub option as multiple-battery support."""
    repo_root = Path(__file__).resolve().parents[1]
    expected = "Communication Hub / multiple batteries"

    for relative_path in (
        "custom_components/renogy/strings.json",
        "custom_components/renogy/translations/en.json",
    ):
        strings = json.loads((repo_root / relative_path).read_text())
        assert (
            strings["options"]["step"]["init"]["data"]["communication_hub_enabled"]
            == expected
        )
