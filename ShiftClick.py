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
from ctypes import wintypes
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk
from typing import SupportsInt


CONFIG_FILE = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ShiftClick" / "config.json"
ICON_FILE = "shiftclick_mouse.ico"

APP_VERSION = "1.0.2"

APP_AUTHOR = "Trax077"
DEFAULT_INTERVAL_MS = 50
DEFAULT_ARMED = False
DEFAULT_MODE = "hold"
DEFAULT_WINDOW_WIDTH = 680
DEFAULT_WINDOW_HEIGHT = 600
WINDOW_TITLE = f"ShiftClick {APP_VERSION} by {APP_AUTHOR}"
TEST_STATS_REFRESH_MS = 100
INPUT_POLL_INTERVAL_S = 0.01
TOGGLE_DEBOUNCE_S = 0.2
SHIFT_CHORD_GRACE_S = 0.25
ULONG_PTR = wintypes.WPARAM
# 64-bit value — safe on x64 Windows where ULONG_PTR / WPARAM is 64 bits wide.
# Tags our own injected clicks so the mouse listener can filter them out.
SHIFTCLICK_INPUT_TAG = 0x5348434C49434B31


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


def get_mouse_event_extra_info(data: object) -> int:
    """Read hook extra-info payload when available."""
    extra_info = getattr(data, "dwExtraInfo", 0)
    if isinstance(extra_info, int):
        return extra_info
    try:
        return int(extra_info)
    except (TypeError, ValueError):
        return 0


def is_shiftclick_mouse_event(data: object) -> bool:
    """Return True only for mouse events emitted by this app.

    Compares the lower 32 bits only.  pynput defines MSLLHOOKSTRUCT.dwExtraInfo
    as ULONG (32-bit) even on x64, so the upper half of our 64-bit
    SHIFTCLICK_INPUT_TAG is silently truncated in the hook callback.
    Masking both sides to 32 bits makes the comparison reliable regardless
    of whether pynput returns a truncated or a full-width value.
    """
    return (get_mouse_event_extra_info(data) & 0xFFFFFFFF) == (SHIFTCLICK_INPUT_TAG & 0xFFFFFFFF)


def is_shift_pressed_win32() -> bool:
    """Read the current Shift state directly from WinAPI."""
    user32 = getattr(ctypes, "windll", None)
    if user32 is None:
        return False

    get_async_key_state = getattr(user32.user32, "GetAsyncKeyState", None)
    if get_async_key_state is None:
        return False

    vk_shift_left = 0xA0
    vk_shift_right = 0xA1
    return bool(
        get_async_key_state(vk_shift_left) & 0x8000
        or get_async_key_state(vk_shift_right) & 0x8000
    )


def is_lmb_pressed_win32() -> bool:
    """Read the current left mouse button state directly from WinAPI."""
    user32 = getattr(ctypes, "windll", None)
    if user32 is None:
        return False

    get_async_key_state = getattr(user32.user32, "GetAsyncKeyState", None)
    if get_async_key_state is None:
        return False

    vk_lbutton = 0x01
    return bool(get_async_key_state(vk_lbutton) & 0x8000)


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
        ("dwExtraInfo", ULONG_PTR),
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
        events = (INPUT * 2)()

        events[0].type = self.INPUT_MOUSE
        events[0].union.mi = MOUSEINPUT(
            0, 0, 0, self.MOUSEEVENTF_LEFTDOWN, 0, SHIFTCLICK_INPUT_TAG,
        )

        events[1].type = self.INPUT_MOUSE
        events[1].union.mi = MOUSEINPUT(
            0, 0, 0, self.MOUSEEVENTF_LEFTUP, 0, SHIFTCLICK_INPUT_TAG,
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
        self.user_lmb_pressed = False   # physical LMB state from mouse listener
        self.clicking_active = False
        self.interval_ms = DEFAULT_INTERVAL_MS
        self.last_toggle_press_at = 0.0
        self.last_shift_seen_at = 0.0

        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_MS))
        self.armed_var = tk.BooleanVar(value=DEFAULT_ARMED)
        self.mode_var = tk.StringVar(value=DEFAULT_MODE)
        self.status_var = tk.StringVar(value="DISARMED")
        self.last_action_var = tk.StringVar(value="Last action: startup")
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
        self.input_thread = threading.Thread(target=self._input_poll_worker, daemon=True)

        self._load_config()
        self._configure_styles()
        self._build_ui()
        self.interval_var.trace_add("write", self._on_interval_var_changed)
        self._apply_loaded_state()

        self.click_thread.start()
        self.input_thread.start()
        self._start_listeners()

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

        hold_info = "Hold: hold Shift + LMB to autoclick, release either to stop."
        toggle_info = "Toggle: Shift + LMB to start, plain LMB to stop."
        info_label = ttk.Label(
            controls_frame,
            text=f"{hold_info}\n{toggle_info}",
            justify="left",
            wraplength=360,
            style="Muted.TLabel",
        )
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

        ttk.Label(
            status_frame,
            textvariable=self.last_action_var,
            style="Muted.TLabel",
            wraplength=240,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))

        stats_frame = ttk.LabelFrame(status_frame, text="Live Stats", padding=(12, 10))
        stats_frame.grid(row=3, column=0, sticky="ew", pady=(14, 0))
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

    def _start_listeners(self):
        """Start keyboard and mouse listeners.

        Keyboard listener: fast, event-driven Shift detection.

        Mouse listener: tracks the PHYSICAL LMB state independently of our
        own injected clicks.  The win32_event_filter suppresses events tagged
        with SHIFTCLICK_INPUT_TAG before they reach on_click, so the callback
        only fires for genuine user input.  This solves two problems:
          - Hold mode: our injected LMB-Up cannot clear user_lmb_pressed,
            so polling no longer sees a false "button released" while the
            physical button is still held.
          - Toggle mode: plain LMB stop is detected reliably without being
            confused with injected LMB-Down events.
        """
        self.keyboard_listener = self.keyboard_mod.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self.mouse_listener = self.mouse_mod.Listener(
            on_click=self._on_physical_mouse_click,
            win32_event_filter=self._mouse_event_filter,
        )
        self.keyboard_listener.start()
        self.mouse_listener.start()

    # ------------------------------------------------------------------
    # Keyboard listener callbacks
    # ------------------------------------------------------------------

    def _is_shift_key(self, key):
        return key in {
            self.keyboard_mod.Key.shift,
            self.keyboard_mod.Key.shift_l,
            self.keyboard_mod.Key.shift_r,
        }

    def _on_key_press(self, key):
        if self._is_shift_key(key):
            with self.state_lock:
                already_pressed = self.shift_pressed
                self.shift_pressed = True
                self.last_shift_seen_at = time.monotonic()
            if not already_pressed:
                # Suppress key-repeat events: Windows fires repeated WM_KEYDOWN
                # while a key is held (~500 ms initial delay then ~30 Hz).
                # Each repeat would call _evaluate_hold_mode with a stale
                # lmb_pressed=False (set by injected LMB-Up), stopping clicking.
                self._evaluate_hold_mode()

    def _on_key_release(self, key):
        if self._is_shift_key(key):
            with self.state_lock:
                self.shift_pressed = False
            self._evaluate_hold_mode()

    # ------------------------------------------------------------------
    # Mouse listener callbacks
    # ------------------------------------------------------------------

    def _mouse_event_filter(self, msg, data):
        """Block our own injected clicks; only physical events reach on_click."""
        return not is_shiftclick_mouse_event(data)

    def _on_physical_mouse_click(self, x, y, button, pressed):
        """Called only for physical (non-injected) LMB events."""
        if button != self.mouse_mod.Button.left:
            return

        with self.state_lock:
            self.user_lmb_pressed = pressed
            mode = self.mode
            clicking_active = self.clicking_active

        if mode == "hold":
            # Delegate to the shared evaluator which checks Shift + user_lmb_pressed.
            self._evaluate_hold_mode()
        elif mode == "toggle" and pressed and clicking_active:
            # Plain LMB press while clicking → stop (if Shift is not active).
            if not self._is_shift_active():
                self._stop_clicking(reason="toggle lmb stop")

    # ------------------------------------------------------------------
    # Shared input helpers
    # ------------------------------------------------------------------

    def _is_shift_active(self):
        with self.state_lock:
            listener_shift_pressed = self.shift_pressed
        return listener_shift_pressed or is_shift_pressed_win32()

    def _is_shift_hotkey_active(self, now, shift_pressed=None):
        with self.state_lock:
            last_shift_seen_at = self.last_shift_seen_at
            listener_shift_pressed = self.shift_pressed

        if shift_pressed is None:
            shift_pressed = listener_shift_pressed or is_shift_pressed_win32()

        return shift_pressed or (now - last_shift_seen_at) <= SHIFT_CHORD_GRACE_S

    # ------------------------------------------------------------------
    # Poll worker (Shift + LMB via WinAPI — fallback / start detection)
    # ------------------------------------------------------------------

    def _sync_polled_input(self, shift_pressed, lmb_pressed):
        now = time.monotonic()
        with self.state_lock:
            previous_lmb_pressed = self.lmb_pressed
            self.shift_pressed = shift_pressed
            if shift_pressed:
                self.last_shift_seen_at = now
            self.lmb_pressed = lmb_pressed
            armed = self.armed
            mode = self.mode
            clicking_active = self.clicking_active
            can_toggle = (now - self.last_toggle_press_at) >= TOGGLE_DEBOUNCE_S

        if mode == "hold":
            if clicking_active:
                # Physical LMB state comes from the mouse listener (user_lmb_pressed).
                # GetAsyncKeyState(VK_LBUTTON) is unreliable while we are injecting:
                # our own LMB-Up clears the bit even when the physical button is held.
                # LMB release is therefore handled by _on_physical_mouse_click →
                # _evaluate_hold_mode.  Here we only watch Shift + Armed.
                if not armed or not shift_pressed:
                    self._stop_clicking(reason="hold poll stop")
            else:
                if armed and shift_pressed and lmb_pressed:
                    self._start_clicking(reason="hold poll start")
            return

        # Toggle mode — only detect the START edge here.
        # STOP is handled reliably by _on_physical_mouse_click (mouse listener),
        # which filters out our own injected LMB-Down events.
        if lmb_pressed and not previous_lmb_pressed and not clicking_active:
            if not armed:
                return
            if self._is_shift_hotkey_active(now, shift_pressed=shift_pressed):
                if not can_toggle:
                    return
                with self.state_lock:
                    self.last_toggle_press_at = now
                self._start_clicking(reason="toggle hotkey start")

    def _input_poll_worker(self):
        while not self.shutdown_event.is_set():
            shift_pressed = is_shift_pressed_win32()
            lmb_pressed = is_lmb_pressed_win32()
            self._sync_polled_input(shift_pressed, lmb_pressed)

            if self.shutdown_event.wait(INPUT_POLL_INTERVAL_S):
                return

    # ------------------------------------------------------------------
    # Hold-mode state evaluator (called from both listeners)
    # ------------------------------------------------------------------

    def _evaluate_hold_mode(self):
        with self.state_lock:
            armed = self.armed
            mode = self.mode
            user_lmb = self.user_lmb_pressed   # physical state from mouse listener

        shift_pressed = self._is_shift_active()

        if mode != "hold":
            return

        if armed and shift_pressed and user_lmb:
            self._start_clicking(reason="hold evaluate start")
        else:
            self._stop_clicking(reason="hold evaluate stop")

    # ------------------------------------------------------------------
    # Clicking state machine
    # ------------------------------------------------------------------

    def _set_last_action(self, text):
        self.last_action_var.set(f"Last action: {text}")

    def _start_clicking(self, reason="start"):
        with self.state_lock:
            if self.clicking_active or not self.armed:
                return
            self.clicking_active = True
            self.clicking_event.set()
        self.gui_queue.put(("action", reason))
        self._queue_status_update()

    def _stop_clicking(self, reason="stop"):
        with self.state_lock:
            if not self.clicking_active:
                return
            self.clicking_active = False
            self.clicking_event.clear()
        self.gui_queue.put(("action", reason))
        self._queue_status_update()

    def _click_worker(self):
        while not self.shutdown_event.is_set():
            if not self.clicking_event.wait(timeout=0.1):
                continue

            while self.clicking_event.is_set() and not self.shutdown_event.is_set():
                try:
                    self.clicker.click_left()
                except Exception as exc:
                    self.gui_queue.put(("action", f"stop: sendinput error {exc}"))
                    self.gui_queue.put(("error", f"Failed to send click input:\n{exc}"))
                    self._stop_clicking(reason="sendinput error")
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
            self._stop_clicking(reason="armed disabled")
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

        self._stop_clicking(reason="mode changed")
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
            elif item_type == "action":
                self._set_last_action(payload)
            elif item_type == "error":
                messagebox.showerror(WINDOW_TITLE, payload)

        if not self.shutdown_event.is_set():
            self.root.after(50, self._process_gui_queue)

    # ------------------------------------------------------------------
    # Test area
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

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
            "interval_ms": normalize_interval(self.interval_var.get()),
            "mode": self.mode_var.get(),
            "geometry": self.root.geometry(),
        }

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning(WINDOW_TITLE, f"Settings could not be saved:\n{exc}")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

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
        if not self._loaded_geometry:
            return
        # Window is non-resizable; restore only the position part (+x+y).
        _, sep, position_part = self._loaded_geometry.partition("+")
        if not sep:
            return
        try:
            self.root.geometry(f"+{position_part}")
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
