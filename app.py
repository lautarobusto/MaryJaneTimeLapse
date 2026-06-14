import os
import subprocess
import glob
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_file
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
CAMERA_URL = os.environ.get('CAMERA_URL', 'rtsp://admin:batusai123@192.168.0.63:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif')
CAPTURE_INTERVAL = int(os.environ.get('CAPTURE_INTERVAL', '60'))
FRAMES_DIR = '/app/frames'
VIDEOS_DIR = '/app/videos'

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)

scheduler = BackgroundScheduler()

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
    
    # Create temp list file for ffmpeg
    list_file = f"{FRAMES_DIR}/timelapse_list_{timestamp}.txt"
    with open(list_file, 'w') as f:
        for frame in filtered:
            f.write(f"file '{os.path.abspath(frame)}'\n")
    
    try:
        result = subprocess.run([
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
            '-i', list_file,
            '-vf', 'fps=10,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-r', '30', output_file
        ], capture_output=True, text=True, timeout=300)
        
        os.remove(list_file)
        
        if result.returncode == 0:
            print(f"[OK] Created timelapse {output_file}")
            return output_file
        else:
            print(f"[ERR] ffmpeg failed: {result.stderr[:500]}")
            return None
    except Exception as e:
        print(f"[ERR] Timelapse creation failed: {e}")
        if os.path.exists(list_file):
            os.remove(list_file)
        return None

# Schedule automatic capture
scheduler.add_job(capture_frame, 'interval', seconds=CAPTURE_INTERVAL, id='capture')
scheduler.start()

@app.route('/')
def index():
    grouped = group_frames_by_day()
    videos = sorted(glob.glob(f"{VIDEOS_DIR}/timelapse_*.mp4"), reverse=True)
    
    frame_count = len(glob.glob(f"{FRAMES_DIR}/frame_*.jpg"))
    
    return render_template('index.html', 
                         grouped=grouped,
                         videos=[os.path.basename(v) for v in videos],
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
    
    output = create_timelapse(date_from, date_to)
    if output:
        return jsonify({'success': True, 'file': os.path.basename(output)})
    else:
        return jsonify({'success': False, 'error': 'Failed to create timelapse'})

@app.route('/api/frames')
def api_frames():
    grouped = group_frames_by_day()
    return jsonify({'days': list(grouped.keys()), 'frames': {k: [f['filename'] for f in v] for k, v in grouped.items()}})

@app.route('/api/videos')
def api_videos():
    videos = sorted(glob.glob(f"{VIDEOS_DIR}/timelapse_*.mp4"), reverse=True)
    return jsonify({'videos': [os.path.basename(v) for v in videos]})

@app.route('/frame/<filename>')
def serve_frame(filename):
    return send_file(f"{FRAMES_DIR}/{filename}")

@app.route('/video/<filename>')
def serve_video(filename):
    return send_file(f"{VIDEOS_DIR}/{filename}")

@app.route('/api/delete_frames', methods=['POST'])
def delete_frames():
    frames = glob.glob(f"{FRAMES_DIR}/frame_*.jpg")
    for f in frames:
        os.remove(f)
    return jsonify({'success': True, 'deleted': len(frames)})

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
