import logging  # Import the logging module for logging messages
import requests  # Import the requests library for making HTTP requests
import ipaddress  # Import the ipaddress module for IP address validation
import asyncio  # Import asyncio for asynchronous operations
from datetime import timedelta  # Import timedelta for time calculations
from enum import Enum  # Import Enum for creating enumerated constants
import voluptuous as vol  # Import voluptuous for configuration validation
from homeassistant.components.sensor import (
    SensorDeviceClass,
    RestoreSensor,
    SensorEntity,
    SensorStateClass,
    PLATFORM_SCHEMA
)
from homeassistant.components.binary_sensor import BinarySensorEntity
import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_NAME
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity
from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

# Define attribute names
ATTR_TOTAL_VOLTAGE = "Total Voltage"
ATTR_CURRENT_AMPS = "Current Amps"
ATTR_BMS_SOC = "BMS Level"
ATTR_OVERRIDDEN_SOC = "Battery Level"
ATTR_USED_CHARGE = "Used Charge"
ATTR_REGENERATED_CHARGE = "Regenerated Charge"
ATTR_UPTIME = "Uptime"
ATTR_CELL_VOLTAGE_TABLE = "Cell Voltage Table"
ATTR_TEMPERATURE_TABLE = "Temperature Table"
ATTR_CHARGE_SPEED = "Charge Speed"

# Configuration constants
CONF_OWIE_IP = 'owie_local_ip'
CONF_MAX_MISSED_PACKETS = 'max_missed_packets'
CONF_SCAN_INTERVAL = 'scan_owie_interval'

# Defaults
DEFAULT_NAME = 'Onewheel Battery Owie'
DEFAULT_SCAN_INTERVAL = 10
MIN_SCAN_INTERVAL = 5
DEFAULT_MAX_MISSED_PACKETS = 3
SCAN_INTERVAL = timedelta(seconds=10)

def _ip_val(value) -> str:
    """Validate input is an IP address."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise vol.Invalid("Not a valid IP address.")    
    return value

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_OWIE_IP): # Required: IP address of the Owie device
        _ip_val,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): # Optional: Custom name for the sensor
        cv.string,
    vol.Optional(CONF_MAX_MISSED_PACKETS, default=DEFAULT_MAX_MISSED_PACKETS): # Optional: Max missed packets
        vol.Coerce(int),
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): # Optional: Scan interval
        vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Owie sensor platform."""

    # Print configurable Variables
    _LOGGER.info("CONF_OWIE_IP: {}".format(config.get(CONF_OWIE_IP)))
    _LOGGER.info("CONF_NAME: {}".format(config.get(CONF_NAME)))
    _LOGGER.info("CONF_SCAN_INTERVAL: {}".format(config.get(CONF_SCAN_INTERVAL)))
    _LOGGER.info("CONF_MAX_MISSED_PACKETS: {}".format(config.get(CONF_MAX_MISSED_PACKETS)))

    # Set a custom SCAN_INTERVAL
    SCAN_INTERVAL = timedelta(seconds=config.get(CONF_SCAN_INTERVAL))

    data = OwieData(config.get(CONF_OWIE_IP))

    # Create and add Owie sensor entities
    sensors = [
        OwieBatterySensor(hass, data, config.get(CONF_NAME)),
        OwieChargingSensor(hass, data, config.get(CONF_NAME), config.get(CONF_MAX_MISSED_PACKETS)),
        OwieConnectivitySensor(hass, data, config.get(CONF_NAME), config.get(CONF_MAX_MISSED_PACKETS))
    ]
    async_add_entities(sensors, True)

def sanitize_response(owie_json):
    """Strip text from values before exporting."""
    _san_properties = ['TOTAL_VOLTAGE', 'CURRENT_AMPS', 'BMS_SOC', 'OVERRIDDEN_SOC', 'USED_CHARGE_MAH', 'REGENERATED_CHARGE_MAH']
    for prop in _san_properties:
        owie_json[prop] = owie_json[prop].strip('v').strip(' Amps').strip('%').strip('%').strip(' mAh').strip(' mAh')

    # Parse CELL_VOLTAGE_TABLE
    cell_voltage_table = owie_json.get('CELL_VOLTAGE_TABLE')
    if cell_voltage_table:
        soup = BeautifulSoup(cell_voltage_table, 'html.parser')
        rows = soup.find_all('tr')
        cell_voltage_values = []

        for row in rows:
            cols = row.find_all('td')
            cell_voltage_row = [col.text.strip() for col in cols if col.text.strip()]
            if cell_voltage_row:
                cell_voltage_values.extend(cell_voltage_row)
                break

        # Create keys starting from 'Cell 1' to 'Cell 15'
        cell_voltage_keys = [f'Cell {i}' for i in range(1, 16)]

        # Map values to keys
        cell_voltage_mapping = dict(zip(cell_voltage_keys, cell_voltage_values))

        owie_json['CELL_VOLTAGE_TABLE'] = cell_voltage_mapping
        # _LOGGER.debug("Owie Data CELL_VOLTAGE_TABLE: {}".format(cell_voltage_mapping))

    # Parse TEMPERATURE_TABLE
    temperature_table = owie_json.get('TEMPERATURE_TABLE')
    if temperature_table:
        soup = BeautifulSoup(temperature_table, 'html.parser')
        rows = soup.find_all('tr')
        temperature_values = []

        for row in rows:
            cols = row.find_all('td')
            temperature_row = [col.text.strip() for col in cols if col.text.strip()]
            if temperature_row:
                temperature_values.extend(temperature_row)
                break

        # Create keys starting from 'Temp 1' to 'Temp 5'
        temperature_keys = [f'Temp {i}' for i in range(1, 6)]

        # Map values to keys
        temperature_mapping = dict(zip(temperature_keys, temperature_values))

        owie_json['TEMPERATURE_TABLE'] = temperature_mapping
        # _LOGGER.debug("Owie Data TEMPERATURE_TABLE: {}".format(temperature_mapping))

    return owie_json

def charge_speed(amps):
    """Determine charge speed based on current amps."""
    if amps >= 0:
        return 'Not Charging'
    elif amps > -1:
        return 'Balance Charging'
    elif amps > -2:
        return 'Pint Charger'
    elif amps > -4:
        return 'XR|Pint Ultracharger'
    elif amps > -6:
        return 'XR Hypercharger'
    else:
        return 'Unknown Charger'

def charge_speed_icon(amps):
    """Determine charge speed icon based on current amps."""
    if amps >= 0:
        return 'mdi:power-plug-off-outline'
    elif amps > -1:
        return 'mdi:scale-balance'
    elif amps > -2:
        return 'mdi:speedometer-slow'
    elif amps > -4:
        return 'mdi:speedometer-medium'
    elif amps > -6:
        return 'mdi:speedometer'
    else:
        return 'mdi:flash-alert-outline'

def charge_icon(soc):
    """Determine battery charge icon based on SOC (State of Charge)."""
    if soc >= 95:
        return 'mdi:battery'
    elif soc >= 90:
        return 'mdi:battery-90'
    elif soc >= 80:
        return 'mdi:battery-80'
    elif soc >= 70:
        return 'mdi:battery-70'
    elif soc >= 60:
        return 'mdi:battery-60'
    elif soc >= 50:
        return 'mdi:battery-50'
    elif soc >= 40:
        return 'mdi:battery-40'
    elif soc >= 30:
        return 'mdi:battery-30'
    elif soc >= 20:
        return 'mdi:battery-20'
    elif soc >= 10:
        return 'mdi:battery-10'
    elif soc >= 0:
        return 'mdi:battery-outline'
    else:
        return 'mdi:battery-unknown'

class OwieBatterySensor(RestoreEntity):
    """Implementation of the battery sensor."""

    def __init__(self, hass, data, name):
        """Initialize the sensor."""
        self.hass = hass
        self.data = data
        self._name = name
        self._state = -1
        self._last_state = None

    @property
    def name(self):
        return self._name

    @property
    def device_class(self):
        return "battery"

    @property
    def state(self):
        """Return the state of the sensor."""
        override_value = int(self.data.info['OVERRIDDEN_SOC'])
        if self._last_state is not None and self._state == -1: # Restore state using last state
            self._state = self._last_state
            self._last_state = None
        elif self._state != -1 and override_value == -1: # Keep using last state while Owie is not connected
            return self._state
        elif override_value != -1: # Use data from Owie while connected
            self._state = override_value
        else:
            self._state = 0 # Used if a new entity with no history and hasn't connected to Owie
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attrs = {
            ATTR_OVERRIDDEN_SOC: self._state,
            ATTR_BMS_SOC: float(self.data.info["BMS_SOC"]),
            ATTR_REGENERATED_CHARGE: float(self.data.info['REGENERATED_CHARGE_MAH']),
            ATTR_USED_CHARGE: float(self.data.info["USED_CHARGE_MAH"]),
            ATTR_TOTAL_VOLTAGE: float(self.data.info['TOTAL_VOLTAGE'])
        }
        
        # Check if CELL_VOLTAGE_TABLE exists in data.info
        if 'CELL_VOLTAGE_TABLE' in self.data.info:
            attrs[ATTR_CELL_VOLTAGE_TABLE] = self.data.info['CELL_VOLTAGE_TABLE']

        # Check if TEMPERATURE_TABLE exists in data.info
        if 'TEMPERATURE_TABLE' in self.data.info:
            attrs[ATTR_TEMPERATURE_TABLE] = self.data.info['TEMPERATURE_TABLE']
        
        return attrs

    @property
    def state_class(self):
        """Return the type of state for HA long term statistics."""
        return "measurement"

    @property
    def icon(self):
        """Icon to use in the frontend"""
        return charge_icon(self._state)

    async def async_added_to_hass(self):
        """Run when entity about to be added to Home Assistant."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state is not None:
            self._last_state = int(last_state.state)

    async def async_update(self):
        """Get the latest data from Owie and update the states."""
        await self.hass.async_add_executor_job(self.data.update)

class OwieChargingSensor(BinarySensorEntity):
    """Implementation of the charging state sensor."""

    def __init__(self, hass, data, name, mpm):
        """Initialize the sensor."""
        self.hass = hass
        self.data = data
        self._name = name
        self.current_current = 1
        self._new_uptime = 'Offline'
        self._old_uptime = 'Offline'
        self._max_missed_packets = mpm
        self._missed_packets = 0
        self._connected = False

    @property
    def name(self):
        return f"{self._name}.ChargingStatus"

    @property
    def device_class(self):
        return "battery_charging"

    @property
    def is_on(self):
        """Return the state of the sensor."""

        # Determine if Owie is connected or not
        self._new_uptime = str(self.data.info['UPTIME'])
        if self._new_uptime != 'Offline' and self._new_uptime == self._old_uptime: # Owie gets disconnected and the time stalls
            # _LOGGER.info("ConnectivityStatus: Owie Disconnected")
            # _LOGGER.debug("_old_uptime: {}".format(self._old_uptime))
            # _LOGGER.debug("_new_uptime: {}".format(self._new_uptime))
            if self._missed_packets < self._max_missed_packets:
                self._missed_packets += 1
                # _LOGGER.info("Time Stale: Missed Packet {}".format(self._missed_packets))
                self._connected = True
            else:
                self._connected = False
        elif self._new_uptime != 'Offline' and self._new_uptime != self._old_uptime: # Owie connected and getting new values
            # _LOGGER.info("ConnectivityStatus: Owie Connected")
            # _LOGGER.debug("_old_uptime: {}".format(self._old_uptime))
            # _LOGGER.debug("_new_uptime: {}".format(self._new_uptime))
            self._missed_packets = 0
            self._old_uptime = self._new_uptime
            self._connected = True
        else: # Owie never connected or hass rebooted
            # _LOGGER.info("ConnectivityStatus: Owie Never Connected")
            self._missed_packets = 0
            self._connected = False

        if self._connected == True:
            self.current_current = float(self.data.info['CURRENT_AMPS'])
            if self.current_current >= 0:
                return False
            else:
                return True
        else:
            self.current_current = 0
            return False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attrs = {
            ATTR_CHARGE_SPEED: charge_speed(self.current_current),
            ATTR_CURRENT_AMPS: float(self.data.info['CURRENT_AMPS'])
        }
        return attrs

    @property
    def icon(self):
        """Icon to use in the frontend"""
        return charge_speed_icon(self.current_current)

    async def async_update(self):
        """Get the latest data from Owie and update the states."""
        await self.hass.async_add_executor_job(self.data.update)

class OwieConnectivitySensor(BinarySensorEntity):
    """Implementation of the connectivity state sensor."""

    def __init__(self, hass, data, name, mpm):
        """Initialize the sensor."""
        self.hass = hass
        self.data = data
        self._name = name
        self._new_uptime = 'Offline'
        self._old_uptime = 'Offline'
        self._max_missed_packets = mpm
        self._missed_packets = 0

    @property
    def name(self):
        return f"{self._name}.ConnectivityStatus"

    @property
    def device_class(self):
        return "connectivity"

    @property
    def is_on(self):
        """Return the state of the sensor."""
        self._new_uptime = str(self.data.info['UPTIME'])
        if self._new_uptime != 'Offline' and self._new_uptime == self._old_uptime: # Owie gets disconnected and the time stalls
            # _LOGGER.info("ConnectivityStatus: Owie Disconnected")
            # _LOGGER.debug("_old_uptime: {}".format(self._old_uptime))
            # _LOGGER.debug("_new_uptime: {}".format(self._new_uptime))
            if self._missed_packets < self._max_missed_packets:
                self._missed_packets += 1
                # _LOGGER.info("Time Stale: Missed Packet {}".format(self._missed_packets))
                return True
            else:
                return False
        elif self._new_uptime != 'Offline' and self._new_uptime != self._old_uptime: # Owie connected and getting new values
            # _LOGGER.info("ConnectivityStatus: Owie Connected")
            # _LOGGER.debug("_old_uptime: {}".format(self._old_uptime))
            # _LOGGER.debug("_new_uptime: {}".format(self._new_uptime))
            self._missed_packets = 0
            self._old_uptime = self._new_uptime
            return True
        else: # Owie never connected or hass rebooted
            # _LOGGER.info("ConnectivityStatus: Owie Never Connected")
            self._missed_packets = 0
            return False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attrs = {
            ATTR_UPTIME: str(self.data.info['UPTIME'])
        }
        return attrs

    @property
    def icon(self):
        """Icon to use in the frontend"""
        if str(self.data.info['UPTIME']) == 'Offline':
            return 'mdi:network-off-outline'
        else:
            return 'mdi:network-outline'

    async def async_update(self):
        """Get the latest data from Owie and update the states."""
        await self.hass.async_add_executor_job(self.data.update)

class OwieData(object):
    """The coordinator for handling the data retrieval."""

    def __init__(self, owie_ip):
        """Initialize the info object."""
        self._owie_ip = owie_ip
        self._owie_address = f"http://{owie_ip}/autoupdate"
        self.info = {}
        self.info.setdefault('TOTAL_VOLTAGE', '0')
        self.info.setdefault('CURRENT_AMPS', '0')
        self.info.setdefault('BMS_SOC', '0')
        self.info.setdefault('OVERRIDDEN_SOC', '-1')
        self.info.setdefault('USED_CHARGE_MAH', '0')
        self.info.setdefault('REGENERATED_CHARGE_MAH', '0')
        self.info.setdefault('UPTIME', 'Offline')
        self.info.setdefault('CELL_VOLTAGE_TABLE', {'Cell {}'.format(i): '0' for i in range(1, 16)})
        self.info.setdefault('TEMPERATURE_TABLE', {'Temp {}'.format(i): '0' for i in range(1, 6)})

    def update(self):
        try:
            response = requests.get(self._owie_address, headers=None, timeout=1)
            if response.status_code == requests.codes.bad:
                # If Owie is online but sending errors
                _LOGGER.error("Updating Owie status got {}:{}".format(response.status_code, response.content))
            else:
                self.info = sanitize_response(response.json())
                # _LOGGER.debug("Owie Data got {}".format(self.info))
        except OSError:
            #If owie offline
            _LOGGER.info("Unable to connect to Owie device: {}".format(self._owie_ip))
