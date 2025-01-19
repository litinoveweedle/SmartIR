import asyncio
import logging

import voluptuous as vol  # type: ignore

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
    DIRECTION_REVERSE,
    DIRECTION_FORWARD,
)
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)
from .smartir_entity import load_device_data_file, SmartIR, PLATFORM_SCHEMA

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Fan"

OSCILLATING = "oscillate"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up the IR Fan platform."""
    _LOGGER.debug("Setting up the SmartIR fan platform")
    if not (
        device_data := await load_device_data_file(
            config,
            "fan",
            {},
            hass,
        )
    ):
        _LOGGER.error("SmartIR fan device data init failed!")
        return

    async_add_entities([SmartIRFan(hass, config, device_data)])


class SmartIRFan(FanEntity, RestoreEntity):
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_data):
        # Initialize SmartIR device
        SmartIR.__init__(self, hass, config, device_data)

        self._speed = None
        self._oscillating = None
        self._on_by_remote = False
        self._support_flags = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )

        # fan speeds
        self._speed_list = device_data["speed"]
        if not self._speed_list:
            _LOGGER.error("Speed shall have at least one valid speed defined!")
            return
        self._speed = self._speed_list[0]

        # fan direction
        if DIRECTION_REVERSE in self._commands and DIRECTION_FORWARD in self._commands:
            self._current_direction = DIRECTION_FORWARD
            self._support_flags = self._support_flags | FanEntityFeature.DIRECTION
        else:
            self._current_direction = "default"

        # fan oscillation
        if OSCILLATING in self._commands:
            self._oscillating = False
            self._support_flags = self._support_flags | FanEntityFeature.OSCILLATE

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if last_state is not None:
            if (
                self._support_flags & FanEntityFeature.SET_SPEED
                and last_state.attributes.get("speed") in self._speed_list
            ):
                self._speed = last_state.attributes.get("speed")

            if self._support_flags & FanEntityFeature.DIRECTION:
                self._current_direction = last_state.attributes.get(
                    "current_direction", DIRECTION_FORWARD
                )

            if self._support_flags & FanEntityFeature.OSCILLATE:
                self._oscillating = last_state.attributes.get("oscillating", False)

    @property
    def percentage(self):
        """Return speed percentage of the fan."""
        if self._on_by_remote and not self._power_sensor_restore_state:
            return None
        elif self._state == STATE_OFF:
            return 0
        else:
            return ordered_list_item_to_percentage(self._speed_list, self._speed)

    @property
    def speed_count(self):
        """Return the number of speeds the fan supports."""
        return len(self._speed_list)

    @property
    def oscillating(self):
        """Return the oscillation state."""
        if self._on_by_remote and not self._power_sensor_restore_state:
            return None
        else:
            return self._oscillating

    @property
    def current_direction(self):
        """Return the direction state."""
        if self._on_by_remote and not self._power_sensor_restore_state:
            return None
        else:
            return self._current_direction

    @property
    def extra_state_attributes(self):
        """Platform specific attributes."""
        return {
            "speed": self._speed,
            "on_by_remote": self._on_by_remote,
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
        }

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the desired speed for the fan."""
        if percentage == 0:
            state = STATE_OFF
            speed = self._speed
        else:
            state = STATE_ON
            speed = percentage_to_ordered_list_item(self._speed_list, percentage)

        await self.send_command(
            state, speed, self._current_direction, self._oscillating
        )

    async def async_oscillate(self, oscillating: bool) -> None:
        """Set oscillation of the fan."""
        if not self._support_flags & FanEntityFeature.OSCILLATE:
            return

        await self.send_command(
            self._state, self._speed, self._current_direction, oscillating
        )

    async def async_set_direction(self, direction: str):
        """Set the direction of the fan"""
        if not self._support_flags & FanEntityFeature.DIRECTION:
            return

        await self.send_command(self._state, self._speed, direction, self._oscillating)

    async def async_turn_on(
        self, percentage: int = None, preset_mode: str = None, **kwargs
    ):
        """Turn on the fan."""
        if percentage is None:
            percentage = ordered_list_item_to_percentage(self._speed_list, self._speed)

        await self.async_set_percentage(percentage)

    async def async_turn_off(self):
        """Turn off the fan."""
        await self.async_set_percentage(0)

    async def send_command(self, state, speed, direction, oscillate):
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

                    if oscillate:
                        if "oscillate" in self._commands:
                            await self._controller.send(self._commands["oscillate"])
                            await asyncio.sleep(self._delay)
                        else:
                            _LOGGER.error(
                                "Missing device IR code for 'oscillate' mode."
                            )
                            return
                    else:
                        if (
                            direction in self._commands
                            and isinstance(self._commands[direction], dict)
                            and speed in self._commands[direction]
                        ):
                            await self._controller.send(
                                self._commands[direction][speed]
                            )
                            await asyncio.sleep(self._delay)
                        else:
                            _LOGGER.error(
                                "Missing device IR code for direction '%s' speed '%s'.",
                                direction,
                                speed,
                            )
                            return

                self._state = state
                self._speed = speed
                self._on_by_remote = False
                self._current_direction = direction
                self._oscillating = oscillate
                self.async_write_ha_state()

            except Exception as e:
                _LOGGER.exception("Exception raised in the in the send_command '%s'", e)
