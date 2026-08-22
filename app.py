import os
import re
import yt_dlp
import boto3
from botocore.config import Config
from flask import Flask, render_template, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ڕێکخستنی زانیارییەکانی Cloudflare R2
R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'my-media-downloader')

# دروستکردنی پەیوەندی بە S3 Clientی R2
s3_client = boto3.client(
    's3',
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)

def sanitize_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title)

COOKIE_PATH = '/etc/secrets/cookies.txt'

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
        'nocheckcertificate': True,
        'geo_bypass': True,
    }

    # تەنها بۆ یوتیوب فایلی cookies بەکاربێنە ئەگەر فۆڵدەرەکە لە سەر سێرڤەر هەبوو
    if 'youtube.com' in url or 'youtu.be' in url:
        if os.path.exists(COOKIE_PATH):
            ydl_opts['cookiefile'] = COOKIE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
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
        format_spec = f'best[height<={height}][ext=mp4]/bestvideo[height<={height}]+bestaudio/best'
        postprocessors = []
    else:
        format_spec = 'best[ext=mp4]/best'
        postprocessors = []

    ydl_opts = {
        'outtmpl': '/tmp/%(title)s_%(id)s.%(ext)s',
        'format': format_spec,
        'noplaylist': True,
        'quiet': True,
        'postprocessors': postprocessors,
        'nocheckcertificate': True,
        'geo_bypass': True,
    }

    # تەنها بۆ یوتیوب فایلی cookies بەکاربێنە
    if 'youtube.com' in url or 'youtu.be' in url:
        if os.path.exists(COOKIE_PATH):
            ydl_opts['cookiefile'] = COOKIE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if format_type == 'mp3':
                base, _ = os.path.splitext(filename)
                filename = base + '.mp3'

            safe_title = sanitize_filename(info.get('title', 'video'))
            ext = 'mp3' if format_type == 'mp3' else 'mp4'
            file_key = f"{safe_title}.{ext}"

            # ١. بەرزکردنەوەی فایلەکە بۆ Cloudflare R2
            s3_client.upload_file(filename, R2_BUCKET_NAME, file_key)

            # ٢. سڕینەوەی فایلەکە لە Render بۆ هێشتنەوەی شوێنی بەتاڵ
            if os.path.exists(filename):
                os.remove(filename)

            # ٣. دروستکردنی لینکی داگرتنی کاتی (Presigned URL) بە شێوازی Force Download
            download_url = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': R2_BUCKET_NAME,
                    'Key': file_key,
                    'ResponseContentDisposition': f'attachment; filename="{file_key}"'
                },
                ExpiresIn=3600
            )

            return jsonify({'download_url': download_url})

    except Exception as e:
        print(f"Download Error: {str(e)}")
        return jsonify({'error': 'Download failed.'}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)