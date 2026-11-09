# Google Flow Image Generation Pipeline

Automates image generation in [Google Flow](https://labs.google/fx/tools/flow)
("Nano Banana 2" model), then cleans and normalizes every downloaded image
before it's considered final output.

## Pipeline order (fixed, do not reorder)

```
Google Flow (Nano Banana 2)
        │  always generates at 1376×768
        ▼
1. Download           (automate_google_flow.py → try_download_image())
        ▼
2. Watermark removal  (remove_watermark() — GeminiWatermarkTool CLI, --force
                        --region 1255,647,48,48 --denoise ai)
        ▼
3. Resize to 16:9     (resize_to_1920x1080() — center-crop tiny aspect
                        excess, LANCZOS resize to exactly 1920×1080)
        ▼
4. Saved to OUTPUT_DIR (as "<prompt_index>.png", e.g. 1.png, 2.png, …)
```

Watermark removal **must** run before resize — resizing shifts pixel
positions and would break the locked-region match.

## Files

| File                          | Purpose |
|-------------------------------|---------|
| `automate_google_flow.py`     | Main pipeline: drives Playwright + real Chrome to submit prompts to Google Flow, waits for each generated image, downloads it, removes the watermark, resizes to 1920×1080, and saves it. Supports single-worker and multi-worker (parallel, dynamic work-stealing) modes. |
| `remove_watermarks_batch.py`  | Standalone cleanup CLI — re-runs *only* the watermark-removal step against a folder of already-downloaded images (e.g. images downloaded before this pipeline existed, or a folder you want cleaned separately). Supports `--output <dir>` to write cleaned copies elsewhere instead of overwriting in place. |
| `tools/gwt-mini`               | Prebuilt CLI binary from [GeminiWatermarkTool](https://github.com/allenk/GeminiWatermarkTool) (v0.3.2, universal x86_64/arm64 macOS binary). Reverses the Screen + Color-Burn blend Google uses to composite the visible "sparkle" watermark. Source repo (for reference/rebuilding) lives at `~/IdeaProjects/GeminiWatermarkTool`. |
| `requirements.txt`             | Python dependencies scoped to this pipeline only (`playwright`, `pillow`, `certifi`). |

## Locked watermark region

Nano Banana 2 always generates at **1376×768**. The watermark's exact pixel
position at that resolution was empirically measured (pixel-diff) and
**visually confirmed clean** across 200+ real downloaded images (`second
batch` + `third batch`, 100 images each):

```python
WATERMARK_LOCKED_REGIONS = {
    (1376, 768): "1255,647,48,48",   # x, y, w, h
}
```

Any resolution not in this table falls back to the tool's own built-in
`--force` default (no `--region` guess) — this path should never actually
trigger in production since Nano Banana's output size is fixed.

**Do not add new entries to this table from pixel-diff evidence alone** — a
plausible diff bounding box is not sufficient proof of correctness (this bit
us once). Only add an entry after directly, visually confirming the result
on real images.

## Setup

```bash
cd google_flow_pipeline
pip install -r requirements.txt
playwright install chrome   # first time only — installs/registers Chrome channel
```

`tools/gwt-mini` is already executable and bundled — no build step required.

## Usage

```bash
# Single worker (default), skips images already on disk
python3 automate_google_flow.py

# Resume/force from a specific prompt number
python3 automate_google_flow.py --start 25

# 4 parallel browser workers, dynamic work-stealing (default)
python3 automate_google_flow.py --workers 4

# Disable watermark removal or resize for this run only
python3 automate_google_flow.py --no-watermark-removal
python3 automate_google_flow.py --no-resize

# Clean watermarks from an already-downloaded folder, in place
python3 remove_watermarks_batch.py "/path/to/folder"

# Same, but write cleaned copies to a separate folder (leaves source untouched)
python3 remove_watermarks_batch.py "/path/to/folder" --output "/path/to/folder_clean"
```

## Configuration

Edit the constants near the top of `automate_google_flow.py`:

- `PROMPTS_FILE` — path to the prompts text file to read from.
- `OUTPUT_DIR` — where final processed images (and progress/failure logs) are saved.
- `PROJECT_URL` / `PROJECT_URLS` — Google Flow project URL(s), one per worker.
- `GENERATION_TIMEOUT_SEC`, `POLL_INTERVAL_SEC`, `POST_DOWNLOAD_WAIT_SEC`, `LOGIN_TIMEOUT_SEC` — timing knobs.

