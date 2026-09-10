"""
cpu_monitor.py - Modern CPU health monitor with PostgreSQL alert logging.

Features
--------
- Live CPU usage readout with a color-coded gauge and rolling line chart.
- Configurable Warning / Danger thresholds.
- Visual + popup alerts when usage crosses those thresholds.
- A "Simulate 100% Load" button that actually pegs every CPU core for a
  set duration, so you can test the alerting/logging path end to end.
- Every time the CPU enters WARNING or DANGER territory, the date/time,
  cpu%, level, and whether it was a simulated spike is written to a
  PostgreSQL table (see db.py). Sustained danger periods log a fresh
  row every 60s so long incidents leave a full timestamp trail.

Run:
    python cpu_monitor.py
"""

import multiprocessing
import time
from collections import deque
from datetime import datetime

import psutil
import ttkbootstrap as tb
from ttkbootstrap.constants import *  # noqa: F401,F403 - style constants (SUCCESS, WARNING, etc.)
from tkinter import messagebox, ttk

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from db import DBLogger

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
POLL_INTERVAL_MS = 1000          # how often we sample CPU usage
HISTORY_LEN = 60                 # samples kept for the chart (1/sec => 60s window)
DEFAULT_WARNING = 80             # %
DEFAULT_DANGER = 95              # %
SUSTAINED_LOG_INTERVAL_S = 60    # re-log while continuously in DANGER
SIMULATION_SECONDS = 10          # default simulated-load duration

LEVEL_OK = "OK"
LEVEL_WARNING = "WARNING"
LEVEL_DANGER = "DANGER"


def _burn_cpu(stop_at: float):
    """Busy-loop worker used to simulate heavy CPU load. Runs in its own process."""
    while time.time() < stop_at:
        pass


class CPUMonitorApp:
    def __init__(self, root: tb.Window):
        self.root = root
        self.root.title("CPU Health Monitor")
        self.root.geometry("900x680")
        self.root.minsize(820, 620)

        # ---- state ----
        self.history = deque([0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self.warning_threshold = tb.IntVar(value=DEFAULT_WARNING)
        self.danger_threshold = tb.IntVar(value=DEFAULT_DANGER)
        self.current_level = LEVEL_OK
        self.last_sustained_log_time = 0.0
        self.simulation_processes = []
        self.simulation_stop_at = 0.0
        self.simulating = False

        self.db = DBLogger()

        self._build_layout()
        self._update_db_status_label()

        psutil.cpu_percent(interval=None)  # prime the internal sampler
        self.root.after(POLL_INTERVAL_MS, self._poll_cpu)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_layout(self):
        outer = tb.Frame(self.root, padding=20)
        outer.pack(fill=BOTH, expand=YES)

        # Header
        header = tb.Frame(outer)
        header.pack(fill=X, pady=(0, 16))
        title_col = tb.Frame(header)
        title_col.pack(side=LEFT)
        tb.Label(
            title_col, text="🖥  CPU Health Monitor",
            font=("Segoe UI", 22, "bold"),
        ).pack(anchor=W)
        tb.Label(
            title_col, text="Live system monitoring & alerting",
            font=("Segoe UI", 10), bootstyle=SECONDARY,
        ).pack(anchor=W)

        self.db_status_label = tb.Label(
            header, text="", font=("Segoe UI", 10, "bold"),
            padding=(12, 6),
        )
        self.db_status_label.pack(side=RIGHT, anchor=CENTER)

        # Alert banner (hidden until needed)
        self.alert_banner = tb.Label(
            outer, text="", font=("Segoe UI", 13, "bold"),
            anchor=CENTER, padding=12, bootstyle=INVERSE + SUCCESS,
        )
        self.alert_banner.pack(fill=X, pady=(0, 16))
        self._set_banner_ok()

        # Gauge card: circular meter + per-core readout, side by side with the chart
        top_row = tb.Frame(outer)
        top_row.pack(fill=BOTH, expand=YES, pady=(0, 16))

        gauge_card = tb.Labelframe(top_row, text="Current Usage", padding=16)
        gauge_card.pack(side=LEFT, fill=Y, padx=(0, 16))

        self.meter = tb.Meter(
            gauge_card,
            metersize=190,
            amountused=0,
            amounttotal=100,
            subtext="CPU USAGE",
            textright="%",
            bootstyle=SUCCESS,
            stripethickness=10,
            interactive=False,
            subtextstyle=SECONDARY,
        )
        self.meter.pack(pady=(4, 12))
        self.per_core_label = tb.Label(
            gauge_card, text="", font=("Segoe UI", 9), justify=CENTER,
            bootstyle=SECONDARY,
        )
        self.per_core_label.pack()

        # Chart card
        chart_frame = tb.Labelframe(top_row, text="Last 60 seconds", padding=12)
        chart_frame.pack(side=LEFT, fill=BOTH, expand=YES)

        self.figure = Figure(figsize=(5, 2.6), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self._style_axes()
        (self.line,) = self.ax.plot(range(HISTORY_LEN), list(self.history), color="#2ecc71", linewidth=2.2)
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill=BOTH, expand=YES)

        # Threshold controls
        threshold_frame = tb.Labelframe(outer, text="Alert Thresholds", padding=16)
        threshold_frame.pack(fill=X, pady=(0, 16))

        self._build_threshold_row(threshold_frame, "Warning at:", self.warning_threshold, WARNING, 0)
        self._build_threshold_row(threshold_frame, "Danger at:", self.danger_threshold, DANGER, 1)

        # Buttons
        button_frame = tb.Frame(outer)
        button_frame.pack(fill=X, pady=(0, 8))

        self.simulate_btn = tb.Button(
            button_frame, text=f"⚡  Simulate 100% Load ({SIMULATION_SECONDS}s)",
            bootstyle=DANGER, command=self.start_simulation,
        )
        self.simulate_btn.pack(side=LEFT, padx=(0, 10), ipadx=4, ipady=2)

        self.stop_sim_btn = tb.Button(
            button_frame, text="■  Stop Simulation", bootstyle=(SECONDARY, OUTLINE),
            command=self.stop_simulation, state=DISABLED,
        )
        self.stop_sim_btn.pack(side=LEFT, padx=(0, 10), ipadx=4, ipady=2)

        tb.Button(
            button_frame, text="📜  View Alert History", bootstyle=(INFO, OUTLINE),
            command=self.show_history,
        ).pack(side=LEFT, padx=(0, 10), ipadx=4, ipady=2)

        tb.Button(
            button_frame, text="↻  Retry DB Connection", bootstyle=(SECONDARY, OUTLINE),
            command=self.retry_db,
        ).pack(side=LEFT, ipadx=4, ipady=2)

        # Status bar
        tb.Separator(outer).pack(fill=X, pady=(4, 8))
        self.status_label = tb.Label(
            outer, text="Ready.", font=("Segoe UI", 9), bootstyle=SECONDARY,
        )
        self.status_label.pack(fill=X)

    def _build_threshold_row(self, parent, label_text, var, style, row):
        tb.Label(parent, text=label_text, width=12, font=("Segoe UI", 10)).grid(
            row=row, column=0, sticky=W, pady=4
        )
        # The scale drags a separate raw (float) variable. Binding the scale
        # directly to the whole-number `var` causes a feedback loop (drag ->
        # command rounds & rewrites var -> scale re-syncs -> command fires
        # again), which is what made the slider laggy and inconsistently
        # rounded. Keeping them separate avoids that entirely.
        raw_var = tb.DoubleVar(value=var.get())
        scale = tb.Scale(
            parent, from_=0, to=100, orient=HORIZONTAL, variable=raw_var,
            bootstyle=style, length=400,
            command=lambda v, var=var: var.set(round(float(v))),
        )
        scale.grid(row=row, column=1, sticky=EW, padx=10)
        value_label = tb.Label(parent, textvariable=var, width=4, font=("Segoe UI", 10, "bold"))
        value_label.grid(row=row, column=2, sticky=W)
        parent.columnconfigure(1, weight=1)

        # Keep a reference so raw_var isn't garbage-collected out from under the widget.
        self._threshold_raw_vars = getattr(self, "_threshold_raw_vars", [])
        self._threshold_raw_vars.append(raw_var)

    def _style_axes(self):
        bg = self.root.style.colors.bg if hasattr(self, "root") else "#22303e"
        fg = self.root.style.colors.fg if hasattr(self, "root") else "#ffffff"
        self.figure.patch.set_facecolor(bg)
        self.ax.set_facecolor(bg)
        self.ax.set_ylim(0, 100)
        self.ax.set_xlim(0, HISTORY_LEN - 1)
        self.ax.set_xticks([])
        self.ax.set_ylabel("CPU %", color=fg, fontsize=9)
        self.ax.tick_params(colors=fg, labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color(fg)
            spine.set_alpha(0.25)
        self.ax.grid(True, alpha=0.15, color=fg)
        self.figure.tight_layout()

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------
    def _poll_cpu(self):
        cpu_pct = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)

        self.history.append(cpu_pct)
        self._refresh_gauge(cpu_pct)
        self._refresh_chart()
        self._refresh_per_core(per_core)
        self._evaluate_alert_state(cpu_pct)

        self.root.after(POLL_INTERVAL_MS, self._poll_cpu)

    def _refresh_gauge(self, cpu_pct):
        self.meter.configure(amountused=round(cpu_pct))

        if cpu_pct >= self.danger_threshold.get():
            style = DANGER
        elif cpu_pct >= self.warning_threshold.get():
            style = WARNING
        else:
            style = SUCCESS
        self.meter.configure(bootstyle=style)

    def _refresh_chart(self):
        ys = list(self.history)
        self.line.set_ydata(ys)
        if max(ys) >= self.danger_threshold.get():
            self.line.set_color("#e74c3c")
        elif max(ys) >= self.warning_threshold.get():
            self.line.set_color("#f39c12")
        else:
            self.line.set_color("#2ecc71")
        self.canvas.draw_idle()

    def _refresh_per_core(self, per_core):
        text = "\n".join(
            "  ".join(f"C{i}: {v:.0f}%" for i, v in group)
            for group in (list(enumerate(per_core))[i:i + 4] for i in range(0, len(per_core), 4))
        )
        self.per_core_label.config(text=text)

    # ------------------------------------------------------------------
    # Alerting + DB logging
    # ------------------------------------------------------------------
    def _evaluate_alert_state(self, cpu_pct):
        danger = self.danger_threshold.get()
        warning = self.warning_threshold.get()

        if cpu_pct >= danger:
            new_level = LEVEL_DANGER
        elif cpu_pct >= warning:
            new_level = LEVEL_WARNING
        else:
            new_level = LEVEL_OK

        now = time.time()
        entered_new_level = new_level != self.current_level

        if entered_new_level:
            self.current_level = new_level
            if new_level == LEVEL_DANGER:
                self._set_banner_danger(cpu_pct)
                self._log_alert(cpu_pct, LEVEL_DANGER)
                self.last_sustained_log_time = now
                self._popup_alert(cpu_pct, LEVEL_DANGER)
            elif new_level == LEVEL_WARNING:
                self._set_banner_warning(cpu_pct)
                self._log_alert(cpu_pct, LEVEL_WARNING)
            else:
                self._set_banner_ok()
        else:
            # Still in the same level - update banner text with the latest %,
            # and re-log periodically if we're stuck in DANGER for a long stretch.
            if new_level == LEVEL_DANGER:
                self._set_banner_danger(cpu_pct)
                if now - self.last_sustained_log_time >= SUSTAINED_LOG_INTERVAL_S:
                    self._log_alert(cpu_pct, LEVEL_DANGER)
                    self.last_sustained_log_time = now
            elif new_level == LEVEL_WARNING:
                self._set_banner_warning(cpu_pct)

    def _log_alert(self, cpu_pct, level):
        row_id = self.db.log_alert(cpu_pct, level, is_simulated=self.simulating)
        self._update_db_status_label()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if row_id is not None:
            self.status_label.config(
                text=f"[{stamp}] Logged {level} alert at {cpu_pct:.0f}% to PostgreSQL (id={row_id})."
            )
        else:
            reason = self.db.last_error() or "not connected"
            self.status_label.config(
                text=f"[{stamp}] {level} alert at {cpu_pct:.0f}% NOT logged - DB {reason}."
            )

    def _popup_alert(self, cpu_pct, level):
        messagebox.showwarning(
            "CPU Danger Level Reached",
            f"CPU usage hit {cpu_pct:.0f}%, at or above your danger threshold "
            f"of {self.danger_threshold.get()}%.\n\nThis has been logged to PostgreSQL.",
        )

    def _set_banner_ok(self):
        self.alert_banner.configure(text="✓ CPU usage is normal.", bootstyle=INVERSE + SUCCESS)

    def _set_banner_warning(self, cpu_pct):
        self.alert_banner.configure(
            text=f"⚠ WARNING: CPU usage at {cpu_pct:.0f}% - approaching danger levels.",
            bootstyle=INVERSE + WARNING,
        )

    def _set_banner_danger(self, cpu_pct):
        self.alert_banner.configure(
            text=f"🔥 DANGER: CPU usage at {cpu_pct:.0f}%! Threshold exceeded.",
            bootstyle=INVERSE + DANGER,
        )

    # ------------------------------------------------------------------
    # Load simulation
    # ------------------------------------------------------------------
    def start_simulation(self):
        if self.simulating:
            return
        self.simulating = True
        self.simulate_btn.configure(state=DISABLED)
        self.stop_sim_btn.configure(state=NORMAL)

        core_count = multiprocessing.cpu_count()
        stop_at = time.time() + SIMULATION_SECONDS
        self.simulation_stop_at = stop_at

        self.simulation_processes = []
        for _ in range(core_count):
            p = multiprocessing.Process(target=_burn_cpu, args=(stop_at,), daemon=True)
            p.start()
            self.simulation_processes.append(p)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.config(
            text=f"[{stamp}] Simulating full load across {core_count} core(s) for {SIMULATION_SECONDS}s..."
        )
        self.root.after(500, self._check_simulation_progress)

    def _check_simulation_progress(self):
        if not self.simulating:
            return
        if time.time() >= self.simulation_stop_at or not any(p.is_alive() for p in self.simulation_processes):
            self._finish_simulation()
        else:
            self.root.after(500, self._check_simulation_progress)

    def stop_simulation(self):
        for p in self.simulation_processes:
            if p.is_alive():
                p.terminate()
        self._finish_simulation()

    def _finish_simulation(self):
        for p in self.simulation_processes:
            p.join(timeout=1)
        self.simulation_processes = []
        self.simulating = False
        self.simulate_btn.configure(state=NORMAL)
        self.stop_sim_btn.configure(state=DISABLED)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.config(text=f"[{stamp}] Simulation ended.")

    # ------------------------------------------------------------------
    # DB status / history window
    # ------------------------------------------------------------------
    def _update_db_status_label(self):
        if self.db.connected:
            self.db_status_label.configure(text="● PostgreSQL: Connected", bootstyle=SUCCESS)
        else:
            reason = self.db.last_error() or "disconnected"
            self.db_status_label.configure(text=f"● PostgreSQL: {reason}", bootstyle=DANGER)

    def retry_db(self):
        self.db.connect()
        self._update_db_status_label()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = "connected" if self.db.connected else f"failed ({self.db.last_error()})"
        self.status_label.config(text=f"[{stamp}] DB reconnect attempt: {state}.")

    def show_history(self):
        win = tb.Toplevel(self.root)
        win.title("Alert History")
        win.geometry("640x400")

        columns = ("id", "time", "cpu", "level", "simulated")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        headings = {
            "id": "ID", "time": "Date / Time", "cpu": "CPU %",
            "level": "Level", "simulated": "Simulated",
        }
        widths = {"id": 50, "time": 170, "cpu": 70, "level": 90, "simulated": 80}
        for col in columns:
            tree.heading(col, text=headings[col])
            tree.column(col, width=widths[col], anchor=CENTER)
        tree.pack(fill=BOTH, expand=YES, padx=10, pady=10)

        rows = self.db.fetch_recent(limit=200)
        if not rows:
            msg = self.db.last_error() or "No alerts logged yet."
            tb.Label(win, text=msg, font=("Segoe UI", 10)).pack(pady=6)
        for row in rows:
            row_id, alert_time, cpu_pct, level, is_sim = row
            tree.insert("", END, values=(
                row_id,
                alert_time.strftime("%Y-%m-%d %H:%M:%S"),
                f"{cpu_pct:.0f}%",
                level,
                "Yes" if is_sim else "No",
            ))

    def on_close(self):
        if self.simulating:
            self.stop_simulation()
        self.db.close()
        self.root.destroy()


def main():
    root = tb.Window(themename="superhero")
    app = CPUMonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()