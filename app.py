import os
import time
import glob
import re
from flask import Flask, render_template, request, send_file, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def cleanup_old_files():
    now = time.time()
    for filepath in glob.glob(os.path.join(DOWNLOAD_FOLDER, '*')):
        if os.path.isfile(filepath):
            if now - os.path.getmtime(filepath) > 600:
                try:
                    os.remove(filepath)
                except Exception as e:
                    print(f"Error deleting file {filepath}: {e}")

def sanitize_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title)

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

@app.route('/get-info', methods=['POST'])
@limiter.limit("20 per minute")
def get_video_info():
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # دەرهێنانی کوالێتییە بەردەستەکان (Dynamic Quality Options)
            formats_available = []
            if 'formats' in info:
                seen_heights = set()
                for fmt in info['formats']:
                    height = fmt.get('height')
                    if height and height not in seen_heights and height >= 240:
                        seen_heights.add(height)
                        formats_available.append({
                            'format_id': f"{height}p",
                            'label': f"{height}p"
                        })
                # ڕێکخستن لە بەرزترینەوە بۆ نزمترین
                formats_available.sort(key=lambda x: int(x['label'].replace('p', '')), reverse=True)

            return jsonify({
                'title': info.get('title', 'Video'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration_string', 'N/A'),
                'uploader': info.get('uploader', info.get('extractor_key', 'Unknown')),
                'qualities': formats_available
            })
    except Exception as e:
        return jsonify({'error': 'Failed to fetch metadata. Invalid URL or private video.'}), 400

@app.route('/download', methods=['POST'])
@limiter.limit("10 per minute")
def download_video():
    cleanup_old_files()
    
    data = request.json
    url = data.get('url')
    format_type = data.get('format', 'best')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    if format_type == 'mp3':
        format_spec = 'bestaudio/best'
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif format_type.endswith('p'):
        height = format_type.replace('p', '')
        format_spec = f'best[height<={height}]/best'
        postprocessors = []
    else:
        format_spec = 'best'
        postprocessors = []

    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s_%(id)s.%(ext)s'),
        'format': format_spec,
        'noplaylist': True,
        'quiet': True,
        'postprocessors': postprocessors
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if format_type == 'mp3':
                base, _ = os.path.splitext(filename)
                filename = base + '.mp3'

            safe_title = sanitize_filename(info.get('title', 'video'))
            ext = 'mp3' if format_type == 'mp3' else 'mp4'
            download_name = f"{safe_title}.{ext}"

        return send_file(filename, as_attachment=True, download_name=download_name)
    except Exception as e:
        print(f"Download Error: {str(e)}")
        return jsonify({'error': 'Download failed.'}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)