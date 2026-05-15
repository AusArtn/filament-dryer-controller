# Code Review Report — v1.0.0 → v1.1.0

> AI-assisted review: bugs identified by structured analysis of
> `filament_dryer.py` against `PATTERNS.md`, `KNOWN_NON_BUGS.md`, and the
> ADRs in `ARCHITECTURE.md`. All findings were cross-checked against the
> intentional-patterns file before being reported. Fixes are documented
> with rationale and verification.

This document is a one-shot audit record. Future reviews should create
their own report; this one stays as a snapshot of the v1.1.0 work.

---

## Summary

10 candidate findings examined. **8 confirmed bugs**, **1 false alarm**
(Cascade hysteresis, withdrawn after re-inspection), **1 hardening item**
documented as enhancement.

| # | Severity | Status | Area |
|---|----------|--------|------|
| 1 |  high | fixed | `regulate_heater` — phantom `DEFAULT_TARGET_TEMP` reading via `safe_float` race |
| 2 |  high | fixed | `regulate_heater` — missing temperature plausibility range check |
| 3 |  medium | fixed | `last_pid_output = 50.0` in safe mode falsified diagnostic record |
| 4 |  medium | fixed | Safe-mode notification create/dismiss not idempotent |
| 5 |  low | enhancement | Two independent state reads per tick — kept intentionally (see NB-008) |
| 6 | — | withdrawn | False alarm: cold→green cascade transition is correct |
| 7 |  low | fixed | DB writes fabricated `0` for unavailable temp / humidity |
| 8 |  trivial | fixed | Unused `datetime` import |
| 9 |  trivial | fixed | `>` vs `>=` inconsistency between override thresholds |
| 10 | trivial | fixed | Blend-range divisor inline; no guard against equal offsets |

---

## Detailed findings

### Bug 1 — Phantom temperature reading via `safe_float` fallback

**Severity:** 🔴 High (safety-critical, potentially silent)

**Location:** `regulate_heater()`, v1.0 lines 380–386

**Symptom in v1.0:**
```python
temp_state = self.get_state(self.e["temp"])
if temp_state in (None, "unavailable", "unknown"):
    self.log("WARNING: ...", level="WARNING")
    return

temp = self.safe_float(self.e["temp"], DEFAULT_TARGET_TEMP)
```

Two separate state reads. Between the explicit guard and `safe_float()`
re-reading the same entity, the state can transition to `unavailable`.
In that race, `safe_float()` returns `DEFAULT_TARGET_TEMP` (65.0) silently.

The fallback was the *default* target temperature, not the user's actual
target. So with a user-configured `target_temp = 50°C` and `hysteresis = 1°C`:
- a phantom `temp = 65°C` reading would compare as `65 > (50 + 1)` → heater
  forced off even if the actual chamber was at 49°C and the heater should
  be on.

This is also the exact failure mode Pattern 2 in `PATTERNS.md` was written
to prevent — the v1.0 implementation almost got it right, then defeated
itself by using `safe_float` for the post-guard read.

**Fix:** Both regulators now go through `_read_temp_validated()`, which
performs the state lookup once, validates type / finite / plausibility
range, and returns `None` on any failure. No silent fallback exists
anywhere in the safety-critical path. The pattern check in `PATTERNS.md`
Pattern 2 has been rewritten to catch this in future reviews.

**Verification:**
```bash
$ grep -n 'safe_float(self\.e\["temp"\]' filament_dryer.py
# (no output — no safe_float call on the temp sensor remains)

$ grep -n '_read_temp_validated' filament_dryer.py
148:    def _read_temp_validated(self):
374:        temp = self._read_temp_validated()    # regulate_fan
484:        temp = self._read_temp_validated()    # regulate_heater
```

---

### Bug 2 — Missing plausibility range check in `regulate_heater`

**Severity:**  High (asymmetric safety — same sensor, different rules)

**Location:** `regulate_heater()`, v1.0 (entire function)

**Symptom in v1.0:** `regulate_fan` rejected readings outside
`0 < temp < 150` °C. `regulate_heater` accepted any numeric value. A
wiring fault producing `temp = -50` would have made `regulate_heater`
compare `-50 < (target - hysteresis)` and switch the heater on. A spike
to `temp = 999` would switch it off (which is fail-safe in that direction,
but still: the heater was reacting to garbage).

Symmetry break: both regulators read the same sensor and must reject the
same readings on the same rules. Otherwise they're not "two safety
checks" but two inconsistent codepaths whose disagreement is hard to
spot from logs.

**Fix:** The range check is centralised in `_read_temp_validated()`, so
both regulators inherit it. New module constants `TEMP_MIN_VALID` and
`TEMP_MAX_VALID` replace the inline magic numbers from `regulate_fan`
(this also satisfies Pattern 5).

**Verification:** Triggering an out-of-range value in unit-test style
manual trace:
```
_read_temp_validated() with state="200.0"
→ WARNING: Temp out-of-range (200.0C) - valid window is 0.0-150.0C
→ returns None
```
Both `regulate_fan` (now: counts as sensor failure tick) and
`regulate_heater` (now: skips one tick, logs WARNING) react identically.

---

### Bug 3 — `last_pid_output = 50.0` in safe mode

**Severity:**  Medium (diagnostic correctness, not control behaviour)

**Location:** `regulate_fan()`, v1.0 line 299

**Symptom in v1.0:**
```python
self.safe_service("fan/set_percentage", entity_id=fan, percentage=50)
...
self.last_pid_output = 50.0
```

Per `KNOWN_NON_BUGS.md` NB-006, `last_pid_output` is a tuning diagnostic:
the raw value the PID requested. In safe mode the PID is reset and not
running — the 50% fan setting is a hardware fail-safe, not a PID output.

Writing 50.0 made safe-mode rows in the DB look like a normal tick under
the `DATABASE.md` query:
```sql
SELECT ABS(fan_speed - pid_output) AS override_delta ...
```
For a safe-mode row: `50 - 50 = 0` → looked like a normal tick.

**Fix:** `last_pid_output = 0.0` during safe mode. `0` is unambiguous —
PID was not active. The original NB-006 entry has been updated to
explicitly document the safe-mode behaviour.

---

### Bug 4 — Notification create/dismiss not idempotent

**Severity:**  Medium (operational reliability)

**Location:** `regulate_fan()`, v1.0 lines 288–305

**Symptom in v1.0:**
- Create fired on `sensor_fail_count == 3` (equality).
- If that `safe_service` call failed (HA hiccup), the create was caught
  and lost. `sensor_fail_count` continued incrementing to 4, 5, … and the
  notification was never retried.
- On recovery (`sensor_fail_count >= 3` was always true post-failure),
  the dismiss path still fired — for a notification ID that didn't exist.
- A flickering sensor that briefly recovered each time before reaching 3
  would reset the counter and never trigger the notification at all
  (acceptable behaviour, but masked by Bug 4's brittleness in the
  longer outage case).

**Fix:** New flag `self._notification_active`, initialized `False`.

- Create path: `if not self._notification_active:` → post and set flag
  `True`. On every fail tick while the flag is still `False` (e.g.
  because the previous post failed), the create is re-attempted.
- Dismiss path: `if self._notification_active:` → dismiss and clear
  flag. Never dismisses a notification that was never posted.
- Decoupled entirely from `sensor_fail_count` semantics.

**Verification:** Manual trace through three scenarios:
1. Clean outage of 5 ticks then recovery: create on tick 3 (flag → True),
   silent on ticks 4/5, dismiss on recovery (flag → False). ✓
2. Outage of 5 ticks, create fails on tick 3: silent retry on tick 4
   (still flag = False), succeeds on tick 5 (flag → True), dismiss on
   recovery. ✓
3. Outage of 2 ticks, recovery, outage of 3 ticks: counter resets at
   recovery; second outage creates on its own tick 3. ✓

---

### Bug 5 — Two independent state reads per tick (kept)

**Severity:**  Low

**Status:** Documented as intentional in `KNOWN_NON_BUGS.md` NB-008.

**Why not fixed:** Combining both regulators into a single callback
would couple their failure modes. The shared validation helper
(`_read_temp_validated()`) achieves the consistency benefit without the
coupling cost. The redundant state lookup is cheap.

---

### Bug 6 — Cascade `cold → green` transition (withdrawn)

**Severity:** Initially flagged as , on re-inspection a false alarm.

**Analysis:** All 6 transitions in `compute_cascade` were re-verified:

| From | To | Threshold | Hysteresis applied |
|------|----|-----------|---------------------|
| cold | green | `temp >= 35.0` | No (ascent — green→cold descent carries it) |
| green | cold | `temp < 35.0 − 0.5` | Yes |
| green | warn | `temp >= 75.0` | No (ascent — warn→green descent carries it) |
| warn | green | `temp < 75.0 − 0.5` | Yes |
| warn | panic_hot | `temp >= 80.0` | No (ascent — panic_hot→warn descent carries it) |
| panic_hot | warn | `temp < 80.0 − 0.5` | Yes |

Hysteresis is correctly applied on descents only. Adding it on ascents
too would create a non-overlapping dead zone (35.0 to 35.5°C in the
cold/green case) where the zone would not change — wrong direction.

The v1.0 implementation is correct. No change.

---

### Bug 7 — DB writes fabricated `0` for unavailable sensors

**Severity:**  Low (data quality, not safety)

**Location:** `db_save()`, v1.0 lines 420–421

**Symptom in v1.0:**
```python
temp     = self.safe_float(self.e["temp"],     0)
humidity = self.safe_float(self.e["humidity"], 0)
```

For an outage during a 2-minute DB tick: writes `0°C` and `0% RH`.
Inconsistent with the heater column in the same function — `safe_is_on()`
correctly writes `NULL` on unavailability, and the v1.0 source even has a
comment on the very next line documenting this as the intended pattern.
Temp/humidity were just overlooked when applying the same convention.

Impact on `DATABASE.md` queries:
- `MIN(temp)` → reports false 0°C cooldowns
- `AVG(temp)` → biased toward zero during partial outages
- humidity-drop query → fake "humidity dropped to 0%" event
- override-delta query → 0 alias for `pid_output`, looks like normal tick

**Fix:** New helper `safe_float_or_none()` returns `None` (→ SQL `NULL`)
for unavailable / non-numeric / non-finite. Used in the DB-write path
only. Documented as Pattern 6 in `PATTERNS.md` for future review.

**Verification:**
```bash
$ grep -n "safe_float(self\.e\[" filament_dryer.py
# All hits are setpoints (target_temp, fan_min, fan_max, heater_hysteresis)
# — none on sensor reads.

$ grep -n "safe_float_or_none" filament_dryer.py
# Defined once, used twice — only in db_save, only on temp and humidity.
```

---

### Bug 8 — Unused `datetime` import

**Severity:**  Trivial

**Fix:** Import removed. DB schema sets `timestamp DATETIME DEFAULT NOW()`,
so the import was never needed.

---

### Bug 9 — `>` vs `>=` inconsistency at override thresholds

**Severity:**  Trivial (cosmetic, not behavioural)

**Location:** `regulate_fan()`, v1.0 line 350 (soft) vs 333 (hard)

**Symptom in v1.0:**
- Hard override (line 333): `if temp >= temp_max:` — fires on equality
- Soft override (line 350): `if temp > temp_threshold:` — does not fire on equality

Inconsistent boundary convention between two related fail-safes.
Practically irrelevant given floating-point comparisons, but the rule
should be uniform: both fail-safe paths fire on equality.

**Fix:** Soft override changed to `>=`. Hard override unchanged.
Comment added explaining the convention.

---

### Bug 10 — Inline blend-range division with no guard

**Severity:** Trivial → Low if misconfigured

**Location:** `regulate_fan()`, v1.0 line 355

**Symptom in v1.0:**
```python
blend = (temp - temp_threshold) / (temp_max - temp_threshold)
```

The denominator is constant (`OVERRIDE_HARD_OFFSET - OVERRIDE_SOFT_OFFSET`
= 2.0). It was recomputed every tick — a minor inefficiency. More
importantly, if a future tuner sets the two override offsets equal
(e.g. both to 4.0 for a tighter override window), the controller would
import fine, run fine until the first soft-override hit, then crash with
`ZeroDivisionError` on a hot chamber — the worst possible time.

**Fix:**
```python
OVERRIDE_BLEND_RANGE = OVERRIDE_HARD_OFFSET - OVERRIDE_SOFT_OFFSET
assert OVERRIDE_BLEND_RANGE > 0, "..."
```

A misconfiguration now fails loudly at import time, before any hardware
control runs. The blend formula uses the constant directly.

---

## What was verified unchanged

All intentional patterns from `KNOWN_NON_BUGS.md` (NB-001 through NB-007)
were re-checked against the new code:

- NB-001: Heater and fan still regulated independently. ✓
- NB-002: Humidity still logged only. ✓
- NB-003: Cascade thresholds still absolute. ✓
- NB-004: Override thresholds still relative to target_temp. ✓
- NB-005: No connection pooling. ✓
- NB-006: `last_pid_output` still raw PID diagnostic value (entry
  updated to clarify safe-mode behaviour). ✓
- NB-007: No anti-cycling for heater. ✓

ADRs 001–008 all still hold; ADR-009 and ADR-010 added.

Cascade hysteresis logic (all 6 transitions): manually re-verified, see
Bug 6 entry above.

PID anti-windup, dt-clamping, integral-freeze-on-override: untouched.

`errno 1054` schema-fallback path: untouched.

---

## Verification commands run

```bash
# Syntax / parse
python3 -m py_compile filament_dryer.py

# Pattern checks
grep -n 'safe_float(self\.e\["temp"\]' filament_dryer.py    # → empty
grep -n '== "on"'                       filament_dryer.py    # → empty
grep -n 'last_pid_output = 50'          filament_dryer.py    # → empty
grep -n '_read_temp_validated'          filament_dryer.py    # → 3 hits
grep -n 'safe_float_or_none'            filament_dryer.py    # → 3 hits
grep -n '_notification_active'          filament_dryer.py    # → 5 hits
```

All checks pass as expected.

---

## Files touched

| File | Change |
|------|--------|
| `filament_dryer.py` | Bug fixes 1–4, 7–10. Version bumped to 1.1.0. |
| `CHANGELOG.md` | v1.1.0 entry added at the top. |
| `ARCHITECTURE.md` | ADR-009 (shared temp validation) and ADR-010 (NULL on DB unavailability) appended. |
| `KNOWN_NON_BUGS.md` | NB-006 clarified for safe-mode behaviour. NB-008/009/010 added for new intentional patterns. |
| `PATTERNS.md` | Pattern 2 rewritten around `_read_temp_validated()`. Pattern 6 added for DB-path None convention. Checklist updated. |
| `DATABASE.md` | Note added about NULL semantics in queries. |
| `README.md` | Safety-model table updated for the new behaviours. |
| `ENTITIES.md` | Unchanged — no entity changes. |
| `cfg_example.yaml` | Unchanged — no config changes. |

## Files not touched

- `ENTITIES.md` — no entity changes.
- `cfg_example.yaml` — no config keys changed. Existing user `cfg.yaml`
  files continue to work as-is on v1.1.0.

---

## Migration notes

**Upgrading from v1.0.0:**

1. No `cfg.yaml` changes required.
2. No DB schema changes required.
3. If you have automation downstream of the DB that interprets `0`
   in `temp` / `humidity` as a real reading, those need to tolerate
   `NULL` now. The provided queries in `DATABASE.md` all do.
4. The HA notification ID is unchanged (`dryer_temp_fail`), so any
   automations referencing it continue to work.
