#!/usr/bin/env python3
"""
Google Flow Image Generation Automation
========================================
Automates pasting prompts into Google Flow, waits for image generation,
and downloads each image before moving to the next prompt.

On every run the script scans the output folder first and skips any
prompt whose image already exists there.

Usage:
    python3 automate_google_flow.py

To resume / force-start from a specific prompt number:
    python3 automate_google_flow.py --start 25

To connect to your already-open Chrome browser:
    1. Quit Chrome completely
    2. Reopen with: open -a "Google Chrome" --args --remote-debugging-port=9222
    3. Run: python3 automate_google_flow.py --use-existing-browser
"""

import re
import time
import os
import sys
import json
import math
import signal
import argparse
import subprocess
import multiprocessing
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ──────────────────────────────────────────────
# CONFIGURATION – edit these if needed
GENERATION_TIMEOUT_SEC = 120   # 2 minutes – mark failed and skip if exceeded
POLL_INTERVAL_SEC      = 3     # how often to check whether generation finished
POST_DOWNLOAD_WAIT_SEC = 3     # pause after each download before next prompt
LOGIN_TIMEOUT_SEC      = 300   # 5 minutes per worker to complete Google sign-in
# ──────────────────────────────────────────────


# ──────────────────────────────────────────────
# GLOBAL STYLE BLOCK — single source of truth for the channel's visual DNA.
#
# Image Prompts.txt intentionally contains ONLY the scene description per
# entry — NOT this style text — per the current STATE 20/21 rules in
# "ver2 video generator.md" and the "Finalized Style Block" defined in the
# channel's own "visual analysis grim stick" file. Repeating this ~130-word
# block inside every one of hundreds of prompt entries would be pure
# duplication; instead it lives in exactly ONE place (here) and is appended
# to each scene description at the moment of actual submission to the image
# model (see process_one_prompt() below), so Nano Banana still receives the
# exact same full prompt it always did.
#
# ⚠️ If the channel's Finalized Style Block is ever revised in
# "visual analysis grim stick", this constant must be updated to match it
# EXACTLY — word for word, unparaphrased. This is the ONLY place in the
# entire automation pipeline where that text is allowed to live.
STYLE_BLOCK = (
    "STYLE IS CRITICAL: Make the image a simple, genuinely HAND-DRAWN 2D educational cartoon with crude, mildly distorted construction throughout EVERY required visual element—characters, objects, props, environmental forms, large color regions, silhouettes, and shared boundaries. Use subtle wobble, asymmetry, imperfect curves, awkward proportions, uneven joins, mildly distorted silhouettes, and irregular THICK BLACK OUTLINES. Never allow large or simple areas to become clean geometric rectangles, bands, panels, ruler-straight divisions, or mathematically perfect shapes unless the scene explicitly requires a man-made geometric edge. Flat colors must remain flat, but the shapes containing them must still have naturally irregular hand-drawn boundaries. Roughness must come ONLY from HAND-DRAWN CONSTRUCTION; keep rendering clean and normally resolved with NO deliberate low-resolution, pixel-stepped, jagged, or degraded finish. No clean vector geometry, and no pencil, crayon, brush, paper, or sketch texture. Use a VERY LIMITED, scene-appropriate palette of BOLD, BRIGHT, CLEAR, relatively SATURATED flat colors with strong visual separation and large solid color regions. Maintain an overall bright, clear, relatively saturated palette, while allowing secondary or background elements to use comparatively lower brightness or saturation when needed for visual hierarchy. No gradients, blending, subtle tonal modeling, glossy effects, realistic textures, or detailed shading. Include only script-required visual elements, simplifying each to the minimum recognizable hand-drawn shapes and lines with no decorative or realistic detail. For complete narrative or atmospheric scenes, fill the entire canvas edge-to-edge with NO accidental white gaps, exposed canvas, white margins, rounded white corners, borders, paper-like edges, or incomplete coverage. Internal boundaries may be irregular, but the overall scene must remain fully continuous to the canvas edges. Preserve intentional white or empty backgrounds only when explicitly required by the scene. Depth should rely mainly on simple layering, overlap, scale, and placement. The result should feel like a crude hand-drawn old educational cartoon, NOT polished vector artwork, low-resolution artwork, photorealism, 3D, or low-poly."
    "STYLE IS CRITICAL: Polished 2D digital illustration with bold, clean black ink outlines, crisp consistent linework, flat color fills, defined cel-shaded shadows, and subtle dimensional shading. Use scene-appropriate, muted natural colors, soft directional lighting, clearly defined cast shadows, moderate overall contrast, sharp silhouettes, detailed illustrated forms, and depth through perspective, scale, and layering. 1920×1080, 16:9 aspect ratio."
)

# ──────────────────────────────────────────────


# ──────────────────────────────────────────────
PROMPTS_FILE    = "/Users/apple/Downloads/idea project backuo/Youtube professional grade project/src/Analysis/grim mack/1: How Did Humans Domesticate Apple?/Image Prompts.txt"
OUTPUT_DIR      = "/Users/apple/Downloads/apple"
PROGRESS_FILE   = "/Users/apple/Downloads/apple/generation_progress.json"
# NOTE: must be a FILE path, not a directory – record_failed_prompt() opens
# this in append mode. Pointing it at the output dir raises
# "IsADirectoryError: [Errno 21] Is a directory" on every failure.
FAILED_REPORT   = "/Users/apple/Downloads/apple/failed_prompts.txt"  # live failure log

# Single-worker default (backward-compatible)
PROJECT_URL  = "https://flow.google.com/project/4712f479-3c89-47a9-b381-d1910a8007cd"

# Per-worker project URLs — Worker N uses PROJECT_URLS[N-1]
PROJECT_URLS = [
    "https://flow.google.com/project/4712f479-3c89-47a9-b381-d1910a8007cd",  # Worker 1
    "https://flow.google.com/u/1/project/eea939b3-44b4-4b1c-8346-d32631fc6c76",  # Worker 2
    "https://flow.google.com/u/1/project/5faf1eeb-4cd3-4337-999e-aa71030baf36",  # Worker 3
    "https://flow.google.com/project/f7408030-f0d7-4009-9d3b-7b4ed1b86615",  # Worker 4
]

USER_DATA_DIR    = os.path.expanduser("~/.flow_chrome_profile")  # stable – survives Downloads cleanup
# Use real Google Chrome so Google does not detect automation and force sign-out.
# Playwright's bundled Chromium triggers Google's bot-detection.
CHROME_EXECUTABLE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# ──────────────────────────────────────────────
# Watermark removal (GeminiWatermarkTool / gwt-mini)
# https://github.com/allenk/GeminiWatermarkTool
# Source repo lives at /Users/apple/IdeaProjects/GeminiWatermarkTool.
# The prebuilt CLI binary this script actually invokes is bundled at
# tools/gwt-mini next to this script (no build step required at runtime).
#
# LOCKED watermark positions — ONLY entries that have been empirically
# measured (pixel-diff) AND visually confirmed clean by direct inspection
# go in this table. DO NOT add a resolution here on pixel-diff evidence
# alone — a tight/plausible diff bbox is NOT sufficient proof (this bit us
# once: an earlier (1920,1080) guess looked correct via diff but produced a
# visible off-position smudge on real images). Any resolution NOT in this
# table falls back to plain --force with no --region (the tool's own
# built-in default profile for that size class) rather than a guessed
# region.
#   (1376, 768) → x=1255,y=647,w=48,h=48
#     Confirmed clean across the full "second batch" (100 imgs) and
#     "third batch" (100 imgs) — the exact logic this function must keep
#     using unchanged going forward. This is the NATIVE Nano Banana output
#     size — the normal pipeline (try_download_image → _save_png) always
#     removes the watermark at THIS resolution, before resize_to_1920x1080()
#     ever runs, specifically so the (1920,1080) case below is never needed
#     on that path.
#   (1920, 1080) → x=1758,y=909,w=68,h=68
#     For the EDGE CASE where an image was already resized to 1920x1080
#     BEFORE watermark removal happened (e.g. remove_watermarks_batch.py run
#     on pre-resized inputs, or any out-of-order manual workflow) — NOT the
#     normal pipeline path. Derived two independent ways that agreed to
#     sub-pixel precision: (1) the exact center-crop+LANCZOS transform
#     resize_to_1920x1080() itself applies, mapped onto the native
#     (1255,647,48,48) region → (1758.2,910.0,67.5,67.5); (2) gwt-mini's own
#     guided multi-scale NCC search (--snap) on the resized image → found
#     (1758,909) 68x68 at 77% confidence (raw NCC 0.860) completely
#     independently. Visually confirmed clean by direct before/after zoom
#     crop inspection on 2026-09-24.
# ──────────────────────────────────────────────

WATERMARK_LOCKED_REGIONS = {
    (1376, 768):  "1255,647,48,48",
    (1920, 1080): "1758,909,68,68",
}


def _default_watermark_binary() -> str:
    """
    Locate the gwt-mini / GeminiWatermarkTool executable.
    Priority: $GWT_BINARY env var  →  tools/gwt-mini next to this script
              →  a built binary inside the moved source repo (fallback).
    Returns "" if nothing usable is found (watermark removal is then
    skipped with a one-time warning, never blocking downloads).
    """
    here = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("GWT_BINARY", ""),
        str(here / "tools" / "gwt-mini"),
        str(here / "tools" / "GeminiWatermarkTool"),
        os.path.expanduser("~/IdeaProjects/GeminiWatermarkTool/build/gwt-mini"),
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


WATERMARK_BINARY  = _default_watermark_binary()
REMOVE_WATERMARKS = True     # --no-watermark-removal disables
WATERMARK_DENOISE = "ai"     # ai | ns | telea | soft | off  (--watermark-denoise)
WATERMARK_LEGACY  = False    # pin pre-Gemini-3.5 profile     (--watermark-legacy)

_watermark_warned_missing = False   # only print the "binary missing" warning once


def remove_watermark(filepath: str) -> bool:
    """
    Strip the visible Gemini "sparkle" watermark from an already-saved PNG,
    IN PLACE, using the bundled GeminiWatermarkTool CLI.

    Uses the LOCKED region for this image's exact pixel dimensions when
    known (see WATERMARK_LOCKED_REGIONS above); otherwise falls back to
    --force with the tool's built-in default position for the size class.
    Always passes --force (skip the tool's own detector) since our images
    are re-encoded through Pillow, which breaks the detector's NCC match
    even though the watermark is clearly present.

    Never raises — on any failure (binary missing, subprocess error,
    timeout, bad output) the original downloaded file is left untouched
    and False is returned so the caller can log/ignore it.
    """
    global _watermark_warned_missing
    if not REMOVE_WATERMARKS:
        return True
    if not WATERMARK_BINARY:
        if not _watermark_warned_missing:
            print("  ⚠ Watermark removal skipped – gwt-mini binary not found "
                  "(expected at tools/gwt-mini, or set GWT_BINARY env var). "
                  "Download it from https://github.com/allenk/GeminiWatermarkTool/releases")
            _watermark_warned_missing = True
        return False

    try:
        from PIL import Image
        with Image.open(filepath) as im:
            size = im.size
    except Exception as e:
        print(f"  ⚠ Watermark removal: could not read image size for "
              f"{os.path.basename(filepath)}: {e}")
        size = None

    region = WATERMARK_LOCKED_REGIONS.get(size) if size else None

    tmp_out = f"{filepath}.gwt_tmp.png"
    cmd = [
        WATERMARK_BINARY,
        "-i", filepath,
        "-o", tmp_out,
        "--force",
        "--denoise", WATERMARK_DENOISE,
        "--quiet",
    ]
    if region:
        cmd += ["--region", region]
    if WATERMARK_LEGACY:
        cmd.append("--legacy")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1000:
            os.replace(tmp_out, filepath)
            return True
        print(f"  ⚠ Watermark removal failed for {os.path.basename(filepath)} "
              f"(exit {result.returncode}): {(result.stderr or result.stdout).strip()[:200]}")
    except subprocess.TimeoutExpired:
        print(f"  ⚠ Watermark removal timed out for {os.path.basename(filepath)}")
    except Exception as e:
        print(f"  ⚠ Watermark removal error for {os.path.basename(filepath)}: {e}")
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except Exception:
                pass
    return False


# ──────────────────────────────────────────────
# Resize to fixed 1920×1080 (16:9)
#
# Strategy (per user-approved plan): when the source isn't already exactly
# 1920×1080, center-crop the tiny aspect-ratio excess (never pad/stretch),
# then resize with LANCZOS to exactly 1920×1080. For a typical 1376×768
# source (ratio 1.79167 vs target 1.77778) this only removes ~5-6px per
# side — no meaningful loss of composition.
# ──────────────────────────────────────────────

RESIZE_ENABLED = True   # --no-resize disables
TARGET_W, TARGET_H = 1920, 1080
_TARGET_RATIO = TARGET_W / TARGET_H


def resize_to_1920x1080(filepath: str) -> bool:
    """
    Center-crop any aspect-ratio excess, then resize to exactly
    TARGET_W×TARGET_H, overwriting filepath in place. No-op (returns True
    immediately) if the image is already exactly the target size.
    Never raises — on any failure the original file is left untouched.
    """
    if not RESIZE_ENABLED:
        return True
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            img.load()
            w, h = img.size
            if (w, h) == (TARGET_W, TARGET_H):
                return True   # already correct – nothing to do

            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

            source_ratio = w / h
            if source_ratio > _TARGET_RATIO:
                # Too wide – crop width, keep full height
                new_w = round(h * _TARGET_RATIO)
                left = (w - new_w) // 2
                img = img.crop((left, 0, left + new_w, h))
            elif source_ratio < _TARGET_RATIO:
                # Too tall – crop height, keep full width
                new_h = round(w / _TARGET_RATIO)
                top = (h - new_h) // 2
                img = img.crop((0, top, w, top + new_h))

            img = img.resize((TARGET_W, TARGET_H), Image.Resampling.LANCZOS)
            img.save(filepath, format="PNG")
        return True
    except Exception as e:
        print(f"  ⚠ Resize to {TARGET_W}x{TARGET_H} failed for "
              f"{os.path.basename(filepath)}: {e}")
        return False


# ──────────────────────────────────────────────
# Multi-worker helpers
# ──────────────────────────────────────────────

def get_worker_profile_dir(worker_id: int) -> str:
    """Each worker gets its own Chrome profile → different Google account per worker."""
    return os.path.expanduser(f"~/.flow_chrome_profile_{worker_id}")


def get_worker_progress_file(worker_id: int) -> str:
    """Separate progress file per worker so they never overwrite each other."""
    base, ext = os.path.splitext(PROGRESS_FILE)
    return f"{base}_worker{worker_id}{ext}"


# ──────────────────────────────────────────────
# Shared-Chrome-session helpers
#
# Both `--login-only` and a real generation run must end up talking to the
# EXACT SAME Chrome process/tabs for a given worker, so that anything the
# user does manually while `--login-only` is open (sign in, delete old
# generated images, etc.) is immediately visible once the real run attaches
# — instead of each command launching its own separate, short-lived Chrome.
#
# The trick: instead of Playwright *launching* the browser (which ties the
# browser's lifetime to that particular Python process / `sync_playwright()`
# block), we spawn Chrome ourselves as a fully DETACHED OS subprocess with
# remote debugging enabled, and always *attach* to it via connect_over_cdp().
# A detached Chrome keeps running after the launching Python process exits,
# so the next invocation (whichever command it is) just finds it already
# alive on the same port and reuses it as-is.
# ──────────────────────────────────────────────

def _cdp_port_alive(port: int, timeout: float = 1.5) -> bool:
    """True if a Chrome DevTools endpoint is already listening on `port`
    (e.g. left running from a previous --login-only session, or a previous
    real run that was Ctrl-C'd before it reached its own close)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=timeout):
            return True
    except Exception:
        return False


def ensure_chrome_running(profile_dir: str, port: int, wait_ready_sec: int = 30) -> None:
    """
    Guarantee that a real Google Chrome instance with remote debugging
    enabled on `port`, using `profile_dir` as its --user-data-dir, is
    running — spawning one (fully detached from this Python process) only
    if nothing is already listening on that port.

    If Chrome is ALREADY alive on `port` this is a no-op: the caller will
    connect_over_cdp() straight into that SAME browser, reusing every open
    tab, cookie, sign-in state, and manually-cleaned-up project exactly as
    the user left it. This is what makes `--login-only` and a later real
    `--workers N` run share one Chrome session per worker instead of each
    launching (and eventually closing) its own.
    """
    if _cdp_port_alive(port):
        print(f"  🔗 Chrome already running on port {port} – reusing that session.")
        return

    print(f"  🚀 Starting detached Chrome on port {port} (profile: {profile_dir}) …")
    os.makedirs(profile_dir, exist_ok=True)

    # Clean up stale lock files left by a previous SIGKILL/crash — otherwise
    # Chrome runs crash-recovery and may clear session cookies on launch.
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = os.path.join(profile_dir, lock)
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass

    args = [
        CHROME_EXECUTABLE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--restore-last-session",   # restore tabs/session after crash/kill
        "--password-store=basic",   # prevent keychain dialogs on macOS
    ]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,   # fully detach – keeps running after this script exits
    )

    deadline = time.time() + wait_ready_sec
    while time.time() < deadline:
        if _cdp_port_alive(port):
            return
        time.sleep(0.5)
    print(f"  ⚠ Chrome did not come up on port {port} within {wait_ready_sec}s "
          f"– continuing anyway, connect_over_cdp() may fail.")


def _pids_listening_on_port(port: int) -> list[int]:
    """PID(s) of the OS process(es) with an open LISTEN socket on `port`
    (i.e. the real Chrome process behind our CDP connection), found via
    `lsof`. Independent of Playwright/CDP entirely — used as the guaranteed
    fallback to actually kill Chrome when a graceful CDP Browser.close()
    doesn't (or can't, e.g. the Python process that owns the CDP socket was
    itself killed/orphaned before it got a chance to call it)."""
    try:
        out = subprocess.run(
            ["lsof", "-t", "-i", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return [int(pid) for pid in out.split() if pid.strip().isdigit()]
    except Exception:
        return []


def force_quit_chrome_on_port(port: int, wait_sec: float = 3.0) -> None:
    """
    Guarantee the real Chrome process bound to `port` actually exits.

    This is a NECESSARY fallback (not just a nice-to-have) because a plain
    CDP `browser.close()` can silently no-op in exactly the situation that
    caused "Ctrl+C doesn't close Chrome" in practice: when running
    `--workers N`, each worker's Chrome-closing logic lives inside a
    *child* process (spawned via multiprocessing) that only closes its own
    Chrome after IT PERSONALLY receives SIGINT. Depending on how the
    surrounding terminal/IDE delivers Ctrl+C, that signal sometimes only
    reaches the top-level parent process — the children (and their Chrome
    windows) are then orphaned and never get a chance to run their
    KeyboardInterrupt handler at all, no matter how correct that handler
    is. Killing the real OS process by PID (found via the TCP listen
    socket, independent of Playwright/CDP) works regardless of which
    process actually receives the interrupt.
    """
    pids = _pids_listening_on_port(port)
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + wait_sec
    while time.time() < deadline and _pids_listening_on_port(port):
        time.sleep(0.2)
    for pid in _pids_listening_on_port(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def robust_close_chrome(browser, port: int) -> None:
    """
    Close a worker's shared Chrome for good, using a graceful CDP
    Browser.close() as a first attempt (in case it works and is cleaner),
    then UNCONDITIONALLY verifying via the real TCP port and force-killing
    the OS process if it's still alive. See force_quit_chrome_on_port()
    for why the fallback is required, not optional.
    """
    try:
        browser.close()
    except Exception:
        pass
    if _cdp_port_alive(port):
        force_quit_chrome_on_port(port)


def split_prompts_for_workers(prompts: list, num_workers: int) -> list[list]:
    """
    Divide sorted prompts evenly among num_workers chunks.
      Worker 1 → first chunk
      Worker N → last (possibly smaller) chunk
    """
    n = len(prompts)
    chunk_size = math.ceil(n / num_workers)
    return [prompts[i * chunk_size : (i + 1) * chunk_size]
            for i in range(num_workers)]


def describe_worker_ranges(prompts: list, num_workers: int):
    """Print the prompt index range that each worker will own."""
    chunks = split_prompts_for_workers(prompts, num_workers)
    for w_idx, chunk in enumerate(chunks):
        if chunk:
            first, last = chunk[0]["index"], chunk[-1]["index"]
            print(f"  Worker {w_idx+1}: prompts {first}–{last}  ({len(chunk)} prompts)")
        else:
            print(f"  Worker {w_idx+1}: (no prompts assigned)")

def parse_prompts(filepath: str) -> list[dict]:
    """Return list of {index, timestamp, text} dicts parsed from the prompts file.

    Supports three formats:
      Format A: [PROMPT 1][00:00] text...
      Format B: [00:00:00] text...            (prompt number auto-assigned)
      Format C: PROMPT 001 / Full ChatGPT prompt: text...  (multi-line block)
    """
    prompts = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # ── Format C detection: file uses "PROMPT NNN" headings ───────────────
    # If the file contains lines like "PROMPT 001" (no brackets) we use the
    # block parser instead of the line-by-line parser.
    if re.search(r"^PROMPT\s+\d+\s*$", content, re.MULTILINE):
        # Split into blocks on blank lines separating each PROMPT
        blocks = re.split(r"\n{2,}", content)
        current_index = None
        current_text  = None
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            # Start of a new PROMPT block
            m = re.match(r"^PROMPT\s+(\d+)", block)
            if m:
                # Save previous if complete
                if current_index is not None and current_text:
                    prompts.append({
                        "index":     current_index,
                        "timestamp": "00:00",
                        "text":      current_text.strip(),
                    })
                current_index = int(m.group(1))
                current_text  = None
                # Look for "Full ChatGPT prompt:" inside this block
                fm = re.search(
                    r"Full ChatGPT prompt:\s*\n([\s\S]+?)(?:\n[A-Z][^\n]*:|$)",
                    block
                )
                if fm:
                    current_text = fm.group(1).strip()
                else:
                    # Fall back to "Image description:"
                    dm = re.search(r"Image description:\s*\n(.+)", block)
                    if dm:
                        current_text = dm.group(1).strip()
        # Save the last block
        if current_index is not None and current_text:
            prompts.append({
                "index":     current_index,
                "timestamp": "00:00",
                "text":      current_text.strip(),
            })
        return prompts

    # ── Format A / B: block-based parser (handles multi-line prompt text) ─
    # The header ([PROMPT N][ts] or [ts]) may have the prompt body on the
    # same line OR spread across the following lines until the next header.
    auto_index = 0
    lines = content.splitlines()
    i = 0

    # Regex patterns compiled once
    RE_A = re.compile(
        r"^\[PROMPT\s+(\d+)\]\s*\[(\d+:\d+(?::\d+)?(?:[,\.]\d+)?)\]\s*(.*)")
    RE_B = re.compile(r"^\[(\d+:\d+(?::\d+)?)\]\s*(.*)")
    # A header starts with one of the two bracket patterns
    RE_HEADER = re.compile(
        r"^\[PROMPT\s+\d+\]|^\[\d+:\d+")

    while i < len(lines):
        line = lines[i].strip()

        # ── Format A ─────────────────────────────────────────────────────
        m = RE_A.match(line)
        if m:
            prompt_index = int(m.group(1))
            timestamp    = m.group(2)
            text_parts   = [m.group(3).strip()] if m.group(3).strip() else []
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if RE_HEADER.match(nxt):   # next prompt header → stop
                    break
                if nxt:
                    text_parts.append(nxt)
                i += 1
            text = " ".join(text_parts).strip()
            if text:
                prompts.append({
                    "index":     prompt_index,
                    "timestamp": timestamp,
                    "text":      text,
                })
            continue

        # ── Format B ─────────────────────────────────────────────────────
        m = RE_B.match(line)
        if m and (m.group(2).strip() or i + 1 < len(lines)):
            auto_index  += 1
            text_parts   = [m.group(2).strip()] if m.group(2).strip() else []
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if RE_HEADER.match(nxt):
                    break
                if nxt:
                    text_parts.append(nxt)
                i += 1
            text = " ".join(text_parts).strip()
            if text:
                prompts.append({
                    "index":     auto_index,
                    "timestamp": m.group(1),
                    "text":      text,
                })
            continue

        i += 1

    return prompts


def prompt_filename(prompt_index: int) -> str:
    """Canonical filename for a given prompt number, e.g. 1.png, 42.png"""
    return f"{prompt_index}.png"


def _looks_like_png(data: bytes) -> bool:
    return bool(data) and data.startswith(b"\x89PNG\r\n\x1a\n")


def ensure_png_bytes(data: bytes) -> bytes | None:
    """
    Always returns TRUE PNG bytes, regardless of what format the CDN
    actually served (WEBP/JPEG/AVIF/etc.).

    We always want files saved as plain "{index}.png". If the CDN honours
    our Accept header and returns WEBP/AVIF instead of PNG, writing those
    raw bytes into a ".png" file fools the extension but not the real
    format — Finder/QuickLook then fails to decode it and shows a generic
    icon (no thumbnail), even though content-sniffing apps still open it.

    Re-encoding through Pillow guarantees the bytes on disk are ALWAYS a
    genuine PNG, so the .png extension is never a lie and thumbnails always
    generate correctly.
    """
    if not data:
        return None
    if _looks_like_png(data):
        return data   # already real PNG – nothing to do
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        img.load()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"  ⚠ Could not re-encode image to real PNG ({e})")
        return None



def normalize_existing_to_png(output_dir: str):
    """
    Startup repair pass: any numbered image file that isn't ALREADY a real
    PNG (wrong format saved with .png extension, or saved with a different
    extension entirely from an earlier version of this script) gets
    re-encoded to a genuine PNG and saved as "{index}.png". Old
    non-.png files are removed after a successful conversion.
    Safe to run every launch – it's a no-op once everything is real PNG.
    """
    IMAGE_EXT = {".jpeg", ".jpg", ".png", ".webp", ".gif", ".avif"}
    if not os.path.isdir(output_dir):
        return
    fixed = 0
    for fname in os.listdir(output_dir):
        base, ext = os.path.splitext(fname)
        if ext.lower() not in IMAGE_EXT or not base.isdigit():
            continue
        fpath = os.path.join(output_dir, fname)
        try:
            with open(fpath, "rb") as fh:
                data = fh.read()
        except Exception:
            continue
        if ext.lower() == ".png" and _looks_like_png(data):
            continue   # already correct – nothing to do
        png_data = ensure_png_bytes(data)
        if not png_data:
            continue   # couldn't decode – leave file untouched
        png_path = os.path.join(output_dir, f"{base}.png")
        try:
            with open(png_path, "wb") as fh:
                fh.write(png_data)
            if fpath != png_path:
                os.remove(fpath)
            fixed += 1
        except Exception:
            pass
    if fixed:
        print(f"  🔧 Re-encoded {fixed} file(s) to genuine PNG (fixes Finder thumbnail icons).")


def get_already_downloaded(output_dir: str) -> set[int]:
    """
    Scan output_dir for image files whose base-name is a plain number.
    e.g.  3.jpeg / 50.png / 100.webp  →  {3, 50, 100}
    Only counts recognised image extensions so chrome_profile/ etc. are ignored.
    """
    IMAGE_EXT = {".jpeg", ".jpg", ".png", ".webp", ".gif"}
    existing: set[int] = set()
    if not os.path.isdir(output_dir):
        return existing
    for fname in os.listdir(output_dir):
        base, ext = os.path.splitext(fname)
        if ext.lower() in IMAGE_EXT and base.isdigit():
            existing.add(int(base))
    return existing


def load_progress() -> int:
    """Return the index of the last successfully completed prompt (0 = none)."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f).get("last_completed", 0)
        except Exception:
            pass
    return 0


def save_progress(prompt_index: int, progress_file: str = PROGRESS_FILE):
    os.makedirs(os.path.dirname(os.path.abspath(progress_file)), exist_ok=True)
    with open(progress_file, "w") as f:
        json.dump({"last_completed": prompt_index}, f, indent=2)


def record_failed_prompt(prompt_index: int, worker_id: int, reason: str = ""):
    """
    Append a failed prompt number to FAILED_REPORT immediately.
    Uses fcntl.flock so concurrent workers never corrupt the file.
    File stays small – one line per failure, e.g.:
        42  [Worker 2]  timeout
        87  [Worker 1]  download failed
    """
    import fcntl
    os.makedirs(os.path.dirname(os.path.abspath(FAILED_REPORT)), exist_ok=True)
    line = f"{prompt_index}  [Worker {worker_id}]"
    if reason:
        line += f"  {reason}"
    line += "\n"
    with open(FAILED_REPORT, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ──────────────────────────────────────────────
# Google Flow UI helpers
# ──────────────────────────────────────────────

INPUT_SELECTORS = [
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea',
    '[placeholder*="create" i]',
    '[placeholder*="what" i]',
    '[aria-label*="prompt" i]',
    '[aria-label*="input" i]',
    '[aria-label*="message" i]',
]

SUBMIT_SELECTORS = [
    'button[aria-label*="send" i]',
    'button[aria-label*="submit" i]',
    'button[aria-label*="generate" i]',
    'button[type="submit"]',
    'button:has(svg)',
]

DOWNLOAD_SELECTORS = [
    'button[aria-label="Download"]',
    'button[aria-label="download"]',
    'button[aria-label*="Download" i]',
    'button[aria-label*="save" i]',
    '[data-tooltip*="download" i]',
    'button[title*="download" i]',
    '[jsaction*="download" i]',
    '[class*="download" i]',
    '[data-action="download"]',
    'a[download]',
    'button:has-text("Download")',
    'button:has-text("Save")',
    '[role="menuitem"]:has-text("Download")',
    '[role="menuitem"]:has-text("Save image")',
]

IMG_SELECTORS = [
    'img[src*="blob"]',
    'img[src*="googleapis"]',
    'img[src*="generativelanguage"]',
    'img[src*="aisrv"]',
    'img[src*="google"]',
    'figure img',
    'div[data-testid*="image"] img',
    'img[alt*="generated" i]',
    # wide fallback: any large img
    'img',
]


def find_input(page):
    for sel in INPUT_SELECTORS:
        try:
            els = page.locator(sel)
            count = els.count()
            if count > 0:
                for idx in range(count - 1, -1, -1):
                    el = els.nth(idx)
                    if el.is_visible():
                        return el
        except Exception:
            continue
    return None


def find_submit(page):
    for sel in SUBMIT_SELECTORS:
        try:
            els = page.locator(sel)
            count = els.count()
            if count > 0:
                for idx in range(count - 1, -1, -1):
                    el = els.nth(idx)
                    if el.is_visible() and el.is_enabled():
                        return el
        except Exception:
            continue
    return None


def count_images(page) -> int:
    """
    Count FULLY DECODED generated images by checking naturalWidth > 200.

    naturalWidth is 0 until image bytes are actually decoded – no need for
    img.complete.  Do NOT use img.width (CSS layout width) as fallback –
    that counts loading placeholders and inflates before_count.
    """
    try:
        n = page.evaluate("""
            () => {
                let count = 0;
                for (const img of document.querySelectorAll('img')) {
                    if (img.naturalWidth > 200 && img.naturalHeight > 200)
                        count++;
                }
                return count;
            }
        """)
        if n is not None:
            return int(n)
    except Exception:
        pass

    SPECIFIC_SELECTORS = [
        'img[src*="blob"]', 'img[src*="googleapis"]',
        'img[src*="generativelanguage"]', 'img[src*="aisrv"]',
        'figure img', 'img[alt*="generated" i]',
    ]
    for sel in SPECIFIC_SELECTORS:
        try:
            n = page.locator(sel).count()
            if n > 0:
                return n
        except Exception:
            continue
    return 0


def wait_for_new_image(page, before_srcs: list, timeout_sec: int) -> bool:
    """
    Poll until a new generated image appears whose src was NOT in before_srcs.

    Uses URL-based detection instead of count-based detection.
    Google Flow uses Virtuoso virtual scrolling: the DOM image count can stay
    constant (old items unloaded as new ones load), so counts are unreliable.
    Scrolling to the top before each poll ensures the newest card is rendered.
    """
    before_set = set(before_srcs)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SEC)
        # Scroll the Virtuoso scroller to the top so the newest card renders
        try:
            page.evaluate("""
                () => {
                    const s = document.querySelector(
                        '[data-virtuoso-scroller="true"]');
                    if (s) s.scrollTop = 0;
                }
            """)
            page.wait_for_timeout(300)
        except Exception:
            pass
        # Look for any loaded generated image whose URL is new.
        # IMPORTANT: use the SAME broad filter as before_srcs (any <img> with
        # naturalWidth > 0, not a data: URI). The old selector
        # img[alt="Generated image"] doesn't match Flow's current markup –
        # it never finds anything, so every prompt times out at 120s even
        # though the image genuinely appeared on screen.
        try:
            current = page.evaluate("""
                () => Array.from(document.querySelectorAll('img'))
                    .filter(i => (i.naturalWidth || 0) > 200
                                 && i.src && !i.src.startsWith('data:'))
                    .map(i => i.src)
            """) or []
            if any(src not in before_set for src in current):
                return True
        except Exception:
            pass
    return False


# ──────────────────────────────────────────────
# Google Flow – generation settings (per-prompt)
# ──────────────────────────────────────────────

def _mouse_at(page, info: dict, label: str) -> bool:
    """Move mouse to info{x,y} and do a real click.  info is the dict returned by JS."""
    if not info:
        print(f"     ✗ [{label}] not found")
        return False
    page.mouse.move(info["x"], info["y"])
    page.wait_for_timeout(150)
    page.mouse.click(info["x"], info["y"])
    print(f"     ✓ [{label}] @ ({info['x']:.0f},{info['y']:.0f})  '{info.get('text','')[:55]}'")
    return True


def setup_generation_settings(page):
    """
    Interact with the Google Flow settings popup:
      1. Click the CHIP in the bottom-right  (model name + quantity, e.g. 'Nano Banana 2 Lite  x2')
         → use SMALLEST-AREA element so we don't hit the parent container
      2. Click the ▼ combobox INSIDE the popup  (above the bottom toolbar)
      3. Click 'Nano Banana 2'  (exclude Lite / Pro)
      4. Click 'x1' button  (inside the popup, not the chip label)
      5. Escape to close
    All interactions use page.mouse.click(x,y) – real OS mouse events.
    """
    print("  ⚙  Settings: Nano Banana 2 + x1 …")
    H = "window.innerHeight"   # shorthand

    # ──────────────────────────────────────────────────────────────────────
    # STEP 1 – click the model/quantity chip in the bottom-right
    # Use TreeWalker to find the SMALLEST element that contains both
    # a model keyword (Banana/Nano/Lite/Flash) AND a quantity (x1/x2/x3/x4).
    # Smallest area = the chip itself, not its parent container.
    # ──────────────────────────────────────────────────────────────────────
    chip = page.evaluate(f"""
        () => {{
            const MODELS = ['Banana', 'Nano', 'Lite', 'Flash'];
            const QTYS   = ['x1', 'x2', 'x3', 'x4'];
            let best = null, bestArea = Infinity;
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {{
                const r = node.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (r.top < {H} * 0.65) continue;          // must be in bottom ~35 %
                const t = (node.innerText || '').trim();
                if (!t) continue;
                if (MODELS.some(k => t.includes(k)) && QTYS.some(q => t.includes(q))) {{
                    const area = r.width * r.height;
                    if (area < bestArea) {{
                        bestArea = area;
                        best = {{ x: r.left + r.width/2, y: r.top + r.height/2,
                                  text: t.substring(0, 60) }};
                    }}
                }}
            }}
            return best;
        }}
    """)
    _mouse_at(page, chip, "chip (open popup)")
    page.wait_for_timeout(1_100)   # wait for popup animation

    # ──────────────────────────────────────────────────────────────────────
    # STEP 2 – click the ▼ combobox INSIDE the popup
    # The popup is rendered ABOVE the bottom toolbar, so we filter
    # for elements with y  <  innerHeight * 0.80
    # ──────────────────────────────────────────────────────────────────────
    dropdown = page.evaluate(f"""
        () => {{
            const MODELS = ['Banana', 'Nano', 'Lite', 'Flash'];
            // Prefer proper combobox roles
            for (const sel of ["[role='combobox']","[aria-haspopup='listbox']",
                                "[aria-haspopup='true']","select"]) {{
                for (const el of document.querySelectorAll(sel)) {{
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top < {H} * 0.80)
                        return {{ x: r.left + r.width/2, y: r.top + r.height/2,
                                  text: (el.innerText||'').substring(0,40) }};
                }}
            }}
            // Fallback: smallest element in popup zone containing model keyword
            let best = null, bestArea = Infinity;
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {{
                const r = node.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (r.top >= {H} * 0.80) continue;         // must be in popup, not toolbar
                if (r.top < 50) continue;                  // skip header/nav
                const t = (node.innerText || '').trim();
                if (MODELS.some(k => t.includes(k)) && t.length < 80) {{
                    const area = r.width * r.height;
                    if (area < bestArea) {{
                        bestArea = area;
                        best = {{ x: r.left + r.width/2, y: r.top + r.height/2,
                                  text: t.substring(0, 50) }};
                    }}
                }}
            }}
            return best;
        }}
    """)
    _mouse_at(page, dropdown, "dropdown ▼")
    page.wait_for_timeout(700)

    # ──────────────────────────────────────────────────────────────────────
    # STEP 3 – click "Nano Banana 2"  (must NOT contain Lite or Pro)
    # ──────────────────────────────────────────────────────────────────────
    option = page.evaluate("""
        () => {
            const tags = "[role='option'],[role='menuitem'],li,button,div,span";
            let best = null, bestArea = Infinity;
            for (const el of document.querySelectorAll(tags)) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const t = (el.innerText || el.textContent || '').trim();
                if (t.includes('Nano Banana 2') && !t.includes('Lite')
                        && !t.includes('Pro')) {
                    const area = r.width * r.height;
                    if (area < bestArea) {
                        bestArea = area;
                        best = { x: r.left + r.width/2, y: r.top + r.height/2,
                                 text: t.substring(0, 50) };
                    }
                }
            }
            return best;
        }
    """)
    _mouse_at(page, option, "Nano Banana 2 option")
    page.wait_for_timeout(800)   # quantity buttons animate in after model change

    # ──────────────────────────────────────────────────────────────────────
    # STEP 4 – click x1  (inside the popup, NOT the status chip in toolbar)
    # Filter: must be above the bottom toolbar  (y < innerHeight * 0.85)
    # ──────────────────────────────────────────────────────────────────────
    x1 = page.evaluate(f"""
        () => {{
            for (const el of document.querySelectorAll(
                    "button,[role='button'],[role='option']")) {{
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (r.top >= {H} * 0.85) continue;         // skip toolbar chip
                const t = (el.innerText || el.textContent || '').trim();
                if (t === 'x1')
                    return {{ x: r.left + r.width/2, y: r.top + r.height/2 }};
            }}
            return null;
        }}
    """)
    _mouse_at(page, x1, "x1 quantity")
    page.wait_for_timeout(300)

    # ──────────────────────────────────────────────────────────────────────
    # STEP 5 – close popup
    # ──────────────────────────────────────────────────────────────────────
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    print("  ⚙  Done.\n")




# ──────────────────────────────────────────────
# Download helper
# ──────────────────────────────────────────────

def try_download_image(page, prompt_info: dict, output_dir: str,
                       before_srcs: list | None = None) -> bool:
    """
    Download the image that was JUST generated for this prompt.

    before_srcs  – list of img.src values captured before the prompt was
                   submitted.  Used to identify the NEW image.

    Filename = {prompt_index}.png  — ALWAYS a genuine PNG (any other
    format the CDN returns gets re-encoded via Pillow before saving,
    see ensure_png_bytes()).

    Strategy order:
      1. Python urllib + session cookies  ← bypasses CORS, most reliable
      2. ⋮ menu → Download
      3. JS canvas dump
    """
    filename = prompt_filename(prompt_info["index"])
    filepath = os.path.join(output_dir, filename)
    before_set = set(before_srcs or [])

    def _save_png(data: bytes) -> bool:
        """Re-encode to real PNG (if needed), write to filepath, then run
        the fixed 3-stage pipeline in this EXACT order:
          1. Write the raw download to disk (native 1376x768 from Nano Banana)
          2. remove_watermark()   – strip the sparkle at the LOCKED
             1376x768 region (1255,647,48,48), proven clean on the full
             second/third batch (200 images).
          3. resize_to_1920x1080() – center-crop tiny aspect excess + resize
             to the final 1920x1080 output size.
        Order matters: watermark removal MUST happen before resize, since
        resizing would shift/stretch the watermark's pixel position and
        break the locked-region match."""
        png_data = ensure_png_bytes(data)
        if not png_data:
            return False
        with open(filepath, "wb") as fh:
            fh.write(png_data)
        remove_watermark(filepath)
        resize_to_1920x1080(filepath)
        return True

    # ── Helper: download URL with Python urllib + browser session cookies ──
    # browser fetch() is CORS-blocked when the image redirects to a CDN on a
    # different domain.  Python urllib has NO CORS restrictions.
    def python_fetch(url: str) -> bool:
        import urllib.request
        import ssl
        try:
            all_cookies = page.context.cookies()
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}" for c in all_cookies
            )
            req = urllib.request.Request(url)
            req.add_header("Cookie", cookie_str)
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36",
            )
            req.add_header("Referer", "https://labs.google/")
            # Strongly prefer PNG so the CDN doesn't hand us WEBP/AVIF –
            # ensure_png_bytes() re-encodes anyway as a safety net.
            req.add_header("Accept", "image/png,image/*;q=0.5,*/*;q=0.1")

            # ── SSL context ──────────────────────────────────────────────
            # This macOS Python install lacks a wired-up CA bundle, causing
            # CERTIFICATE_VERIFY_FAILED on every request. Prefer certifi's
            # bundle; fall back to an unverified context (fine here – we're
            # only fetching Google's own signed image CDN URLs).
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl._create_unverified_context()

            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                data = resp.read()
            if len(data) > 5_000:
                return _save_png(data)
        except Exception as e:
            print(f"  ⚠ python_fetch error: {e}")
        return False

    # ── JS helper: find the card container of the NEW image ────────────────
    # A "new" image is one whose src was NOT present before we submitted.
    # If before_set is empty (first prompt), just take the last large image.
    FIND_NEW_CARD_JS = """
        (beforeSrcs) => {
            const beforeSet = new Set(beforeSrcs);
            let targetImg = null;

            for (const img of document.querySelectorAll('img')) {
                // naturalWidth > 0 means real pixels decoded – no need for img.complete
                // Never check img.width (CSS size) – it's non-zero even for placeholders
                if (img.naturalWidth <= 200 || img.naturalHeight <= 200) continue;
                if (img.src.startsWith('data:')) continue;
                const r = img.getBoundingClientRect();
                if (r.width < 50 || r.height < 50) continue;

                // Prefer an image whose src is genuinely new
                if (!beforeSet.has(img.src)) {
                    targetImg = img;
                    break;   // take the first new one found
                }
                // Fallback: remember the last loaded large image
                targetImg = img;
            }

            if (!targetImg) return null;

            // Walk up to the card container
            const ir = targetImg.getBoundingClientRect();
            let el = targetImg;
            for (let i = 0; i < 8; i++) {
                if (!el.parentElement) break;
                const pr = el.parentElement.getBoundingClientRect();
                if (pr.width > window.innerWidth * 0.9) break;
                el = el.parentElement;
            }
            const cr = el.getBoundingClientRect();
            return {
                cx:  cr.left + cr.width / 2,
                cy:  cr.top  + cr.height / 2,
                src: targetImg.src
            };
        }
    """

    # ──────────────────────────────────────────────────────────────────────
    # STRATEGY 1 – authenticated fetch of the new image URL (fastest)
    # The img.src is already in the DOM; fetch it inside the browser so
    # session cookies are sent automatically.  Handles redirects too.
    # ──────────────────────────────────────────────────────────────────────
    try:
        card = page.evaluate(FIND_NEW_CARD_JS, list(before_set))
        if card and card.get("src"):
            src = card["src"]
            print(f"     fetching: {src[:80]} …")
            if python_fetch(src):
                print(f"  ✓ Downloaded via URL fetch      →  {filename}")
                return True
            print("  ⚠ URL fetch returned no data – trying ⋮ menu …")
    except Exception as e:
        print(f"  ⚠ Strategy 1 (URL fetch) error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # STRATEGY 2 – hover NEW card → click ⋮ → click "Download"
    # ──────────────────────────────────────────────────────────────────────
    try:
        card = page.evaluate(FIND_NEW_CARD_JS, list(before_set))

        if card:
            page.mouse.move(card["cx"], card["cy"])
            page.wait_for_timeout(800)

            dots = page.evaluate("""
                () => {
                    for (const btn of document.querySelectorAll('button')) {
                        const t   = (btn.innerText || btn.textContent || '').trim();
                        const lbl = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const r   = btn.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (t.includes('more_vert') || t.includes('more_horiz')
                                || lbl.includes('more') || lbl.includes('option')
                                || t === '⋮' || t === '···' || t === '…') {
                            return { x: r.left + r.width/2, y: r.top + r.height/2,
                                     dbg: (lbl || t).substring(0, 30) };
                        }
                    }
                    return null;
                }
            """)

            if dots:
                print(f"     ⋮ button: '{dots['dbg']}'")
                page.mouse.click(dots["x"], dots["y"])
                page.wait_for_timeout(700)

                dl_item = page.evaluate("""
                    () => {
                        for (const sel of ['[role="menuitem"]','li','[role="option"]',
                                           'button','div','span']) {
                            for (const el of document.querySelectorAll(sel)) {
                                const t = (el.innerText || el.textContent || '').trim();
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0
                                        && t.toLowerCase() === 'download') {
                                    return { x: r.left + r.width/2,
                                             y: r.top  + r.height/2 };
                                }
                            }
                        }
                        return null;
                    }
                """)

                if dl_item:
                    with page.expect_download(timeout=30_000) as dl_ctx:
                        page.mouse.click(dl_item["x"], dl_item["y"])
                    download = dl_ctx.value
                    tmp_path = f"{filepath}.tmp_download"
                    download.save_as(tmp_path)
                    try:
                        with open(tmp_path, "rb") as fh:
                            raw = fh.read()
                    except Exception:
                        raw = b""
                    ok = _save_png(raw)
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    if ok:
                        print(f"  ✓ Downloaded via ⋮ → Download  →  {filename}")
                        return True
                    print("  ⚠ ⋮ → Download produced an unreadable image – trying canvas dump …")

                page.keyboard.press("Escape")
                page.wait_for_timeout(400)

    except Exception as e:
        print(f"  ⚠ Strategy 2 (⋮ menu) error: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # STRATEGY 3 – JS canvas dump (last resort)
    # ──────────────────────────────────────────────────────────────────────
    try:
        result = page.evaluate(
            """async (beforeSrcs) => {
                const beforeSet = new Set(beforeSrcs);
                const imgs = Array.from(document.querySelectorAll('img'))
                    .filter(i => (i.naturalWidth||0) > 200
                                 && i.src && !i.src.startsWith('data:'));
                const img = imgs.find(i => !beforeSet.has(i.src)) || imgs[imgs.length-1];
                if (!img) return null;
                try {
                    const r = await fetch(img.src, {
                        credentials: 'include',
                        headers: { 'Accept': 'image/png,image/*;q=0.5,*/*;q=0.1' }
                    });
                    if (r.ok) {
                        const buf = await r.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    }
                } catch(_) {}
                try {
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth; c.height = img.naturalHeight;
                    c.getContext('2d').drawImage(img, 0, 0);
                    const b64 = c.toDataURL('image/png').split(',')[1];
                    const bin = atob(b64);
                    return Array.from({length: bin.length},
                                      (_, i) => bin.charCodeAt(i));
                } catch(_) {}
                return null;
            }""",
            list(before_set),
        )
        if result and len(result) > 5_000:
            # NOTE: the JS above tries a raw fetch() FIRST (may return
            # whatever format the CDN served) and only falls back to a
            # canvas re-encode (guaranteed real PNG) if that fails.
            # _save_png() re-encodes via Pillow if it isn't already PNG,
            # so the file on disk is always a genuine PNG named "<n>.png".
            if _save_png(bytes(result)):
                print(f"  ✓ Downloaded via JS canvas      →  {filename}")
                return True
    except Exception as e:
        print(f"  ⚠ Strategy 3 (JS canvas) error: {e}")

    print(f"  ✗ All download strategies failed for prompt {prompt_info['index']}")
    return False


# ──────────────────────────────────────────────
# Per-prompt processing (shared by static AND dynamic-queue modes)
# ──────────────────────────────────────────────

def process_one_prompt(page, prompt_info: dict, output_dir: str, worker_id: int,
                        worker_progress_file: str, total_label) -> bool:
    """
    Submit ONE prompt, wait for the image, download it, and record success /
    failure.  Shared by both the static per-worker list loop and the dynamic
    shared-queue loop so behaviour is identical in both modes.

    Returns True on a successful download, False otherwise.
    Raises KeyboardInterrupt upward unchanged so callers can stop cleanly.
    """
    idx = prompt_info["index"]
    print(f"{'─'*60}")
    print(f"Prompt {idx}/{total_label}  [{prompt_info['timestamp']}]")
    print(f"  {prompt_info['text'][:120]} …")

    try:
        # Scroll to top so Virtuoso renders the newest card,
        # then snapshot which generated images already exist.
        page.evaluate("""
            () => {
                const s = document.querySelector(
                    '[data-virtuoso-scroller="true"]');
                if (s) s.scrollTop = 0;
            }
        """)
        page.wait_for_timeout(400)

        before_srcs: list[str] = page.evaluate("""
            () => Array.from(document.querySelectorAll('img'))
                .filter(i => (i.naturalWidth||0) > 0
                             && i.src && !i.src.startsWith('data:'))
                .map(i => i.src)
        """) or []

        inp = find_input(page)
        if not inp:
            print("  ✗ Input field not found, skipping …")
            return False

        inp.click()
        page.wait_for_timeout(300)
        page.keyboard.press("Meta+a")
        page.keyboard.press("Delete")
        page.wait_for_timeout(200)
        # Normalise to a single line – newlines in the text would
        # trigger an accidental Enter / form submission in the chat box.
        # Image Prompts.txt intentionally stores ONLY the scene description
        # (no style text — see STYLE_BLOCK comment near the top of this
        # file). The channel's Finalized Style Block is appended here, once,
        # immediately before submission, so the model still receives the
        # exact same full prompt every time.
        scene_text = " ".join(prompt_info["text"].split())
        clean_text = f"{scene_text} {STYLE_BLOCK}"
        inp.fill(clean_text)
        page.wait_for_timeout(500)

        submit_btn = find_submit(page)
        if submit_btn:
            submit_btn.click()
            print("  → Submitted via button")
        else:
            inp.press("Enter")
            print("  → Submitted via Enter key")

        print(f"  … Waiting for image (up to {GENERATION_TIMEOUT_SEC}s) …")
        generated = wait_for_new_image(page, before_srcs, GENERATION_TIMEOUT_SEC)

        if not generated:
            print(f"  ✗ FAILED – no image within {GENERATION_TIMEOUT_SEC}s – skipping prompt {idx}")
            record_failed_prompt(idx, worker_id, "timeout")
            ss_path = os.path.join(output_dir, f"failed_prompt_{idx:03d}.png")
            try:
                page.screenshot(path=ss_path)
                print(f"    Screenshot saved: {ss_path}")
            except Exception:
                pass
            time.sleep(POST_DOWNLOAD_WAIT_SEC)
            return False

        print("  ✓ Image appeared!")
        page.wait_for_timeout(2000)

        success = try_download_image(page, prompt_info, output_dir, before_srcs)
        if success:
            save_progress(idx, worker_progress_file)
        else:
            record_failed_prompt(idx, worker_id, "download failed")

        time.sleep(POST_DOWNLOAD_WAIT_SEC)
        return success

    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"  ✗ Unexpected error on prompt {idx}: {e}")
        try:
            page.screenshot(path=os.path.join(output_dir, f"error_prompt_{idx:03d}.png"))
        except Exception:
            pass
        return False


# ──────────────────────────────────────────────
# Main automation loop
# ──────────────────────────────────────────────

def run(start_from: int, use_existing_browser: bool, cdp_port: int,
        worker_id: int = 1, worker_prompts: list | None = None,
        interactive: bool = True, project_url: str = PROJECT_URL,
        work_queue=None, total_prompts: int | None = None,
        watermark_removal: bool = True, watermark_denoise: str = "ai",
        watermark_legacy: bool = False, gwt_binary: str | None = None,
        resize_enabled: bool = True, login_only: bool = False):
    """
    login_only – when True, this worker launches its Chrome session, waits
    for Google sign-in + the Flow input box to appear, then STOPS: no
    prompts are loaded/parsed, no settings popup is touched, and nothing is
    generated or downloaded. The browser is left open (headless=False) so
    the user can manually delete/clean up previously-generated images in
    that Flow project (avoiding the RAM/CPU buildup of 4 heavy Chrome tabs
    full of cached generation history) before a real run starts. Chrome is
    spawned as a DETACHED process (see ensure_chrome_running()) that keeps
    running after Ctrl+C exits this script — the next real `--workers N`
    run attaches to that SAME Chrome via connect_over_cdp(), reusing the
    exact session (sign-in, cleaned-up project, open tabs) instead of
    launching a brand-new browser.

    work_queue (optional) – a multiprocessing.Manager().Queue() shared across
    ALL worker processes for DYNAMIC work-stealing mode.  Each item is a
    prompt dict; a `None` sentinel tells this worker no more work is coming.

    IMPORTANT correctness guarantee: Manager().Queue().get() hands each item
    to EXACTLY ONE calling process — this is what makes it impossible for two
    workers to ever pick up the same prompt number, even though they run as
    fully independent OS processes with separate Chrome sessions. No extra
    locking is required for this guarantee.

    When work_queue is None, the OLD static per-worker prompt-list behaviour
    is used unchanged (worker_prompts, or the full file for single-worker
    mode).

    watermark_removal / watermark_denoise / watermark_legacy / gwt_binary /
    resize_enabled – forwarded from CLI args. These are applied to the
    module-level globals HERE (rather than relying on the values argparse
    set in the parent process) because multiprocessing uses "spawn" on
    macOS: each worker process re-imports this module from scratch, so any
    globals mutated only inside `if __name__ == "__main__":` in the parent
    do NOT carry over to spawned children. Setting them at the top of run()
    guarantees every worker (and the single-process mode) picks up the same
    configuration.
    """
    global REMOVE_WATERMARKS, WATERMARK_DENOISE, WATERMARK_LEGACY, WATERMARK_BINARY, RESIZE_ENABLED
    REMOVE_WATERMARKS = watermark_removal
    WATERMARK_DENOISE = watermark_denoise
    WATERMARK_LEGACY  = watermark_legacy
    RESIZE_ENABLED    = resize_enabled
    if gwt_binary:
        WATERMARK_BINARY = gwt_binary

    # ── Per-worker paths ──────────────────────────────────────────────────
    profile_dir          = get_worker_profile_dir(worker_id)
    worker_progress_file = get_worker_progress_file(worker_id)
    dynamic_mode          = work_queue is not None
    tag                  = f"[Worker {worker_id}] " if (worker_prompts is not None or dynamic_mode) else ""

    # ── Load prompts ──────────────────────────────────────────────────────
    if login_only:
        prompts = None
        print(f"{tag}--login-only mode: skipping prompt loading entirely. "
              f"This session will only launch Chrome + wait for sign-in.")
    elif dynamic_mode:
        prompts = None
        print(f"{tag}Dynamic work-stealing mode — pulling prompts from the "
              f"shared pool ({total_prompts} pending across all workers).")
    elif worker_prompts is not None:
        prompts = worker_prompts
        print(f"{tag}Handling {len(prompts)} prompts "
              f"(#{prompts[0]['index']}–#{prompts[-1]['index']}).")
    else:
        prompts = parse_prompts(PROMPTS_FILE)
        print(f"Loaded {len(prompts)} prompts from file.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # NOTE: normalize_existing_to_png(OUTPUT_DIR) is intentionally NOT called
    # here anymore. When several worker processes start at once, each one
    # calling it concurrently would race on renaming/rewriting the same
    # files. Callers now run it exactly ONCE, before any workers are spawned.

    # ── Scan folder for already-downloaded images ──
    # (Dynamic mode already filtered these out of the shared queue up front,
    # so skip the redundant per-worker scan + duplicate log message there.)
    already_done: set[int] = set()
    if not login_only and not dynamic_mode:
        already_done = get_already_downloaded(OUTPUT_DIR)
        if already_done:
            print(f"Found {len(already_done)} already-downloaded image(s) in '{OUTPUT_DIR}' – those will be skipped.")

    # ── Determine start index ──
    if start_from == 0:
        # Default: always scan from prompt 1.
        # The already_done disk-scan (above) skips what's already downloaded.
        # Do NOT use the progress file here – it can be stale when images were
        # deleted from disk or a previous run failed mid-download.
        start_from = 1
    else:
        print(f"Manual --start {start_from} given → fast-forwarding to prompt {start_from}")

    with sync_playwright() as pw:
        if use_existing_browser:
            print(f"Connecting to existing Chrome on port {cdp_port} ...")
        else:
            # Default (no --use-existing-browser): make sure a real, DETACHED
            # Chrome with remote debugging is already running on this worker's
            # port — spawning one only if needed — then always ATTACH via CDP
            # instead of launching a brand-new browser tied to this process.
            # This is exactly what lets `--login-only` and a later real
            # `--workers N` run reuse the SAME Chrome window/tabs/session per
            # worker, instead of each command opening (and closing) its own.
            print(f"{tag}Ensuring shared Chrome session on port {cdp_port} …")
            ensure_chrome_running(profile_dir, cdp_port)

        browser = pw.chromium.connect_over_cdp(f"http://localhost:{cdp_port}", slow_mo=300)
        context = browser.contexts[0]
        page = None
        for p in context.pages:
            if "labs.google" in p.url or "flow" in p.url:
                page = p
                break
        if page is None:
            page = context.new_page()
            try:
                page.goto(project_url, timeout=0)
            except PlaywrightTimeoutError:
                pass   # handled by the login-wait loop below

        # ── Wait for page load ──
        print("Waiting for Google Flow to load …")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

        print(f"\n⚠️  {tag}Sign in to Google now if prompted.")
        print(f"   Script continues automatically once the input box appears.")
        print(f"   (waiting up to {LOGIN_TIMEOUT_SEC}s) …\n")

        input_ready = False
        deadline = time.time() + LOGIN_TIMEOUT_SEC
        while time.time() < deadline:
            if find_input(page):
                input_ready = True
                break
            # If still on a non-Flow page (e.g. Google login), try navigating once ready
            try:
                if project_url not in page.url and "accounts.google" not in page.url:
                    page.goto(project_url, timeout=0)
            except Exception:
                pass
            time.sleep(2)

        if not input_ready:
            print("ERROR: Could not find the prompt input field.")
            dump = os.path.join(OUTPUT_DIR, "page_dump.html")
            with open(dump, "w") as fh:
                fh.write(page.content())
            print(f"Page HTML saved to: {dump}")
            # `browser` was obtained via connect_over_cdp() – calling
            # .close() on a CDP-attached Browser sends the real Chrome
            # DevTools "Browser.close" command, which fully quits that
            # Chrome process. That's fine here: the login/session itself
            # is safe because it lives in the on-disk profile directory
            # (profile_dir), not in the running process — the NEXT run's
            # ensure_chrome_running() will simply spawn a fresh Chrome
            # against that same profile and restore the signed-in state.
            robust_close_chrome(browser, cdp_port)
            return


        print(f"\n✅ Page ready. Starting from prompt {start_from} …\n")

        if login_only:
            # ── LOGIN-ONLY MODE ──────────────────────────────────────────
            # Stop here on purpose: no settings popup, no prompt submission,
            # no downloads. The browser stays open so the user can manually
            # delete previously-generated images from this Flow project
            # (reduces the RAM/CPU load of 4 heavy Chrome tabs full of
            # cached generation history before a real multi-worker run).
            #
            # NOTE: we deliberately do NOT use input() here. In multi-worker
            # mode this function runs in N separate OS processes that all
            # share the parent terminal's stdin — input() across several
            # concurrent processes is unreliable (only one gets each
            # keystroke, order is undefined). Instead every worker just
            # blocks indefinitely; press Ctrl+C in the terminal once you're
            # done cleaning up.
            #
            # Ctrl+C here CLOSES this worker's Chrome window (real quit, via
            # the CDP Browser.close command) so you don't have to close each
            # of the N windows by hand. This is safe for the "shared session"
            # goal because the login/cookies/sign-in state live in the
            # on-disk profile directory (profile_dir), not in the running
            # process — the next `--workers N` real run's
            # ensure_chrome_running() just spawns a fresh Chrome against that
            # same profile and you're still signed in, no re-login needed.
            print(f"{'='*60}")
            print(f"{tag}🧹 LOGIN-ONLY MODE — browser is ready for manual cleanup.")
            print(f"   Project: {project_url}")
            print(f"   Go delete/clear generated images in this Flow project now.")
            print(f"   Nothing will be generated or downloaded in this mode.")
            print(f"   Press Ctrl+C in this terminal when done — Chrome will close")
            print(f"   automatically (your sign-in is saved and reused next run).")
            print(f"{'='*60}\n")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print(f"\n{tag}Ctrl+C received — closing Chrome …")
                robust_close_chrome(browser, cdp_port)
            return

        # ── Apply model/quantity settings ONCE before the first prompt ──────
        setup_generation_settings(page)

        if dynamic_mode:
            # ── DYNAMIC WORK-STEALING MODE ───────────────────────────────
            # Pull the next available prompt from the shared queue. Because
            # Manager().Queue().get() delivers each item to exactly one
            # process, two workers can NEVER receive the same prompt — no
            # extra locking needed. When a worker finishes its current
            # prompt faster than the others, it immediately loops back here
            # and grabs the NEXT unclaimed prompt instead of exiting, which
            # is exactly the "reshuffle remaining work" behaviour requested.
            # The browser/Chrome session for this worker stays open the
            # entire time — it only closes once the queue is fully drained
            # (signalled by the `None` sentinel).
            while True:
                prompt_info = work_queue.get()   # blocks until work or sentinel
                if prompt_info is None:
                    print(f"{tag}Shared queue drained — no prompts left for "
                          f"this worker. Closing its browser now.")
                    break

                idx = prompt_info["index"]
                # Defensive check only (queue already guarantees uniqueness):
                # skip if the file somehow already exists on disk.
                if os.path.exists(os.path.join(OUTPUT_DIR, prompt_filename(idx))):
                    print(f"  ↷ Prompt {idx:03d} already downloaded – skipping.")
                    continue

                try:
                    process_one_prompt(page, prompt_info, OUTPUT_DIR, worker_id,
                                        worker_progress_file, total_prompts)
                except KeyboardInterrupt:
                    print("\n\nInterrupted by user. Progress saved.")
                    break
        else:
            # ── STATIC MODE (original behaviour, unchanged) ─────────────
            for prompt_info in prompts:
                idx = prompt_info["index"]

                # ── Skip if below start_from ──
                if idx < start_from:
                    continue

                # ── Skip if image already exists in the folder ──
                if idx in already_done:
                    print(f"  ↷ Prompt {idx:03d} already downloaded – skipping.")
                    continue

                try:
                    success = process_one_prompt(page, prompt_info, OUTPUT_DIR,
                                                  worker_id, worker_progress_file,
                                                  len(prompts))
                    if success:
                        already_done.add(idx)
                except KeyboardInterrupt:
                    print("\n\nInterrupted by user. Progress saved.")
                    break

        print(f"\n{'='*60}")
        print(f"{tag}Done! Images saved to: {OUTPUT_DIR}")
        print(f"{'='*60}")

        if interactive:
            input("Press Enter to close the browser …")
        # `browser` was obtained via connect_over_cdp() – closing it should
        # quit that real Chrome process (this IS what we want: pressing
        # Ctrl+C / finishing a run should close the window(s), not leave
        # them hanging around requiring a manual close). This does NOT lose
        # your sign-in state — cookies/session live in the on-disk profile
        # directory (profile_dir), which persists independently of the
        # Chrome process. The next --login-only or --workers run just
        # spawns a fresh Chrome against that same profile and you're still
        # signed in automatically. robust_close_chrome() also force-kills
        # the OS process by PID if the graceful CDP close doesn't take
        # effect (see its docstring for why that fallback is required).
        robust_close_chrome(browser, cdp_port)


# ──────────────────────────────────────────────
# Parallel worker orchestration
# ──────────────────────────────────────────────

def _shutdown_all_workers(processes: list, ports: list[int]) -> None:
    """
    Guaranteed-shutdown path used from the PARENT process whenever it
    receives Ctrl+C (KeyboardInterrupt) while blocked on p.join().

    This does NOT depend on the child worker processes ever receiving
    their own SIGINT/KeyboardInterrupt. That dependency is exactly what
    caused "Ctrl+C doesn't close the Chromes" in practice: depending on how
    the terminal/IDE delivers Ctrl+C, it can hit only the parent process,
    leaving each spawned child (and the real Chrome process it's attached
    to) running forever, orphaned. Here the PARENT — which is guaranteed to
    see the interrupt since p.join() is what's actually blocking it —
    force-kills every worker's real Chrome process directly by port
    (independent of whether that worker's own process is still alive or
    responsive), then terminates the now-useless child processes itself.
    """
    print(f"\nClosing {len(ports)} Chrome session(s) …")
    for port in ports:
        force_quit_chrome_on_port(port)
    for p in processes:
        if p.is_alive():
            p.terminate()
    for p in processes:
        p.join(timeout=10)

def run_worker_process(worker_id: int, num_workers: int, chunk: list,
                       start_from: int, use_existing_browser: bool,
                       cdp_port: int, project_url: str,
                       watermark_removal: bool = True, watermark_denoise: str = "ai",
                       watermark_legacy: bool = False, gwt_binary: str | None = None,
                       resize_enabled: bool = True):
    """
    Entry-point for each child process.
    Each worker uses its own Chrome profile (set via worker_id) so a different
    Google account can be signed in for each browser window.
    When --use-existing-browser is set, worker N connects to port cdp_port+(N-1).
    """
    effective_port = cdp_port + (worker_id - 1)
    print(f"\n{'='*60}")
    print(f"Worker {worker_id}/{num_workers}  |  "
          f"prompts #{chunk[0]['index']}–#{chunk[-1]['index']}  |  "
          f"{'CDP port ' + str(effective_port) if use_existing_browser else 'fresh Chrome profile'}")
    print(f"Project URL: {project_url}")
    print(f"{'='*60}\n")
    run(
        start_from=start_from,
        use_existing_browser=use_existing_browser,
        cdp_port=effective_port,
        worker_id=worker_id,
        worker_prompts=chunk,
        interactive=False,
        project_url=project_url,
        watermark_removal=watermark_removal,
        watermark_denoise=watermark_denoise,
        watermark_legacy=watermark_legacy,
        gwt_binary=gwt_binary,
        resize_enabled=resize_enabled,
    )


def run_parallel_static(num_workers: int, start_from: int,
                        use_existing_browser: bool, cdp_port: int,
                        watermark_removal: bool = True, watermark_denoise: str = "ai",
                        watermark_legacy: bool = False, gwt_binary: str | None = None,
                        resize_enabled: bool = True):
    """
    OLD behaviour, kept for --static-split: each worker gets a FIXED,
    pre-computed range of prompts (e.g. Worker 1 = 1-94). If a worker
    finishes its range early it just closes — it does NOT pick up any of
    the other workers' remaining prompts.
    """
    prompts = parse_prompts(PROMPTS_FILE)
    print(f"Loaded {len(prompts)} prompts from file.")

    chunks = split_prompts_for_workers(prompts, num_workers)
    print(f"\nDividing work across {num_workers} workers (STATIC split):")
    describe_worker_ranges(prompts, num_workers)
    print()

    processes: list[multiprocessing.Process] = []
    ports: list[int] = []
    for w_idx, chunk in enumerate(chunks):
        worker_id = w_idx + 1
        if not chunk:
            print(f"  ⚠ Worker {worker_id} has no prompts – skipping.")
            continue
        p = multiprocessing.Process(
            target=run_worker_process,
            args=(worker_id, num_workers, chunk,
                  start_from, use_existing_browser, cdp_port,
                  PROJECT_URLS[w_idx] if w_idx < len(PROJECT_URLS) else PROJECT_URL,
                  watermark_removal, watermark_denoise, watermark_legacy, gwt_binary,
                  resize_enabled),
            name=f"flow-worker-{worker_id}",
            daemon=False,
        )
        processes.append(p)
        ports.append(cdp_port + (worker_id - 1))

    print(f"Launching {len(processes)} worker(s) …\n")
    for p in processes:
        p.start()
        time.sleep(4)   # stagger launches so Chrome profiles don't collide

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        _shutdown_all_workers(processes, ports)

    print("\n✅ All workers finished (static split).")


def run_worker_process_dynamic(worker_id: int, num_workers: int, work_queue,
                               total_prompts: int, start_from: int,
                               use_existing_browser: bool, cdp_port: int,
                               project_url: str,
                               watermark_removal: bool = True, watermark_denoise: str = "ai",
                               watermark_legacy: bool = False, gwt_binary: str | None = None,
                               resize_enabled: bool = True):
    """
    Entry-point for each child process in DYNAMIC work-stealing mode.
    Every worker shares the SAME queue — whichever worker finishes its
    current prompt first simply calls work_queue.get() again and receives
    whatever prompt is next in line, regardless of which "batch" it would
    have belonged to under the old static split. A worker only stops (and
    closes its browser) once it receives the `None` sentinel, meaning the
    entire shared queue has been drained by all workers combined.
    """
    effective_port = cdp_port + (worker_id - 1)
    print(f"\n{'='*60}")
    print(f"Worker {worker_id}/{num_workers}  |  dynamic work-stealing mode  |  "
          f"{'CDP port ' + str(effective_port) if use_existing_browser else 'fresh Chrome profile'}")
    print(f"Project URL: {project_url}")
    print(f"{'='*60}\n")
    run(
        start_from=start_from,
        use_existing_browser=use_existing_browser,
        cdp_port=effective_port,
        worker_id=worker_id,
        worker_prompts=None,
        interactive=False,
        project_url=project_url,
        work_queue=work_queue,
        total_prompts=total_prompts,
        watermark_removal=watermark_removal,
        watermark_denoise=watermark_denoise,
        watermark_legacy=watermark_legacy,
        gwt_binary=gwt_binary,
        resize_enabled=resize_enabled,
    )


def run_parallel_dynamic(num_workers: int, start_from: int,
                         use_existing_browser: bool, cdp_port: int,
                         watermark_removal: bool = True, watermark_denoise: str = "ai",
                         watermark_legacy: bool = False, gwt_binary: str | None = None,
                         resize_enabled: bool = True):
    """
    NEW default behaviour: all pending prompts are placed into ONE shared
    queue (multiprocessing.Manager().Queue()). Every worker process pulls
    the next prompt from that same queue as soon as it's free, instead of
    owning a fixed pre-computed range.

    Why this is conflict-safe:
      Manager().Queue() is backed by a dedicated manager server process.
      Every .get() call is a single atomic operation against that server —
      it hands out each queued item to exactly ONE caller and then removes
      it. Two workers calling .get() concurrently can never receive the
      same item. This is what guarantees no two Chrome sessions will ever
      submit/download the same prompt number, even though the queue is
      shared across 4 completely independent OS processes.

    Effect: if Worker 3 finishes all currently-in-flight work sooner than
    Workers 1/2/4 (faster account, faster generation, etc.), it does NOT
    shut down — it immediately grabs the next unclaimed prompt from the
    pool. Only once the queue is fully empty (every worker has received its
    `None` sentinel) do all browsers close.
    """
    prompts = parse_prompts(PROMPTS_FILE)
    print(f"Loaded {len(prompts)} prompts from file.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Run the PNG-repair pass exactly ONCE here, before any worker process
    # starts, so 4 processes never race renaming the same files concurrently.
    normalize_existing_to_png(OUTPUT_DIR)

    already_done = get_already_downloaded(OUTPUT_DIR)
    if already_done:
        print(f"Found {len(already_done)} already-downloaded image(s) in "
              f"'{OUTPUT_DIR}' – those will be excluded from the shared queue.")

    effective_start = start_from if start_from > 0 else 1
    pending = [p for p in prompts
               if p["index"] >= effective_start and p["index"] not in already_done]

    print(f"\n{len(pending)} prompt(s) pending → shared across {num_workers} "
          f"dynamic worker(s) (no fixed ranges — workers reshuffle automatically "
          f"as they finish).\n")

    if not pending:
        print("Nothing to do — every prompt is already downloaded.")
        return

    manager = multiprocessing.Manager()
    work_queue = manager.Queue()
    for p in pending:
        work_queue.put(p)
    # One sentinel per worker so each one knows exactly when to stop.
    for _ in range(num_workers):
        work_queue.put(None)

    processes: list[multiprocessing.Process] = []
    for w in range(num_workers):
        worker_id = w + 1
        project_url = PROJECT_URLS[w] if w < len(PROJECT_URLS) else PROJECT_URL
        p = multiprocessing.Process(
            target=run_worker_process_dynamic,
            args=(worker_id, num_workers, work_queue, len(pending),
                  start_from, use_existing_browser, cdp_port, project_url,
                  watermark_removal, watermark_denoise, watermark_legacy, gwt_binary,
                  resize_enabled),
            name=f"flow-worker-{worker_id}",
            daemon=False,
        )
        processes.append(p)

    print(f"Launching {len(processes)} worker(s) …\n")
    for p in processes:
        p.start()
        time.sleep(4)   # stagger launches so Chrome profiles don't collide

    ports = [cdp_port + i for i in range(num_workers)]
    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        _shutdown_all_workers(processes, ports)

    print("\n✅ All workers finished (dynamic work-stealing — queue fully drained).")


def run_parallel(num_workers: int, start_from: int,
                 use_existing_browser: bool, cdp_port: int,
                 static_split: bool = False,
                 watermark_removal: bool = True, watermark_denoise: str = "ai",
                 watermark_legacy: bool = False, gwt_binary: str | None = None,
                 resize_enabled: bool = True):
    """
    Dispatcher: dynamic work-stealing is now the DEFAULT (static_split=False).
    Pass static_split=True (via --static-split) to restore the old
    fixed-range-per-worker behaviour.
    """
    kwargs = dict(watermark_removal=watermark_removal, watermark_denoise=watermark_denoise,
                  watermark_legacy=watermark_legacy, gwt_binary=gwt_binary,
                  resize_enabled=resize_enabled)
    if static_split:
        run_parallel_static(num_workers, start_from, use_existing_browser, cdp_port, **kwargs)
    else:
        run_parallel_dynamic(num_workers, start_from, use_existing_browser, cdp_port, **kwargs)


# ──────────────────────────────────────────────
# Login-only mode – launch N Chrome sessions, sign in, then STOP.
#
# No prompt file is parsed and no work queue/chunking is built (login-only
# mode doesn't need any of that), which is why this is a small dedicated
# path rather than reusing run_worker_process*/run_parallel* — those all
# assume real prompt data exists to hand each worker.
# ──────────────────────────────────────────────

def run_login_only_worker(worker_id: int, num_workers: int,
                          use_existing_browser: bool, cdp_port: int,
                          project_url: str):
    """Entry-point for each child process in --login-only mode."""
    effective_port = cdp_port + (worker_id - 1)
    print(f"\n{'='*60}")
    print(f"Worker {worker_id}/{num_workers}  |  LOGIN-ONLY  |  "
          f"{'CDP port ' + str(effective_port) if use_existing_browser else 'fresh Chrome profile'}")
    print(f"Project URL: {project_url}")
    print(f"{'='*60}\n")
    run(
        start_from=0,
        use_existing_browser=use_existing_browser,
        cdp_port=effective_port,
        worker_id=worker_id,
        worker_prompts=None,
        interactive=False,
        project_url=project_url,
        login_only=True,
    )


def run_login_only_parallel(num_workers: int, use_existing_browser: bool, cdp_port: int):
    """
    Launch num_workers Chrome sessions (one per Flow project URL) that each
    sign in and stop — no prompts loaded, no generation, no downloads.
    Every worker blocks until Ctrl+C is pressed in this terminal.

    Shutdown is driven from THIS parent process, not from each child
    worker's own SIGINT handler — p.join() below is what's actually
    blocking the parent, so the parent is guaranteed to see the
    KeyboardInterrupt regardless of whether the terminal/IDE also happens
    to deliver SIGINT to the child processes individually. The parent then
    force-kills each worker's real Chrome process directly by port. Your
    sign-in state is not lost — it's saved in each worker's on-disk profile
    directory (~/.flow_chrome_profile_N), so the next `--workers N` real
    run spawns a fresh Chrome per worker against that same profile and is
    still signed in automatically, with no manual re-login needed.
    """
    processes: list[multiprocessing.Process] = []
    for w in range(num_workers):
        worker_id = w + 1
        project_url = PROJECT_URLS[w] if w < len(PROJECT_URLS) else PROJECT_URL
        p = multiprocessing.Process(
            target=run_login_only_worker,
            args=(worker_id, num_workers, use_existing_browser, cdp_port, project_url),
            name=f"flow-worker-{worker_id}-loginonly",
            daemon=False,
        )
        processes.append(p)

    print(f"Launching {len(processes)} browser session(s) in LOGIN-ONLY mode "
          f"(no generation/downloads) …\n")
    for p in processes:
        p.start()
        time.sleep(4)   # stagger launches so Chrome profiles don't collide

    ports = [cdp_port + i for i in range(num_workers)]
    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        _shutdown_all_workers(processes, ports)

    print("\n✅ All login-only Chrome windows closed. Sign-in state is saved "
          "on disk — the next `--workers N` run will reuse it automatically.")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)   # safe on macOS/Windows

    parser = argparse.ArgumentParser(description="Automate Google Flow image generation")
    parser.add_argument(
        "--start", type=int, default=0,
        help="Prompt number to start from (default: 1, skipping already-downloaded)"
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Number of parallel browser workers (default: 1)"
    )
    parser.add_argument(
        "--worker-id", type=int, default=0,
        help=(
            "Run only this specific worker (1-based). "
            "Must be used with --workers N so the prompt range is computed correctly."
        )
    )
    parser.add_argument(
        "--use-existing-browser", action="store_true",
        help=(
            "Connect to already-running Chrome browser(s). "
            "Worker W connects to port (cdp-port + W - 1). "
            "Start each Chrome with: "
            "open -a 'Google Chrome' --args --remote-debugging-port=<port>"
        )
    )
    parser.add_argument(
        "--login-timeout", type=int, default=0,
        help=(
            "Seconds to wait for Google sign-in per worker "
            f"(default: {LOGIN_TIMEOUT_SEC}s). Override with e.g. --login-timeout 600"
        )
    )
    parser.add_argument(
        "--cdp-port", type=int, default=9222,
        help="Base Chrome DevTools Protocol port (default: 9222). "
             "Worker 1=9222, Worker 2=9223, …"
    )
    parser.add_argument(
        "--static-split", action="store_true",
        help=(
            "Use the OLD fixed-range-per-worker behaviour (Worker N always "
            "owns prompts [start,end]; finishes early → just closes). "
            "Default is DYNAMIC work-stealing: all workers share one pool "
            "and a worker that finishes early immediately grabs the next "
            "unclaimed prompt instead of closing."
        )
    )
    parser.add_argument(
        "--no-watermark-removal", action="store_true",
        help=(
            "Disable automatic Gemini sparkle-watermark removal "
            "(https://github.com/allenk/GeminiWatermarkTool). Enabled by default."
        )
    )
    parser.add_argument(
        "--watermark-denoise", default="ai", choices=["ai", "ns", "telea", "soft", "off"],
        help="Cleanup pass after watermark removal (default: ai)."
    )
    parser.add_argument(
        "--watermark-legacy", action="store_true",
        help="Pin the pre-Gemini-3.5 watermark profile for removal (--legacy)."
    )
    parser.add_argument(
        "--gwt-binary", default=None,
        help="Path to the GeminiWatermarkTool/gwt-mini CLI binary "
             "(default: tools/gwt-mini next to this script, or $GWT_BINARY)."
    )
    parser.add_argument(
        "--no-resize", action="store_true",
        help=(
            "Disable automatic center-crop + resize to 1920x1080 that "
            "normally runs right after watermark removal. Enabled by default."
        )
    )
    parser.add_argument(
        "--login-only", action="store_true",
        help=(
            "Launch Chrome session(s) (respects --workers N for multiple), "
            "wait for Google sign-in + the Flow input box to appear, then "
            "STOP — no prompts are loaded, nothing is generated/downloaded. "
            "Use this to manually clear out previously-generated images from "
            "a Flow project (reduces RAM/CPU load) before a real run. "
            "Every browser stays open until you press Ctrl+C in this terminal."
        )
    )
    args = parser.parse_args()

    # Allow CLI override of the login timeout global
    if args.login_timeout > 0:
        LOGIN_TIMEOUT_SEC = args.login_timeout

    if args.login_only:
        # ── LOGIN-ONLY MODE ──────────────────────────────────────────────
        # Deliberately bypasses prompt parsing/chunking/watermark/resize
        # config entirely — none of that is relevant since nothing gets
        # generated or downloaded in this mode.
        # Worker count follows --workers exactly like every other mode:
        # plain `--login-only` (workers defaults to 1) → ONE Chrome only.
        # `--workers 4 --login-only` → all 4 Chrome sessions.
        if args.workers > 1:
            run_login_only_parallel(
                num_workers=args.workers,
                use_existing_browser=args.use_existing_browser,
                cdp_port=args.cdp_port,
            )
        else:
            run(
                start_from=0,
                use_existing_browser=args.use_existing_browser,
                cdp_port=args.cdp_port,
                project_url=PROJECT_URLS[0] if PROJECT_URLS else PROJECT_URL,
                interactive=False,
                login_only=True,
            )
        sys.exit(0)

    watermark_kwargs = dict(
        watermark_removal=not args.no_watermark_removal,
        watermark_denoise=args.watermark_denoise,
        watermark_legacy=args.watermark_legacy,
        gwt_binary=args.gwt_binary,
        resize_enabled=not args.no_resize,
    )

    if args.workers > 1 and args.worker_id == 0:
        # ── Launch all workers in parallel (dynamic work-stealing by default) ──
        run_parallel(
            num_workers=args.workers,
            start_from=args.start,
            use_existing_browser=args.use_existing_browser,
            cdp_port=args.cdp_port,
            static_split=args.static_split,
            **watermark_kwargs,
        )

    elif args.workers > 1 and args.worker_id > 0:
        # ── Run a single specific worker (manual / debug mode) ────────────
        # This mode always uses the STATIC range for that one worker id —
        # a shared dynamic queue only makes sense when all workers are
        # launched together from the same parent process.
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        normalize_existing_to_png(OUTPUT_DIR)   # one-time repair pass
        prompts = parse_prompts(PROMPTS_FILE)
        chunks  = split_prompts_for_workers(prompts, args.workers)
        if args.worker_id > len(chunks):
            print(f"ERROR: --worker-id {args.worker_id} > --workers {args.workers}")
            sys.exit(1)
        chunk = chunks[args.worker_id - 1]
        if not chunk:
            print(f"Worker {args.worker_id} has no prompts assigned.")
            sys.exit(0)
        print(f"Running Worker {args.worker_id}/{args.workers}  "
              f"(prompts #{chunk[0]['index']}–#{chunk[-1]['index']})")
        run(
            start_from=args.start,
            use_existing_browser=args.use_existing_browser,
            cdp_port=args.cdp_port + (args.worker_id - 1),
            worker_id=args.worker_id,
            worker_prompts=chunk,
            interactive=True,
            project_url=PROJECT_URLS[args.worker_id - 1] if args.worker_id - 1 < len(PROJECT_URLS) else PROJECT_URL,
            **watermark_kwargs,
        )

    else:
        # ── Original single-worker mode (backward-compatible) ─────────────
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        normalize_existing_to_png(OUTPUT_DIR)   # one-time repair pass
        run(
            start_from=args.start,
            use_existing_browser=args.use_existing_browser,
            cdp_port=args.cdp_port,
            project_url=PROJECT_URLS[0] if PROJECT_URLS else PROJECT_URL,
            **watermark_kwargs,
        )


