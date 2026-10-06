# Omoda / Jaecoo for Home Assistant

An **unofficial Home Assistant custom integration** for vehicles using the European **OMODA JAECOO** app. It provides native account setup, cloud telemetry, optional location tracking and opt-in vehicle controls.

**Preview release — UK-tested, EU backend only.** Not CarLinko, not a universal Chery integration, and not affiliated with the manufacturer or Home Assistant. Model-specific capabilities vary. Lock/climate requests have been reported working by the reference user, but broad compatibility and terminal command acknowledgements are not established.

Requires **Home Assistant 2026.7.1+**. Offline integration tests run against 2026.7.1 and 2026.9.4.

## Installation

### HACS custom repository

This project is **not yet in the HACS default catalogue**. Add it as a custom repository:

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/raldred/ha-omoda-jaecoo` and select **Integration**.
3. Download **Omoda / Jaecoo**. Preview releases may require enabling beta versions in HACS.
4. Restart Home Assistant Core.
5. Open **Settings → Devices & services → Add integration → Omoda / Jaecoo**.

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=raldred&repository=ha-omoda-jaecoo&category=integration)

Do not install alongside another custom integration using the same `omoda_jaecoo` domain/folder. Review and remove any conflict first.

### Manual installation

Download `omoda_jaecoo.zip` from a [release](https://github.com/raldred/ha-omoda-jaecoo/releases), verify it against `SHA256SUMS`, and extract it into `/config/custom_components/`. The result must be `/config/custom_components/omoda_jaecoo/manifest.json`—do not nest the folder twice. Back up the existing integration before replacing it, then restart HA Core.

Alternatively, copy only `custom_components/omoda_jaecoo/` from this repository. No YAML configuration is required.

## Account setup

Choose two things independently:

| Account identifier | Sign-in method |
|---|---|
| Email | Password or one-time email code |
| Phone | Password or one-time SMS code |

Use the identifier **already registered in the official app**. Phone inputs accept national formatting and +/00 international formatting; the selected country code must match. Significant zeros are preserved where appropriate.

For OTP sign-in, explicitly request a code, then enter it in the masked field. Nothing is sent just by opening a form. Resends wait at least 60 seconds and respect the server's `Retry-After`; three rejected code attempts require a new code. Discovery failures can be retried using the authenticated session without another login or code request.

Select the vehicle(s) to add, then optionally save the **separate vehicle-control PIN**. This is not the account password or OTP. The PIN is not checked during setup; remote controls remain disabled until explicitly enabled.

### Sessions and credentials

- Passwords and OTPs are used transiently and **not saved**. Access/refresh tokens are saved and rotated automatically.
- Normal polling and HA restarts do not perform password login or send OTPs. Native reauthentication uses the saved account and method if the session can no longer be refreshed.
- Email/password has been live-validated on the reference account. The other three routes are protocol-backed and offline-tested, not equivalent live-compatibility claims. SMS delivery may be refused by the backend's client filtering; the integration does not weaken TLS or cycle clients to bypass a refusal.
- Signing in through HA, another identifier, or the official app may invalidate another session. A separate delegated account may help, subject to its actual permissions. Duplicate VINs across integration entries are blocked.
- Tokens, the optional PIN, identifiers and vehicle metadata live in HA configuration/backups, which are **not automatically encrypted**. Protect them. Use **Reconfigure** to replace or remove a saved PIN.
- Diagnostics are allowlisted: no credentials, VINs, GPS values or raw responses. HTTP debug/proxy traces can nevertheless contain credentials; do not publish them.

## Vehicle entities

### Enabled by default

| Entity | Behaviour |
|---|---|
| Battery | Percentage normalized to one decimal place |
| Electric range | Native km, miles suggested for display |
| Odometer | Native km, miles suggested for display |
| Reported speed | Native km/h, mph suggested for display |
| Cabin temperature | Uses HA temperature-unit preferences |
| Charging status | Unplugged / Plugged in / Charging, otherwise unknown |
| Charge time remaining | Minutes, when reported while charging |
| Estimated charging finish | Derived from fresh observation time + remaining minutes |
| Vehicle report time / Cloud last checked / Telemetry freshness | Keep source age separate from fetch time |
| Door lock, climate running, four doors and boot | Read-only reported states |

HA handles distance/speed conversions and preserves your later entity-unit preferences. API display flags do not change the native kilometre fields.

### Additional entities, disabled by default

Enable relevant entities from the vehicle's entity list:

- Four windows and sunroof.
- Windscreen defrost, heated windscreen, rear-window heating and steering-wheel heating.
- Driver/passenger/rear seat heating and ventilation levels.
- HV battery voltage/current and direct reported charging power.
- Raw charging diagnostics for troubleshooting.

A field in a shared SDK payload does **not** establish that the hardware is fitted. Seat levels remain numeric codes, not invented Low/Medium/High labels. Unsupported codes and missing values stay unknown. Actual tyre pressure/temperature readings have not yet been validated for the reference vehicle; the pressure-unit flag alone is not used to invent them.

## Charging data

The supplied official EU app confirms cable connection when `chargeGunState == 1` or `fastChargingGunStatus == 1`, charging when `chargeState == 1`, and completed when `chargeState == 2`. Completed-but-connected appears as **Plugged in**. Unknown/contradictory codes are not treated as false. Raw numeric codes are available as attributes.

**Remaining time is minutes**, verified by tracing the official app's formatter: it divides the unchanged field by 60 for hours and uses the remainder for minutes. Raw `165` means **2 h 45 min**. There is no user API-unit calibration setting; old calibration options are removed automatically.

ETA requires an explicit, nonfuture source timestamp no older than 15 minutes. It is anchored to that sample—not now plus a stale duration—and assumes charging continues at the reported estimate. Smart-charger pauses can extend the actual finish. Missing time, stopped charging or an ambiguous timestamp yields unknown, not zero.

Reported charging power uses the direct `chargingPower` field in kW according to the community mapping, when charging is reported. It still needs model-specific comparison with the app. No `voltage × current` estimate or claim of mains/grid input power is made.

### Experimental schedule and target reads

Enable **Read experimental charging settings** to request the vehicle SDK's schedule/depth data, at most every 15 minutes:

- Schedule reports include the main switch and returned plans. Start/duration follow the upstream minute-based interpretation; repeat-day codes remain raw. No timezone, weekday mapping or next-start timestamp is invented. Missing is not disabled. This is the **vehicle's schedule**, not an Ohme/charger or Octopus smart-charging plan.
- The charging-depth query is a candidate for a charge target, not verified target SoC. Its raw diagnostic is disabled by default. Only after comparing it with the car's actual target should **Charging depth is a verified target percentage** be enabled. This merely labels data as %, never changes a limit. No default target is fabricated.

These optional queries are not yet live-validated on the reference vehicle. Failures cannot block the main telemetry refresh; explicit endpoint failures back off for an hour. No charge start/stop, schedule or limit writes are implemented.

## Optional location

Enable **Record last-reported vehicle location** to add a native map tracker. It requests existing data through `queryVehicleLocation`, **not** the separate locate/wake command. Queries are limited to the normal cadence, at most once per five minutes; fast control-feedback reads do not repeatedly query GPS.

The tracker rejects invalid coordinates and placeholder 0,0, does not guess coordinate scaling, and does not restore a point after a failed query. Only explicit GPS/position timestamps count as fix times. This is **last-reported**, not guaranteed live GPS—do not use it alone for security or automatic unlocking.

**Privacy:** off by default. When enabled, coordinates enter HA state/history/backups. Disabling stops new tracking but does not erase existing history. Coordinates are not included in integration diagnostics.

## Native refresh action — no wake

Open **Developer tools → Actions → Omoda / Jaecoo: Refresh cloud status** and select the account. The UI fills the required config-entry ID:

```yaml
action: omoda_jaecoo.refresh_status
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
```

This refreshes the selected vehicles on that account and updates their entities. It is **not a physical ping or wake**, cannot prove the vehicle is awake, and may return cached data. It does not send a PIN check, OTP, horn, locate or control command.

Direct callers require administrator access; trusted automations may call it without a user context. There is no implicit all-accounts target. Overlapping manual requests are rejected and manual refreshes have a 10-second cooldown. Cloud failures raise an action error; readings remain in normal HA entities rather than a response payload.

## Experimental lock and climate controls

1. Save the correct control PIN using **Reconfigure**.
2. Enable **Experimental remote controls** in options.
3. Choose a climate duration actually reported as supported by the vehicle (default 15 minutes).
4. Test while safely parked and physically able to verify the result. Hybrid preconditioning may run the engine: use a safe, ventilated location. Do not automate unlocking before supervised checks.

Native `lock.lock` / `lock.unlock` and `climate.turn_on` / `climate.turn_off` are supported. Climate uses OFF / HEAT_COOL and discovered temperature bounds/step. Changing temperature while off/unknown only records a local preference; when reported on it submits one command. Set HVAC mode and temperature separately—combined requests are rejected rather than silently ignored.

**Accepted is not completed.** Locking/Unlocking appears immediately. After acceptance, up to five passive refreshes over at most 60 seconds improve feedback without resending the command. A credible requested-state report changes the native state; unchanged cached targets are not confirmation. A missing result ends pending feedback as unconfirmed/unknown. Climate gets the same bounded reads but does not claim terminal acknowledgement. MQTT completion messages are not currently consumed.

Commands are serialized, with at least 30 seconds between attempts. Timeouts/ambiguous outcomes are never automatically retried. Failed or inconclusive PIN checks pause controls until explicit PIN re-entry after checking the correct value in the app. Setup, polling, reauthentication and state restoration never operate the car.

## Freshness and polling

Polling defaults to **five minutes**, configurable from 5–60 minutes. It reads existing cloud snapshots and never sends a wake command. Vehicle data may remain stale when parked. Fetch time is not observation time; ambiguous timezones are not guessed.

Invalid battery/HV placeholder frames do not publish misleading zeros. Genuine zeros are retained unless a known degraded-frame pattern is present. Empty replies keep prior primary readings in memory but mark the missing snapshot; network failures make entities unavailable. Optional settings/location reads run in separate throttled tasks and do not hold up lock feedback.

## Development and release checks

Automated tests are retained in source for regression coverage, but **never included in the installation ZIP**. Fixtures are synthetic and real sockets are blocked. No production credentials belong in tests or CI.

```sh
uv sync --locked --group dev
uv run pytest -q

# Real Home Assistant harness, separate Python 3.14 environment
cd tests/ha
uv sync --locked
uv run pytest -q
```

The compatibility CI also runs HA 2026.7.1. HACS validation and Hassfest run without ignored checks; catalogue requirements are documented in [docs/HACS.md](https://github.com/raldred/ha-omoda-jaecoo/blob/main/docs/HACS.md).

Build a release from a committed tree:

```sh
python3 scripts/build_release.py --ref HEAD --expected-version 0.6.0
(cd dist && shasum -a 256 -c SHA256SUMS)
```

The builder produces a deterministic component-only ZIP, checksums and metadata identifying the exact source commit. It rejects unsafe paths, unexpected files, symlinks and private-key material. The manual release workflow runs the tests first and creates a **draft pre-release**, never automatically publishing it.

## Privacy, attribution and boundaries

App archives, decompiled sources, analysis, captures, credentials, private keys and development environments are not distribution content. The early standalone prototype probe has been retired from the current source; use native HA setup and diagnostics.

The software is MIT-licensed. Manufacturer branding is separately attributed and **not covered by MIT**; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Brand use identifies the integration's intended products and does not imply endorsement or affiliation.

The API implementation references [chery-connect-ha/omoda9-ha](https://github.com/chery-connect-ha/omoda9-ha), with attribution retained. No vendor MQTT certificates/private keys are bundled. Protocol constants are application constants, not account credentials. The private API can change without notice; no manufacturer support or universal compatibility is implied.
