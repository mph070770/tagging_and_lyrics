import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from .const import DOMAIN, CONF_MEDIA_PLAYER, CONF_HOST, CONF_PORT, CONF_ACCESS_KEY, CONF_ACCESS_SECRET

class TaggingAndLyricsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = "local_push"

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="Tagging and Lyrics", data=user_input)

        data_schema = vol.Schema({
            vol.Required(CONF_MEDIA_PLAYER): cv.string,
            vol.Required(CONF_PORT, default=6056): cv.port,
            vol.Required(CONF_HOST): cv.string,
            vol.Required(CONF_ACCESS_KEY): cv.string,
            vol.Required(CONF_ACCESS_SECRET): cv.string,
        })

        return self.async_show_form(step_id="user", data_schema=data_schema)
