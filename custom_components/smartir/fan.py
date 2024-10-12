import asyncio
import logging

import voluptuous as vol

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
    PLATFORM_SCHEMA,
    DIRECTION_REVERSE,
    DIRECTION_FORWARD,
)
from homeassistant.components.template.template_entity import TemplateEntity
from homeassistant.const import (
    CONF_NAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)
from . import (
    SmartIR,
    DeviceData,
    DEFAULT_DELAY,
    DEFAULT_POWER_SENSOR_DELAY,
    CONF_UNIQUE_ID,
    CONF_DEVICE_CODE,
    CONF_CONTROLLER_DATA,
    CONF_DELAY,
    CONF_POWER_SENSOR,
    CONF_POWER_TEMPLATE,
    CONF_POWER_SENSOR_DELAY,
    CONF_POWER_RESTORE_STATE,
    CONF_AVAILABILITY_SENSOR,
    CONF_AVAILABILITY_TEMPLATE,
    CONF_AVAILABILITY_WHEN_ON,
)
from .controller import get_controller, get_controller_schema

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Fan"
OSCILLATING = "oscillate"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_DEVICE_CODE): cv.positive_int,
        vol.Required(CONF_CONTROLLER_DATA): get_controller_schema(vol, cv),
        vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.positive_float,
        vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
        vol.Optional(CONF_POWER_TEMPLATE): cv.template,
        vol.Optional(
            CONF_POWER_SENSOR_DELAY, default=DEFAULT_POWER_SENSOR_DELAY
        ): cv.positive_int,
        vol.Optional(CONF_POWER_RESTORE_STATE, default=True): cv.boolean,
        vol.Optional(CONF_AVAILABILITY_SENSOR): cv.entity_id,
        vol.Optional(CONF_AVAILABILITY_TEMPLATE): cv.template,
        vol.Optional(CONF_AVAILABILITY_WHEN_ON, default=False): cv.boolean,
    }
)


async def async_setup_platform(
    hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up the IR Fan platform."""
    _LOGGER.debug("Setting up the SmartIR fan platform")
    if not (
        device_data := await DeviceData.load_file(
            config.get(CONF_DEVICE_CODE),
            "fan",
            {},
            hass,
        )
    ):
        _LOGGER.error("SmartIR fan device data init failed!")
        return

    async_add_entities([SmartIRFan(hass, config, device_data)])


class SmartIRFan(SmartIR, FanEntity, TemplateEntity, RestoreEntity):
    _attr_should_poll = False
    _attr_assumed_state = True
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_template = config.get(CONF_POWER_TEMPLATE)
        self._power_sensor_delay = config.get(CONF_POWER_SENSOR_DELAY)
        self._power_sensor_restore_state = config.get(CONF_POWER_RESTORE_STATE)
        self._availability_sensor = config.get(CONF_AVAILABILITY_SENSOR)
        self._availability_template = config.get(CONF_AVAILABILITY_TEMPLATE)
        self._availability_when_on = config.get(CONF_AVAILABILITY_WHEN_ON)

        self._state = STATE_OFF
        self._availability = True
        self._speed = None
        self._oscillating = None
        self._on_by_remote = False
        self._support_flags = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )
        self._power_sensor_check_expect = None
        self._power_sensor_check_cancel = None

        self._manufacturer = device_data["manufacturer"]
        self._supported_models = device_data["supportedModels"]
        self._supported_controller = device_data["supportedController"]
        self._commands_encoding = device_data["commandsEncoding"]
        self._commands = device_data["commands"]

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

        # Init exclusive lock for sending IR commands
        self._temp_lock = asyncio.Lock()

        # Init the IR/RF controller
        self._controller = get_controller(
            self.hass,
            self._supported_controller,
            self._commands_encoding,
            self._controller_data,
        )

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if last_state is not None:
            self._state = last_state.state

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

            if self._power_sensor:
                self._on_by_remote = last_state.attributes.get("on_by_remote", False)

        if self._power_sensor:
            async_track_state_change_event(
                self.hass, self._power_sensor, self._async_power_sensor_changed
            )
        elif self._power_template:
            self.add_template_attribute(
                "_power_sensor",
                self._power_template,
                None,
                self._async_power_template_changed,
                none_on_template_error=True,
            )

        if self._availability_sensor:
            async_track_state_change_event(
                self.hass,
                self._availability_sensor,
                self._async_availability_sensor_changed,
            )
        elif self._availability_template:
            self.add_template_attribute(
                "_availability",
                self._availability_template,
                None,
                self._async_availability_template_changed,
                none_on_template_error=True,
            )

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the display name of the fan."""
        return self._name

    @property
    def state(self):
        """Return the current state."""
        if not self._availability and (
            self._availability_when_on or self._state == STATE_OFF
        ):
            return STATE_UNAVAILABLE
        else:
            return self._state

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
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

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
        if not self._availability:
            return
        elif percentage == 0:
            state = STATE_OFF
            speed = self._speed
        else:
            state = STATE_ON
            speed = percentage_to_ordered_list_item(self._speed_list, percentage)

        await self._send_command(
            state, speed, self._current_direction, self._oscillating
        )

    async def async_oscillate(self, oscillating: bool) -> None:
        """Set oscillation of the fan."""
        if not self._availability:
            return
        elif not self._support_flags & FanEntityFeature.OSCILLATE:
            return

        await self._send_command(
            self._state, self._speed, self._current_direction, oscillating
        )

    async def async_set_direction(self, direction: str):
        """Set the direction of the fan"""
        if not self._availability:
            return
        elif not self._support_flags & FanEntityFeature.DIRECTION:
            return

        await self._send_command(self._state, self._speed, direction, self._oscillating)

    async def async_turn_on(
        self, percentage: int = None, preset_mode: str = None, **kwargs
    ):
        """Turn on the fan."""
        if not self._availability:
            return
        elif percentage is None:
            percentage = ordered_list_item_to_percentage(self._speed_list, self._speed)

        await self.async_set_percentage(percentage)

    async def async_turn_off(self):
        """Turn off the fan."""
        if not self._availability:
            return
        await self.async_set_percentage(0)

    async def _send_command(self, state, speed, direction, oscillate):
        async with self._temp_lock:

            if self._power_sensor and self._state != state:
                self._async_power_sensor_check_schedule(state)

            try:
                if state == STATE_OFF:
                    if "off" in self._commands:
                        await self._controller.send(self._commands["off"])
                        await asyncio.sleep(self._delay)
                    else:
                        _LOGGER.error("Missing device IR code for 'off' mode.")
                        return
                else:
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
                _LOGGER.exception(
                    "Exception raised in the in the _send_command '%s'", e
                )
