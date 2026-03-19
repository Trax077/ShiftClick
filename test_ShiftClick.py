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


class DummyMouseData:
    def __init__(self, flags):
        self.flags = flags


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
    app.clicking_active = False
    app.interval_ms = ShiftClick.DEFAULT_INTERVAL_MS
    return app


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


def test_is_injected_mouse_event_detects_windows_injected_flags():
    assert ShiftClick.is_injected_mouse_event(DummyMouseData(0x00000001)) is True
    assert ShiftClick.is_injected_mouse_event(DummyMouseData(0x00000002)) is True
    assert ShiftClick.is_injected_mouse_event(DummyMouseData(0)) is False


def test_hold_mode_starts_when_shift_and_lmb_are_pressed():
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"
    app.shift_pressed = True

    app._handle_lmb_press()

    assert app.lmb_pressed is True
    assert app.clicking_active is True
    assert app.clicking_event.is_set()


def test_hold_mode_stops_when_condition_is_released():
    app = build_app_stub()
    app.armed = True
    app.mode = "hold"
    app.shift_pressed = True
    app.lmb_pressed = True

    app._start_clicking()
    app._handle_lmb_release()

    assert app.lmb_pressed is False
    assert app.clicking_active is False
    assert not app.clicking_event.is_set()


def test_toggle_mode_shift_lmb_toggles_clicking_on_and_off():
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"
    app.shift_pressed = True

    app._handle_lmb_press()
    assert app.clicking_active is True

    app._handle_lmb_press()
    assert app.clicking_active is False


def test_toggle_mode_plain_lmb_stops_active_clicking():
    app = build_app_stub()
    app.armed = True
    app.mode = "toggle"
    app.clicking_active = True
    app.clicking_event.set()

    app._handle_lmb_press()

    assert app.clicking_active is False
    assert not app.clicking_event.is_set()


def test_sanitize_interval_updates_cached_value_and_text():
    app = build_app_stub()
    app.interval_var = DummyVar("-25")

    value = app._sanitize_interval()

    assert value == 0
    assert app.interval_ms == 0
    assert app.interval_var.get() == "0"
