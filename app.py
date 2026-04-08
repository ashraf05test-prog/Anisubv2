import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file

from extractor import extract_sources
from fb_uploader import upload_to_facebook
from translator import convert_vtt_to_srt, parse_srt, translate_srt_text
from uploader import upload_to_telegram


app = Flask(__name__)
TASKS = {}
BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = Path('/tmp/anisubv2')
TMP_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_DIR = TMP_DIR / 'cookies'
COOKIE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = TMP_DIR / 'outputs'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_CANDIDATES = {
    'noto_sans_bn': {
        'display': 'Noto Sans Bengali',
        'family': 'Noto Sans Bengali',
        'paths': [
            '/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf',
            '/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf',
            '/app/fonts/NotoSansBengali-Regular.ttf',
        ],
    },
    'noto_serif_bn': {
        'display': 'Noto Serif Bengali',
        'family': 'Noto Serif Bengali',
        'paths': [
            '/usr/share/fonts/truetype/noto/NotoSerifBengali-Regular.ttf',
            '/usr/share/fonts/truetype/noto/NotoSerifBengali-Bold.ttf',
            '/app/fonts/NotoSerifBengali-Regular.ttf',
        ],
    },
    'lohit_bn': {
        'display': 'Lohit Bengali',
        'family': 'Lohit Bengali',
        'paths': [
            '/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf',
            '/app/fonts/Lohit-Bengali.ttf',
        ],
    },
    'kalpurush': {
    'display': 'Kalpurush',
    'family': 'Kalpurush',
    'paths': ['/app/fonts/Kalapurush.ttf'],
},
'solaimanlipi': {
    'display': 'SolaimanLipi',
    'family': 'SolaimanLipi',
    'paths': ['/app/fonts/SolaimanLipi.ttf'],
},

}


def log(task, message, icon='ℹ️'):
    task['logs'].append({'ts': time.time(), 'icon': icon, 'message': message})


def ensure_fonts_dir():
    fonts_dir = TMP_DIR / 'fonts'
    fonts_dir.mkdir(parents=True, exist_ok=True)
    for meta in FONT_CANDIDATES.values():
        for p in meta['paths']:
            if os.path.exists(p):
                dst = fonts_dir / Path(p).name
                if not dst.exists():
                    shutil.copy2(p, dst)
    return str(fonts_dir)


def get_font_family(font_key):
    return FONT_CANDIDATES.get(font_key, FONT_CANDIDATES['noto_sans_bn'])['family']


def ass_color(name):
    return {
        'white': '&H00FFFFFF',
        'yellow': '&H0000FFFF',
        'cyan': '&H00FFFF00',
    }.get((name or 'white').lower(), '&H00FFFFFF')


def ass_alignment(position):
    return {
        'bottom': 2,
        'middle': 5,
        'top': 8,
    }.get((position or 'bottom').lower(), 2)


def ass_background(bg):
    key = (bg or 'semi-transparent').lower()
    if key in ['none', 'transparent']:
        return 1, '&H00000000', 2, 0
    if key in ['black', 'black box', 'box']:
        return 3, '&HAA000000', 1, 0
    return 1, '&H80000000', 2, 0


def ffmpeg_escape_filter_path(path: str):
    path = path.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'").replace(',', '\\,')
    return path


def srt_time_to_ass(ts: str) -> str:
    return ts.replace(',', '.')


def srt_to_ass(srt_text, ass_path, font_key='noto_sans_bn', color='white',
               position='bottom', background='semi-transparent',
               bold=False, italic=False, font_size=42):
    items = parse_srt(srt_text)
    font_family = get_font_family(font_key)
    border_style, back_colour, outline, shadow = ass_background(background)
    alignment = ass_alignment(position)
    margin_v = {'bottom': 34, 'middle': 120, 'top': 34}.get((position or 'bottom').lower(), 34)

    header = (
        '[Script Info]\n'
        'Title: AniSub Bengali\n'
        'ScriptType: v4.00+\n'
        'PlayResX: 1280\n'
        'PlayResY: 720\n'
        'ScaledBorderAndShadow: yes\n'
        'WrapStyle: 2\n'
        'YCbCr Matrix: TV.709\n\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
        'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Default,{font_family},{font_size},{ass_color(color)},&H0000FFFF,&H00111111,{back_colour},'
        f'{-1 if bold else 0},{-1 if italic else 0},0,0,100,100,0,0,{border_style},{outline},{shadow},'
        f'{alignment},60,60,{margin_v},1\n\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )

    dialogue_lines = []
    for item in items:
        parts = item['times'].replace('-->', '|').split('|')
        if len(parts) < 2:
            continue
        start, end = parts[0].strip(), parts[1].strip()
        text = item['text'].replace('\n', r'\N')
        text = re.sub(r'<[^>]+>', '', text)
        dialogue_lines.append(
            f'Dialogue: 0,{srt_time_to_ass(start)},{srt_time_to_ass(end)},Default,,0,0,0,,{text}'
        )

    content = header + '\n'.join(dialogue_lines) + '\n'
    Path(ass_path).write_text(content, encoding='utf-8')
    return ass_path


def download_text(url: str) -> str:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    response.encoding = response.encoding or 'utf-8'
    return response.text


def _parse_ffmpeg_time(line: str):
    """ffmpeg stderr থেকে time= value সেকেন্ডে বের করো"""
    m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _get_duration(path_or_url: str):
    """ffprobe দিয়ে duration বের করো"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path_or_url],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def run_ffmpeg_with_progress(cmd, task, duration=None, progress_start=50, progress_end=75):
    """
    ffmpeg কে Popen দিয়ে চালাও, stderr থেকে live progress parse করো।
    UI হ্যাং হবে না কারণ background thread এ চলে।
    """
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    for line in proc.stderr:
        line = line.rstrip()
        if not line:
            continue

        # live log — শুধু গুরুত্বপূর্ণ লাইন
        if any(k in line for k in ('frame=', 'fps=', 'time=', 'speed=', 'Error', 'error')):
            task['logs'].append({'ts': time.time(), 'icon': '🎞️', 'message': line})

        # progress calculate
        if duration and 'time=' in line:
            elapsed = _parse_ffmpeg_time(line)
            if elapsed is not None:
                ratio = min(elapsed / duration, 1.0)
                task['progress'] = int(progress_start + ratio * (progress_end - progress_start))

    proc.wait()
    return proc.returncode


def process_task(task_id, data):
    task = TASKS[task_id]
    task['status'] = 'Running'
    task['stage'] = 'extract'
    work_dir = OUTPUT_DIR / task_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        log(task, 'Task started', '🚀')
        source_url = data.get('source_url')
        video_url = data.get('video_url')
        subtitle_url = data.get('subtitle_url')
        cookie_path = data.get('cookie_path')

        # ── Extract (video_url সরাসরি দিলে extractor skip) ──────
        if video_url:
            # Direct URL mode — extractor বাদ, সরাসরি ffmpeg এ যাবে
            log(task, f'Direct video: {video_url[:80]}...', '🔗')
        elif source_url:
            result = extract_sources(source_url, cookie_path=cookie_path)
            video_url = result.get('video_url') or result.get('m3u8_url')
            if not subtitle_url and result.get('subtitles'):
                subtitle_url = result['subtitles'][0]['url']
            task['extract_result'] = result
            log(task, f'Extracted video: {video_url}', '🔎')
            log(task, f'Extracted subtitle: {subtitle_url}', '📝')
        else:
            raise RuntimeError('Video URL বা Episode URL দাও')

        if not video_url:
            raise RuntimeError('No video URL found')

        # ── Download subtitle (optional) ──────────────────────────
        srt_text = None
        ass_path_direct = None  # .ass file সরাসরি দিলে
        if subtitle_url:
            task['stage'] = 'download'
            task['progress'] = 10

            if subtitle_url.startswith('/'):
                subtitle_text = open(subtitle_url, encoding='utf-8').read()
                log(task, 'Subtitle loaded from uploaded file', '📂')
            else:
                subtitle_text = download_text(subtitle_url)
                log(task, 'Subtitle downloaded', '⬇️')

            # .ass file হলে সরাসরি use করো — convert দরকার নেই
            if subtitle_url.lower().endswith('.ass') or subtitle_text.lstrip().startswith('[Script Info]'):
                ass_path_direct = str(work_dir / 'subtitle.ass')
                Path(ass_path_direct).write_text(subtitle_text, encoding='utf-8')
                log(task, 'ASS subtitle loaded directly', '🎨')
            elif subtitle_url.lower().endswith('.vtt') or subtitle_text.lstrip().startswith('WEBVTT'):
                srt_text = convert_vtt_to_srt(subtitle_text)
                log(task, 'VTT → SRT converted', '🔁')
            else:
                srt_text = subtitle_text

            if srt_text:
                original_srt_path = work_dir / 'original.srt'
                original_srt_path.write_text(srt_text, encoding='utf-8')
        else:
            log(task, 'No subtitle — video only mode', 'ℹ️')

        # ── Translate ─────────────────────────────────────────────
        translated_srt = None
        if srt_text:
            task['stage'] = 'translate'
            task['progress'] = 30
            translate_to_bn = bool(data.get('translate_to_bn', True))
            if translate_to_bn:
                translated_srt = translate_srt_text(
                    srt_text,
                    gemini_api_key=data.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY'),
                    grok_api_key=data.get('grok_api_key') or os.environ.get('XAI_API_KEY'),
                    batch_size=int(data.get('batch_size', 20)),
                )
                log(task, 'Subtitle translated to Bangla', '🇧🇩')
            else:
                translated_srt = srt_text
                log(task, 'Translation skipped', '⏭️')

            translated_srt_path = work_dir / 'translated.srt'
            translated_srt_path.write_text(translated_srt, encoding='utf-8')
            task['translated_srt_path'] = str(translated_srt_path)

        # ── ASS subtitle ──────────────────────────────────────────
        task['stage'] = 'process'
        task['progress'] = 50
        ass_path = ass_path_direct  # .ass সরাসরি দিলে এটাই use হবে
        if translated_srt:
            ass_path = str(work_dir / 'subtitle.ass')
            srt_to_ass(
                translated_srt, ass_path,
                font_key=data.get('font_family', 'noto_sans_bn'),
                color=data.get('subtitle_color', 'white'),
                position=data.get('subtitle_position', 'bottom'),
                background=data.get('subtitle_background', 'semi-transparent'),
                bold=bool(data.get('subtitle_bold', False)),
                italic=bool(data.get('subtitle_italic', False)),
                font_size=int(data.get('subtitle_size', 42)),
            )
            log(task, 'ASS subtitle prepared', '🎨')

        # ── Download video first (CDN 403 fix) ──────────────────
        raw_video = str(work_dir / 'source.mp4')
        task['stage'] = 'download'
        task['progress'] = 40
        log(task, 'Downloading video...', '⬇️')

        dl = subprocess.run(
            ['yt-dlp', '-o', raw_video, '--no-playlist', video_url],
            capture_output=True, text=True
        )
        if not os.path.exists(raw_video) or os.path.getsize(raw_video) < 1024:
            # yt-dlp fail করলে ffmpeg দিয়ে try
            subprocess.run([
                'ffmpeg', '-y',
                '-user_agent', 'Mozilla/5.0',
                '-i', video_url, '-c', 'copy', raw_video
            ], capture_output=True)

        if not os.path.exists(raw_video) or os.path.getsize(raw_video) < 1024:
            raise RuntimeError('Video download failed')

        log(task, 'Video downloaded', '✅')

        # ── ffmpeg render (live progress) ─────────────────────────
        final_video_path = str(work_dir / 'final.mp4')
        fonts_dir = ensure_fonts_dir()

        if ass_path:
            vf_filter = (
                f"scale=1280:-2,"
                f"ass='{ffmpeg_escape_filter_path(ass_path)}':"
                f"fontsdir='{ffmpeg_escape_filter_path(fonts_dir)}'"
            )
        else:
            vf_filter = 'scale=1280:-2'

        cmd = [
            'ffmpeg', '-y',
            '-i', raw_video,
            '-vf', vf_filter,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            final_video_path,
        ]

        log(task, 'Rendering video...', '🎬')
        duration = _get_duration(video_url)
        returncode = run_ffmpeg_with_progress(cmd, task, duration=duration)

        if returncode != 0 or not os.path.exists(final_video_path):
            raise RuntimeError(f'ffmpeg failed with code {returncode}')

        log(task, 'Video render complete', '✅')
        task['progress'] = 75
        task['final_video_path'] = final_video_path
        task['ass_path'] = ass_path

        # ── Upload ────────────────────────────────────────────────
        task['stage'] = 'upload'
        upload_targets = data.get('upload_targets', [])

        if 'telegram' in upload_targets:
            log(task, 'Uploading to Telegram...', '☁️')

            def tg_prog(pct):
                task['progress'] = 75 + int(pct * 0.15)
                if pct % 20 == 0:
                    log(task, f'Telegram upload: {pct}%', '📤')

            tg_link = upload_to_telegram(
                final_video_path,
                data.get('tg_title', 'AniSub Video'),
                data.get('tg_caption', ''),
                tg_prog,
            )
            task['tg_link'] = tg_link
            log(task, f'Telegram done: {tg_link}', '✅')

        if 'facebook' in upload_targets:
            fb_page_id = data.get('fb_page_id') or os.environ.get('FB_PAGE_ID')
            fb_token = data.get('fb_token') or os.environ.get('FB_PAGE_TOKEN')
            if not fb_page_id or not fb_token:
                log(task, 'Facebook credentials missing, skipping', '⚠️')
            else:
                log(task, 'Uploading to Facebook...', '📘')

                def fb_prog(pct):
                    task['progress'] = 90 + int(pct * 0.10)
                    if pct % 20 == 0:
                        log(task, f'Facebook upload: {pct}%', '📤')

                fb_link = upload_to_facebook(
                    final_video_path,
                    data.get('tg_title', 'AniSub Video'),
                    data.get('tg_caption', ''),
                    fb_page_id, fb_token, fb_prog,
                )
                task['fb_link'] = fb_link
                task['post_link'] = fb_link
                log(task, f'Facebook done: {fb_link}', '✅')

        task['stage'] = 'done'
        task['progress'] = 100
        task['status'] = 'Done'
        log(task, 'All done!', '🏁')

    except Exception as exc:
        task['status'] = 'Error'
        task['stage'] = 'error'
        task['error'] = str(exc)
        log(task, f'Error: {exc}', '❌')


# ── Routes ────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', fonts=FONT_CANDIDATES)


@app.route('/extract', methods=['POST'])
def extract_route():
    payload = request.get_json(force=True)
    result = extract_sources(payload['url'], cookie_path=payload.get('cookie_path'))
    return jsonify(result)


@app.route('/start', methods=['POST'])
def start_route():
    payload = request.get_json(force=True)
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        'id': task_id,
        'status': 'Queued',
        'stage': 'queued',
        'progress': 0,
        'logs': [],
    }
    threading.Thread(target=process_task, args=(task_id, payload), daemon=True).start()
    return jsonify({'task_id': task_id, 'status': 'Queued'})


@app.route('/status/<task_id>')
def status_route(task_id):
    task = TASKS.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    offset = int(request.args.get('log_offset', 0))
    return jsonify({
        'id': task['id'],
        'status': task['status'],
        'stage': task['stage'],
        'progress': task['progress'],
        'logs': task['logs'][offset:],   # offset দিয়ে নতুন লগ মাত্র পাঠাচ্ছি
        'log_total': len(task['logs']),
        'tg_link': task.get('tg_link'),
        'fb_link': task.get('fb_link'),
        'post_link': task.get('post_link'),
        'error': task.get('error'),
        'final_video_path': task.get('final_video_path'),
    })


@app.route('/preview/<task_id>')
def preview_route(task_id):
    task = TASKS.get(task_id)
    if not task or not task.get('final_video_path') or not os.path.exists(task['final_video_path']):
        return jsonify({'error': 'Preview not ready'}), 404
    return send_file(task['final_video_path'], mimetype='video/mp4')


@app.route('/upload_subtitle', methods=['POST'])
def upload_subtitle_route():
    if 'subtitle_file' not in request.files:
        return jsonify({'error': 'No subtitle_file uploaded'}), 400
    file = request.files['subtitle_file']
    dest = TMP_DIR / f"{uuid.uuid4().hex[:8]}_{file.filename}"
    file.save(dest)
    return jsonify({'ok': True, 'subtitle_path': str(dest)})


@app.route('/upload_subtitle_text', methods=['POST'])
def upload_subtitle_text_route():
    data = request.get_json(force=True)
    filename = data.get('filename', 'subtitle.srt')
    content = data.get('content', '')
    if not content:
        return jsonify({'error': 'No content'}), 400
    dest = TMP_DIR / f"{uuid.uuid4().hex[:8]}_{filename}"
    dest.write_text(content, encoding='utf-8')
    return jsonify({'ok': True, 'subtitle_path': str(dest)})


@app.route('/upload_cookie', methods=['POST'])
def upload_cookie_route():
    if 'cookie_file' not in request.files:
        return jsonify({'error': 'No cookie_file uploaded'}), 400
    file = request.files['cookie_file']
    dest = COOKIE_DIR / file.filename
    file.save(dest)
    return jsonify({'ok': True, 'cookie_path': str(dest)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=True)
