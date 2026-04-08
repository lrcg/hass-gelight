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
"""

import logging
import sys
import threading
from datetime import timedelta
from time import sleep, time

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

sys.path.append("/config/deps/lib/python3.11/site-packages/")
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
        vol.Optional(CONF_LIGHTS, default=[]): vol.All(
            cv.ensure_list, [LIGHT_SCHEMA]
        ),
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
            response = responses[i:i + 4]
            if len(response) < 4:
                break

            devid = response[0]
            if devid == 0:
                break

            _LOGGER.debug("callback parsed devid=%s response=%r", devid, response)
            device = mesh.devices.get(devid)
            if device is None:
                _LOGGER.debug("callback unknown device id=%s response=%r", devid, response)
                continue

            brightness = response[2]

            if brightness >= 128:
                brightness = brightness - 128
                red = int(((response[3] & 0xE0) >> 5) * 255 / 7)
                green = int(((response[3] & 0x1C) >> 2) * 255 / 7)
                blue = int((response[3] & 0x03) * 255 / 3)

                device.red = red
                device.green = green
                device.blue = blue
                device._hs_color = colorutil.color_RGB_to_hs(red, green, blue)
                device._last_confirmed_hs = device._hs_color
                device._device_last_temp_value = None
                device._attr_color_mode = ColorMode.HS
                device._last_confirmed_at = time()
            else:
                # raw device temp byte; convert later once scale is confirmed
                device._device_last_temp_value = response[3]
                device._last_confirmed_hs = None
                device._attr_color_mode = ColorMode.COLOR_TEMP

            device._brightness = 255 * brightness // 100

            _LOGGER.debug(
                "%s confirmed state mode=%s brightness=%s hs=%s raw_temp=%s",
                device._name,
                device._attr_color_mode,
                device._brightness,
                getattr(device, "_hs_color", None),
                getattr(device, "_device_last_temp_value", None),
            )

            device.schedule_update_ha_state()

            _LOGGER.debug(
                "callback device=%s brightness=%s mode=%s raw=%r",
                device._name,
                device._brightness,
                device._attr_color_mode,
                response,
            )

    except Exception:
        _LOGGER.exception("callback failed data=%r", data)

async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up GE light platform."""
    devices = config.get(CONF_LIGHTS)
    lights = []

    mesh = laurel_mesh(
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

    mesh.devices = {}
    for light in lights:
        mesh.devices[light.id] = light

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

        self.power = None
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
        self._pending_hs_target = None
        self._last_confirmed_hs = None
        self._last_confirmed_at = 0

        # HA-facing Kelvin API
        self._attr_min_color_temp_kelvin = 2000   # warmest supported
        self._attr_max_color_temp_kelvin = 7000   # coldest supported
        self._color_temp_kelvin = 2000

        # Internal device-side mired values preserved from original logic
        self._device_min_mireds = colorutil.color_temperature_kelvin_to_mired(7000)
        self._device_max_mireds = colorutil.color_temperature_kelvin_to_mired(2000)

        # Exact original formula:
        # ratio = -100 / (max_mireds - min_mireds)
        self._device_temp_ratio = -100.0 / (
            self._device_max_mireds - self._device_min_mireds
        )

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

        _LOGGER.debug(
            "%s init: id=%s mac=%s type=%s rgb=%s ct=%s supported_color_modes=%s "
            "min_k=%s max_k=%s min_mired=%s max_mired=%s ratio=%s",
            self._name,
            self.id,
            self.mac,
            self.type,
            self.support_rgb(),
            self.support_color_temp(),
            self._attr_supported_color_modes,
            self._attr_min_color_temp_kelvin,
            self._attr_max_color_temp_kelvin,
            self._device_min_mireds,
            self._device_max_mireds,
            self._device_temp_ratio,
        )

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
        return self.power

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
        return self.type in {
            5, 6, 7, 8, 10, 11, 19, 20, 21, 22, 23, 31, 80, 83, 85, 147
        }

    def calc_brightness(self):
        if self._cl is None:
            self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is None:
                _LOGGER.debug("%s circadian lighting data unavailable", self._name)
                return self.brightness

        if self._cl.data["percent"] > 0:
            result = self._max_brightness
        else:
            result = int(
                ((self._max_brightness - self._min_brightness)
                 * ((100 + self._cl.data["percent"]) / 100))
                + self._min_brightness
            )

        _LOGGER.debug(
            "%s calc_brightness percent=%s result=%s",
            self._name,
            self._cl.data.get("percent"),
            result,
        )
        return result

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("%s turn_on kwargs=%r", self._name, kwargs)

        await self.hass.async_add_executor_job(self.set_power, True)

        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            _LOGGER.debug("%s requested brightness=%s", self._name, brightness)
            await self.hass.async_add_executor_job(self.set_brightness, brightness)
        elif CIRCADIAN_BRIGHTNESS:
            brightness = self.calc_brightness()
            _LOGGER.debug("%s circadian brightness=%s", self._name, brightness)
            await self.hass.async_add_executor_job(self.set_brightness, brightness)

        if ATTR_HS_COLOR in kwargs and self.support_rgb():
            requested_hs = kwargs[ATTR_HS_COLOR]
            _LOGGER.debug("%s requested hs_color=%s", self._name, requested_hs)

            if self.support_color_temp():
                mapped_kelvin = self.hs_to_white_kelvin(requested_hs)
                if mapped_kelvin is not None:
                    _LOGGER.debug(
                        "%s remapping white-ish hs_color=%s to color_temp_kelvin=%s",
                        self._name,
                        requested_hs,
                        mapped_kelvin,
                    )
                    await self.hass.async_add_executor_job(
                        self.set_color_temp_kelvin, mapped_kelvin
                    )
                    return

            await self.hass.async_add_executor_job(self.set_hs, requested_hs)
            return

        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if color_temp_kelvin is not None and self.support_color_temp():
            _LOGGER.debug("%s requested color_temp_kelvin=%s", self._name, color_temp_kelvin)
            await self.hass.async_add_executor_job(
                self.set_color_temp_kelvin, color_temp_kelvin
            )
            return

        if CIRCADIAN_BRIGHTNESS and self.support_color_temp() and brightness is None:
            if self._cl is None:
                self._cl = self.hass.data.get(DATA_CIRCADIAN_LIGHTING)
            if self._cl is not None:
                kelvin = int(self._cl.data["colortemp"])
                _LOGGER.debug("%s circadian color_temp_kelvin=%s", self._name, kelvin)
                await self.hass.async_add_executor_job(
                    self.set_color_temp_kelvin, kelvin
                )

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("%s turn_off", self._name)
        await self.hass.async_add_executor_job(self.set_power, False)

    def set_power(self, power):
        _LOGGER.debug("%s set_power power=%s", self._name, power)
        self.network.send_critical_command(
            self.id,
            "power",
            0xD0,
            [int(power)],
        )
        self.power = power

    def set_brightness(self, brightness):
        device_brightness = 100 * brightness // 255
        _LOGGER.debug(
            "%s set_brightness ha=%s device=%s",
            self._name,
            brightness,
            device_brightness,
        )
        self.network.send_critical_command(
            self.id,
            "brightness",
            0xD2,
            [device_brightness],
        )
        self._brightness = brightness

    def set_hs(self, hs_color):
        self._pending_hs_target = hs_color

        hue, saturation = hs_color
        red, green, blue = colorutil.color_hsv_to_RGB(
            hue, saturation, self._brightness * 100 / 255
        )

        _LOGGER.debug(
            "%s set_hs target_hs=%s rgb=(%s,%s,%s) brightness=%s",
            self._name,
            hs_color,
            red,
            green,
            blue,
            self._brightness,
        )

        def _send():
            self.network.send_critical_command(
                self.id,
                "hs_color",
                0xE2,
                [0x04, red, green, blue],
            )

        # Clear old confirmation before sending so we only accept fresh callback data
        self._last_confirmed_hs = None
        self._last_confirmed_at = 0.0

        # First attempt
        _send()

        # Give callback time to report the bulb's confirmed state
        sleep(0.45)

        if self.hs_matches(hs_color):
            _LOGGER.debug(
                "%s hs confirmed after first attempt target=%s confirmed=%s",
                self._name,
                hs_color,
                self._last_confirmed_hs,
            )
        else:
            _LOGGER.debug(
                "%s hs NOT confirmed after first attempt target=%s confirmed=%s; retrying",
                self._name,
                hs_color,
                self._last_confirmed_hs,
            )

            # Retry this bulb only
            self._last_confirmed_hs = None
            self._last_confirmed_at = 0.0
            _send()
            sleep(0.45)

            if self.hs_matches(hs_color):
                _LOGGER.debug(
                    "%s hs confirmed after retry target=%s confirmed=%s",
                    self._name,
                    hs_color,
                    self._last_confirmed_hs,
                )
            else:
                _LOGGER.warning(
                    "%s hs still not confirmed after retry target=%s confirmed=%s",
                    self._name,
                    hs_color,
                    self._last_confirmed_hs,
                )

        # Keep optimistic state for HA
        self._hs_color = hs_color
        self.red = red
        self.green = green
        self.blue = blue
        self._attr_color_mode = ColorMode.HS

    def set_color_temp_kelvin(self, color_temp_kelvin):
        original_kelvin = color_temp_kelvin
        color_temp_kelvin = max(
            self._attr_min_color_temp_kelvin,
            min(self._attr_max_color_temp_kelvin, int(color_temp_kelvin)),
        )

        color_temp_mired = colorutil.color_temperature_kelvin_to_mired(
            color_temp_kelvin
        )

        value = int(
            self._device_temp_ratio * (color_temp_mired - self._device_max_mireds)
        )
        value = max(0, min(100, value))

        _LOGGER.debug(
            "%s set_color_temp_kelvin requested=%s clamped=%s mired=%s packet_value=%s "
            "min_k=%s max_k=%s min_mired=%s max_mired=%s ratio=%s current_mode=%s",
            self._name,
            original_kelvin,
            color_temp_kelvin,
            color_temp_mired,
            value,
            self._attr_min_color_temp_kelvin,
            self._attr_max_color_temp_kelvin,
            self._device_min_mireds,
            self._device_max_mireds,
            self._device_temp_ratio,
            self._attr_color_mode,
        )

        self.network.send_critical_command(
            self.id,
            "color_temp",
            0xE2,
            [0x05, value],
        )

        if self._brightness > 0:
            resend_brightness = 100 * self._brightness // 255
            _LOGGER.debug(
                "%s resend brightness after white-mode switch ha=%s device=%s",
                self._name,
                self._brightness,
                resend_brightness,
            )
            self.network.send_critical_command(
                self.id,
                "brightness_after_white",
                0xD2,
                [resend_brightness],
            )

        self._color_temp_kelvin = color_temp_kelvin
        self._attr_color_mode = ColorMode.COLOR_TEMP

    def hs_matches(self, target_hs, hue_tolerance=8, sat_tolerance=10, max_age=2.0):
        if self._attr_color_mode != ColorMode.HS:
            return False

        if self._last_confirmed_hs is None:
            return False

        if (time() - self._last_confirmed_at) > max_age:
            return False

        hue, sat = self._last_confirmed_hs
        target_hue, target_sat = target_hs

        hue_diff = abs(hue - target_hue)
        hue_diff = min(hue_diff, 360 - hue_diff)

        return hue_diff <= hue_tolerance and abs(sat - target_sat) <= sat_tolerance

    def hs_to_white_kelvin(self, hs_color):
        """
        Narrow remap for HomeKit/Siri white-ish HS requests.

        Only remap warm-white/daylight style HS values that are low-saturation
        and close to the white range Siri appears to use.

        Returns:
            Kelvin int if this should be treated as white mode
            None otherwise
        """
        hue, saturation = hs_color

        # Very low saturation: treat as neutral white
        if saturation <= 8:
            return 4000

        # Narrow warm-white/daylight band seen from Siri/HomeKit voice requests.
        # Example observed: (31.0, 33.0)
        if 20 <= hue <= 45 and saturation <= 35:
            # Map warm -> cooler white across a restrained range.
            # 20 hue => 2700K
            # 45 hue => 5000K
            kelvin = int(2700 + (hue - 20) * (5000 - 2700) / (45 - 20))
            kelvin = max(
                self._attr_min_color_temp_kelvin,
                min(self._attr_max_color_temp_kelvin, kelvin),
            )
            return kelvin

        return None

    def update(self):
        _LOGGER.debug("%s manual update packet", self._name)
        self.network.send_packet(self.id, 0xDA, [])

    async def async_update(self):
        if self.id == 0:
            _LOGGER.debug("%s async_update triggered", self._name)
            await self.hass.async_add_executor_job(self.update)


class laurel_mesh:
    def __init__(self, address, password):
        self.address = str(address)
        self.password = str(password)
        self.devices = {}
        self.link = None
        self.lock = threading.Lock()

        # Reliability tuning for critical commands
        self.critical_repeat_count = 2
        self.critical_inter_packet_delay = 0.15
        self.connect_retry_count = 3
        self.connect_retry_delay = 1.0

    def __del__(self):
        if self.link and self.link.device:
            self.link.device.disconnect()

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
            sleep(0.05)

    def send_critical_command(self, id_, label, command, params):
        """
        Safer critical-command profile:
        - ensure mesh is connected once
        - send packet
        - if still connected, send same packet a second time
        - do not repeatedly reconnect in a loop
        """
        _LOGGER.debug(
            "critical command start id=%s label=%s command=0x%02X params=%s",
            id_,
            label,
            command,
            params,
        )

        # First send: may connect if needed
        self.send_packet(id_, command, params)

        # Second send: only replay if link still exists
        if self.link is not None:
            sleep(0.15)
            try:
                _LOGGER.debug(
                    "critical command replay id=%s label=%s command=0x%02X params=%s",
                    id_,
                    label,
                    command,
                    params,
                )
                self.link.send_packet(id_, command, params)
                sleep(0.05)
            except Exception as err:
                _LOGGER.exception(
                    "critical command replay failed id=%s label=%s command=0x%02X params=%s error=%r",
                    id_,
                    label,
                    command,
                    params,
                    err,
                )
                self.link = None

        _LOGGER.debug(
            "critical command done id=%s label=%s command=0x%02X params=%s",
            id_,
            label,
            command,
            params,
        )

    def update_status(self):
        _LOGGER.debug("Broadcast mesh status update")
        self.send_packet(0xFFFF, 0xDA, [])
