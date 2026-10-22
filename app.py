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
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, render_template, jsonify, request, send_file, Response, abort
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
CAMERA_URL = os.environ.get('CAMERA_URL', '')
TIMELAPSE_FRAME_DURATION = float(os.environ.get('TIMELAPSE_FRAME_DURATION', '0.1'))
BOX_REMOTE = os.environ.get('BOX_REMOTE', 'box:MaryJaneTimeLapse').rstrip('/')
UPLOAD_INTERVAL = int(os.environ.get('UPLOAD_INTERVAL', '600'))
LOCAL_RETENTION_HOURS = float(os.environ.get('LOCAL_RETENTION_HOURS', '48'))
MIN_CAPTURE_INTERVAL = 60

# Storage layout:
#   SPOOL_DIR (tmpfs/RAM): the last LOCAL_RETENTION_HOURS of frames, so short timelapses
#     need no download. spool/pending holds hardlinks of frames not yet uploaded to Box.
#   WORK_DIR  (tmpfs/RAM): scratch space for building timelapses
#   DATA_DIR  (disk): small settings/index files only
#   Box: {BOX_REMOTE}/frames/YYYY-MM-DD/frame_*.jpg, thumbs/ (same layout, 320px), videos/
SPOOL_DIR = '/app/spool'
WORK_DIR = '/app/work'
DATA_DIR = '/app/data'
SPOOL_FRAMES = f"{SPOOL_DIR}/frames"
SPOOL_THUMBS = f"{SPOOL_DIR}/thumbs"
SPOOL_TMP = f"{SPOOL_DIR}/tmp"
SPOOL_PENDING = f"{SPOOL_DIR}/pending"
SETTINGS_FILE = f"{DATA_DIR}/settings.json"
VIDEOS_INDEX = f"{DATA_DIR}/videos.json"

for d in (SPOOL_FRAMES, SPOOL_THUMBS, SPOOL_TMP, SPOOL_PENDING, WORK_DIR, DATA_DIR):
    os.makedirs(d, exist_ok=True)

FRAME_RE = re.compile(r'^frame_(\d{8})_(\d{6})\.jpg$')
VIDEO_RE = re.compile(r'^timelapse_\d{8}_\d{6}(_thumb\.jpg|\.mp4)$')


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
remote_videos = RemoteCache(lambda: _list_remote('videos'), ttl=300)
remote_about = RemoteCache(lambda: json.loads(rclone('about', 'box:', '--json', timeout=60).stdout or '{}'), ttl=600)


# --- frames -----------------------------------------------------------------

def parse_frame_date(filename):
    """Extract date from frame_YYYYMMDD_HHMMSS.jpg"""
    match = FRAME_RE.match(filename)
    if match:
        return datetime.strptime(match.group(1) + match.group(2), '%Y%m%d%H%M%S')
    return None


def all_frames():
    """Return {filename: datetime} for frames in Box plus frames still in the spool."""
    frames = {}
    for rel in remote_frames.get():
        name = rel.rsplit('/', 1)[-1]
        dt = parse_frame_date(name)
        if dt:
            frames[name] = dt
    for _, _, files in os.walk(SPOOL_FRAMES):
        for name in files:
            dt = parse_frame_date(name)
            if dt:
                frames[name] = dt
    return frames


def group_frames_by_day():
    """Group frames by day, sorted by time"""
    grouped = {}
    for name, dt in all_frames().items():
        grouped.setdefault(dt.strftime('%Y-%m-%d'), []).append({
            'filename': name,
            'datetime': dt,
            'time': dt.strftime('%H:%M:%S')
        })
    for day in grouped:
        grouped[day].sort(key=lambda x: x['datetime'], reverse=True)
    return dict(sorted(grouped.items(), reverse=True))


def capture_frame():
    """Capture a frame (plus a small thumbnail) from the camera into the RAM spool"""
    now = datetime.now()
    name = f"frame_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
    day = now.strftime('%Y-%m-%d')
    tmp_frame = f"{SPOOL_TMP}/{name}"
    tmp_thumb = f"{SPOOL_TMP}/thumb_{name}"

    try:
        result = subprocess.run([
            'ffmpeg', '-y', '-rtsp_transport', 'tcp',
            '-i', CAMERA_URL,
            '-frames:v', '1', '-q:v', '2', tmp_frame,
            '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '5', tmp_thumb
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and os.path.getsize(tmp_frame) > 0:
            # Move into place atomically so the uploader never sees partial files,
            # and queue a hardlink for the next batch upload
            for src, base, kind in ((tmp_thumb, SPOOL_THUMBS, 'thumbs'), (tmp_frame, SPOOL_FRAMES, 'frames')):
                os.makedirs(f"{base}/{day}", exist_ok=True)
                os.makedirs(f"{SPOOL_PENDING}/{kind}/{day}", exist_ok=True)
                os.replace(src, f"{base}/{day}/{name}")
                os.link(f"{base}/{day}/{name}", f"{SPOOL_PENDING}/{kind}/{day}/{name}")
            print(f"[OK] Captured {name}")
            return True
        print(f"[ERR] ffmpeg failed: {result.stderr[-200:]}")
        return False
    except Exception as e:
        print(f"[ERR] Capture failed: {e}")
        return False
    finally:
        for f in (tmp_frame, tmp_thumb):
            if os.path.exists(f):
                os.remove(f)


def remove_empty_day_dirs(base):
    # Skip today so we don't race a capture creating its directory
    today = datetime.now().strftime('%Y-%m-%d')
    for day in os.listdir(base):
        path = f"{base}/{day}"
        if day != today and os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)


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
        remove_empty_day_dirs(pending)
    if moved:
        remote_frames.invalidate()
    prune_local()


def prune_local():
    """Drop local frames older than LOCAL_RETENTION_HOURS that are already in Box"""
    cutoff = datetime.now().timestamp() - LOCAL_RETENTION_HOURS * 3600
    removed = 0
    for base, kind in ((SPOOL_FRAMES, 'frames'), (SPOOL_THUMBS, 'thumbs')):
        for day in os.listdir(base):
            for name in os.listdir(f"{base}/{day}"):
                dt = parse_frame_date(name)
                if dt and dt.timestamp() < cutoff and not os.path.exists(f"{SPOOL_PENDING}/{kind}/{day}/{name}"):
                    os.remove(f"{base}/{day}/{name}")
                    removed += 1
        remove_empty_day_dirs(base)
    if removed:
        print(f"[OK] Pruned {removed} local files older than {LOCAL_RETENTION_HOURS:g}h")


def spool_path(base, filename):
    dt = parse_frame_date(filename)
    if not dt:
        abort(404)
    return dt.strftime('%Y-%m-%d'), f"{base}/{dt.strftime('%Y-%m-%d')}/{filename}"


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
    names = set(remote_videos.get())
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


def create_timelapse(job_id, date_from=None, date_to=None, look=DEFAULT_LOOK):
    """Download the frames in range from Box to RAM, render, and upload the video to Box"""
    def status(msg):
        jobs[job_id]['progress'] = msg

    selected = []
    for name, dt in all_frames().items():
        if date_from and dt.date() < date_from:
            continue
        if date_to and dt.date() > date_to:
            continue
        selected.append((dt, name))

    if len(selected) < 2:
        print(f"[ERR] Not enough frames for timelapse (found {len(selected)})")
        raise ValueError('No hay suficientes frames')
    selected.sort()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    job_dir = f"{WORK_DIR}/{job_id}"
    src_dir = f"{job_dir}/src"
    seq_dir = f"{job_dir}/seq"
    os.makedirs(src_dir)
    os.makedirs(seq_dir)

    try:
        # Frames kept locally (last LOCAL_RETENTION_HOURS) are copied; the rest come from Box
        to_download = []
        for dt, name in selected:
            day, local = spool_path(SPOOL_FRAMES, name)
            if os.path.exists(local):
                os.makedirs(f"{src_dir}/{day}", exist_ok=True)
                shutil.copy(local, f"{src_dir}/{day}/{name}")
            else:
                to_download.append(f"{day}/{name}")

        if to_download:
            status(f'Descargando {len(to_download)} frames de Box...')
            list_file = f"{job_dir}/files.txt"
            with open(list_file, 'w') as f:
                f.write('\n'.join(to_download))
            result = rclone('copy', f"{BOX_REMOTE}/frames", src_dir, '--files-from', list_file,
                            '--transfers', '16', '--checkers', '16', timeout=3600)
            if result.returncode != 0:
                raise RuntimeError(f"Descarga de Box falló: {result.stderr[-200:]}")

        i = 0
        for dt, name in selected:
            path = f"{src_dir}/{dt.strftime('%Y-%m-%d')}/{name}"
            if os.path.exists(path) and os.path.getsize(path) > 0:
                i += 1
                os.symlink(path, f"{seq_dir}/frame_{i:06d}.jpg")
        if i < 2:
            raise ValueError('No hay suficientes frames')

        status(f'Generando video con {i} frames...')
        output_file = f"{job_dir}/timelapse_{timestamp}.mp4"
        result = subprocess.run([
            'ffmpeg', '-y', '-framerate', str(1 / TIMELAPSE_FRAME_DURATION),
            '-i', f"{seq_dir}/frame_%06d.jpg",
            '-vf', LOOKS.get(look, LOOKS[DEFAULT_LOOK]),
            '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', output_file
        ], capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            print(f"[ERR] ffmpeg failed: {result.stderr[-500:]}")
            raise RuntimeError('ffmpeg falló al generar el video')

        thumb_path = output_file.replace('.mp4', '_thumb.jpg')
        generate_thumb(output_file, thumb_path)
        duration = get_video_duration(output_file)
        shutil.rmtree(src_dir, ignore_errors=True)

        status('Subiendo video a Box...')
        for f in (output_file, thumb_path):
            if os.path.exists(f):
                result = rclone('copy', f, f"{BOX_REMOTE}/videos", timeout=3600)
                if result.returncode != 0:
                    raise RuntimeError(f"Subida a Box falló: {result.stderr[-200:]}")

        with videos_lock:
            index = load_json(VIDEOS_INDEX, {})
            index[os.path.basename(output_file)] = duration
            save_json(VIDEOS_INDEX, index)
        remote_videos.invalidate()
        print(f"[OK] Created timelapse {os.path.basename(output_file)}")
        return os.path.basename(output_file)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# Schedule automatic capture and upload
scheduler.add_job(capture_frame, 'interval', seconds=CAPTURE_INTERVAL, id='capture')
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
    grouped = group_frames_by_day()
    return render_template('index.html',
                           grouped=grouped,
                           videos=list_videos(),
                           frame_count=sum(len(v) for v in grouped.values()),
                           interval=CAPTURE_INTERVAL,
                           camera_url=urlparse(CAMERA_URL).hostname or '')


@app.route('/api/capture', methods=['POST'])
def api_capture():
    success = capture_frame()
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

    look = data.get('look') or DEFAULT_LOOK
    if look not in LOOKS:
        return jsonify({'success': False, 'error': f'look debe ser uno de: {", ".join(LOOKS)}'}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'processing', 'file': None, 'error': None, 'progress': 'Preparando...'}

    def run():
        try:
            output = create_timelapse(job_id, date_from, date_to, look)
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
    grouped = group_frames_by_day()
    return jsonify({'days': list(grouped.keys()), 'frames': {k: [f['filename'] for f in v] for k, v in grouped.items()}})


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
    thumb = request.args.get('thumb') == '1'
    base, remote = (SPOOL_THUMBS, 'thumbs') if thumb else (SPOOL_FRAMES, 'frames')
    day, local = spool_path(base, filename)
    if os.path.exists(local):
        return send_file(local)
    return stream_remote(f"{remote}/{day}/{filename}", 'image/jpeg')


@app.route('/video/<filename>')
def serve_video(filename):
    if not VIDEO_RE.match(filename):
        abort(404)
    return stream_remote(f"videos/{filename}", 'video/mp4', download_name=filename)


@app.route('/thumb/<filename>')
def serve_thumb(filename):
    if not VIDEO_RE.match(filename):
        abort(404)
    return stream_remote(f"videos/{filename}", 'image/jpeg')


@app.route('/api/delete_frames', methods=['POST'])
def delete_frames():
    count = len(all_frames())
    for path in ('frames', 'thumbs'):
        rclone('purge', f"{BOX_REMOTE}/{path}", timeout=1800)
    for d in (SPOOL_FRAMES, SPOOL_THUMBS, SPOOL_PENDING):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    remote_frames.invalidate()
    return jsonify({'success': True, 'deleted': count})


@app.route('/api/delete_videos', methods=['POST'])
def delete_videos():
    deleted = len(remote_videos.get())
    rclone('purge', f"{BOX_REMOTE}/videos", timeout=1800)
    with videos_lock:
        save_json(VIDEOS_INDEX, {})
    remote_videos.invalidate()
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
