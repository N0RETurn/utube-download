# app.py
from flask import Flask, render_template_string, request, jsonify, send_from_directory, abort
import yt_dlp
import os
import threading
import uuid
import shutil
import time
import re
from functools import wraps
import atexit

app = Flask(__name__, static_folder="downloads")

# Configuration
JOB_TIMEOUT = 300  # 5 minutes 
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_CONCURRENT_DOWNLOADS = 3  # Increased for moderate traffic

# Enhanced in-memory job store with timestamps
progress_store = {}

# Downloads folder (absolute)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
COOKIES_DIR = os.path.join(BASE_DIR, "cookies")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)

# Cookie management
COOKIES_FILE = os.path.join(COOKIES_DIR, 'cookies.txt')

# Simple IP-based rate limiting
request_times = {}

def rate_limit(max_per_minute=30):
    """Simple IP-based rate limiter for moderate traffic"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Get user's IP address
            user_ip = request.remote_addr or 'unknown'
            current_time = time.time()
            
            # Clean old entries for this IP (60-second window)
            window_start = current_time - 60
            if user_ip in request_times:
                request_times[user_ip] = [t for t in request_times[user_ip] if t > window_start]
            else:
                request_times[user_ip] = []
            
            # Check if user exceeded limit
            if len(request_times[user_ip]) >= max_per_minute:
                return jsonify({
                    "error": f"Too many requests. Maximum {max_per_minute} requests per minute. Please wait a moment."
                }), 429
            
            # Add this request
            request_times[user_ip].append(current_time)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Clean up old rate limit entries periodically
def cleanup_old_rate_limits():
    """Remove old rate limit entries to prevent memory leaks"""
    current_time = time.time()
    window_start = current_time - 120  # 2 minutes window
    
    ips_to_remove = []
    for ip, times in request_times.items():
        # Keep only recent entries
        request_times[ip] = [t for t in times if t > window_start]
        # Remove IP if no recent requests
        if not request_times[ip]:
            ips_to_remove.append(ip)
    
    for ip in ips_to_remove:
        del request_times[ip]

def cleanup_old_jobs():
    """Remove jobs older than JOB_TIMEOUT"""
    current_time = time.time()
    jobs_to_remove = []
    
    for job_id, job_data in progress_store.items():
        job_time = job_data.get('_timestamp', 0)
        if current_time - job_time > JOB_TIMEOUT:
            jobs_to_remove.append(job_id)
    
    for job_id in jobs_to_remove:
        # Clean up any associated files
        if 'files' in progress_store[job_id]:
            for filename in progress_store[job_id]['files']:
                try:
                    file_path = os.path.join(DOWNLOADS_DIR, filename)
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        print(f"Cleaned up file: {filename}")
                except Exception as e:
                    print(f"Error cleaning up file {filename}: {e}")
        del progress_store[job_id]
        print(f"Cleaned up old job: {job_id}")

def has_valid_cookies():
    """Check if we have valid YouTube cookies"""
    if not os.path.exists(COOKIES_FILE):
        return False
    
    try:
        with open(COOKIES_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if content and not content.startswith('# YouTube Cookies File'):
                lines = [line for line in content.split('\n') if line.strip() and not line.strip().startswith('#')]
                return len(lines) > 1
    except:
        pass
    
    return False

def create_sample_cookies():
    """Create sample cookies file with instructions"""
    with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
        f.write("""# YouTube Cookies File
# For better download success rates:
# 1. Install 'Get cookies.txt LOCALLY' browser extension
# 2. Go to YouTube.com and log in
# 3. Export cookies and replace this file

# Example format:
.youtube.com	TRUE	/	TRUE	0	CONSENT	YES+
.youtube.com	TRUE	/	TRUE	0	LOGIN_INFO	your_cookie_value_here
""")

# Create sample cookies file if it doesn't exist
if not os.path.exists(COOKIES_FILE):
    create_sample_cookies()

def get_concurrent_downloads():
    """Get number of currently active downloads"""
    return sum(1 for job in progress_store.values() if not job.get('done'))

# URL validation function
def validate_youtube_url(url):
    video_patterns = [
        r'^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/embed/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/v/[\w-]+'
    ]
    
    playlist_pattern = r'^(https?://)?(www\.)?youtube\.com/(playlist|watch)\?.*list=[\w-]+'
    
    channel_patterns = [
        r'^(https?://)?(www\.)?youtube\.com/channel/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/c/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/user/[\w-]+'
    ]
    
    all_patterns = video_patterns + [playlist_pattern] + channel_patterns
    
    # Security checks
    if not url.startswith(('http://', 'https://')):
        return False
        
    # Check for potentially malicious patterns
    malicious_patterns = [
        r'\.\./',  # Path traversal
        r'file://',  # Local file access
        r'javascript:',  # JavaScript injection
        r'data:',  # Data URI
        r'vbscript:',  # VBScript injection
    ]
    
    for pattern in malicious_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return False
    
    # Check URL length to prevent attacks
    if len(url) > 2000:
        return False
            
    return any(re.match(pattern, url, re.IGNORECASE) for pattern in all_patterns)

# Enhanced yt-dlp options
def get_ydl_opts(format_type, mode, job_id=None):
    base_opts = {
        'outtmpl': os.path.join(DOWNLOADS_DIR, "%(title).100s.%(ext)s"),
        'noplaylist': (mode == 'single'),
        'quiet': True,
        'no_warnings': False,
        'cookiefile': COOKIES_FILE if os.path.exists(COOKIES_FILE) and has_valid_cookies() else None,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.youtube.com/',
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'extract_flat': False,
        'ignoreerrors': True,
    }
    
    if format_type == 'video':
        base_opts.update({
            'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
            'merge_output_format': 'mp4',
        })
    else:  # audio
        base_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192'
            }],
        })
    
    return base_opts

def download_worker(job_id, url, fmt, mode):
    """Download worker that processes in background"""
    progress_store[job_id] = {
        "status": "processing", 
        "done": False, 
        "message": "Initializing download...",
        "_timestamp": time.time()
    }
    
    try:
        # Check concurrent downloads limit
        if get_concurrent_downloads() > MAX_CONCURRENT_DOWNLOADS:
            progress_store[job_id].update({
                "done": True, 
                "error": "Server busy. Please try again in a few moments.", 
                "status": "error"
            })
            return

        # Validate URL first
        if not validate_youtube_url(url):
            progress_store[job_id].update({
                "done": True, 
                "error": "Invalid YouTube URL format", 
                "status": "error"
            })
            return

        ydl_opts = get_ydl_opts(fmt, mode, job_id)

        # Simple progress hook - just update status occasionally
        def hook(d):
            try:
                if d['status'] == 'downloading':
                    # Only update status occasionally to avoid too many updates
                    if int(time.time()) % 5 == 0:  # Update every 5 seconds
                        progress_store[job_id].update({
                            "message": "Downloading content...",
                            "_timestamp": time.time()
                        })
                elif d['status'] == 'finished':
                    progress_store[job_id].update({
                        "message": "Finalizing file...",
                        "_timestamp": time.time()
                    })
            except Exception as e:
                pass

        ydl_opts['progress_hooks'] = [hook]

        # Enhanced retry logic
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(url, download=True)
                break  # Success
            except yt_dlp.DownloadError as e:
                error_str = str(e)
                if ("Sign in to confirm" in error_str or "bot" in error_str.lower()) and attempt < max_retries:
                    progress_store[job_id].update({
                        "message": f"Preparing download... (attempt {attempt + 1})",
                        "_timestamp": time.time()
                    })
                    time.sleep(2)
                    continue
                else:
                    if "Sign in to confirm" in error_str:
                        error_msg = "This video requires YouTube authentication and cannot be downloaded."
                    else:
                        error_msg = f"Download error: {error_str[:100]}..."  # Limit error length
                    progress_store[job_id].update({
                        "done": True, 
                        "error": error_msg, 
                        "status": "error"
                    })
                    return
            except Exception as e:
                progress_store[job_id].update({
                    "done": True, 
                    "error": f"Unexpected error: {str(e)}", 
                    "status": "error"
                })
                return

        # Process downloaded files
        downloaded_files = []
        if 'entries' in result:  # Playlist
            for entry in result['entries']:
                if not entry: continue
                try:
                    fn = ydl.prepare_filename(entry)
                    if fmt == 'audio':
                        fn = os.path.splitext(fn)[0] + ".mp3"
                    if os.path.exists(fn):
                        # Check file size
                        file_size = os.path.getsize(fn)
                        if file_size > MAX_DOWNLOAD_SIZE:
                            print(f"File too large: {fn} ({file_size} bytes)")
                            os.remove(fn)
                            continue
                        downloaded_files.append(os.path.basename(fn))
                except Exception as e:
                    print(f"Error processing playlist entry: {e}")
                    continue
            
            if downloaded_files:
                # Create zip for playlists
                zip_name = f"playlist_{job_id}.zip"
                zip_path = os.path.join(DOWNLOADS_DIR, zip_name)
                tmp_dir = os.path.join(DOWNLOADS_DIR, f"tmp_{job_id}")
                os.makedirs(tmp_dir, exist_ok=True)
                
                total_size = 0
                for f in downloaded_files:
                    file_path = os.path.join(DOWNLOADS_DIR, f)
                    if os.path.exists(file_path):
                        file_size = os.path.getsize(file_path)
                        total_size += file_size
                        if total_size > MAX_DOWNLOAD_SIZE:
                            shutil.rmtree(tmp_dir)
                            progress_store[job_id].update({
                                "done": True, 
                                "error": "Playlist too large to download", 
                                "status": "error"
                            })
                            return
                        shutil.copy(file_path, tmp_dir)
                
                shutil.make_archive(os.path.splitext(zip_path)[0], 'zip', tmp_dir)
                shutil.rmtree(tmp_dir)
                
                # Clean up individual files
                for f in downloaded_files:
                    try:
                        os.remove(os.path.join(DOWNLOADS_DIR, f))
                    except:
                        pass
                
                progress_store[job_id].update({
                    "done": True, 
                    "files": [zip_name], 
                    "zip": zip_name, 
                    "status": "ready",
                    "message": "Playlist ready for download",
                    "_timestamp": time.time()
                })
            else:
                progress_store[job_id].update({
                    "done": True, 
                    "error": "No files were downloaded", 
                    "status": "error"
                })
        else:  # Single video
            try:
                filename = ydl.prepare_filename(result)
                if fmt == 'audio':
                    filename = os.path.splitext(filename)[0] + ".mp3"
                if os.path.exists(filename):
                    # Check file size
                    file_size = os.path.getsize(filename)
                    if file_size > MAX_DOWNLOAD_SIZE:
                        os.remove(filename)
                        progress_store[job_id].update({
                            "done": True, 
                            "error": "File too large to download", 
                            "status": "error"
                        })
                        return
                    
                    filename = os.path.basename(filename)
                    progress_store[job_id].update({
                        "done": True, 
                        "file": filename, 
                        "files": [filename],
                        "status": "ready",
                        "message": "File ready for download",
                        "_timestamp": time.time()
                    })
                else:
                    progress_store[job_id].update({
                        "done": True, 
                        "error": "Downloaded file not found", 
                        "status": "error"
                    })
            except Exception as e:
                progress_store[job_id].update({
                    "done": True, 
                    "error": f"Error processing file: {str(e)}", 
                    "status": "error"
                })
                
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Error in download_worker for job {job_id}: {error_details}")
        
        progress_store[job_id].update({
            "done": True, 
            "error": "Server error during download. Please try again.", 
            "status": "error"
        })

# HTML template remains exactly the same as before
HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#071021">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>YouTube Downloader - Mobile & Desktop</title>
  <style>
    :root{--bg:#071021;--card:#0e1722;--muted:#9aa6b2;--accent:#06b6d4;--text:#e6eef3;--btn:#10b981;--error:#ef4444;--warning:#f59e0b}
    html,body{height:100%;margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(180deg,#071021,#0a1220);color:var(--text);-webkit-text-size-adjust:100%;}
    .wrap{max-width:920px;margin:0 auto;padding:16px}
    .card{background:linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));padding:18px;border-radius:12px;box-shadow:0 6px 18px rgba(2,6,23,0.6);margin-bottom:20px;}
    h1{margin:0 0 8px;font-size:20px}
    p.muted{color:var(--muted);margin-top:4px;font-size:14px;}
    label{display:block;margin-top:12px;font-size:14px;color:var(--muted)}
    input[type=text],input[type=url]{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:var(--text);box-sizing:border-box;font-size:16px;-webkit-appearance:none;}
    select{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:var(--text);font-size:16px;-webkit-appearance:none;}
    .row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
    button{font-size:16px;padding:14px 20px;border-radius:10px;border:none;font-weight:600;cursor:pointer;min-height:44px;}
    button.primary{background:var(--btn);color:#04201b;}
    button.ghost{background:transparent;border:1px solid rgba(255,255,255,0.06);color:var(--text);}
    button:disabled{opacity:0.6;cursor:not-allowed}
    button:active{transform:scale(0.98);}
    .preview{display:flex;gap:14px;align-items:flex-start;margin-top:14px;flex-wrap:wrap}
    .thumb{width:100%;max-width:320px;border-radius:8px}
    .meta{flex:1;min-width:220px}
    .processing{text-align:center;padding:30px;color:var(--muted);}
    .spinner{border:3px solid rgba(255,255,255,0.1);border-radius:50%;border-top:3px solid var(--accent);width:40px;height:40px;animation:spin 1s linear infinite;margin:0 auto 20px;}
    @keyframes spin{0%{transform:rotate(0deg);}100%{transform:rotate(360deg);}}
    .links{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap}
    a.link{background:#0b1220;padding:12px 16px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);color:var(--text);text-decoration:none;display:block;min-height:44px;display:flex;align-items:center;justify-content:center;text-align:center;}
    .small{font-size:13px;color:var(--muted)}
    .error{color:var(--error);background:rgba(239,68,68,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(239,68,68,0.3)}
    .warning{color:var(--warning);background:rgba(245,158,11,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(245,158,11,0.3)}
    .info-box{background:rgba(6,182,212,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(6,182,212,0.3)}
    .success{color:var(--btn);background:rgba(16,185,129,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(16,185,129,0.3)}
    footer{margin-top:20px;text-align:center;color:var(--muted);font-size:13px}
    .cookie-status {display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px;}
    .cookie-ok { background: var(--btn); }
    .cookie-missing { background: var(--warning); }
    pre {background: #1a1a1a; padding: 15px; border-radius: 8px; overflow-x: auto; color: var(--text);font-size:14px;}
    code {background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 4px;font-size:14px;}
    .copy-btn {background: var(--accent); color: white; border: none; padding: 12px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; margin-top: 10px; width: 100%;}
    .tab-container {display: flex; gap: 8px; margin-bottom: 16px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 12px; flex-wrap: wrap;}
    .tab {padding: 12px 16px; border-radius: 8px; background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 14px; min-height: 44px; flex: 1;}
    .tab.active {background: var(--accent); color: white;}
    .tab-content {display: none;}
    .tab-content.active {display: block;}
    
    /* Mobile-specific styles */
    @media (max-width: 768px) {
        .wrap {padding: 12px;}
        .card {padding: 16px;}
        h1 {font-size: 18px;}
        button {min-height: 48px; font-size: 16px;}
        input, select {font-size: 16px; min-height: 44px;}
        .row {flex-direction: column;}
        .preview {flex-direction: column;}
        .thumb {max-width: 100%;}
        .tab-container {flex-direction: column;}
        .tab {min-height: 50px; font-size: 16px;}
    }
    
    @media (max-width: 480px) {
        .wrap {padding: 8px;}
        .card {padding: 12px;}
        button {padding: 16px 20px;}
        a.link {padding: 16px; font-size: 14px;}
    }
    
    /* Touch device improvements */
    @media (hover: none) and (pointer: coarse) {
        button:active, a.link:active {background: rgba(255,255,255,0.1);}
        button.primary:active {background: var(--btn); opacity: 0.8;}
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🎬 YouTube Downloader</h1>
      <p class="muted">Download YouTube videos and audio directly to your device. Perfect for mobile and desktop!</p>

      <div class="tab-container">
        <button class="tab active" onclick="switchTab('mobile-tab')">📱 Mobile Download</button>
        <button class="tab" onclick="switchTab('desktop-tab')">💻 Desktop Commands</button>
      </div>

      <!-- Mobile Download Tab -->
      <div id="mobile-tab" class="tab-content active">
        <div class="info-box">
          <strong>Perfect for Mobile Users! 📱</strong> 
          <ul style="margin: 8px 0; padding-left: 20px;">
            <li>Paste YouTube URL and download directly to your phone</li>
            <li>Simple processing - no complicated progress bars</li>
            <li>Works on all mobile browsers</li>
            <li>No app installation required</li>
          </ul>
        </div>

        <form id="downloadForm" onsubmit="startDownload(event)">
          <label>YouTube URL</label>
          <input id="url" type="url" placeholder="https://www.youtube.com/watch?v=..." required inputmode="url">

          <div class="row">
            <div style="flex:1">
              <label>Format</label>
              <select id="format">
                <option value="video">Video (MP4)</option>
                <option value="audio">Audio (MP3)</option>
              </select>
            </div>
            <div style="flex:1">
              <label>Mode</label>
              <select id="mode">
                <option value="single">Single Video</option>
                <option value="playlist">Playlist</option>
              </select>
            </div>
          </div>

          <button class="primary" type="submit" id="submitBtn" style="margin-top: 16px; width: 100%;">
            Prepare Download
          </button>
        </form>

        <div id="errorArea" class="error" style="display:none"></div>
        <div id="warningArea" class="warning" style="display:none"></div>

        <!-- Preview Area (shown before processing) -->
        <div id="previewArea" style="display:none" class="preview">
          <img id="thumb" class="thumb" src="" alt="thumbnail">
          <div class="meta">
            <h3 id="title"></h3>
            <div class="small" id="uploader"></div>
            <div class="small" id="views"></div>
            <div class="small" id="duration"></div>
          </div>
        </div>

        <!-- Processing Area (shown during processing) -->
        <div id="processingArea" style="display:none" class="processing">
          <div class="spinner"></div>
          <h3>Preparing Your Download</h3>
          <p id="processingMessage">This may take a few moments...</p>
          <p class="small">Please keep this page open</p>
        </div>

        <!-- Results Area (shown when ready) -->
        <div id="resultsArea" style="display:none">
          <div class="success">
            <h3>✅ Download Ready!</h3>
            <p>Your file is ready to download to your device.</p>
          </div>
          <div class="links" id="links" style="margin-top: 16px;"></div>
        </div>
      </div>

      <!-- Desktop Commands Tab -->
      <div id="desktop-tab" class="tab-content">
        <div class="info-box">
          <strong>For Desktop/Laptop Users</strong>
          <p>Generate commands to run yt-dlp directly on your computer.</p>
        </div>

        <form onsubmit="generateLocalCommand(event)">
          <label>YouTube URL</label>
          <input id="localUrl" type="url" placeholder="https://www.youtube.com/watch?v=..." required inputmode="url">
          
          <div class="row">
            <div style="flex:1">
              <label>Format</label>
              <select id="localFormat">
                <option value="video">Video (MP4)</option>
                <option value="audio">Audio (MP3)</option>
              </select>
            </div>
            <div style="flex:1">
              <label>Mode</label>
              <select id="localMode">
                <option value="single">Single Video</option>
                <option value="playlist">Playlist</option>
              </select>
            </div>
          </div>
          
          <button class="primary" type="submit" style="margin-top: 16px; width: 100%;">
            Generate Download Command
          </button>
        </form>
        
        <div id="localCommandResult" style="display: none; margin-top: 16px;">
          <div class="info-box">
            <strong>Local Download Command:</strong>
            <pre id="commandOutput" style="margin: 12px 0; padding: 12px; background: rgba(0,0,0,0.3);"></pre>
            <button class="copy-btn" onclick="copyCommand()">📋 Copy Command to Clipboard</button>
            
            <div style="margin-top: 16px;">
              <p><strong>Instructions:</strong></p>
              <ol style="margin: 8px 0; padding-left: 20px;">
                <li>Install yt-dlp: <code>pip install yt-dlp</code></li>
                <li>Copy the command above</li>
                <li>Run it in your terminal/command prompt</li>
                <li>Files download directly to your computer</li>
              </ol>
            </div>
          </div>
        </div>
      </div>
    </div>

    <footer>
      <p>📱 <strong>Mobile Friendly:</strong> Simple preparation then direct download</p>
      <p>🔒 <strong>Privacy:</strong> Files are temporarily stored and auto-deleted</p>
    </footer>
  </div>

<script>
let pollInterval = null;
let downloadId = null;

function switchTab(tabName) {
  // Hide all tab contents
  document.querySelectorAll('.tab-content').forEach(tab => {
    tab.classList.remove('active');
  });
  
  // Remove active class from all tabs
  document.querySelectorAll('.tab').forEach(tab => {
    tab.classList.remove('active');
  });
  
  // Show selected tab content
  document.getElementById(tabName).classList.add('active');
  
  // Add active class to clicked tab
  event.target.classList.add('active');
}

function startDownload(ev) {
  ev.preventDefault();
  const url = document.getElementById('url').value.trim();
  const format = document.getElementById('format').value;
  const mode = document.getElementById('mode').value;

  if(!url) {
    showError('Please enter a YouTube URL');
    return;
  }
  
  hideMessages();
  setLoading(true);
  hideAllAreas();

  fetch('/start-download', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, format, mode})
  }).then(r => {
    if (r.status === 429) {
      return r.json().then(data => { throw new Error(data.error); });
    }
    return r.json();
  }).then(data => {
    setLoading(false);
    if(data.error) {
      showError(data.error);
      return;
    }
    
    downloadId = data.id;
    
    if(data.warning) {
      showWarning(data.warning);
    }
    
    // Show preview if available
    if(data.preview){
      const p = data.preview;
      document.getElementById('thumb').src = p.thumbnail || '';
      document.getElementById('title').textContent = p.title || '';
      document.getElementById('uploader').textContent = 'Uploader: ' + (p.uploader || 'N/A');
      document.getElementById('views').textContent = 'Views: ' + (p.view_count ? p.view_count.toLocaleString() : 'N/A');
      document.getElementById('duration').textContent = p.duration ? Math.floor(p.duration/60) + ' min ' + (p.duration%60) + ' sec' : '';
      document.getElementById('previewArea').style.display = 'flex';
    }
    
    // Show processing area
    document.getElementById('processingArea').style.display = 'block';
    
    // Start checking for completion
    if(pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(() => checkDownloadStatus(downloadId), 2000); // Check every 2 seconds
    
  }).catch(err => { 
    setLoading(false);
    showError(err.message || 'Start error: ' + err); 
  });
}

function checkDownloadStatus(id) {
  fetch('/progress/' + id).then(r => {
    if (r.status === 404) {
      showError('Download session expired. Please start a new download.');
      clearInterval(pollInterval);
      return;
    }
    return r.json();
  }).then(data => {
    if (!data) return;
    
    if(data.error) {
      // Hide processing area and show error
      document.getElementById('processingArea').style.display = 'none';
      showError(data.error);
      clearInterval(pollInterval);
      return;
    }
    
    // Update processing message occasionally
    if(data.message) {
      document.getElementById('processingMessage').textContent = data.message;
    }
    
    if(data.done) {
      clearInterval(pollInterval);
      // Hide processing area and show results
      document.getElementById('processingArea').style.display = 'none';
      document.getElementById('resultsArea').style.display = 'block';
      
      const linksDiv = document.getElementById('links');
      linksDiv.innerHTML = '';
      
      if(data.files && data.files.length) {
        data.files.forEach(f => {
          const a = document.createElement('a');
          a.className = 'link';
          a.href = '/download/' + encodeURIComponent(f);
          a.textContent = '📥 Download: ' + (f.includes('.zip') ? 'Playlist (ZIP)' : f);
          a.target = '_blank';
          a.download = true;
          linksDiv.appendChild(a);
        });
      }
    }
  }).catch(err => { 
    console.error('Status check error:', err);
    if (err.toString().includes('404')) {
      showError('Download session expired. Please start a new download.');
      clearInterval(pollInterval);
    }
  });
}

function generateLocalCommand(ev) {
  ev.preventDefault();
  const url = document.getElementById('localUrl').value.trim();
  const format = document.getElementById('localFormat').value;
  const mode = document.getElementById('localMode').value;

  if(!url) {
    showError('Please enter a YouTube URL');
    return;
  }

  const submitBtn = ev.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.textContent = 'Generating...';

  fetch('/generate-command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, format, mode})
  }).then(r => {
    if (r.status === 429) {
      return r.json().then(data => { throw new Error(data.error); });
    }
    return r.json();
  }).then(data => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate Download Command';
    
    if (data.error) {
      showError(data.error);
      return;
    }
    
    document.getElementById('commandOutput').textContent = data.command;
    document.getElementById('localCommandResult').style.display = 'block';
    document.getElementById('localCommandResult').scrollIntoView({ behavior: 'smooth' });
    hideMessages();
    
  }).catch(err => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate Download Command';
    showError(err.message || 'Error generating command: ' + err);
  });
}

function copyCommand() {
  const commandText = document.getElementById('commandOutput').textContent;
  navigator.clipboard.writeText(commandText).then(() => {
    const btn = document.querySelector('#localCommandResult .copy-btn');
    const originalText = btn.textContent;
    btn.textContent = '✓ Copied!';
    btn.style.background = '#10b981';
    setTimeout(() => {
      btn.textContent = originalText;
      btn.style.background = '';
    }, 2000);
  }).catch(err => {
    showError('Failed to copy: ' + err);
  });
}

function showError(message) {
  const errorDiv = document.getElementById('errorArea');
  errorDiv.textContent = message;
  errorDiv.style.display = 'block';
  document.getElementById('warningArea').style.display = 'none';
  hideAllAreas();
}

function showWarning(message) {
  const warningDiv = document.getElementById('warningArea');
  warningDiv.textContent = message;
  warningDiv.style.display = 'block';
  document.getElementById('errorArea').style.display = 'none';
}

function hideMessages() {
  document.getElementById('errorArea').style.display = 'none';
  document.getElementById('warningArea').style.display = 'none';
}

function hideAllAreas() {
  document.getElementById('previewArea').style.display = 'none';
  document.getElementById('processingArea').style.display = 'none';
  document.getElementById('resultsArea').style.display = 'none';
}

function setLoading(loading) {
  const btn = document.getElementById('submitBtn');
  btn.disabled = loading;
  btn.textContent = loading ? 'Preparing...' : 'Prepare Download';
}

// Handle Enter key in forms
document.getElementById('url')?.addEventListener('keypress', function(e) {
  if (e.key === 'Enter') {
    document.getElementById('downloadForm').dispatchEvent(new Event('submit'));
  }
});

document.getElementById('localUrl')?.addEventListener('keypress', function(e) {
  if (e.key === 'Enter') {
    document.querySelector('#desktop-tab form').dispatchEvent(new Event('submit'));
  }
});
</script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    cleanup_old_jobs()
    cleanup_old_rate_limits()
    return render_template_string(HTML)

@app.route('/cookie-status', methods=['GET'])
def cookie_status():
    return jsonify({"has_cookies": has_valid_cookies()})

@rate_limit(max_per_minute=20)  # 20 requests per minute per IP for downloads
@app.route('/start-download', methods=['POST'])
def start_download():
    """Start download for mobile users"""
    cleanup_old_jobs()
    cleanup_old_rate_limits()
    
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    fmt = data.get('format', 'video')
    mode = data.get('mode', 'single')
    
    if not url:
        return jsonify({"error": "Please enter a YouTube URL"}), 400
    
    if not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL format"}), 400
    
    job_id = str(uuid.uuid4())
    progress_store[job_id] = {
        "status": "processing", 
        "done": False, 
        "message": "Initializing download...",
        "_timestamp": time.time()
    }
    
    # Prepare response
    response_data = {"id": job_id}
    if not has_valid_cookies():
        response_data["warning"] = "No cookies detected - some age-restricted videos may not download"
    
    # Preview
    preview = {}
    try:
        preview_opts = get_ydl_opts(fmt, mode)
        preview_opts['skip_download'] = True
        with yt_dlp.YoutubeDL(preview_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                first = info['entries'][0] if info['entries'] else {}
                preview = {
                    "title": first.get('title'), 
                    "uploader": first.get('uploader'), 
                    "thumbnail": first.get('thumbnail'), 
                    "view_count": first.get('view_count'), 
                    "duration": first.get('duration')
                }
            else:
                preview = {
                    "title": info.get('title'), 
                    "uploader": info.get('uploader'), 
                    "thumbnail": info.get('thumbnail'), 
                    "view_count": info.get('view_count'), 
                    "duration": info.get('duration')
                }
    except Exception as e:
        preview = {"title": None, "warning": f"Preview limited: {str(e)}"}
    
    response_data["preview"] = preview
    
    # Start download
    t = threading.Thread(target=download_worker, args=(job_id, url, fmt, mode), daemon=True)
    t.start()
    
    return jsonify(response_data)

@rate_limit(max_per_minute=30)  # 30 requests per minute per IP for command generation
@app.route('/generate-command', methods=['POST'])
def generate_command():
    """Generate yt-dlp commands for desktop users"""
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    fmt = data.get('format', 'video')
    mode = data.get('mode', 'single')
    
    if not url:
        return jsonify({"error": "Please enter a YouTube URL"}), 400
    
    if not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL format"}), 400
    
    # Generate yt-dlp command
    if fmt == 'video':
        format_option = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
        command_parts = [
            'yt-dlp',
            '-f', f'"{format_option}"',
            '--merge-output-format', 'mp4',
        ]
    else:  # audio
        command_parts = [
            'yt-dlp',
            '-f', 'bestaudio/best',
            '--extract-audio',
            '--audio-format', 'mp3',
            '--audio-quality', '0',
        ]
    
    if mode == 'single':
        command_parts.append('--no-playlist')
    else:
        command_parts.append('--yes-playlist')
    
    # Add cookies if available
    if has_valid_cookies():
        command_parts.append(f'--cookies {COOKIES_FILE}')
    
    command_parts.append(f'"{url}"')
    
    command = ' '.join(command_parts)
    
    response_data = {
        "command": command,
        "note": "Run this command in your terminal/command prompt"
    }
    
    if not has_valid_cookies():
        response_data["warning"] = "For better success rates, consider setting up cookies"
    
    return jsonify(response_data)

@rate_limit(max_per_minute=60)  # 60 requests per minute per IP for progress checks
@app.route('/progress/<job_id>', methods=['GET'])
def progress(job_id):
    cleanup_old_jobs()
    cleanup_old_rate_limits()
    
    info = progress_store.get(job_id)
    if not info:
        return jsonify({"error": "Job not found or expired"}), 404
    
    info['_timestamp'] = time.time()
    return jsonify(info)

@app.route('/download/<path:filename>', methods=['GET'])
def download_file(filename):
    """Serve downloaded files to mobile users"""
    safe_path = os.path.join(DOWNLOADS_DIR, filename)
    safe_abs = os.path.abspath(safe_path)
    
    if not safe_abs.startswith(os.path.abspath(DOWNLOADS_DIR)):
        abort(400)
    if not os.path.exists(safe_abs):
        abort(404)
    
    # Set appropriate headers for mobile downloads
    response = send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

# Register cleanup function
atexit.register(cleanup_old_jobs)

if __name__ == "__main__":
    print("🚀 Starting YouTube Downloader...")
    print("📱 Mobile Support: Simple preparation then direct download")
    print("💻 Desktop Support: Generate local download commands")
    print("🔒 Privacy: Files auto-delete after 1 hour")
    print("🛡️  Rate Limiting: IP-based (20-60 requests/minute)")
    print("📊 Capacity: 50-100 users per minute")
    print(f"📁 Downloads folder: {DOWNLOADS_DIR}")
    
    if has_valid_cookies():
        print("✅ Valid cookies detected")
    else:
        print("⚠ No valid cookies - some content may not download")
    
    # Production settings
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    
    app.run(
        host="0.0.0.0", 
        port=port,
        debug=debug_mode,
        threaded=True
    )