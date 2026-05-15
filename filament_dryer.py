import appdaemon.plugins.hass.hassapi as hass
import math
import time
import pymysql
import yaml
import os

# ════════════════════════════════════════════════════
# CASCADE ZONE BOUNDARIES  (adjust here for tuning)
# Ascent at these values, descent only CASCADE_HYSTERESIS °C lower
# ════════════════════════════════════════════════════
CASCADE_COLD_UNTIL        = 35.0   # °C  below this: cold zone (pre-heat)
CASCADE_GREEN_UNTIL       = 75.0   # °C  below this: green zone (target range)
CASCADE_WARN_UNTIL        = 80.0   # °C  below this: warning zone
CASCADE_HYSTERESIS        = 0.5    # °C  hysteresis at all zone transitions

CASCADE_COLD_VMAX         = 30     # %   fan v_max ceiling in cold zone (retain heat)
CASCADE_WARN_VMIN         = 40     # %   fan v_min floor in warning zone
CASCADE_PANIC_HOT_VMIN    = 70     # %   fan v_min floor in hot-panic zone
CASCADE_PANIC_HOT_VMAX    = 100    # %   fan v_max in hot-panic (overrides basis)

# ════════════════════════════════════════════════════
# DEFAULT FALLBACKS  (used when input_number entity is unavailable)
# ════════════════════════════════════════════════════
DEFAULT_TARGET_TEMP       = 65.0   # °C  safe middle ground for most filaments
DEFAULT_FAN_MIN           = 10.0   # %   keep some airflow always
DEFAULT_FAN_MAX           = 80.0   # %

# ════════════════════════════════════════════════════
# TEMPERATURE OVERRIDE THRESHOLDS
# Soft blend starts at target + OVERRIDE_SOFT_OFFSET
# Hard 100% fan kicks in at target + OVERRIDE_HARD_OFFSET
# Blend range derived once — guards against zero-division if the two
# offsets are ever set equal during tuning.
# ════════════════════════════════════════════════════
OVERRIDE_SOFT_OFFSET      = 3.0    # °C above target -> soft blend starts
OVERRIDE_HARD_OFFSET      = 5.0    # °C above target -> fan to 100%, PID reset
OVERRIDE_BLEND_RANGE      = OVERRIDE_HARD_OFFSET - OVERRIDE_SOFT_OFFSET
assert OVERRIDE_BLEND_RANGE > 0, (
    "OVERRIDE_HARD_OFFSET must be strictly greater than OVERRIDE_SOFT_OFFSET; "
    "otherwise the soft-blend formula divides by zero."
)

# ════════════════════════════════════════════════════
# TEMPERATURE PLAUSIBILITY RANGE
# Used by _read_temp_validated() to reject sensor spikes / wiring faults
# before any control decision is made.
# ════════════════════════════════════════════════════
TEMP_MIN_VALID            = 0.0    # °C  below this: sensor likely faulty
TEMP_MAX_VALID            = 150.0  # °C  above this: sensor likely faulty


# ════════════════════════════════════════════════════
# PID CONTROLLER
# ════════════════════════════════════════════════════
class TempController:
    def __init__(self, kp, ki, kd, v_min, v_max):
        self.kp    = kp
        self.ki    = ki
        self.kd    = kd
        self.v_min = v_min
        self.v_max = v_max

        self.integral   = 0.0
        self.last_error = None
        self.last_time  = None

    def update(self, current_temp, target_temp):
        now = time.monotonic()

        if self.last_time is None:
            dt = 5.0
        else:
            dt = now - self.last_time
            if dt <= 0 or dt > 30:
                dt = 5.0

        # Error: positive when too hot (fan should increase)
        error = current_temp - target_temp
        p_out = self.kp * error

        self.integral += error * dt
        integral_max   = (self.v_max - self.v_min) / max(self.ki, 0.001)
        self.integral  = max(-integral_max, min(integral_max, self.integral))
        i_out = self.ki * self.integral

        if self.last_error is not None:
            d_out = self.kd * (error - self.last_error) / dt
        else:
            d_out = 0.0

        self.last_error = error
        self.last_time  = now

        raw = p_out + i_out + d_out
        return max(self.v_min, min(self.v_max, raw))

    def reset(self):
        self.integral   = 0.0
        self.last_error = None
        self.last_time  = None


class FilamentDryer(hass.Hass):

    def initialize(self):
        self.log("Filament Dryer Controller started! Version 1.1.0")

        # ── Load configuration from cfg.yaml ─────────────
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cfg.yaml")
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            self.cfg    = cfg
            self.e      = cfg["entities"]
            self.db_cfg = cfg["db"]
            self.log("cfg.yaml loaded successfully")
        except Exception as ex:
            self.log(f"ERROR: cfg.yaml could not be loaded - controller cannot start: {ex}",
                    level="ERROR")
            raise

        # ── State variables ───────────────────────────────
        self.sensor_fail_count      = 0
        self.last_pid_output        = 0.0
        self.cascade_zone           = None   # None = first determination on first tick
        self.v_min_active           = DEFAULT_FAN_MIN
        self.v_max_active           = DEFAULT_FAN_MAX
        # PID integral freeze on soft override entry (see regulate_fan)
        self._temp_override_active  = False
        # Tracks whether the safe-mode HA notification is currently posted.
        # Decoupled from sensor_fail_count so a failed create() doesn't leave
        # the dismiss path firing for a non-existent notification (see ADR-009).
        self._notification_active   = False

        pid_cfg = cfg.get("pid", {})
        self.pid = TempController(
            kp=pid_cfg.get("kp", 15.0),
            ki=pid_cfg.get("ki", 0.3),
            kd=pid_cfg.get("kd", 2.0),
            v_min=DEFAULT_FAN_MIN,
            v_max=DEFAULT_FAN_MAX
        )

        # ── State listeners ───────────────────────────────
        self.listen_state(self.regulate_fan,     self.e["temp"])
        self.listen_state(self.regulate_heater,  self.e["temp"])

        # ── Periodic DB logging ───────────────────────────
        self.run_every(self.db_save, "now", 120)

        # ── Initial run ───────────────────────────────────
        self.regulate_fan(None, None, None, None, None)
        self.regulate_heater(None, None, None, None, None)

    # ════════════════════════════════════════════════════
    # SAFE FLOAT
    # Handles None / unavailable / unknown / nan / inf.
    # math.isfinite check added — float("nan") and float("inf")
    # pass float() without error but cause downstream issues.
    # ════════════════════════════════════════════════════
    def safe_float(self, entity, fallback):
        state = self.get_state(entity)
        if state in (None, "unavailable", "unknown"):
            return fallback
        try:
            val = float(state)
        except (ValueError, TypeError):
            self.log(f"WARNING: Invalid value: {entity}={state}",
                    level="WARNING")
            return fallback
        if not math.isfinite(val):
            self.log(f"WARNING: Non-finite value: {entity}={state}",
                    level="WARNING")
            return fallback
        return val

    # ════════════════════════════════════════════════════
    # SAFE FLOAT OR NONE
    # Like safe_float, but returns None instead of a fallback when the
    # sensor is unavailable, non-numeric, or non-finite.
    #
    # Use this for the DB-write path: a sensor outage must write NULL to
    # the database, not a fabricated 0 (which would corrupt AVG/MIN/MAX
    # queries and the humidity-drop / temperature-stability analytics in
    # DATABASE.md). See ADR-010.
    # ════════════════════════════════════════════════════
    def safe_float_or_none(self, entity):
        state = self.get_state(entity)
        if state in (None, "unavailable", "unknown"):
            return None
        try:
            val = float(state)
        except (ValueError, TypeError):
            self.log(f"WARNING: Invalid value (DB path): {entity}={state}",
                    level="WARNING")
            return None
        if not math.isfinite(val):
            self.log(f"WARNING: Non-finite value (DB path): {entity}={state}",
                    level="WARNING")
            return None
        return val

    # ════════════════════════════════════════════════════
    # SAFE SERVICE - absorbs HA hiccups
    # ════════════════════════════════════════════════════
    def safe_service(self, service, **kwargs):
        try:
            self.call_service(service, **kwargs)
        except Exception as e:
            self.log(f"WARNING: {service} failed: {e}",
                    level="WARNING")

    # ════════════════════════════════════════════════════
    # SAFE IS_ON - tri-state switch check
    # Returns:
    #   True   when switch is reliably "on"
    #   False  when switch is reliably "off"
    #   None   when switch is "unavailable"/"unknown"/None
    #
    # "unavailable" must never be silently treated as "off".
    # ════════════════════════════════════════════════════
    def safe_is_on(self, entity):
        state = self.get_state(entity)
        if state in (None, "unavailable", "unknown"):
            return None
        return state == "on"

    # ════════════════════════════════════════════════════
    # READ TEMP — VALIDATED  (shared by both regulators)
    # Single source of truth for "is the temperature usable right now?".
    #
    # Returns the temperature as float on success, or None on any failure
    # (unavailable / non-numeric / non-finite / out of plausible range).
    #
    # Why a shared helper:
    #   - Pattern 2 in PATTERNS.md requires an explicit get_state() guard
    #     before any safe_float() call for safety-critical reads. Doing
    #     this in two places duplicated code and risked drift — and in
    #     fact regulate_heater used to call safe_float() with
    #     DEFAULT_TARGET_TEMP as fallback after its guard, which would
    #     have silently injected a 65°C reading on a race condition.
    #   - regulate_fan validated the temperature range (0–150°C);
    #     regulate_heater did not. Symmetry break — both regulators read
    #     the same sensor and must apply the same plausibility check.
    #
    # This helper does NOT log "unavailable" — the caller decides whether
    # the situation warrants a WARNING (regulate_fan increments the safe-
    # mode counter; regulate_heater just skips one tick).
    # See ADR-009.
    # ════════════════════════════════════════════════════
    def _read_temp_validated(self):
        state = self.get_state(self.e["temp"])
        if state in (None, "unavailable", "unknown"):
            return None
        try:
            temp = float(state)
        except (ValueError, TypeError):
            self.log(f"WARNING: Temp sensor non-numeric: {state}",
                    level="WARNING")
            return None
        if not math.isfinite(temp):
            self.log(f"WARNING: Temp sensor non-finite: {state}",
                    level="WARNING")
            return None
        if not TEMP_MIN_VALID < temp < TEMP_MAX_VALID:
            self.log(
                f"WARNING: Temp out-of-range ({temp:.1f}C) - "
                f"valid window is {TEMP_MIN_VALID}-{TEMP_MAX_VALID}C",
                level="WARNING"
            )
            return None
        return temp

    # ════════════════════════════════════════════════════
    # CASCADE CONTROL  (Layer 1)
    # Computes v_min/v_max window for the PID
    # based on current chamber temperature.
    # Hysteresis prevents flapping at zone boundaries.
    # ════════════════════════════════════════════════════
    def compute_cascade(self, temp, v_min_basis, v_max_basis):
        zone_prev = self.cascade_zone
        h         = CASCADE_HYSTERESIS

        if zone_prev is None:
            # First call: determine zone directly, no hysteresis
            if temp < CASCADE_COLD_UNTIL:
                zone = "cold"
            elif temp < CASCADE_GREEN_UNTIL:
                zone = "green"
            elif temp < CASCADE_WARN_UNTIL:
                zone = "warn"
            else:
                zone = "panic_hot"
            self.log(f"Cascade: start zone={zone} | Temp={temp:.1f}C")

        elif zone_prev == "cold":
            zone = "green" if temp >= CASCADE_COLD_UNTIL else "cold"

        elif zone_prev == "green":
            if temp < (CASCADE_COLD_UNTIL - h):
                zone = "cold"
            elif temp >= CASCADE_GREEN_UNTIL:
                zone = "warn"
            else:
                zone = "green"

        elif zone_prev == "warn":
            if temp < (CASCADE_GREEN_UNTIL - h):
                zone = "green"
            elif temp >= CASCADE_WARN_UNTIL:
                zone = "panic_hot"
            else:
                zone = "warn"

        else:  # panic_hot
            zone = "warn" if temp < (CASCADE_WARN_UNTIL - h) else "panic_hot"

        # Modify v_min/v_max per zone
        if zone == "cold":
            # Pre-heat: limit fan to retain heat, heater does the work
            v_min = v_min_basis
            v_max = min(v_max_basis, CASCADE_COLD_VMAX)
        elif zone == "green":
            # Optimal drying range: full PID range
            v_min = v_min_basis
            v_max = v_max_basis
        elif zone == "warn":
            # Getting hot: preemptively increase airflow
            v_min = max(v_min_basis, CASCADE_WARN_VMIN)
            v_max = v_max_basis
        else:  # panic_hot
            # Too hot: maximum cooling, overrides basis
            v_min = max(v_min_basis, CASCADE_PANIC_HOT_VMIN)
            v_max = CASCADE_PANIC_HOT_VMAX

        # Safety clamp
        if v_min > v_max:
            self.log(
                f"WARNING: Cascade v_min({v_min}) > v_max({v_max}) -> clamping to v_max",
                level="WARNING"
            )
            v_min = v_max

        if zone != zone_prev and zone_prev is not None:
            self.log(f"Cascade: zone changed {zone_prev} -> {zone} | Temp={temp:.1f}C")

        self.log(
            f"Cascade: zone={zone} | Temp={temp:.1f}C | v_min={v_min} | v_max={v_max}"
        )

        self.cascade_zone  = zone
        self.v_min_active  = v_min
        self.v_max_active  = v_max

        return zone, v_min, v_max

    # ════════════════════════════════════════════════════
    # FAN REGULATION – three layers
    #   Layer 1: Cascade (v_min/v_max window by temperature)
    #   Layer 2: Temp-PID (control within the window)
    #   Layer 3: Temp-Override (hard safety net)
    # ════════════════════════════════════════════════════
    def regulate_fan(self, entity, attribute, old, new, kwargs):

        fan = self.e["fan"]

        # ── Temp sensor guard ──────────────────────────────
        # _read_temp_validated() returns None for unavailable / non-numeric /
        # non-finite / out-of-plausible-range readings (the latter two are
        # treated as hard sensor failures — they should not control hardware).
        # Logs are emitted inside the helper for non-numeric / non-finite /
        # out-of-range; the bare "unavailable" case is silent because it's
        # the most common transient and the safe-mode escalation below logs
        # it explicitly once the counter rises.
        temp = self._read_temp_validated()
        if temp is None:
            self.sensor_fail_count += 1
            if self.sensor_fail_count >= 3:
                self.log("WARNING: Temp sensor failed 3x -> safe mode",
                        level="WARNING")
                # Notification idempotency: post the HA notification only on
                # the *transition* into the notification-active state. Using
                # _notification_active (not sensor_fail_count == 3) means the
                # dismiss path will never fire for a notification that was
                # never successfully posted, and a flaky create() can be
                # retried on the next tick instead of being missed forever.
                if not self._notification_active:
                    self.safe_service("persistent_notification/create",
                                    notification_id="dryer_temp_fail",
                                    title="Filament Dryer Alert",
                                    message="Temp sensor failed - safe mode: fan 50%, heater off")
                    self._notification_active = True
                self.pid.reset()
                self.safe_service("fan/set_percentage",
                                entity_id=fan,
                                percentage=50)
                self.safe_service("switch/turn_off",
                                entity_id=self.e["heater"])
                # Safe mode means the PID is not running — record 0.0 so the
                # diagnostic pid_output column in the DB doesn't fabricate a
                # plausible-looking 50% PID request (which would alias to a
                # normal tick under the override_delta query in DATABASE.md).
                self.last_pid_output = 0.0
            return

        # Sensor back after failure — dismiss only if we actually posted.
        if self._notification_active:
            self.safe_service("persistent_notification/dismiss",
                            notification_id="dryer_temp_fail")
            self._notification_active = False
        self.sensor_fail_count = 0

        target_temp = self.safe_float(self.e["target_temp"], DEFAULT_TARGET_TEMP)
        v_min_basis = self.safe_float(self.e["fan_min"],     DEFAULT_FAN_MIN)
        v_max_basis = self.safe_float(self.e["fan_max"],     DEFAULT_FAN_MAX)

        # ── Layer 1: Cascade ──────────────────────────────
        zone, v_min, v_max = self.compute_cascade(temp, v_min_basis, v_max_basis)

        # Always update PID limits, even when override fires below
        self.pid.v_min = v_min
        self.pid.v_max = v_max

        # ── Layer 3: Hard temp-override ───────────────────
        temp_threshold = target_temp + OVERRIDE_SOFT_OFFSET
        temp_max       = target_temp + OVERRIDE_HARD_OFFSET

        if temp >= temp_max:
            # Directly to 100% — intentionally ignores cascade limits
            self.pid.reset()
            self.safe_service("fan/set_percentage",
                            entity_id=fan,
                            percentage=100)
            self.log(f"COOLING EMERGENCY: Temp={temp:.1f}C -> 100%")
            return

        # ── Layer 2: Temp-PID ─────────────────────────────
        pid_speed = self.pid.update(temp, target_temp)

        # ── Layer 3: Soft temp-override (linear blend) ────
        # PID integral freeze on override entry — while the override
        # pushes the fan up, the PID integral keeps accumulating.
        # After override ends the PID overshoots. Fix: reset once on entry,
        # use _temp_override_active flag to prevent double resets.
        # Comparison is `>=` to match the hard-override boundary
        # convention (both fail-safe paths fire on equality).
        if temp >= temp_threshold:
            if not self._temp_override_active:
                self.pid.reset()
                self._temp_override_active = True
                self.log(f"Temp override: entry (Temp={temp:.1f}C) -> PID reset")
            blend = (temp - temp_threshold) / OVERRIDE_BLEND_RANGE
            blend = max(0.0, min(1.0, blend))
            speed = pid_speed + blend * (v_max - pid_speed)
        else:
            if self._temp_override_active:
                self._temp_override_active = False
                self.log(f"Temp override: exit (Temp={temp:.1f}C) -> normal PID")
            speed = pid_speed

        speed = int(max(v_min, min(v_max, speed)))
        self.last_pid_output = round(pid_speed, 3)

        self.safe_service("fan/set_percentage",
                        entity_id=fan,
                        percentage=speed)
        self.log(f"Fan: Temp={temp:.1f}C | target={target_temp} | "
                f"PID={pid_speed:.1f}% | speed={speed}% | zone={zone}")

    # ════════════════════════════════════════════════════
    # HEATER REGULATION
    # Simple hysteresis control — heater on when below target,
    # off when above. PID handles the fan; heater is binary.
    # ════════════════════════════════════════════════════
    def regulate_heater(self, entity, attribute, old, new, kwargs):

        # Single source of truth for temperature validation. Previously this
        # function had its own ad-hoc guard followed by a safe_float() call
        # that fell back to DEFAULT_TARGET_TEMP — meaning a race condition
        # between the guard and the fallback read could have silently
        # injected a 65°C "reading" into hysteresis comparisons. The shared
        # helper performs the read once and applies the same plausibility
        # range as regulate_fan, so both regulators react to identical
        # validation rules.
        temp = self._read_temp_validated()
        if temp is None:
            self.log("WARNING: Temp sensor unusable -> heater unchanged",
                    level="WARNING")
            return

        target       = self.safe_float(self.e["target_temp"], DEFAULT_TARGET_TEMP)
        hysteresis   = self.safe_float(self.e["heater_hysteresis"], 1.0)
        currently_on = self.safe_is_on(self.e["heater"])

        if currently_on is None:
            self.log("WARNING: Heater status unclear - skipping", level="WARNING")
            return

        if temp < (target - hysteresis) and not currently_on:
            self.safe_service("switch/turn_on",
                            entity_id=self.e["heater"])
            self.log(f"Heater ON: {temp:.1f}C < {target - hysteresis:.1f}C")

        elif temp > (target + hysteresis) and currently_on:
            self.safe_service("switch/turn_off",
                            entity_id=self.e["heater"])
            self.log(f"Heater OFF: {temp:.1f}C > {target + hysteresis:.1f}C")

    # ════════════════════════════════════════════════════
    # DATABASE
    # ════════════════════════════════════════════════════
    def db_connect(self):
        db = self.db_cfg
        return pymysql.connect(
            host=db["host"],
            port=db.get("port", 3306),
            user=db["user"],
            password=db["password"],
            database=db["database"]
        )

    def db_save(self, kwargs):
        try:
            # safe_float_or_none: unavailable / non-numeric / non-finite all
            # write SQL NULL rather than a fabricated 0. This preserves the
            # correctness of AVG/MIN/MAX queries in DATABASE.md — a sensor
            # outage no longer drags reported averages toward zero and no
            # longer registers as a fake "chamber cooled to 0°C" event.
            # Mirrors what safe_is_on already does for the heater column.
            temp     = self.safe_float_or_none(self.e["temp"])
            humidity = self.safe_float_or_none(self.e["humidity"])
            # safe_is_on: True/False/None — unavailable writes NULL, not False
            heater   = self.safe_is_on(self.e["heater"])

            brightness = self.get_state(self.e["fan"], attribute="percentage")
            try:
                speed = int(float(brightness)) if brightness is not None else 0
            except (ValueError, TypeError):
                speed = 0

            zone_str = self.cascade_zone if self.cascade_zone is not None else "unknown"

            with self.db_connect() as conn:
                with conn.cursor() as cursor:
                    try:
                        cursor.execute('''
                            INSERT INTO measurements
                            (temp, humidity, fan_speed, pid_output,
                             kp, ki, kd, heater,
                             cascade_zone, v_min_active, v_max_active)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (
                            temp, humidity, speed,
                            self.last_pid_output,
                            self.pid.kp, self.pid.ki, self.pid.kd,
                            heater,
                            zone_str,
                            self.v_min_active,
                            self.v_max_active
                        ))
                    except (pymysql.err.OperationalError,
                            pymysql.err.ProgrammingError) as col_err:
                        # Fallback to old schema if cascade columns missing (errno 1054)
                        errno = col_err.args[0] if col_err.args else None
                        if errno != 1054:
                            raise
                        self.log(
                            f"WARNING: Cascade columns missing, using old schema "
                            f"(ALTER TABLE pending): {col_err}",
                            level="WARNING"
                        )
                        cursor.execute('''
                            INSERT INTO measurements
                            (temp, humidity, fan_speed, pid_output,
                             kp, ki, kd, heater)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (
                            temp, humidity, speed,
                            self.last_pid_output,
                            self.pid.kp, self.pid.ki, self.pid.kd,
                            heater
                        ))
                conn.commit()
            self.log("DB: measurement saved")

        except Exception as e:
            self.log(f"ERROR: DB error: {e}", level="ERROR")
