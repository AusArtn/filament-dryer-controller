# Changelog

> **AI-assisted development note:** Architecture decisions, bug analysis,
> and fix verification in this changelog were produced collaboratively with
> AI coding assistance. All validation was done on real hardware.

---

## v1.1.0 — Code review fixes

**Findings from a structured code review against PATTERNS.md, KNOWN_NON_BUGS.md, and the ADRs.**
Bug numbering matches the review report. All fixes preserve every
intentional pattern listed in `KNOWN_NON_BUGS.md` (NB-001 through NB-007).

### Critical fixes

- **Bug 1 — `regulate_heater` could inject `DEFAULT_TARGET_TEMP` as a phantom
  temperature reading.** The function had an explicit `unavailable` guard
  (good, per Pattern 2), but then re-read the same sensor via
  `safe_float(self.e["temp"], DEFAULT_TARGET_TEMP)`. A state change between
  the two reads would silently inject 65.0°C — and because the fallback was
  the *default* target rather than the *actual* target, hysteresis could
  collapse (`temp ≈ target`) or fire wrongly. Now both regulators read the
  temperature through one helper (`_read_temp_validated()`), and the helper
  never has a fallback that pretends a missing sensor is okay.

- **Bug 2 — `regulate_heater` had no plausibility range check.**
  `regulate_fan` rejected readings outside 0–150°C; `regulate_heater`
  accepted any numeric value, including obviously broken ones (spikes to
  −50°C or 999°C from a wiring fault would have toggled the heater).
  Range check is now applied in the shared helper, so both regulators
  reject the same readings on the same rules.

### Diagnostic / correctness fixes

- **Bug 3 — `last_pid_output = 50.0` during safe mode falsified the
  diagnostic record.** Per NB-006, `last_pid_output` is a tuning
  diagnostic: it should reflect what the PID asked for. In safe mode the
  PID is reset and the 50% fan setting is unconditional — writing 50.0
  aliased safe-mode rows to look like normal PID ticks under the
  `override_delta = fan_speed - pid_output` query in `DATABASE.md`.
  Now records 0.0, which is unambiguous.

- **Bug 4 — Safe-mode notification create/dismiss was not idempotent.**
  The create path fired on exactly `sensor_fail_count == 3`. If that
  service call failed (HA hiccup, caught by `safe_service`), no
  notification was ever posted, but the dismiss path on recovery still
  fired for a notification ID that didn't exist. The create path also
  could not be retried on subsequent failed ticks. Introduced
  `self._notification_active` which is flipped only after a successful
  intent to post; dismiss only fires when it's actually True; retries on
  every failed tick until it sticks.

- **Bug 7 — DB write fabricated `0` for unavailable temperature /
  humidity.** Wrong by the same logic as `safe_is_on()` for the heater
  column (which the file's own comment on line 422 in v1.0 explicitly
  highlighted as the correct pattern — but it had only been applied to
  the heater, not to temp/humidity). A sensor outage during a 2-minute
  DB tick wrote 0°C / 0%RH instead of SQL `NULL`, dragging averages and
  registering as a fake "chamber cooled to freezing" event in the
  trend queries. New helper `safe_float_or_none()` returns `None`,
  which `pymysql` translates to `NULL`.

### Polish

- **Bug 8 — `from datetime import datetime` was unused.** Removed.
  (DB schema sets `timestamp DATETIME DEFAULT NOW()`, see `DATABASE.md`.)

- **Bug 9 — Soft-override entry used `>` while hard-override used `>=`.**
  Inconsistent. Both safety thresholds now fire on equality (`>=`).

- **Bug 10 — Blend formula divided by `(temp_max - temp_threshold)`
  recomputed every tick, with no guard against the two override offsets
  being set equal during tuning.** Promoted to module constant
  `OVERRIDE_BLEND_RANGE` with a startup `assert > 0`, so a misconfiguration
  fails loudly at import instead of crashing on the first hot tick.

### Pattern 5 hardening (no bug, related cleanup)

- Magic numbers `0.0` and `150.0` (sensor plausibility range) promoted to
  module constants `TEMP_MIN_VALID` / `TEMP_MAX_VALID` per Pattern 5.
  They were already documented in `KNOWN_NON_BUGS.md` discussions but
  lived inside a function body.

### New entry points / state

| New | Purpose |
|---|---|
| `_read_temp_validated()` | Single validation path for both regulators (see ADR-009) |
| `safe_float_or_none()` | DB-path read that returns `None` instead of a fabricated fallback (see ADR-010) |
| `self._notification_active` | Idempotency flag for the safe-mode HA notification |
| `OVERRIDE_BLEND_RANGE` | Derived constant `OVERRIDE_HARD_OFFSET − OVERRIDE_SOFT_OFFSET`, asserted positive |
| `TEMP_MIN_VALID` / `TEMP_MAX_VALID` | Promoted sensor plausibility range |

### Verified unchanged

- All 7 intentional patterns in `KNOWN_NON_BUGS.md` (NB-001 to NB-007)
  re-checked and still hold.
- Cascade hysteresis logic for all 6 zone transitions is symmetric and
  unchanged.
- PID anti-windup, `dt` clamping, and integral-freeze-on-override
  semantics are unchanged.
- `errno 1054` DB schema fallback is unchanged.

---

## v1.0.0 — Initial release

**PID temperature controller for filament drying chambers**

### Architecture

Three-layer control structure (defense in depth):

- **Layer 1 — Cascade:** 4 temperature zones dynamically adjust the PID's
  v_min/v_max operating window. Cold zone retains heat during pre-heat;
  warn and panic_hot zones increase airflow preemptively before hard override fires.

- **Layer 2 — Temp-PID:** Controls fan speed within the cascade window.
  Error is `current_temp - target_temp` — positive error (too hot) increases
  fan speed. Anti-windup built in.

- **Layer 3 — Temperature override:** Soft blend (linear mix toward v_max)
  from target+3°C; hard 100% fan + heater off from target+5°C. Intentionally
  ignores cascade limits — defense in depth.

### Safety features

- Temp sensor guard before all safety-critical decisions (explicit state check
  before `safe_float()` — silent fallback would leave safety net blind)
- `safe_is_on()` tri-state for all switch reads (True/False/None —
  "unavailable" never silently treated as "off")
- `safe_service()` wrapper on all HA calls (hiccups logged, controller continues)
- Sensor safe mode after 3 consecutive failures: fan 50%, heater off, HA notification
- PID integral freeze on soft override entry (prevents post-override overshoot)
- Schema backward compatibility: falls back to old INSERT if cascade columns missing

### Inherited patterns from ha-climate-controller

This project shares its core patterns with
[ha-climate-controller](https://github.com/AusArtn/ha-climate-controller):

| Pattern | Description |
|---------|-------------|
| `safe_float()` | Handles None / unavailable / nan / inf |
| `safe_is_on()` | Tri-state switch read |
| `safe_service()` | HA call wrapper |
| Cascade + PID + Override | Three-layer control architecture |
| cfg.yaml | All entity IDs and credentials external |
| errno 1054 DB fallback | Schema migration compatibility |

Differences from ha-climate-controller:
- No VPD logic (temperature is the only control variable)
- No humidifier / light scheduling
- Simpler cascade (4 zones vs 5)
- Heater regulation replaces heat lamp (hysteresis uses `safe_is_on()`, not `== "on"`)
- Sensor safe mode triggers on temp sensor (not VPD)
