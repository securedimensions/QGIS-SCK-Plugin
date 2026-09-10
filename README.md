# STAplus SCK

QGIS 4 plugin that signs in with [AUTHENIX](https://authenix.eu), reads a [Smart Citizen Kit](https://smartcitizen.me) over USB serial, and publishes observations to a [STAplus](https://docs.ogc.org/is/22-022r1/22-022r1.html) service.

| | |
|---|---|
| QGIS | 4.0 – 4.99 (Qt 6) |
| Version | 0.5.0 (experimental) |
| License | [GPL-2.0-or-later](LICENSE) |
| Author | [Secure Dimensions GmbH](https://www.secure-dimensions.de) |

This software is in development. Please report issues to [support@secure-dimensions.de](mailto:support@secure-dimensions.de).

## Features

- AUTHENIX sign-in with a public OAuth2 client (authorization code + PKCE, system browser, loopback redirect)
- Thing location as a marker on the QGIS map (click to place, or **Locate me**)
- Optional Feature of Interest from named public OpenStreetMap places near the marker
- USB serial connection to a Smart Citizen Kit (temperature, humidity, light, noise, pressure, PM1 / PM2.5 / PM10)
- Live readings in the dock and charts on the map at the Thing marker
- STAplus Party / Thing / Datastream setup, then MQTT publish of ObservationGroups

The default STAplus endpoint is the CitiObs demo:

https://citiobs.demo.secure-dimensions.de/stapluscelltest/v1.1

## Requirements

- [QGIS](https://qgis.org) 4.x
- A Smart Citizen Kit v2.1 and a reliable USB cable
- Network access for AUTHENIX, STAplus, MQTT, and (optional) Overpass / elevation
- Serial access: [pyserial](https://pypi.org/project/pyserial/) in the QGIS Python environment, **or** a QGIS build that includes Qt Serial Port

Typical kit ports:

- Linux: `/dev/ttyACM0`
- macOS: `/dev/tty.usbmodem…`
- Windows: `COMx`

On macOS, **Locate me** needs Location Services enabled for QGIS (System Settings → Privacy & Security → Location Services).

## Installation

The plugin is marked experimental. In **Plugins → Manage and Install Plugins → Settings**, enable **Show also experimental plugins**.

### Install from ZIP

1. Zip the plugin **folder** (the directory that contains `metadata.txt`), not the files inside it:

   ```bash
   zip -r STAplus-SCK.zip qgis-sck-plugin
   ```

   Avoid Finder’s “Compress” on macOS if the archive has no root folder; QGIS then reports that it is not a valid plugin.

2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Enable **STAplus SCK**.

### Install for development

1. **Settings → User Profiles → Open Active Profile Folder**.
2. Copy or symlink this repository into `python/plugins/qgis-sck-plugin`.
3. Restart QGIS and enable the plugin under **Plugins → Manage and Install Plugins → Installed**.

After a code change, use **Plugins → Reload Plugin** if you have a reloader installed, or restart QGIS.

## Usage

Open the dock from **Web → STAplus SCK** or the Web toolbar icon. The panel docks on the right.

1. **Sign in** — the system browser opens AUTHENIX. After you authorize, the plugin stores the access token. There is no silent refresh; when the token expires, sign in again.
2. **Name** — location name written to STAplus `Location.name` (default: `QGIS SCK marker`).
3. **Place marker on map** or **Locate me**, then **Use this marker** to confirm the Thing location.
4. Optional: **Find nearby places** (OpenStreetMap Overpass, 300 m). Click a public park, lake, or civic building to use it as Feature of Interest **with its public geometry**. **Clear FoI** returns to **The World** (no geometry).
5. Choose the serial port, **Refresh** if needed, then **Connect kit**. The kit MAC becomes STAplus `Thing` property `sck_id`.
6. **Start publishing** — choose a Party display name, a License template, attribution if required, and consent. The plugin creates Party / Thing / Datastreams and starts MQTT.
7. **Stop** ends publishing. The serial connection stays open until you disconnect the kit.

**Show charts** (or click the Thing marker) draws the last 30 minutes of samples on the map.

Plugin messages appear in the dock log and under **Log Messages → STAplus SCK**.

## Privacy

STAplus can attach the signed-in user to the `Thing` and `Datastream` via `Party`. For `Party/role` `individual`, `Thing/Location`, `HistoricalLocation`, and observation times are personal data.

Do not attach a Feature of Interest geometry unless the observed object is publicly observable (park, lake, public building). Private homes and gardens should keep geometry empty. The default FoI is **The World** with no geometry. Nearby-place search lists named public OSM objects only.

Before publishing, you must consent that the Party display name is stored on the service and can be read by anyone using it.

The configured STAplus endpoint follows the STAplus GDPR note: `/Locations` and `HistoricalLocations` are returned only to the user linked to `Thing/Party`.

## License

This plugin is free software under the [GNU General Public License version 2 or later](LICENSE).

It adapts AUTHENIX and STAplus behaviour from the [STAplus-SCK-App](https://github.com/securedimensions/STAPlus-SCK-App) (Secure Dimensions GmbH).

The source code was generated with [Cursor](https://cursor.com).