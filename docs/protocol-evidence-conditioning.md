# Protocol evidence: rapid heating and rear-window defrost

A sanitized summary of the conditioning routes introduced in the 0.7.0 preview
and included in stable 0.7.1, with the additional rear-window permission checks
and explicit climate-coupled action. The app archives, decompiled artefacts, captures and
working analysis stay private and are not part of this repository's
distribution; nothing below reproduces app source, resources, keys, credentials,
a VIN or an account identifier.

Evidence type: symbol and resource metadata read from the official EU iOS
application binary (its vehicle SDK request classes and endpoint format
strings), cross-checked against the Android build's string table. "Established"
below means *present in the official app*, not *proven against a car*. No
request was sent to any vehicle or backend while gathering this.

## Routes

The official app has separate endpoints for these two functions:

| Function | Route |
|---|---|
| One-touch ("rapid") heating | `/asc/vehicleControl/heatingControl` |
| Rear-window defrost | `/asc/vehicleControl/backDefrostingControl` |
| Ordinary air conditioning (already shipped) | `/asc/vehicleControl/airControl` |

They are separately addressable requests with separate field sets, and both sit
on the same TSP host, signing and `taskId` flow as the existing lock/climate
commands. They are not mutually exclusive: the heating request itself declares a
`backDefrosting` switch (see below), so a one-touch heating request *can* include
rear-window heating when that field is supplied. This integration never supplies
it. The normal rear switch uses its own route; the explicit coupled action
below instead supplies `backDefrosting` to `airControl`. What a
backend does with the omitted switches — ignore them, apply a default, or reject
the request — is not established.

## Request fields

The official heating request declares these fields, all as strings:
`airControlType`, `airType`, `temperature`, **`duration`**, plus individual
switches for the steering wheel (`steerWheelHeatSwitch`), the rear window
(`backDefrosting`), the windscreen (`frontWindshieldHeat`) and four seats
(`mSeatHeating`, `pSeatHeating`, `blSeatHeating`, `brSeatHeating`).

The official rear-window request declares exactly two string fields:
`backDefrosting` and **`times`**.

Consequences for this integration:

- Heating uses **`duration`**; air conditioning and the rear window use
  **`times`**. The field names are not interchangeable, and the upstream
  community integration's `heatingControl` handling agrees on `duration`.
  The official app is what this implementation follows.
- `omoda_jaecoo.rapid_heat` sends `airControlType`, `airType`, `temperature` and
  `duration` only. The seat, steering-wheel and window switches are deliberately
  omitted: this integration must not actuate hardware it cannot confirm is
  fitted, nor clear a switch the driver set in the car. They are omitted rather
  than sent as `"0"`, so the guarantee is only that this integration never *asks*
  for seat, steering-wheel or window heating — not that a heating request leaves
  them untouched. A backend that insists on them answers with a documented
  parameter rejection, which is surfaced rather than retried with a different
  encoding.
- The rear-window switch sends `backDefrosting` and `times` only, and sends **no
  temperature** — the route has no such field, so none is invented.
- `"1"`/`"0"` string values follow the `airControlType` convention proven by the
  already-shipped air-conditioning command. The field *types* are established;
  the exact value encoding for `backDefrosting` was originally an inference from
  that convention and is now corroborated by the upstream command catalog below,
  but is not proven against the reference vehicle. A wrong value produces a
  documented rejection (below) rather than a silent action.

## Parameter rejection codes

The app's own error table gives definitive, non-retryable meanings for the codes
these routes can return, which is why they are classified as rejections rather
than ambiguous outcomes:

| Codes | Meaning (paraphrased) |
|---|---|
| `A00606`, `A00613`, `A00769`, `A00772` | Duration empty or invalid |
| `A00615`, `A00770` | Air-conditioning type empty or invalid |
| `A00639`, `A00640` | One-touch heating switch empty or invalid |
| `A00616`, `A00617` | Windscreen / rear-window defrost switch invalid |
| `A00618`–`A00621` | Individual seat-heating switch invalid |
| `A00637`, `A00638` | Steering-wheel heating switch empty or invalid |

Acceptance is still only `000000` or `A00079`, and acceptance is not completion.
Anything else — unknown code, malformed body, transport failure, cancellation —
remains an ambiguous outcome that is reported and **never** automatically
retried.

## Rear-window permissions and two routes

Additional community protocol evidence was read through GitHub's contents API,
not through a car or account API. Repository `chery-connect-ha/omoda9-ha` has
default branch `master`. The inspected revision is
`a43ece50e7aa6a814af08c5d8a2f0c21107526be`:

- [core/commands.py](https://github.com/chery-connect-ha/omoda9-ha/blob/a43ece50e7aa6a814af08c5d8a2f0c21107526be/custom_components/omoda9/core/commands.py)
  defines dedicated ON with `backDefrosting="1", times="15"` and OFF with
  `backDefrosting="0"`. We retain our app-declared `times` field on both states.
- [core/permessi.py](https://github.com/chery-connect-ha/omoda9-ha/blob/a43ece50e7aa6a814af08c5d8a2f0c21107526be/custom_components/omoda9/core/permessi.py)
  maps `backDefrostingControl` to category **232**, its rear-window child to
  **2321**, and the alternative `airControl.backDefrosting` to **20411** under
  category **204**, with climate core **2041**.
- `instrada()` constructs a coupled air request with `airControlType`, `airType`,
  `temperature`, `times` and `backDefrosting`. It explicitly warns that turning
  the accessory OFF also turns climate OFF. This is community implementation
  evidence, not independent proof of rear-window behavior on the tested E5.

This integration does **not** copy upstream's fail-open permission rules,
hardcoded 21 C/15-minute fallback, or automatic rerouting. Every rear-window
request gets fresh authority before discovery, PIN checking or physical submission:

| User action | Required positive permissions | Payload route |
|---|---|---|
| Existing rear switch ON/OFF | 232 and 2321 | `backDefrostingControl` |
| Explicit coupled action ON/OFF | 204, 2041 and 20411 | `airControl` |

Missing, malformed, conflicting or denied required permissions block the request.
A dedicated rejection, including **A00084**, is final. It never triggers another
route, even if the alternative permissions would allow it. `A00084` means
"No vehicle control command permission", not an invalid rear-window parameter.
On the tested E5, cloud authority denies rear-window remote access despite
fitted hardware. The diagnostic comparison found no local/upstream parser
mismatch. Neither rear-window route has been physically validated; independent
rear defrost in the car does not prove remote permission.

### Explicit alternate action

`omoda_jaecoo.rear_defrost_with_climate` targets the existing rear-window switch.
It requires `enable`, `temperature_celsius`, `duration` and
`acknowledge_climate_change: true`. The temperature is always Celsius, even in a
Fahrenheit HA installation. The caller must supply a supported temperature/step
and duration; neither is guessed from stale telemetry or silently clamped.

ON sends `airControlType="1", backDefrosting="1"`; OFF sends both as `"0"`.
Both include `airType="1"`, the supplied temperature and `times`. ON starts or
reconfigures cabin heating and resets its run timer. **OFF stops cabin climate,
including heating.** The action description and acknowledgement disclose this.
Use the existing dedicated switch for independent OFF if authorized; otherwise
there is no established safe remote encoding for rear OFF while retaining cabin
heating. We do not invent `airControlType="1", backDefrosting="0"` semantics.

The dedicated switch sends no cabin setpoint or cabin on/off fields and never
sends an air-control command, irrespective of reported cabin state. Its `times`
field is the requested rear-window duration. The explicit
coupled action changes those settings only as requested. Neither path updates
reported rear/climate state optimistically. Both share opt-in, PIN blocking,
account serialization, cooldown and accepted-only feedback. No wake is added.

Rapid heating has been confirmed working on the tested E5. Its existing
204/core-2041 proxy remains unchanged.
Upstream maps `heatingControl` separately to 209/2093, which is evidence for a
separate follow-up rather than a reason to expand this rear-window change.

### Read-only permission diagnostic

Administrators can call `omoda_jaecoo.diagnose_rear_defrost_permissions` with an
explicit `config_entry_id` for a loaded account. It reads only cloud authority
using a current, confirmed cached session. It never logs in, refreshes
credentials, checks a PIN or sends an actuator or wake command. A missing or
expired session fails without authentication.

The response and `REAR_DEFROST_PERMISSION_SNAPSHOT` log entry compare local and
upstream interpretation of the five permission IDs above, without vehicle or
credential identifiers or raw responses. Missing and unknown differ from
denied, and none grants permission to act.

## Capability and bounds metadata

- Allowed run durations come from discovery's `maxAirDuration`, which is a
  **comma-separated set of allowed minutes, not an upper bound**. Membership is
  required; a request is never clamped onto a nearby allowed value. Discovery
  publishes no heating- or defrost-specific duration metadata, so the same
  reported set gates all three conditioning routes and a missing set disables
  them.
- Temperature bounds and step come from the vehicle's own discovery entry. The
  reference E5 reports **16–30 °C**; upstream's hardcoded 31 °C fallback is not
  copied, and no bound is widened for the heating route. Whether the official
  app restricts one-touch heating to a narrower range than air conditioning is
  **not established**.
- Discovery publishes **no rear-window capability flag** (`isHaveLoAndHi` and the
  temperature fields are the only climate-related capability metadata). The only
  available evidence that a rear-window heater exists is the car's own reported
  `rWinHeatingState`, so the switch is created only for vehicles that report a
  parsed on/off value. An absent or unparsed reading is never treated as
  capability, and never displayed as "off".

## Remaining limits

- Rapid heating works on the tested E5. Both rear-window routes remain
  permission-gated and vehicle-dependent, without physical validation.
- There is still no REST result endpoint correlated to a request. Completion
  arrives over MQTT, which this integration does not consume, so a submitted
  conditioning request can only ever be *accepted, unconfirmed*, followed by
  bounded passive reads.
- Cloud acceptance alone does not prove vehicle action. Check the vehicle rather
  than treating an accepted request as completed.
