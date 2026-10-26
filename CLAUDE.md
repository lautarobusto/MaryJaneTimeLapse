# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Flask app (`app.py`) + one Jinja template (`templates/index.html`) that captures frames from an RTSP IP camera (Imou IPC-C22E-A) with ffmpeg, stores them in Box.com via rclone, and stitches them into timelapse videos. UI text and user-facing error strings are in Spanish.

## Commands

Runs only inside Docker (hardcoded `/app/...` paths; ffmpeg, ffprobe and rclone must be on PATH).

**Production** (`ssh lautaro@192.168.0.10`): Portainer stack `maryjanetimelapse` (https://192.168.0.10:9443) running `ghcr.io/lautarobusto/maryjanetimelapse:latest`. Deploy = push to `master` → CI builds and pushes the image → Watchtower (label `com.centurylinklabs.watchtower.enable`) pulls it, or redeploy the stack in Portainer with "re-pull image". The stack's compose lives in Portainer (not the repo's `docker-compose.yml`); `CAMERA_URL` is a stack env var; `/home/lautaro/MaryJaneTimeLapse/data` holds the persisted settings. The repo's `docker-compose.yml` is for building/running locally.

```bash
docker compose up -d --build      # http://<host>:5000
docker compose logs -f timelapse  # [OK]/[ERR] lines from capture, upload, timelapse
```

Requires a `.env` (see `.env.example`: `CAMERA_URL`, `CAPTURE_INTERVAL`) and an rclone config with a remote named `box` at `~/.config/rclone/rclone.conf` on the host (mounted read-write: rclone refreshes the OAuth token in place). The container runs as uid 1000, which must own that file.

No tests or linter. CI (`.github/workflows/docker-build.yml`) builds the image and pushes it to GHCR on non-PR events.

## Architecture

Design goal: **avoid writing frames to the server's SSD.**

- **Storage tiers**: `/app/spool` and `/app/work` are tmpfs (RAM) mounts; `/app/data` (disk) only holds `settings.json` (capture interval set from the UI) and `videos.json` (video durations). Everything else lives in Box under `BOX_REMOTE` (default `box:MaryJaneTimeLapse`): `frames/YYYY-MM-DD/frame_YYYYMMDD_HHMMSS.jpg`, `thumbs/` (same layout, 320px). Generated videos are **not** uploaded to Box: `timelapse_<ts>.mp4` + `_thumb.jpg` live in `/app/work/videos` (RAM) for download and are lost on container restart.
- **Filenames are the metadata** (no database). Date/time come from `frame_YYYYMMDD_HHMMSS.jpg`; keep the naming and day-folder layout. Names use the container's clock (the image sets no `TZ`); don't change the container TZ, as new names would collide with existing ones and overwrite them in Box. The UI converts to the browser's time using `server_utc_offset()`, and `POST /api/timelapse` takes the browser's `utc_offset` so date ranges mean local days.
- **Capture → batch upload**: an APScheduler job captures frame + thumb into `spool/tmp`, `os.replace`s them into `spool/{frames,thumbs}/<day>/` and hardlinks them into `spool/pending/{frames,thumbs}/<day>/`. `upload_spool` (every `UPLOAD_INTERVAL`, default 600s, and on SIGTERM — compose gives it a 2m grace period because the tmpfs is wiped on stop) `rclone move`s the pending dir to Box, so only the hardlink disappears. `prune_local` then deletes local frames older than `LOCAL_RETENTION_HOURS` (48) that are no longer pending. Spool tmpfs is capped at 3g.
- **Reads**: frame listings come from `rclone lsf` cached by `RemoteCache` (invalidate after writes), merged with local frames. Frames are served from the spool when present, otherwise streamed from Box with `rclone cat` (`stream_remote`).
- **Frame viewer** (UI): the page embeds `frames_by_day()` (`{day: [HHMMSS, ...]}`, also `GET /api/frames?compact=1`) and the JS rebuilds filenames from it. Dragging/swiping scrubs through thumbs (preloaded with limited concurrency around the current frame); the full frame loads once it stops. When the viewer reaches a day it calls `POST /api/thumbs/<day>/prefetch`, which copies that day's thumbs from Box into `/app/work/thumbcache/<day>` (RAM) with one `rclone copy`, keeping the `THUMB_CACHE_DAYS` (7) most recently viewed days.
- **Timelapse** is async: `POST /api/timelapse` starts a thread; the UI polls `GET /api/timelapse/<job_id>` (in-memory `jobs`, with a `progress` text and a `percent` for the UI progress bar, fed by the downloaded file count and ffmpeg `-progress`). `create_timelapse` copies frames still kept locally and downloads the rest of the date range into `/app/work/<job>` (RAM, 8g cap) with `rclone copy --files-from`, renders with ffmpeg via a numbered symlink sequence, moves the mp4 + thumb to `/app/work/videos` and deletes the job dir.
- Capture interval minimum is 60s (`MIN_CAPTURE_INTERVAL`).
