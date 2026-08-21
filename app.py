import os
import time
import glob
from flask import Flask, render_template, request, send_file, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)

# 1. Rate Limiter (سنووردارکردنی داواکارییەکان بۆ ڕێگیری لە فۆڕمسپام)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# 2. پاککردنەوەی خۆکارانەی فایلە کۆنەکان (زیاتر لە ١٠ خولەک)
def cleanup_old_files():
    now = time.time()
    for filepath in glob.glob(os.path.join(DOWNLOAD_FOLDER, '*')):
        if os.path.isfile(filepath):
            if now - os.path.getmtime(filepath) > 600:  # 600 چرکە = 10 خولەک
                try:
                    os.remove(filepath)
                except Exception as e:
                    print(f"Error deleting file {filepath}: {e}")

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

# 3. هێنانی زانیاری ڤیدیۆ پێش داگرتن (Info / Preview)
@app.route('/get-info', methods=['POST'])
@limiter.limit("15 per minute")
def get_video_info():
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'لینکەکە بەتاڵە'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                'title': info.get('title', 'Video'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration_string', ''),
                'uploader': info.get('uploader', 'Unknown')
            })
    except Exception as e:
        return jsonify({'error': 'نەتوانرا زانیاری ڤیدیۆکە بهێنرێت. تکایە لە دروستی لینکەکە دڵنیا ببنەوە.'}), 400

# 4. داگرتنی ڤیدیۆ/دەنگ بەپێی کوالێتی
@app.route('/download', methods=['POST'])
@limiter.limit("10 per minute")
def download_video():
    cleanup_old_files()  # ئەنجامدانی پاککردنەوە لە کاتی هەر داواکارییەکی نوێدا
    
    data = request.json
    url = data.get('url')
    format_type = data.get('format', 'best')  # best, 1080p, 720p, 480p, mp3

    if not url:
        return jsonify({'error': 'لینکەکە بەتاڵە'}), 400

    # ڕێکخستنی شێوازی داگرتن بۆ گونجان لەگەڵ PythonAnywhere (بێ پێویستی بە FFmpeg)
    if format_type == 'mp3':
        format_spec = 'bestaudio/best'
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif format_type == '1080p':
        format_spec = 'best[height<=1080]/best'
        postprocessors = []
    elif format_type == '720p':
        format_spec = 'best[height<=720]/best'
        postprocessors = []
    elif format_type == '480p':
        format_spec = 'best[height<=480]/best'
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
            
            # ئەگەر جۆری mp3 هەڵبژێردرابوو، پاشگرەکە بگۆڕە
            if format_type == 'mp3':
                base, _ = os.path.splitext(filename)
                filename = base + '.mp3'

        return send_file(filename, as_attachment=True)
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)