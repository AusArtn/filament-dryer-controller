# Database Setup & Queries

MariaDB schema, migration scripts, and useful analysis queries.

---

## Initial schema

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

Verify after creation:
```sql
DESCRIBE measurements;
```

### Note on NULL values (since v1.1)

Sensor outages (`temp`, `humidity`, `heater`) write SQL `NULL` rather
than a fabricated `0` / `false`. Aggregate functions
(`AVG`, `MIN`, `MAX`, `STDDEV`, `COUNT(col)`) skip `NULL` naturally,
so the queries below work correctly across outage gaps.

If you want to detect outages explicitly:
```sql
SELECT COUNT(*) AS outage_ticks
FROM measurements
WHERE timestamp >= NOW() - INTERVAL 1 DAY
  AND temp IS NULL;
```

The `cascade_zone` column never writes `NULL`; it writes the literal
string `"unknown"` until the first valid tick determines a zone.

---

## Schema migration (adding cascade columns to existing base schema)

```sql
ALTER TABLE measurements
    ADD COLUMN cascade_zone  VARCHAR(20) AFTER heater,
    ADD COLUMN v_min_active  FLOAT       AFTER cascade_zone,
    ADD COLUMN v_max_active  FLOAT       AFTER v_min_active;
```

---

## Analysis queries

### Recent measurements

```sql
SELECT timestamp, temp, humidity, fan_speed, pid_output, cascade_zone, heater
FROM measurements
ORDER BY timestamp DESC
LIMIT 50;
```

### Drying session — temperature and humidity trend

```sql
-- Adjust the time range to your drying session
SELECT
    timestamp,
    temp,
    humidity,
    fan_speed,
    heater
FROM measurements
WHERE timestamp >= NOW() - INTERVAL 8 HOUR
ORDER BY timestamp ASC;
```

### Humidity drop rate (drying progress)

```sql
SELECT
    MIN(humidity) AS humidity_min,
    MAX(humidity) AS humidity_max,
    MAX(humidity) - MIN(humidity) AS total_drop,
    MIN(timestamp) AS session_start,
    MAX(timestamp) AS session_end
FROM measurements
WHERE timestamp >= NOW() - INTERVAL 8 HOUR;
```

### Temperature stability (PID tuning quality)

```sql
-- Standard deviation of temp — lower is better
SELECT
    AVG(temp)                                          AS avg_temp,
    SQRT(AVG(POW(temp - (SELECT AVG(temp)
         FROM measurements
         WHERE timestamp >= NOW() - INTERVAL 2 HOUR), 2))) AS stddev_temp,
    MIN(temp)                                          AS min_temp,
    MAX(temp)                                          AS max_temp
FROM measurements
WHERE timestamp >= NOW() - INTERVAL 2 HOUR;
```

### Cascade zone distribution

```sql
SELECT
    cascade_zone,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM measurements
WHERE timestamp >= NOW() - INTERVAL 8 HOUR
GROUP BY cascade_zone
ORDER BY count DESC;
```

### PID vs actual fan speed (detect override activity)

```sql
SELECT
    timestamp,
    temp,
    pid_output,
    fan_speed,
    fan_speed - pid_output AS override_delta,
    cascade_zone
FROM measurements
WHERE ABS(fan_speed - pid_output) > 5
  AND timestamp >= NOW() - INTERVAL 8 HOUR
ORDER BY override_delta DESC
LIMIT 20;
```

### Heater duty cycle

```sql
SELECT
    SUM(heater = 1) AS cycles_on,
    SUM(heater = 0) AS cycles_off,
    ROUND(SUM(heater = 1) * 100.0 / COUNT(*), 1) AS duty_pct
FROM measurements
WHERE timestamp >= NOW() - INTERVAL 2 HOUR;
```

### DB health check

```sql
SELECT
    MIN(timestamp) AS oldest,
    MAX(timestamp) AS newest,
    COUNT(*)       AS total_rows
FROM measurements;
```

---

## Cleanup

```sql
-- Keep 90 days of data
DELETE FROM measurements
WHERE timestamp < NOW() - INTERVAL 90 DAY;
```
