import ctypes
import importlib
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk
from typing import SupportsInt


CONFIG_FILE = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ShiftClick" / "config.json"
ICON_FILE = "shiftclick_mouse.ico"

APP_VERSION = "1.0.1"

APP_AUTHOR = "Trax077"
DEFAULT_INTERVAL_MS = 50
DEFAULT_ARMED = False
DEFAULT_MODE = "hold"
DEFAULT_WINDOW_WIDTH = 680
DEFAULT_WINDOW_HEIGHT = 600
WINDOW_TITLE = f"ShiftClick {APP_VERSION} by {APP_AUTHOR}"
TEST_STATS_REFRESH_MS = 100


class PynputImportError(RuntimeError):
    """Raised when pynput is not available."""


def normalize_interval(value: str | SupportsInt | None) -> int:
    """Convert user input to a safe non-negative click interval."""
    if value is None:
        return DEFAULT_INTERVAL_MS
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MS
    return max(0, interval)


def normalize_mode(value: str | None) -> str:
    """Ensure mode stays within the supported values."""
    return value if value in {"hold", "toggle"} else DEFAULT_MODE


def compute_status(armed: bool, clicking_active: bool) -> str:
    """Map internal state to the UI status text."""
    if clicking_active:
        return "CLICKING"
    if armed:
        return "ARMED"
    return "DISARMED"


def is_injected_mouse_event(data: object) -> bool:
    """Return True for mouse events injected by software on Windows."""
    flags = getattr(data, "flags", 0)
    return bool(flags & 0x00000001 or flags & 0x00000002)


def normalize_geometry(geometry: object) -> str | None:
    """Prevent restored window geometry from shrinking below the intended UI size."""
    if not isinstance(geometry, str):
        return None

    size_part, separator, position_part = geometry.partition("+")
    try:
        width_text, height_text = size_part.lower().split("x", 1)
        width = max(DEFAULT_WINDOW_WIDTH, int(width_text))
        height = max(DEFAULT_WINDOW_HEIGHT, int(height_text))
    except (TypeError, ValueError):
        return None

    normalized = f"{width}x{height}"
    if separator:
        normalized = f"{normalized}+{position_part}"
    return normalized


def resource_path(name):
    """Resolve resource paths for source runs and PyInstaller bundles."""
    base_dir = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    return Path(base_dir) / name


def load_pynput():
    """Import pynput lazily so the app can show a friendly error message."""
    try:
        keyboard = importlib.import_module("pynput.keyboard")
        mouse = importlib.import_module("pynput.mouse")
    except ImportError as exc:
        raise PynputImportError(
            "The 'pynput' package is not installed.\n"
            "Install it with:\n"
            "pip install pynput"
        ) from exc
    return keyboard, mouse


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("union", INPUT_UNION),
    ]


class WinClicker:
    """Self-contained wrapper around WinAPI SendInput for left clicks."""

    INPUT_MOUSE = 0
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(self):
        self._send_input = ctypes.windll.user32.SendInput

    def click_left(self):
        extra = ctypes.c_ulong(0)
        events = (INPUT * 2)()

        events[0].type = self.INPUT_MOUSE
        events[0].union.mi = MOUSEINPUT(
            0,
            0,
            0,
            self.MOUSEEVENTF_LEFTDOWN,
            0,
            ctypes.pointer(extra),
        )

        events[1].type = self.INPUT_MOUSE
        events[1].union.mi = MOUSEINPUT(
            0,
            0,
            0,
            self.MOUSEEVENTF_LEFTUP,
            0,
            ctypes.pointer(extra),
        )

        sent = self._send_input(2, ctypes.byref(events), ctypes.sizeof(INPUT))
        if sent != 2:
            raise ctypes.WinError()


class ShiftClickApp:
    def __init__(self, root):
        self.root = root
        self.root.title(WINDOW_TITLE)
        self.root.resizable(False, False)
        self._set_window_icon()

        self.keyboard_mod, self.mouse_mod = load_pynput()
        self.clicker = WinClicker()

        self.gui_queue = queue.Queue()
        self.shutdown_event = threading.Event()
        self.clicking_event = threading.Event()
        self.state_lock = threading.Lock()

        self.armed = DEFAULT_ARMED
        self.mode = DEFAULT_MODE
        self.shift_pressed = False
        self.lmb_pressed = False
        self.clicking_active = False
        self.interval_ms = DEFAULT_INTERVAL_MS

        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_MS))
        self.armed_var = tk.BooleanVar(value=DEFAULT_ARMED)
        self.mode_var = tk.StringVar(value=DEFAULT_MODE)
        self.status_var = tk.StringVar(value="DISARMED")
        self.sent_var = tk.StringVar(value="Sent: 0")
        self.received_var = tk.StringVar(value="Received: 0")
        self.current_cps_var = tk.StringVar(value="Current CPS: 0")
        self.peak_cps_var = tk.StringVar(value="Peak CPS: 0")

        self.sent_clicks_total = 0
        self.received_clicks_total = 0
        self.received_timestamps = deque()
        self.peak_cps = 0

        self.keyboard_listener = None
        self.mouse_listener = None
        self.click_thread = threading.Thread(target=self._click_worker, daemon=True)

        self._load_config()
        self._configure_styles()
        self._build_ui()
        self.interval_var.trace_add("write", self._on_interval_var_changed)
        self._apply_loaded_state()

        self.click_thread.start()
        self._start_global_listeners()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(0, self._restore_geometry)
        self.root.after(50, self._process_gui_queue)
        self.root.after(TEST_STATS_REFRESH_MS, self._refresh_test_stats)

    def _set_window_icon(self):
        icon_path = resource_path(ICON_FILE)
        try:
            if icon_path.exists():
                self.root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass

    def _configure_styles(self):
        default_font = tkfont.nametofont("TkDefaultFont")
        text_font = tkfont.nametofont("TkTextFont")
        heading_font = default_font.copy()
        heading_font.configure(size=default_font.cget("size") + 1, weight="bold")

        # Increase the Tk-wide singleton fonts once for better readability.
        default_font.configure(size=default_font.cget("size") + 1)
        text_font.configure(size=text_font.cget("size") + 1)

        style = ttk.Style(self.root)
        style.configure("TLabel", padding=(0, 2))
        style.configure("TButton", padding=(10, 8))
        style.configure("TCheckbutton", padding=(0, 4))
        style.configure("TRadiobutton", padding=(0, 4))
        style.configure("Armed.TCheckbutton", font=heading_font, padding=(0, 4))
        style.configure("TLabelframe", padding=12)
        style.configure("Status.TLabel", font=heading_font, padding=(12, 10))
        style.configure("Section.TLabelframe", padding=14)
        style.configure("Section.TLabelframe.Label", font=heading_font)
        style.configure("StatusCaption.TLabel", foreground="#4b5563")
        style.configure("Stats.TLabel", font=text_font)
        style.configure("Muted.TLabel", foreground="#374151")
        style.configure("Warning.TLabel", foreground="#9a3412")
        style.configure("StatValue.TLabel", font=heading_font)
        style.configure("TestArea.TButton", font=heading_font, padding=(18, 36))

    def _build_ui(self):
        self.root.geometry(f"{DEFAULT_WINDOW_WIDTH}x{DEFAULT_WINDOW_HEIGHT}")
        self.root.minsize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)

        outer = ttk.Frame(self.root, padding=18)
        outer.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer.columnconfigure(0, weight=4)
        outer.columnconfigure(1, weight=3)
        outer.rowconfigure(1, weight=1)

        controls_frame = ttk.LabelFrame(outer, text="Controls", style="Section.TLabelframe")
        controls_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=(0, 10))
        controls_frame.columnconfigure(1, weight=1)

        ttk.Label(controls_frame, text="Interval (ms)").grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.interval_spinbox = ttk.Spinbox(
            controls_frame,
            from_=0,
            to=999999,
            width=14,
            textvariable=self.interval_var,
            justify="right",
            font=tkfont.nametofont("TkTextFont"),
        )
        self.interval_spinbox.grid(row=0, column=1, sticky="ew", pady=(0, 12), padx=(16, 0))
        self.interval_spinbox.bind("<FocusOut>", self._on_interval_changed)
        self.interval_spinbox.bind("<Return>", self._on_interval_changed)

        ttk.Label(
            controls_frame,
            text="Warning: 0 ms uses the fastest safe loop and can raise CPU usage.",
            wraplength=360,
            justify="left",
            style="Warning.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.armed_check = ttk.Checkbutton(
            controls_frame,
            text="Armed",
            style="Armed.TCheckbutton",
            variable=self.armed_var,
            command=self._on_armed_changed,
        )
        self.armed_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 14))

        mode_frame = ttk.LabelFrame(controls_frame, text="Mode", padding=(14, 10))
        mode_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        mode_frame.columnconfigure(0, weight=1)
        mode_frame.columnconfigure(1, weight=1)

        self.hold_radio = ttk.Radiobutton(
            mode_frame,
            text="Hold",
            value="hold",
            variable=self.mode_var,
            command=self._on_mode_changed,
        )
        self.hold_radio.grid(row=0, column=0, sticky="w")

        self.toggle_radio = ttk.Radiobutton(
            mode_frame,
            text="Toggle",
            value="toggle",
            variable=self.mode_var,
            command=self._on_mode_changed,
        )
        self.toggle_radio.grid(row=0, column=1, sticky="w")

        info_text = "Hotkey: Shift + Left Mouse Button\nToggle mode: plain LMB stops active autoclick"
        info_label = ttk.Label(controls_frame, text=info_text, justify="left", wraplength=360, style="Muted.TLabel")
        info_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))

        status_frame = ttk.LabelFrame(outer, text="Status", style="Section.TLabelframe")
        status_frame.grid(row=0, column=1, sticky="nsew", pady=(0, 10))
        status_frame.columnconfigure(0, weight=1)

        ttk.Label(status_frame, text="Current state", style="StatusCaption.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        status_label = ttk.Label(
            status_frame,
            textvariable=self.status_var,
            style="Status.TLabel",
            relief="sunken",
            anchor="center",
            width=18,
        )
        status_label.grid(row=1, column=0, sticky="ew")

        stats_frame = ttk.LabelFrame(status_frame, text="Live Stats", padding=(12, 10))
        stats_frame.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        stats_frame.columnconfigure(0, weight=1)

        self.sent_label = ttk.Label(stats_frame, textvariable=self.sent_var, style="Stats.TLabel", width=22, anchor="w")
        self.sent_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.received_label = ttk.Label(
            stats_frame, textvariable=self.received_var, style="Stats.TLabel", width=22, anchor="w"
        )
        self.received_label.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.current_cps_label = ttk.Label(
            stats_frame, textvariable=self.current_cps_var, style="Stats.TLabel", width=22, anchor="w"
        )
        self.current_cps_label.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.peak_cps_label = ttk.Label(
            stats_frame, textvariable=self.peak_cps_var, style="Stats.TLabel", width=22, anchor="w"
        )
        self.peak_cps_label.grid(row=3, column=0, sticky="ew")

        test_frame = ttk.LabelFrame(outer, text="Click Test", style="Section.TLabelframe")
        test_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        test_frame.columnconfigure(0, weight=1)
        test_frame.rowconfigure(1, weight=1)

        ttk.Label(
            test_frame,
            text="Move the cursor over the area below and start autoclicking to measure delivered clicks.",
            wraplength=620,
            justify="left",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.test_area = ttk.Button(
            test_frame,
            text="Test Click Area",
            style="TestArea.TButton",
            width=34,
            takefocus=False,
        )
        self.test_area.grid(row=1, column=0, sticky="nsew", pady=(0, 14))
        self.test_area.bind("<ButtonPress-1>", self._on_test_area_click, add="+")

        reset_button = ttk.Button(test_frame, text="Reset Test", command=self._reset_test_stats)
        reset_button.grid(row=2, column=0, sticky="e")

    def _apply_loaded_state(self):
        self._sanitize_interval()
        self._set_armed(self.armed_var.get())
        self._set_mode(self.mode_var.get())
        self._update_status()

    def _start_global_listeners(self):
        self.keyboard_listener = self.keyboard_mod.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self.mouse_listener = self.mouse_mod.Listener(
            on_click=self._on_global_click,
            win32_event_filter=self._mouse_event_filter,
        )

        self.keyboard_listener.start()
        self.mouse_listener.start()

    def _is_shift_key(self, key):
        return key in {
            self.keyboard_mod.Key.shift,
            self.keyboard_mod.Key.shift_l,
            self.keyboard_mod.Key.shift_r,
        }

    def _on_key_press(self, key):
        if self._is_shift_key(key):
            with self.state_lock:
                self.shift_pressed = True
            self._evaluate_hold_mode()

    def _on_key_release(self, key):
        if self._is_shift_key(key):
            with self.state_lock:
                self.shift_pressed = False
            self._evaluate_hold_mode()

    def _mouse_event_filter(self, msg, data):
        return not is_injected_mouse_event(data)

    def _on_global_click(self, x, y, button, pressed):
        if button != self.mouse_mod.Button.left:
            return

        if pressed:
            self._handle_lmb_press()
        else:
            self._handle_lmb_release()

    def _handle_lmb_press(self):
        with self.state_lock:
            self.lmb_pressed = True
            armed = self.armed
            mode = self.mode
            shift_pressed = self.shift_pressed
            clicking_active = self.clicking_active

        if not armed:
            return

        if mode == "hold":
            if shift_pressed:
                self._start_clicking()
            return

        if shift_pressed:
            if clicking_active:
                self._stop_clicking()
            else:
                self._start_clicking()
            return

        if clicking_active:
            self._stop_clicking()

    def _handle_lmb_release(self):
        with self.state_lock:
            self.lmb_pressed = False
        self._evaluate_hold_mode()

    def _evaluate_hold_mode(self):
        with self.state_lock:
            armed = self.armed
            mode = self.mode
            shift_pressed = self.shift_pressed
            lmb_pressed = self.lmb_pressed

        if mode != "hold":
            return

        if armed and shift_pressed and lmb_pressed:
            self._start_clicking()
        else:
            self._stop_clicking()

    def _start_clicking(self):
        with self.state_lock:
            if self.clicking_active or not self.armed:
                return
            self.clicking_active = True
            self.clicking_event.set()
        self._queue_status_update()

    def _stop_clicking(self):
        with self.state_lock:
            if not self.clicking_active:
                return
            self.clicking_active = False
            self.clicking_event.clear()
        self._queue_status_update()

    def _click_worker(self):
        while not self.shutdown_event.is_set():
            if not self.clicking_event.wait(timeout=0.1):
                continue

            while self.clicking_event.is_set() and not self.shutdown_event.is_set():
                try:
                    self.clicker.click_left()
                except Exception as exc:
                    self.gui_queue.put(("error", f"Failed to send click input:\n{exc}"))
                    self._stop_clicking()
                    break

                with self.state_lock:
                    self.sent_clicks_total += 1

                interval_ms = self._get_interval_ms()
                if interval_ms <= 0:
                    if self.shutdown_event.wait(0.001):
                        return
                else:
                    if self.shutdown_event.wait(interval_ms / 1000.0):
                        return

    def _get_interval_ms(self):
        with self.state_lock:
            return self.interval_ms

    def _sanitize_interval(self):
        value = normalize_interval(self.interval_var.get())
        self.interval_var.set(str(value))
        with self.state_lock:
            self.interval_ms = value
        return value

    def _on_interval_changed(self, _event=None):
        self._sanitize_interval()

    def _on_interval_var_changed(self, *_args):
        value = normalize_interval(self.interval_var.get())
        with self.state_lock:
            self.interval_ms = value

    def _on_armed_changed(self):
        self._set_armed(self.armed_var.get())

    def _set_armed(self, value):
        with self.state_lock:
            self.armed = bool(value)
            armed = self.armed

        if not armed:
            self._stop_clicking()
        else:
            self._evaluate_hold_mode()

        self._update_status()

    def _on_mode_changed(self):
        self._set_mode(self.mode_var.get())

    def _set_mode(self, value):
        mode = normalize_mode(value)
        self.mode_var.set(mode)

        with self.state_lock:
            self.mode = mode

        self._stop_clicking()
        if mode == "hold":
            self._evaluate_hold_mode()
        self._update_status()

    def _queue_status_update(self):
        self.gui_queue.put(("status", None))

    def _update_status(self):
        with self.state_lock:
            armed = self.armed
            clicking_active = self.clicking_active

        self.status_var.set(compute_status(armed, clicking_active))

    def _process_gui_queue(self):
        while True:
            try:
                item_type, payload = self.gui_queue.get_nowait()
            except queue.Empty:
                break

            if item_type == "status":
                self._update_status()
            elif item_type == "error":
                messagebox.showerror(WINDOW_TITLE, payload)

        if not self.shutdown_event.is_set():
            self.root.after(50, self._process_gui_queue)

    def _on_test_area_click(self, _event):
        now = time.monotonic()
        self.received_clicks_total += 1
        self.received_timestamps.append(now)
        self._recalculate_cps(now)
        self._update_test_stat_labels()

    def _trim_received_timestamps(self, now):
        cutoff = now - 1.0
        while self.received_timestamps and self.received_timestamps[0] < cutoff:
            self.received_timestamps.popleft()

    def _recalculate_cps(self, now=None):
        if now is None:
            now = time.monotonic()

        self._trim_received_timestamps(now)
        current_cps = len(self.received_timestamps)
        if current_cps > self.peak_cps:
            self.peak_cps = current_cps
        return current_cps

    def _update_test_stat_labels(self, current_cps=None):
        if current_cps is None:
            current_cps = self._recalculate_cps()

        with self.state_lock:
            sent_clicks = self.sent_clicks_total

        self.sent_var.set(f"Sent: {sent_clicks}")
        self.received_var.set(f"Received: {self.received_clicks_total}")
        self.current_cps_var.set(f"Current CPS: {current_cps}")
        self.peak_cps_var.set(f"Peak CPS: {self.peak_cps}")

    def _refresh_test_stats(self):
        current_cps = self._recalculate_cps()
        self._update_test_stat_labels(current_cps=current_cps)
        if not self.shutdown_event.is_set():
            self.root.after(TEST_STATS_REFRESH_MS, self._refresh_test_stats)

    def _reset_test_stats(self):
        with self.state_lock:
            self.sent_clicks_total = 0
        self.received_clicks_total = 0
        self.received_timestamps.clear()
        self.peak_cps = 0
        self._update_test_stat_labels(current_cps=0)

    def _load_config(self):
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._loaded_geometry = None
            return

        interval = normalize_interval(data.get("interval_ms", DEFAULT_INTERVAL_MS))
        mode = normalize_mode(data.get("mode", DEFAULT_MODE))
        geometry = data.get("geometry")

        self.interval_var.set(str(interval))
        self.armed_var.set(DEFAULT_ARMED)
        self.mode_var.set(mode)
        self._loaded_geometry = normalize_geometry(geometry)

    def _save_config(self):
        data = {
            "interval_ms": self._sanitize_interval(),
            "mode": self.mode_var.get(),
            "geometry": self.root.geometry(),
        }

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning(WINDOW_TITLE, f"Settings could not be saved:\n{exc}")

    def _stop_listeners(self):
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener is None:
                continue
            try:
                listener.stop()
            except RuntimeError:
                continue
            except Exception as exc:
                print(f"Warning: failed to stop listener {listener!r}: {exc}", file=sys.stderr)

    def _on_close(self):
        self._save_config()
        self.shutdown_event.set()
        self._stop_clicking()
        self._stop_listeners()
        self.root.destroy()

    def _restore_geometry(self):
        if self._loaded_geometry:
            try:
                self.root.geometry(self._loaded_geometry)
            except tk.TclError:
                pass


def main():
    if sys.platform != "win32":
        raise RuntimeError("This script must run on Windows.")

    root = tk.Tk()

    try:
        app = ShiftClickApp(root)
    except PynputImportError as exc:
        messagebox.showerror(WINDOW_TITLE, str(exc))
        root.destroy()
        return
    except Exception as exc:
        messagebox.showerror(WINDOW_TITLE, f"Failed to start application:\n{exc}")
        root.destroy()
        return

    root.mainloop()


if __name__ == "__main__":
    main()
