# ha-omoda-jaecoo

An **unofficial, early-stage** project exploring Home Assistant support for Omoda and Jaecoo vehicles using the European **OMODA JAECOO** app.

**Current state: a native Home Assistant custom integration with read-only sensors and a standalone diagnostic script.** Tested offline against HA **2026.7.1 and 2026.9.4**. Account-password login and the battery/range/odometer field mappings were also verified with the standalone probe on one user's vehicle; the async HA implementation still needs live validation. This is not an official HA core integration or a guarantee of compatibility with every model.

It supports accounts on the EU OMODA JAECOO backend, not CarLinko or automatic regional routing. Lock/unlock and climate control remain planned and **are not enabled in this release**.

## Home Assistant installation and setup

Requires **Home Assistant 2026.7.1 or newer** for the current native config-flow implementation. No vehicle commands are issued during setup, startup, polling or reauthentication.

### Install manually (private repository)

1. Clone/download this repository using your GitHub account with access.
2. Copy **only** `custom_components/omoda_jaecoo/` into HA's configuration directory, resulting in `/config/custom_components/omoda_jaecoo/manifest.json`. Keep the folder name exactly `omoda_jaecoo`.
3. Restart Home Assistant at a suitable time. Installation/restart is a deliberate user action, not something the probe performs.
4. Go to **Settings → Devices & services → Add integration → Omoda / Jaecoo**.
5. Enter your account email, **account password**, and country dialling code.
6. If multiple vehicles are discovered, select the ones to add. A single vehicle is selected automatically.
7. Optionally enter the **separate vehicle control PIN**, or leave it blank. The form explains that it is **stored only, not verified or used** in this read-only release. No PIN request is sent to the vehicle API.

HACS metadata is included for future distribution, but this repo is currently private; do not assume ordinary public HACS installation will work. Manual installation is the documented path. Do not overwrite an existing unrelated integration using the same `omoda_jaecoo` domain—review/remove any conflict first.

### Native entities

Each selected vehicle gets a device with:

| Sensor | Native data | Default display |
|---|---|---|
| Battery | `dumpEnergy`, percent | % |
| Electric range | `dynamicPureElectricRange` (fallback `electricRange`/`pureElectricRange`), km | Miles |
| Odometer | `odometer`, km | Miles |
| Vehicle report time | Explicit-zone/epoch source timestamp, if understood | Timestamp or unknown |
| Cloud last checked | Time HA fetched the cloud snapshot | Timestamp |
| Telemetry freshness | Source timestamp age / snapshot status | Current, stale, unknown, no snapshot or unreliable |

**Miles are the initial default, even on a metric HA installation.** Choose kilometres in an entity's standard settings if preferred. HA handles conversion; the integration always stores distance measurements in kilometres and does not override your later preference.

`rangeUnit` is not used to reinterpret the raw kilometre fields: the probe confirmed 262 km → 163 mi on the reference vehicle. Other models/field fallbacks need validation. Negative, nonfinite and out-of-range battery values become unknown, not zero. Genuine zero is retained unless accompanied by the known invalid HV-frame pattern (zero voltage and -1000 current with zero battery/range). Those degraded snapshots keep previous readings, if any, and are labelled unreliable. This first version does not yet identify every possible vendor sleep sentinel.

Cloud reads default to **every 5 minutes**, configurable from 5–60 minutes under the integration's options. HTTP rate limiting backs polling off up to an hour. This is a conservative implementation choice, not a published vendor allowance. No wake/locate command, MQTT connection or automatic climate activation is involved. Empty/asleep replies retain previous measurements in memory but show **No snapshot returned**. Network failures make entities unavailable. A successful cloud read can still contain an old snapshot.

A report time without a timezone is **not guessed**. Unrecognized/missing source timestamps produce **Observation time unknown**. Source timestamps older than 15 minutes are labelled stale. This label is based on the timestamp returned by the service, not independent verification of individual sensors. **Cloud last checked is never presented as the vehicle's observation time.**

### Credentials and maintenance

- The account password is used once in the flow and **never saved** in the config entry. Access/refresh tokens are saved for automatic refresh, including rotated tokens. If the session is revoked, HA offers native reauthentication for the same account.
- The control PIN is optional. Use **Reconfigure** to replace it or remove it; blank means keep the existing value. It is never checked against the cloud in this release.
- HA config-entry storage/backups are **not automatically encrypted**. Protect saved tokens, optional PIN, account email and vehicle identifiers. Diagnostics expose only an allowlisted structural summary, not these values or raw telemetry/GPS.
- Logging into the official app can invalidate the HA session and vice versa. The integration never saves your password for repeated automatic login attempts.
- The private API uses a refresh-token query parameter (matching the upstream working protocol) over verified HTTPS. Integration exceptions do not expose URLs, and redirects are disabled. Treat HTTP debug/proxy traces as sensitive; do not publish them.
- Removal/unloading stops coordinator polling; it does not issue a cloud logout that could affect other clients.
- No mobile-app binaries, vendor MQTT certificates or client private keys are distributed.

## Standalone diagnostic probe

The probe remains useful before installation or when investigating a response shape. It does not share saved credentials with HA.

### What the probe does

1. Prompts for account email and password (password entry is hidden).
2. Signs in through the EU app gateway and lists vehicles authorized for that account.
3. With `--telemetry`, asks which vehicle to inspect if necessary, obtains the vehicle-service session, and reads one existing cloud telemetry snapshot.
4. Prints a reduced report: field names/types and selected telemetry values, without full VINs, location values or account-profile values. It can optionally save the report or a **private** raw capture.

**No control PIN, wake-up, locate, lock, unlock, climate, vehicle binding/default selection, or MQTT connection.** Only four hard-coded API routes are permitted. No arbitrary endpoint execution. No automatic retries, background polling or automatic login refresh.

Signing in can still invalidate your official mobile-app session. This is a third-party API and we cannot guarantee that account/session operations are side-effect-free. Use only an account and vehicle you are authorized to access.

## Run locally

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). From this repository:

```sh
uv sync --locked

# Offline preview: no login, prompts or network calls from the script
uv run python probe.py --plan --telemetry

# First test: account login and vehicle discovery only
uv run python probe.py --country-code 44

# Read battery/range and other available telemetry; save a reduced report
uv run python probe.py --country-code 44 --telemetry --output captures/report-1.json
```

`44` is the UK dialling code. Use the country code associated with your account (e.g. `39` Italy, `49` Germany), without `+`. This changes a country header, **not** the EU API host. Other account countries are not validated.

The script asks for confirmation before making any request, then for email/password. It **does not accept a `--password` flag**, piped passwords, or environment-variable credentials, keeping passwords out of shell history and process arguments. An optional `--email you@example.com` flag avoids the email prompt, but the email then appears in shell history/process arguments.

Passwords and session tokens are used in memory only, never intentionally persisted. Closing the process does not necessarily restore the mobile app's previous session; you may need to sign into the app again. Python cannot guarantee secure zeroization of in-memory strings.

### Optional private raw capture

If the reduced report isn't enough to understand the response shape:

```sh
uv run python probe.py --country-code 44 --telemetry \
  --output captures/report-2.json \
  --raw-output captures/private-2.json
```

Raw captures contain only discovery/telemetry responses, **never login responses**. Credential-like keys and known session tokens are redacted on a best-effort basis. Nevertheless **VINs, GPS coordinates, user identifiers and other private data may remain. Never post raw captures to GitHub or chat.** Review reduced reports before sharing too; schema field names and telemetry can still be identifying in unusual responses.

Use `captures/` for **all** output files; this directory is gitignored. Files are created owner-only (`0600` on Unix), refuse overwrite and do not follow existing file symlinks. Do not put output in public/shared folders. File permissions are not encryption; consider backups and cloud sync. Choose a new filename each run.

## Interpreting results

- `vehicle_count`: vehicle records recognized in the server reply. Zero can mean no bound car, missing permissions, an API rejection or an unexpected response shape—not necessarily that you have no vehicle.
- `observed_fields`: available battery/range/charge/climate/lock fields in their **original source format**. Typical candidates include `dumpEnergy` for battery, and `dynamicPureElectricRange`, `electricRange` or `pureElectricRange` for EV range. We do not assume that every model uses these identically.
- `discovery_schema` / `realtime_schema`: all discovered field names with their types, not their values. Useful for building model-specific parsers. Unknown/free-form telemetry strings are omitted from `observed_fields` for privacy; they remain visible as field types here.
- `discovery_status` / `realtime_status`: short machine status codes when present; free-form server messages are withheld.
- `fetched_at_utc`: when the script fetched the cloud response, **not** when the vehicle measured it.

A parked car may return stale data, placeholder values or no telemetry. Do not treat a zero as an empty battery without checking its meaning. Units and sentinel values have not yet been validated. For a useful comparison, run while the car is already charging or awake, and compare against its display/the app without unnecessarily starting a competing app session. The probe **does not wake it**. Do not enable climate just to make this test pass without understanding the effect.

If login fails, do not repeatedly retry. Confirm you can sign in normally, that the account has a password, and that it belongs to this app/backend. OTP/challenge flows, phone-number login, automatic token refresh and anti-bot workarounds are out of scope. Network errors and HTTP failures are reported without printing server bodies or raw exceptions that could disclose secrets.

Exit codes: `0` completed (or declined), `1` local/API failure, `2` usage error, `3` no recognized vehicle/telemetry payload, `130` cancelled. A successful fetch does not establish data freshness.

## Development

```sh
uv sync --locked --group dev
uv run pytest -q
```

To run the native Home Assistant flow/entity tests (separate Python 3.14 environment):

```sh
cd tests/ha
uv sync --locked
uv run pytest -q
```

The compatibility suite also runs against HA 2026.7.1 (the reference installation):

```sh
# From repository root
HA_TEST_VERSION=2026.7.1 HA_TEST_PLUGIN_VERSION=0.13.345 \
  uv run --no-project --python 3.14 \
  --with pytest-homeassistant-custom-component==0.13.345 \
  pytest -c tests/ha/pyproject.toml -q tests/ha
```

Tests use synthetic credentials and fake responses; real sockets are blocked. No live credentials belong in CI. HTTPS certificate/hostname verification remains enabled and redirects are disabled. Environment proxies and `.netrc` are deliberately ignored for this probe; no TLS-bypass switch is provided.

The HA integration lives under `custom_components/omoda_jaecoo/`, with an async API client independent of HA, native config/reauth/reconfigure flows, one coordinator per account, native sensors and allowlisted diagnostics. The client uses HA's shared HTTP session and core-provided aiohttp/cryptography dependencies. The standalone probe remains separate to avoid a dependency on HA.

Before adding controls: validate model capabilities, PIN/authorization, asynchronous command results, timeout/lockout handling, credential distribution and TLS requirements. Never turn a passive sensor refresh into an implicit climate/wake command.

## Repository boundaries

App archives, decompiled sources, binary dumps, analysis, local investigation notes, captures and credentials are gitignored and **not distributed**. There are no bundled client certificates/private keys. Wire-protocol constants in `probe.py` are app-level constants from the public reference implementation, not personal account credentials.

Protocol work is based on [chery-connect-ha/omoda9-ha](https://github.com/chery-connect-ha/omoda9-ha), inspected at commit `7d80cd6a7215168f58d147cbd475c82e52cd3944`. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and upstream MIT terms.

Not affiliated with Omoda, Jaecoo, Chery or Home Assistant. APIs can change without notice. No manufacturer support or compatibility guarantee is implied.
