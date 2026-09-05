# camcorder-browser

Local web app for browsing and converting old camcorder SD-card footage.
Built for Panasonic-style `.MOD` (MPEG-2) files but works with any format ffmpeg can read.

## What it does

- Scans an SD card for videos, groups them by day
- Big thumbnails with duration badges
- Click a thumbnail to preview in a modal player with a scrubbable time bar
- Click a filename to rename the actual file on disk (cache follows the rename)
- Multiselect + one click to batch-convert to MP4 (hardware-accelerated via `h264_videotoolbox`)
- Picks the output folder via native macOS folder dialog

## Requirements

- macOS (uses `osascript` for the folder picker and `h264_videotoolbox` for fast encoding)
- `ffmpeg` and `ffprobe` on PATH: `brew install ffmpeg`
- Python 3 (stdlib only, no packages needed)

## Run

```sh
python3 server.py
```

Opens `http://127.0.0.1:8765` in the default browser.

By default it scans `/Volumes/Untitled/SD_VIDEO` and `/Volumes/Untitled/DCIM`.
Pass alternative roots as CLI args: `python3 server.py /path/to/card`.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `Cmd/Ctrl-A` | Select all |
| `Esc` | Clear selection / close preview / cancel rename |
| `←` `→` | Previous / next video (in preview modal) |
| `Enter` | Rename current video (in preview modal) |

## Caches

Thumbnails and 720p preview MP4s are cached under
`~/Library/Caches/camcorder-browser/`. Delete that folder to reclaim disk.
