# app.py
import os
import atexit
from flask import Flask, render_template_string, request, jsonify
import threading
import time
import re
from functools import wraps

app = Flask(__name__)

# Configuration
request_times = {}

def rate_limit(max_per_minute=15):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user_ip = request.remote_addr or 'unknown'
            current_time = time.time()
            
            window_start = current_time - 60
            if user_ip in request_times:
                request_times[user_ip] = [t for t in request_times[user_ip] if t > window_start]
            else:
                request_times[user_ip] = []
            
            if len(request_times[user_ip]) >= max_per_minute:
                return jsonify({
                    "error": f"Too many requests. Maximum {max_per_minute} requests per minute."
                }), 429
            
            request_times[user_ip].append(current_time)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def validate_youtube_url(url):
    patterns = [
        r'^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/embed/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/(playlist|watch)\?.*list=[\w-]+',
    ]
    
    if not url.startswith(('http://', 'https://')):
        return False
        
    malicious_patterns = [r'\.\./', r'file://', r'javascript:', r'data:', r'vbscript:']
    for pattern in malicious_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return False
    
    return any(re.match(pattern, url, re.IGNORECASE) for pattern in patterns)

def extract_video_id(url):
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})',
        r'(?:youtube\.com/embed/)([\w-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

# FIXED: Complete implementation of create_client_command
def create_client_command(url, fmt, mode, platform):
    """Generate download command for different platforms"""
    if platform == "windows":
        base_command = "python -m yt_dlp"
    elif platform == "mobile":
        base_command = "pkg install python -y && pip install yt-dlp && python -m yt_dlp"
    else:  # mac, linux
        base_command = "python3 -m yt_dlp"
    
    # Format options
    if fmt == 'video':
        format_option = 'best[height<=1080]/best[height<=720]/best'
        command_parts = [base_command, f'-f "{format_option}"', '--merge-output-format', 'mp4']
    else:  # audio
        command_parts = [base_command, '-f', 'bestaudio/best', '--extract-audio', '--audio-format', 'mp3']
        
    # Mode options
    if mode == 'single':
        command_parts.append('--no-playlist')
    else:
        command_parts.append('--yes-playlist')
    
    # Platform-specific options
    if platform == "windows":
        command_parts.append('--no-check-certificate')
    elif platform == "mobile":
        command_parts.append('--no-check-certificate')
        command_parts.append('--compat-options no-certifi')
    
    # Add the URL
    command_parts.append(f'"{url}"')
    
    return ' '.join(command_parts)

def generate_desktop_command(url, fmt, mode):
    base_command = "python -m yt_dlp"
    
    if fmt == 'video':
        format_option = 'best[height<=1080]/best'
        command_parts = [base_command, '-f', f'"{format_option}"', '--merge-output-format', 'mp4']
    else:
        command_parts = [base_command, '-f', 'bestaudio/best', '--extract-audio', '--audio-format', 'mp3']
    
    if mode == 'single':
        command_parts.append('--no-playlist')
    else:
        command_parts.append('--yes-playlist')
    
    command_parts.append('# Add: --cookies cookies.txt for better success')
    command_parts.append(f'"{url}"')
    return ' '.join(command_parts)

def generate_online_tools(video_id):
    return {
        "method": "online_converters",
        "instructions": "Click any link below to download directly from online converters",
        "tools": [
            {
                "name": "SSYouTube",
                "url": f"https://ssyoutube.com/watch?v/{video_id}",
                "description": "Fast and reliable YouTube video downloader"
            },
            {
                "name": "SaveFrom.net",
                "url": f"https://savefrom.net/watch?v={video_id}",
                "description": "Browser extension method"
            }
        ]
    }

HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>YouTube Download Assistant</title>
  <style>
    :root{--bg:#071021;--card:#0e1722;--muted:#9aa6b2;--accent:#06b6d4;--text:#e6eef3;--btn:#10b981;--error:#ef4444}
    html,body{height:100%;margin:0;font-family:Inter,-apple-system,sans-serif;background:linear-gradient(180deg,#071021,#0a1220);color:var(--text);}
    .wrap{max-width:920px;margin:0 auto;padding:16px}
    .card{background:linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));padding:18px;border-radius:12px;box-shadow:0 6px 18px rgba(2,6,23,0.6);margin-bottom:20px;}
    h1{margin:0 0 8px;font-size:20px}
    p.muted{color:var(--muted);margin-top:4px;font-size:14px;}
    label{display:block;margin-top:12px;font-size:14px;color:var(--muted)}
    input[type=url]{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:var(--text);box-sizing:border-box;font-size:16px;}
    select{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:var(--text);font-size:16px;}
    .row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
    button{font-size:16px;padding:14px 20px;border-radius:10px;border:none;font-weight:600;cursor:pointer;min-height:44px;}
    button.primary{background:var(--btn);color:#04201b;}
    button:disabled{opacity:0.6;cursor:not-allowed}
    .error{color:var(--error);background:rgba(239,68,68,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(239,68,68,0.3)}
    .info-box{background:rgba(6,182,212,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(6,182,212,0.3)}
    .success{color:var(--btn);background:rgba(16,185,129,0.1);padding:12px;border-radius:8px;margin-top:10px;border:1px solid rgba(16,185,129,0.3)}
    pre{background:#1a1a1a;padding:15px;border-radius:8px;overflow-x:auto;color:var(--text);font-size:14px;}
    code{background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:4px;font-size:14px;}
    .copy-btn{background:var(--accent);color:white;border:none;padding:12px 16px;border-radius:6px;cursor:pointer;font-size:14px;margin-top:10px;width:100%;}
    .tab-container{display:flex;gap:8px;margin-bottom:16px;border-bottom:1px solid rgba(255,255,255,0.06);padding-bottom:12px;flex-wrap:wrap;}
    .tab{padding:12px 16px;border-radius:8px;background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:14px;min-height:44px;flex:1;}
    .tab.active{background:var(--accent);color:white;}
    .tab-content{display:none;}
    .tab-content.active{display:block;}
    .platform-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:12px 0;}
    .platform-btn{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);padding:12px;border-radius:8px;cursor:pointer;text-align:center;}
    .platform-btn.active{background:var(--accent);border-color:var(--accent);}
    .tool-card{background:rgba(255,255,255,0.05);padding:15px;border-radius:8px;margin:10px 0;}
    .tool-name{font-weight:bold;color:var(--accent);margin-bottom:5px;}
    .tool-desc{color:var(--muted);font-size:0.9em;margin-bottom:10px;}
    @media (max-width:768px){.wrap{padding:12px}.card{padding:16px}.tab-container{flex-direction:column}.platform-options{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🎬 YouTube Download Assistant</h1>
      <p class="muted">Download YouTube videos directly to your device</p>

      <div class="tab-container">
        <button class="tab active" onclick="switchTab('client-tab')">📱 Client Download</button>
        <button class="tab" onclick="switchTab('desktop-tab')">💻 Desktop Commands</button>
        <button class="tab" onclick="switchTab('online-tab')">🌐 Online Tools</button>
      </div>

      <div id="client-tab" class="tab-content active">
        <form onsubmit="generateClientCommand(event)">
          <label>YouTube URL</label>
          <input id="clientUrl" type="url" placeholder="https://www.youtube.com/watch?v=..." required>
          
          <div class="row">
            <div style="flex:1">
              <label>Format</label>
              <select id="clientFormat">
                <option value="video">Video (MP4)</option>
                <option value="audio">Audio (MP3)</option>
              </select>
            </div>
            <div style="flex:1">
              <label>Mode</label>
              <select id="clientMode">
                <option value="single">Single Video</option>
                <option value="playlist">Playlist</option>
              </select>
            </div>
          </div>

          <label style="margin-top:16px;">Select Your Platform:</label>
          <div class="platform-options">
            <div class="platform-btn active" onclick="selectPlatform('windows', this)">Windows</div>
            <div class="platform-btn" onclick="selectPlatform('mac', this)">Mac</div>
            <div class="platform-btn" onclick="selectPlatform('linux', this)">Linux</div>
            <div class="platform-btn" onclick="selectPlatform('mobile', this)">Mobile</div>
          </div>
          
          <button class="primary" type="submit" style="margin-top:16px;width:100%;">
            Generate Download Command
          </button>
        </form>
        
        <div id="clientCommandResult" style="display:none;margin-top:16px;">
          <div class="info-box">
            <strong>Download Command:</strong>
            <pre id="clientCommandOutput"></pre>
            <button class="copy-btn" onclick="copyClientCommand()">📋 Copy Command</button>
          </div>
        </div>
      </div>

      <div id="desktop-tab" class="tab-content">
        <form onsubmit="generateLocalCommand(event)">
          <label>YouTube URL</label>
          <input id="localUrl" type="url" placeholder="https://www.youtube.com/watch?v=..." required>
          
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
          
          <button class="primary" type="submit" style="margin-top:16px;width:100%;">
            Generate Desktop Command
          </button>
        </form>
        
        <div id="localCommandResult" style="display:none;margin-top:16px;">
          <div class="info-box">
            <strong>Desktop Download Command:</strong>
            <pre id="commandOutput"></pre>
            <button class="copy-btn" onclick="copyCommand()">📋 Copy Command</button>
          </div>
        </div>
      </div>

      <div id="online-tab" class="tab-content">
        <form onsubmit="generateOnlineTools(event)">
          <label>YouTube URL</label>
          <input id="onlineUrl" type="url" placeholder="https://www.youtube.com/watch?v=..." required>
          <button class="primary" type="submit" style="margin-top:16px;width:100%;">
            Get Online Converter Links
          </button>
        </form>
        
        <div id="onlineResult" style="display:none;margin-top:16px;"></div>
      </div>
    </div>
  </div>

<script>
let selectedPlatform = 'windows';

function switchTab(tabName) {
  document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
  document.getElementById(tabName).classList.add('active');
  event.target.classList.add('active');
}

function selectPlatform(platform, element) {
  selectedPlatform = platform;
  document.querySelectorAll('.platform-btn').forEach(btn => btn.classList.remove('active'));
  element.classList.add('active');
}

function generateClientCommand(ev) {
  ev.preventDefault();
  const url = document.getElementById('clientUrl').value.trim();
  const format = document.getElementById('clientFormat').value;
  const mode = document.getElementById('clientMode').value;

  if(!url) {
    showError('Please enter a YouTube URL');
    return;
  }

  const submitBtn = ev.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.textContent = 'Generating...';

  fetch('/generate-client-command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, format, mode, platform: selectedPlatform})
  }).then(r => r.json()).then(data => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate Download Command';
    
    if (data.error) {
      showError(data.error);
      return;
    }
    
    document.getElementById('clientCommandOutput').textContent = data.command;
    document.getElementById('clientCommandResult').style.display = 'block';
    document.getElementById('clientCommandResult').scrollIntoView({ behavior: 'smooth' });
    hideError();
  }).catch(err => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate Download Command';
    showError('Error generating command');
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
  }).then(r => r.json()).then(data => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate Desktop Command';
    
    if (data.error) {
      showError(data.error);
      return;
    }
    
    document.getElementById('commandOutput').textContent = data.command;
    document.getElementById('localCommandResult').style.display = 'block';
    document.getElementById('localCommandResult').scrollIntoView({ behavior: 'smooth' });
    hideError();
  }).catch(err => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate Desktop Command';
    showError('Error generating command');
  });
}

function generateOnlineTools(ev) {
  ev.preventDefault();
  const url = document.getElementById('onlineUrl').value.trim();

  if(!url) {
    showError('Please enter a YouTube URL');
    return;
  }

  const submitBtn = ev.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.textContent = 'Getting Tools...';

  fetch('/online-tools', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url})
  }).then(r => r.json()).then(data => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Get Online Converter Links';
    
    if (data.error) {
      showError(data.error);
      return;
    }
    
    const result = document.getElementById('onlineResult');
    result.innerHTML = `
      <div class="success">
        <h3>Online Download Tools</h3>
        <p>${data.instructions}</p>
        ${data.tools.map(tool => `
          <div class="tool-card">
            <div class="tool-name">${tool.name}</div>
            <div class="tool-desc">${tool.description}</div>
            <a href="${tool.url}" target="_blank" style="background:var(--accent);color:white;padding:10px 15px;border-radius:6px;text-decoration:none;display:inline-block;">
              Open ${tool.name}
            </a>
          </div>
        `).join('')}
      </div>
    `;
    result.style.display = 'block';
    hideError();
  }).catch(err => {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Get Online Converter Links';
    showError('Error getting tools');
  });
}

function copyClientCommand() {
  copyToClipboard(document.getElementById('clientCommandOutput').textContent);
}

function copyCommand() {
  copyToClipboard(document.getElementById('commandOutput').textContent);
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    alert('Command copied to clipboard!');
  });
}

function showError(message) {
  let errorDiv = document.getElementById('errorArea');
  if (!errorDiv) {
    errorDiv = document.createElement('div');
    errorDiv.id = 'errorArea';
    errorDiv.className = 'error';
    document.querySelector('.card').appendChild(errorDiv);
  }
  errorDiv.textContent = message;
  errorDiv.style.display = 'block';
}

function hideError() {
  const errorDiv = document.getElementById('errorArea');
  if (errorDiv) errorDiv.style.display = 'none';
}

document.querySelectorAll('input[type="url"]').forEach(input => {
  input.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') {
      const activeTab = document.querySelector('.tab-content.active').id;
      if (activeTab === 'client-tab') generateClientCommand(new Event('submit'));
      else if (activeTab === 'desktop-tab') generateLocalCommand(new Event('submit'));
      else if (activeTab === 'online-tab') generateOnlineTools(new Event('submit'));
    }
  });
});
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)

@rate_limit()
@app.route('/generate-client-command', methods=['POST'])
def generate_client_command():
    data = request.get_json()
    url = data.get('url', '').strip()
    fmt = data.get('format', 'video')
    mode = data.get('mode', 'single')
    platform = data.get('platform', 'windows')
    
    if not url or not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"})
    
    # Call the actual command generation function
    command = create_client_command(url, fmt, mode, platform)
    return jsonify({"command": command})

@rate_limit()
@app.route('/generate-command', methods=['POST'])
def generate_command():
    data = request.get_json()
    url = data.get('url', '').strip()
    fmt = data.get('format', 'video')
    mode = data.get('mode', 'single')
    
    if not url or not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"})
    
    command = generate_desktop_command(url, fmt, mode)
    return jsonify({"command": command})

@rate_limit()
@app.route('/online-tools', methods=['POST'])
def online_tools():
    data = request.get_json()
    url = data.get('url', '').strip()
    
    if not url or not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"})
    
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Could not extract video ID"})
    
    # Return both tools with their direct URLs
    tools = {
        "method": "online_converters", 
        "instructions": "Click any link below to download directly from online converters",
        "tools": [
            {
                "name": "SSYouTube",
                "url": f"https://ssyoutube.com/{video_id}",
                "description": "Fast and reliable YouTube video downloader"
            },
            {
                "name": "SaveFrom.net", 
                "url": f"https://savefrom.net/watch?v={video_id}",
                "description": "Browser extension method"
            }
        ]
    }
    return jsonify(tools)

if __name__ == "__main__":
    print("🚀 YouTube Download Assistant Started")
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port, debug=False)