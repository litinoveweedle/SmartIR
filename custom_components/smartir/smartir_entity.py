import asyncio
import logging
import os.path

import voluptuous as vol  # type: ignore
from homeassistant.core import HomeAssistant, Event, EventStateChangedData, callback
from homeassistant.components.climate import PLATFORM_SCHEMA
from homeassistant.const import CONF_NAME, STATE_ON, STATE_OFF
from homeassistant.helpers.event import async_track_state_change_event, async_call_later
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .device_data import DeviceData
from .controller import get_controller, get_controller_schema

_LOGGER = logging.getLogger(__name__)

DEFAULT_DELAY = 0.5
DEFAULT_OPTIMISTIC_STATE = True
DEFAULT_POWER_SENSOR_DELAY = 10

CONF_UNIQUE_ID = "unique_id"
CONF_DEVICE_CODE = "device_code"
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_OPTIMISTIC_STATE = "optimistic"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_SENSOR_DELAY = "power_sensor_delay"
CONF_POWER_SENSOR_RESTORE_STATE = "power_sensor_restore_state"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_DEVICE_CODE): cv.positive_int,
        vol.Required(CONF_CONTROLLER_DATA): get_controller_schema(vol, cv),
        vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.positive_float,
        vol.Optional(
            CONF_OPTIMISTIC_STATE, default=DEFAULT_OPTIMISTIC_STATE
        ): cv.boolean,
        vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
        vol.Optional(
            CONF_POWER_SENSOR_DELAY, default=DEFAULT_POWER_SENSOR_DELAY
        ): cv.positive_int,
        vol.Optional(CONF_POWER_SENSOR_RESTORE_STATE, default=True): cv.boolean,
    }
)


@staticmethod
async def load_device_data_file(config, device_class, check_data, hass):
    device_code = config.get(CONF_DEVICE_CODE)

    """Load device JSON file."""
    device_json_file_name = str(device_code) + ".json"

    device_files_subdir = os.path.join("custom_codes", device_class)
    device_files_absdir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), device_files_subdir
    )
    if os.path.isdir(device_files_absdir):
        device_json_file_path = os.path.join(device_files_absdir, device_json_file_name)
        if os.path.exists(device_json_file_path):
            _LOGGER.debug(
                "Loading custom %s device JSON file '%s'.",
                device_class,
                device_json_file_name,
            )
            device_data = await hass.async_add_executor_job(
                DeviceData.read_file_as_json, device_json_file_path
            )
            if await DeviceData.check_file(
                device_json_file_name,
                device_data,
                device_class,
                check_data,
            ):
                return device_data
            else:
                return None
    else:
        os.makedirs(device_files_absdir)

    device_files_subdir = os.path.join("codes", device_class)
    device_files_absdir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), device_files_subdir
    )
    if os.path.isdir(device_files_absdir):
        device_json_file_path = os.path.join(device_files_absdir, device_json_file_name)
        if os.path.exists(device_json_file_path):
            _LOGGER.debug(
                "Loading %s device JSON file '%s'.",
                device_class,
                device_json_file_name,
            )
            device_data = await hass.async_add_executor_job(
                DeviceData.read_file_as_json, device_json_file_path
            )
            if await DeviceData.check_file(
                device_json_file_name,
                device_data,
                device_class,
                check_data,
            ):
                return device_data
            else:
                return None
        else:
            _LOGGER.error(
                "Device JSON file '%s' doesn't exists!", device_json_file_name
            )
    else:
        _LOGGER.error(
            "Devices JSON files directory '%s' doesn't exists!", device_files_absdir
        )

    return None


class SmartIR:
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_data):
        _LOGGER.debug(
            "SmartIR init started for device %s supported models %s",
            config.get(CONF_NAME),
            device_data["supportedModels"],
        )
        self.hass = hass
        self._support_flags = 0
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._optimistic_state = config.get(CONF_OPTIMISTIC_STATE)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_sensor_delay = config.get(CONF_POWER_SENSOR_DELAY)
        self._power_sensor_restore_state = config.get(CONF_POWER_SENSOR_RESTORE_STATE)

        self._state = STATE_OFF
        self._on_by_remote = False
        self._power_sensor_check_expect = None
        self._power_sensor_check_cancel = None
        self._update_entity = None

        self._manufacturer = device_data["manufacturer"]
        self._supported_models = device_data["supportedModels"]
        self._supported_controller = device_data["supportedController"]
        self._commands_encoding = device_data["commandsEncoding"]
        self._commands = device_data["commands"]

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
        last_state = await self.async_get_last_state()

        if last_state is not None:
            if last_state.state == STATE_OFF:
                self._state = STATE_OFF
            else:
                self._state = STATE_ON

            if self._power_sensor:
                self._on_by_remote = last_state.attributes.get("on_by_remote", False)

        if self._power_sensor:
            async_track_state_change_event(
                self.hass, self._power_sensor, self._async_power_sensor_changed
            )

    async def _async_power_sensor_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle power sensor changes."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if new_state is None:
            return

        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON and self._state != STATE_ON:
            self._state = STATE_ON
            self._on_by_remote = True
            if self._update_entity is not None:
                await self._update_entity()
        elif new_state.state == STATE_OFF:
            self._on_by_remote = False
            if self._state != STATE_OFF:
                self._state = STATE_OFF
                if self._update_entity is not None:
                    await self._update_entity()
        self.async_write_ha_state()

    @callback
    def _async_power_sensor_check_schedule(self, state):
        if self._power_sensor_check_cancel:
            self._power_sensor_check_cancel()
            self._power_sensor_check_cancel = None
            self._power_sensor_check_expect = None

        @callback
        def _async_power_sensor_check(*_):
            self._power_sensor_check_cancel = None
            expected_state = self._power_sensor_check_expect
            self._power_sensor_check_expect = None
            current_state = getattr(
                self.hass.states.get(self._power_sensor), "state", None
            )
            _LOGGER.debug(
                "Executing power sensor check for expected state '%s', current state '%s'.",
                expected_state,
                current_state,
            )

            if (
                expected_state in [STATE_ON, STATE_OFF]
                and current_state in [STATE_ON, STATE_OFF]
                and expected_state != current_state
            ):
                self._state = current_state
                _LOGGER.debug(
                    "Power sensor check failed, reverted device state to '%s'.",
                    self._state,
                )
                self.async_write_ha_state()

        self._power_sensor_check_expect = state
        self._power_sensor_check_cancel = async_call_later(
            self.hass, self._power_sensor_delay, _async_power_sensor_check
        )
        _LOGGER.debug("Scheduled power sensor check for '%s' state", state)

    async def _async_power_on(self):
        if "on" in self._commands.keys() and isinstance(self._commands["on"], str):
            if (
                self._power_sensor is not None or self._optimistic_state
            ) and self._state == STATE_ON:
                # prevent to send 'on' command if assumed state is on and we use power sensors or assumed state is optimistic
                _LOGGER.debug(
                    "As state is based on power sensors or optimistic state is set to 'True' and current assumed state is '%s', skipping sending '%s' command",
                    self._state,
                    "on",
                )
            elif (
                "off" in self._commands.keys()
                and isinstance(self._commands["off"], str)
                and self._commands["off"] == self._commands["on"]
                and self._state == STATE_ON
                and self._optimistic_state
            ):
                # prevent to resend 'on' command if same as 'off' and device is already on
                _LOGGER.debug(
                    "As 'on' and 'off' commands are identical and device is already in requested '%s' state, skipping sending '%s' command",
                    self._state,
                    "on",
                )
            else:
                _LOGGER.debug("Sending 'on' command.")
                await self._controller.send(self._commands["on"])
                await asyncio.sleep(self._delay)
        else:
            # if on code is not present, the on bit can be still set later in the all functional codes
            _LOGGER.debug(
                "Missing device IR code for 'on' command, this may not be and issue as an 'on' bit could be set in functional codes."
            )

    async def _async_power_off(self):
        if "off" in self._commands.keys() and isinstance(self._commands["off"], str):
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
                _LOGGER.debug("Sending 'off' command.")
                await self._controller.send(self._commands["off"])
                await asyncio.sleep(self._delay)
        else:
            _LOGGER.error("Missing device IR code for 'off' command.")

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the climate device."""
        return self._name

    @property
    def state(self):
        """Return the current state."""
        return self._state

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return self._support_flags
