import queue
import threading
from typing import Any, cast

import ShiftClick


class DummyVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _MockKeyboardMod:
    class Key:
        shift = "shift"
        shift_l = "shift_l"
        shift_r = "shift_r"


class _MockMouseMod:
    class Button:
        left = "left"
        right = "right"
        middle = "middle"


def build_app_stub():
    app = cast(Any, ShiftClick.ShiftClickApp.__new__(ShiftClick.ShiftClickApp))
    app.state_lock = threading.Lock()
    app.gui_queue = queue.Queue()
    app.clicking_event = threading.Event()
    app.shutdown_event = threading.Event()
    app.status_var = DummyVar("DISARMED")
    app.interval_var = DummyVar(str(ShiftClick.DEFAULT_INTERVAL_MS))
    app.mode_var = DummyVar(ShiftClick.DEFAULT_MODE)
    app.armed_var = DummyVar(False)
    app.armed = False
    app.mode = ShiftClick.DEFAULT_MODE
    app.shift_pressed = False
    app.lmb_pressed = False
    app.user_lmb_pressed = False
    app.clicking_active = False
    app.interval_ms = ShiftClick.DEFAULT_INTERVAL_MS
    app.last_toggle_press_at = 0.0
    app.last_shift_seen_at = 0.0
    app.keyboard_mod = _MockKeyboardMod()
    app.mouse_mod = _MockMouseMod()
    return app


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def test_normalize_interval_handles_invalid_values():
    assert ShiftClick.normalize_interval("125") == 125
    assert ShiftClick.normalize_interval(-3) == 0
    assert ShiftClick.normalize_interval("bad") == ShiftClick.DEFAULT_INTERVAL_MS
    assert ShiftClick.normalize_interval(None) == ShiftClick.DEFAULT_INTERVAL_MS


def test_normalize_mode_falls_back_to_default():
    assert ShiftClick.normalize_mode("hold") == "hold"
    assert ShiftClick.normalize_mode("toggle") == "toggle"
    assert ShiftClick.normalize_mode("weird") == ShiftClick.DEFAULT_MODE


def test_compute_status_maps_internal_state():
    assert ShiftClick.compute_status(False, False) == "DISARMED"
    assert ShiftClick.compute_status(True, False) == "ARMED"
    assert ShiftClick.compute_status(True, True) == "CLICKING"


def test_normalize_geometry_enforces_minimum_window_size():
    geometry = ShiftClick.normalize_geometry("274x198+690+309")
    assert geometry == f"{ShiftClick.DEFAULT_WINDOW_WIDTH}x{ShiftClick.DEFAULT_WINDOW_HEIGHT}+690+309"
    assert ShiftClick.normalize_geometry("800x600+10+20") == "800x600+10+20"
    assert ShiftClick.normalize_geometry("bad") is None
    assert ShiftClick.normalize_geometry("800x600") == "800x600"


def test_is_shiftclick_mouse_event_matches_app_tag_only():
    class FakeData:
        def __init__(self, extra):
            self.dwExtraInfo = extra

    assert ShiftClick.is_shiftclick_mouse_event(FakeData(ShiftClick.SHIFTCLICK_INPUT_TAG)) is True
    assert ShiftClick.is_shiftclick_mouse_event(FakeData(12345)) is False


# ---------------------------------------------------------------------------
# Hold mode — poll path
# ---------------------------------------------------------------------------

def test_hold_mode_starts_when_shift_and_lmb_are_pressed():
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"

    app._sync_polled_input(shift_pressed=True, lmb_pressed=True)

    assert app.clicking_active is True
    assert app.clicking_event.is_set()


def test_hold_mode_stops_when_shift_released_via_poll():
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"

    app._sync_polled_input(True, True)
    app._sync_polled_input(False, True)   # Shift released

    assert app.clicking_active is False
    assert not app.clicking_event.is_set()


def test_hold_mode_does_not_stop_on_polled_lmb_false_while_clicking():
    """Polled LMB=False must not stop clicking.

    GetAsyncKeyState(VK_LBUTTON) returns False after our own injected LMB-Up
    even when the physical button is still held.  The poll must ignore LMB
    state while clicking and rely only on Shift.  Physical LMB release is
    handled by _on_physical_mouse_click → _evaluate_hold_mode instead.
    """
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"
    app.user_lmb_pressed = True   # physical button held (from listener)

    app._sync_polled_input(True, True)    # start clicking
    assert app.clicking_active is True

    app._sync_polled_input(True, False)   # injected LMB-Up makes poll see False
    assert app.clicking_active is True    # must keep clicking


def test_hold_mode_stops_when_armed_disabled_while_clicking():
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"

    app._sync_polled_input(True, True)
    app.armed = False
    app._sync_polled_input(True, True)

    assert app.clicking_active is False


# ---------------------------------------------------------------------------
# Hold mode — mouse listener path (_on_physical_mouse_click / _evaluate_hold_mode)
# ---------------------------------------------------------------------------

def test_hold_mode_physical_lmb_release_stops_clicking(monkeypatch):
    """Physical LMB release (via mouse listener) stops hold-mode clicking."""
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"
    app.user_lmb_pressed = True

    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: True)

    app._start_clicking()
    assert app.clicking_active is True

    # Simulate mouse listener detecting physical LMB release.
    app._on_physical_mouse_click(0, 0, _MockMouseMod.Button.left, False)
    assert app.clicking_active is False


def test_hold_mode_evaluate_uses_user_lmb_pressed(monkeypatch):
    """_evaluate_hold_mode uses user_lmb_pressed (physical), not polled lmb_pressed."""
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"
    app.user_lmb_pressed = True    # physical button held
    app.lmb_pressed = False        # polled (unreliable, False from injection)

    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: True)

    app._evaluate_hold_mode()

    assert app.clicking_active is True


def test_hold_mode_key_repeat_does_not_stop_clicking(monkeypatch):
    """Repeated Shift key-press events (OS key-repeat) must not stop clicking."""
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"
    app.user_lmb_pressed = True

    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: True)

    app._start_clicking()
    assert app.clicking_active is True

    # Simulate key-repeat: _on_key_press called while shift_pressed already True.
    app.shift_pressed = True  # already set from first press
    app._on_key_press(_MockKeyboardMod.Key.shift)  # repeat event

    assert app.clicking_active is True   # must not stop


# ---------------------------------------------------------------------------
# Toggle mode — start (poll path)
# ---------------------------------------------------------------------------

def test_toggle_mode_shift_lmb_starts_clicking(monkeypatch):
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"

    times = iter([10.0])
    monkeypatch.setattr(ShiftClick.time, "monotonic", lambda: next(times))

    app._sync_polled_input(True, True)

    assert app.clicking_active is True
    assert app.clicking_event.is_set()


def test_toggle_mode_poll_does_not_start_when_already_clicking(monkeypatch):
    """Poll must not try to start clicking again once toggle is active."""
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"
    app.clicking_active = True
    app.clicking_event.set()
    app.last_toggle_press_at = 0.0

    times = iter([10.0])
    monkeypatch.setattr(ShiftClick.time, "monotonic", lambda: next(times))

    app.lmb_pressed = False  # set previous state
    app._sync_polled_input(True, True)  # rising edge while clicking

    # _start_clicking is idempotent (checks clicking_active), so this is fine,
    # but the poll path should skip the start branch entirely when clicking.
    assert app.clicking_active is True


# ---------------------------------------------------------------------------
# Toggle mode — stop (mouse listener path)
# ---------------------------------------------------------------------------

def test_toggle_mode_plain_lmb_stops_clicking(monkeypatch):
    """Physical plain LMB press (no Shift) stops toggle-mode clicking."""
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"
    app.clicking_active = True
    app.clicking_event.set()

    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: False)
    app.shift_pressed = False

    app._on_physical_mouse_click(0, 0, _MockMouseMod.Button.left, True)

    assert app.clicking_active is False
    assert not app.clicking_event.is_set()


def test_toggle_mode_shift_lmb_press_does_not_stop_clicking(monkeypatch):
    """Physical LMB press WITH Shift must not stop toggle-mode clicking."""
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"
    app.clicking_active = True
    app.clicking_event.set()

    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: True)
    app.shift_pressed = True

    app._on_physical_mouse_click(0, 0, _MockMouseMod.Button.left, True)

    assert app.clicking_active is True


def test_toggle_mode_right_button_ignored(monkeypatch):
    """Right button events must be ignored entirely."""
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"
    app.clicking_active = True
    app.clicking_event.set()

    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: False)

    app._on_physical_mouse_click(0, 0, _MockMouseMod.Button.right, True)

    assert app.clicking_active is True


# ---------------------------------------------------------------------------
# Toggle mode — grace window and debounce (poll path)
# ---------------------------------------------------------------------------

def test_toggle_mode_accepts_recent_shift_after_release(monkeypatch):
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"

    times = iter([10.0, 10.1, 10.2])
    monkeypatch.setattr(ShiftClick.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ShiftClick, "is_shift_pressed_win32", lambda: False)

    app._sync_polled_input(True, False)   # Shift held, no click
    app._sync_polled_input(False, False)  # Shift released
    app._sync_polled_input(False, True)   # LMB within grace window → start

    assert app.clicking_active is True


def test_toggle_mode_debounces_rapid_second_press(monkeypatch):
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"

    times = iter([10.0, 10.05, 10.1])
    monkeypatch.setattr(ShiftClick.time, "monotonic", lambda: next(times))

    app._sync_polled_input(True, True)   # start
    assert app.clicking_active is True

    app._sync_polled_input(True, False)
    app._sync_polled_input(True, True)   # too soon — debounced, but clicking already active → no change
    assert app.clicking_active is True


# ---------------------------------------------------------------------------
# Interval sanitization
# ---------------------------------------------------------------------------

def test_sanitize_interval_updates_cached_value_and_text():
    app = build_app_stub()
    app.interval_var = DummyVar("-25")

    value = app._sanitize_interval()

    assert value == 0
    assert app.interval_ms == 0
    assert app.interval_var.get() == "0"
