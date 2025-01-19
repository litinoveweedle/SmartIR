import asyncio
import logging

import voluptuous as vol  # type: ignore

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaType,
)
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from .smartir_entity import load_device_data_file, SmartIR, PLATFORM_SCHEMA

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Media Player"
DEFAULT_DEVICE_CLASS = "tv"

CONF_SOURCE_NAMES = "source_names"
CONF_DEVICE_CLASS = "device_class"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_SOURCE_NAMES): dict,
        vol.Optional(CONF_DEVICE_CLASS, default=DEFAULT_DEVICE_CLASS): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up the IR Media Player platform."""
    _LOGGER.debug("Setting up the SmartIR media player platform")
    if not (
        device_data := await load_device_data_file(
            config,
            "media_player",
            {},
            hass,
        )
    ):
        _LOGGER.error("SmartIR media player device data init failed!")
        return

    async_add_entities([SmartIRMediaPlayer(hass, config, device_data)])


class SmartIRMediaPlayer(MediaPlayerEntity, RestoreEntity):

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_data):
        # Initialize SmartIR device
        SmartIR.__init__(self, hass, config, device_data)

        self._device_class = config.get(CONF_DEVICE_CLASS)

        self._sources_list = []
        self._source = None
        self._support_flags = 0

        # Supported features
        if "off" in self._commands and self._commands["off"] is not None:
            self._support_flags = (
                self._support_flags | MediaPlayerEntityFeature.TURN_OFF
            )

        if "on" in self._commands and self._commands["on"] is not None:
            self._support_flags = self._support_flags | MediaPlayerEntityFeature.TURN_ON

        if (
            "previousChannel" in self._commands
            and self._commands["previousChannel"] is not None
        ):
            self._support_flags = (
                self._support_flags | MediaPlayerEntityFeature.PREVIOUS_TRACK
            )

        if (
            "nextChannel" in self._commands
            and self._commands["nextChannel"] is not None
        ):
            self._support_flags = (
                self._support_flags | MediaPlayerEntityFeature.NEXT_TRACK
            )

        if (
            "volumeDown" in self._commands and self._commands["volumeDown"] is not None
        ) or ("volumeUp" in self._commands and self._commands["volumeUp"] is not None):
            self._support_flags = (
                self._support_flags | MediaPlayerEntityFeature.VOLUME_STEP
            )

        if "mute" in self._commands and self._commands["mute"] is not None:
            self._support_flags = (
                self._support_flags | MediaPlayerEntityFeature.VOLUME_MUTE
            )

        if "sources" in self._commands and self._commands["sources"] is not None:
            self._support_flags = (
                self._support_flags
                | MediaPlayerEntityFeature.SELECT_SOURCE
                | MediaPlayerEntityFeature.PLAY_MEDIA
            )

            for source, new_name in config.get(CONF_SOURCE_NAMES, {}).items():
                if source in self._commands["sources"]:
                    if new_name is not None:
                        self._commands["sources"][new_name] = self._commands["sources"][
                            source
                        ]

                    del self._commands["sources"][source]

            # Sources list
            for key in self._commands["sources"]:
                self._sources_list.append(key)

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        # if last_state is not None:
        # TODO add attributes restore

    @property
    def device_class(self):
        """Return the device_class of the media player."""
        return self._device_class

    @property
    def media_title(self):
        """Return the title of current playing media."""
        return None

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return MediaType.CHANNEL

    @property
    def source_list(self):
        return self._sources_list

    @property
    def source(self):
        if self._on_by_remote and not self._power_sensor_restore_state:
            return None
        else:
            return self._source

    @property
    def extra_state_attributes(self):
        """Platform specific attributes."""
        return {
            "on_by_remote": self._on_by_remote,
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
        }

    async def async_turn_off(self):
        """Turn the media player off."""
        await self.send_command(STATE_OFF, [])

    async def async_turn_on(self):
        """Turn the media player off."""
        await self.send_command(STATE_ON, [])

    async def async_media_previous_track(self):
        """Send previous track command."""
        await self.send_command(self._state, [["previousChannel"]])

    async def async_media_next_track(self):
        """Send next track command."""
        await self.send_command(self._state, [["nextChannel"]])

    async def async_volume_down(self):
        """Turn volume down for media player."""
        await self.send_command(self._state, [["volumeDown"]])

    async def async_volume_up(self):
        """Turn volume up for media player."""
        await self.send_command(self._state, [["volumeUp"]])

    async def async_mute_volume(self, mute):
        """Mute the volume."""
        await self.send_command(self._state, [["mute"]])

    async def async_select_source(self, source):
        """Select channel from source."""
        self._source = source
        await self.send_command(self._state, [["sources", source]])

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Support channel change through play_media service."""
        if media_type != MediaType.CHANNEL:
            _LOGGER.error("invalid media type")
            return
        if not media_id.isdigit():
            _LOGGER.error("media_id must be a channel number")
            return

        self._source = "Channel {}".format(media_id)
        commands = []
        for digit in media_id:
            commands.append(["sources", "Channel {}".format(digit)])
        await self.send_command(STATE_ON, commands)

    async def send_command(self, state, commands):
        async with self._temp_lock:

            if self._power_sensor and self._state != state:
                self._async_power_sensor_check_schedule(state)

            try:
                if state == STATE_OFF:
                    if "off" in self._commands.keys() and isinstance(
                        self._commands["off"], str
                    ):
                        if (
                            "on" in self._commands.keys()
                            and isinstance(self._commands["on"], str)
                            and self._commands["on"] == self._commands["off"]
                            and self._state == STATE_OFF
                        ):
                            # prevent to resend 'off' command if same as 'on' and device is already off
                            _LOGGER.debug(
                                "As 'on' and 'off' commands are identical and device is already in requested '%s' state, skipping sending '%s' command",
                                self._state,
                                "off",
                            )
                        else:
                            _LOGGER.debug("Found 'off' operation mode command.")
                            await self._controller.send(self._commands["off"])
                            await asyncio.sleep(self._delay)
                    else:
                        _LOGGER.error("Missing device IR code for 'off' mode.")
                        return
                else:
                    if "on" in self._commands.keys() and isinstance(
                        self._commands["on"], str
                    ):
                        if (
                            "off" in self._commands.keys()
                            and isinstance(self._commands["off"], str)
                            and self._commands["off"] == self._commands["on"]
                            and self._state == STATE_ON
                        ):
                            # prevent to resend 'on' command if same as 'off' and device is already on
                            _LOGGER.debug(
                                "As 'on' and 'off' commands are identical and device is already in requested '%s' state, skipping sending '%s' command",
                                self._state,
                                "on",
                            )
                        else:
                            # if on code is not present, the on bit can be still set later in the all operation codes
                            _LOGGER.debug("Found 'on' operation mode command.")
                            await self._controller.send(self._commands["on"])
                            await asyncio.sleep(self._delay)

                    for keys in commands:
                        data = self._commands
                        for idx in range(len(keys)):
                            if not (isinstance(data, dict) and keys[idx] in data):
                                _LOGGER.error(
                                    "Missing device IR code for '%s' command.",
                                    keys[idx],
                                )
                                return
                            elif idx + 1 == len(keys):
                                if not isinstance(data[keys[idx]], str):
                                    _LOGGER.error(
                                        "Missing device IR code for '%s' command.",
                                        keys[idx],
                                    )
                                    return
                                else:
                                    await self._controller.send(data[keys[idx]])
                                    await asyncio.sleep(self._delay)
                            elif isinstance(data[keys[idx]], dict):
                                data = data[keys[idx]]
                            else:
                                _LOGGER.error(
                                    "Missing device IR code for '%s' command.",
                                    keys[idx],
                                )
                                return

                self._state = state
                self._on_by_remote = False
                self.async_write_ha_state()

            except Exception as e:
                _LOGGER.exception("Exception raised in the in the send_command '%s'", e)
