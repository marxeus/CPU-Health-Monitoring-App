# CPU Health Monitor

A desktop CPU monitor with a modern dark GUI (ttkbootstrap), a live rolling
chart, configurable Warning/Danger thresholds, a one-click load simulator
for testing, and PostgreSQL logging of every time the CPU hits a dangerous
level.

## Features

- **Live gauge + chart** — current CPU % (color-coded green/orange/red) and
  a 60-second rolling line chart, refreshed every second.
- **Configurable thresholds** — sliders for the Warning (default 80%) and
  Danger (default 95%) levels.
- **Alerts** — a banner changes color the moment you cross a threshold, and
  a popup fires when you hit Danger.
- **"Simulate 100% Load" button** — spins up one CPU-bound worker process
  per core for 10 seconds so you can actually watch usage spike and confirm
  alerting/logging works, without needing to stress your machine some other
  way. "Stop Simulation" ends it early.
- **PostgreSQL logging** — every time the CPU enters WARNING or DANGER, a
  row with the date/time, CPU %, level, and whether it was a simulated
  spike is written to a `cpu_alerts` table. Sustained DANGER periods get a
  fresh row every 60 seconds so long incidents leave a full timestamp
  trail. Use "View Alert History" in the app to browse logged alerts.

## Setup (Windows / PowerShell)

1. **Install PostgreSQL** if you don't already have it (get it from
   postgresql.org — the installer bundles pgAdmin, which is the easiest way
   to do step 2 on Windows).

2. **Create the database.** PowerShell doesn't have `createdb` on PATH by
   default, so either:
   - Open **pgAdmin** → right-click *Databases* → *Create* → *Database...*
     → name it `cpu_monitor`, or
   - Add `C:\Program Files\PostgreSQL\<version>\bin` to your PATH and run
     `createdb -U postgres cpu_monitor` from PowerShell.

   You only need the *database* to exist — the app auto-creates the
   `cpu_alerts` table itself on first successful connection, so you don't
   need to run `schema.sql` unless you prefer to set the table up by hand.

3. **Install Python dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```
   `tkinter` ships with the standard Python Windows installer, so no extra
   step is needed there.

4. **Configure your DB credentials.** Copy `.env.example` to `.env` in the
   project folder and fill in your values:
   ```powershell
   copy .env.example .env
   ```
   Then edit `.env` with a text editor:
   ```
   DB_HOST=localhost
   DB_PORT=5432
   DB_NAME=cpu_monitor
   DB_USER=postgres
   DB_PASSWORD=postgres
   ```
   `db.py` loads this file automatically on startup via `python-dotenv` —
   no need to `export`/`$env:` anything by hand, and it persists across
   terminal sessions. If you skip this step the app still runs — it just
   shows "PostgreSQL: disconnected" in the header and skips logging until
   you hit "Retry DB Connection".

   (If you'd rather not use a `.env` file, PowerShell's equivalent of
   `export` is `$env:DB_HOST="localhost"` etc. — but that only lasts for
   the current terminal window.)

5. **Run it**:
   ```powershell
   python cpu_monitor.py
   ```

## Files

| File              | Purpose                                              |
|-------------------|-------------------------------------------------------|
| `cpu_monitor.py`  | Main GUI app — monitoring loop, alerts, simulation.   |
| `db.py`           | PostgreSQL connection pool + insert/fetch helpers.    |
| `schema.sql`      | Optional manual table setup (app also auto-creates it)|
| `requirements.txt`| Python dependencies.                                  |
| `.env.example`    | Template for DB connection environment variables.     |

## How the alert logging works

- The monitor samples CPU usage once per second.
- Crossing from OK → WARNING or WARNING/OK → DANGER logs one row
  immediately (edge-triggered).
- If usage stays at DANGER continuously, a new row is logged every 60
  seconds so you get a timestamped trail of a sustained spike, not just the
  first moment.
- Rows made while the "Simulate 100% Load" button is active are flagged
  `is_simulated = TRUE` so you can tell real incidents apart from tests.

## Customizing

- Change `POLL_INTERVAL_MS`, `HISTORY_LEN`, `SIMULATION_SECONDS`, or
  `SUSTAINED_LOG_INTERVAL_S` near the top of `cpu_monitor.py` to tune
  behavior.
- Change the ttkbootstrap theme by editing `themename="darkly"` in `main()`
  — other options include `"cyborg"`, `"superhero"`, `"flatly"`, `"litera"`,
  etc.
