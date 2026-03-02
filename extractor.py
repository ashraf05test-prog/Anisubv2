import cloudscraper
from bs4 import BeautifulSoup
import re
import json
import base64
import shutil
import subprocess

def extract_from_episode_page(url, cookie_path=None):
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
    
    result = {
        'm3u8_url': None,
        'subtitles': [],
        'iframe_urls': [],
        'errors': []
    }
    
    try:
        res = scraper.get(url, timeout=15)
        res.raise_for_status()
        html = res.text
    except Exception as e:
        result['errors'].append(f"Main page fetch failed: {e}")
        return result
        
    soup = BeautifulSoup(html, 'lxml')
    
    m3u8_patterns = [
        r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
        r'file["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'src["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'url["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'source["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'hls["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'hlsUrl["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'videoUrl["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']'
    ]
    
    for pattern in m3u8_patterns:
        matches = re.findall(pattern, html)
        for match in matches:
            if is_valid_m3u8(match):
                result['m3u8_url'] = match
                break
        if result['m3u8_url']:
            break
            
    if not result['m3u8_url']:
        unpacked = unpack_js(html)
        if unpacked:
            for pattern in m3u8_patterns:
                matches = re.findall(pattern, unpacked)
                for match in matches:
                    if is_valid_m3u8(match):
                        result['m3u8_url'] = match
                        break
                if result['m3u8_url']:
                    break
                    
    result['subtitles'].extend(extract_subtitles(html))
    
    iframes = soup.find_all('iframe')
    for iframe in iframes:
        src = iframe.get('src') or iframe.get('data-src') or iframe.get('data-lazy-src')
        if src and src.startswith('http'):
            result['iframe_urls'].append(src)
            
    iframe_matches = re.findall(r'<iframe[^>]+src=["\'](http[^"\']+)["\']', html)
    for src in iframe_matches:
        if src not in result['iframe_urls']:
            result['iframe_urls'].append(src)
            
    if not result['m3u8_url'] and result['iframe_urls']:
        for iframe_url in result['iframe_urls']:
            try:
                if_res = scraper.get(iframe_url, timeout=15)
                if_html = if_res.text
                
                b64_matches = re.findall(r'atob\([\'"]([^(\'"]+)[\'"]\)', if_html)
                for b64 in b64_matches:
                    try:
                        decoded = base64.b64decode(b64).decode('utf-8')
                        if '.m3u8' in decoded and is_valid_m3u8(decoded):
                            result['m3u8_url'] = decoded
                            break
                    except:
                        pass
                
                if not result['m3u8_url']:
                    for pattern in m3u8_patterns:
                        matches = re.findall(pattern, if_html)
                        for match in matches:
                            if is_valid_m3u8(match):
                                result['m3u8_url'] = match
                                break
                        if result['m3u8_url']:
                            break
                
                if not result['m3u8_url']:
                    unpacked = unpack_js(if_html)
                    if unpacked:
                        for pattern in m3u8_patterns:
                            matches = re.findall(pattern, unpacked)
                            for match in matches:
                                if is_valid_m3u8(match):
                                    result['m3u8_url'] = match
                                    break
                            if result['m3u8_url']:
                                break
                                
                result['subtitles'].extend(extract_subtitles(if_html))
                
                if result['m3u8_url']:
                    break
            except Exception as e:
                result['errors'].append(f"Iframe fetch failed ({iframe_url}): {e}")
                
    if not result['m3u8_url']:
        if shutil.which('yt-dlp') is not None:
            cmd = ['yt-dlp', '--dump-json', '--no-download', url]
            if cookie_path:
                cmd.extend(['--cookies', cookie_path])
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if proc.returncode == 0:
                    data = json.loads(proc.stdout.split('\n')[0])
                    if data.get('url') and is_valid_m3u8(data['url']):
                        result['m3u8_url'] = data['url']
                    elif data.get('formats'):
                        for f in reversed(data['formats']):
                            if f.get('url') and is_valid_m3u8(f['url']):
                                result['m3u8_url'] = f['url']
                                break
            except Exception as e:
                result['errors'].append(f"yt-dlp fallback failed: {e}")
                
    seen = set()
    unique_subs = []
    for s in result['subtitles']:
        if s['url'] not in seen:
            seen.add(s['url'])
            unique_subs.append(s)
    result['subtitles'] = unique_subs
    
    return result

def is_valid_m3u8(url):
    if not url.startswith('http'):
        return False
    if '.m3u8' not in url:
        return False
    invalid_exts = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp']
    lower_url = url.lower()
    if any(lower_url.endswith(ext) for ext in invalid_exts):
        return False
    if 'image' in lower_url and ('cdn' in lower_url or 'thumb' in lower_url):
        return False
    return True

def unpack_js(html):
    match = re.search(r'eval\((function\(p,a,c,k,e,?[rd]?\).*?)\)', html)
    if not match:
        return ""
    return match.group(1)

def extract_subtitles(html):
    subs = []
    patterns = [
        r'https?://[^"\'\s]+\.(?:srt|vtt|ass)',
        r'subtitle["\']?\s*:\s*["\'](http[^"\']+)["\']',
        r'subtitles["\']?\s*:\s*["\'](http[^"\']+)["\']',
        r'sub["\']?\s*:\s*["\'](http[^"\']+)["\']',
        r'track["\']?\s*:\s*["\'](http[^"\']+)["\']'
    ]
    soup = BeautifulSoup(html, 'lxml')
    for track in soup.find_all('track'):
        if track.get('kind') in ['subtitles', 'captions']:
            src = track.get('src')
            if src and src.startswith('http'):
                subs.append({'url': src, 'lang': detect_lang(src, track.get('srclang', ''))})
                
    for p in patterns:
        for match in re.findall(p, html):
            if match.startswith('http'):
                subs.append({'url': match, 'lang': detect_lang(match, '')})
    return subs

def detect_lang(url, srclang):
    s = f"{url} {srclang}".lower()
    if 'bn' in s or 'bangla' in s or 'bengali' in s:
        return 'bn'
    return 'en'