# ha-omoda-jaecoo

An **unofficial, early-stage** project exploring Home Assistant support for Omoda and Jaecoo vehicles using the European **OMODA JAECOO** app.

**Current state: a native Home Assistant custom integration with passive telemetry, experimental opt-in lock/climate controls, and a read-only diagnostic script.** Tested offline against HA **2026.7.1 and 2026.9.4**. Account-password login and native battery/range/odometer sensors have been validated on one user's vehicle. **Physical commands have not yet been tested on that vehicle.** This is not an official HA core integration or a guarantee of compatibility with every model.

It supports accounts on the EU OMODA JAECOO backend, not CarLinko or automatic regional routing. Controls are **disabled by default**. The local integration icon uses the manufacturer-owned Android app artwork; see the licensing note below.

## Home Assistant installation and setup

Requires **Home Assistant 2026.7.1 or newer** for the current native config-flow implementation. No vehicle commands are issued during setup, startup, polling or reauthentication.

### Install manually (private repository)

1. Clone/download this repository using your GitHub account with access.
2. Copy **only** `custom_components/omoda_jaecoo/` into HA's configuration directory, resulting in `/config/custom_components/omoda_jaecoo/manifest.json`. Keep the folder name exactly `omoda_jaecoo`.
3. Restart Home Assistant at a suitable time. Installation/restart is a deliberate user action, not something the probe performs.
4. Go to **Settings → Devices & services → Add integration → Omoda / Jaecoo**.
5. Choose **Email address or Phone number**, then **Account password or One-time code**. All four combinations have native setup paths.
6. Enter the registered identifier and country dialling code. For password sign-in, enter the masked account password. For OTP, explicitly confirm **Send a sign-in code**, then enter the masked email/SMS code. No message is sent just by opening a form.
7. If multiple vehicles are discovered, select the ones to add. A single vehicle is selected automatically.
8. Optionally enter the **separate vehicle control PIN**, or leave it blank. The PIN is **not verified during setup**. It is used only for explicit commands after you enable controls. No PIN request or physical command is sent simply by setting up or enabling the integration.

HACS metadata is included for future distribution, but this repo is currently private; do not assume ordinary public HACS installation will work. Manual installation is the documented path. Do not overwrite an existing unrelated integration using the same `omoda_jaecoo` domain—review/remove any conflict first.

### Four sign-in combinations

| Identifier | Authentication | Delivery |
|---|---|---|
| Email | Password | No code requested |
| Phone | Password | No code requested |
| Email | One-time code | Email, explicitly requested |
| Phone | One-time code | SMS, explicitly requested |

Use the identifier **already registered in the official app**, not a new account address/number. Phone inputs accept national formatting and +/00 international formatting; the selected country must match. A phone-number library removes national trunk prefixes correctly and preserves significant zeros (for example Italy). This validates the format, not ownership/assignment; the server still authenticates the account.

OTP delivery uses the app gateway's captcha create/check flow, then a single email/SMS request. Challenge images are processed off the HA event loop with strict size/work limits; no screenshots, codes or challenge secrets are persisted. Failed/ambiguous challenges do not trigger automatic retry loops. In particular, some SMS endpoints may refuse automated TLS clients: the flow reports this rather than weakening TLS or cycling clients to get around a refusal. Phone/password is an alternative that does not request SMS.

You must explicitly request or resend a code. Resends wait at least **60 seconds** and honor a longer server `Retry-After`; authentication/discovery rate limits are also respected. After **three rejected code attempts**, request a fresh code instead of guessing. If a send response is ambiguous, the code-entry step remains available in case the message arrives. A discovery failure after authentication offers a read-only retry using the minted session—not another login or code submission.

Your identifier and chosen method are saved for fixed-account reauthentication. **Passwords, OTPs and captcha material are not saved.** Normal startup/polling uses access/refresh tokens and never sends an OTP automatically. Old email/password entries retain their original IDs and continue working without migration or re-entry.

Email/password login has been validated live on the reference vehicle. **The three new routes and captcha handling are protocol-backed and offline-tested, not yet live-verified on that account.** No real codes were sent during development. Only one setup should own a vehicle; logging in through another identifier for the same backend account may invalidate an existing app/HA session, and duplicate VINs are blocked before creating another entry.

### Native entities

Each selected vehicle gets a device with:

| Sensor | Native data | Default display |
|---|---|---|
| Battery | `dumpEnergy`, percent, normalized to one decimal place | %, one decimal |
| Electric range | `dynamicPureElectricRange` (fallback `electricRange`/`pureElectricRange`), km | Miles |
| Odometer | `odometer`, km | Miles |
| Cabin temperature | `inCarTemperature` | HA temperature unit preference |
| Vehicle report time | Explicit-zone/epoch source timestamp, if understood | Timestamp or unknown |
| Cloud last checked | Time HA fetched the cloud snapshot | Timestamp |
| Telemetry freshness | Source timestamp age / snapshot status | Current, stale, unknown, no snapshot or unreliable |

**Miles are the initial default, even on a metric HA installation.** Choose kilometres in an entity's standard settings if preferred. HA handles conversion; the integration always stores distance measurements in kilometres and does not override your later preference.

`rangeUnit` is not used to reinterpret the raw kilometre fields: the probe confirmed 262 km → 163 mi on the reference vehicle. Other models/field fallbacks need validation. Negative, nonfinite and out-of-range battery values become unknown, not zero. Genuine zero is retained unless accompanied by the known invalid HV-frame pattern (zero voltage and -1000 current with zero battery/range). Those degraded snapshots keep previous readings, if any, and are labelled unreliable. This first version does not yet identify every possible vendor sleep sentinel.

Cloud reads default to **every 5 minutes**, configurable from 5–60 minutes under the integration's options. HTTP rate limiting backs polling off up to an hour. This is a conservative implementation choice, not a published vendor allowance. No wake/locate command, MQTT connection or automatic climate activation is involved. Empty/asleep replies retain previous measurements in memory but show **No snapshot returned**. Network failures make entities unavailable. A successful cloud read can still contain an old snapshot.

A report time without a timezone is **not guessed**. Unrecognized/missing source timestamps produce **Observation time unknown**. Source timestamps older than 15 minutes are labelled stale. This label is based on the timestamp returned by the service, not independent verification of individual sensors. **Cloud last checked is never presented as the vehicle's observation time.**

### Door/climate state and automation triggers

Read-only binary sensors expose the reported lock state, climate running state, each of the four doors and the boot. They use ordinary HA state triggers in automations; no custom event or helper is needed. For the lock-status binary sensor, **on means unlocked**, following HA's LOCK device class. Missing/invalid fields remain unknown rather than off/closed. A cached cloud state is not proof of the car's current physical state.

### Experimental remote controls (explicit opt-in)

1. Save the **correct control PIN for this account** using Reconfigure if not already present.
2. Open the integration's options and enable **Experimental remote controls**. This adds a native `lock` entity, and a native `climate` entity only when temperature limits, step and allowed durations are known.
3. Choose the climate run duration in options (default 15 minutes). It must be one of the durations returned by your vehicle. Unknown capabilities or permissions fail closed.
4. Perform the first tests while safely parked and physically able to verify the result. On hybrid models, preconditioning may run the engine: use a safe, ventilated location. **Do not build automatic unlocking rules before those supervised tests.**

Native actions:

- `lock.lock` / `lock.unlock` request locking/unlocking.
- `climate.turn_on` / `climate.turn_off`, or OFF / HEAT_COOL mode, request cabin conditioning. HEAT_COOL is HA's model for the car choosing heating/cooling to achieve the target—not a separate vendor heat/cool command.
- Changing temperature while off or unknown only remembers a local preference; explicitly turn on to operate the car. If telemetry reports climate on, changing temperature submits one command. Combined `hvac_mode` + temperature requests are rejected; use separate mode and temperature actions. The selected/requested temperature is separate from any last-reported cloud setpoint.

Controls check the discovered vehicle and reported account permissions, select it, verify the PIN once, and submit one signed request. No wake/locate fallback, MQTT certificate, background command or automatic write retry is used. The explicit lock/climate command may itself wake/operate the vehicle.

**Accepted is not completed.** The Command status sensor and control attributes distinguish submitting, accepted-but-unconfirmed, requested lock state reported, no lock-state confirmation, rejected, unknown outcome, and PIN safety blocking. Native lock/HVAC state comes from cloud telemetry and is **never switched optimistically to the requested final state**. This REST-only implementation does not receive terminal MQTT acknowledgements; a reported state is not a sequence-correlated command acknowledgement.

**Responsive feedback:** the lock entity shows native **Locking / Unlocking** immediately while the request is in progress. After a lock or climate request is accepted, a background task performs at most **five extra passive status refreshes over up to 60 seconds**, rather than waiting for the normal five-minute poll. It never repeats the physical command, PIN check or wake operation. Refreshes cover the selected vehicles through the existing serialized coordinator. They stop on read errors/rate limits, unload or supersession; the normal polling interval is unchanged.

Lock checks also stop early when the requested state is reported with a source time at/after the request, or a change from a known prior state when no usable source time is available. An unchanged cached target, stale timestamp or unknown prior state is not enough. HA then shows **Locked / Unlocked** from that report. If the budget expires without a credible report, pending feedback ends and the lock is **unknown** (or unavailable after a read failure), with **Lock state not confirmed — check vehicle** in Command status. A credible report of the opposite state remains visible instead of pretending the command succeeded. Later normal polling can recover the state. Climate receives the same bounded extra reads but does not claim terminal command confirmation.

Only one command can run per account, with at least 30 seconds between attempts. A timeout/ambiguous response is **not automatically retried** because the car may already have acted. Check it before trying again. Failed or inconclusive PIN verification (including cancellation mid-check) persistently pauses controls until you verify and explicitly re-enter the PIN in Reconfigure. Never try candidate PINs. A failed preparation/cancelled preparation sends no physical command.

A dedicated delegated account may reduce conflicts with the official app, but its permissions and simultaneous-session behaviour must be validated. Our discovery supports authorized vehicles; the controls require explicit server-reported authority. To switch accounts currently, remove the old integration entry and add the delegated one; duplicate VINs across account entries are intentionally blocked.

### Credentials and maintenance

- The account password or OTP is used once and **never saved** in the config entry. Access/refresh tokens are saved for automatic refresh, including rotated tokens. If the session is revoked, HA offers native reauthentication for the same account and saved method. OTP reauthentication still requires an explicit request to send the code.
- The control PIN is optional for telemetry. Use **Reconfigure** to replace/remove it; blank keeps the existing value and any safety block. Explicitly entering a PIN clears the local safety block, so check it in the app before doing so. Verification happens only on a user-requested command, never startup or polling.
- HA config-entry storage/backups are **not automatically encrypted**. Protect saved tokens, optional PIN, account email and vehicle identifiers. Diagnostics expose only an allowlisted structural summary, not these values or raw telemetry/GPS.
- Logging into the official app can invalidate the HA session and vice versa. The integration never saves your password for repeated automatic login attempts.
- The private API uses query parameters for refresh tokens, email OTP exchange and captcha verification (matching the upstream working protocol), over verified HTTPS. Phone OTP and password grants use form bodies. Query values can be credential-equivalent even when encoded/encrypted. Integration exceptions do not expose URLs, and redirects are disabled. Treat HTTP debug/proxy traces as sensitive; do not publish them.
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

If login fails, do not repeatedly retry. Confirm you can sign in normally, that the account has a password, and that it belongs to this app/backend. For the **standalone probe**, OTP/challenge flows, phone-number login, automatic token refresh and anti-bot workarounds are out of scope; use the native HA flow for the additional sign-in methods. Network errors and HTTP failures are reported without printing server bodies or raw exceptions that could disclose secrets.

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
  --with phonenumbers==9.0.40 \
  pytest -c tests/ha/pyproject.toml -q tests/ha
```

Tests use synthetic credentials and fake responses; real sockets are blocked. No live credentials belong in CI. HTTPS certificate/hostname verification remains enabled and redirects are disabled. Environment proxies and `.netrc` are deliberately ignored for this probe; no TLS-bypass switch is provided.

The HA integration lives under `custom_components/omoda_jaecoo/`, with an async API client independent of HA, native config/reauth/reconfigure flows, one coordinator per account, native sensors and allowlisted diagnostics. The client uses HA's shared HTTP session and core-provided aiohttp/cryptography/Pillow dependencies. HA installs the declared `phonenumbers` requirement for country-aware phone formatting. No NumPy or browser automation is needed for OTP delivery. The standalone probe remains separate to avoid a dependency on HA.

Before treating controls as production-ready, perform supervised model-specific PIN/permission, lock/unlock, climate and session tests. Terminal command acknowledgement remains unimplemented without the vendor MQTT channel. Never turn passive sensor refresh into an implicit climate/wake command.

## Repository boundaries

App archives, decompiled sources, binary dumps, analysis, local investigation notes, captures and credentials are gitignored and **not distributed**. The only explicitly requested app-artwork exception is the pair of local HA brand icons: vendor-owned imagery, **not MIT-licensed**. The source icon is 192px, upscaled to HA's 256/512px sizes. Keep the repository private pending permission review for public redistribution; this does not imply manufacturer affiliation. There are no bundled client certificates/private keys. Wire-protocol constants are app-level constants from the public reference implementation, not personal account credentials.

Protocol work is based on [chery-connect-ha/omoda9-ha](https://github.com/chery-connect-ha/omoda9-ha), inspected at commit `7d80cd6a7215168f58d147cbd475c82e52cd3944`. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and upstream MIT terms.

Not affiliated with Omoda, Jaecoo, Chery or Home Assistant. APIs can change without notice. No manufacturer support or compatibility guarantee is implied.
