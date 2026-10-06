"""Native account-level actions. Refresh is a cloud read, never a vehicle wake."""

from __future__ import annotations

from time import monotonic

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import DOMAIN

SERVICE_REFRESH_STATUS = "refresh_status"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
REFRESH_COOLDOWN_SECONDS = 10


@callback
def async_register_actions(hass: HomeAssistant) -> None:
    """Keep the action registered even when its target account is unloaded."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_STATUS):
        return
    in_progress: set[str] = set()
    last_finished: dict[str, float] = {}

    async def refresh_status(call: ServiceCall) -> None:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_refresh_target"
            )
        if entry.state is not ConfigEntryState.LOADED or not getattr(
            entry, "runtime_data", None
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="refresh_target_unloaded"
            )
        if entry_id in in_progress:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="refresh_in_progress"
            )
        if (
            entry_id in last_finished
            and monotonic() - last_finished[entry_id] < REFRESH_COOLDOWN_SECONDS
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="refresh_cooldown"
            )
        coordinator = entry.runtime_data
        in_progress.add(entry_id)
        try:
            # Public coordinator refresh serializes with normal/post-command reads.
            # It never calls a command client, checks a PIN, or requests a login OTP.
            await coordinator.async_refresh()
        finally:
            in_progress.discard(entry_id)
            last_finished[entry_id] = monotonic()
        if (
            entry.state is not ConfigEntryState.LOADED
            or entry.runtime_data is not coordinator
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="refresh_target_changed"
            )
        if not coordinator.last_update_success:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="refresh_failed"
            )

    # Like HA's administrative update actions, trusted automations are allowed;
    # explicitly authenticated callers need admin rights for account-level refresh.
    service.async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_REFRESH_STATUS,
        refresh_status,
        schema=vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string}),
    )
