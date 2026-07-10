import os
import subprocess
import glob
import re
import uuid
import threading
import shutil
import tempfile
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_file
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
CAMERA_URL = os.environ.get('CAMERA_URL', 'rtsp://admin:batusai123@192.168.0.63:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif')
CAPTURE_INTERVAL = int(os.environ.get('CAPTURE_INTERVAL', '60'))
TIMELAPSE_FRAME_DURATION = float(os.environ.get('TIMELAPSE_FRAME_DURATION', '0.1'))
FRAMES_DIR = '/app/frames'
VIDEOS_DIR = '/app/videos'

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)

scheduler = BackgroundScheduler()
jobs = {}

def parse_frame_date(filename):
    """Extract date from frame_YYYYMMDD_HHMMSS.jpg"""
    match = re.search(r'frame_(\d{8})_(\d{6})', filename)
    if match:
        date_str = match.group(1)
        time_str = match.group(2)
        return datetime.strptime(date_str + time_str, '%Y%m%d%H%M%S')
    return None

def group_frames_by_day():
    """Group frames by day, sorted by time"""
    frames = glob.glob(f"{FRAMES_DIR}/frame_*.jpg")
    grouped = {}
    
    for f in frames:
        dt = parse_frame_date(os.path.basename(f))
        if dt:
            day_key = dt.strftime('%Y-%m-%d')
            if day_key not in grouped:
                grouped[day_key] = []
            grouped[day_key].append({
                'filename': os.path.basename(f),
                'datetime': dt,
                'time': dt.strftime('%H:%M:%S')
            })
    
    # Sort each day's frames by time
    for day in grouped:
        grouped[day].sort(key=lambda x: x['datetime'], reverse=True)
    
    # Sort days descending
    return dict(sorted(grouped.items(), reverse=True))

def capture_frame():
    """Capture a single frame from the camera"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{FRAMES_DIR}/frame_{timestamp}.jpg"
    
    try:
        result = subprocess.run([
            'ffmpeg', '-y', '-rtsp_transport', 'tcp',
            '-i', CAMERA_URL,
            '-vframes', '1', '-q:v', '2',
            filename
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            print(f"[OK] Captured {filename}")
            return True
        else:
            print(f"[ERR] ffmpeg failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        print(f"[ERR] Capture failed: {e}")
        return False

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
    seek_time = max(1, dur / 2) if dur else 5
    try:
        subprocess.run([
            'ffmpeg', '-y', '-ss', str(seek_time), '-i', filepath,
            '-vframes', '1', '-q:v', '3', '-vf', 'scale=480:-1',
            thumb_path
        ], capture_output=True, text=True, timeout=30)
    except Exception:
        pass

def create_timelapse(date_from=None, date_to=None):
    """Create timelapse from frames in date range"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = f"{VIDEOS_DIR}/timelapse_{timestamp}.mp4"
    
    frames = glob.glob(f"{FRAMES_DIR}/frame_*.jpg")
    
    # Filter by date range if specified
    filtered = []
    for f in frames:
        dt = parse_frame_date(os.path.basename(f))
        if dt:
            if os.path.getsize(f) == 0:
                continue
            if date_from and dt.date() < date_from:
                continue
            if date_to and dt.date() > date_to:
                continue
            filtered.append(f)
    
    if len(filtered) < 2:
        print(f"[ERR] Not enough frames for timelapse (found {len(filtered)})")
        return None
    
    # Sort by time
    filtered.sort()
    
    temp_dir = tempfile.mkdtemp(prefix=f"timelapse_{timestamp}_", dir=FRAMES_DIR)
    for i, frame in enumerate(filtered, start=1):
        os.symlink(os.path.abspath(frame), os.path.join(temp_dir, f"frame_{i:06d}.jpg"))
    
    try:
        result = subprocess.run([
            'ffmpeg', '-y', '-framerate', str(1 / TIMELAPSE_FRAME_DURATION),
            '-i', f"{temp_dir}/frame_%06d.jpg",
            '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-r', '30', output_file
        ], capture_output=True, text=True, timeout=900)
        
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        if result.returncode == 0:
            print(f"[OK] Created timelapse {output_file}")
            # Generate thumbnail
            thumb_path = output_file.replace('.mp4', '_thumb.jpg')
            generate_thumb(output_file, thumb_path)
            return output_file
        else:
            print(f"[ERR] ffmpeg failed: {result.stderr[:500]}")
            return None
    except Exception as e:
        print(f"[ERR] Timelapse creation failed: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

# Schedule automatic capture
scheduler.add_job(capture_frame, 'interval', seconds=CAPTURE_INTERVAL, id='capture')
scheduler.start()

@app.route('/')
def index():
    grouped = group_frames_by_day()
    frame_count = len(glob.glob(f"{FRAMES_DIR}/frame_*.jpg"))
    
    videos_glob = sorted(glob.glob(f"{VIDEOS_DIR}/timelapse_*.mp4"), reverse=True)
    video_list = []
    for v in videos_glob:
        basename = os.path.basename(v)
        duration = get_video_duration(v)
        has_thumb = os.path.exists(v.replace('.mp4', '_thumb.jpg'))
        video_list.append({
            'filename': basename,
            'duration': format_duration(duration),
            'has_thumb': has_thumb
        })
    
    return render_template('index.html', 
                         grouped=grouped,
                         videos=video_list,
                         frame_count=frame_count,
                         interval=CAPTURE_INTERVAL,
                         camera_url=CAMERA_URL)

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
    
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'processing', 'file': None, 'error': None}
    
    def run():
        try:
            output = create_timelapse(date_from, date_to)
            if output:
                jobs[job_id] = {'status': 'done', 'file': os.path.basename(output), 'error': None}
            else:
                jobs[job_id] = {'status': 'error', 'file': None, 'error': 'No hay suficientes frames'}
        except Exception as e:
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

def format_duration(seconds):
    if seconds is None:
        return 'N/A'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'

@app.route('/api/videos')
def api_videos():
    videos = sorted(glob.glob(f"{VIDEOS_DIR}/timelapse_*.mp4"), reverse=True)
    result = []
    for v in videos:
        basename = os.path.basename(v)
        duration = get_video_duration(v)
        has_thumb = os.path.exists(v.replace('.mp4', '_thumb.jpg'))
        result.append({
            'filename': basename,
            'duration': format_duration(duration),
            'has_thumb': has_thumb
        })
    return jsonify({'videos': result})

@app.route('/frame/<filename>')
def serve_frame(filename):
    return send_file(f"{FRAMES_DIR}/{filename}")

@app.route('/video/<filename>')
def serve_video(filename):
    return send_file(f"{VIDEOS_DIR}/{filename}", as_attachment=True)

@app.route('/thumb/<filename>')
def serve_thumb(filename):
    thumb_path = f"{VIDEOS_DIR}/{filename}"
    if os.path.exists(thumb_path):
        return send_file(thumb_path)
    return ('', 404)

@app.route('/api/delete_frames', methods=['POST'])
def delete_frames():
    frames = glob.glob(f"{FRAMES_DIR}/frame_*.jpg")
    for f in frames:
        os.remove(f)
    return jsonify({'success': True, 'deleted': len(frames)})

@app.route('/api/delete_videos', methods=['POST'])
def delete_videos():
    files = glob.glob(f"{VIDEOS_DIR}/timelapse_*.mp4") + glob.glob(f"{VIDEOS_DIR}/timelapse_*_thumb.jpg")
    deleted = 0
    for f in files:
        if os.path.exists(f):
            os.remove(f)
            deleted += 1
    return jsonify({'success': True, 'deleted': deleted})

@app.route('/api/config', methods=['POST'])
def update_config():
    global CAPTURE_INTERVAL
    data = request.json
    if 'interval' in data:
        CAPTURE_INTERVAL = int(data['interval'])
        scheduler.reschedule_job('capture', trigger='interval', seconds=CAPTURE_INTERVAL)
    return jsonify({'success': True, 'interval': CAPTURE_INTERVAL})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
