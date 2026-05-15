# Architecture Decision Records

> AI-assisted development: these records reflect real design discussions
> and decisions made during development.

---

## ADR-001 — Fan as PID output, heater as binary

**Decision:** The fan is the continuous PID output. The heater is binary
(on/off with hysteresis), independent of the PID.

**Rationale:** A PID-controlled heater with PWM or relay switching would
require careful duty-cycle management and relay lifetime considerations.
A binary heater with hysteresis is simpler, safer, and sufficient — the
PID fan does the fine regulation. Together they form a stable control loop:
heater provides energy, fan dissipates excess and circulates air.

**Consequence:** Heater and fan are regulated independently but react to
the same temperature sensor. No coupling logic needed.

---

## ADR-002 — Humidity sensor logged only, not controlled

**Decision:** Chamber humidity is read and logged to DB but not used as a
control variable.

**Rationale:** In a drying chamber, humidity falls naturally as temperature
rises (warmer air holds more moisture) and as moisture leaves the filament.
Active humidity control would conflict with temperature control. Logging
humidity provides useful data to track drying progress without adding
control complexity.

**Consequence:** No dehumidifier logic. Humidity trend in DB shows when
filament is approaching target dryness.

---

## ADR-003 — cfg.yaml loaded once at startup

**Decision:** Configuration is read once in `initialize()`. No runtime reload.

**Rationale:** Entity IDs and DB credentials don't change at runtime.
A full AppDaemon restart is acceptable for config changes.
Dynamic reload adds complexity without meaningful benefit.

**Consequence:** cfg.yaml errors are fatal at startup (intentional).

---

## ADR-004 — Cascade zones based on absolute temperature, not offset from target

**Decision:** Cascade zone thresholds are absolute °C values (e.g. cold < 35°C,
green 35–75°C), not relative to the target setpoint.

**Rationale:** The drying temperature range is bounded by material constraints,
not by user preference. Nylon at 75°C is in green zone regardless of whether
target is 70°C or 75°C. Absolute thresholds make the safety boundaries
hardware-based rather than setpoint-dependent.

**Consequence:** If target_temp is set near a zone boundary, the PID operates
at the edge. Document clearly in README. Zone constants can be adjusted at the
top of `filament_dryer.py`.

---

## ADR-005 — Tri-state safe_is_on() for all switch reads

**Decision:** All switch state reads use `safe_is_on()` which returns
True/False/None. Code paths must handle the None case explicitly.

**Rationale:** "Unavailable" must never be silently treated as "off".
Inherited from ha-climate-controller where this pattern was introduced after
several bugs caused by `== "on"` comparisons on unavailable entities.

**Policy:** On unclear state, skip the action and log a WARNING.
The next sensor update will trigger a retry.

---

## ADR-006 — Sensor safe mode on temp sensor (not on a secondary sensor)

**Decision:** Safe mode (fan 50%, heater off, HA notification) triggers
when the temperature sensor fails 3 consecutive times.

**Rationale:** Temperature is the only safety-critical sensor in this
controller. Without it, neither heater nor fan can be safely controlled.
50% fan is a conservative middle ground — keeps airflow, won't overheat
from fan heat alone, won't let an already-hot chamber cook uncontrolled.
Heater is forced off on sensor failure (fail-safe).

---

## ADR-007 — PID integral freeze on soft override entry

**Decision:** When the soft temperature override zone is entered, the PID
is reset once. `_temp_override_active` flag prevents repeated resets.

**Rationale:** Inherited from ha-climate-controller (ADR-007 there).
During override, the fan is pushed above PID request. The PID sees "still
too hot" and accumulates integral. After override ends, this causes
significant overshoot. One reset on entry prevents accumulation.

**Consequence:** After a heat event, PID restarts from zero integral.
Settling takes a few cycles — preferable to overshoot.

---

## ADR-008 — DB schema backward compatibility via errno 1054

**Decision:** When cascade columns are missing, the app catches errno 1054
and falls back to the base INSERT schema. WARNING logged, app continues.

**Rationale:** Allows deploying new app version before running ALTER TABLE.
Avoids hard startup failure on schema mismatch.

**Consequence:** Both OperationalError and ProgrammingError caught.
Only errno 1054 triggers fallback — other errors re-raise.
Inherited from ha-climate-controller.

---

## ADR-009 — Shared `_read_temp_validated()` helper for both regulators

**Decision:** Both `regulate_fan()` and `regulate_heater()` obtain the
chamber temperature exclusively through `_read_temp_validated()`. The helper
performs state lookup, numeric conversion, `math.isfinite()` check, and
plausibility-range check (`TEMP_MIN_VALID < t < TEMP_MAX_VALID`) and returns
either a valid float or `None`. No regulator may bypass it.

**Rationale:** Before v1.1, the two regulators independently implemented
their guards, and they had drifted apart:

- `regulate_fan` had the full chain (state guard → `float()` → range check)
- `regulate_heater` had only the state guard, then called `safe_float()`
  with `DEFAULT_TARGET_TEMP` as fallback — meaning a race condition between
  the guard and the fallback read could have silently produced a 65°C
  reading, and the heater had no plausibility range check at all

This is exactly the failure mode Pattern 2 in `PATTERNS.md` was written
to prevent. Centralising the read into one helper means future regulators
inherit the validation by construction, and any change to validation
rules takes effect uniformly.

**Consequence:** `safe_float()` is no longer the right tool for the
temperature sensor — its silent fallback is the wrong semantic for a
safety-critical reading. `safe_float()` remains correct for setpoints
and configuration helpers (`target_temp`, `fan_min`, `fan_max`,
`heater_hysteresis`) where a brief unavailability should fall back to a
sensible default rather than block control.

The notification idempotency flag `_notification_active` is part of the
same refactor: now that both regulators can fail a temperature read, the
notification path needs to be robust to repeated entry, partial failure
of `persistent_notification/create`, and retry on subsequent ticks.
The previous trigger (`sensor_fail_count == 3`) made the notification a
one-shot that couldn't be retried if HA was momentarily unresponsive.

---

## ADR-010 — DB writes use SQL NULL for unavailable sensors

**Decision:** The DB-write path (`db_save`) uses `safe_float_or_none()`
for sensor readings, which returns `None` (translated by `pymysql` to
SQL `NULL`) when the sensor is unavailable, non-numeric, or non-finite.
It does not fabricate a fallback value.

**Rationale:** The original `db_save` used `safe_float(..., 0)` for both
`temp` and `humidity`. This was inconsistent with the heater column,
which already used `safe_is_on()` and wrote `NULL` for unavailable
state (and a comment in the v1.0 source even highlighted this as the
correct pattern). A sensor outage during a 2-minute DB tick therefore
wrote `0°C` and `0% RH` instead of `NULL`:

- the temperature-stability query in `DATABASE.md` would compute its
  average and stddev including a sequence of zeros — visually obvious,
  but quietly fatal for any automated alerting on the same numbers
- the humidity-drop query would report a fake "humidity reached 0%"
  which never happened
- the override-delta query (`fan_speed - pid_output`) would treat the
  zero as a real PID request

Writing `NULL` makes `AVG()`, `MIN()`, `MAX()`, etc. naturally skip the
missing samples (SQL standard semantics) and makes outages visible as
gaps in the trend rather than as spikes.

**Consequence:** Consumers of the DB (dashboards, alerting, ad-hoc
queries from `DATABASE.md`) must tolerate `NULL`. The existing queries
do so without modification because they all use SQL aggregate functions
or row-by-row reads where `NULL` is the natural "no data" sentinel.
The cascade-zone column also already uses the literal string
`"unknown"` for the same reason (the zone is unknown until the first
valid tick) — `NULL` and `"unknown"` together make the missing-data
state observable.

`safe_float()` remains in use elsewhere in the codebase where a default
is the right semantic — DB writes are the only place where the
*absence* of data is itself the data point.
