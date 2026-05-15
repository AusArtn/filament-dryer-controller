# filament-dryer-controller

A PID-based temperature controller for filament drying chambers, running as an
[AppDaemon](https://appdaemon.readthedocs.io/) app inside
[Home Assistant](https://www.home-assistant.io/).

Controls a heater and circulation fan via temperature PID regulation,
with a cascade safety layer, hard override protection, and MariaDB logging.

> **AI-assisted development:** This project was built iteratively with
> AI coding assistance (Claude by Anthropic). The codebase, documentation
> structure, and architecture decisions were developed collaboratively.
> All operational validation was done by the human author on real hardware.

---

## What it does

- **Heater** — binary on/off with configurable hysteresis around target temperature
- **Circulation fan** — variable speed via PID targeting the temperature setpoint
- **Temperature cascade** — dynamically adjusts PID operating window (v_min/v_max) across 4 temperature zones
- **Temperature override** — hard safety net at configurable thresholds (default +3 / +5 °C above target)
- **Sensor safe mode** — fan set to 50%, heater forced off, HA notification on repeated sensor failure
- **MariaDB logging** — all key values every 2 minutes for trend analysis and tuning

---

## Why PID for the fan?

The heater is binary (on/off). The fan does the fine regulation:
- When temperature is too high → fan speed increases, heat dissipates faster
- When temperature is below target → fan slows down, heater builds up heat

This gives smooth, stable temperature control without the hunting typical
of pure on/off heater control.

---

## Filament drying temperatures

| Material | Target temp | Notes |
|----------|------------|-------|
| PLA | 45–50 °C | Sensitive — too high warps |
| PETG | 55–65 °C | |
| ABS | 60–80 °C | |
| ASA | 60–80 °C | |
| Nylon (PA) | 70–80 °C | Very hygroscopic, needs longest time |
| TPU | 50–60 °C | |
| PC | 80–90 °C | Check your hardware temp limits |

Drying time: typically 4–8 hours. Log humidity to track progress.

---

## Requirements

- Home Assistant (HAOS recommended)
- AppDaemon 4.x (as Add-on or standalone container)
- MariaDB (as HA Add-on or separate container)
- Python packages: `pymysql`, `pyyaml` (both available in AppDaemon environment)

---

## Installation

1. Copy `filament_dryer.py` into your AppDaemon apps directory
   (e.g. `/config/appdaemon/apps/`)

2. Copy `cfg.example.yaml` to `cfg.yaml` in the same folder and fill in
   your entity IDs and database credentials

3. Register the app in `apps.yaml`:

```yaml
filament_dryer:
  module: filament_dryer
  class: FilamentDryer
```

4. Create the MariaDB table (see [Database Setup](#database-setup))

5. Restart AppDaemon

---

## Configuration

All entity IDs, database credentials, and PID starting values are loaded
from `cfg.yaml`. The code contains no hardcoded strings.

See `cfg.example.yaml` for a fully documented template.

**`cfg.yaml` is in `.gitignore` — never commit it.**

### PID tuning guide

| Symptom | Action |
|---------|--------|
| Fan oscillates / hunts | Lower `kp` to ~8, set `kd` to 0 |
| Too slow to respond | Increase `kp` gradually |
| Persistent steady-state offset | Increase `ki` slightly |

Keep `ki` small — the integral term only eliminates residual offset.
Anti-windup is built in: integral is capped at `(v_max - v_min) / ki`.

---

## Temperature cascade zones

The cascade dynamically adjusts the PID's `v_min`/`v_max` window.
All thresholds are module-level constants at the top of `filament_dryer.py`.

| Zone | Temp range | Effect |
|------|-----------|--------|
| `cold` | < 35 °C | Cap fan at 30% — retain heat while pre-heating |
| `green` | 35–75 °C | Full PID range — optimal drying |
| `warn` | 75–80 °C | Raise fan floor to 40% — preemptive cooling |
| `panic_hot` | > 80 °C | Floor 70%, ceiling 100% — maximum cooling |

Hysteresis (default 0.5 °C) prevents zone flapping.

The hard temperature override (Layer 3) acts as a defense-in-depth
safety net independent of the cascade:
- At target + 3 °C: soft blend — fan speed pushed toward v_max
- At target + 5 °C: fan to 100% directly, PID reset

---

## Database setup

```sql
CREATE TABLE measurements (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    timestamp     DATETIME DEFAULT NOW(),
    temp          FLOAT,
    humidity      FLOAT,
    fan_speed     INT,
    pid_output    FLOAT,
    kp            FLOAT,
    ki            FLOAT,
    kd            FLOAT,
    heater        BOOLEAN,
    cascade_zone  VARCHAR(20),
    v_min_active  FLOAT,
    v_max_active  FLOAT
);
```

---

## Home Assistant helpers needed

**`input_number`** helpers:
- `dryer_target_temp` — drying temperature setpoint (°C)
- `dryer_fan_min` — fan speed floor (%)
- `dryer_fan_max` — fan speed ceiling (%)
- `dryer_heater_hysteresis` — heater dead band (°C, default 1.0)

All entity IDs are configured in `cfg.yaml`.

---

## Architecture overview

```
Layer 3 (hard safety net)
  └─ Temp ≥ target + 5°C → fan 100%, PID reset, heater off

Layer 3 (soft override, linear blend)
  └─ Temp between target+3 and target+5 → blend PID output toward v_max
     PID integral frozen on zone entry to prevent post-override overshoot

Layer 1 (cascade)
  └─ Temperature zone → adjusts v_min / v_max window

Layer 2 (PID)
  └─ Temp error → fan speed within v_min/v_max window

Heater (independent)
  └─ Simple hysteresis around target_temp ± hysteresis
```

---

## Safety model

| Situation | Behaviour |
|-----------|-----------|
| Temp sensor unavailable / non-numeric / non-finite / out of plausible range (0–150 °C) | Treated as a sensor failure tick — counted; safe mode triggers after 3 in a row |
| Safe mode (3 consecutive sensor failures) | Fan 50%, heater off, HA notification, PID reset, `last_pid_output` set to 0 |
| Sensor recovers from safe mode | Notification dismissed (only if it was actually posted), normal control resumes |
| Heater status unavailable | Heater regulation skipped, WARNING logged |
| cfg.yaml missing at startup | ERROR + raise — controller does not start |
| `OVERRIDE_HARD_OFFSET ≤ OVERRIDE_SOFT_OFFSET` (misconfiguration) | App fails to import (AssertionError) — fails loudly, never silently divides by zero |
| Sensor outage during DB tick | Writes SQL `NULL` for the affected column, not a fabricated value |
| Any HA service call fails | Caught and logged, controller keeps running |

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

---

## License

MIT License — see [LICENSE](LICENSE)
