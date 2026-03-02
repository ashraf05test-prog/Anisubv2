import cloudscraper
from bs4 import BeautifulSoup
import re
import json
import base64
import shutil
import subprocess
import requests

def extract_from_episode_page(url, cookie_path=None):
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )

    result = {
        'm3u8_url': None,
        'subtitles': [],
        'iframe_urls': [],
        'errors': []
    }

    # ── Site-specific handlers ──────────────────────────────────────
    if 'dramacool' in url or 'asianc' in url or 'dramacooll' in url:
        _extract_dramacool(url, scraper, result)
    elif 'gogoanime' in url or 'anitaku' in url or 'gogoanime.tel' in url:
        _extract_gogoanime(url, scraper, result)
    else:
        _extract_generic(url, scraper, result)

    # ── Subtitle dedup ──────────────────────────────────────────────
    seen = set()
    unique_subs = []
    for s in result['subtitles']:
        if s['url'] not in seen:
            seen.add(s['url'])
            unique_subs.append(s)
    result['subtitles'] = unique_subs

    return result


# ════════════════════════════════════════════════════════════════════
# DRAMACOOL EXTRACTOR
# ════════════════════════════════════════════════════════════════════
def _extract_dramacool(url, scraper, result):
    try:
        res = scraper.get(url, timeout=15)
        html = res.text
        soup = BeautifulSoup(html, 'lxml')

        # Subtitle from dramacool
        result['subtitles'].extend(extract_subtitles(html))

        # Get embed iframes
        iframes = []
        for tag in soup.find_all('iframe'):
            src = tag.get('src') or tag.get('data-src') or ''
            if src.startswith('http'):
                iframes.append(src)
        for m in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html):
            if m.startswith('http') and m not in iframes:
                iframes.append(m)

        result['iframe_urls'] = iframes

        # Try each embed
        for embed_url in iframes:
            if result['m3u8_url']:
                break
            m3u8 = _extract_from_embed(embed_url, scraper, referer=url)
            if m3u8:
                result['m3u8_url'] = m3u8

    except Exception as e:
        result['errors'].append(f"Dramacool extraction failed: {e}")


# ════════════════════════════════════════════════════════════════════
# GOGOANIME EXTRACTOR
# ════════════════════════════════════════════════════════════════════
def _extract_gogoanime(url, scraper, result):
    try:
        res = scraper.get(url, timeout=15)
        html = res.text
        soup = BeautifulSoup(html, 'lxml')

        result['subtitles'].extend(extract_subtitles(html))

        # Gogoanime embed links
        iframes = []
        for tag in soup.find_all('iframe'):
            src = tag.get('src') or tag.get('data-src') or ''
            if src.startswith('http'):
                iframes.append(src)

        # Gogoanime specific: video_id in page
        vid_match = re.search(r'data-video=["\']([^"\']+)["\']', html)
        if vid_match:
            embed = vid_match.group(1)
            if not embed.startswith('http'):
                embed = 'https:' + embed
            if embed not in iframes:
                iframes.append(embed)

        result['iframe_urls'] = iframes

        for embed_url in iframes:
            if result['m3u8_url']:
                break
            m3u8 = _extract_from_embed(embed_url, scraper, referer=url)
            if m3u8:
                result['m3u8_url'] = m3u8

        # Gogoanime ajax fallback
        if not result['m3u8_url']:
            ep_match = re.search(r'id=["\']movie_id["\'][^>]*value=["\'](\d+)["\']', html)
            if ep_match:
                ep_id = ep_match.group(1)
                ajax_url = f"https://ajax.gogo-load.com/ajax/loadserver?id={ep_id}&refer={url}"
                try:
                    ajax_res = scraper.get(ajax_url, timeout=10)
                    ajax_data = ajax_res.json()
                    for server in ajax_data.get('html', '').split('</li>'):
                        link_m = re.search(r'data-video=["\']([^"\']+)["\']', server)
                        if link_m:
                            embed = link_m.group(1)
                            if not embed.startswith('http'):
                                embed = 'https:' + embed
                            m3u8 = _extract_from_embed(embed, scraper, referer=url)
                            if m3u8:
                                result['m3u8_url'] = m3u8
                                break
                except:
                    pass

    except Exception as e:
        result['errors'].append(f"Gogoanime extraction failed: {e}")


# ════════════════════════════════════════════════════════════════════
# EMBED HANDLERS (vidbasic, streamwish, filemoon, etc.)
# ════════════════════════════════════════════════════════════════════
def _extract_from_embed(embed_url, scraper, referer=''):
    try:
        headers = {
            'Referer': referer or embed_url,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        # vidbasic / vidhide / embedsito
        if any(x in embed_url for x in ['vidbasic', 'vidhide', 'embedsito', 'vidmoly']):
            return _extract_vidbasic(embed_url, scraper, headers)

        # streamwish / wishembed
        if any(x in embed_url for x in ['streamwish', 'wishembed', 'sfastwish']):
            return _extract_streamwish(embed_url, scraper, headers)

        # filemoon / moonplayer
        if any(x in embed_url for x in ['filemoon', 'moonplayer', 'fmoonembed']):
            return _extract_filemoon(embed_url, scraper, headers)

        # doodstream
        if any(x in embed_url for x in ['dood', 'ds2play']):
            return _extract_doodstream(embed_url, scraper, headers)

        # generic embed
        return _extract_embed_generic(embed_url, scraper, headers)

    except Exception as e:
        return None


def _extract_vidbasic(url, scraper, headers):
    try:
        res = scraper.get(url, headers=headers, timeout=15)
        html = res.text

        # Method 1: jwplayer setup
        m = re.search(r'jwplayer\([^)]+\)\.setup\(\s*(\{.*?\})\s*\)', html, re.DOTALL)
        if m:
            try:
                cfg = json.loads(m.group(1))
                for src in cfg.get('sources', []):
                    if src.get('file') and '.m3u8' in src['file']:
                        return src['file']
            except:
                pass

        # Method 2: file: 'url' pattern
        m = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', html)
        if m:
            return m.group(1)

        # Method 3: sources array
        m = re.search(r'sources\s*:\s*\[.*?file\s*:\s*["\']([^"\']+)["\']', html, re.DOTALL)
        if m and '.m3u8' in m.group(1):
            return m.group(1)

        # Method 4: base64 encoded
        for b64 in re.findall(r'atob\([\'"]([^"\']+)[\'"]\)', html):
            try:
                decoded = base64.b64decode(b64).decode('utf-8')
                if '.m3u8' in decoded:
                    m = re.search(r'https?://[^\s"\']+\.m3u8[^\s"\']*', decoded)
                    if m:
                        return m.group(0)
            except:
                pass

        # Method 5: eval unpacking
        unpacked = unpack_js(html)
        if unpacked:
            m = re.search(r'https?://[^\s"\']+\.m3u8[^\s"\']*', unpacked)
            if m and is_valid_m3u8(m.group(0)):
                return m.group(0)

        # Method 6: any m3u8 in page
        for m3u8 in re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html):
            if is_valid_m3u8(m3u8):
                return m3u8

    except Exception as e:
        pass
    return None


def _extract_streamwish(url, scraper, headers):
    try:
        res = scraper.get(url, headers=headers, timeout=15)
        html = res.text
        # streamwish uses jwplayer
        m = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', html)
        if m:
            return m.group(1)
        for m3u8 in re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html):
            if is_valid_m3u8(m3u8):
                return m3u8
    except:
        pass
    return None


def _extract_filemoon(url, scraper, headers):
    try:
        res = scraper.get(url, headers=headers, timeout=15)
        html = res.text
        unpacked = unpack_js(html)
        src = unpacked or html
        m = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', src)
        if m:
            return m.group(1)
        for m3u8 in re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', src):
            if is_valid_m3u8(m3u8):
                return m3u8
    except:
        pass
    return None


def _extract_doodstream(url, scraper, headers):
    try:
        res = scraper.get(url, headers=headers, timeout=15)
        html = res.text
        m = re.search(r'/pass_md5/[^"\']+', html)
        if m:
            pass_url = 'https://dood.to' + m.group(0)
            token = re.search(r'token=([^&"\']+)', html)
            if token:
                pass_res = scraper.get(pass_url, headers=headers, timeout=10)
                video_url = pass_res.text.strip() + 'zHQIBfkzFx?token=' + token.group(1)
                return video_url
    except:
        pass
    return None


def _extract_embed_generic(url, scraper, headers):
    try:
        res = scraper.get(url, headers=headers, timeout=15)
        html = res.text

        # Direct m3u8
        for m3u8 in re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html):
            if is_valid_m3u8(m3u8):
                return m3u8

        # base64
        for b64 in re.findall(r'atob\([\'"]([^"\']+)[\'"]\)', html):
            try:
                decoded = base64.b64decode(b64).decode('utf-8')
                if '.m3u8' in decoded:
                    m = re.search(r'https?://[^\s"\']+\.m3u8[^\s"\']*', decoded)
                    if m:
                        return m.group(0)
            except:
                pass

        # eval unpacking
        unpacked = unpack_js(html)
        if unpacked:
            for m3u8 in re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', unpacked):
                if is_valid_m3u8(m3u8):
                    return m3u8
    except:
        pass
    return None


# ════════════════════════════════════════════════════════════════════
# GENERIC EXTRACTOR (fallback)
# ════════════════════════════════════════════════════════════════════
def _extract_generic(url, scraper, result):
    try:
        res = scraper.get(url, timeout=15)
        html = res.text
        soup = BeautifulSoup(html, 'lxml')

        result['subtitles'].extend(extract_subtitles(html))

        # Direct m3u8 in page
        for m3u8 in re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html):
            if is_valid_m3u8(m3u8):
                result['m3u8_url'] = m3u8
                return

        # Iframes
        iframes = []
        for tag in soup.find_all('iframe'):
            src = tag.get('src') or tag.get('data-src') or ''
            if src.startswith('http'):
                iframes.append(src)
        result['iframe_urls'] = iframes

        for embed_url in iframes:
            if result['m3u8_url']:
                break
            m3u8 = _extract_from_embed(embed_url, scraper, referer=url)
            if m3u8:
                result['m3u8_url'] = m3u8

        # yt-dlp fallback
        if not result['m3u8_url'] and shutil.which('yt-dlp'):
            try:
                proc = subprocess.run(
                    ['yt-dlp', '--dump-json', '--no-download', url],
                    capture_output=True, text=True, timeout=30
                )
                if proc.returncode == 0:
                    data = json.loads(proc.stdout.split('\n')[0])
                    for f in reversed(data.get('formats', [])):
                        if f.get('url') and is_valid_m3u8(f['url']):
                            result['m3u8_url'] = f['url']
                            break
                    if not result['m3u8_url'] and data.get('url'):
                        result['m3u8_url'] = data['url']
            except:
                pass

    except Exception as e:
        result['errors'].append(f"Generic extraction failed: {e}")


# ════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════
def is_valid_m3u8(url):
    if not url or not url.startswith('http'):
        return False
    if '.m3u8' not in url:
        return False
    bad = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico']
    lower = url.lower()
    if any(lower.endswith(e) for e in bad):
        return False
    return True


def unpack_js(html):
    match = re.search(r'eval\((function\(p,a,c,k,e,?[rd]?\).*?)\)', html, re.DOTALL)
    if not match:
        return ""
    return match.group(1)


def extract_subtitles(html):
    subs = []
    soup = BeautifulSoup(html, 'lxml')
    for track in soup.find_all('track'):
        if track.get('kind') in ['subtitles', 'captions']:
            src = track.get('src', '')
            if src.startswith('http'):
                subs.append({'url': src, 'lang': detect_lang(src, track.get('srclang', ''))})
    for pattern in [
        r'https?://[^\s"\'<>]+\.(?:srt|vtt|ass)',
        r'subtitle["\']?\s*:\s*["\'](http[^"\']+)["\']',
        r'subtitles["\']?\s*:\s*["\'](http[^"\']+)["\']',
    ]:
        for match in re.findall(pattern, html):
            if match.startswith('http'):
                subs.append({'url': match, 'lang': detect_lang(match, '')})
    return subs


def detect_lang(url, srclang):
    s = f"{url} {srclang}".lower()
    if 'bn' in s or 'bangla' in s or 'bengali' in s:
        return 'bn'
    return 'en'
