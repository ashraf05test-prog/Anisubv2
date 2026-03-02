import os
import requests
import subprocess
import threading
import uuid
import time
import json
import shutil
from flask import Flask, render_template, request, jsonify, send_file
from extractor import extract_from_episode_page
from translator import convert_vtt_to_srt, translate_google, translate_gemini
from uploader import upload_to_telegram

FONTS = {
    'Noto Sans Bengali': 'https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansBengali/NotoSansBengali-Regular.ttf',
    'Kalpurush': 'https://github.com/googlefonts/kalpurush/raw/main/fonts/ttf/Kalpurush.ttf',
    'SolaimanLipi': 'https://raw.githubusercontent.com/maateen/bangla-web-fonts/master/fonts/SolaimanLipi/SolaimanLipi.ttf',
}

def setup_fonts():
    os.makedirs('/tmp/fonts', exist_ok=True)
    for name, url in FONTS.items():
        path = f'/tmp/fonts/{name}.ttf'
        if not os.path.exists(path):
            try:
                r = requests.get(url, timeout=30)
                open(path, 'wb').write(r.content)
                print(f'Font downloaded: {name}')
            except Exception as e:
                print(f'Font failed: {name}: {e}')
    subprocess.run(['fc-cache', '-fv', '/tmp/fonts'], capture_output=True)

setup_fonts()


def srt_to_ass(srt_path, ass_path, font_name='Noto Sans Bengali', font_size=24,
               color='White', position='bottom', font_style='Normal', bg='None'):
    """Convert SRT to ASS format for proper Bengali rendering"""
    import re

    # Color map — ASS uses BGR hex
    color_map = {
        'White': '&H00FFFFFF',
        'Yellow': '&H0000FFFF',
        'Cyan': '&H00FFFF00',
        'white': '&H00FFFFFF',
        'yellow': '&H0000FFFF',
        'cyan': '&H00FFFF00',
    }
    p_color = color_map.get(color, '&H00FFFFFF')

    align_map = {'bottom': 2, 'middle': 5, 'top': 8}
    align = align_map.get(position, 2)

    bold = -1 if font_style == 'Bold' else 0
    italic = -1 if font_style == 'Italic' else 0

    if bg in ('Semi-transparent', 'semi'):
        border_style, back_color = 3, '&H80000000'
    elif bg in ('Black box', 'black'):
        border_style, back_color = 3, '&H00000000'
    else:
        border_style, back_color = 1, '&H00000000'

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{p_color},&H000000FF,&H00000000,{back_color},{bold},{italic},0,0,100,100,0,0,{border_style},1,0,{align},10,10,25,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def srt_time_to_ass(t):
        t = t.strip().replace(',', '.')
        parts = t.split(':')
        return f"{parts[0]}:{parts[1]}:{parts[2]}"

    srt_text = open(srt_path, encoding='utf-8').read().strip()
    blocks = re.split(r'\n\s*\n', srt_text)
    events = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        try:
            ts = lines[1].split(' --> ')
            text = r'\N'.join(lines[2:])
            # Remove HTML tags
            text = re.sub(r'<[^>]+>', '', text)
            events.append(f"Dialogue: 0,{srt_time_to_ass(ts[0])},{srt_time_to_ass(ts[1])},Default,,0,0,0,,{text}")
        except:
            continue

    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(header + '\n'.join(events))
    return ass_path


app = Flask(__name__)
os.makedirs('/tmp/anisub', exist_ok=True)

tasks = {}


@app.route('/')
def index():
    return render_template('index.html')


# Support both /extract and /api/extract
@app.route('/extract', methods=['POST'])
@app.route('/api/extract', methods=['POST'])
def extract():
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    cookie_path = '/tmp/anisub/cookies.txt' if os.path.exists('/tmp/anisub/cookies.txt') else None
    result = extract_from_episode_page(url, cookie_path)
    # Also return m3u8 and subtitle as simple keys for new frontend
    result['m3u8'] = result.get('m3u8_url')
    result['subtitle'] = result['subtitles'][0]['url'] if result.get('subtitles') else None
    return jsonify(result)


@app.route('/upload_sub', methods=['POST'])
def upload_sub():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    path = f"/tmp/anisub/{uuid.uuid4()}_{file.filename}"
    file.save(path)
    return jsonify({'path': path, 'filename': file.filename})


@app.route('/upload_cookie', methods=['POST'])
def upload_cookie():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    file.save('/tmp/anisub/cookies.txt')
    return jsonify({'ok': True})


# Support both /start and /api/start
@app.route('/start', methods=['POST'])
@app.route('/api/start', methods=['POST'])
def start_task():
    # Handle both JSON and FormData
    if request.content_type and 'multipart' in request.content_type:
        data = request.form.to_dict()
        # Handle file uploads
        if 'sub_file' in request.files:
            f = request.files['sub_file']
            path = f"/tmp/anisub/{uuid.uuid4()}_{f.filename}"
            f.save(path)
            data['sub_file_path'] = path
        if 'translate_file' in request.files:
            f = request.files['translate_file']
            path = f"/tmp/anisub/{uuid.uuid4()}_{f.filename}"
            f.save(path)
            data['trans_sub_file'] = path
        # Map new frontend keys to backend keys
        data['video_url'] = data.get('video_url', '')
        data['sub_type'] = data.get('sub_mode', data.get('sub_type', 'url'))
        data['sub_url'] = data.get('sub_url', '')
        data['trans_sub_url'] = data.get('translate_url', data.get('trans_sub_url', ''))
        data['trans_engine'] = data.get('translate_engine', data.get('trans_engine', 'google'))
        data['trans_lang'] = 'bn'
        data['gemini_api_key'] = data.get('gemini_key', data.get('gemini_api_key', ''))
        data['tg_title'] = data.get('title', data.get('tg_title', 'AniSub Video'))
        data['tg_caption'] = data.get('caption', data.get('tg_caption', ''))
        # Subtitle style
        data['font_name'] = data.get('font_name', 'Noto Sans Bengali')
        data['font_size'] = data.get('font_size', '24')
        data['color'] = data.get('font_color', data.get('color', 'White'))
        data['font_style'] = data.get('font_style', 'Normal')
        data['position'] = data.get('position', 'bottom')
        data['bg'] = data.get('background', data.get('bg', 'None'))
    else:
        data = request.json or {}

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        'status': 'Downloading',
        'stage': 'download',
        'progress': 0,
        'logs': [],
        'tg_link': None,
        'post_link': None,
        'error': None,
        'output_path': None,
        'has_preview': False
    }

    thread = threading.Thread(target=process_task, args=(task_id, data), daemon=False)
    thread.start()

    return jsonify({'task_id': task_id})


# Support both /status/<id> and /api/status/<id>
@app.route('/status/<task_id>', methods=['GET'])
@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    if task_id not in tasks:
        return jsonify({'error': 'Task not found'}), 404

    offset = int(request.args.get('offset', 0))
    task = tasks[task_id]

    # Map status to stage for new frontend
    status_to_stage = {
        'Downloading': 'download',
        'Subtitle': 'translate',
        'Processing': 'process',
        'Uploading': 'upload',
        'Done': 'done',
        'Error': 'error'
    }

    return jsonify({
        'status': task['status'].lower() if task['status'] in ('Done', 'Error') else task['status'],
        'stage': status_to_stage.get(task['status'], 'download'),
        'progress': task['progress'],
        'logs': task['logs'][offset:],
        'tg_link': task['tg_link'],
        'post_link': task.get('post_link'),
        'error': task['error'],
        'has_preview': task['has_preview']
    })


@app.route('/preview/<task_id>')
def preview(task_id):
    if task_id in tasks and tasks[task_id]['has_preview']:
        return send_file(tasks[task_id]['output_path'])
    return "Not found", 404


def get_duration(file_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(res.stdout.strip())
    except:
        return None


def parse_time_to_sec(time_str):
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0


def process_task(task_id, data):
    task = tasks[task_id]

    def log(msg, icon="ℹ️"):
        t = time.strftime("%H:%M:%S")
        task['logs'].append(f"[{t}] {icon} {msg}")

    try:
        log("Task started", "🚀")

        # STEP 1 - Download
        task['status'] = 'Downloading'
        task['stage'] = 'download'
        video_url = data.get('video_url', '')
        iframe_url = data.get('iframe_url', '')
        cookie_path = '/tmp/anisub/cookies.txt' if os.path.exists('/tmp/anisub/cookies.txt') else None
        raw_video_path = f"/tmp/anisub/{task_id}_raw.mp4"
        downloaded = False

        for attempt_url, label in [(iframe_url, 'iframe'), (video_url, 'm3u8')]:
            if downloaded or not attempt_url:
                continue
            if shutil.which('yt-dlp'):
                log(f"yt-dlp trying {label}: {attempt_url[:60]}...", "⬇️")
                cmd = ['yt-dlp', '-f', 'bestvideo[height<=1080]+bestaudio/best',
                       '--merge-output-format', 'mp4',
                       '-o', raw_video_path, '--no-playlist']
                if cookie_path:
                    cmd += ['--cookies', cookie_path]
                cmd.append(attempt_url)
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in iter(proc.stdout.readline, ''):
                    l = line.strip()
                    if '[download]' in l and '%' in l:
                        try:
                            pct = float(l.split('%')[0].split()[-1])
                            task['progress'] = int(pct * 0.25)
                        except:
                            pass
                    if l:
                        task['logs'].append(f"[YT-DLP] {l}")
                proc.wait()
                if os.path.exists(raw_video_path) and os.path.getsize(raw_video_path) > 1024 * 1024:
                    downloaded = True
                    log(f"Downloaded via yt-dlp ({label})", "✅")

        if not downloaded and video_url:
            log(f"FFmpeg trying: {video_url[:60]}...", "⬇️")
            cmd = ['ffmpeg', '-y', '-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                   '-headers', f'Referer: {video_url}',
                   '-i', video_url, '-c', 'copy', raw_video_path]
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
            for line in iter(proc.stderr.readline, ''):
                l = line.strip()
                if l:
                    task['logs'].append(f"[FFMPEG-DL] {l}")
                    if 'time=' in l:
                        task['progress'] = 15
            proc.wait()
            if os.path.exists(raw_video_path) and os.path.getsize(raw_video_path) > 1024 * 1024:
                downloaded = True
                task['progress'] = 25
                log("Downloaded via FFmpeg", "✅")

        if not downloaded:
            raise Exception("All download methods failed")

        # STEP 2 - Subtitle
        task['status'] = 'Subtitle'
        task['stage'] = 'translate'
        log("Processing subtitle...", "📝")

        sub_type = data.get('sub_type', 'url')
        srt_content = ""

        if sub_type == 'file':
            sub_path = data.get('sub_file_path')
            if sub_path and os.path.exists(sub_path):
                with open(sub_path, 'r', encoding='utf-8') as f:
                    srt_content = f.read()
                if sub_path.endswith('.vtt') or 'WEBVTT' in srt_content:
                    srt_content = convert_vtt_to_srt(srt_content)
                log("Subtitle loaded from file", "✅")

        elif sub_type == 'url':
            sub_url = data.get('sub_url', '')
            if sub_url:
                res = requests.get(sub_url, timeout=15)
                srt_content = res.text
                if '.vtt' in sub_url or 'WEBVTT' in srt_content:
                    srt_content = convert_vtt_to_srt(srt_content)
                log("Subtitle downloaded from URL", "✅")

        elif sub_type == 'translate':
            engine = data.get('trans_engine', 'google')
            src_file = data.get('trans_sub_file', '')
            src_url = data.get('trans_sub_url', '')
            api_key = data.get('gemini_api_key', '')
            dest_lang = data.get('trans_lang', 'bn')

            src_content = ""
            if src_file and os.path.exists(src_file):
                with open(src_file, 'r', encoding='utf-8') as f:
                    src_content = f.read()
            elif src_url:
                res = requests.get(src_url, timeout=15)
                src_content = res.text

            if src_content:
                if 'WEBVTT' in src_content or src_url.endswith('.vtt'):
                    src_content = convert_vtt_to_srt(src_content)
                log(f"Translating via {engine}...", "🔄")
                if engine == 'gemini' and api_key:
                    srt_content = translate_gemini(src_content, api_key, dest_lang)
                else:
                    srt_content = translate_google(src_content, dest_lang)
                log("Translation complete", "✅")

        task['progress'] = 35

        final_video_path = f"/tmp/anisub/{task_id}_final.mp4"
        sub_file_path = f"/tmp/anisub/{task_id}.srt"

        if srt_content:
            with open(sub_file_path, 'w', encoding='utf-8') as f:
                f.write(srt_content)

            # STEP 3 - FFmpeg burn with ASS filter for Bengali
            task['status'] = 'Processing'
            task['stage'] = 'process'
            log("Burning subtitles (ASS filter for Bengali)...", "🔥")

            duration = get_duration(raw_video_path)

            font_name = data.get('font_name', 'Noto Sans Bengali')
            font_size = data.get('font_size', '24')
            color = data.get('color', 'White')
            bg = data.get('bg', 'None')
            position = data.get('position', 'bottom')
            font_style = data.get('font_style', 'Normal')

            # Convert SRT to ASS using FFmpeg (most reliable for Bengali)
            ass_file_path = f"/tmp/anisub/{task_id}.ass"
            conv = subprocess.run(
                ['ffmpeg', '-y', '-i', sub_file_path, ass_file_path],
                capture_output=True, text=True
            )
            if conv.returncode != 0 or not os.path.exists(ass_file_path):
                # Fallback to manual conversion
                srt_to_ass(sub_file_path, ass_file_path,
                    font_name=font_name, font_size=int(str(font_size)),
                    color=color, position=position, font_style=font_style, bg=bg)
                log("ASS converted (manual)", "✅")
            else:
                log("ASS converted via FFmpeg", "✅")

            # scale=1280:-2 for speed + ass filter for Bengali
            sub_filter = f"scale=1280:-2,ass='{ass_file_path}':fontsdir=/tmp/fonts/"

            cmd = ['ffmpeg', '-y', '-i', raw_video_path,
                   '-vf', sub_filter,
                   '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                   '-threads', '0', '-c:a', 'copy',
                   '-max_muxing_queue_size', '1024',
                   final_video_path]

            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
            for line in iter(proc.stderr.readline, ''):
                l = line.strip()
                if l:
                    task['logs'].append(f"[FFMPEG] {l}")
                    if 'time=' in l and duration:
                        try:
                            sec = parse_time_to_sec(l.split('time=')[1].split()[0])
                            task['progress'] = 35 + int((sec / duration) * 40)
                        except:
                            pass
            proc.wait()

            if proc.returncode != 0 or not os.path.exists(final_video_path):
                log("FFmpeg burn failed, using raw video", "⚠️")
                shutil.copy(raw_video_path, final_video_path)
        else:
            log("No subtitle, skipping burn", "⚠️")
            shutil.copy(raw_video_path, final_video_path)
            task['progress'] = 75

        task['output_path'] = final_video_path
        task['has_preview'] = True

        # STEP 4 - Telegram upload
        if task.get('tg_link') or task.get('uploading'):
            log("Already uploaded, skipping", "⚠️")
            return

        task['uploading'] = True
        task['status'] = 'Uploading'
        task['stage'] = 'upload'
        log("Uploading to Telegram...", "☁️")

        title = data.get('tg_title', 'AniSub Video')
        caption = data.get('tg_caption', '')

        def prog_cb(pct):
            task['progress'] = 75 + int(pct * 0.25)
            if pct % 10 == 0:
                log(f"Upload: {pct}%", "📤")

        tg_link = upload_to_telegram(final_video_path, title, caption, prog_cb)
        task['tg_link'] = tg_link
        task['post_link'] = tg_link

        # STEP 5 - Done
        task['progress'] = 100
        task['status'] = 'Done'
        task['stage'] = 'done'
        log("Task completed!", "✅")

        def cleanup():
            threading.Event().wait(3600)
            for p in [raw_video_path, final_video_path, sub_file_path, f"/tmp/anisub/{task_id}.ass"]:
                try:
                    os.remove(p)
                except:
                    pass

        threading.Thread(target=cleanup, daemon=False).start()

    except Exception as e:
        task['status'] = 'Error'
        task['stage'] = 'error'
        task['error'] = str(e)
        log(f"Task failed: {e}", "❌")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
