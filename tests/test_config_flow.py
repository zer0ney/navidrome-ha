"""HA 2026.11 form results, credential errors, and stable-entry reconfiguration."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.navidrome.api import (
    CannotConnect,
    InvalidAuth,
    NotAdmin,
    NotSupported,
    ProtocolError,
)
from custom_components.navidrome.config_flow import NavidromeConfigFlow

DATA = {"url": "https://music.example/music/", "username": "admin", "password": "secret"}


@pytest.fixture
def flow(hass):
    instance = NavidromeConfigFlow()
    instance.hass = hass
    instance.flow_id = "test"
    instance.context = {"source": "user"}
    instance._async_current_entries = Mock(return_value=[])
    return instance


async def test_user_success_and_secret_schema(flow):
    with patch("custom_components.navidrome.config_flow.NavidromeClient.validate", new=AsyncMock()):
        result = await flow.async_step_user(DATA)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["url"] == "https://music.example/music"
    assert "secret" not in str(await flow.async_step_user())


@pytest.mark.parametrize(
    ("exception", "key"),
    [
        (InvalidAuth, "invalid_auth"),
        (NotAdmin, "not_admin"),
        (NotSupported, "not_supported"),
        (CannotConnect, "cannot_connect"),
        (ProtocolError, "invalid_response"),
    ],
)
async def test_errors(flow, exception, key):
    with patch(
        "custom_components.navidrome.config_flow.NavidromeClient.validate",
        new=AsyncMock(side_effect=exception("sanitized")),
    ):
        result = await flow.async_step_user(DATA)
    assert result["errors"] == {"base": key}
    assert "secret" not in str(result)


async def test_invalid_url_and_duplicate(flow):
    assert (await flow.async_step_user(DATA | {"url": "ftp://bad"}))["errors"] == {
        "url": "invalid_url"
    }
    flow._async_current_entries.return_value = [Mock(entry_id="other", data=DATA)]
    with patch("custom_components.navidrome.config_flow.NavidromeClient.validate", new=AsyncMock()):
        with pytest.raises(AbortFlow):
            await flow.async_step_user(DATA)


async def test_reconfigure_keeps_entry_and_password(flow):
    existing = Mock(data=DATA, entry_id="stable-entry")
    flow._get_reconfigure_entry = Mock(return_value=existing)
    flow.async_update_reload_and_abort = Mock(return_value={"type": FlowResultType.ABORT})
    with patch("custom_components.navidrome.config_flow.NavidromeClient.validate", new=AsyncMock()):
        await flow.async_step_reconfigure({"url": "https://new.example/music", "password": ""})
    args, kwargs = flow.async_update_reload_and_abort.call_args
    assert args == (existing,)
    assert kwargs["data_updates"]["password"] == "secret"
    assert kwargs["data_updates"]["url"] == "https://new.example/music"


async def test_reauth(flow):
    existing = Mock(data=DATA)
    flow._get_reauth_entry = Mock(return_value=existing)
    flow.async_update_reload_and_abort = Mock(return_value={"type": FlowResultType.ABORT})
    assert (await flow.async_step_reauth(DATA))["step_id"] == "reauth_confirm"
    with patch("custom_components.navidrome.config_flow.NavidromeClient.validate", new=AsyncMock()):
        await flow.async_step_reauth_confirm({"password": "replacement"})
    assert flow.async_update_reload_and_abort.call_args.kwargs["data_updates"]["password"] == (
        "replacement"
    )


def test_packaging_metadata_and_translation_consistency():
    import json
    import tomllib
    from pathlib import Path

    root = Path(__file__).parents[1]
    component = root / "custom_components" / "navidrome"
    manifest = json.loads((component / "manifest.json").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())
    hacs = json.loads((root / "hacs.json").read_text())
    assert manifest["version"] == project["project"]["version"]
    assert manifest["iot_class"] == "local_push"
    assert manifest["config_flow"]
    assert set(manifest["requirements"]) <= set(project["project"]["dependencies"])
    assert f"homeassistant=={hacs['homeassistant']}" in project["dependency-groups"]["dev"]
    assert json.loads((component / "strings.json").read_text()) == json.loads(
        (component / "translations" / "en.json").read_text()
    )
