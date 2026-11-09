#!/usr/bin/env python3
"""
automate_google_flow_human.py
==============================
Humanized entry point for the Google Flow image-generation automation.

THIS FILE DOES NOT MODIFY automate_google_flow.py IN ANY WAY. It imports
that module and reuses 100% of its logic — prompt parsing, the settings
popup automation, all three download strategies, watermark removal,
1920x1080 resizing, progress tracking, the shared-Chrome-session helpers,
and both the static and dynamic multi-worker orchestration.

The ONLY thing added here is human-like interaction behaviour, supplied by
`human_behavior.py` (curved mouse movement, variable click timing, paste
pauses, jittered waits, idle mouse drift). Those patches are installed at
the PLAYWRIGHT CLASS LEVEL, so every mouse/keyboard/wait call already
performed inside the unmodified `automate_google_flow.py` functions
automatically becomes human-like — nothing in that file had to change.

Why a separate file at all (instead of editing automate_google_flow.py)
-------------------------------------------------------------------------
Per your instructions: the existing, working automation must be left
completely untouched. This file is a thin "humanizing shell" around it:
same CLI flags, same behaviour, same output — just less robotic timing.

Usage
-----
Identical to automate_google_flow.py, e.g.:

    python3 automate_google_flow_human.py
    python3 automate_google_flow_human.py --workers 4
    python3 automate_google_flow_human.py --start 25 --use-existing-browser
    python3 automate_google_flow_human.py --login-only --workers 4

One extra flag is added:

    --no-human-behavior     Disable all humanization patches (falls back to
                             automate_google_flow.py's original raw timing,
                             useful for A/B comparison or debugging).

Why plain paste (Locator.fill) is still used for prompt text
-------------------------------------------------------------------------
As discussed: a human who copies a prompt and pastes it, repeating that
for every entry, is completely natural — arguably MORE realistic for long
templated prompts than typing them letter by letter would be. We keep the
paste (automate_google_flow.py's `inp.fill(...)` call is untouched), and
`human_behavior.py` only wraps that paste action with the small human
pauses a person naturally takes right before/after hitting Cmd+V.
"""

import argparse
import multiprocessing
import os
import random
import sys
import time

import automate_google_flow as flow
import human_behavior as hb


# ──────────────────────────────────────────────
# Apply humanization patches as early as possible.
#
# This runs at IMPORT time, which matters for multiprocessing's "spawn"
# start method (used on macOS): every worker child process re-imports this
# module from scratch before calling its target function, so this line
# guarantees patches are installed in EVERY worker, not just the main
# process. (flow's own worker target functions live in automate_google_flow
# and are intentionally NOT used directly as multiprocessing targets below
# — see the wrapper *_human functions further down — precisely so that
# importing THIS module, and therefore running this apply_patches() call,
# is guaranteed to happen in every child.)
# ──────────────────────────────────────────────
hb.apply_patches()


# ──────────────────────────────────────────────
# Idle-wander augmentation for the generation-wait poll loop
# ──────────────────────────────────────────────
# automate_google_flow.wait_for_new_image() already gets jittered
# time.sleep()/wait_for_timeout() timing for free via the patches above.
# On top of that, we occasionally nudge the mouse a little while polling —
# real humans don't leave the cursor perfectly frozen for up to 120s.
# This wraps (not reimplements) the original polling function: the actual
# "did a new image appear" detection logic is 100% untouched.
_orig_wait_for_new_image = flow.wait_for_new_image


def _humanized_wait_for_new_image(page, before_srcs, timeout_sec):
    if not hb.is_enabled() or random.random() >= 0.35:
        return _orig_wait_for_new_image(page, before_srcs, timeout_sec)

    # ~35% of the time, split the wait into two shorter polls with a
    # small idle mouse drift in between — still calls the EXACT same
    # original detection function for both halves, just interleaved with
    # one harmless mouse wiggle so the page isn't perfectly motionless.
    first_half = max(5, timeout_sec // 2)
    if _orig_wait_for_new_image(page, before_srcs, first_half):
        return True
    hb.idle_mouse_wander(page)
    remaining = timeout_sec - first_half
    return _orig_wait_for_new_image(page, before_srcs, remaining) if remaining > 0 else False


flow.wait_for_new_image = _humanized_wait_for_new_image


# ──────────────────────────────────────────────
# Thin worker entry points
#
# These mirror automate_google_flow.run_worker_process /
# run_worker_process_dynamic / run_login_only_worker EXACTLY, with one
# difference: they live in THIS module, so multiprocessing (spawn) will
# import automate_google_flow_human (running apply_patches()) in every
# child process before invoking them. Everything else is delegated
# straight to flow.run(...) — the unmodified original logic.
# ──────────────────────────────────────────────

def run_worker_process_human(worker_id, num_workers, chunk, start_from,
                             use_existing_browser, cdp_port, project_url,
                             watermark_removal=True, watermark_denoise="ai",
                             watermark_legacy=False, gwt_binary=None,
                             resize_enabled=True):
    effective_port = cdp_port + (worker_id - 1)
    print(f"\n{'='*60}")
    print(f"[human] Worker {worker_id}/{num_workers}  |  "
          f"prompts #{chunk[0]['index']}-#{chunk[-1]['index']}  |  "
          f"{'CDP port ' + str(effective_port) if use_existing_browser else 'fresh Chrome profile'}")
    print(f"Project URL: {project_url}")
    print(f"{'='*60}\n")
    flow.run(
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


def run_worker_process_dynamic_human(worker_id, num_workers, work_queue,
                                     total_prompts, start_from,
                                     use_existing_browser, cdp_port, project_url,
                                     watermark_removal=True, watermark_denoise="ai",
                                     watermark_legacy=False, gwt_binary=None,
                                     resize_enabled=True):
    effective_port = cdp_port + (worker_id - 1)
    print(f"\n{'='*60}")
    print(f"[human] Worker {worker_id}/{num_workers}  |  dynamic work-stealing mode  |  "
          f"{'CDP port ' + str(effective_port) if use_existing_browser else 'fresh Chrome profile'}")
    print(f"Project URL: {project_url}")
    print(f"{'='*60}\n")
    flow.run(
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


def run_login_only_worker_human(worker_id, num_workers, use_existing_browser,
                                cdp_port, project_url):
    effective_port = cdp_port + (worker_id - 1)
    print(f"\n{'='*60}")
    print(f"[human] Worker {worker_id}/{num_workers}  |  LOGIN-ONLY  |  "
          f"{'CDP port ' + str(effective_port) if use_existing_browser else 'fresh Chrome profile'}")
    print(f"Project URL: {project_url}")
    print(f"{'='*60}\n")
    flow.run(
        start_from=0,
        use_existing_browser=use_existing_browser,
        cdp_port=effective_port,
        worker_id=worker_id,
        worker_prompts=None,
        interactive=False,
        project_url=project_url,
        login_only=True,
    )


# ──────────────────────────────────────────────
# Thin orchestration wrappers
#
# These mirror flow.run_parallel_static / run_parallel_dynamic /
# run_login_only_parallel, reusing every helper (parse_prompts,
# split_prompts_for_workers, describe_worker_ranges, get_already_downloaded,
# normalize_existing_to_png, force_quit_chrome_on_port, PROJECT_URLS,
# PROJECT_URL, OUTPUT_DIR, PROMPTS_FILE, _shutdown_all_workers) verbatim
# from the untouched flow module. The only change: spawned processes target
# the *_human functions above instead of flow's own worker entry points, so
# humanization patches are guaranteed active in every child.
# ──────────────────────────────────────────────

def run_parallel_static_human(num_workers, start_from, use_existing_browser,
                              cdp_port, watermark_removal=True,
                              watermark_denoise="ai", watermark_legacy=False,
                              gwt_binary=None, resize_enabled=True):
    prompts = flow.parse_prompts(flow.PROMPTS_FILE)
    print(f"Loaded {len(prompts)} prompts from file.")

    chunks = flow.split_prompts_for_workers(prompts, num_workers)
    print(f"\nDividing work across {num_workers} workers (STATIC split, humanized):")
    flow.describe_worker_ranges(prompts, num_workers)
    print()

    processes = []
    ports = []
    for w_idx, chunk in enumerate(chunks):
        worker_id = w_idx + 1
        if not chunk:
            print(f"  ⚠ Worker {worker_id} has no prompts – skipping.")
            continue
        p = multiprocessing.Process(
            target=run_worker_process_human,
            args=(worker_id, num_workers, chunk, start_from, use_existing_browser,
                  cdp_port,
                  flow.PROJECT_URLS[w_idx] if w_idx < len(flow.PROJECT_URLS) else flow.PROJECT_URL,
                  watermark_removal, watermark_denoise, watermark_legacy, gwt_binary,
                  resize_enabled),
            name=f"flow-worker-{worker_id}-human",
            daemon=False,
        )
        processes.append(p)
        ports.append(cdp_port + (worker_id - 1))

    print(f"Launching {len(processes)} worker(s) …\n")
    for p in processes:
        p.start()
        time.sleep(4)

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        flow._shutdown_all_workers(processes, ports)

    print("\n✅ All workers finished (static split, humanized).")


def run_parallel_dynamic_human(num_workers, start_from, use_existing_browser,
                               cdp_port, watermark_removal=True,
                               watermark_denoise="ai", watermark_legacy=False,
                               gwt_binary=None, resize_enabled=True):
    prompts = flow.parse_prompts(flow.PROMPTS_FILE)
    print(f"Loaded {len(prompts)} prompts from file.")

    os.makedirs(flow.OUTPUT_DIR, exist_ok=True)
    flow.normalize_existing_to_png(flow.OUTPUT_DIR)

    already_done = flow.get_already_downloaded(flow.OUTPUT_DIR)
    if already_done:
        print(f"Found {len(already_done)} already-downloaded image(s) in "
              f"'{flow.OUTPUT_DIR}' – those will be excluded from the shared queue.")

    effective_start = start_from if start_from > 0 else 1
    pending = [p for p in prompts
               if p["index"] >= effective_start and p["index"] not in already_done]

    print(f"\n{len(pending)} prompt(s) pending → shared across {num_workers} "
          f"dynamic worker(s), humanized (no fixed ranges — workers reshuffle "
          f"automatically as they finish).\n")

    if not pending:
        print("Nothing to do — every prompt is already downloaded.")
        return

    manager = multiprocessing.Manager()
    work_queue = manager.Queue()
    for p in pending:
        work_queue.put(p)
    for _ in range(num_workers):
        work_queue.put(None)

    processes = []
    for w in range(num_workers):
        worker_id = w + 1
        project_url = flow.PROJECT_URLS[w] if w < len(flow.PROJECT_URLS) else flow.PROJECT_URL
        p = multiprocessing.Process(
            target=run_worker_process_dynamic_human,
            args=(worker_id, num_workers, work_queue, len(pending), start_from,
                  use_existing_browser, cdp_port, project_url,
                  watermark_removal, watermark_denoise, watermark_legacy, gwt_binary,
                  resize_enabled),
            name=f"flow-worker-{worker_id}-human",
            daemon=False,
        )
        processes.append(p)

    print(f"Launching {len(processes)} worker(s) …\n")
    for p in processes:
        p.start()
        time.sleep(4)

    ports = [cdp_port + i for i in range(num_workers)]
    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        flow._shutdown_all_workers(processes, ports)

    print("\n✅ All workers finished (dynamic work-stealing, humanized — queue fully drained).")


def run_parallel_human(num_workers, start_from, use_existing_browser, cdp_port,
                       static_split=False, watermark_removal=True,
                       watermark_denoise="ai", watermark_legacy=False,
                       gwt_binary=None, resize_enabled=True):
    kwargs = dict(watermark_removal=watermark_removal, watermark_denoise=watermark_denoise,
                  watermark_legacy=watermark_legacy, gwt_binary=gwt_binary,
                  resize_enabled=resize_enabled)
    if static_split:
        run_parallel_static_human(num_workers, start_from, use_existing_browser, cdp_port, **kwargs)
    else:
        run_parallel_dynamic_human(num_workers, start_from, use_existing_browser, cdp_port, **kwargs)


def run_login_only_parallel_human(num_workers, use_existing_browser, cdp_port):
    processes = []
    for w in range(num_workers):
        worker_id = w + 1
        project_url = flow.PROJECT_URLS[w] if w < len(flow.PROJECT_URLS) else flow.PROJECT_URL
        p = multiprocessing.Process(
            target=run_login_only_worker_human,
            args=(worker_id, num_workers, use_existing_browser, cdp_port, project_url),
            name=f"flow-worker-{worker_id}-loginonly-human",
            daemon=False,
        )
        processes.append(p)

    print(f"Launching {len(processes)} browser session(s) in LOGIN-ONLY mode, humanized "
          f"(no generation/downloads) …\n")
    for p in processes:
        p.start()
        time.sleep(4)

    ports = [cdp_port + i for i in range(num_workers)]
    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        flow._shutdown_all_workers(processes, ports)

    print("\n✅ All login-only Chrome windows closed. Sign-in state is saved "
          "on disk — the next `--workers N` run will reuse it automatically.")


# ──────────────────────────────────────────────
# CLI — identical surface to automate_google_flow.py, plus one extra flag.
# ──────────────────────────────────────────────

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)

    parser = argparse.ArgumentParser(
        description="Automate Google Flow image generation (humanized wrapper)"
    )
    parser.add_argument("--start", type=int, default=0,
                         help="Prompt number to start from (default: 1, skipping already-downloaded)")
    parser.add_argument("--workers", type=int, default=1,
                         help="Number of parallel browser workers (default: 1)")
    parser.add_argument("--worker-id", type=int, default=0,
                         help="Run only this specific worker (1-based). Use with --workers N.")
    parser.add_argument("--use-existing-browser", action="store_true",
                         help="Connect to already-running Chrome browser(s).")
    parser.add_argument("--login-timeout", type=int, default=0,
                         help=f"Seconds to wait for Google sign-in per worker "
                              f"(default: {flow.LOGIN_TIMEOUT_SEC}s).")
    parser.add_argument("--cdp-port", type=int, default=9222,
                         help="Base Chrome DevTools Protocol port (default: 9222).")
    parser.add_argument("--static-split", action="store_true",
                         help="Use the OLD fixed-range-per-worker behaviour instead of "
                              "dynamic work-stealing.")
    parser.add_argument("--no-watermark-removal", action="store_true",
                         help="Disable automatic Gemini sparkle-watermark removal.")
    parser.add_argument("--watermark-denoise", default="ai",
                         choices=["ai", "ns", "telea", "soft", "off"],
                         help="Cleanup pass after watermark removal (default: ai).")
    parser.add_argument("--watermark-legacy", action="store_true",
                         help="Pin the pre-Gemini-3.5 watermark profile for removal.")
    parser.add_argument("--gwt-binary", default=None,
                         help="Path to the GeminiWatermarkTool/gwt-mini CLI binary.")
    parser.add_argument("--no-resize", action="store_true",
                         help="Disable automatic center-crop + resize to 1920x1080.")
    parser.add_argument("--login-only", action="store_true",
                         help="Launch Chrome session(s), wait for sign-in, then STOP.")
    parser.add_argument("--no-human-behavior", action="store_true",
                         help="Disable all humanization patches (curved mouse movement, "
                              "paste pauses, jittered waits) — falls back to the exact raw "
                              "timing of automate_google_flow.py. Useful for A/B comparison.")
    args = parser.parse_args()

    if args.no_human_behavior:
        hb.set_enabled(False)
        print("  ⚠ Human-behavior patches DISABLED via --no-human-behavior "
              "(raw automate_google_flow.py timing).")

    if args.login_timeout > 0:
        flow.LOGIN_TIMEOUT_SEC = args.login_timeout

    if args.login_only:
        if args.workers > 1:
            run_login_only_parallel_human(
                num_workers=args.workers,
                use_existing_browser=args.use_existing_browser,
                cdp_port=args.cdp_port,
            )
        else:
            flow.run(
                start_from=0,
                use_existing_browser=args.use_existing_browser,
                cdp_port=args.cdp_port,
                project_url=flow.PROJECT_URLS[0] if flow.PROJECT_URLS else flow.PROJECT_URL,
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
        run_parallel_human(
            num_workers=args.workers,
            start_from=args.start,
            use_existing_browser=args.use_existing_browser,
            cdp_port=args.cdp_port,
            static_split=args.static_split,
            **watermark_kwargs,
        )

    elif args.workers > 1 and args.worker_id > 0:
        os.makedirs(flow.OUTPUT_DIR, exist_ok=True)
        flow.normalize_existing_to_png(flow.OUTPUT_DIR)
        prompts = flow.parse_prompts(flow.PROMPTS_FILE)
        chunks = flow.split_prompts_for_workers(prompts, args.workers)
        if args.worker_id > len(chunks):
            print(f"ERROR: --worker-id {args.worker_id} > --workers {args.workers}")
            sys.exit(1)
        chunk = chunks[args.worker_id - 1]
        if not chunk:
            print(f"Worker {args.worker_id} has no prompts assigned.")
            sys.exit(0)
        print(f"Running Worker {args.worker_id}/{args.workers} (humanized)  "
              f"(prompts #{chunk[0]['index']}-#{chunk[-1]['index']})")
        flow.run(
            start_from=args.start,
            use_existing_browser=args.use_existing_browser,
            cdp_port=args.cdp_port + (args.worker_id - 1),
            worker_id=args.worker_id,
            worker_prompts=chunk,
            interactive=True,
            project_url=(flow.PROJECT_URLS[args.worker_id - 1]
                         if args.worker_id - 1 < len(flow.PROJECT_URLS) else flow.PROJECT_URL),
            **watermark_kwargs,
        )

    else:
        os.makedirs(flow.OUTPUT_DIR, exist_ok=True)
        flow.normalize_existing_to_png(flow.OUTPUT_DIR)
        flow.run(
            start_from=args.start,
            use_existing_browser=args.use_existing_browser,
            cdp_port=args.cdp_port,
            project_url=flow.PROJECT_URLS[0] if flow.PROJECT_URLS else flow.PROJECT_URL,
            **watermark_kwargs,
        )



