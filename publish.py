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

"""Build STAplus ObservationGroup payloads and normalize SCK samples for MQTT publish."""

import logging
import math
import traceback

from qgis.PyQt.QtCore import QThread, pyqtSignal

from .catalog import DATASTREAMS
from .kit import _phenomenon_time, _utc_now_iso

_logger = logging.getLogger("sck.publish")


def pressure_normalization_factor(temperature=15.0, elevation=0.0):
    """Multiplicative factor from station pressure at elevation to sea-level equivalent."""
    gravity = 9.80665
    molar_mass = 0.0289644
    gas_constant = 8.31447
    t_kelvin = float(temperature) + 273.15
    return math.exp((gravity * molar_mass * float(elevation)) / (gas_constant * t_kelvin))


def apply_sample(config, sample):
    """Normalize one SCK sample (including sea-level pressure) for display and MQTT."""
    now = _utc_now_iso()
    stamp = _phenomenon_time((sample or {}).get("phenomenon_time"))
    temperature = float(sample["temperature"])
    humidity = float(sample["humidity"])
    light = float(sample["light"])
    noise = float(sample["noise"])
    elevation = float((config or {}).get("elevation") or 0)
    factor = pressure_normalization_factor(temperature, elevation)
    pressure = round(float(sample["pressure"]) * factor, 2)
    return {
        "phenomenon_time": stamp,
        "result_time": now,
        "temperature": temperature,
        "humidity": humidity,
        "light": light,
        "noise": noise,
        "pressure": pressure,
        "pm1": float(sample["pm1"]),
        "pm25": float(sample["pm25"]),
        "pm10": float(sample["pm10"]),
        "pressure_factor": factor,
    }


def observation_group_payload(config, values):
    """JSON-serializable ObservationGroup for MQTT topic v1.1/ObservationGroups."""
    result_time = values["result_time"]
    phenomenon_time = values["phenomenon_time"]
    observations = []
    for spec in DATASTREAMS:
        observations.append(
            {
                "phenomenonTime": phenomenon_time,
                "resultTime": result_time,
                "result": values[spec["sample_key"]],
                "Datastream": {"@iot.id": config[spec["config_key"]]},
                "FeatureOfInterest": {"@iot.id": config["foi_id"]},
            }
        )
    return {
        "name": "OG %s" % result_time,
        "description": " ",
        "creationTime": result_time,
        "endTime": result_time,
        "Party": {"@iot.id": config["party_id"]},
        "License": {"@iot.id": config["license_id"]},
        "Observations": observations,
    }


class SetupWorker(QThread):
    """Create Party / Thing / Datastreams / License on STAplus without blocking the UI."""

    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, params, parent=None):
        super().__init__(parent)
        self.params = dict(params or {})

    def run(self):
        try:
            from . import authenix
            from .sta import StaClient

            session = authenix.load_session()
            client = StaClient(session)
            config = client.setup_publishing(**self.params)
            self.succeeded.emit(config)
        except Exception:
            self.failed.emit(traceback.format_exc())
