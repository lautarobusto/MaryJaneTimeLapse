import os
import subprocess
import re
import uuid
import json
import threading
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from flask import Flask, render_template, jsonify, request, send_file, Response, abort
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
TIMELAPSE_FRAME_DURATION = float(os.environ.get('TIMELAPSE_FRAME_DURATION', '0.1'))
BOX_REMOTE = os.environ.get('BOX_REMOTE', 'box:MaryJaneTimeLapse').rstrip('/')
UPLOAD_INTERVAL = int(os.environ.get('UPLOAD_INTERVAL', '600'))
LOCAL_RETENTION_HOURS = float(os.environ.get('LOCAL_RETENTION_HOURS', '48'))
MIN_CAPTURE_INTERVAL = 60
THUMB_CACHE_DAYS = int(os.environ.get('THUMB_CACHE_DAYS', '7'))

# Storage layout:
#   SPOOL_DIR (tmpfs/RAM): the last LOCAL_RETENTION_HOURS of frames, so short timelapses
#     need no download. spool/pending holds hardlinks of frames not yet uploaded to Box.
#   WORK_DIR  (tmpfs/RAM): scratch space for building timelapses
#   DATA_DIR  (disk): small settings/index files only
#   Box, first camera: {BOX_REMOTE}/frames/YYYY-MM-DD/frame_*.jpg (thumbs/ mirrors it)
#   Box, later cameras: {BOX_REMOTE}/frames/<id>/YYYY-MM-DD/frame_*.jpg
SPOOL_DIR = '/app/spool'
WORK_DIR = '/app/work'
DATA_DIR = '/app/data'
SPOOL_FRAMES = f"{SPOOL_DIR}/frames"
SPOOL_THUMBS = f"{SPOOL_DIR}/thumbs"
SPOOL_TMP = f"{SPOOL_DIR}/tmp"
SPOOL_PENDING = f"{SPOOL_DIR}/pending"
# Generated videos live on the data disk so a container restart does not delete them.
# Not uploaded to Box. Render scratch stays in WORK_DIR (RAM).
VIDEOS_DIR = f"{DATA_DIR}/videos"
# Whole days of thumbs copied from Box so the frame viewer can scrub them quickly
THUMB_CACHE = f"{WORK_DIR}/thumbcache"
SETTINGS_FILE = f"{DATA_DIR}/settings.json"
VIDEOS_INDEX = f"{DATA_DIR}/videos.json"

for d in (SPOOL_FRAMES, SPOOL_THUMBS, SPOOL_TMP, SPOOL_PENDING, WORK_DIR, VIDEOS_DIR, THUMB_CACHE, DATA_DIR):
    os.makedirs(d, exist_ok=True)

FRAME_RE = re.compile(r'^frame_(\d{8})_(\d{6})\.jpg$')
VIDEO_RE = re.compile(r'^timelapse_(?:[a-z0-9-]+_)?\d{8}_\d{6}(?:_thumb\.jpg|\.mp4)$')
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def adopt_legacy_videos():
    """Videos used to live in the work tmpfs. Keep any that are still there."""
    legacy = f"{WORK_DIR}/videos"
    if not os.path.isdir(legacy):
        return
    for name in os.listdir(legacy):
        src = f"{legacy}/{name}"
        dst = f"{VIDEOS_DIR}/{name}"
        if os.path.isfile(src) and VIDEO_RE.match(name) and not os.path.exists(dst):
            shutil.move(src, dst)
            print(f"[OK] Moved video to disk {name}")


adopt_legacy_videos()
CAM_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
DEFAULT_CAM = 'default'


def load_cameras():
    """CAMERA_URL keeps the original layout. CAMERA_2_* adds another camera under its own folder."""
    cameras = []
    url = os.environ.get('CAMERA_URL', '').strip()
    if url:
        cameras.append({
            'id': DEFAULT_CAM,
            'name': os.environ.get('CAMERA_NAME', '').strip() or 'Cámara',
            'url': url,
            'host': urlparse(url).hostname or '',
        })
    url2 = os.environ.get('CAMERA_2_URL', '').strip()
    if url2:
        cid = os.environ.get('CAMERA_2_ID', 'carpa-a').strip() or 'carpa-a'
        if not CAM_RE.match(cid) or cid == DEFAULT_CAM:
            print(f"[ERR] Ignoring CAMERA_2_URL, CAMERA_2_ID is invalid: {cid}")
        else:
            cameras.append({
                'id': cid,
                'name': os.environ.get('CAMERA_2_NAME', '').strip() or 'Carpa A',
                'url': url2,
                'host': urlparse(url2).hostname or '',
            })
    return cameras


CAMERAS = load_cameras()
CAM_BY_ID = {c['id']: c for c in CAMERAS}


def camera_public():
    return [{'id': c['id'], 'name': c['name'], 'host': c['host']} for c in CAMERAS]


def redact_url(text, url):
    if not text:
        return ''
    if url:
        text = text.replace(url, 'rtsp://***')
    return text


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


settings = load_json(SETTINGS_FILE, {})
CAPTURE_INTERVAL = max(MIN_CAPTURE_INTERVAL, int(settings.get('interval', os.environ.get('CAPTURE_INTERVAL', '60'))))

scheduler = BackgroundScheduler()
jobs = {}


# --- rclone helpers ---------------------------------------------------------

def rclone(*args, timeout=300):
    return subprocess.run(['rclone', *args], capture_output=True, text=True, timeout=timeout)


class RemoteCache:
    """Caches a remote listing for `ttl` seconds."""

    def __init__(self, loader, ttl):
        self.loader = loader
        self.ttl = ttl
        self.value = None
        self.at = 0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.value is None or time.time() - self.at > self.ttl:
                try:
                    self.value = self.loader()
                    self.at = time.time()
                except Exception as e:
                    print(f"[ERR] Remote listing failed: {e}")
                    if self.value is None:
                        return []
            return self.value

    def invalidate(self):
        with self.lock:
            self.at = 0


def _list_remote(path):
    result = rclone('lsf', '-R', '--files-only', f"{BOX_REMOTE}/{path}", timeout=120)
    if result.returncode != 0:
        if 'directory not found' in result.stderr:
            return []
        raise RuntimeError(result.stderr[:300])
    return [line for line in result.stdout.splitlines() if line]


remote_frames = RemoteCache(lambda: _list_remote('frames'), ttl=120)
remote_about = RemoteCache(lambda: json.loads(rclone('about', 'box:', '--json', timeout=60).stdout or '{}'), ttl=600)


# --- frames -----------------------------------------------------------------

def parse_frame_date(filename):
    """Extract date from frame_YYYYMMDD_HHMMSS.jpg"""
    match = FRAME_RE.match(filename)
    if match:
        return datetime.strptime(match.group(1) + match.group(2), '%Y%m%d%H%M%S')
    return None


def frame_rel(cam, day, name):
    """Path under frames/ or thumbs/. The first camera stays at the historical day folders."""
    if cam == DEFAULT_CAM:
        return f"{day}/{name}"
    return f"{cam}/{day}/{name}"


def frame_dir(cam, day):
    if cam == DEFAULT_CAM:
        return day
    return f"{cam}/{day}"


def parse_listed_rel(rel):
    """Return (cam, day, name, dt) for a path relative to frames/ or thumbs/."""
    parts = [p for p in rel.split('/') if p]
    if not parts:
        return None
    name = parts[-1]
    dt = parse_frame_date(name)
    if not dt:
        return None
    if len(parts) == 2 and DAY_RE.match(parts[0]):
        return DEFAULT_CAM, parts[0], name, dt
    if len(parts) == 3 and CAM_RE.match(parts[0]) and parts[0] != DEFAULT_CAM and DAY_RE.match(parts[1]):
        return parts[0], parts[1], name, dt
    return None


def iter_local_frame_files(base):
    if not os.path.isdir(base):
        return
    for root, _, files in os.walk(base):
        rel = os.path.relpath(root, base).replace('\\', '/')
        if rel == '.':
            rel = ''
        for name in files:
            if parse_frame_date(name):
                yield rel, name


def iter_frames():
    """Yield (cam, day, name, dt) for frames in Box plus frames still in the spool."""
    seen = set()
    for rel in remote_frames.get():
        parsed = parse_listed_rel(rel)
        if not parsed:
            continue
        cam, day, name, dt = parsed
        key = (cam, name)
        if key in seen:
            continue
        seen.add(key)
        yield cam, day, name, dt
    for rel, name in iter_local_frame_files(SPOOL_FRAMES):
        parsed = parse_listed_rel(f"{rel}/{name}" if rel else name)
        if not parsed:
            continue
        cam, day, name, dt = parsed
        key = (cam, name)
        if key in seen:
            continue
        seen.add(key)
        yield cam, day, name, dt


def server_utc_offset():
    """Minutes east of UTC of the container's clock, which names the frames (UTC unless TZ is set).
    The UI uses it to show frame times in the viewer's own timezone."""
    return int(datetime.now().astimezone().utcoffset().total_seconds() // 60)


def frames_by_camera():
    """Compact listing for the UI: {cam: {day: [HHMMSS, ...]}} in chronological order"""
    grouped = {c['id']: {} for c in CAMERAS}
    for cam, _, name, dt in iter_frames():
        day = dt.strftime('%Y-%m-%d')
        times = grouped.setdefault(cam, {}).setdefault(day, [])
        stamp = dt.strftime('%H%M%S')
        if stamp not in times:
            times.append(stamp)
    for days in grouped.values():
        for day in days:
            days[day].sort()
    return grouped


def capture_frame(cam):
    """Capture a frame (plus a small thumbnail) from one camera into the RAM spool"""
    now = datetime.now()
    name = f"frame_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
    day = now.strftime('%Y-%m-%d')
    rel_dir = frame_dir(cam['id'], day)
    tmp_frame = f"{SPOOL_TMP}/{cam['id']}_{name}"
    tmp_thumb = f"{SPOOL_TMP}/{cam['id']}_thumb_{name}"

    try:
        result = subprocess.run([
            'ffmpeg', '-y', '-rtsp_transport', 'tcp',
            '-i', cam['url'],
            '-frames:v', '1', '-q:v', '2', tmp_frame,
            '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '5', tmp_thumb
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and os.path.getsize(tmp_frame) > 0:
            # Move into place atomically so the uploader never sees partial files,
            # and queue a hardlink for the next batch upload
            for src, base, kind in ((tmp_thumb, SPOOL_THUMBS, 'thumbs'), (tmp_frame, SPOOL_FRAMES, 'frames')):
                os.makedirs(f"{base}/{rel_dir}", exist_ok=True)
                os.makedirs(f"{SPOOL_PENDING}/{kind}/{rel_dir}", exist_ok=True)
                os.replace(src, f"{base}/{rel_dir}/{name}")
                os.link(f"{base}/{rel_dir}/{name}", f"{SPOOL_PENDING}/{kind}/{rel_dir}/{name}")
            print(f"[OK] Captured {cam['name']} {name}")
            return True
        print(f"[ERR] ffmpeg failed for {cam['name']}: {redact_url(result.stderr[-200:], cam['url'])}")
        return False
    except Exception as e:
        print(f"[ERR] Capture failed for {cam['name']}: {e}")
        return False
    finally:
        for f in (tmp_frame, tmp_thumb):
            if os.path.exists(f):
                os.remove(f)


def capture_all():
    ok = False
    for cam in CAMERAS:
        ok = capture_frame(cam) or ok
    return ok


def remove_empty_dirs(base):
    # Skip today's folders so we don't race a capture creating its directory
    if not os.path.isdir(base):
        return
    today = datetime.now().strftime('%Y-%m-%d')
    for root, dirs, files in os.walk(base, topdown=False):
        if root == base or files or dirs:
            continue
        leaf = os.path.basename(root)
        if leaf == today:
            continue
        try:
            os.rmdir(root)
        except OSError:
            pass


def count_files(base):
    return sum(len(files) for _, _, files in os.walk(base))


def upload_spool():
    """Upload the pending batch to Box; the pending hardlink is only removed after a successful upload"""
    moved = False
    for kind in ('thumbs', 'frames'):
        pending = f"{SPOOL_PENDING}/{kind}"
        os.makedirs(pending, exist_ok=True)
        n = count_files(pending)
        if n:
            result = rclone('move', pending, f"{BOX_REMOTE}/{kind}",
                            '--min-age', '2s', '--transfers', '8', '--retries', '3', timeout=3600)
            if result.returncode == 0:
                moved = True
                print(f"[OK] Uploaded {n} {kind} to Box")
            else:
                print(f"[ERR] Upload to Box failed: {result.stderr[-300:]}")
        remove_empty_dirs(pending)
    if moved:
        remote_frames.invalidate()
    prune_local()


def prune_local():
    """Drop local frames older than LOCAL_RETENTION_HOURS that are already in Box"""
    cutoff = datetime.now().timestamp() - LOCAL_RETENTION_HOURS * 3600
    removed = 0
    for base, kind in ((SPOOL_FRAMES, 'frames'), (SPOOL_THUMBS, 'thumbs')):
        for rel, name in list(iter_local_frame_files(base)):
            dt = parse_frame_date(name)
            local = f"{base}/{rel}/{name}" if rel else f"{base}/{name}"
            pending = f"{SPOOL_PENDING}/{kind}/{rel}/{name}" if rel else f"{SPOOL_PENDING}/{kind}/{name}"
            if dt and dt.timestamp() < cutoff and not os.path.exists(pending):
                os.remove(local)
                removed += 1
        remove_empty_dirs(base)
    if removed:
        print(f"[OK] Pruned {removed} local files older than {LOCAL_RETENTION_HOURS:g}h")


thumb_cache_lock = threading.Lock()
thumb_cache_busy = set()


def thumb_cache_key(cam, day):
    return f"{cam}__{day}"


def cache_day_thumbs(cam, day):
    """Copy a whole day of thumbs from Box into RAM with one rclone call, instead of one
    `rclone cat` per frame while the viewer scrubs through it"""
    key = thumb_cache_key(cam, day)
    tmp = f"{THUMB_CACHE}/.{key}"
    remote = f"{BOX_REMOTE}/thumbs/{frame_dir(cam, day)}"
    try:
        result = rclone('copy', remote, tmp,
                        '--transfers', '16', '--checkers', '16', timeout=1800)
        if result.returncode == 0:
            os.replace(tmp, f"{THUMB_CACHE}/{key}")
            print(f"[OK] Cached thumbs for {cam} {day}")
        else:
            print(f"[ERR] Thumb cache for {cam} {day} failed: {result.stderr[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        with thumb_cache_lock:
            thumb_cache_busy.discard(key)
    # Keep the most recently viewed days, with room for every camera
    keep = THUMB_CACHE_DAYS * max(1, len(CAMERAS))
    days = sorted((d for d in os.listdir(THUMB_CACHE) if not d.startswith('.')),
                  key=lambda d: os.path.getmtime(f"{THUMB_CACHE}/{d}"), reverse=True)
    for d in days[keep:]:
        shutil.rmtree(f"{THUMB_CACHE}/{d}", ignore_errors=True)


def local_frame(base, cam, filename):
    dt = parse_frame_date(filename)
    if not dt or cam not in CAM_BY_ID:
        abort(404)
    day = dt.strftime('%Y-%m-%d')
    return day, f"{base}/{frame_rel(cam, day, filename)}"


def stream_remote(path, mimetype, download_name=None):
    proc = subprocess.Popen(['rclone', 'cat', f"{BOX_REMOTE}/{path}"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while chunk := proc.stdout.read(256 * 1024):
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    headers = {'Cache-Control': 'public, max-age=86400'}
    if download_name:
        headers['Content-Disposition'] = f'attachment; filename="{download_name}"'
    return Response(generate(), mimetype=mimetype, headers=headers)


# --- videos -----------------------------------------------------------------

videos_lock = threading.Lock()


def get_video_duration(filepath):
    """Get video duration in seconds using ffprobe"""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', filepath
        ], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def generate_thumb(filepath, thumb_path):
    """Extract thumbnail from video at 50% duration"""
    dur = get_video_duration(filepath)
    seek_time = dur / 2 if dur else 0
    try:
        subprocess.run([
            'ffmpeg', '-y', '-ss', str(seek_time), '-i', filepath,
            '-vframes', '1', '-q:v', '3', '-vf', 'scale=480:-1',
            thumb_path
        ], capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def format_duration(seconds):
    if seconds is None:
        return 'N/A'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def list_videos():
    index = load_json(VIDEOS_INDEX, {})
    names = set(os.listdir(VIDEOS_DIR))
    result = []
    for name in sorted((n for n in names if n.endswith('.mp4')), reverse=True):
        result.append({
            'filename': name,
            'duration': format_duration(index.get(name)),
            'has_thumb': name.replace('.mp4', '_thumb.jpg') in names
        })
    return result


# ffmpeg filter chains for the timelapse render. Raw frames in Box are never modified.
_FIT_1080 = 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black'
# deflicker evens out exposure jumps between frames; framerate blends frames up to 30fps
# instead of duplicating them, so motion looks smooth rather than stepped
_GRADE = ('deflicker=size=5:mode=pm,' + _FIT_1080 + ','
          'eq=contrast=1.10:saturation=1.18:gamma=0.96,'
          'colorbalance=rs=-0.05:bs=0.07:rm=0.02:bm=-0.02:rh=0.07:gh=0.02:bh=-0.06,'
          "curves=master='0/0 0.25/0.20 0.75/0.82 1/1',"
          'unsharp=5:5:0.5,framerate=fps=30')
_FINISH = 'vignette=PI/4.5,noise=alls=5:allf=t+u,format=yuv420p'
LOOKS = {
    # Clean, sharp and true to the plants: less noise/flicker, livelier greens
    'plantas': ('deflicker=size=5:mode=pm,' + _FIT_1080 + ','
                'hqdn3d=3:2:4:3,cas=0.6,vibrance=intensity=0.25,eq=contrast=1.06:gamma=0.98,'
                'framerate=fps=30,format=yuv420p'),
    'natural': _FIT_1080 + ',fps=30',
    'cinematic': _GRADE + ',' + _FINISH,
    # 2.39:1 letterbox; also hides the camera's timestamp and logo
    'cinemascope': _GRADE + ',crop=1920:804:0:138,pad=1920:1080:0:138:black,' + _FINISH,
}
DEFAULT_LOOK = os.environ.get('TIMELAPSE_LOOK', 'plantas')


def create_timelapse(job_id, date_from=None, date_to=None, look=DEFAULT_LOOK, tz=None, cam_id=DEFAULT_CAM):
    """Download the frames in range from Box to RAM and render the video into VIDEOS_DIR.
    The date range is in `tz` (the browser's timezone) when given, otherwise in server time."""
    def status(msg, percent=None):
        jobs[job_id]['progress'] = msg
        if percent is not None:
            jobs[job_id]['percent'] = round(percent, 1)

    cam = CAM_BY_ID.get(cam_id) or CAM_BY_ID.get(DEFAULT_CAM)
    if not cam:
        raise ValueError('No hay cámara configurada')
    cam_id = cam['id']

    selected = []
    for frame_cam, _, name, dt in iter_frames():
        if frame_cam != cam_id:
            continue
        day = dt.astimezone(tz).date() if tz else dt.date()
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        selected.append((dt, name))

    if len(selected) < 2:
        print(f"[ERR] Not enough frames for timelapse (found {len(selected)})")
        raise ValueError('No hay suficientes frames')
    selected.sort()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    video_prefix = '' if cam_id == DEFAULT_CAM else f"{cam_id}_"
    job_dir = f"{WORK_DIR}/{job_id}"
    src_dir = f"{job_dir}/src"
    seq_dir = f"{job_dir}/seq"
    os.makedirs(src_dir)
    os.makedirs(seq_dir)

    try:
        # Frames kept locally (last LOCAL_RETENTION_HOURS) are copied; the rest come from Box
        to_download = []
        for dt, name in selected:
            day = dt.strftime('%Y-%m-%d')
            local = f"{SPOOL_FRAMES}/{frame_rel(cam_id, day, name)}"
            if os.path.exists(local):
                os.makedirs(f"{src_dir}/{day}", exist_ok=True)
                shutil.copy(local, f"{src_dir}/{day}/{name}")
            else:
                to_download.append(f"{day}/{name}")

        # Progress: download 0-40% (only when needed), render up to 95%, thumbnail to 100%
        render_start = 40 if to_download else 0
        if to_download:
            status(f'Descargando {len(to_download)} frames de Box...', 0)
            list_file = f"{job_dir}/files.txt"
            with open(list_file, 'w') as f:
                f.write('\n'.join(to_download))
            local_count = len(selected) - len(to_download)
            remote_frames_dir = f"{BOX_REMOTE}/frames" if cam_id == DEFAULT_CAM else f"{BOX_REMOTE}/frames/{cam_id}"
            proc = subprocess.Popen(['rclone', 'copy', remote_frames_dir, src_dir, '--files-from', list_file,
                                     '--transfers', '16', '--checkers', '16'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            # Drain stderr in the background so the pipe never fills up while we poll
            err = []
            reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
            reader.start()
            deadline = time.time() + 3600
            while proc.poll() is None:
                if time.time() > deadline:
                    proc.kill()
                    break
                done = max(0, count_files(src_dir) - local_count)
                status(f'Descargando frames de Box ({done}/{len(to_download)})...',
                       render_start * done / len(to_download))
                time.sleep(1)
            proc.wait()
            reader.join(timeout=5)
            if proc.returncode != 0:
                raise RuntimeError(f"Descarga de Box falló: {''.join(err)[-200:]}")

        i = 0
        for dt, name in selected:
            path = f"{src_dir}/{dt.strftime('%Y-%m-%d')}/{name}"
            if os.path.exists(path) and os.path.getsize(path) > 0:
                i += 1
                os.symlink(path, f"{seq_dir}/frame_{i:06d}.jpg")
        if i < 2:
            raise ValueError('No hay suficientes frames')

        status(f'Generando video con {i} frames...', render_start)
        output_file = f"{job_dir}/timelapse_{video_prefix}{timestamp}.mp4"
        expected = i * TIMELAPSE_FRAME_DURATION
        proc = subprocess.Popen([
            'ffmpeg', '-y', '-nostats', '-progress', 'pipe:1',
            '-framerate', str(1 / TIMELAPSE_FRAME_DURATION),
            '-i', f"{seq_dir}/frame_%06d.jpg",
            '-vf', LOOKS.get(look, LOOKS[DEFAULT_LOOK]),
            '-c:v', 'libx264', '-preset', 'slow', '-crf', '23',
            '-maxrate', '2300k', '-bufsize', '5000k', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', output_file
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        err = []
        reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
        reader.start()
        timer = threading.Timer(3600, proc.kill)
        timer.start()
        for line in proc.stdout:
            if line.startswith('out_time_us='):
                try:
                    frac = min(1.0, int(line.split('=', 1)[1]) / 1e6 / expected)
                except ValueError:
                    continue
                status(f'Generando video con {i} frames ({frac * 100:.0f}%)...',
                       render_start + (95 - render_start) * frac)
        proc.wait()
        timer.cancel()
        reader.join(timeout=5)
        if proc.returncode != 0:
            print(f"[ERR] ffmpeg failed: {''.join(err)[-500:]}")
            raise RuntimeError('ffmpeg falló al generar el video')

        status('Generando miniatura...', 95)
        thumb_path = output_file.replace('.mp4', '_thumb.jpg')
        generate_thumb(output_file, thumb_path)
        duration = get_video_duration(output_file)
        for f in (thumb_path, output_file):
            if os.path.exists(f):
                shutil.move(f, f"{VIDEOS_DIR}/{os.path.basename(f)}")

        with videos_lock:
            index = load_json(VIDEOS_INDEX, {})
            index[os.path.basename(output_file)] = duration
            save_json(VIDEOS_INDEX, index)
        status('Listo', 100)
        print(f"[OK] Created timelapse {os.path.basename(output_file)}")
        return os.path.basename(output_file)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# Schedule automatic capture and upload
scheduler.add_job(capture_all, 'interval', seconds=CAPTURE_INTERVAL, id='capture', max_instances=1)
scheduler.add_job(upload_spool, 'interval', seconds=UPLOAD_INTERVAL, id='upload', max_instances=1)
scheduler.start()


def shutdown(signum, frame):
    # The spool is tmpfs, so flush the pending batch to Box before the container stops
    print("[OK] Shutting down, uploading pending batch...")
    scheduler.shutdown(wait=False)
    upload_spool()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)


@app.route('/')
def index():
    return render_template('index.html',
                           days=frames_by_camera(),
                           cameras=camera_public(),
                           utc_offset=server_utc_offset(),
                           videos=list_videos(),
                           looks=list(LOOKS),
                           default_look=DEFAULT_LOOK,
                           interval=CAPTURE_INTERVAL)


@app.route('/api/capture', methods=['POST'])
def api_capture():
    data = request.get_json(silent=True) or {}
    cam_id = data.get('cam')
    if cam_id:
        cam = CAM_BY_ID.get(cam_id)
        success = capture_frame(cam) if cam else False
    else:
        success = capture_all()
    return jsonify({'success': success})


@app.route('/api/timelapse', methods=['POST'])
def api_timelapse():
    data = request.json or {}
    date_from = None
    date_to = None

    if data.get('date_from'):
        date_from = datetime.strptime(data['date_from'], '%Y-%m-%d').date()
    if data.get('date_to'):
        date_to = datetime.strptime(data['date_to'], '%Y-%m-%d').date()

    tz = None
    if data.get('utc_offset') is not None:
        tz = timezone(timedelta(minutes=int(data['utc_offset'])))

    look = data.get('look') or DEFAULT_LOOK
    if look not in LOOKS:
        return jsonify({'success': False, 'error': f'look debe ser uno de: {", ".join(LOOKS)}'}), 400

    cam_id = data.get('cam') or DEFAULT_CAM
    if cam_id not in CAM_BY_ID:
        return jsonify({'success': False, 'error': 'Cámara desconocida'}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'processing', 'file': None, 'error': None, 'progress': 'Preparando...', 'percent': 0}

    def run():
        try:
            output = create_timelapse(job_id, date_from, date_to, look, tz, cam_id)
            jobs[job_id] = {'status': 'done', 'file': output, 'error': None}
        except Exception as e:
            print(f"[ERR] Timelapse creation failed: {e}")
            jobs[job_id] = {'status': 'error', 'file': None, 'error': str(e)}

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'})


@app.route('/api/timelapse/<job_id>')
def api_timelapse_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(job)


@app.route('/api/frames')
def api_frames():
    days = frames_by_camera()
    if request.args.get('compact') == '1':
        return jsonify({'cameras': camera_public(), 'days': days, 'utc_offset': server_utc_offset()})
    return jsonify({'cameras': camera_public(), 'days': days, 'utc_offset': server_utc_offset()})


@app.route('/api/videos')
def api_videos():
    return jsonify({'videos': list_videos()})


@app.route('/api/storage')
def api_storage():
    return jsonify({
        'box': remote_about.get(),
        'pending_upload': count_files(f"{SPOOL_PENDING}/frames"),
        'local_frames': count_files(SPOOL_FRAMES),
        'local_retention_hours': LOCAL_RETENTION_HOURS,
        'upload_interval': UPLOAD_INTERVAL
    })


@app.route('/frame/<filename>')
def serve_frame(filename):
    if not FRAME_RE.match(filename):
        abort(404)
    cam = request.args.get('cam') or DEFAULT_CAM
    if cam not in CAM_BY_ID:
        abort(404)
    thumb = request.args.get('thumb') == '1'
    base, remote = (SPOOL_THUMBS, 'thumbs') if thumb else (SPOOL_FRAMES, 'frames')
    day, local = local_frame(base, cam, filename)
    if thumb and not os.path.exists(local):
        local = f"{THUMB_CACHE}/{thumb_cache_key(cam, day)}/{filename}"
    if os.path.exists(local):
        # Frames never change once captured
        return send_file(local, max_age=86400)
    return stream_remote(f"{remote}/{frame_rel(cam, day, filename)}", 'image/jpeg')


@app.route('/api/thumbs/<day>/prefetch', methods=['POST'])
def prefetch_thumbs(day):
    """Called by the viewer when it reaches a day, so scrubbing it doesn't hit Box per frame"""
    if not DAY_RE.match(day):
        abort(404)
    cam = request.args.get('cam') or DEFAULT_CAM
    if cam not in CAM_BY_ID:
        abort(404)
    key = thumb_cache_key(cam, day)
    cached = f"{THUMB_CACHE}/{key}"
    if os.path.isdir(cached):
        os.utime(cached)
        return jsonify({'status': 'cached'})
    local = f"{SPOOL_THUMBS}/{frame_dir(cam, day)}"
    total = len(frames_by_camera().get(cam, {}).get(day, []))
    if not total or (os.path.isdir(local) and len(os.listdir(local)) >= total):
        return jsonify({'status': 'local'})
    with thumb_cache_lock:
        if key in thumb_cache_busy:
            return jsonify({'status': 'loading'})
        thumb_cache_busy.add(key)
    threading.Thread(target=cache_day_thumbs, args=(cam, day), daemon=True).start()
    return jsonify({'status': 'loading'})


@app.route('/video/<filename>')
def serve_video(filename):
    if not VIDEO_RE.match(filename):
        abort(404)
    path = f"{VIDEOS_DIR}/{filename}"
    if not os.path.exists(path):
        abort(404)
    download = request.args.get('descarga') == '1'
    return send_file(
        path,
        mimetype='video/mp4',
        conditional=True,
        as_attachment=download,
        download_name=filename if download else None,
    )


@app.route('/thumb/<filename>')
def serve_thumb(filename):
    if not VIDEO_RE.match(filename):
        abort(404)
    path = f"{VIDEOS_DIR}/{filename}"
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype='image/jpeg')


def delete_camera_frames(cam):
    """Remove one camera's frames. The first camera lives in the day folders, not under frames/default."""
    count = sum(1 for frame_cam, _, _, _ in iter_frames() if frame_cam == cam)
    if cam == DEFAULT_CAM:
        for kind in ('frames', 'thumbs'):
            result = rclone('lsf', f"{BOX_REMOTE}/{kind}", '--dirs-only', '--max-depth', '1', timeout=120)
            if result.returncode != 0 and 'directory not found' not in result.stderr:
                raise RuntimeError(result.stderr[:300])
            for line in result.stdout.splitlines():
                day = line.strip().strip('/')
                if DAY_RE.match(day):
                    rclone('purge', f"{BOX_REMOTE}/{kind}/{day}", timeout=1800)
        for base in (SPOOL_FRAMES, SPOOL_THUMBS, f"{SPOOL_PENDING}/frames", f"{SPOOL_PENDING}/thumbs"):
            if not os.path.isdir(base):
                continue
            for name in os.listdir(base):
                path = f"{base}/{name}"
                if DAY_RE.match(name) and os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
    else:
        for kind in ('frames', 'thumbs'):
            rclone('purge', f"{BOX_REMOTE}/{kind}/{cam}", timeout=1800)
        for base in (SPOOL_FRAMES, SPOOL_THUMBS, f"{SPOOL_PENDING}/frames", f"{SPOOL_PENDING}/thumbs"):
            shutil.rmtree(f"{base}/{cam}", ignore_errors=True)
    if os.path.isdir(THUMB_CACHE):
        prefix = f"{cam}__"
        for name in os.listdir(THUMB_CACHE):
            if name.startswith(prefix) or (cam == DEFAULT_CAM and DAY_RE.match(name)):
                shutil.rmtree(f"{THUMB_CACHE}/{name}", ignore_errors=True)
    remote_frames.invalidate()
    return count


@app.route('/api/delete_frames', methods=['POST'])
def delete_frames():
    data = request.get_json(silent=True) or {}
    cam = data.get('cam') or DEFAULT_CAM
    if cam not in CAM_BY_ID:
        return jsonify({'success': False, 'error': 'Cámara desconocida'}), 400
    try:
        count = delete_camera_frames(cam)
    except Exception as e:
        print(f"[ERR] Delete frames failed: {e}")
        return jsonify({'success': False, 'error': 'No se pudieron borrar los frames'}), 500
    return jsonify({'success': True, 'deleted': count})


@app.route('/api/delete_videos', methods=['POST'])
def delete_videos():
    with videos_lock:
        deleted = len(os.listdir(VIDEOS_DIR))
        shutil.rmtree(VIDEOS_DIR, ignore_errors=True)
        os.makedirs(VIDEOS_DIR, exist_ok=True)
        save_json(VIDEOS_INDEX, {})
    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/config', methods=['POST'])
def update_config():
    global CAPTURE_INTERVAL
    data = request.json or {}
    if 'interval' in data:
        CAPTURE_INTERVAL = max(MIN_CAPTURE_INTERVAL, int(data['interval']))
        scheduler.reschedule_job('capture', trigger='interval', seconds=CAPTURE_INTERVAL)
        settings['interval'] = CAPTURE_INTERVAL
        save_json(SETTINGS_FILE, settings)
    return jsonify({'success': True, 'interval': CAPTURE_INTERVAL})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
