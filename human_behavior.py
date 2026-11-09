#!/usr/bin/env python3
"""
human_behavior.py
==================
A self-contained "humanization" toolkit for Playwright-driven browser
automation. This file does NOT contain any Google-Flow-specific logic and
does NOT import or modify `automate_google_flow.py` in any way.

Why this exists
----------------
Google's abuse-detection systems don't just look at *what* your browser
does (they can't easily tell a real click from a scripted one at the DOM
level) — they also look at the *shape* of the low-level input events:

  • Mouse moves that teleport instantly from A to B with zero intermediate
    events, in a perfectly straight line, at literally-machine-precise
    coordinates, over and over, are a classic automation signature.
  • Clicks that happen with an identical, suspiciously short mouse-down to
    mouse-up duration every single time.
  • Waits between actions that are always EXACTLY the same number of
    milliseconds (e.g. always exactly 3000ms, always exactly 500ms).
  • Zero "idle" mouse activity — real humans wiggle the mouse, overshoot
    targets slightly and correct, pause before clicking, etc.

None of this changes *what* the automation does (same prompt text, same
buttons, same download flow) — it only changes the *timing and physical
path* of mouse/keyboard input, which is exactly what you asked for:
"we need to maintain some human like behaviour... so browser does not
understand that it is automated".

Pasting text (Locator.fill) is intentionally KEPT as the input method —
per your own note, a human who copies a prompt and pastes it (instead of
typing it letter by letter) is completely natural and, if anything, MORE
realistic for repeated/templated prompts than slow character-by-character
typing would be. We just wrap the paste action with human-like pauses
around it (the beat a person takes before/after hitting Cmd+V), instead of
faking individual keystrokes.

How it's used
--------------
Nothing in this file runs automatically. A caller (see
`automate_google_flow_human.py`) must explicitly call `apply_patches()`
once, near the very start of the process. That call monkey-patches, at the
PLAYWRIGHT CLASS LEVEL (not by touching automate_google_flow.py):

    Mouse.move            → curved, multi-step, variable-speed movement
    Mouse.click            → move-first + randomized down/up delay
    Locator.click           → curved mouse glide to the element first
    Locator.fill            → human pauses around the paste action
    Locator.press           → tiny pre-press pause
    Keyboard.press          → tiny pre-press pause
    Page.wait_for_timeout   → jittered wait duration
    time.sleep (process-wide) → jittered sleep duration + rare "distraction" pause

Because these are patched on the shared Playwright classes (and the global
`time` module), EVERY interaction already performed by
`automate_google_flow.py` — even deep inside functions like
`setup_generation_settings()` or `try_download_image()` — automatically
becomes human-like, with zero changes to that file.

Call `remove_patches()` to restore the original behaviour, or use
`set_enabled(False)` to make every patched function transparently fall
back to the pristine Playwright behaviour without having to re-patch.
"""

from __future__ import annotations

import math
import random
import time as _time_module

# ──────────────────────────────────────────────
# Global on/off switch
# ──────────────────────────────────────────────
# When False, every patched function calls straight through to the
# original Playwright implementation with no modification at all. This
# lets a caller flip humanization on/off (e.g. via a --no-human-behavior
# CLI flag) without touching this module or re-importing anything.
_ENABLED = True


def set_enabled(enabled: bool) -> None:
    global _ENABLED
    _ENABLED = enabled


def is_enabled() -> bool:
    return _ENABLED


# ──────────────────────────────────────────────
# Tunable "personality" knobs
# ──────────────────────────────────────────────
# Kept as plain module-level constants (not a class) so they're trivial to
# tweak later without touching any call sites.
MOUSE_MIN_STEP_DELAY   = 0.004   # seconds between intermediate mouse-move points
MOUSE_MAX_STEP_DELAY   = 0.020
MOUSE_OVERSHOOT_CHANCE = 0.12    # probability of a slight overshoot + correction
CLICK_PRE_DELAY        = (0.04, 0.16)   # pause after arriving, before mouse-down
CLICK_POST_DELAY       = (0.05, 0.15)   # pause after mouse-up
CLICK_DOWN_UP_MS       = (35, 130)      # Playwright's own "delay" between down/up

FILL_PRE_DELAY  = (0.15, 0.45)  # beat before pasting (like tabbing/clicking in, then Cmd+V)
FILL_POST_DELAY = (0.10, 0.30)  # beat after pasting, before hitting submit

PRESS_PRE_DELAY = (0.03, 0.12)

WAIT_JITTER_LOW  = -0.20   # wait_for_timeout / time.sleep jitter range (fraction of requested time)
WAIT_JITTER_HIGH = 0.35
DISTRACTION_CHANCE   = 0.05      # rare pause simulating a human glancing away
DISTRACTION_EXTRA_SEC = (0.3, 1.4)

IDLE_WANDER_RADIUS = 35   # px, small aimless mouse drift used by idle_wander()


# ──────────────────────────────────────────────
# Internal state
# ──────────────────────────────────────────────
_patched = False
_last_pos: dict[int, tuple[float, float]] = {}   # id(Mouse instance) -> last known (x, y)

# Saved originals — captured eagerly at import time (Playwright is a hard
# dependency of this whole pipeline already, see requirements.txt), so
# these are never None and apply_patches()/remove_patches() just swap
# which implementation the class attribute points to.
from playwright.sync_api import Mouse as _Mouse, Locator as _Locator, Keyboard as _Keyboard, Page as _Page  # noqa: E402

_orig_mouse_move             = _Mouse.move
_orig_mouse_click            = _Mouse.click
_orig_locator_click          = _Locator.click
_orig_locator_fill           = _Locator.fill
_orig_locator_press          = _Locator.press
_orig_keyboard_press         = _Keyboard.press
_orig_page_wait_for_timeout  = _Page.wait_for_timeout
_orig_time_sleep = _time_module.sleep


# ──────────────────────────────────────────────
# Low-level helpers
# ──────────────────────────────────────────────

def _rand(a: float, b: float) -> float:
    return random.uniform(a, b)


def human_pause(low: float, high: float) -> None:
    """Sleep a small human-ish amount using the ORIGINAL (unjittered)
    time.sleep, so callers explicitly requesting a pause always get one
    (avoids double-jitter/confusing recursion if time.sleep is patched)."""
    _orig_time_sleep(_rand(low, high))


def _curved_path(x0: float, y0: float, x1: float, y1: float, segments: int):
    """Quadratic Bezier path from (x0,y0) to (x1,y1) with a randomized
    perpendicular bow, so the mouse drifts along a gentle arc instead of a
    perfectly straight machine-drawn line."""
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    # Perpendicular unit vector
    px, py = -dy / dist, dx / dist
    bow = _rand(-0.18, 0.18) * dist
    ctrl_x = (x0 + x1) / 2 + px * bow
    ctrl_y = (y0 + y1) / 2 + py * bow

    pts = []
    for i in range(1, segments + 1):
        t = i / segments
        it = 1 - t
        xt = it * it * x0 + 2 * it * t * ctrl_x + t * t * x1
        yt = it * it * y0 + 2 * it * t * ctrl_y + t * t * y1
        pts.append((xt, yt))
    return pts


def _humanized_move_impl(mouse_self, x: float, y: float, *, steps=None):
    """Move the given Playwright Mouse instance to (x, y) through a curved,
    variable-speed, multi-point path with real wall-clock gaps between
    events (so consecutive mousemove timestamps look physically plausible
    instead of an instant teleport)."""
    key = id(mouse_self)
    x0, y0 = _last_pos.get(key, (x + _rand(-50, 50), y + _rand(-50, 50)))

    distance = math.hypot(x - x0, y - y0)
    segments = max(3, min(26, int(distance / 14) + 3))
    path = _curved_path(x0, y0, x, y, segments)

    for i, (px_, py_) in enumerate(path):
        _orig_mouse_move(mouse_self, px_, py_)
        # Ease-in / ease-out: slower near the start and end, faster mid-path.
        t = i / max(1, len(path) - 1)
        ease = math.sin(math.pi * t)          # 0 → 1 → 0
        delay = MOUSE_MAX_STEP_DELAY - ease * (MOUSE_MAX_STEP_DELAY - MOUSE_MIN_STEP_DELAY)
        _orig_time_sleep(max(0.001, delay + _rand(-0.002, 0.002)))

    # Occasionally overshoot the target slightly then correct — humans
    # rarely land exactly on the first try when moving quickly.
    if random.random() < MOUSE_OVERSHOOT_CHANCE and distance > 20:
        ox, oy = x + _rand(-7, 7), y + _rand(-7, 7)
        _orig_mouse_move(mouse_self, ox, oy)
        _orig_time_sleep(_rand(0.02, 0.06))
        _orig_mouse_move(mouse_self, x, y)

    _last_pos[key] = (x, y)


def humanized_mouse_move(mouse_self, x: float, y: float, *, steps=None):
    if not _ENABLED:
        return _orig_mouse_move(mouse_self, x, y, steps=steps)
    return _humanized_move_impl(mouse_self, x, y, steps=steps)


def humanized_mouse_click(mouse_self, x: float, y: float, **kwargs):
    if not _ENABLED:
        return _orig_mouse_click(mouse_self, x, y, **kwargs)

    # Approach the point with a tiny random final offset, then settle.
    _humanized_move_impl(mouse_self, x + _rand(-2, 2), y + _rand(-2, 2))
    _orig_time_sleep(_rand(*CLICK_PRE_DELAY))
    kwargs.setdefault("delay", random.randint(*CLICK_DOWN_UP_MS))
    result = _orig_mouse_click(mouse_self, x, y, **kwargs)
    _orig_time_sleep(_rand(*CLICK_POST_DELAY))
    _last_pos[id(mouse_self)] = (x, y)
    return result


def humanized_locator_click(locator_self, **kwargs):
    if not _ENABLED:
        return _orig_locator_click(locator_self, **kwargs)
    try:
        box = locator_self.bounding_box()
        page = locator_self.page
        if box and page is not None:
            tx = box["x"] + box["width"] / 2 + _rand(-box["width"] * 0.15, box["width"] * 0.15)
            ty = box["y"] + box["height"] / 2 + _rand(-box["height"] * 0.15, box["height"] * 0.15)
            _humanized_move_impl(page.mouse, tx, ty)
            _orig_time_sleep(_rand(*CLICK_PRE_DELAY))
    except Exception:
        pass   # never let a humanization glitch break the real click
    return _orig_locator_click(locator_self, **kwargs)


def humanized_locator_fill(locator_self, value: str, **kwargs):
    if not _ENABLED:
        return _orig_locator_fill(locator_self, value, **kwargs)
    # The pause a person takes after focusing the box, right before
    # hitting Cmd+V to paste the (already-copied) prompt text.
    _orig_time_sleep(_rand(*FILL_PRE_DELAY))
    result = _orig_locator_fill(locator_self, value, **kwargs)
    # ...and the small beat afterwards before reaching for Enter/Submit.
    _orig_time_sleep(_rand(*FILL_POST_DELAY))
    return result


def humanized_locator_press(locator_self, key: str, **kwargs):
    if not _ENABLED:
        return _orig_locator_press(locator_self, key, **kwargs)
    _orig_time_sleep(_rand(*PRESS_PRE_DELAY))
    return _orig_locator_press(locator_self, key, **kwargs)


def humanized_keyboard_press(keyboard_self, key: str, **kwargs):
    if not _ENABLED:
        return _orig_keyboard_press(keyboard_self, key, **kwargs)
    _orig_time_sleep(_rand(*PRESS_PRE_DELAY))
    return _orig_keyboard_press(keyboard_self, key, **kwargs)


def humanized_wait_for_timeout(page_self, timeout: float):
    if not _ENABLED:
        return _orig_page_wait_for_timeout(page_self, timeout)
    jitter = timeout * _rand(WAIT_JITTER_LOW, WAIT_JITTER_HIGH)
    new_timeout = max(30.0, timeout + jitter)
    return _orig_page_wait_for_timeout(page_self, new_timeout)


def humanized_time_sleep(seconds: float):
    if not _ENABLED or seconds is None or seconds <= 0:
        return _orig_time_sleep(seconds)
    jitter = seconds * _rand(WAIT_JITTER_LOW, WAIT_JITTER_HIGH)
    extra = 0.0
    if random.random() < DISTRACTION_CHANCE:
        extra = _rand(*DISTRACTION_EXTRA_SEC)
    return _orig_time_sleep(max(0.01, seconds + jitter + extra))


def idle_mouse_wander(page) -> None:
    """Small, aimless mouse drift — useful to call occasionally while a
    script is otherwise just waiting (e.g. polling for image generation),
    so the browser isn't COMPLETELY motionless for 30-120s at a stretch,
    which is itself an unnatural pattern for a human sitting at the page.
    Safe to call frequently; it silently no-ops on any error."""
    if not _ENABLED:
        return
    try:
        vw = page.evaluate("() => window.innerWidth") or 1200
        vh = page.evaluate("() => window.innerHeight") or 800
        cx, cy = vw / 2, vh / 2
        tx = min(max(0, cx + _rand(-IDLE_WANDER_RADIUS, IDLE_WANDER_RADIUS)), vw - 1)
        ty = min(max(0, cy + _rand(-IDLE_WANDER_RADIUS, IDLE_WANDER_RADIUS)), vh - 1)
        _humanized_move_impl(page.mouse, tx, ty)
    except Exception:
        pass


# ──────────────────────────────────────────────
# Patch installation / removal
# ──────────────────────────────────────────────

def apply_patches() -> None:
    """
    Monkey-patch Playwright's Mouse / Locator / Keyboard / Page classes and
    the global `time.sleep`, so that every interaction performed anywhere
    (including deep inside an already-imported, UNMODIFIED
    automate_google_flow.py) automatically gains human-like timing and
    curved mouse movement.

    Idempotent — calling this more than once is a no-op after the first
    call. Safe to call at the very top of any entry-point script/process
    (including inside each spawned multiprocessing worker).
    """
    global _patched
    if _patched:
        return

    _Mouse.move             = humanized_mouse_move
    _Mouse.click            = humanized_mouse_click
    _Locator.click          = humanized_locator_click
    _Locator.fill           = humanized_locator_fill
    _Locator.press          = humanized_locator_press
    _Keyboard.press         = humanized_keyboard_press
    _Page.wait_for_timeout  = humanized_wait_for_timeout

    _time_module.sleep = humanized_time_sleep

    _patched = True
    print("  🧑 human_behavior: patches applied "
          "(curved mouse movement, paste pauses, jittered waits).")


def remove_patches() -> None:
    """Restore every original Playwright/`time` method. Mostly useful for
    tests; normal scripts can just leave patches installed for their whole
    process lifetime."""
    global _patched
    if not _patched:
        return

    _Mouse.move             = _orig_mouse_move
    _Mouse.click            = _orig_mouse_click
    _Locator.click          = _orig_locator_click
    _Locator.fill           = _orig_locator_fill
    _Locator.press          = _orig_locator_press
    _Keyboard.press         = _orig_keyboard_press
    _Page.wait_for_timeout  = _orig_page_wait_for_timeout
    _time_module.sleep      = _orig_time_sleep

    _patched = False
    print("  🧑 human_behavior: patches removed (back to raw Playwright timing).")



