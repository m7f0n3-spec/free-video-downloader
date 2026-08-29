import os
import re
import base64
import json
import time
import queue
import random
import threading
import subprocess
import zipfile
import shutil
import yt_dlp
import boto3
from botocore.config import Config
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from flask_limiter import Limiter

app = Flask(__name__)

ADMIN_SECRET_KEY = os.getenv('ADMIN_SECRET_KEY', 'mysecret123')
BANNED_IPS = set(os.getenv('BANNED_IPS', '').split(',')) if os.getenv('BANNED_IPS') else set()

PROXY_URL = os.getenv('PROXY_URL')
PROXY_LIST_ENV = os.getenv('PROXY_LIST')
PROXIES = [p.strip() for p in PROXY_LIST_ENV.split(',')] if PROXY_LIST_ENV else ([PROXY_URL] if PROXY_URL else [])

def get_random_proxy():
    return random.choice(PROXIES) if PROXIES else None

def get_client_ip():
    cf_ip = request.headers.get('CF-Connecting-IP')
    if cf_ip:
        return cf_ip.strip()
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

limiter = Limiter(
    get_client_ip,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

visitor_logs = []

@app.before_request
def check_banned_ip_and_log():
    ip = get_client_ip()
    if ip in BANNED_IPS:
        return jsonify({"error": "Access denied. Your IP is blocked."}), 403

    path = request.path
    if not path.startswith('/static') and not path.startswith('/admin'):
        visitor_logs.append({
            "ip": ip,
            "path": path,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        if len(visitor_logs) > 500:
            visitor_logs.pop(0)

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'my-media-downloader').strip().strip('/')

PO_TOKEN = os.getenv('PO_TOKEN')
YOUTUBE_COOKIES_BASE64 = os.getenv('YOUTUBE_COOKIES_BASE64')
COOKIES_CONTENT = os.getenv('COOKIES_CONTENT')

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

def auto_cleanup_downloads_folder():
    while True:
        try:
            now = time.time()
            max_age_seconds = 30 * 60
            if os.path.exists(DOWNLOAD_DIR):
                for filename in os.listdir(DOWNLOAD_DIR):
                    file_path = os.path.join(DOWNLOAD_DIR, filename)
                    if os.path.isfile(file_path):
                        if now - os.path.getmtime(file_path) > max_age_seconds:
                            os.remove(file_path)
                    elif os.path.isdir(file_path):
                        if now - os.path.getmtime(file_path) > max_age_seconds:
                            shutil.rmtree(file_path, ignore_errors=True)
        except Exception as e:
            print(f"[Cleanup Error]: {e}")
        time.sleep(900)

cleanup_thread = threading.Thread(target=auto_cleanup_downloads_folder, daemon=True)
cleanup_thread.start()

s3_client = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    s3_client = boto3.client(
        's3',
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )

def sanitize_filename(title, max_length=60):
    clean_title = re.sub(r'[^\w\s-]', '', title)
    clean_title = re.sub(r'\s+', '_', clean_title).strip('_')
    if len(clean_title) > max_length:
        clean_title = clean_title[:max_length].rstrip('_')
    return clean_title if clean_title else 'video'

COOKIE_PATH = os.path.join(os.path.dirname(__file__), 'cookies.txt')

if COOKIES_CONTENT:
    try:
        with open(COOKIE_PATH, 'w', encoding='utf-8') as f:
            f.write(COOKIES_CONTENT)
    except Exception as e:
        print(f"Failed to write COOKIES_CONTENT: {e}")
elif YOUTUBE_COOKIES_BASE64:
    try:
        decoded_cookies = base64.b64decode(YOUTUBE_COOKIES_BASE64).decode('utf-8')
        with open(COOKIE_PATH, 'w', encoding='utf-8') as f:
            f.write(decoded_cookies)
    except Exception as e:
        print(f"Failed to load cookies: {e}")

def fetch_po_token():
    if PO_TOKEN:
        return PO_TOKEN
    try:
        js_script = os.path.join(os.path.dirname(__file__), 'generate-token.js')
        if os.path.exists(js_script):
            res = subprocess.run(['node', js_script], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                return data.get('poToken')
    except Exception as e:
        print(f"Error executing generate-token.js: {e}")
    return None

def get_base_ydl_opts(url=None):
    yt_client_config = ['web', 'mweb', 'android', 'ios']
    yt_extractor_args = {
        'youtube': {
            'player_client': yt_client_config,
        },
        'tiktok': {
            'app_version': '35.1.1',
            'manifest_app_version': '35.1.1',
            'download_host': 'v16-webapp-prime.tiktok.com'
        }
    }
    
    current_po_token = fetch_po_token()
    if current_po_token:
        yt_extractor_args['youtube']['po_token'] = [current_po_token]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    if url:
        if 'instagram.com' in url:
            headers['Referer'] = 'https://www.instagram.com/'
        elif 'facebook.com' in url or 'fb.watch' in url:
            headers['Referer'] = 'https://www.facebook.com/'
        elif 'tiktok.com' in url:
            headers['Referer'] = 'https://www.tiktok.com/'

    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'http_headers': headers,
        'extractor_args': yt_extractor_args,
        'js_runtimes': {'node': {}},  # ڕێکخستنی دروست بۆ نۆد لە yt-dlp
        'retries': 10,
        'fragment_retries': 10
    }
    
    if os.path.exists(COOKIE_PATH):
        opts['cookiefile'] = COOKIE_PATH

    active_proxy = get_random_proxy()
    if active_proxy:
        opts['proxy'] = active_proxy

    return opts

progress_queues = {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/youtube')
def youtube():
    return render_template('youtube.html')

@app.route('/facebook')
def facebook():
    return render_template('facebook.html')

@app.route('/instagram')
def instagram():
    return render_template('instagram.html')

@app.route('/tiktok')
def tiktok():
    return render_template('tiktok.html')

@app.route('/downloads/<path:filename>')
def serve_downloaded_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True, download_name=filename)

@app.route('/progress/<task_id>')
def progress_stream(task_id):
    def event_stream():
        q = progress_queues.get(task_id)
        if not q:
            yield f"data: {json.dumps({'percent': 100, 'status': 'done'})}\n\n"
            return
        
        while True:
            try:
                data = q.get(timeout=30)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get('status') in ['completed', 'error']:
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'percent': 0, 'status': 'keep-alive'})}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/get-info', methods=['POST'])
@limiter.limit("20 per minute")
def get_video_info():
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL is required'}), 400

    ydl_opts = get_base_ydl_opts(url)
    ydl_opts['skip_download'] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            is_playlist = 'entries' in info
            
            thumbnail_url = info.get('thumbnail', '')
            if not thumbnail_url and 'thumbnails' in info and info['thumbnails']:
                thumbnail_url = info['thumbnails'][-1].get('url', '')
            
            if not thumbnail_url and 'id' in info:
                thumbnail_url = f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"

            return jsonify({
                'title': info.get('title', 'Media'),
                'thumbnail': thumbnail_url,
                'is_playlist': is_playlist
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download', methods=['POST'])
@limiter.limit("10 per minute")
def download_video():
    data = request.json or {}
    url = data.get('url')
    format_type = data.get('format', 'best')
    start_time = data.get('start_time')
    end_time = data.get('end_time')
    task_id = data.get('task_id', str(time.time()))

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    q = queue.Queue()
    progress_queues[task_id] = q

    postprocessors = []
    
    if format_type == 'mp3':
        format_spec = 'bestaudio/best'
        postprocessors.append({'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'})
    else:
        format_spec = 'best/bestvideo+bestaudio'

    def progress_hook(d):
        if d['status'] == 'downloading':
            p = d.get('_percent_str', '0%').strip()
            p_clean = re.sub(r'\x1b\[[0-9;]*m', '', p).replace('%', '')
            try:
                percent_val = float(p_clean)
            except:
                percent_val = 0
            q.put({'percent': percent_val, 'status': 'downloading'})
        elif d['status'] == 'finished':
            q.put({'percent': 95, 'status': 'uploading'})

    task_download_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(task_download_dir, exist_ok=True)

    ydl_opts = get_base_ydl_opts(url)
    ydl_opts.update({
        'outtmpl': os.path.join(task_download_dir, '%(title)s_%(id)s.%(ext)s'),
        'format': format_spec,
        'merge_output_format': 'mp4' if format_type != 'mp3' else None,
        'postprocessors': postprocessors,
        'progress_hooks': [progress_hook]
    })

    if start_time or end_time:
        try:
            s_val = float(start_time) if start_time else 0.0
            e_val = float(end_time) if end_time else 999999.0
            ydl_opts['download_ranges'] = yt_dlp.utils.download_range_func(None, [(s_val, e_val)])
        except Exception as e:
            print(f"Range Error: {e}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_files = os.listdir(task_download_dir)
            if not downloaded_files:
                raise Exception("No file downloaded")
            
            single_file = os.path.join(task_download_dir, downloaded_files[0])
            safe_title = sanitize_filename(info.get('title', 'video'))
            ext = 'mp3' if format_type == 'mp3' else single_file.split('.')[-1]
            file_key = f"{safe_title}_{info.get('id', 'media')}.{ext}"
            final_file_path = os.path.join(DOWNLOAD_DIR, file_key)
            os.rename(single_file, final_file_path)
            shutil.rmtree(task_download_dir, ignore_errors=True)

            q.put({'percent': 100, 'status': 'completed'})
            return jsonify({'download_url': f"/downloads/{file_key}"})
    except Exception as e:
        q.put({'percent': 0, 'status': 'error', 'error': str(e)})
        return jsonify({'error': str(e)}), 500
    finally:
        if task_id in progress_queues:
            del progress_queues[task_id]

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)