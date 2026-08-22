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

R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'my-media-downloader')

# پروکسی و PO Token ئەگەر هەبن
PROXY_URL = os.getenv('PROXY_URL')  # بۆ نموونە: http://user:pass@ip:port
PO_TOKEN = os.getenv('PO_TOKEN')    # بۆ نموونە: web+YOUR_PO_TOKEN_HERE

# ناو نیشانی Cloudflare Worker ەکەت
CF_WORKER_PROXY = "https://yt-proxy.m7f0n3.workers.dev"

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

# دڵنیا بوونەوە لە ڕێڕەوی دروستی cookies.txt
COOKIE_PATH = os.path.join(os.path.dirname(__file__), 'cookies.txt')

def get_base_ydl_opts(url=""):
    """ڕێکخستنە نوێکراوەکان بۆ تێپەڕاندنی بلۆکی یوتیوب لە ڕێگەی Cloudflare Worker"""
    yt_client_config = ['tv', 'android', 'ios', 'web']
    
    yt_extractor_args = {
        'player_client': yt_client_config
    }
    
    # ئەگەر PO Token هەبوو، زیای بکە
    if PO_TOKEN:
        yt_extractor_args['po_token'] = [PO_TOKEN]

    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        'extractor_args': {
            'youtube': yt_extractor_args
        }
    }

    # ئەگەر داواکارییەکە بۆ یوتیوب بێت، بڕوات لە ڕێگەی Worker ەکەوە
    if 'youtube.com' in url or 'youtu.be' in url:
        opts['source_address'] = '0.0.0.0'
        opts['force_generic_extractor'] = False
    
    # بەکارهێنانی فایلی cookies.txt ئەگەر لە فۆڵدەرەکەدا هەبێت
    if os.path.exists(COOKIE_PATH):
        opts['cookiefile'] = COOKIE_PATH
        
    # بەکارهێنانی پروکسی ئەگەر دیاریکرا بێت
    if PROXY_URL:
        opts['proxy'] = PROXY_URL
    
    return opts

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

    # ئامادەکردنی لینکەکە بۆ تێپەڕبوون بە Cloudflare Worker
    target_url = url
    if 'youtube.com' in url or 'youtu.be' in url:
        target_url = f"{CF_WORKER_PROXY}/?url={url}"

    ydl_opts = get_base_ydl_opts(url)

    if 'tiktok.com' in url:
        ydl_opts['extractor_args'] = {'tiktok': {'app_version': 'v2'}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            
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
        print(f"Fetch Error: {str(e)}")
        return jsonify({'error': 'Failed to fetch metadata.'}), 400

@app.route('/download', methods=['POST'])
@limiter.limit("10 per minute")
def download_video():
    data = request.json
    url = data.get('url')
    format_type = data.get('format', 'best')
    start_time = data.get('start_time')
    end_time = data.get('end_time')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    target_url = url
    if 'youtube.com' in url or 'youtu.be' in url:
        target_url = f"{CF_WORKER_PROXY}/?url={url}"

    postprocessors = []

    if format_type == 'mp3':
        format_spec = 'bestaudio/best'
        postprocessors.append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        })
    elif format_type.endswith('p'):
        height = format_type.replace('p', '')
        format_spec = f'best[height<={height}][ext=mp4]/bestvideo[height<={height}]+bestaudio/best'
    else:
        format_spec = 'best[ext=mp4]/best'

    if start_time or end_time:
        postprocessors.append({
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        })

    ydl_opts = get_base_ydl_opts(url)
    ydl_opts.update({
        'outtmpl': '/tmp/%(title)s_%(id)s.%(ext)s',
        'format': format_spec,
        'noplaylist': True,
        'postprocessors': postprocessors,
    })

    if 'tiktok.com' in url:
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['extractor_args'] = {'tiktok': {'app_version': 'v2'}}

    if start_time or end_time:
        def set_download_range(info_dict, ydl):
            return [{'start_time': float(start_time or 0), 'end_time': float(end_time or info_dict.get('duration', 0))}]
        ydl_opts['download_ranges'] = set_download_range

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            filename = ydl.prepare_filename(info)
            
            if format_type == 'mp3':
                base, _ = os.path.splitext(filename)
                filename = base + '.mp3'

            safe_title = sanitize_filename(info.get('title', 'video'))
            ext = 'mp3' if format_type == 'mp3' else 'mp4'
            file_key = f"{safe_title}.{ext}"

            s3_client.upload_file(filename, R2_BUCKET_NAME, file_key)

            if os.path.exists(filename):
                os.remove(filename)

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