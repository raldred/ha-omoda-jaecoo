# Protocol evidence: rapid heating and rear-window defrost

A sanitized summary of what the **locally held official EU app** establishes
about the two conditioning requests added in 0.7.0, and — just as importantly —
what it does not establish. The app archives, decompiled artefacts, captures and
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
it, so the rear window is only ever requested through its own route. What a
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
  the exact value encoding for `backDefrosting` is an inference from that
  convention, not an independently proven mapping. A wrong value produces a
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

## Permissions: no dedicated id established

No dedicated permission id for `heatingControl` or `backDefrostingControl` could
be established, and none was invented. Both routes therefore require the same
authority this integration already enforces for air conditioning:

- category **204** must be reported as allowed, and
- core child **2041** must not be reported as denied.

Unknown authority fails closed: nothing is sent and no PIN check is performed.
This is a deliberately conservative proxy, documented as such. It is possible
that a vehicle or account permits air conditioning but not one-touch heating (or
the reverse); that case would surface as a cloud rejection, not as a wrong local
decision to send.

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

- Nothing here has been validated against a vehicle by this work: no command was
  sent. The first real proof will be a supervised manual test by the maintainer.
- There is still no REST result endpoint correlated to a request. Completion
  arrives over MQTT, which this integration does not consume, so a submitted
  conditioning request can only ever be *accepted, unconfirmed*, followed by
  bounded passive reads.
- The 2026-11-12 incident on the reference vehicle — a cloud-accepted climate
  request with no observed vehicle action — remains unexplained. It is the
  reason acceptance is never presented as success anywhere in this release.
