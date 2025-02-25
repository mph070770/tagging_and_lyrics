import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from .tagging import setup_tagging_service
from .lyrics import setup_lyrics_service
from .const import (
    DOMAIN,
    CONF_MEDIA_PLAYER,
    CONF_ACCESS_KEY,
    CONF_ACCESS_SECRET,
    CONF_PORT,
    CONF_HOST
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_MEDIA_PLAYER): cv.entity_id,
                vol.Required(CONF_PORT, default=6056): cv.port,
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_ACCESS_KEY): cv.string,
                vol.Required(CONF_ACCESS_SECRET): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup_entry(hass: HomeAssistant, config_entry) -> bool:
    """Set up the Tagging and Lyrics integration from a config entry."""
    _LOGGER.info("Setting up the Tagging and Lyrics integration from config entry.")

    hass.data[DOMAIN] = config_entry.data

    # Register the tagging and lyrics services asynchronously
    await async_setup_tagging_service(hass)
    setup_lyrics_service(hass)

    # Ensure logging level is set to debug for troubleshooting
    logging.getLogger("custom_components.tagging_and_lyrics").setLevel(logging.DEBUG)

    # Autostart the fetch_lyrics service
    async def autostart(event):
        _LOGGER.debug("Autostarting fetch_lyrics service.")
        try:
            entity_id = config_entry.data[CONF_MEDIA_PLAYER]  # Use the configured media player
            await hass.services.async_call(
                "tagging_and_lyrics",
                "fetch_lyrics",
                {"entity_id": entity_id}
            )
            _LOGGER.info("Autostarted fetch_lyrics service for entity: %s", entity_id)
        except Exception as e:
            _LOGGER.error("Error in autostarting fetch_lyrics service: %s", e)

    # Listen for Home Assistant start event
    hass.bus.async_listen_once("homeassistant_start", autostart)
    _LOGGER.debug("Registered autostart listener for homeassistant_start.")

    return True
