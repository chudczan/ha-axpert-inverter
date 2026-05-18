from datetime import datetime
import logging
import math
from typing import Optional, Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfApparentPower,
    UnitOfEnergy,
    UnitOfTemperature,
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import AxpertDataUpdateCoordinator
from .entity import AxpertEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Axpert sensor entities."""
    coordinator: AxpertDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        AxpertGridInputSensor(coordinator, "grid_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertGridInputSensor(coordinator, "grid_frequency", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY),
        AxpertSensor(coordinator, "ac_output_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertSensor(coordinator, "ac_output_frequency", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY),
        AxpertSensor(coordinator, "ac_output_active_power", UnitOfPower.WATT, SensorDeviceClass.POWER),
        AxpertSensor(coordinator, "ac_output_apparent_power", UnitOfApparentPower.VOLT_AMPERE, SensorDeviceClass.APPARENT_POWER),
        AxpertSensor(coordinator, "output_load_percent", PERCENTAGE, None),
        AxpertSensor(coordinator, "battery_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertSensor(coordinator, "battery_charging_current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
        AxpertSensor(coordinator, "battery_discharge_current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
        AxpertSensor(coordinator, "battery_capacity", PERCENTAGE, SensorDeviceClass.BATTERY),
        AxpertSensor(coordinator, "heat_sink_temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
        AxpertSensor(coordinator, "pv_input_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertSensor(coordinator, "pv_input_current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
        AxpertPVSensor(coordinator),
        AxpertOutputCurrentSensor(coordinator),
        AxpertGridCurrentSensor(coordinator),
        AxpertGridPowerSensor(coordinator),
        AxpertInverterLossSensor(coordinator),
        AxpertStatusSensor(coordinator),
        AxpertMachineTypeSensor(coordinator),
        AxpertReactivePowerSensor(coordinator),
        AxpertPowerFactorSensor(coordinator),
    ]
    
    entities.append(AxpertEnergySensor(coordinator, "pv_energy", "pv_power"))
    entities.append(AxpertEnergySensor(coordinator, "load_energy", "ac_output_active_power"))
    entities.append(AxpertEnergySensor(coordinator, "grid_energy", "grid_power"))

    async_add_entities(entities)

class AxpertSensor(AxpertEntity, SensorEntity):
    """Representation of an Axpert Sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, key, unit, device_class):
        """Initialize."""
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_unique_id = f"axpert_{key}"
        self._attr_state_class = SensorStateClass.MEASUREMENT if device_class else None
        if device_class == SensorDeviceClass.VOLTAGE:
            self._attr_suggested_display_precision = 2
        elif device_class in (SensorDeviceClass.CURRENT, SensorDeviceClass.POWER, SensorDeviceClass.APPARENT_POWER):
            self._attr_suggested_display_precision = 1
        elif device_class == SensorDeviceClass.TEMPERATURE:
            self._attr_suggested_display_precision = 1

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self.coordinator.data.get(self._key)

class AxpertGridInputSensor(AxpertSensor):
    """Sensor for Grid/Generator Input (Voltage/Frequency)."""
    
    @property
    def translation_key(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        base = "generator" if machine_type == "01" else "grid"
        if "voltage" in self._key:
            return f"{base}_voltage"
        if "frequency" in self._key:
            return f"{base}_frequency"
        return self._key

    @property
    def icon(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "mdi:generator-portable"
        return "mdi:transmission-tower"

class AxpertPVSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for PV Power (V * A)."""
    
    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_name = "PV Power"
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_unique_id = "axpert_pv_power"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self):
        if "pv_charging_power" in self.coordinator.data:
            return float(self.coordinator.data["pv_charging_power"])
        v = self.coordinator.data.get("pv_input_voltage", 0)
        a = self.coordinator.data.get("pv_input_current", 0)
        return round(float(v) * float(a), 1)

class AxpertEnergySensor(AxpertEntity, RestoreEntity, SensorEntity):
    """Sensor that integrates power over time to calculate energy (kWh)."""
    
    _MAX_INTEGRATION_INTERVAL = 300

    def __init__(self, coordinator, key, source_key):
        super().__init__(coordinator, source_type="calculated")
        self._key = key
        self._source_key = source_key
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_unique_id = f"axpert_{key}_total"
        self._attr_suggested_display_precision = 3
        self._state = 0.0
        self._last_update_time = None
        self._last_power = None

    @property
    def translation_key(self):
        if self._key == "grid_energy":
            machine_type = self.coordinator.data.get("machine_type", "00")
            if machine_type == "01":
                return "generator_energy"
        return self._attr_translation_key

    @property
    def icon(self):
        if self._key == "grid_energy":
            machine_type = self.coordinator.data.get("machine_type", "00")
            if machine_type == "01":
                return "mdi:generator-portable"
            return "mdi:transmission-tower"
        return None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state:
            try:
                self._state = float(state.state)
            except ValueError:
                self._state = 0.0
        self._last_update_time = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        now = dt_util.utcnow()
        current_power = 0.0
        if self._source_key == "pv_power":
            if "pv_charging_power" in self.coordinator.data:
                current_power = float(self.coordinator.data["pv_charging_power"])
            else:
                v = self.coordinator.data.get("pv_input_voltage", 0)
                a = self.coordinator.data.get("pv_input_current", 0)
                current_power = float(v) * float(a)
        elif self._source_key == "grid_power":
            try:
                p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
                batt_v = float(self.coordinator.data.get("battery_voltage", 0))
                batt_chg_i = float(self.coordinator.data.get("battery_charging_current", 0))
                p_charge = batt_v * batt_chg_i
                batt_dis_i = float(self.coordinator.data.get("battery_discharge_current", 0))
                p_discharge = batt_v * batt_dis_i
                pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
                pv_i = float(self.coordinator.data.get("pv_input_current", 0))
                p_pv = pv_v * pv_i
                current_power = p_load + p_charge - p_discharge - p_pv
                if current_power < 0:
                    current_power = 0.0
            except (ValueError, TypeError):
                current_power = 0.0
        else:
            current_power = float(self.coordinator.data.get(self._source_key, 0))

        if self._last_update_time is None or self._last_power is None:
            self._last_update_time = now
            self._last_power = current_power
            return

        time_diff_seconds = (now - self._last_update_time).total_seconds()
        if time_diff_seconds > self._MAX_INTEGRATION_INTERVAL:
            _LOGGER.debug(f"Time difference {time_diff_seconds}s > {self._MAX_INTEGRATION_INTERVAL}s. Skipping integration to avoid spikes.")
            self._last_update_time = now
            self._last_power = current_power
            return

        time_diff_hours = time_diff_seconds / 3600.0
        avg_power = (self._last_power + current_power) / 2.0
        added_energy_kwh = (avg_power / 1000.0) * time_diff_hours
        if added_energy_kwh > 0:
            self._state += added_energy_kwh
        self._last_update_time = now
        self._last_power = current_power
        self.async_write_ha_state()

    @property
    def native_value(self):
        return round(self._state, 3)

class AxpertOutputCurrentSensor(AxpertEntity, SensorEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_name = "Output Current"
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_unique_id = "axpert_output_current"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:current-ac"
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self):
        s = self.coordinator.data.get("ac_output_apparent_power", 0)
        v = self.coordinator.data.get("ac_output_voltage", 0)
        try:
            s_val = float(s)
            v_val = float(v)
            if v_val == 0:
                return 0.0
            return round(s_val / v_val, 1)
        except (ValueError, TypeError):
            return 0.0

class AxpertGridCurrentSensor(AxpertEntity, SensorEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_unique_id = "axpert_grid_current"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

    @property
    def translation_key(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "generator_current"
        return "grid_current"

    @property
    def icon(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "mdi:generator-portable"
        return "mdi:transmission-tower"

    @property
    def native_value(self):
        try:
            p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
            s_load = float(self.coordinator.data.get("ac_output_apparent_power", 0))
            q_load_sq = max(0, (s_load ** 2) - (p_load ** 2))
            q_load = math.sqrt(q_load_sq)
            batt_v = float(self.coordinator.data.get("battery_voltage", 0))
            batt_chg_i = float(self.coordinator.data.get("battery_charging_current", 0))
            p_charge = batt_v * batt_chg_i
            batt_dis_i = float(self.coordinator.data.get("battery_discharge_current", 0))
            p_discharge = batt_v * batt_dis_i
            pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
            pv_i = float(self.coordinator.data.get("pv_input_current", 0))
            p_pv = pv_v * pv_i
            p_grid = p_load + p_charge - p_discharge - p_pv
            s_grid = math.sqrt((p_grid ** 2) + (q_load ** 2))
            v_grid = float(self.coordinator.data.get("grid_voltage", 0))
            if v_grid < 10:
                return 0.0
            i_grid = s_grid / v_grid
            if p_grid < 0:
                i_grid = 0.0
            return round(i_grid, 1)
        except (ValueError, TypeError):
            return 0.0

class AxpertGridPowerSensor(AxpertEntity, SensorEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_unique_id = "axpert_grid_power"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

    @property
    def translation_key(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "generator_power"
        return "grid_power"

    @property
    def icon(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "mdi:generator-portable"
        return "mdi:transmission-tower"

    @property
    def native_value(self):
        try:
            p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
            batt_v = float(self.coordinator.data.get("battery_voltage", 0))
            batt_chg_i = float(self.coordinator.data.get("battery_charging_current", 0))
            p_charge = batt_v * batt_chg_i
            batt_dis_i = float(self.coordinator.data.get("battery_discharge_current", 0))
            p_discharge = batt_v * batt_dis_i
            pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
            pv_i = float(self.coordinator.data.get("pv_input_current", 0))
            p_pv = pv_v * pv_i
            p_grid = p_load + p_charge - p_discharge - p_pv
            if p_grid < 0:
                p_grid = 0.0
            return round(p_grid, 1)
        except (ValueError, TypeError):
            return 0.0

class AxpertInverterLossSensor(AxpertEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "inverter_consumption"

    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_unique_id = "axpert_inverter_consumption"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self):
        try:
            v_grid = float(self.coordinator.data.get("grid_voltage", 0))
            if v_grid >= 10:
                return 0.0
            p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
            batt_v = float(self.coordinator.data.get("battery_voltage", 0))
            batt_chg_i = float(self.coordinator.data.get("battery_charging_current", 0))
            p_charge = batt_v * batt_chg_i
            batt_dis_i = float(self.coordinator.data.get("battery_discharge_current", 0))
            p_discharge = batt_v * batt_dis_i
            pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
            pv_i = float(self.coordinator.data.get("pv_input_current", 0))
            p_pv = pv_v * pv_i
            p_loss = (p_pv + p_discharge) - (p_load + p_charge)
            if p_loss < 0:
                p_loss = 0.0
            return round(p_loss, 1)
        except (ValueError, TypeError):
            return 0.0

class AxpertMachineTypeSensor(AxpertEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "machine_type"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = ["grid_tie", "off_grid", "hybrid"]

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = "axpert_machine_type"

    @property
    def native_value(self):
        m_type = self.coordinator.data.get("machine_type", "")
        if m_type == "00":
            return "grid_tie"
        if m_type == "01":
            return "off_grid"
        if m_type == "10":
            return "hybrid"
        return None

class AxpertStatusSensor(AxpertEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["power_on", "standby", "line", "battery", "fault", "power_saving", "bypass", "unknown"]

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = "axpert_status"

    @property
    def native_value(self):
        mode = self.coordinator.data.get("mode", "")
        mapping = {"P": "power_on", "S": "standby", "L": "line", "B": "battery", "F": "fault", "H": "power_saving", "D": "bypass"}
        return mapping.get(mode, "unknown")

class AxpertReactivePowerSensor(AxpertEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "reactive_power"

    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_native_unit_of_measurement = "var"
        self._attr_device_class = SensorDeviceClass.REACTIVE_POWER
        self._attr_unique_id = "axpert_reactive_power"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self):
        try:
            p = float(self.coordinator.data.get("ac_output_active_power", 0))
            s = float(self.coordinator.data.get("ac_output_apparent_power", 0))
            q_sq = max(0, (s ** 2) - (p ** 2))
            return round(math.sqrt(q_sq), 1)
        except (ValueError, TypeError):
            return 0.0

class AxpertPowerFactorSensor(AxpertEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "power_factor"

    def __init__(self, coordinator):
        super().__init__(coordinator, source_type="calculated")
        self._attr_native_unit_of_measurement = None
        self._attr_unique_id = "axpert_power_factor"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2

    @property
    def native_value(self):
        try:
            p = float(self.coordinator.data.get("ac_output_active_power", 0))
            s = float(self.coordinator.data.get("ac_output_apparent_power", 0))
            if s == 0:
                return 0.0
            return round(p / s, 2)
        except (ValueError, TypeError):
            return 0.0
