# Entity Reference

All Home Assistant entities read or written by the controller.
All entity IDs are configured in `cfg.yaml`.

Since v1.0, no entity IDs are hardcoded in the Python source.

---

## Sensors

| cfg.yaml key | Type | Unit | Description |
|---|---|---|---|
| `temp` | Physical sensor | °C | Chamber temperature. Primary control variable for PID, cascade, override, and heater regulation. Safety-critical — sensor failure triggers safe mode. |
| `humidity` | Physical sensor | % RH | Chamber humidity. Logged to DB only — not used as a control variable. Useful for tracking drying progress. |

---

## Actuators — Switches

| cfg.yaml key | Description |
|---|---|
| `heater` | **Heater element** (PTC, heat mat, ceramic, etc.). Binary on/off, controlled via hysteresis around `target_temp`. Forced off in sensor safe mode. |

---

## Actuators — Fan

| cfg.yaml key | Description |
|---|---|
| `fan` | **Circulation fan** (variable speed). Primary PID output. Controlled via `safe_service("fan/set_percentage", ...)`. `percentage` attribute read back for DB logging. |

---

## Helpers — Setpoints (`input_number`)

| cfg.yaml key | Unit | Default | Description |
|---|---|---|---|
| `target_temp` | °C | 65.0 | Drying temperature setpoint. Adjust per filament material. |
| `fan_min` | % | 10 | Fan speed floor (`v_min` for PID). Cascade can raise this in warm/hot zones. |
| `fan_max` | % | 80 | Fan speed ceiling (`v_max` for PID). Cascade can lower this in cold zone, raise in panic_hot. |
| `heater_hysteresis` | °C | 1.0 | Dead band for heater on/off. Heater ON below `target - hysteresis`, OFF above `target + hysteresis`. |

---

## Entity count

- 2 sensors
- 1 switch
- 1 fan
- 4 `input_number` helpers

→ **8 entities** referenced in the controller
