"""EU cloud telemetry with separately enabled, explicit vehicle controls."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, JaecooApi, TokenSet
from .commands import CommandClient
from .const import CONF_COUNTRY_CODE, CONF_TOKENS
from .coordinator import OmodaJaecooCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.LOCK, Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Use saved tokens, never re-submit an account password on startup."""
    try:
        tokens = TokenSet.from_dict(entry.data[CONF_TOKENS])
    except (ApiError, AttributeError, KeyError, TypeError, ValueError) as err:
        raise ConfigEntryAuthFailed("Saved session is invalid; sign in again.") from err

    @callback
    def tokens_updated(new_tokens: TokenSet) -> None:
        # Rotation must survive restart. This is configuration data, not entity state.
        # No reload on token changes: that would create a refresh/reload loop.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TOKENS: new_tokens.to_dict(),
            },
        )

    api = JaecooApi(
        async_get_clientsession(hass),
        country_code=entry.data[CONF_COUNTRY_CODE],
        tokens=tokens,
        on_tokens=tokens_updated,
    )
    command_client = CommandClient(
        api, async_get_clientsession(hass), entry.data[CONF_COUNTRY_CODE]
    )
    coordinator = OmodaJaecooCoordinator(hass, entry, api, command_client)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Only option changes reload; token writes and PIN changes alone do not."""
    coordinator = entry.runtime_data
    if dict(entry.options) != coordinator.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloading entities unsubscribes coordinator timers; HA owns the HTTP session."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
