# app.py
from flask import Flask, render_template_string, request, jsonify, send_from_directory, abort
import yt_dlp
import os
import threading
import uuid
import shutil

app = Flask(__name__, static_folder="downloads")

# In-memory job store: job_id -> status dict
progress_store = {}

# Downloads folder (absolute)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# HTML template (responsive, includes JS to poll progress)
HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>YouTube Downloader</title>
  <style>
    :root{--bg:#071021;--card:#0e1722;--muted:#9aa6b2;--accent:#06b6d4;--text:#e6eef3;--btn:#10b981}
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
    footer{margin-top:20px;text-align:center;color:var(--muted);font-size:13px}
    @media (max-width:640px){.preview{flex-direction:column}.thumb{width:100%}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>YouTube Downloader (Flask)</h1>
      <p class="muted">Paste a YouTube or playlist URL, preview metadata, choose MP4 or MP3, and download. Mobile-friendly.</p>

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
          <button class="primary" type="submit">Start Download</button>
          <button class="ghost" type="button" onclick="clearUI()">Clear</button>
        </div>
      </form>

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

    <footer>Tip: install ffmpeg for merging/conversion (required for MP3 and best video output).</footer>
  </div>

<script>
let pollInterval = null;
let downloadId = null;

function startDownload(ev){
  ev.preventDefault();
  const url = document.getElementById('url').value.trim();
  const format = document.getElementById('format').value;
  const mode = document.getElementById('mode').value;

  if(!url) return alert('Enter a URL');

  fetch('/start', {
    method: 'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url, format, mode})
  }).then(r=>r.json()).then(data=>{
    if(data.error) return alert(data.error);
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
  }).catch(err=>{ alert('Start error: ' + err); });
}

function fetchProgress(id){
  fetch('/progress/' + id).then(r=>r.json()).then(data=>{
    if(data.error){
      document.getElementById('status').textContent = data.error;
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
}
</script>
</body>
</html>
"""

def download_worker(job_id, url, fmt, mode):
    progress_store[job_id] = {"status":"started","percent":0,"done":False,"message":"Initializing"}
    try:
        # metadata preview
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)

        # progress hook
        def hook(d):
            try:
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    downloaded = d.get('downloaded_bytes', 0)
                    percent = int(downloaded / total * 100) if total else 0
                    progress_store[job_id].update({
                        "status":"downloading","percent":percent,
                        "status_msg": d.get('eta') and f"{percent}% - ETA {d.get('eta')}s" or f"{percent}%",
                        "message": d.get('filename') or 'downloading'
                    })
                elif d['status'] == 'finished':
                    progress_store[job_id].update({
                        "status":"finishing","percent":100,"status_msg":"Merging/Finalizing","message":"finished"
                    })
            except Exception:
                pass

        outtmpl = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
        ydl_opts = {'outtmpl': outtmpl, 'progress_hooks':[hook], 'noplaylist': (mode == 'single')}

        if fmt == 'video':
            ydl_opts.update({'format':'bestvideo+bestaudio/best','merge_output_format':'mp4'})
        else:
            ydl_opts.update({
                'format':'bestaudio/best',
                'postprocessors':[{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}]
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=True)

        # Post-download: handle playlist vs single
        if 'entries' in result:
            files = []
            for entry in result['entries']:
                if not entry: continue
                fn = ydl.prepare_filename(entry)
                if fmt == 'audio':
                    fn = os.path.splitext(fn)[0] + ".mp3"
                files.append(os.path.abspath(fn))
            zip_name = f"playlist_{job_id}.zip"
            zip_path = os.path.join(DOWNLOADS_DIR, zip_name)
            tmp_dir = os.path.join(DOWNLOADS_DIR, f"tmp_{job_id}")
            os.makedirs(tmp_dir, exist_ok=True)
            for f in files:
                if os.path.exists(f): shutil.copy(f, tmp_dir)
            shutil.make_archive(os.path.splitext(zip_path)[0], 'zip', tmp_dir)
            shutil.rmtree(tmp_dir)
            progress_store[job_id].update({"done":True,"percent":100,"files":files,"zip":os.path.basename(zip_path),"status":"done","status_msg":"Playlist zipped"})
        else:
            filename = ydl.prepare_filename(result)
            if fmt == 'audio':
                filename = os.path.splitext(filename)[0] + ".mp3"
            filename = os.path.abspath(filename)
            progress_store[job_id].update({"done":True,"percent":100,"file":os.path.basename(filename),"status":"done","status_msg":"File ready"})
    except Exception as e:
        progress_store[job_id].update({"done":True,"percent":progress_store[job_id].get('percent',0),"error":str(e),"status":"error","status_msg":"Error occurred"})

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(force=True)
    url = data.get('url')
    fmt = data.get('format', 'video')
    mode = data.get('mode', 'single')
    if not url:
        return jsonify({"error":"Missing URL"}), 400
    job_id = str(uuid.uuid4())
    progress_store[job_id] = {"status":"queued","percent":0,"done":False,"message":"Queued"}
    # try preview
    preview = {}
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                first = info['entries'][0] if info['entries'] else {}
                preview = {"title": first.get('title'), "uploader": first.get('uploader'), "thumbnail": first.get('thumbnail'), "view_count": first.get('view_count'), "duration": first.get('duration')}
            else:
                preview = {"title": info.get('title'), "uploader": info.get('uploader'), "thumbnail": info.get('thumbnail'), "view_count": info.get('view_count'), "duration": info.get('duration')}
    except Exception as e:
        preview = {"title": None, "error": str(e)}
    t = threading.Thread(target=download_worker, args=(job_id, url, fmt, mode), daemon=True)
    t.start()
    return jsonify({"id":job_id, "preview": preview})

@app.route('/progress/<job_id>', methods=['GET'])
def progress(job_id):
    info = progress_store.get(job_id)
    if not info:
        return jsonify({"error":"Unknown job id"}), 404
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
