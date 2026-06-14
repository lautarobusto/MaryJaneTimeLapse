import os
import subprocess
import glob
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
CAMERA_URL = os.environ.get('CAMERA_URL', 'rtsp://admin:batusai123@192.168.0.63:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif')
CAPTURE_INTERVAL = int(os.environ.get('CAPTURE_INTERVAL', '60'))  # seconds
FRAMES_DIR = '/app/frames'
VIDEOS_DIR = '/app/videos'

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)

scheduler = BackgroundScheduler()

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

def create_timelapse():
    """Create a timelapse video from captured frames"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = f"{VIDEOS_DIR}/timelapse_{timestamp}.mp4"
    
    # Get list of frames sorted by time
    frames = sorted(glob.glob(f"{FRAMES_DIR}/frame_*.jpg"))
    
    if len(frames) < 2:
        print("[ERR] Not enough frames for timelapse")
        return None
    
    try:
        # Create timelapse with ffmpeg
        result = subprocess.run([
            'ffmpeg', '-y', '-framerate', '10',
            '-pattern_type', 'glob', '-i', f'{FRAMES_DIR}/frame_*.jpg',
            '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-r', '30', output_file
        ], capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            print(f"[OK] Created timelapse {output_file}")
            return output_file
        else:
            print(f"[ERR] ffmpeg failed: {result.stderr[:500]}")
            return None
    except Exception as e:
        print(f"[ERR] Timelapse creation failed: {e}")
        return None

# Schedule automatic capture
scheduler.add_job(capture_frame, 'interval', seconds=CAPTURE_INTERVAL, id='capture')
scheduler.start()

@app.route('/')
def index():
    frames = sorted(glob.glob(f"{FRAMES_DIR}/frame_*.jpg"), reverse=True)[:50]
    videos = sorted(glob.glob(f"{VIDEOS_DIR}/timelapse_*.mp4"), reverse=True)
    
    frame_count = len(glob.glob(f"{FRAMES_DIR}/frame_*.jpg"))
    
    return render_template('index.html', 
                         frames=[os.path.basename(f) for f in frames],
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
    output = create_timelapse()
    if output:
        return jsonify({'success': True, 'file': os.path.basename(output)})
    else:
        return jsonify({'success': False, 'error': 'Failed to create timelapse'})

@app.route('/api/frames')
def api_frames():
    frames = sorted(glob.glob(f"{FRAMES_DIR}/frame_*.jpg"), reverse=True)
    return jsonify({'frames': [os.path.basename(f) for f in frames]})

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
        # Reschedule job
        scheduler.reschedule_job('capture', trigger='interval', seconds=CAPTURE_INTERVAL)
    return jsonify({'success': True, 'interval': CAPTURE_INTERVAL})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
