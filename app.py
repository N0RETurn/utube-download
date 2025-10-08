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

app = Flask(__name__, static_folder="downloads")

# In-memory job store: job_id -> status dict
progress_store = {}

# Downloads folder (absolute)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
COOKIES_DIR = os.path.join(BASE_DIR, "cookies")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)

# Rate limiting decorator
def rate_limit(max_per_minute=6):
    interval = 60.0 / max_per_minute
    def decorator(func):
        last_called = [0.0]
        @wraps(func)
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            left_to_wait = interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            ret = func(*args, **kwargs)
            last_called[0] = time.time()
            return ret
        return wrapper
    return decorator

# URL validation function
def validate_youtube_url(url):
    # YouTube video patterns
    video_patterns = [
        r'^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/embed/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/v/[\w-]+'
    ]
    
    # YouTube playlist pattern  
    playlist_pattern = r'^(https?://)?(www\.)?youtube\.com/(playlist|watch)\?.*list=[\w-]+'
    
    # YouTube channel patterns
    channel_patterns = [
        r'^(https?://)?(www\.)?youtube\.com/channel/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/c/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/user/[\w-]+'
    ]
    
    # Check all patterns
    all_patterns = video_patterns + [playlist_pattern] + channel_patterns
    return any(re.match(pattern, url, re.IGNORECASE) for pattern in all_patterns)

# Enhanced yt-dlp options with cookie support
def get_ydl_opts(format_type, mode, job_id=None):
    base_opts = {
        'outtmpl': os.path.join(DOWNLOADS_DIR, "%(title).100s.%(ext)s"),
        'noplaylist': (mode == 'single'),
        'quiet': True,
        'no_warnings': False,
        # Cookie and authentication options
        'cookiefile': os.path.join(COOKIES_DIR, 'cookies.txt'),
        # Browser user agent to appear more legitimate
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.youtube.com/',
        # Retry settings
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'extract_flat': False,
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

# HTML template (responsive, includes JS to poll progress)
HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>YouTube Downloader</title>
  <style>
    :root{--bg:#071021;--card:#0e1722;--muted:#9aa6b2;--accent:#06b6d4;--text:#e6eef3;--btn:#10b981;--error:#ef4444}
    html,body{height:100%;margin:0;font-family:Inter,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(180deg,#071021,#0a1220);color:var(--text)}
    .wrap{max-width:920px;margin:24px auto;padding:16px}
    .card{background:linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));padding:18px;border-radius:12px;box-shadow:0 6px 18px rgba(2,6,23,0.6)}
    h1{margin:0 0 8px;font-size:20px}
    p.muted{color:var(--muted);margin-top:4px}
    label{display:block;margin-top:12px;font-size:14px;color:var(--muted)}
    input[type=text]{width:100%;padding:10px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:var(--text);box-sizing:border-box}
    select{padding:10px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:var(--text);width:100%}
    .row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
    button.primary{background:var(--btn);color:#04201b;padding:10px 14px;border-radius:10px;border:none;font-weight:600;cursor:pointer}
    button.ghost{background:transparent;border:1px solid rgba(255,255,255,0.06);color:var(--text);padding:10px;border-radius:10px;cursor:pointer}
    button:disabled{opacity:0.6;cursor:not-allowed}
    .preview{display:flex;gap:14px;align-items:flex-start;margin-top:14px;flex-wrap:wrap}
    .thumb{width:320px;max-width:100%;border-radius:8px}
    .meta{flex:1;min-width:220px}
    .progress-wrap{margin-top:12px}
    .progress{height:14px;background:rgba(255,255,255,0.04);border-radius:999px;overflow:hidden}
    .progress > i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#7c3aed);width:0%}
    .status{margin-top:8px;color:var(--muted);font-size:13px}
    .links{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap}
    a.link{background:#0b1220;padding:8px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);color:var(--text);text-decoration:none}
    .small{font-size:13px;color:var(--muted)}
    .error{color:var(--error);background:rgba(239,68,68,0.1);padding:10px;border-radius:8px;margin-top:10px;border:1px solid rgba(239,68,68,0.3)}
    .info-box{background:rgba(6,182,212,0.1);padding:10px;border-radius:8px;margin-top:10px;border:1px solid rgba(6,182,212,0.3)}
    footer{margin-top:20px;text-align:center;color:var(--muted);font-size:13px}
    @media (max-width:640px){.preview{flex-direction:column}.thumb{width:100%}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>YouTube Downloader (Flask)</h1>
      <p class="muted">Paste a YouTube or playlist URL, preview metadata, choose MP4 or MP3, and download. Mobile-friendly.</p>

      <div class="info-box">
        <strong>Important:</strong> For best results, ensure you have <code>cookies.txt</code> in the cookies folder to avoid bot detection.
      </div>

      <form id="startForm" onsubmit="startDownload(event)">
        <label>Video / Playlist URL</label>
        <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=..." required>

        <div class="row">
          <div style="flex:1">
            <label>Format</label>
            <select id="format">
              <option value="video">Video (MP4)</option>
              <option value="audio">Audio (MP3)</option>
            </select>
          </div>
          <div style="width:160px">
            <label>Mode</label>
            <select id="mode">
              <option value="single">Single Video</option>
              <option value="playlist">Playlist</option>
            </select>
          </div>
        </div>

        <div class="row" style="margin-top:14px">
          <button class="primary" type="submit" id="submitBtn">Start Download</button>
          <button class="ghost" type="button" onclick="clearUI()">Clear</button>
        </div>
      </form>

      <div id="errorArea" class="error" style="display:none"></div>

      <div id="previewArea" style="display:none" class="preview">
        <img id="thumb" class="thumb" src="" alt="thumbnail">
        <div class="meta">
          <h3 id="title"></h3>
          <div class="small" id="uploader"></div>
          <div class="small" id="views"></div>
          <div class="small" id="duration"></div>

          <div class="progress-wrap">
            <div class="progress"><i id="bar"></i></div>
            <div class="status" id="status">Waiting...</div>
          </div>

          <div class="links" id="links"></div>
        </div>
      </div>
      

      <div id="log" style="margin-top:12px;color:var(--muted);font-size:13px"></div>
    </div>

    <footer>Tip: Install ffmpeg for merging/conversion (required for MP3 and best video output).</footer>
  </div>

<script>
let pollInterval = null;
let downloadId = null;

function showError(message) {
  const errorDiv = document.getElementById('errorArea');
  errorDiv.textContent = message;
  errorDiv.style.display = 'block';
}

function hideError() {
  document.getElementById('errorArea').style.display = 'none';
}

function setLoading(loading) {
  document.getElementById('submitBtn').disabled = loading;
  document.getElementById('submitBtn').textContent = loading ? 'Starting...' : 'Start Download';
}

function startDownload(ev){
  ev.preventDefault();
  const url = document.getElementById('url').value.trim();
  const format = document.getElementById('format').value;
  const mode = document.getElementById('mode').value;

  if(!url) return showError('Please enter a URL');
  
  hideError();
  setLoading(true);

  fetch('/start', {
    method: 'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url, format, mode})
  }).then(r=>r.json()).then(data=>{
    setLoading(false);
    if(data.error) return showError(data.error);
    downloadId = data.id;
    document.getElementById('log').textContent = 'Job started: ' + data.id;
    if(data.preview){
      const p = data.preview;
      document.getElementById('thumb').src = p.thumbnail || '';
      document.getElementById('title').textContent = p.title || '';
      document.getElementById('uploader').textContent = 'Uploader: ' + (p.uploader || 'N/A');
      document.getElementById('views').textContent = 'Views: ' + (p.view_count ? p.view_count.toLocaleString() : 'N/A');
      document.getElementById('duration').textContent = p.duration ? Math.floor(p.duration/60) + ' min ' + (p.duration%60) + ' sec' : '';
      document.getElementById('previewArea').style.display = 'flex';
    }
    if(pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(()=>fetchProgress(downloadId), 1000);
  }).catch(err=>{ 
    setLoading(false);
    showError('Start error: ' + err); 
  });
}

function fetchProgress(id){
  fetch('/progress/' + id).then(r=>r.json()).then(data=>{
    if(data.error){
      document.getElementById('status').textContent = data.error;
      if(data.error.includes('bot') || data.error.includes('Sign in')) {
        showError('YouTube requires authentication. Please ensure cookies are set up properly.');
      }
      return;
    }
    const pct = data.percent || 0;
    document.getElementById('bar').style.width = pct + '%';
    document.getElementById('status').textContent = data.status_msg || (pct + '%');

    const linksDiv = document.getElementById('links');
    linksDiv.innerHTML = '';
    if(data.done){
      clearInterval(pollInterval);
      if(data.files && data.files.length){
        data.files.forEach(f=>{
          const a = document.createElement('a');
          a.className='link';
          a.href = '/file/' + encodeURIComponent(f);
          a.textContent = 'Download: ' + f.split('/').pop();
          linksDiv.appendChild(a);
        });
      } else if(data.file){
        const a = document.createElement('a');
        a.className='link';
        a.href = '/file/' + encodeURIComponent(data.file);
        a.textContent = 'Download: ' + data.file.split('/').pop();
        linksDiv.appendChild(a);
      } else if(data.zip){
        const a = document.createElement('a');
        a.className='link';
        a.href = '/file/' + encodeURIComponent(data.zip);
        a.textContent = 'Download ZIP';
        linksDiv.appendChild(a);
      }
      document.getElementById('log').textContent = 'Job complete';
    } else {
      document.getElementById('log').textContent = data.message || 'Downloading...';
    }
  }).catch(err=>{ console.error('progress fetch err', err); });
}

function clearUI(){
  if(pollInterval) clearInterval(pollInterval);
  downloadId = null;
  hideError();
  document.getElementById('previewArea').style.display='none';
  document.getElementById('thumb').src='';
  document.getElementById('title').textContent='';
  document.getElementById('uploader').textContent='';
  document.getElementById('views').textContent='';
  document.getElementById('duration').textContent='';
  document.getElementById('bar').style.width='0%';
  document.getElementById('status').textContent='';
  document.getElementById('links').innerHTML='';
  document.getElementById('log').textContent='';
  setLoading(false);
}
</script>
</body>
</html>
"""

def download_worker(job_id, url, fmt, mode):
    progress_store[job_id] = {"status":"started","percent":0,"done":False,"message":"Initializing"}
    try:
        # Validate URL first
        if not validate_youtube_url(url):
            progress_store[job_id].update({
                "done": True, "error": "Invalid YouTube URL format", 
                "status": "error", "status_msg": "Invalid URL"
            })
            return

        # Get yt-dlp options with cookie support
        ydl_opts = get_ydl_opts(fmt, mode, job_id)
        
        # Check if cookies file exists
        cookies_file = ydl_opts.get('cookiefile')
        if not os.path.exists(cookies_file):
            progress_store[job_id]["message"] = "No cookies file found - may encounter bot detection"

        # Progress hook
        def hook(d):
            try:
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    downloaded = d.get('downloaded_bytes', 0)
                    percent = int(downloaded / total * 100) if total and total > 0 else 0
                    eta = d.get('eta')
                    status_msg = f"{percent}%"
                    if eta:
                        status_msg += f" - ETA {eta}s"
                    
                    progress_store[job_id].update({
                        "status": "downloading",
                        "percent": percent,
                        "status_msg": status_msg,
                        "message": d.get('filename') or 'downloading'
                    })
                elif d['status'] == 'finished':
                    progress_store[job_id].update({
                        "status": "finishing",
                        "percent": 100,
                        "status_msg": "Merging/Finalizing",
                        "message": "finished"
                    })
            except Exception as e:
                # Don't crash the download on progress hook errors
                pass

        ydl_opts['progress_hooks'] = [hook]

        # Extract info with retry logic for bot detection
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(url, download=True)
                break  # Success, break out of retry loop
            except yt_dlp.DownloadError as e:
                error_str = str(e)
                if ("Sign in to confirm" in error_str or "bot" in error_str.lower()) and attempt < max_retries:
                    progress_store[job_id].update({
                        "message": f"Bot detection encountered, retrying... (attempt {attempt + 1}/{max_retries + 1})"
                    })
                    time.sleep(2)  # Wait before retry
                    continue
                else:
                    # Final attempt failed or non-retryable error
                    if "Sign in to confirm" in error_str:
                        error_msg = "YouTube bot detection triggered. Please set up cookies.txt file for authentication."
                    else:
                        error_msg = f"Download error: {error_str}"
                    progress_store[job_id].update({
                        "done": True, "error": error_msg, 
                        "status": "error", "status_msg": "Download failed"
                    })
                    return
            except Exception as e:
                progress_store[job_id].update({
                    "done": True, "error": f"Unexpected error: {str(e)}", 
                    "status": "error", "status_msg": "Download failed"
                })
                return

        # Post-download: handle playlist vs single
        if 'entries' in result:
            files = []
            for entry in result['entries']:
                if not entry: 
                    continue
                try:
                    fn = ydl.prepare_filename(entry)
                    if fmt == 'audio':
                        fn = os.path.splitext(fn)[0] + ".mp3"
                    if os.path.exists(fn):
                        files.append(os.path.abspath(fn))
                except Exception:
                    continue
            
            if files:
                zip_name = f"playlist_{job_id}.zip"
                zip_path = os.path.join(DOWNLOADS_DIR, zip_name)
                tmp_dir = os.path.join(DOWNLOADS_DIR, f"tmp_{job_id}")
                os.makedirs(tmp_dir, exist_ok=True)
                for f in files:
                    if os.path.exists(f):
                        shutil.copy(f, tmp_dir)
                shutil.make_archive(os.path.splitext(zip_path)[0], 'zip', tmp_dir)
                shutil.rmtree(tmp_dir)
                progress_store[job_id].update({
                    "done": True, "percent": 100, "files": files, 
                    "zip": os.path.basename(zip_path), "status": "done", 
                    "status_msg": "Playlist zipped"
                })
            else:
                progress_store[job_id].update({
                    "done": True, "error": "No files were downloaded from playlist", 
                    "status": "error", "status_msg": "No files downloaded"
                })
        else:
            try:
                filename = ydl.prepare_filename(result)
                if fmt == 'audio':
                    filename = os.path.splitext(filename)[0] + ".mp3"
                if os.path.exists(filename):
                    filename = os.path.abspath(filename)
                    progress_store[job_id].update({
                        "done": True, "percent": 100, 
                        "file": os.path.basename(filename), "status": "done", 
                        "status_msg": "File ready"
                    })
                else:
                    progress_store[job_id].update({
                        "done": True, "error": "Downloaded file not found", 
                        "status": "error", "status_msg": "File missing"
                    })
            except Exception as e:
                progress_store[job_id].update({
                    "done": True, "error": f"Error processing file: {str(e)}", 
                    "status": "error", "status_msg": "Processing failed"
                })
                
    except Exception as e:
        progress_store[job_id].update({
            "done": True, "error": f"Unexpected error: {str(e)}", 
            "status": "error", "status_msg": "Download failed"
        })

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML)

@rate_limit(max_per_minute=6)
@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    fmt = data.get('format', 'video')
    mode = data.get('mode', 'single')
    
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    
    # Validate URL format
    if not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL format"}), 400
    
    job_id = str(uuid.uuid4())
    progress_store[job_id] = {"status": "queued", "percent": 0, "done": False, "message": "Queued"}
    
    # Try preview with enhanced error handling
    preview = {}
    try:
        preview_opts = {
            'quiet': True, 
            'skip_download': True,
            'cookiefile': os.path.join(COOKIES_DIR, 'cookies.txt'),
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
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
        error_str = str(e)
        if "Sign in to confirm" in error_str:
            preview = {"title": None, "warning": "Authentication may be required for full access"}
        else:
            preview = {"title": None, "warning": f"Preview limited: {error_str}"}
    
    # Start download in background thread
    t = threading.Thread(target=download_worker, args=(job_id, url, fmt, mode), daemon=True)
    t.start()
    
    return jsonify({"id": job_id, "preview": preview})

@app.route('/progress/<job_id>', methods=['GET'])
def progress(job_id):
    info = progress_store.get(job_id)
    if not info:
        return jsonify({"error": "Unknown job id"}), 404
    return jsonify(info)

@app.route('/file/<path:filename>', methods=['GET'])
def serve_file(filename):
    safe = os.path.join(DOWNLOADS_DIR, filename)
    safe_abs = os.path.abspath(safe)
    if not safe_abs.startswith(os.path.abspath(DOWNLOADS_DIR)):
        abort(400)
    if not os.path.exists(safe_abs):
        abort(404)
    return send_from_directory(DOWNLOADS_DIR, os.path.basename(safe_abs), as_attachment=True)

# Cleanup old job data periodically (optional)
def cleanup_old_jobs():
    """Remove jobs older than 1 hour to prevent memory leaks"""
    current_time = time.time()
    for job_id in list(progress_store.keys()):
        job = progress_store[job_id]
        if job.get('done') and current_time - job.get('_timestamp', current_time) > 3600:
            del progress_store[job_id]

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)