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

# --- وەرگرتنی کلیدی نهێنی بۆ پاراستنی لاپەڕەی ئەدمین ---
ADMIN_SECRET_KEY = os.getenv('ADMIN_SECRET_KEY', 'mysecret123')

# --- لیستی IPیە بلۆککراوەکان (IP Blacklist) ---
BANNED_IPS = set(os.getenv('BANNED_IPS', '').split(',')) if os.getenv('BANNED_IPS') else set()

# --- لیستی پراکسی بۆ Rotation ---
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

# --- API Rate Limiting ---
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
    """سڕینەوەی فایلی کۆن بۆ پاراستنی هارد و بیرگەی سێرڤەر"""
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
    yt_client_config = ['tv', 'android', 'ios', 'mweb']
    yt_extractor_args = {
        'youtube': {'player_client': yt_client_config},
        'tiktok': {
            'app_version': '34.0.0',
            'manifest_app_version': '34.0.0',
            'download_host': 'v16-webapp-prime.tiktok.com'
        }
    }
    
    current_po_token = fetch_po_token()
    if current_po_token:
        yt_extractor_args['youtube']['po_token'] = [current_po_token]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
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
        'retries': 5, # Error Handling: دووبارەکردنەوەی ئۆتۆماتیکی لە کاتی پچڕان
        'fragment_retries': 5
    }
    
    if os.path.exists(COOKIE_PATH):
        opts['cookiefile'] = COOKIE_PATH

    active_proxy = get_random_proxy()
    if active_proxy:
        opts['proxy'] = active_proxy

    return opts

progress_queues = {}

# --- PWA Service Worker & Manifest Endpoints ---
@app.route('/service-worker.js')
def service_worker():
    return send_from_directory('static', 'service-worker.js')

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "uptime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "storage_ok": os.path.exists(DOWNLOAD_DIR)
    }), 200

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

@app.route('/admin/ips')
def view_admin_ips():
    key = request.args.get('key')
    if key != ADMIN_SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({
        "total_requests": len(visitor_logs),
        "banned_ips": list(BANNED_IPS),
        "logs": visitor_logs
    })

@app.route('/admin/ban', methods=['POST'])
def ban_ip():
    data = request.json or {}
    key = data.get('key')
    ip_to_ban = data.get('ip')
    if key != ADMIN_SECRET_KEY or not ip_to_ban:
        return jsonify({"error": "Unauthorized or missing IP"}), 403
    
    BANNED_IPS.add(ip_to_ban.strip())
    return jsonify({"success": True, "banned": ip_to_ban})

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

    # Error Handling validation
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            is_playlist = 'entries' in info
            playlist_count = len(info.get('entries', [])) if is_playlist else 0

            duration_sec = info.get('duration')
            if duration_sec:
                mins, secs = divmod(int(duration_sec), 60)
                hrs, mins = divmod(mins, 60)
                duration_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"
            else:
                duration_str = info.get('duration_string', 'N/A')

            formats_available = []
            if not is_playlist and 'formats' in info:
                seen_heights = set()
                for fmt in info['formats']:
                    height = fmt.get('height')
                    filesize = fmt.get('filesize') or fmt.get('filesize_approx')
                    size_mb = round(filesize / (1024 * 1024), 1) if filesize else None
                    size_str = f" ({size_mb} MB)" if size_mb else ""
                    
                    if height and height not in seen_heights and height >= 240:
                        seen_heights.add(height)
                        formats_available.append({
                            'format_id': f"{height}p",
                            'label': f"{height}p{size_str}"
                        })
                formats_available.sort(key=lambda x: int(x['label'].split('p')[0]), reverse=True)

            return jsonify({
                'title': info.get('title', 'Media'),
                'thumbnail': info.get('thumbnail', '') if not is_playlist else (info['entries'][0].get('thumbnail') if info['entries'] else ''),
                'duration': duration_str,
                'uploader': info.get('uploader', info.get('extractor_key', 'Unknown')),
                'qualities': formats_available,
                'is_playlist': is_playlist,
                'playlist_count': playlist_count
            })
    except yt_dlp.utils.DownloadError as de:
        print(f"YTDLP DownloadError: {str(de)}")
        return jsonify({'error': 'Invalid URL or video is private/unsupported.'}), 400
    except Exception as e:
        print(f"Fetch Error: {str(e)}")
        return jsonify({'error': 'Failed to fetch video information from server.'}), 500

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
        format_spec = 'ba/b'
        postprocessors.append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        })
    elif format_type.endswith('p'):
        height = format_type.replace('p', '')
        format_spec = f'b[height<={height}]/bv[height<={height}]+ba/b'
    else:
        format_spec = 'b/best'

    if start_time or end_time:
        postprocessors.append({
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        })

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
    if not os.path.exists(task_download_dir):
        os.makedirs(task_download_dir)

    ydl_opts = get_base_ydl_opts(url)
    ydl_opts.update({
        'outtmpl': os.path.join(task_download_dir, '%(title)s_%(id)s.%(ext)s'),
        'format': format_spec,
        'postprocessors': postprocessors,
        'progress_hooks': [progress_hook]
    })

    if start_time or end_time:
        def set_download_range(info_dict, ydl):
            return [{'start_time': float(start_time or 0), 'end_time': float(end_time or info_dict.get('duration', 0))}]
        ydl_opts['download_ranges'] = set_download_range

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            is_playlist = 'entries' in info
            
            if is_playlist:
                zip_filename = f"{sanitize_filename(info.get('title', 'playlist'))}_{task_id}.zip"
                zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)

                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(task_download_dir):
                        for file in files:
                            zipf.write(os.path.join(root, file), file)
                
                final_file_path = zip_path
                file_key = zip_filename
            else:
                downloaded_files = os.listdir(task_download_dir)
                if not downloaded_files:
                    raise Exception("No file downloaded by core engine")
                
                single_file = os.path.join(task_download_dir, downloaded_files[0])
                safe_title = sanitize_filename(info.get('title', 'video'))
                ext = 'mp3' if format_type == 'mp3' else single_file.split('.')[-1]
                file_key = f"{safe_title}_{info.get('id', 'media')}.{ext}"
                final_file_path = os.path.join(DOWNLOAD_DIR, file_key)
                os.rename(single_file, final_file_path)

            shutil.rmtree(task_download_dir, ignore_errors=True)

            if s3_client:
                s3_client.upload_file(
                    Filename=final_file_path,
                    Bucket=R2_BUCKET_NAME,
                    Key=file_key
                )
                download_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={
                        'Bucket': R2_BUCKET_NAME,
                        'Key': file_key,
                        'ResponseContentDisposition': f'attachment; filename="{file_key}"'
                    },
                    ExpiresIn=3600
                )
                q.put({'percent': 100, 'status': 'completed'})
                return jsonify({'download_url': download_url})
            else:
                q.put({'percent': 100, 'status': 'completed'})
                return jsonify({'download_url': f"/downloads/{os.path.basename(final_file_path)}"})

    except Exception as e:
        print(f"Download Error Trace: {str(e)}")
        q.put({'percent': 0, 'status': 'error', 'error': str(e)})
        return jsonify({'error': str(e)}), 500

    finally:
        if task_id in progress_queues:
            del progress_queues[task_id]

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)