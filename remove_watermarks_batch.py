#!/usr/bin/env python3
"""
remove_watermarks_batch.py
===========================
Batch-cleanup pass for a folder of ALREADY-DOWNLOADED images (any filename,
any source format: .png/.jpg/.jpeg/.webp/etc.) that still have the visible
Gemini "sparkle" watermark. Strips it using the bundled GeminiWatermarkTool
CLI (https://github.com/allenk/GeminiWatermarkTool), automatically picking
the correct LOCKED region for whatever resolution each image actually is
(see WATERMARK_LOCKED_REGIONS in automate_google_flow.py — currently
supports the native 1376x768 Nano Banana output AND the already-resized
1920x1080 case).

Output is ALWAYS a genuine PNG — same as every other part of this pipeline
(ensure_png_bytes(), prompt_filename(), etc.) — regardless of what format
the source file actually was. Only the BASE filename is preserved (no
renaming to numbers, no random suffix); the extension is normalized to
".png". e.g. "photo.jpeg" in -> "photo.png" out.

Idempotent: keeps a small state file (.watermarks_removed.json) inside the
target folder listing which base filenames have already been cleaned, so
re-running the script (e.g. after downloading more images) only processes
NEW files and never re-applies removal to an already-cleaned image.

Usage:
    python3 remove_watermarks_batch.py /path/to/source_dir --output /path/to/output_dir
    python3 remove_watermarks_batch.py /path/to/source_dir --output /path/to/output_dir --denoise ai
    python3 remove_watermarks_batch.py /path/to/source_dir --redo   # ignore state file, reprocess all
"""

import argparse
import json
import os
import sys

# Reuse the exact same removal logic / binary discovery as the main pipeline.
import automate_google_flow as flow

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}


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


def save_as_png(src_path: str, dst_path: str) -> bool:
    """
    Load src_path (whatever format it actually is) and write a GENUINE PNG
    to dst_path — same guarantee as automate_google_flow.ensure_png_bytes().
    No-op copy if src is already a real PNG at heart, still re-saved for
    consistency. Never raises; returns False on failure.
    """
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.save(dst_path, format="PNG")
        return True
    except Exception as e:
        print(f"  ⚠ Could not convert {os.path.basename(src_path)} to PNG: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Batch-remove Gemini watermarks from a folder of downloaded images (any filenames/formats). Output is always PNG.")
    parser.add_argument("folder", help="Directory containing images (any filenames, .png/.jpg/.jpeg/.webp/...)")
    parser.add_argument("--output", default=None,
                         help=(
                             "Optional separate output directory. When given, the "
                             "SOURCE folder is left completely untouched — each file "
                             "is converted to a genuine PNG (same base filename, "
                             "'.png' extension) in --output first, and watermark "
                             "removal runs on that PNG copy only."
                         ))
    parser.add_argument("--denoise", default="ai", choices=["ai", "ns", "telea", "soft", "off"],
                         help="Cleanup pass after removal (default: ai)")
    parser.add_argument("--legacy", action="store_true", help="Pin the pre-Gemini-3.5 watermark profile")
    parser.add_argument("--gwt-binary", default=None, help="Path to gwt-mini/GeminiWatermarkTool binary")
    parser.add_argument("--redo", action="store_true", help="Ignore the state file and reprocess every image")
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"ERROR: not a directory: {folder}")
        sys.exit(1)

    out_dir = os.path.abspath(args.output) if args.output else folder
    if args.output:
        os.makedirs(out_dir, exist_ok=True)

    flow.REMOVE_WATERMARKS = True
    flow.WATERMARK_DENOISE = args.denoise
    flow.WATERMARK_LEGACY = args.legacy
    if args.gwt_binary:
        flow.WATERMARK_BINARY = args.gwt_binary

    if not flow.WATERMARK_BINARY:
        print("ERROR: gwt-mini binary not found. Place it at tools/gwt-mini next to "
              "automate_google_flow.py, or pass --gwt-binary /path/to/gwt-mini")
        print("Download: https://github.com/allenk/GeminiWatermarkTool/releases")
        sys.exit(1)

    state_path = os.path.join(out_dir, ".watermarks_removed.json")
    done = set() if args.redo else load_state(state_path)

    targets = []
    for fname in sorted(os.listdir(folder)):
        base, ext = os.path.splitext(fname)
        if ext.lower() in IMAGE_EXTS and base not in done:
            targets.append((base, os.path.join(folder, fname)))

    print(f"Found {len(targets)} image(s) to process from '{folder}' "
          f"(denoise={args.denoise}{', legacy' if args.legacy else ''}, "
          f"known resolutions: {sorted(flow.WATERMARK_LOCKED_REGIONS.keys())})"
          f"{' -> output: ' + out_dir if args.output else ' (in-place)'} "
          f"— output is always PNG.")

    ok_count = fail_count = 0
    for base, src_path in targets:
        # Base filename preserved EXACTLY — only the extension is normalized
        # to ".png" (output is ALWAYS a genuine PNG, no matter the source
        # format), matching this pipeline's existing convention everywhere
        # else (ensure_png_bytes(), prompt_filename(), etc.).
        dst_path = os.path.join(out_dir, f"{base}.png")
        if not os.path.exists(dst_path):
            if not save_as_png(src_path, dst_path):
                fail_count += 1
                print(f"  ✗ {base} (conversion to PNG failed, left untouched)")
                continue
        success = flow.remove_watermark(dst_path)
        if success:
            done.add(base)
            ok_count += 1
            print(f"  ✓ {base}.png")
        else:
            fail_count += 1
            print(f"  ✗ {base}.png (left untouched)")
        # Persist progress after every file so a crash/interrupt loses no work.
        save_state(state_path, done)

    print(f"\nDone. {ok_count} processed, {fail_count} failed/skipped.")


if __name__ == "__main__":
    main()

