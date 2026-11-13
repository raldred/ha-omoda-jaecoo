# Scheduled conditioning blueprint

`blueprints/automation/omoda_jaecoo/scheduled_conditioning.yaml` is a generic Home
Assistant automation blueprint for requesting cabin conditioning and/or
rear-window defrost at a time you choose, with optional cold, presence, battery
and manual-enable gates.

It is **experimental and opt-in**. Nothing is scheduled by installing the
integration or copying the blueprint: a schedule exists only after you create an
automation from it, and you should leave that automation switched off until you
have tested every action by hand.

> **Safety.** Remote conditioning physically operates the vehicle. On fuel and
> hybrid models it may start the engine, so never schedule it while the car is
> parked in a garage, carport or any other enclosed space. Test only while the
> car is safely parked outdoors and you can see it.

> **Honesty.** The cloud accepting a request **does not prove** the car acted.
> The 2026-11-12 incident on the reference vehicle (accepted request, no
> observed action) is still unexplained. The blueprint never claims success and
> never repeats a request, and an ambiguous or failed request is
> **never retried** automatically. Check the car and the integration's
> *Command status* sensor.

## Installation

The blueprint is **not** installed automatically.

- **HACS does not install blueprints** for an integration repository. HACS
  downloads `custom_components/omoda_jaecoo/` only.
- The blueprint is **not included in the release ZIP** either: `omoda_jaecoo.zip`
  is a deterministic, component-only archive, and a packaging test asserts that
  no blueprint is ever added to it.

So install it yourself, in one of two ways.

### Import from GitHub (once the repository is public)

1. Go to **Settings → Automations & scenes → Blueprints → Import blueprint**.
2. Paste:

   ```text
   https://github.com/raldred/ha-omoda-jaecoo/blob/main/blueprints/automation/omoda_jaecoo/scheduled_conditioning.yaml
   ```

3. Select **Preview blueprint**, then **Import blueprint**.

The repository is currently **private**, so this URL returns a 404 for Home
Assistant and the import will fail until the repository (or at least that file)
is published. Use the manual copy below in the meantime.

### Copy the file manually

1. Copy `blueprints/automation/omoda_jaecoo/scheduled_conditioning.yaml` from the
   repository into your configuration folder, keeping the same sub-path:

   ```text
   /config/blueprints/automation/omoda_jaecoo/scheduled_conditioning.yaml
   ```

2. Reload automations (**Developer tools → YAML → Automations**) or restart Home
   Assistant Core.
3. The blueprint appears as *Omoda / Jaecoo scheduled conditioning* under
   **Settings → Automations & scenes → Blueprints**.

Updating is the same copy again; blueprints are not version-managed by HACS.

## Prerequisites

- A control PIN saved through **Reconfigure**, and **Experimental remote
  controls** enabled in the integration options. Without both, the vehicle
  `climate` entity does not exist and there is nothing to schedule.
- A conditioning run duration in the integration options that your car actually
  reports as allowed.
- For the rear-window option: a vehicle that **reports** a rear-window heater
  state, which is the only evidence this integration accepts that the hardware
  exists. If the switch is not in your device's entity list, your car does not
  report it and this option must stay empty.

## Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| Start time | yes | — | The time conditioning **starts**, not your departure time. Subtract your own lead time: for a 07:00 departure with 20 minutes of warm-up, enter 06:40. Home Assistant time triggers cannot do that arithmetic for you. |
| Days | no | all seven days | Days the schedule may run. |
| Vehicle climate entity | yes | — | The Omoda / Jaecoo `climate` entity for the car to condition. |
| Cabin conditioning | no | Rapid heating | *Rapid heating* uses the vehicle's one-touch heating request; *Climate entity* sets the target temperature and turns the entity on; *None* gives a defrost-only schedule. |
| Target temperature | no | 21 | Used by both cabin options. Values outside the range and step your vehicle reports are refused without a command being sent. |
| Run duration | no | 0 | Minutes, applied to the **rapid heating** request only. `0` means "use the duration configured in the integration options", and no duration field is sent at all. Only durations your car reports as allowed are accepted. The rear-window switch takes no duration of its own and always uses the configured option. |
| Rear-window defrost switch | no | empty | Optional, independent request. Skipped when the car already reports it on. |
| Cold or frost gate sensor | no | empty | Optional outdoor-air or weather temperature entity. |
| Cold or frost threshold | no | 3 | Run only below this reading, in that sensor's unit. |
| Presence or location gate | no | empty | Optional person/tracker/helper that must match the state below. |
| Required presence state | no | `home` | Exact state match, for example `home` or `on`. |
| Minimum battery sensor | no | empty | Optional battery percentage sensor. |
| Minimum battery level | no | 30 | Skip below this percentage. |
| Manual enable helper | no | empty | Optional toggle that must be on, so you can skip tomorrow with one tap. |

Nothing personal is baked in: no entity IDs, no time of day, no weekday pattern,
no threshold and no zone. Every optional entity input defaults to empty.

### How the optional gates behave

Every gate **fails closed**. If a gate you selected is missing, `unknown`,
`unavailable`, empty or non-numeric, no command is sent that day:

- **Cold gate** — uses the `temperature` attribute for a `weather.*` entity and
  the state for a sensor. A reading that cannot be parsed as a number blocks the
  run, and a threshold that cannot be parsed blocks it too. Choose an outdoor or
  forecast temperature: **do not choose the car's cabin temperature** sensor. A
  warm cabin says nothing about frost on the glass, and a cold cabin is not a
  forecast. Even a correct outdoor reading cannot predict frost — glass can
  frost while the air is a few degrees above zero — so treat the threshold as a
  starting point you tune.
- **Presence gate** — an exact state match, so `unavailable` or a renamed state
  skips the run. Do not rely on it for security.
- **Battery gate** — a missing or unparsable percentage skips the run. Readings
  can be stale while the car is asleep.
- **Manual enable helper** — anything other than `on`, including `unavailable`,
  skips the run.

### Duplicate and serialized requests

- The cabin request is skipped when the car already **reports** conditioning as
  running. An `unknown` report does not block a deliberate request.
- The rear-window request is skipped when the switch already reports `on`.
- The integration allows one command at a time per account and at least
  **30 seconds** between commands, so when both controls are selected the
  rear-window request is delayed behind the cabin request instead of being
  refused. With only one control selected there is no delay.
- Rapid heating as sent by this integration asks for cabin heating only. The
  heating route does accept rear-window, seat and steering-wheel switches, but
  this integration never sends them, so the two requests it submits never ask
  for the same thing twice.
- `mode: single` with `max_exceeded: silent` means a second trigger while a run
  is still in progress is dropped, not queued.

## Sample configuration

Replace the placeholder entity IDs with your own. These are examples, not
recommended values.

A frost-gated weekday warm-up, cabin plus rear window, 25 minutes before leaving:

```yaml
automation:
  - alias: Vehicle warm-up before the commute
    use_blueprint:
      path: omoda_jaecoo/scheduled_conditioning.yaml
      input:
        start_time: "06:35:00"
        days: [mon, tue, wed, thu, fri]
        climate_entity: climate.YOUR_VEHICLE
        conditioning_mode: rapid_heat
        target_temperature: 21
        run_duration: 15
        defrost_entity: switch.YOUR_VEHICLE_rear_window_defrost
        temperature_entity: sensor.YOUR_OUTDOOR_TEMPERATURE
        temperature_threshold: 2
        presence_entity: person.YOUR_PERSON
        presence_state: home
        battery_entity: sensor.YOUR_VEHICLE_battery
        battery_minimum: 35
        enable_entity: input_boolean.YOUR_PRECONDITION_TOGGLE
```

A defrost-only weekend schedule with no gates at all:

```yaml
automation:
  - alias: Clear the rear window on Saturday
    use_blueprint:
      path: omoda_jaecoo/scheduled_conditioning.yaml
      input:
        start_time: "08:15:00"
        days: [sat]
        climate_entity: climate.YOUR_VEHICLE
        conditioning_mode: none
        defrost_entity: switch.YOUR_VEHICLE_rear_window_defrost
```

Create one automation per schedule. Several automations can use the blueprint at
the same time, for different vehicles, times, days or actions; they are
independent, and the integration serializes whatever they submit per account.

A new automation is created enabled, so switch it off in the automation's
overflow menu until you have finished testing, and keep the enable-helper input
in mind as the one-tap way to skip a morning.

## Limitations

- No frost automation, schedule or helper is created for you, and nothing is
  enabled by default.
- There is no lead-time arithmetic: you choose the start time yourself.
- The blueprint cannot verify execution. It reads reported state to avoid
  pointless duplicate requests, but reported state can be stale, and acceptance
  is not completion.
- Conditioning requests are refused by the integration when controls are
  disabled, the PIN is blocked, cloud permission is denied or unknown, or the
  requested temperature/duration is not one your vehicle reports.
- The rear-window option only exists for cars that report that state; it is
  deliberately absent otherwise rather than guessed.

See also [docs/protocol-evidence-conditioning.md](protocol-evidence-conditioning.md)
for what the official app actually establishes about these two requests.
