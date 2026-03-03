import os
import re
import requests
import subprocess
import threading
import uuid
import time
import shutil
from flask import Flask, render_template, request, jsonify, send_file
from extractor import extract_from_episode_page
from translator import convert_vtt_to_srt, translate_google, translate_gemini
from uploader import upload_to_telegram

FONTS = {
    'Noto Sans Bengali': 'https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansBengali/NotoSansBengali-Regular.ttf',
    'Kalpurush': 'https://github.com/googlefonts/kalpurush/raw/main/fonts/ttf/Kalpurush.ttf',
    'SolaimanLipi': 'https://raw.githubusercontent.com/maateen/bangla-web-fonts/master/fonts/SolaimanLipi/SolaimanLipi.ttf',
    'Hind Siliguri': 'https://github.com/googlefonts/hind/raw/main/fonts/Hind_Siliguri/HindSiliguri-Regular.ttf',
    'Hind Siliguri Bold': 'https://github.com/googlefonts/hind/raw/main/fonts/Hind_Siliguri/HindSiliguri-Bold.ttf',
    'Baloo Da 2': 'https://github.com/googlefonts/Baloo2/raw/main/fonts/ttf/BalooDa2-Regular.ttf',
    'Baloo Da 2 Bold': 'https://github.com/googlefonts/Baloo2/raw/main/fonts/ttf/BalooDa2-Bold.ttf',
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
    color_map = {'White':'&H00FFFFFF','Yellow':'&H0000FFFF','Cyan':'&H00FFFF00',
                 'white':'&H00FFFFFF','yellow':'&H0000FFFF','cyan':'&H00FFFF00'}
    p_color = color_map.get(color, '&H00FFFFFF')
    align = {'bottom':2,'middle':5,'top':8}.get(position, 2)
    bold = -1 if font_style == 'Bold' else 0
    italic = -1 if font_style == 'Italic' else 0
    if bg in ('Semi-transparent','semi'):
        border_style, back_color = 3, '&H80000000'
    elif bg in ('Black box','black'):
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
            text = re.sub(r'<[^>]+>', '', text)
            events.append(f"Dialogue: 0,{srt_time_to_ass(ts[0])},{srt_time_to_ass(ts[1])},Default,,0,0,0,,{text}")
        except:
            continue
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(header + '\n'.join(events))
    return ass_path

def apply_netflix_style(ass_path, font_name=None, font_size=None, color=None, bg=None):
    import re as _r
    try:
        c = open(ass_path, encoding='utf-8').read()
        ex = _r.search(r'Style: Default,([^\n]+)', c)
        ex = ex.group(1).split(',') if ex else []
        fn = font_name or (ex[0] if ex else 'Noto Sans Bengali')
        fs = str(font_size or (ex[1].strip() if len(ex)>1 else '28'))
        cm = {'White':'&H00FFFFFF','white':'&H00FFFFFF','Yellow':'&H0000FFFF','yellow':'&H0000FFFF','Cyan':'&H00FFFF00','cyan':'&H00FFFF00'}
        pc = cm.get(color,'&H00FFFFFF') if color else (ex[2].strip() if len(ex)>2 else '&H00FFFFFF')
        if bg in ('Semi-transparent','semi'):
            bs,bc = 3,'&H99000000'
        elif bg in ('Black box','black'):
            bs,bc = 3,'&HDD000000'
        else:
            bs,bc = 1,'&H00000000'
        style = ','.join(['Style: Default',fn,fs,pc,'&H000000FF','&H00141414',bc,'0','0','0','0','100','105','0.3','0',str(bs),'2.5','1.5','2','15','15','28','1'])
        c = _r.sub(r'Style: Default,[^\n]+', style, c)
        open(ass_path,'w',encoding='utf-8').write(c)
        print(f'[Style] {fn} {fs}px {color} {bg}')
    except Exception as e:
        print(f'[Style] Error: {e}')

def index():
    return render_template('index.html')

@app.route('/extract', methods=['POST'])
@app.route('/api/extract', methods=['POST'])
def extract():
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    cookie_path = '/tmp/anisub/cookies.txt' if os.path.exists('/tmp/anisub/cookies.txt') else None
    result = extract_from_episode_page(url, cookie_path)
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

@app.route('/start', methods=['POST'])
@app.route('/api/start', methods=['POST'])
def start_task():
    if request.content_type and 'multipart' in request.content_type:
        data = request.form.to_dict()
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
        data['sub_type'] = data.get('sub_mode', data.get('sub_type', 'none'))
        data['trans_sub_url'] = data.get('translate_url', data.get('trans_sub_url', ''))
        data['trans_engine'] = data.get('translate_engine', data.get('trans_engine', 'google'))
        data['trans_lang'] = 'bn'
        data['gemini_api_key'] = data.get('gemini_key', data.get('gemini_api_key', ''))
        data['tg_title'] = data.get('title', data.get('tg_title', 'AniSub Video'))
        data['tg_caption'] = data.get('caption', data.get('tg_caption', ''))
    else:
        data = request.json or {}

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        'status': 'Processing', 'stage': 'download',
        'progress': 0, 'logs': [],
        'tg_link': None, 'post_link': None,
        'error': None, 'output_path': None, 'has_preview': False
    }
    threading.Thread(target=process_task, args=(task_id, data), daemon=False).start()
    return jsonify({'task_id': task_id})

@app.route('/status/<task_id>', methods=['GET'])
@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    if task_id not in tasks:
        return jsonify({'error': 'Task not found'}), 404
    offset = int(request.args.get('offset', 0))
    task = tasks[task_id]
    stage_map = {
        'Downloading':'download','Subtitle':'translate',
        'Processing':'process','Uploading':'upload',
        'Done':'done','Error':'error'
    }
    return jsonify({
        'status': task['status'].lower() if task['status'] in ('Done','Error') else task['status'],
        'stage': stage_map.get(task['status'], 'download'),
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

def get_duration(path):
    try:
        res = subprocess.run(
            ['ffprobe','-v','error','-show_entries','format=duration',
             '-of','default=noprint_wrappers=1:nokey=1',path],
            capture_output=True, text=True, timeout=10)
        return float(res.stdout.strip())
    except:
        return None

def parse_time(t):
    try:
        h,m,s = t.split(':')
        return int(h)*3600 + int(m)*60 + float(s)
    except:
        return 0

def process_task(task_id, data):
    task = tasks[task_id]

    def log(msg, icon="ℹ️"):
        task['logs'].append(f"[{time.strftime('%H:%M:%S')}] {icon} {msg}")

    try:
        log("Task started", "🚀")
        video_url = data.get('video_url', '').strip()
        cookie_path = '/tmp/anisub/cookies.txt' if os.path.exists('/tmp/anisub/cookies.txt') else None
        final_video_path = f"/tmp/anisub/{task_id}_final.mp4"
        raw_video_path = f"/tmp/anisub/{task_id}_raw.mp4"

        # ── SUBTITLE ─────────────────────────────────────────────
        task['status'] = 'Subtitle'
        task['stage'] = 'translate'
        log("Subtitle processing...", "📝")

        sub_type = data.get('sub_type', 'none')
        srt_content = ""

        if sub_type == 'file':
            sub_path = data.get('sub_file_path', '')
            if sub_path and os.path.exists(sub_path):
                with open(sub_path, 'r', encoding='utf-8') as f:
                    srt_content = f.read()
                if sub_path.endswith('.vtt') or 'WEBVTT' in srt_content:
                    srt_content = convert_vtt_to_srt(srt_content)
                log("Subtitle loaded from file", "✅")

        elif sub_type == 'url':
            sub_url = data.get('sub_url', '').strip()
            if sub_url:
                res = requests.get(sub_url, timeout=15)
                srt_content = res.text
                if '.vtt' in sub_url or 'WEBVTT' in srt_content:
                    srt_content = convert_vtt_to_srt(srt_content)
                log("Subtitle downloaded", "✅")

        elif sub_type == 'translate':
            engine = data.get('trans_engine', 'google')
            src_url = data.get('trans_sub_url', '').strip()
            src_file = data.get('trans_sub_file', '')
            api_key = data.get('gemini_api_key', '')
            src = ""
            if src_file and os.path.exists(src_file):
                with open(src_file, 'r', encoding='utf-8') as f:
                    src = f.read()
            elif src_url:
                res = requests.get(src_url, timeout=15)
                src = res.text
            if src:
                if 'WEBVTT' in src or src_url.endswith('.vtt'):
                    src = convert_vtt_to_srt(src)
                log(f"Translating via {engine}...", "🔄")
                srt_content = translate_gemini(src, api_key, 'bn') if engine == 'gemini' and api_key else translate_google(src, 'bn')
                log("Translation done", "✅")

        task['progress'] = 20

        # ── ASS PREPARE ──────────────────────────────────────────
        ass_file_path = None
        if srt_content:
            sub_file_path = f"/tmp/anisub/{task_id}.srt"
            ass_file_path = f"/tmp/anisub/{task_id}.ass"
            with open(sub_file_path, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            conv = subprocess.run(['ffmpeg','-y','-i',sub_file_path,ass_file_path], capture_output=True, text=True)
            if conv.returncode != 0 or not os.path.exists(ass_file_path):
                srt_to_ass(sub_file_path, ass_file_path)
            apply_netflix_style(ass_file_path, font_name=data.get("font_name"), font_size=data.get("font_size"), color=data.get("color"), bg=data.get("bg"))
            log("Subtitle ready (Netflix style) ✨", "✅")

        task['progress'] = 30
        sub_filter = f"scale=1280:-2,ass='{ass_file_path}':fontsdir=/tmp/fonts/" if ass_file_path else "scale=1280:-2"

        # ── VIDEO PROCESS ─────────────────────────────────────────
        task['status'] = 'Processing'
        task['stage'] = 'process'

        if '.m3u8' in video_url:
            # ⚡ m3u8 → DIRECT BURN (no download!)
            log("m3u8 direct burn ⚡ (no download step!)", "🔥")
            cmd = [
                'ffmpeg', '-y',
                '-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                '-i', video_url,
                '-vf', sub_filter,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                '-threads', '0', '-c:a', 'copy',
                '-max_muxing_queue_size', '1024',
                final_video_path
            ]
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
            for line in iter(proc.stderr.readline, ''):
                l = line.strip()
                if l:
                    task['logs'].append(f"[FFMPEG] {l}")
                    if 'time=' in l:
                        try:
                            sec = parse_time(l.split('time=')[1].split()[0])
                            task['progress'] = min(30 + int(sec / 2), 74)
                        except:
                            pass
            proc.wait()
            if proc.returncode != 0 or not os.path.exists(final_video_path) or os.path.getsize(final_video_path) < 1024*1024:
                raise Exception("m3u8 direct burn failed")
            log("Burn complete!", "✅")

        else:
            # mp4 or other — download then burn
            task['status'] = 'Downloading'
            task['stage'] = 'download'
            downloaded = False

            if shutil.which('yt-dlp'):
                log(f"Downloading...", "⬇️")
                cmd = ['yt-dlp', '-o', raw_video_path, '--no-playlist']
                if cookie_path:
                    cmd += ['--cookies', cookie_path]
                cmd.append(video_url)
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
                if os.path.exists(raw_video_path) and os.path.getsize(raw_video_path) > 1024*1024:
                    downloaded = True
                    log("Download done", "✅")

            if not downloaded:
                log("FFmpeg download fallback...", "⬇️")
                subprocess.run(['ffmpeg','-y','-user_agent','Mozilla/5.0','-i',video_url,'-c','copy',raw_video_path], capture_output=True)
                if os.path.exists(raw_video_path) and os.path.getsize(raw_video_path) > 1024*1024:
                    downloaded = True

            if not downloaded:
                raise Exception("Download failed")

            task['status'] = 'Processing'
            task['stage'] = 'process'
            log("Burning subtitles...", "🔥")
            duration = get_duration(raw_video_path)

            cmd = ['ffmpeg','-y','-i',raw_video_path,'-vf',sub_filter,
                   '-c:v','libx264','-preset','ultrafast','-crf','23',
                   '-threads','0','-c:a','copy','-max_muxing_queue_size','1024',final_video_path]
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
            for line in iter(proc.stderr.readline, ''):
                l = line.strip()
                if l:
                    task['logs'].append(f"[FFMPEG] {l}")
                    if 'time=' in l and duration:
                        try:
                            sec = parse_time(l.split('time=')[1].split()[0])
                            task['progress'] = 35 + int((sec/duration)*40)
                        except:
                            pass
            proc.wait()
            if proc.returncode != 0 or not os.path.exists(final_video_path):
                shutil.copy(raw_video_path, final_video_path)

        task['output_path'] = final_video_path
        task['has_preview'] = True

        # ── UPLOAD ───────────────────────────────────────────────
        if task.get('uploading'):
            return
        task['uploading'] = True
        task['status'] = 'Uploading'
        task['stage'] = 'upload'
        log("Uploading to Telegram...", "☁️")

        def prog_cb(pct):
            task['progress'] = 75 + int(pct * 0.25)
            if pct % 10 == 0:
                log(f"Upload: {pct}%", "📤")

        tg_link = upload_to_telegram(
            final_video_path,
            data.get('tg_title', 'AniSub Video'),
            data.get('tg_caption', ''),
            prog_cb
        )
        task['tg_link'] = tg_link
        task['post_link'] = tg_link
        task['progress'] = 100
        task['status'] = 'Done'
        task['stage'] = 'done'
        log("Done!", "✅")

        def cleanup():
            threading.Event().wait(3600)
            for p in [raw_video_path, final_video_path,
                      f"/tmp/anisub/{task_id}.srt", f"/tmp/anisub/{task_id}.ass"]:
                try:
                    os.remove(p)
                except:
                    pass
        threading.Thread(target=cleanup, daemon=False).start()

    except Exception as e:
        task['status'] = 'Error'
        task['stage'] = 'error'
        task['error'] = str(e)
        log(f"Failed: {e}", "❌")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
