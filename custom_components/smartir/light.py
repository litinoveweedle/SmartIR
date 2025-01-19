import asyncio
import logging

import voluptuous as vol  # type: ignore

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from .smartir_helpers import closest_match_index, closest_match_value
from .smartir_entity import load_device_data_file, SmartIR, PLATFORM_SCHEMA

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Light"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string}
)


async def async_setup_platform(
    hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up the IR Light platform."""
    _LOGGER.debug("Setting up the SmartIR light platform")
    if not (
        device_data := await load_device_data_file(
            config,
            "light",
            {},
            hass,
        )
    ):
        _LOGGER.error("SmartIR light device data init failed!")
        return

    async_add_entities([SmartIRLight(hass, config, device_data)])


class SmartIRLight(SmartIR, LightEntity, RestoreEntity):

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_data):
        # Initialize SmartIR device
        SmartIR.__init__(self, hass, config, device_data)

        self._brightness = None
        self._color_temp = None

        self._brightness_list = device_data.get("brightness")
        self._color_temp_list = device_data.get("colorTemperatures")

        if self._color_temp_list is not None:
            # The light can be dimmed and its color temperature is present in the state.
            self._attr_supported_color_modes = [ColorMode.COLOR_TEMP]
            self._brightness = self._brightness_list[-1]
            self._color_temp = self._color_temp_list[-1]
        elif self._brightness_list is not None:
            # The light can be dimmed. This mode must be the only supported mode if supported by the light.
            self._attr_supported_color_modes = [ColorMode.BRIGHTNESS]
            self._brightness = self._brightness_list[-1]
        else:
            # The light can be turned on or off. This mode must be the only supported mode if supported by the light.
            self._attr_supported_color_modes = [ColorMode.ONOFF]

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            if (
                ATTR_BRIGHTNESS in last_state.attributes
                and self._brightness_list is not None
                and last_state.attributes[ATTR_BRIGHTNESS] in self._brightness_list
            ):
                self._brightness = last_state.attributes[ATTR_BRIGHTNESS]
            if (
                ATTR_COLOR_TEMP_KELVIN in last_state.attributes
                and self._color_temp_list is not None
                and last_state.attributes[ATTR_COLOR_TEMP_KELVIN]
                in self._color_temp_list
            ):
                self._color_temp = last_state.attributes[ATTR_COLOR_TEMP_KELVIN]

    @property
    def color_mode(self):
        # We only support a single color mode currently, so no need to track it
        return self._attr_supported_color_modes[0]

    @property
    def color_temp_kelvin(self):
        return self._color_temp

    @property
    def min_color_temp_kelvin(self):
        if self._color_temp_list:
            return self._color_temp_list[0]
        else:
            return None

    @property
    def max_color_temp_kelvin(self):
        if self._color_temp_list:
            return self._color_temp_list[-1]
        else:
            return None

    @property
    def is_on(self):
        if self._state == STATE_ON:
            return True
        else:
            return False

    @property
    def brightness(self):
        return self._brightness

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

    async def async_turn_on(self, **kwargs):
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        color_temp = kwargs.get(ATTR_COLOR_TEMP_KELVIN, self._color_temp)

        if self._brightness_list is not None and brightness is None:
            _LOGGER.debug(
                "No power on brightness argument found, setting last brightness '%s'",
                self._brightness,
            )
            brightness = self._brightness

        if self._color_temp_list is not None and color_temp is None:
            _LOGGER.debug(
                "No power on color temperature argument found, setting last color temperature '%s'",
                self._color_temp,
            )
            color_temp = self._color_temp

        await self.send_command(STATE_ON, brightness, color_temp)

    async def async_turn_off(self):
        await self.send_command(STATE_OFF, self._brightness, self._color_temp)

    async def async_toggle(self):
        await (self.async_turn_on() if not self.is_on else self.async_turn_off())

    async def send_command(self, state, brightness, color_temp):
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
                            # if on code is not present, the on bit can be still set later in the all operation/fan codes"""
                            _LOGGER.debug("Found 'on' operation mode command.")
                            await self._controller.send(self._commands["on"])
                            await asyncio.sleep(self._delay)

                    if color_temp is not None:
                        if "colorTemperature" in self._commands and isinstance(
                            self._commands["colorTemperature"], dict
                        ):
                            color_temp = closest_match_value(
                                color_temp, self._color_temp_list
                            )
                            _LOGGER.debug(
                                "Changing color temp from '%s'K to '%s'K using found remote command for {final_color_temp}K",
                                color_temp,
                            )
                            await self._controller.send(
                                self._commands["colorTemperature"][str(color_temp)]
                            )
                            await asyncio.sleep(self._delay)
                        else:
                            old_color_temp_index = closest_match_index(
                                self._color_temp, self._color_temp_list
                            )
                            new_color_temp_index = closest_match_index(
                                color_temp, self._color_temp_list
                            )
                            color_temp = self._color_temp_list[new_color_temp_index]
                            steps = new_color_temp_index - old_color_temp_index
                            if steps < 0:
                                cmd = "warmer"
                                steps = abs(steps)
                            else:
                                cmd = "colder"

                            if (
                                new_color_temp_index == len(self._color_temp_list) - 1
                                or new_color_temp_index == 0
                            ):
                                # If we are heading for the highest or lowest value,
                                # take the opportunity to resync by issuing enough
                                # commands to go the full range.
                                steps = len(self._color_temp_list)

                            _LOGGER.debug(
                                "Changing color temp from '%s'K index {old_color_temp} to {target}K index {new_color_temp}"
                            )
                            while steps > 0:
                                steps -= 1
                                await self._controller.send(self._commands[cmd])
                                await asyncio.sleep(self._delay)

                    if brightness is not None:
                        # before checking the supported brightnesses, make a special case
                        # when a nightlight is fitted for brightness of 1
                        if brightness == 1 and "night" in self._commands:
                            await self._controller.send(self._commands["night"])
                            await asyncio.sleep(self._delay)
                        elif "brightness" in self._commands and isinstance(
                            self._commands["brightness"], dict
                        ):
                            brightness = closest_match_value(
                                brightness, self._brightness_list
                            )
                            _LOGGER.debug(
                                "Changing color temp from '%s'K to '%s'K using found remote command for {final_color_temp}K",
                                color_temp,
                            )
                            await self._controller.send(
                                self._commands["brightness"][str(brightness)]
                            )
                            await asyncio.sleep(self._delay)
                        else:
                            old_brightness_index = closest_match_index(
                                self._brightness, self._brightness_list
                            )
                            new_brightness_index = closest_match_index(
                                brightness, self._brightness_list
                            )
                            brightness = self._brightness_list[new_brightness_index]
                            steps = new_brightness_index - old_brightness_index
                            if steps < 0:
                                cmd = "dim"
                                steps = abs(steps)
                            else:
                                cmd = "brighten"

                            if (
                                new_brightness_index == len(self._brightness_list) - 1
                                or new_brightness_index == 0
                            ):
                                # If we are heading for the highest or lowest value,
                                # take the opportunity to resync by issuing enough
                                # commands to go the full range.
                                steps = len(self._brightness_list)

                            _LOGGER.debug(
                                "Changing color temp from '%s'K index {old_color_temp} to {target}K index {new_color_temp}"
                            )
                            while steps > 0:
                                steps -= 1
                                await self._controller.send(self._commands[cmd])
                                await asyncio.sleep(self._delay)

                self._on_by_remote = False
                self._state = state
                self._brightness = brightness
                self._color_temp = color_temp
                self.async_write_ha_state()

            except Exception as e:
                _LOGGER.exception("Exception raised in the in the send_command '%s'", e)
