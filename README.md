# USPS Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-usps.svg)](https://github.com/ha-parcel-integrations/ha-usps/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-usps/total.svg)](https://github.com/ha-parcel-integrations/ha-usps/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration for two separate USPS sources:

- **Informed Delivery** discovers incoming packages for the addresses enrolled on your USPS account. No tracking numbers to type.
- **API Tracking** follows tracking numbers you enter, through USPS's official Tracking API with your own developer app.

Each config entry uses one source; you can add both.

> **Pre-release.** Signing in to Informed Delivery is confirmed against a real account, but neither source has seen real package data yet. Statuses may be incomplete. Can you help test? See [#1 (Informed Delivery)](https://github.com/ha-parcel-integrations/ha-usps/issues/1) and [#2 (API Tracking)](https://github.com/ha-parcel-integrations/ha-usps/issues/2).

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Informed Delivery automatically discovers packages for enrolled addresses, including their expected delivery day
- API Tracking follows the tracking codes you add, with optional event history
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery days
- `usps.track_parcel` / `usps.untrack_parcel` services (API Tracking), so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- Informed Delivery: a USPS account with at least one address enrolled at [informeddelivery.usps.com](https://informeddelivery.usps.com)
- API Tracking: an app on the [USPS developer portal](https://developers.usps.com) with the Tracking API added, and its Consumer Key and Consumer Secret

Scanned letter mail from Informed Delivery is not supported (yet); this integration covers packages only.

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-usps` as an **Integration**.
3. Install **USPS** and restart Home Assistant. Until the first release, pick the `main` version.

### Manual

Copy `custom_components/usps` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → USPS**, then choose a source.

### Informed Delivery

1. Enter your USPS username (or email) and password.
2. USPS usually asks for a verification step: choose how to receive a passcode (for example by email), then enter the passcode. The form shows USPS's own text for each step.
3. The entry is created once USPS confirms at least one address enrolled in Informed Delivery.

Your password is not stored; only the session USPS hands back is kept, and it is renewed automatically while Home Assistant runs. After Home Assistant has been offline for a while that session expires, and Home Assistant asks you to sign in again (including the passcode).

Packages appear by themselves; there is nothing to add.

### API Tracking

1. Create an app on the [USPS developer portal](https://developers.usps.com) and add the **Tracking** API to it.
2. Enter the app's **Consumer Key** and **Consumer Secret**. They are checked against USPS right away.
3. Add parcels via the integration's **Configure** dialog, the [`usps.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

If USPS stops accepting the key (for example after you rotate it), Home Assistant asks for new credentials.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels (API Tracking only) | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Settings | Delivered parcels: filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Settings | Include status history | off | Adds a `history` attribute per parcel with each status update. Only API Tracking reports history. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing. The two sources behave a bit
differently, since only Informed Delivery can ever report a delivery window
and only API Tracking entries can run out of parcels to track:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet — this is the fallback that always fires for an API
  Tracking entry, since that source never reports a delivery window at all).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused, API Tracking only** — once every tracked parcel has been
  delivered, or nothing is tracked at all, an API Tracking entry stops
  polling until you add a parcel back (adding one always triggers an
  immediate check, regardless of the pause). An Informed Delivery entry never
  fully stops: with nothing hot or in transit it keeps polling at the normal
  cadence, since that's also how a newly discovered package shows up.
- A small, fixed per-hub offset is added on top, so not every USPS hub out
  there polls at exactly the same second.

## Removal

Standard HA removal applies: **Settings → Devices & Services → USPS → ⋮ → Delete**. Nothing is stored on USPS's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.usps_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.usps_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.usps_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.usps_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.usps_last_successful_update` | Diagnostic: when USPS was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. Informed Delivery only reaches `delivered` / `out_for_delivery` / `in_transit` / `unknown`; API Tracking's broader vocabulary can plausibly reach every status below:

| Status | Meaning |
|---|---|
| `registered` | Announced / received by USPS |
| `in_transit` | In the sorting network |
| `out_for_delivery` | With the courier today |
| `at_pickup_point` | Waiting for you at a pickup location |
| `delivered` | Delivered |
| `returning` | Going back to the sender |
| `problem` | USPS reports an exception |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the USPS device):

| Event | When |
|---|---|
| `usps_parcel_registered` | A new parcel appears in the active list |
| `usps_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `usps_parcel_delivered` | A parcel is delivered |
| `usps_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `usps.track_parcel` | `tracking_code`, optional `entry_id` | Start tracking through an API Tracking entry |
| `usps.untrack_parcel` | `tracking_code`, optional `entry_id` | Stop tracking through an API Tracking entry |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

To log sign-in as well, enable debug logging before adding the integration, in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.usps: debug
```

For an existing entry you can also use **Settings → Devices & Services → USPS → ⋮ → Enable debug logging**; disabling it again downloads the log.

The log never contains passwords, keys or tokens, and addresses are only logged as a short hash. It does contain USPS's own sign-in text, which can include your masked email address, so check it before posting. **⋮ → Download diagnostics** gives a redacted snapshot.

## Troubleshooting

- **A parcel shows `unknown`** — USPS has not scanned it yet (their API answers `not_found` until the first scan), or the code is wrong. It will pick up automatically once scanned.
- **A status logs "Unrecognised USPS status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-usps/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended.
- **"No address on this USPS account is enrolled in Informed Delivery"** — sign-in worked, but the account has no enrolled address. Enroll one at [informeddelivery.usps.com](https://informeddelivery.usps.com) first.
- **"USPS did not accept that verification code"** — the passcode was wrong or expired. Sign in again to receive a new one.
- **Informed Delivery asks to sign in again** — the session expired, usually after Home Assistant was offline for a while. This cannot be renewed without you, because USPS asks for a passcode.
- **"These credentials do not have the USPS Tracking product enabled"** — add the Tracking API to your app on the USPS developer portal.
- **"Could not reach USPS"** — a network problem or a temporary USPS outage. Try again later; if it keeps happening, attach a debug log to an issue.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by USPS, Home Assistant, or any other third party referenced in this project. Please don't contact USPS for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to USPS's terms. Data is sent only to USPS's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses USPS account and API surfaces supplied by the user.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
