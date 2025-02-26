import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from .tagging import async_setup_tagging_service
from .lyrics import async_setup_lyrics_service
from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_ACCESS_KEY,
    CONF_ACCESS_SECRET,
    CONF_PORT,
    CONF_MEDIA_PLAYER,
    CONF_LYRICS_ENABLE
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema({
            vol.Required(CONF_HOST): cv.string,
            vol.Required(CONF_ACCESS_KEY): cv.string,
            vol.Required(CONF_ACCESS_SECRET): cv.string,
            vol.Optional(CONF_PORT, default=6056): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(CONF_MEDIA_PLAYER): cv.entity_id,
            vol.Optional(CONF_LYRICS_ENABLE, default=True): cv.boolean,
        })
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the component."""
    # Store the config in hass.data
    conf = config.get(DOMAIN) #Changed
    if not conf:
        return True
    
    _LOGGER.info("Configuration: %s", conf)
    hass.data[DOMAIN] = conf

    # This is now done in async_setup_entry
    #await async_setup_tagging_service(hass)
    #await async_setup_lyrics_service(hass)

    return True

async def async_setup_entry(hass, entry):
    _LOGGER.info("async setup entry: %s", entry)
    """Set up platform from a ConfigEntry."""
    # Create a new entry object, this way we can add/remove options from the entry later on
    await async_setup_tagging_service(hass)
    await async_setup_lyrics_service(hass)
    return True
