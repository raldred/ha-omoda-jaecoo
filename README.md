# ha-omoda-jaecoo

An **unofficial, early-stage** project exploring Home Assistant support for Omoda and Jaecoo vehicles using the European **OMODA JAECOO** app.

**Current state: a standalone, read-only diagnostic script. This is not yet an installable Home Assistant integration.** It has offline tests; compatibility with your account/model still needs live validation. It does not support CarLinko or automatically select other regional backends.

Planned HA scope: EV battery percentage and range, door locks, and climate control. Those controls are **not implemented in this probe**.

## What the probe does

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

Tests use synthetic credentials and fake responses; real sockets are blocked. No live credentials belong in CI. HTTPS certificate/hostname verification remains enabled and redirects are disabled. Environment proxies and `.netrc` are deliberately ignored for this probe; no TLS-bypass switch is provided.

The future HA integration would live under `custom_components/omoda_jaecoo/`. Before adding controls: validate model capabilities, PIN/authorization, asynchronous command results, timeout/lockout handling, credential distribution and TLS requirements. Never turn a passive sensor refresh into an implicit climate/wake command.

## Repository boundaries

App archives, decompiled sources, binary dumps, analysis, local investigation notes, captures and credentials are gitignored and **not distributed**. There are no bundled client certificates/private keys. Wire-protocol constants in `probe.py` are app-level constants from the public reference implementation, not personal account credentials.

Protocol work is based on [chery-connect-ha/omoda9-ha](https://github.com/chery-connect-ha/omoda9-ha), inspected at commit `7d80cd6a7215168f58d147cbd475c82e52cd3944`. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and upstream MIT terms.

Not affiliated with Omoda, Jaecoo, Chery or Home Assistant. APIs can change without notice. No manufacturer support or compatibility guarantee is implied.
