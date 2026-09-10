# Copyright (C) 2026 Secure Dimensions GmbH, Munich, Germany.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""STAplus ObservedProperty / Sensor / Datastream catalog for the Smart Citizen Kit."""

OM_MEASUREMENT = "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement"

CELSIUS = {
    "name": "Celsius",
    "symbol": "C",
    "definition": "https://qudt.org/vocab/unit/DEG_C",
}
PERCENT = {
    "name": "Percentage",
    "symbol": "%",
    "definition": "https://qudt.org/vocab/unit/PERCENT",
}
LUX = {
    "name": "Lumens per square meter",
    "symbol": "LUX",
    "definition": "https://qudt.org/vocab/unit/LUX",
}
DBA = {
    "name": "A-weighted decibel",
    "symbol": "dBA",
    "definition": "https://qudt.org/vocab/unit/DeciB_A",
}
KPA = {
    "name": "kiloPascals",
    "symbol": "kPa",
    "definition": "https://qudt.org/vocab/unit/KiloPA",
}
UGM3 = {
    "name": "Microgram per cubic meter",
    "symbol": "µg/m³",
    "definition": "http://dd.eionet.europa.eu/vocabulary/uom/concentration/ug.m-3",
}

OBSERVED_PROPERTIES = {
    "temperature": {
        "name": "temp",
        "definition": "https://vocabs.lter-europe.net/EnvThes/en/page/22035",
        "description": "Air Temperature",
    },
    "humidity": {
        "name": "RH",
        "definition": "http://vocabs.lter-europe.net/EnvThes/22032",
        "description": "Relative Humidity",
    },
    "light": {
        "name": "light",
        "definition": "https://qudt.org/vocab/quantitykind/LuminousExposure",
        "description": "Ambient Light",
    },
    "noise": {
        "name": "noise",
        "definition": "https://www.merriam-webster.com/dictionary/noise",
        "description": "Noise Level",
    },
    "pressure": {
        "name": "pres",
        "definition": "https://qudt.org/vocab/quantitykind/AtmosphericPressure",
        "description": "Barometric Pressure",
    },
    "pm1": {
        "name": "PM1",
        "definition": "http://codes.wmo.int/wmdr/ParticleSizeRange/60",
        "description": (
            "Particulate matter with an average aerodynamic diameter of up to 1 micrometers"
        ),
    },
    "pm25": {
        "name": "PM25",
        "definition": "https://codes.wmo.int/wmdr/ParticleSizeRange/_70",
        "description": (
            "Particulate matter with an average aerodynamic diameter of up to 2.5 micrometers"
        ),
    },
    "pm10": {
        "name": "PM10",
        "definition": "https://codes.wmo.int/wmdr/ParticleSizeRange/_100",
        "description": (
            "Particulate matter with an average aerodynamic diameter of up to 10 micrometers"
        ),
    },
}

SENSORS = {
    "sht31": {
        "name": "Sensirion SHT31",
        "description": "Sensirion SHT31 Humidity and Temperature Sensor",
        "encodingType": "application/pdf",
        "metadata": "https://www.farnell.com/datasheets/2901984.pdf",
        "properties": {
            "sck_id": "SHT31",
            "description": "https://www.seeedstudio.com/Smart-Citizen-Starter-Kit-p-2865.html",
        },
    },
    "bh1721": {
        "name": "Rohm BH1721FVC",
        "description": "Rohm BH1721FVC Digital 16bit Serial Output Type Ambient Light Sensor ICt",
        "encodingType": "application/pdf",
        "metadata": "https://fscdn.rohm.com/en/products/databook/datasheet/ic/sensor/light/bh1721fvc-e.pdf",
        "properties": {
            "description": "https://www.seeedstudio.com/Smart-Citizen-Starter-Kit-p-2865.html",
        },
    },
    "ics43434": {
        "name": "Invensense ICS-434342",
        "description": "Invensense ICS-434342. Low‐Noise Microphone with I2S Digital Output",
        "encodingType": "application/pdf",
        "metadata": "https://invensense.tdk.com/wp-content/uploads/2015/02/ICS-43432-data-sheet-v1.3.pdf",
        "properties": {
            "description": "https://www.seeedstudio.com/Smart-Citizen-Starter-Kit-p-2865.html",
        },
    },
    "mpl3115": {
        "name": "MPL3115A2S",
        "description": "I2C precision pressure sensor with altimetry",
        "encodingType": "application/pdf",
        "metadata": "https://www.nxp.com/docs/en/data-sheet/MPL3115A2S.pdf",
        "properties": {
            "description": "https://www.seeedstudio.com/Smart-Citizen-Starter-Kit-p-2865.html",
        },
    },
    "pms5003": {
        "name": "Planttower PMS 5003",
        "description": "Planttower PMS 5003 Digital universal particle concentration sensor",
        "encodingType": "application/pdf",
        "metadata": "https://cdn-shop.adafruit.com/product-files/3686/plantower-pms5003-manual_v2-3.pdf",
        "properties": {
            "description": "https://www.seeedstudio.com/Smart-Citizen-Starter-Kit-p-2865.html",
        },
    },
}

# config key -> datastream fields (same eight series as STAplus-SCK-App).
DATASTREAMS = (
    {
        "config_key": "temp_id",
        "sample_key": "temperature",
        "name": "Air Temperature",
        "description": "air temperature measured with the SmartCitizen Kit",
        "unit": CELSIUS,
        "observed_property": "temperature",
        "sensor": "sht31",
    },
    {
        "config_key": "humidity_id",
        "sample_key": "humidity",
        "name": "Relative Humidity",
        "description": "air relative humidity measured with the SmartCitizen Kit",
        "unit": PERCENT,
        "observed_property": "humidity",
        "sensor": "sht31",
    },
    {
        "config_key": "light_id",
        "sample_key": "light",
        "name": "Ambient Light",
        "description": "ambient light measured with the SmartCitizen Kit",
        "unit": LUX,
        "observed_property": "light",
        "sensor": "bh1721",
    },
    {
        "config_key": "noise_id",
        "sample_key": "noise",
        "name": "Noise Level",
        "description": "noise measured with the SmartCitizen Kit",
        "unit": DBA,
        "observed_property": "noise",
        "sensor": "ics43434",
    },
    {
        "config_key": "pressure_id",
        "sample_key": "pressure",
        "name": "Barometric Pressure",
        "description": "air pressure measured with the SmartCitizen Kit",
        "unit": KPA,
        "observed_property": "pressure",
        "sensor": "mpl3115",
    },
    {
        "config_key": "pm1_id",
        "sample_key": "pm1",
        "name": "PM 1",
        "description": "PM 1 measured with the SmartCitizen Kit",
        "unit": UGM3,
        "observed_property": "pm1",
        "sensor": "pms5003",
    },
    {
        "config_key": "pm25_id",
        "sample_key": "pm25",
        "name": "PM 2.5",
        "description": "PM 2.5 measured with the SmartCitizen Kit",
        "unit": UGM3,
        "observed_property": "pm25",
        "sensor": "pms5003",
    },
    {
        "config_key": "pm10_id",
        "sample_key": "pm10",
        "name": "PM 10",
        "description": "PM 10 measured with the SmartCitizen Kit",
        "unit": UGM3,
        "observed_property": "pm10",
        "sensor": "pms5003",
    },
)
