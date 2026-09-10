-- Run this manually if you'd rather set up the table yourself instead of
-- letting the app create it automatically on first connect.
--
--   createdb cpu_monitor
--   psql -d cpu_monitor -f schema.sql

CREATE TABLE IF NOT EXISTS cpu_alerts (
    id SERIAL PRIMARY KEY,
    alert_time TIMESTAMP NOT NULL DEFAULT NOW(),
    cpu_percent REAL NOT NULL,
    level VARCHAR(20) NOT NULL,          -- 'WARNING' or 'DANGER'
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_cpu_alerts_time ON cpu_alerts (alert_time DESC);
