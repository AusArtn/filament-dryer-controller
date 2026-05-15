# Code Patterns & Symmetry Checklist

Reference for AI coding sessions and code reviews.
Read this before any code review or new feature addition.

> These patterns were extracted from real bugs found during development
> of the parent project (ha-climate-controller). They are applied here
> from the start to avoid repeating the same mistakes.

---

## How to use this document

Before any code review:
1. Work through every pattern below
2. grep the source for the listed terms
3. Check findings against `KNOWN_NON_BUGS.md` before reporting

---

## Pattern 1 — safe_is_on() for all switch state reads

**Rule:** Every read of a switch/boolean entity state must use `safe_is_on()`.
Never compare `get_state(entity) == "on"` directly.

**Why:** "unavailable" returns None from `get_state()`. `None == "on"` is False.
This silently treats "we don't know" as "off", which can cause:
- Heater wrongly assumed off when status is unclear
- False `False` written to DB instead of NULL

**Grep:**
```bash
grep -n '== "on"' filament_dryer.py
grep -n '!= "on"' filament_dryer.py
```

**Expected findings:** None. All switch reads in this project use `safe_is_on()`.
Unlike the parent project, there are no legacy `== "on"` patterns here.

---

## Pattern 2 — sensor guard before safety-critical reads

**Rule:** Safety-critical sensor reads must go through a single validated
helper (`_read_temp_validated()` for the temperature sensor) that handles
state lookup, numeric conversion, finite check, and plausibility-range
check in one place. Do not combine an inline `get_state()` guard with a
later `safe_float()` call for the same entity.

**Why:** Two failure modes from v1.0:

1. `safe_float()` returns a fallback value silently. A fallback temperature
   of 65.0°C would cause the cascade to compute zone "green" even when the
   sensor is broken — no log, no warning, safety net blind.

2. Even with a prior `get_state()` guard, a follow-up `safe_float()` re-reads
   the entity. Between the two reads the state can change. The v1.0
   `regulate_heater` had exactly this shape — guard, then `safe_float(...,
   DEFAULT_TARGET_TEMP)` — and a race condition could have injected 65°C
   as a phantom reading.

Centralising into one helper also forces both regulators to apply the
*same* rules (the v1.0 fan regulator validated `0 < temp < 150`; the v1.0
heater regulator did not). See ADR-009.

**Correct pattern:**
```python
temp = self._read_temp_validated()
if temp is None:
    # caller decides: log+skip, increment fail counter, etc.
    return
# temp is guaranteed: float, finite, within TEMP_MIN_VALID..TEMP_MAX_VALID
```

**Grep:**
```bash
grep -n 'safe_float(self\.e\["temp"\]' filament_dryer.py
# Expected: no matches. Temp reads go through _read_temp_validated().

grep -n '_read_temp_validated' filament_dryer.py
# Expected: defined once, called by regulate_fan and regulate_heater.
```

**Note on `safe_float()`:** Still correct for setpoints / config helpers
(`target_temp`, `fan_min`, `fan_max`, `heater_hysteresis`) where a brief
unavailability should fall back to a sensible default rather than block
control. The rule above is specifically about sensors driving safety
decisions.

---

## Pattern 3 — safe conversion for hardware attribute reads

**Rule:** Fan `percentage` attribute must be converted via try/except,
not with raw `int(float(value))`.

**Why:** Hardware attributes can be None or non-numeric when device is briefly
offline. A TypeError here aborts the DB write cycle.

**Pattern:**
```python
raw = self.get_state(self.e["fan"], attribute="percentage")
try:
    speed = int(float(raw)) if raw is not None else 0
except (ValueError, TypeError):
    speed = 0
```

---

## Pattern 4 — PID limits updated before override checks

**Rule:** `self.pid.v_min` and `self.pid.v_max` must be set from cascade output
before any override check fires and returns early.

**Why:** If an override returns early without updating PID limits, the next
regular tick inherits stale limits from the previous cascade computation.
On zone changes during an override episode this produces incorrect behavior.

**Grep:**
```bash
grep -n "pid.v_min\|pid.v_max" filament_dryer.py
# Verify assignment appears before the hard override return
```

---

## Pattern 5 — module-level constants for all thresholds

**Rule:** All numeric thresholds go at the top of the file as module-level
constants with clear comments. No magic numbers inside functions.

**Why:** Tuning requires changing values without hunting through logic.
Constants also self-document what the value means.

**Grep:**
```bash
grep -n "^\s*[0-9]" filament_dryer.py
# Any numeric literal inside a function body is a candidate for promotion
```

---

## Pattern 6 — DB-write path uses `None`-returning variants, not fallback values

**Rule:** Sensor reads on the DB-write path (`db_save`) must use
`safe_float_or_none()` (returns `None` for unavailable) rather than
`safe_float()` (returns a configured fallback). Switch reads already use
`safe_is_on()` which returns `None` on unavailable — apply the same
principle to numeric sensors.

**Why:** The DB is the historical record. Writing `0` for an unavailable
sensor fabricates a data point that never happened — it drags
`AVG()`/`STDDEV()`/`MIN()`/`MAX()` toward zero, registers as a fake
"chamber cooled to freezing" in `MIN(temp)` queries, and aliases under the
`override_delta` query in `DATABASE.md`. Writing `NULL` makes outages
visible as gaps and naturally excludes them from aggregates. See ADR-010.

**Grep:**
```bash
grep -n 'safe_float(self\.e\[' filament_dryer.py
# Expected: only on target_temp / fan_min / fan_max / heater_hysteresis
#           (i.e. setpoint reads in the control path, never in db_save).

grep -n "safe_float_or_none" filament_dryer.py
# Expected: only inside db_save, only on temp and humidity.
```

---

## Checklist for new code additions

Before committing any new control logic:

- [ ] New switch state reads use `safe_is_on()` (Pattern 1)
- [ ] New temperature reads for control decisions go through
      `_read_temp_validated()` — no ad-hoc `safe_float` on the temp sensor
      (Pattern 2)
- [ ] New hardware attribute reads use try/except conversion (Pattern 3)
- [ ] PID limits updated before any early-return override (Pattern 4)
- [ ] Module-level constants used for all thresholds (Pattern 5)
- [ ] New DB-write columns use `_or_none` variants — no fabricated zeros
      for unavailable sensors (Pattern 6)
- [ ] `safe_service()` wrapper used for all HA service calls
- [ ] HA notifications use a `_*_active` flag for idempotency, not a
      counter equality check
- [ ] `KNOWN_NON_BUGS.md` checked — is this a known intentional pattern?
- [ ] `ARCHITECTURE.md` checked — does the change conflict with any ADR?
