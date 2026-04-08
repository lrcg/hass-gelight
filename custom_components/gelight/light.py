"""
Async platform to control **SOME** GE light devices.
Supports color temperature and brightness. RGB color light is not tested.
This is based on some code of the tuya light and (python-laurel)[https://github.com/google/python-laurel].
Example configuration:
  - platform: gelight
    username: user_from_ge
    password: pass_from_ge
    lights:
      - id: 1_from_ge
        mac: mac_in_lowercase_from_ge
        name: name_in_hass
        type: typeid_from_ge

Home Assistant 2026.4+ compatible:
- HA-facing color temperature uses Kelvin only
- Device packet generation still uses internal mired conversion
- Explicit supported_color_modes and color_mode
- Added debug logging for troubleshooting
- Added reliable critical-command retry profile
- Serialized mesh operations
- Callback-based end-state confirmation with selective retry
- Per-device operation token to ignore stale callback state
- Active status refresh after each attempt to confirm fresh state
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from time import monotonic

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    PLATFORM_SCHEMA,
)
from homeassistant.const import (
    CONF_ID,
    CONF_LIGHTS,
    CONF_MAC,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TYPE,
    CONF_USERNAME,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import color as colorutil

import dimond

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=240)

CONF_MAX_BRIGHT = "max_brightness"
CONF_MIN_BRIGHT = "min_brightness"
DEFAULT_ID = "1"

CIRCADIAN_BRIGHTNESS = True
try:
    from custom_components.circadian_lighting import DATA_CIRCADIAN_LIGHTING
except Exception:
    CIRCADIAN_BRIGHTNESS = False


LIGHT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ID, default=DEFAULT_ID): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_MAC): cv.string,
        vol.Optional(CONF_TYPE): cv.string,
        vol.Optional(CONF_MIN_BRIGHT, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional(CONF_MAX_BRIGHT, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_ID, default=DEFAULT_ID): cv.string,
        vol.Optional(CONF_LIGHTS, default=[]): vol.All(cv.ensure_list, [LIGHT_SCHEMA]),
    }
)


def callback(mesh, data):
    _LOGGER.debug("callback raw data=%r", data)
    try:
        if len(data) < 11:
            _LOGGER.debug("callback short packet: %r", data)
            return

        if data[7] != 0xDC:
            _LOGGER.debug("callback ignoring packet type=0x%02X data=%r", data[7], data)
            return

        responses = data[10:]
        _LOGGER.debug("callback status packet raw=%r responses=%r", data, responses)

        for i in range(0, len(responses), 4):
            response = responses[i : i + 4]
            if len(response) < 4:
                break

            devid = response[0]
            if devid == 0:
                break

            device = mesh.devices.get(devid)
            if device is None:
                _LOGGER.debug("callback unknown device id=%s response=%r", devid, response)
                continue

            brightness_pct = response[2]
            now = monotonic()

            device.power = brightness_pct > 0

            if brightness_pct >= 128:
                brightness_pct -= 128

                red = int(((response[3] & 0xE0) >> 5) * 255 / 7)
                green = int(((response[3] & 0x1C) >> 2) * 255 / 7)
                blue = int((response[3] & 0x03) * 255 / 3)

                device.red = red
                device.green = green
                device.blue = blue
                device._hs_color = colorutil.color_RGB_to_hs(red, green, blue)
                device._device_last_temp_value = None
                device._attr_color_mode = ColorMode.HS
            else:
                raw_temp = response[3]
                device._device_last_temp_value = raw_temp
                device._color_temp_kelvin = device._packet_value_to_color_temp_kelvin(raw_temp)
                device._attr_color_mode = ColorMode.COLOR_TEMP
                device._hs_color = (0.0, 0.0)

            device._brightness = 255 * brightness_pct // 100

            with device._callback_condition:
                device._last_callback_at = now
                device._last_callback_snapshot = {
                    "power": bool(device.power),
                    "brightness": int(device._brightness),
                    "color_mode": device._attr_color_mode,
                    "hs_color": tuple(device._hs_color) if device._hs_color is not None else None,
                    "color_temp_kelvin": int(device._color_temp_kelvin),
                }
                device._callback_condition.notify_all()

            _LOGGER.debug(
                "%s callback state power=%s mode=%s brightness=%s hs=%s kelvin=%s raw_temp=%s",
                device._name,
                device.power,
                device._attr_color_mode,
                device._brightness,
                device._hs_color,
                device._color_temp_kelvin,
                device._device_last_temp_value,
            )

            device.schedule_update_ha_state()

    except Exception:
        _LOGGER.exception("callback failed data=%r", data)


async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up GE light platform."""
    devices = config.get(CONF_LIGHTS, [])
    lights = []

    mesh = LaurelMesh(
        config.get(CONF_USERNAME),
        config.get(CONF_PASSWORD),
    )

    for device_config in devices:
        lights.append(
            GEDevice(
                hass=hass,
                network=mesh,
                mac=device_config.get(CONF_MAC),
                lightid=device_config.get(CONF_ID),
                name=device_config.get(CONF_NAME),
                type_=device_config.get(CONF_TYPE),
                max_brightness=device_config.get(CONF_MAX_BRIGHT),
                min_brightness=device_config.get(CONF_MIN_BRIGHT),
            )
        )

    async_add_devices(lights)
    mesh.devices = {light.id: light for light in lights}

    _LOGGER.debug("Connecting mesh with %s configured lights", len(lights))
    await hass.async_add_executor_job(mesh.connect)

    async def async_update(now=None):
        _LOGGER.debug("Scheduled mesh status update")
        await hass.async_add_executor_job(mesh.update_status)

    async_track_time_interval(hass, async_update, SCAN_INTERVAL)


class GEDevice(LightEntity):
    """Representation of a GE light."""

    def __init__(
        self,
        hass,
        network,
        mac,
        lightid,
        name,
        type_,
        max_brightness,
        min_brightness,
        icon=None,
    ):
        self.hass = hass
        self.id = int(lightid)
        self.mac = mac
        self.type = int(type_)
        self.network = network

        self.power = False
        self._unique_id = format_mac(mac)
        self._name = name
        self._icon = icon
        self._cl = None

        self._brightness = 0
        self._max_brightness = int(255 * max_brightness / 100.0)
        self._min_brightness = int(255 * min_brightness / 100.0)

        self._hs_color = (0.0, 0.0)
        self.red = 0
        self.green = 0
        self.blue = 0

        self._attr_min_color_temp_kelvin = 2000
        self._attr_max_color_temp_kelvin = 7000
        self._color_temp_kelvin = 2000
        self._device_last_temp_value = None

        self._device_min_mireds = colorutil.color_temperature_kelvin_to_mired(7000)
        self._device_max_mireds = colorutil.color_temperature_kelvin_to_mired(2000)
        self._device_temp_ratio = -100.0 / (
            self._device_max_mireds - self._device_min_mireds
        )

        self._callback_condition = threading.Condition()
        self._last_callback_at = 0.0
        self._last_callback_snapshot = None

        self._op_token = 0
        self._op_started_at = 0.0

        color_modes = set()
        if self.support_rgb():
            color_modes.add(ColorMode.HS)
        if self.support_color_temp():
            color_modes.add(ColorMode.COLOR_TEMP)
        if not color_modes:
            color_modes.add(ColorMode.BRIGHTNESS)

        self._attr_supported_color_modes = color_modes

        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.HS in color_modes:
            self._attr_color_mode = ColorMode.HS
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return self._name

    @property
    def icon(self):
        return self._icon

    @property
    def is_on(self):
        return bool(self.power)

    @property
    def brightness(self):
        return int(self._brightness)

    @property
    def hs_color(self):
        return self._hs_color

    @property
    def color_temp_kelvin(self):
        return int(self._color_temp_kelvin)

    @property
    def assumed_state(self):
        return True

    def support_rgb(self):
        return self.type in {6, 7, 8, 21, 22, 23, 31, 147}

    def support_color_temp(self):
        return self.type in {5, 6, 7, 8, 10, 11, 19, 20, 21, 22, 23, 31, 80, 83, 85, 147}

    def calc_brightness(self):
        if self._cl is None:
            self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is None:
                return self.brightness

        if self._cl.data["percent"] > 0:
            return self._max_brightness

        return int(
            ((self._max_brightness - self._min_brightness)
             * ((100 + self._cl.data["percent"]) / 100))
            + self._min_brightness
        )

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("%s turn_on kwargs=%r", self._name, kwargs)

        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            brightness = int(brightness)
        elif CIRCADIAN_BRIGHTNESS:
            brightness = self.calc_brightness()

        requested_hs = None
        requested_kelvin = None

        if ATTR_HS_COLOR in kwargs and self.support_rgb():
            requested_hs = kwargs[ATTR_HS_COLOR]
            if self.support_color_temp():
                mapped_kelvin = self.hs_to_white_kelvin(requested_hs)
                if mapped_kelvin is not None:
                    requested_kelvin = mapped_kelvin
                    requested_hs = None

        if requested_kelvin is None:
            color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            if color_temp_kelvin is not None and self.support_color_temp():
                requested_kelvin = int(color_temp_kelvin)

        if (
            requested_hs is None
            and requested_kelvin is None
            and CIRCADIAN_BRIGHTNESS
            and self.support_color_temp()
            and ATTR_BRIGHTNESS not in kwargs
        ):
            if self._cl is None:
                self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is not None:
                requested_kelvin = int(self._cl.data["colortemp"])

        steps, optimistic, target = await self.hass.async_add_executor_job(
            self._build_turn_on_plan,
            brightness,
            requested_kelvin,
            requested_hs,
        )

        await self.hass.async_add_executor_job(
            self._run_plan_with_confirmation,
            "turn_on",
            steps,
            target,
        )

        self._apply_optimistic_state(optimistic)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("%s turn_off", self._name)

        steps = [("power", 0xD0, [0])]
        target = {"power": False}

        await self.hass.async_add_executor_job(
            self._run_plan_with_confirmation,
            "turn_off",
            steps,
            target,
        )

        self.power = False
        self.async_write_ha_state()

    def _begin_operation(self):
        with self._callback_condition:
            self._op_token += 1
            token = self._op_token
            self._op_started_at = monotonic()
            return token, self._op_started_at

    def _request_status(self):
        _LOGGER.debug("%s requesting fresh status", self._name)
        self.network.send_packet(self.id, 0xDA, [])

    def _build_retry_steps(self, target):
        """Retry only the minimum needed to reach the desired end state."""
        steps = []

        if target.get("power") is False:
            steps.append(("power", 0xD0, [0]))
            return steps

        if target.get("power") is True and not self.power:
            steps.append(("power", 0xD0, [1]))

        if target.get("hs_color") is not None:
            hue, saturation = target["hs_color"]
            effective_brightness = target.get("brightness")
            if effective_brightness is None:
                effective_brightness = self._brightness
            red, green, blue = colorutil.color_hsv_to_RGB(
                hue,
                saturation,
                max(1, int(effective_brightness)) * 100 / 255,
            )
            steps.append(("hs_color", 0xE2, [0x04, red, green, blue]))
            return steps

        if target.get("color_temp_kelvin") is not None:
            final_kelvin = self._clamp_color_temp_kelvin(target["color_temp_kelvin"])
            value = self._color_temp_packet_value(final_kelvin)
            steps.append(("color_temp", 0xE2, [0x05, value]))

            effective_brightness = target.get("brightness")
            if effective_brightness is None:
                effective_brightness = self._brightness

            if effective_brightness > 0:
                steps.append(
                    (
                        "brightness_after_white",
                        0xD2,
                        [self._device_brightness_value(effective_brightness)],
                    )
                )
            return steps

        if target.get("brightness") is not None:
            steps.append(
                (
                    "brightness",
                    0xD2,
                    [self._device_brightness_value(target["brightness"])],
                )
            )

        return steps

    def _run_plan_with_confirmation(self, label, steps, target):
        token, _started_at = self._begin_operation()

        _LOGGER.debug(
            "%s starting operation token=%s label=%s target=%r",
            self._name,
            token,
            label,
            target,
        )

        # Attempt 1: full sequence
        self.network.run_critical_sequence(self.id, f"{label}:{token}", steps)
        confirmed = self._refresh_and_wait_for_target(
            token=token,
            label=f"{label}:confirm1",
            target=target,
            timeout=1.25,
        )
        if confirmed:
            _LOGGER.debug("%s target confirmed token=%s label=%s", self._name, token, label)
            return

        # Attempt 2: minimal retry sequence
        retry_steps = self._build_retry_steps(target)
        _LOGGER.debug(
            "%s target not confirmed token=%s label=%s; retrying minimal sequence steps=%s",
            self._name,
            token,
            label,
            retry_steps,
        )
        if retry_steps:
            self.network.run_critical_sequence(
                self.id,
                f"{label}:{token}:retry",
                retry_steps,
            )

        confirmed = self._refresh_and_wait_for_target(
            token=token,
            label=f"{label}:confirm2",
            target=target,
            timeout=1.5,
        )
        if confirmed:
            _LOGGER.debug("%s target confirmed after retry token=%s", self._name, token)
        else:
            _LOGGER.warning(
                "%s target still not confirmed after retry token=%s target=%r",
                self._name,
                token,
                target,
            )

    def _refresh_and_wait_for_target(self, token, label, target, timeout):
        """
        Ask the bulb for fresh status, then only accept callbacks that arrive
        after that request.
        """
        with self._callback_condition:
            if self._op_token != token:
                return False
            refresh_started_at = monotonic()

        _LOGGER.debug(
            "%s requesting confirmation token=%s label=%s refresh_started_at=%s",
            self._name,
            token,
            label,
            refresh_started_at,
        )
        self._request_status()

        deadline = monotonic() + timeout
        with self._callback_condition:
            while True:
                if self._op_token != token:
                    _LOGGER.debug(
                        "%s abandoning confirmation for stale token=%s current_token=%s",
                        self._name,
                        token,
                        self._op_token,
                    )
                    return False

                if (
                    self._last_callback_at >= refresh_started_at
                    and self._target_matches_snapshot(target)
                ):
                    return True

                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False

                self._callback_condition.wait(timeout=remaining)

    def _target_matches_snapshot(self, target):
        snapshot = self._last_callback_snapshot
        if snapshot is None:
            return False

        if "power" in target and bool(snapshot["power"]) != bool(target["power"]):
            return False

        if not target.get("power", True):
            return True

        if "brightness" in target and target["brightness"] is not None:
            if abs(int(snapshot["brightness"]) - int(target["brightness"])) > 8:
                return False

        if "color_mode" in target and target["color_mode"] is not None:
            if snapshot["color_mode"] != target["color_mode"]:
                return False

        if "hs_color" in target and target["hs_color"] is not None:
            current_hue, current_sat = snapshot["hs_color"]
            target_hue, target_sat = target["hs_color"]
            hue_diff = abs(current_hue - target_hue)
            hue_diff = min(hue_diff, 360 - hue_diff)
            if hue_diff > 8 or abs(current_sat - target_sat) > 10:
                return False

        if "color_temp_kelvin" in target and target["color_temp_kelvin"] is not None:
            if abs(int(snapshot["color_temp_kelvin"]) - int(target["color_temp_kelvin"])) > 250:
                return False

        return True

    def _apply_optimistic_state(self, optimistic):
        self.power = optimistic["power"]

        if optimistic["brightness"] is not None:
            self._brightness = optimistic["brightness"]

        if optimistic["color_mode"] is not None:
            self._attr_color_mode = optimistic["color_mode"]

        if optimistic["hs_color"] is not None:
            self._hs_color = optimistic["hs_color"]
            red, green, blue = colorutil.color_hsv_to_RGB(
                optimistic["hs_color"][0],
                optimistic["hs_color"][1],
                max(1, self._brightness) * 100 / 255,
            )
            self.red = red
            self.green = green
            self.blue = blue

        if optimistic["color_temp_kelvin"] is not None:
            self._color_temp_kelvin = optimistic["color_temp_kelvin"]

    def _build_turn_on_plan(self, brightness=None, color_temp_kelvin=None, hs_color=None):
        steps = [("power", 0xD0, [1])]

        effective_brightness = self._brightness
        if brightness is not None:
            effective_brightness = int(brightness)
            steps.append(
                ("brightness", 0xD2, [self._device_brightness_value(effective_brightness)])
            )

        optimistic = {
            "power": True,
            "brightness": effective_brightness if brightness is not None else None,
            "color_mode": None,
            "hs_color": None,
            "color_temp_kelvin": None,
        }

        target = {
            "power": True,
            "brightness": effective_brightness if brightness is not None else None,
            "color_mode": None,
            "hs_color": None,
            "color_temp_kelvin": None,
        }

        if hs_color is not None:
            hue, saturation = hs_color
            red, green, blue = colorutil.color_hsv_to_RGB(
                hue,
                saturation,
                max(1, effective_brightness) * 100 / 255,
            )
            steps.append(("hs_color", 0xE2, [0x04, red, green, blue]))
            optimistic["hs_color"] = hs_color
            optimistic["color_mode"] = ColorMode.HS
            target["hs_color"] = hs_color
            target["color_mode"] = ColorMode.HS

        elif color_temp_kelvin is not None:
            final_kelvin = self._clamp_color_temp_kelvin(color_temp_kelvin)
            value = self._color_temp_packet_value(final_kelvin)
            steps.append(("color_temp", 0xE2, [0x05, value]))

            if effective_brightness > 0:
                steps.append(
                    (
                        "brightness_after_white",
                        0xD2,
                        [self._device_brightness_value(effective_brightness)],
                    )
                )

            optimistic["color_temp_kelvin"] = final_kelvin
            optimistic["color_mode"] = ColorMode.COLOR_TEMP
            target["color_temp_kelvin"] = final_kelvin
            target["color_mode"] = ColorMode.COLOR_TEMP

        elif brightness is not None and ColorMode.BRIGHTNESS in self._attr_supported_color_modes:
            optimistic["color_mode"] = ColorMode.BRIGHTNESS
            target["color_mode"] = ColorMode.BRIGHTNESS

        return steps, optimistic, target

    def update(self):
        _LOGGER.debug("%s manual update packet", self._name)
        self.network.send_packet(self.id, 0xDA, [])

    def _clamp_color_temp_kelvin(self, color_temp_kelvin):
        return max(
            self._attr_min_color_temp_kelvin,
            min(self._attr_max_color_temp_kelvin, int(color_temp_kelvin)),
        )

    def _device_brightness_value(self, brightness):
        return 100 * int(brightness) // 255

    def _color_temp_packet_value(self, color_temp_kelvin):
        color_temp_kelvin = self._clamp_color_temp_kelvin(color_temp_kelvin)
        color_temp_mired = colorutil.color_temperature_kelvin_to_mired(color_temp_kelvin)
        value = int(
            self._device_temp_ratio * (color_temp_mired - self._device_max_mireds)
        )
        return max(0, min(100, value))

    def _packet_value_to_color_temp_kelvin(self, value):
        value = max(0, min(100, int(value)))
        color_temp_mired = self._device_max_mireds + (value / self._device_temp_ratio)
        kelvin = colorutil.color_temperature_mired_to_kelvin(color_temp_mired)
        return self._clamp_color_temp_kelvin(kelvin)

    async def async_update(self):
        if self.id == 0:
            await self.hass.async_add_executor_job(self.update)

    def hs_to_white_kelvin(self, hs_color):
        hue, saturation = hs_color

        if saturation <= 8:
            return 4000

        if (
            (20 <= hue <= 45 and saturation <= 35)
            or (0 <= hue <= 1 and saturation == 0)
            or (200 <= hue <= 220 and saturation <= 20)
            or (28 <= hue <= 32 and saturation <= 85)
        ):
            if 20 <= hue <= 45:
                kelvin = int(2700 + (hue - 20) * (5000 - 2700) / 25)
            elif 200 <= hue <= 220:
                kelvin = 5500
            else:
                kelvin = 4000
            return self._clamp_color_temp_kelvin(kelvin)

        return None


class LaurelMesh:
    def __init__(self, address, password):
        self.address = str(address)
        self.password = str(password)
        self.devices = {}
        self.link = None
        self.lock = threading.Lock()
        self.sequence_lock = threading.Lock()

    def close(self):
        try:
            if self.link and self.link.device:
                self.link.device.disconnect()
        except Exception:
            _LOGGER.exception("Failed to disconnect mesh link during close")
        finally:
            self.link = None

    def connect(self):
        if self.link is not None:
            _LOGGER.debug("Mesh already connected")
            return

        _LOGGER.debug("Attempting mesh connect for account=%s", self.address)
        last_error = None

        for device in self.devices.values():
            try:
                _LOGGER.debug(
                    "Trying mesh device mac=%s id=%s name=%s",
                    device.mac,
                    device.id,
                    device.name,
                )
                self.link = dimond.dimond(
                    0x0211, device.mac, self.address, self.password, self, callback
                )
                self.link.connect()
                _LOGGER.debug(
                    "Connected to mesh via mac=%s id=%s name=%s",
                    device.mac,
                    device.id,
                    device.name,
                )
                return
            except Exception as err:
                last_error = err
                _LOGGER.exception(
                    "Connect failed mac=%s id=%s name=%s error=%r",
                    device.mac,
                    device.id,
                    device.name,
                    err,
                )
                self.link = None

        raise Exception(
            f"Unable to connect to mesh {self.address}; last_error={last_error!r}"
        )

    def send_packet(self, id_, command, params):
        with self.lock:
            _LOGGER.debug(
                "mesh send_packet id=%s command=0x%02X params=%s link=%s",
                id_,
                command,
                params,
                self.link is not None,
            )
            try:
                if self.link is None:
                    self.connect()
                self.link.send_packet(id_, command, params)
            except Exception as err:
                _LOGGER.exception(
                    "mesh send_packet failed id=%s command=0x%02X params=%s error=%r",
                    id_,
                    command,
                    params,
                    err,
                )
                self.link = None
                self.connect()
                _LOGGER.debug("mesh reconnected, retrying packet once")
                self.link.send_packet(id_, command, params)

    def send_critical_command(self, id_, label, command, params):
        _LOGGER.debug(
            "critical command id=%s label=%s command=0x%02X params=%s",
            id_,
            label,
            command,
            params,
        )
        self.send_packet(id_, command, params)

    def run_critical_sequence(self, id_, label, steps):
        with self.sequence_lock:
            _LOGGER.debug(
                "sequence start id=%s label=%s steps=%s",
                id_,
                label,
                [(step_label, f"0x{command:02X}", params) for step_label, command, params in steps],
            )
            for step_label, command, params in steps:
                self.send_critical_command(id_, f"{label}:{step_label}", command, params)
            _LOGGER.debug("sequence done id=%s label=%s", id_, label)

    def update_status(self):
        _LOGGER.debug("Broadcast mesh status update")
        self.send_packet(0xFFFF, 0xDA, [])
