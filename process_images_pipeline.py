#!/usr/bin/env python3
"""
process_images_pipeline.py
===========================
Runs any folder of images through the EXACT SAME 3-stage pipeline used by
automate_google_flow.py right after a download:

    1. Convert to a genuine PNG        (ensure_png_bytes)
    2. Remove the Gemini sparkle watermark (remove_watermark, LOCKED region)
    3. Center-crop excess aspect ratio + resize to 1920x1080
       (resize_to_1920x1080)

Order matters (identical to the live pipeline): watermark removal MUST run
BEFORE resize, since resizing shifts/stretches the watermark's pixel
position and would break the locked-region match.

Every file is OVERWRITTEN IN PLACE inside the same directory it was found
in. Non-PNG source files (e.g. .jpg/.jpeg/.webp) are converted and saved as
"<name>.png", and the original non-PNG file is then deleted so no
duplicate remains.

Idempotent: keeps a small state file (.pipeline_processed.json) inside the
target folder listing which filenames have already been fully processed,
so re-running only touches NEW files. Pass --redo to ignore it and
reprocess everything.

Usage:
    python3 process_images_pipeline.py
    python3 process_images_pipeline.py /path/to/folder
    python3 process_images_pipeline.py /path/to/folder --redo
    python3 process_images_pipeline.py /path/to/folder --no-watermark-removal
    python3 process_images_pipeline.py /path/to/folder --no-resize
"""

import argparse
import json
import os
import sys

# Reuse the exact same conversion / removal / resize logic as the main pipeline.
import automate_google_flow as flow

DEFAULT_DIR = "/Users/apple/Downloads/process"

IMAGE_EXT = {".jpeg", ".jpg", ".png", ".webp", ".gif", ".avif", ".bmp", ".tiff", ".tif"}


def load_state(state_path: str) -> set[str]:
    if os.path.exists(state_path):
        try:
            with open(state_path) as fh:
                return set(json.load(fh).get("done", []))
        except Exception:
            pass
    return set()


def save_state(state_path: str, done: set[str]):
    with open(state_path, "w") as fh:
        json.dump({"done": sorted(done)}, fh, indent=2)


def process_one(fpath: str) -> tuple[bool, str]:
    """
    Run the 3-stage pipeline on a single file, overwriting it in place.
    Returns (success, final_path). final_path may differ from fpath if the
    source wasn't already a .png (e.g. photo.jpg -> photo.png).
    """
    directory = os.path.dirname(fpath)
    fname = os.path.basename(fpath)
    base, ext = os.path.splitext(fname)
    png_path = os.path.join(directory, f"{base}.png")

    try:
        with open(fpath, "rb") as fh:
            data = fh.read()
    except Exception as e:
        print(f"  ✗ Could not read file: {e}")
        return False, fpath

    # ── Stage 1: guarantee a genuine PNG on disk ──────────────────────────
    if ext.lower() == ".png" and flow._looks_like_png(data):
        print("  • Already a genuine PNG")
    else:
        png_data = flow.ensure_png_bytes(data)
        if not png_data:
            print("  ✗ Could not convert to PNG – skipping")
            return False, fpath
        try:
            with open(png_path, "wb") as fh:
                fh.write(png_data)
            if fpath != png_path and os.path.exists(fpath):
                os.remove(fpath)
            print(f"  ✓ Converted → {os.path.basename(png_path)}")
        except Exception as e:
            print(f"  ✗ Failed writing PNG: {e}")
            return False, fpath

    # ── Stage 2: watermark removal (LOCKED region, in place) ─────────────
    wm_ok = flow.remove_watermark(png_path)
    print(f"  {'✓' if wm_ok else '⚠'} Watermark removal {'done' if wm_ok else 'skipped/failed'}")

    # ── Stage 3: center-crop + resize to 1920x1080 (in place) ────────────
    rs_ok = flow.resize_to_1920x1080(png_path)
    print(f"  {'✓' if rs_ok else '⚠'} Resize {'done' if rs_ok else 'failed'}")

    return True, png_path


def main():
    parser = argparse.ArgumentParser(
        description="Run existing images through convert->watermark-removal->resize, in place."
    )
    parser.add_argument("folder", nargs="?", default=DEFAULT_DIR,
                         help=f"Directory of images to process (default: {DEFAULT_DIR})")
    parser.add_argument("--redo", action="store_true",
                         help="Ignore the state file and reprocess every image")
    parser.add_argument("--no-watermark-removal", action="store_true",
                         help="Skip stage 2 (watermark removal)")
    parser.add_argument("--no-resize", action="store_true",
                         help="Skip stage 3 (resize to 1920x1080)")
    parser.add_argument("--watermark-denoise", default="ai",
                         choices=["ai", "ns", "telea", "soft", "off"],
                         help="Cleanup pass after watermark removal (default: ai)")
    parser.add_argument("--watermark-legacy", action="store_true",
                         help="Pin the pre-Gemini-3.5 watermark profile")
    parser.add_argument("--gwt-binary", default=None,
                         help="Path to gwt-mini/GeminiWatermarkTool binary")
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"ERROR: not a directory: {folder}")
        sys.exit(1)

    # Wire the shared module's globals so remove_watermark()/resize_to_1920x1080()
    # behave exactly as requested by the CLI flags.
    flow.REMOVE_WATERMARKS = not args.no_watermark_removal
    flow.WATERMARK_DENOISE = args.watermark_denoise
    flow.WATERMARK_LEGACY  = args.watermark_legacy
    flow.RESIZE_ENABLED    = not args.no_resize
    if args.gwt_binary:
        flow.WATERMARK_BINARY = args.gwt_binary

    if flow.REMOVE_WATERMARKS and not flow.WATERMARK_BINARY:
        print("⚠ gwt-mini binary not found — watermark removal will be skipped for "
              "every image (place it at tools/gwt-mini next to automate_google_flow.py, "
              "or pass --gwt-binary /path/to/gwt-mini).")
        print("  Download: https://github.com/allenk/GeminiWatermarkTool/releases")

    state_path = os.path.join(folder, ".pipeline_processed.json")
    done = set() if args.redo else load_state(state_path)

    all_files = sorted(f for f in os.listdir(folder)
                        if os.path.splitext(f)[1].lower() in IMAGE_EXT)
    targets = [f for f in all_files if f not in done]

    skipped = len(all_files) - len(targets)
    print(f"Found {len(all_files)} image(s) in '{folder}'"
          f"{f' ({skipped} already processed, skipping)' if skipped else ''}.")
    print(f"Processing {len(targets)} image(s) …\n")

    ok_count = 0
    fail_count = 0

    for fname in targets:
        fpath = os.path.join(folder, fname)
        print(f"── {fname} " + "─" * max(0, 50 - len(fname)))
        success, final_path = process_one(fpath)
        if success:
            ok_count += 1
            done.add(os.path.basename(final_path))
        else:
            fail_count += 1
        # Persist progress after every file so a crash/interrupt loses no work.
        save_state(state_path, done)

    print(f"\n{'='*60}")
    print(f"Done. Processed: {ok_count}   Failed: {fail_count}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

