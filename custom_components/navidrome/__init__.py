"""Native-push Navidrome now playing."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InvalidAuth, NavidromeClient, NavidromeError, NotAdmin, NotSupported
from .coordinator import NavidromeCoordinator

type NavidromeConfigEntry = ConfigEntry[NavidromeCoordinator]

PLATFORMS = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: NavidromeConfigEntry) -> None:
    """Subscribe and snapshot before creating persistent per-user entities."""
    api = NavidromeClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = NavidromeCoordinator(hass, entry, api)
    try:
        await coordinator.async_start()
    except (InvalidAuth, NotAdmin):
        raise ConfigEntryAuthFailed("Valid Navidrome administrator credentials required") from None
    except NotSupported:
        raise ConfigEntryError(
            "Navidrome SSE unavailable; check version, BasePath and proxy"
        ) from None
    except (NavidromeError, TimeoutError):
        raise ConfigEntryNotReady("Cannot retrieve Navidrome current state") from None
    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_stop()
        raise


async def async_unload_entry(hass: HomeAssistant, entry: NavidromeConfigEntry) -> bool:
    """Do not leave HTTP streams, timers or listeners behind on reload."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unloaded
