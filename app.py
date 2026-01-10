import os
import io
import json
import zipfile
import logging
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, send_file, render_template
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import humanize
from youtubesearchpython import VideosSearch
import yt_dlp

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.config.from_mapping(
    SECRET_KEY=os.getenv('SECRET_KEY', 'dev-secret-key'),
    DOWNLOAD_FOLDER=os.path.expanduser(os.getenv('DOWNLOAD_FOLDER', '~/dl')),
    MAX_CONTENT_LENGTH=int(os.getenv('MAX_CONTENT_LENGTH', 4294967296)),  # 4GB
    CACHE_TYPE='SimpleCache',
    CACHE_DEFAULT_TIMEOUT=300
)

# Initialize extensions
cache = Cache(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[os.getenv('RATELIMIT_DEFAULT', '10/minute')],
    storage_uri="memory://",
)

# Ensure download directory exists
DOWNLOAD_FOLDER = Path(app.config['DOWNLOAD_FOLDER'])
DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def allowed_file(filename):
    """Check if file extension is allowed"""
    allowed = os.getenv('ALLOWED_EXTENSIONS', 'mp4,mp3,avi,mkv,wav,zip,rar,jpg,png,pdf,txt').split(',')
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed

def get_file_info(filepath):
    """Get detailed file information"""
    stat = filepath.stat()
    return {
        'filename': filepath.name,
        'size': stat.st_size,
        'size_formatted': humanize.naturalsize(stat.st_size),
        'extension': filepath.suffix.lower(),
        'created': datetime.fromtimestamp(stat.st_ctime).isoformat(),
        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        'selected': False
    }

@app.route("/", methods=["GET", "POST"])
def index():
    """Main route serving the HTML interface"""
    if request.method == "POST":
        return handle_post_request()
    return render_template('index.html')

def handle_post_request():
    """Handle POST requests based on action"""
    action = request.form.get("action")
    
    if action == "search_youtube":
        return handle_youtube_search()
    elif action == "download_youtube":
        return handle_youtube_download()
    elif action == "download_file":
        return handle_file_download()
    elif action == "zip_and_download":
        return handle_zip_download()
    elif action == "get_files":
        return handle_get_files()
    else:
        return jsonify({"error": "Invalid action"}), 400

@cache.memoize(timeout=300)
def search_youtube(query, limit=10):
    """Search YouTube for videos with caching"""
    try:
        videos_search = VideosSearch(query, limit=limit)
        results = videos_search.result()
        
        formatted_results = []
        for item in results.get('result', []):
            formatted_results.append({
                'title': item.get('title', 'No title'),
                'url': item.get('link', ''),
                'thumbnail': item.get('thumbnails', [{}])[0].get('url', ''),
                'duration': item.get('duration', 'N/A'),
                'channel': item.get('channel', {}).get('name', 'Unknown'),
                'views': item.get('viewCount', {}).get('short', 'N/A'),
                'published': item.get('publishedTime', 'N/A')
            })
        return formatted_results
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []

def download_youtube_video(url, download_type='video', quality='best'):
    """Download YouTube video using yt-dlp"""
    try:
        ydl_opts = {
            'outtmpl': str(DOWNLOAD_FOLDER / '%(title)s.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
        }
        
        if download_type == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            ydl_opts['format'] = quality
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # Find the downloaded file
            if '_filename' in info:
                filename = info['_filename']
            else:
                # Try to find the most recent file in download folder
                files = sorted(DOWNLOAD_FOLDER.glob('*'), key=os.path.getmtime)
                filename = files[-1].name if files else None
            
            return {
                'status': 'success',
                'filename': filename,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'filesize': info.get('filesize', 0)
            }
    except Exception as e:
        logger.error(f"YouTube download error: {e}")
        return {'status': 'error', 'message': str(e)}

def handle_youtube_search():
    """Handle YouTube search requests"""
    query = request.form.get("query", "").strip()
    if not query:
        return jsonify([])
    
    results = search_youtube(query)
    return jsonify(results)

@limiter.limit("5/minute")
def handle_youtube_download():
    """Handle YouTube download requests with rate limiting"""
    url = request.form.get("url", "").strip()
    download_type = request.form.get("type", "video")
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    result = download_youtube_video(url, download_type)
    return jsonify(result)

def handle_file_download():
    """Handle file download requests"""
    filename = request.form.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "No filename provided"}), 400
    
    filepath = DOWNLOAD_FOLDER / filename
    if not filepath.exists() or not filepath.is_file():
        return jsonify({"error": "File not found"}), 404
    
    return send_from_directory(
        DOWNLOAD_FOLDER,
        filename,
        as_attachment=True,
        download_name=filename
    )

def handle_zip_download():
    """Handle zip download requests"""
    filenames = request.form.getlist("filenames[]")
    if not filenames:
        return jsonify({"error": "No files selected"}), 400
    
    # Create zip in memory
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename in filenames:
            filepath = DOWNLOAD_FOLDER / filename
            if filepath.exists() and filepath.is_file():
                zf.write(filepath, filename)
    
    memory_file.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"downloads_{timestamp}.zip"
    )

def handle_get_files():
    """Handle file listing requests"""
    query = request.form.get("query", "").lower().strip()
    
    files = []
    try:
        for filepath in DOWNLOAD_FOLDER.iterdir():
            if filepath.is_file():
                if query and query not in filepath.name.lower():
                    continue
                files.append(get_file_info(filepath))
        
        # Sort by modified time (newest first)
        files.sort(key=lambda x: x['modified'], reverse=True)
    except Exception as e:
        logger.error(f"File listing error: {e}")
    
    return jsonify(files)

@app.route('/api/files')
@cache.cached(timeout=60, query_string=True)
def api_get_files():
    """API endpoint for file listing with caching"""
    query = request.args.get('q', '').lower().strip()
    return handle_get_files()

@app.route('/api/disk-usage')
def api_disk_usage():
    """Get disk usage information"""
    total, used, free = shutil.disk_usage(DOWNLOAD_FOLDER)
    return jsonify({
        'total': humanize.naturalsize(total),
        'used': humanize.naturalsize(used),
        'free': humanize.naturalsize(free),
        'percent_used': round((used / total) * 100, 2)
    })

@app.route('/api/cleanup', methods=['POST'])
def api_cleanup():
    """Clean up old files"""
    days = int(request.form.get('days', 30))
    cutoff = datetime.now().timestamp() - (days * 24 * 60 * 60)
    
    deleted = []
    errors = []
    
    for filepath in DOWNLOAD_FOLDER.iterdir():
        if filepath.is_file() and filepath.stat().st_mtime < cutoff:
            try:
                filepath.unlink()
                deleted.append(filepath.name)
            except Exception as e:
                errors.append(str(e))
    
    return jsonify({
        'deleted': deleted,
        'errors': errors,
        'message': f"Deleted {len(deleted)} file(s)"
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)
    
    # Start Flask app
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=os.getenv('FLASK_ENV') == 'development'
    )
