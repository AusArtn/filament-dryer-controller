# Known Non-Bugs

Intentional code patterns that look like bugs but are not.
Check this file before reporting a finding from a code review.

---

## NB-001 — Heater and fan regulated independently with no coupling

**Location:** `regulate_heater()` and `regulate_fan()` are separate functions
with no shared state other than the temperature sensor.

**Why not a bug:** This is the intended architecture (ADR-001). The heater
provides energy, the fan dissipates excess. They react to the same temperature
reading without needing to coordinate. The cascade and override in `regulate_fan()`
provide sufficient safety even without heater awareness.

**Status:** Intentional.

---

## NB-002 — Humidity not used as control variable

**Location:** `humidity` entity is read in `db_save()` only, not in any
regulation function.

**Why not a bug:** Humidity in a drying chamber is a consequence of temperature,
not a control target (ADR-002). Logging provides drying progress data.
Active humidity control would conflict with temperature control.

**Status:** Intentional.

---

## NB-003 — Cascade zone thresholds are absolute, not relative to target_temp

**Location:** `CASCADE_COLD_UNTIL`, `CASCADE_GREEN_UNTIL` etc. are fixed °C values.

**Why not a bug:** Safety zone boundaries are based on material constraints,
not on user setpoint (ADR-004). The green zone (35–75°C) covers the safe
operating range for all common filament materials. The panic_hot zone (>80°C)
is a hardware safety boundary regardless of target.

**Consequence to watch:** If `target_temp` is set very close to a zone boundary
(e.g. target=74°C puts you at the edge of green/warn), the PID may frequently
cross the boundary. Adjust zone constants if needed for your specific setup.

**Status:** Intentional.

---

## NB-004 — Override thresholds relative to target_temp, not absolute

**Location:** `temp_threshold = target_temp + OVERRIDE_SOFT_OFFSET`

**Why not a bug:** Override thresholds (soft +3°C, hard +5°C) are intentionally
relative to the setpoint, unlike cascade zone thresholds (ADR-004). The override
is a "something went wrong with PID control" signal — it should be the same
distance from target regardless of what the target is.

**Status:** Intentional. The two different strategies (absolute cascade,
relative override) are complementary.

---

## NB-005 — No connection pooling for MariaDB

**Location:** `db_connect()` called fresh every 2 minutes in `db_save()`

**Why not a bug:** 2-minute interval is low-frequency. A persistent connection
would require reconnect-on-fail logic. A fresh `with conn:` block is simpler
and safe at this frequency.

**Status:** Intentional.

---

## NB-006 — `last_pid_output` stores raw PID value, not clamped fan speed

**Location:** `regulate_fan()`, `self.last_pid_output = round(pid_speed, 3)`

**Why not a bug:** Intentional — this is a diagnostic value for tuning.
The actual fan speed sent to hardware is in the `fan_speed` DB column.
Seeing both values makes it possible to identify when cascade or override
is actively modifying the PID output.

**Safe-mode behaviour (since v1.1):** When the temp sensor has failed
3× and the controller is in safe mode, `last_pid_output` is set to `0.0`
(not the safe-mode fan speed of 50). The PID is reset and not running
during safe mode — recording the actual hardware setting (50) would
make safe-mode rows look identical to a normal tick under the
`override_delta = fan_speed - pid_output` query in `DATABASE.md`.
A value of `0.0` is unambiguous: PID was not active.

**Status:** Intentional.

---

## NB-007 — No anti-cycling protection for heater

**Location:** `regulate_heater()` — no minimum on/off time enforced.

**Why not a bug (currently):** Hysteresis (default ±1°C) provides sufficient
protection against rapid switching in practice. A 1°C dead band at typical
drying temperatures means the heater cycles slowly.

**Revisit if:** Relay wear becomes observable, or if a smaller hysteresis
is needed for precision drying.

**Status:** Intentional for now. Add minimum cycle time if relay protection
becomes a concern.

---

## NB-008 — Both regulators read the temperature sensor independently per tick

**Location:** `regulate_fan()` and `regulate_heater()` both call
`self._read_temp_validated()` at the start of each invocation, instead of
sharing a single read per Home Assistant state change.

**Why not a bug:** AppDaemon's `listen_state` design fires each callback
independently. Combining them into one callback that dispatches to both
regulators would tie their failure modes together — a bug in fan control
that raises an exception would also block heater control on the same
tick. Two independent calls give each regulator its own validation
context and its own failure isolation.

The shared helper `_read_temp_validated()` ensures both regulators apply
identical validation rules (see ADR-009), so the only cost is a second
state lookup on the same tick — cheap compared to AppDaemon's IPC
overhead, and irrelevant for a 1 Hz sensor.

**Status:** Intentional.

---

## NB-009 — `_read_temp_validated()` returns `None` on out-of-range reading without entering safe mode immediately

**Location:** `_read_temp_validated()` returns `None` for readings outside
`TEMP_MIN_VALID..TEMP_MAX_VALID`. In `regulate_fan`, this increments
`sensor_fail_count` exactly like an `unavailable` reading.

**Why not a bug:** An out-of-range reading is a *transient* signal — a
wiring spike, a sensor reset, an EMI event during a relay click. The
3-strike rule treats it the same as any other failure mode: one spike
is logged as a WARNING and ignored, three in a row escalate to safe
mode. This avoids both extremes — neither acting on bad data, nor
panicking on a single glitch.

**Status:** Intentional.

---

## NB-010 — `safe_float()` still has a fallback; `safe_float_or_none()` is a separate function

**Location:** `safe_float()` returns a configurable fallback; the new
`safe_float_or_none()` returns `None` for the same conditions.

**Why not a bug:** Both behaviours are legitimate for different paths.
Setpoints (`target_temp`, `fan_min`, `fan_max`, `heater_hysteresis`)
should fall back to a sensible default during transient unavailability —
otherwise a flickering `input_number` helper would block control. DB
writes should record absence as absence (see ADR-010). Two functions,
two semantics, named to make the intent obvious at the call site.

**Grep check:**
```bash
grep -n "safe_float\b" filament_dryer.py
# Expected: only on target_temp, fan_min, fan_max, heater_hysteresis
grep -n "safe_float_or_none" filament_dryer.py
# Expected: only on temp and humidity inside db_save
```

**Status:** Intentional.
