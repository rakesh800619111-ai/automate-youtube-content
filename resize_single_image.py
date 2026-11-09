#!/usr/bin/env python3
"""
resize_single_image.py
=======================
One-off / test utility: resize a SINGLE image to 1920x1080 using the exact
same center-crop + LANCZOS resize logic already defined in
automate_google_flow.py (resize_to_1920x1080()) — WITHOUT touching
watermark removal at all.

The source file is left completely untouched; the image is copied into the
output directory first (saved as a genuine PNG, matching this pipeline's
"{name}.png" convention) and only the COPY is resized in place.

Usage:
    python3 resize_single_image.py /path/to/source.jpeg /path/to/output_dir
    python3 resize_single_image.py /path/to/source.jpeg   # defaults to /Users/apple/Downloads/resized
"""

import os
import sys

# Reuse the exact same resize logic as the main pipeline (no duplication).
import automate_google_flow as flow


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 resize_single_image.py <source_image> [output_dir]")
        sys.exit(1)

    src_path = os.path.abspath(sys.argv[1])
    out_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else "/Users/apple/Downloads/resized"

    if not os.path.isfile(src_path):
        print(f"ERROR: not a file: {src_path}")
        sys.exit(1)

    os.makedirs(out_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(src_path))[0]
    dst_path = os.path.join(out_dir, f"{base}.png")

    # Load + re-save as a genuine PNG copy (resize_to_1920x1080 always writes
    # PNG bytes regardless of source format, so the output extension matches
    # what actually ends up on disk).
    from PIL import Image
    with Image.open(src_path) as im:
        im.load()
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        im.save(dst_path, format="PNG")

    print(f"Copied  -> {dst_path}  ({Image.open(dst_path).size[0]}x{Image.open(dst_path).size[1]})")

    ok = flow.resize_to_1920x1080(dst_path)
    from PIL import Image as Image2
    with Image2.open(dst_path) as im2:
        final_size = im2.size

    if ok:
        print(f"Resized -> {dst_path}  ({final_size[0]}x{final_size[1]})  [watermark removal NOT run]")
    else:
        print(f"ERROR: resize failed for {dst_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()

