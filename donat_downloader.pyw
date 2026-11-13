"""Donat Downloader — pengunduh video/musik (YouTube + ~1.700 situs lain via yt-dlp) untuk Windows 11 (tkinter/Sun Valley)."""
import contextlib, ctypes, ctypes.wintypes, functools, gc, glob, hashlib, json, os, queue, re, shutil, socket, subprocess, sys
import threading, time, traceback, urllib.parse, urllib.request, winreg, winsound, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib import metadata
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, font as tkfont

import pystray, sv_ttk
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

APP = 'Donat Downloader'
DATA = os.path.join(os.environ['APPDATA'], 'DonatDownloader')
THUMBS = os.path.join(DATA, 'thumbs')
UPDATES = os.path.join(DATA, 'updates')  # newer yt-dlp (+ yt-dlp-ejs) wheels fetched by the app, used over the bundled copy
CACHE = os.path.join(os.environ.get('LOCALAPPDATA', DATA), 'DonatDownloader', 'cache')  # yt-dlp's (player JS, solvers)
_getaddrinfo = socket.getaddrinfo


def v4_first(*args, **kwargs):
    """IPv4 addresses first, IPv6 kept as the fallback. Connections try addresses one at a time with the full timeout
    (20 s) each: where IPv6 to Google drops out now and then, all 8 YouTube IPv6 addresses timed out before IPv4 was
    tried = 160 s stuck on "Preparing…". On an IPv6-only network IPv4 fails instantly (unreachable), so no cost there."""
    return sorted(_getaddrinfo(*args, **kwargs), key=lambda r: r[0] != socket.AF_INET)


socket.getaddrinfo = v4_first


def vtuple(v): return tuple(int(x) for x in re.findall(r'\d+', v))


def use_updates():
    """Put the newest downloaded wheels ahead of the bundled yt-dlp when they are newer; drop the rest: older updates
    (an update can't delete the wheel the running app has loaded, so it lands next to it) and stale ones (new build)."""
    try:
        bundled = metadata.version('yt-dlp')
    except metadata.PackageNotFoundError:
        bundled = '0'
    wheels = glob.glob(os.path.join(UPDATES, '*.whl'))
    newest = {}  # package -> (version, wheel)
    for w in wheels:
        pkg, ver = os.path.basename(w).split('-')[:2]
        if pkg not in newest or vtuple(ver) > vtuple(newest[pkg][0]):
            newest[pkg] = (ver, w)
    keep = [w for _, w in newest.values()] if vtuple(newest.get('yt_dlp', ('0',))[0]) > vtuple(bundled) else []
    for w in set(wheels) - set(keep):  # nothing is imported yet: nothing locked
        with contextlib.suppress(OSError):
            os.remove(w)
    sys.path[:0] = keep  # zipimport: a pure-python wheel is importable as-is


@functools.lru_cache(64)
def _query_parts(url):
    u = urllib.parse.urlparse(url)
    return u, {k: urllib.parse.urlencode({k: v}, True) for k, v in urllib.parse.parse_qs(u.query).items()}


def fast_query(orig):
    """yt-dlp's YouTube extractor builds ~23,000 caption URLs per video (every auto-translation language x format) from
    a handful of base URLs, re-parsing and re-encoding each one in full: ~3 s of GIL-holding CPU per extraction, which
    also stuttered the UI. Same URLs, with each base URL parsed and encoded once."""
    def update_url_query(*args, **kwargs):
        if len(args) != 2 or kwargs or not isinstance(args[0], str) or not args[1]:
            return orig(*args, **kwargs)
        u, parts = _query_parts(args[0])
        parts = {**parts, **{k: urllib.parse.urlencode({k: v}, True) for k, v in args[1].items()}}
        return urllib.parse.urlunparse(u._replace(query='&'.join(p for p in parts.values() if p)))
    return update_url_query


_ytdl, _ytdl_lock = None, threading.Lock()


def ytdl():
    """yt-dlp, imported on first use (~0.65 s, always from a worker thread) so the window doesn't wait for it."""
    global _ytdl
    with _ytdl_lock:
        if _ytdl is None:
            use_updates()
            try:
                import yt_dlp
            except Exception:  # a broken update must never brick the app: fall back to the bundled yt-dlp
                sys.path[:] = [p for p in sys.path if not p.startswith(UPDATES)]
                for m in [m for m in sys.modules if m.startswith('yt_dlp')]:
                    del sys.modules[m]
                shutil.rmtree(UPDATES, ignore_errors=True)
                import yt_dlp
            # QuickJS re-parses YouTube's 3 MB player for every video (11.8 s); yt-dlp can cache the parsed player
            # (0.7 s after the first video per player version) but ships it off because it never rotates the files
            with contextlib.suppress(Exception):  # ponytail: yt-dlp internal flag; harmless if a later yt-dlp renames it
                from yt_dlp.extractor.youtube.jsc._builtin import ejs
                ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
            with contextlib.suppress(Exception):  # ponytail: patches a module global; a renamed one just isn't sped up
                from yt_dlp.extractor.youtube import _video
                _video.update_url_query = fast_query(_video.update_url_query)
            for old in sorted(glob.glob(os.path.join(CACHE, 'challenge-solver', '*')), key=os.path.getmtime)[:-2]:
                with contextlib.suppress(OSError):  # the rotation: ~4 MB per player version, keep the 2 newest
                    os.remove(old)
            _ytdl = yt_dlp
    return _ytdl

PORT = 51677  # ponytail: fixed localhost port doubles as single-instance lock; change it if another app owns it
YT = re.compile(r'(?:youtube\.com/(?:watch\?(?:\S*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})')
H1 = ('Segoe UI Variable Display Semibold', 16)
THUMB = (64, 36)  # list thumbnails (px at 100%)
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'

HERE = [p for p in (getattr(sys, '_MEIPASS', ''), os.path.dirname(sys.executable)) if p]  # frozen exe / venv Scripts
def find(exe, *first): return shutil.which(exe, path=os.pathsep.join(list(first) + HERE + [os.environ.get('PATH', '')]))
DEV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build')


def unpack_tools():
    """ffmpeg + ffprobe (minimal build, build-ffmpeg.sh) and QuickJS (JS runtime for YouTube) ride inside the exe as
    one LZMA zip. Unpacked once per build to LOCALAPPDATA: a onefile exe re-extracts its files on every launch, and
    Windows Defender re-scans every fresh .exe, which cost ~1.2 s per start."""
    z = os.path.join(getattr(sys, '_MEIPASS', DEV), 'tools.zip')
    if not os.path.exists(z):
        return None
    with zipfile.ZipFile(z) as zf:  # build identity from the zip's own CRCs: no need to hash 10 MB on every start
        key = hashlib.sha256(repr([(i.filename, i.CRC, i.file_size) for i in zf.infolist()]).encode()).hexdigest()[:12]
    root = os.path.join(os.environ.get('LOCALAPPDATA', DATA), 'DonatDownloader', 'tools')
    d = os.path.join(root, key)
    if not os.path.exists(os.path.join(d, 'ok')):
        shutil.rmtree(d + '.tmp', ignore_errors=True)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(d + '.tmp')
        open(os.path.join(d + '.tmp', 'ok'), 'w').close()
        shutil.rmtree(d, ignore_errors=True)
        os.replace(d + '.tmp', d)
        for old in glob.glob(os.path.join(root, '*')):  # tools of older builds
            if old != d:
                shutil.rmtree(old, ignore_errors=True)
    return d


TOOLS = unpack_tools()
QJS = find('qjs', *filter(None, [TOOLS, DEV]))
FFMPEG = find('ffmpeg', *filter(None, [TOOLS, os.path.join(DEV, 'ffmpeg-min')]))
URL = re.compile(r'https?://[^\s<>"\']+')
SITES = []  # yt-dlp site extractors (not the catch-all generic one); filled in the background by warm_sites()

CODECS = {'Auto': None, 'H.264 (paling kompatibel)': 'vcodec:h264', 'VP9': 'vcodec:vp9', 'AV1': 'vcodec:av01'}
RES = ('Terbaik', '2160p', '1440p', '1080p', '720p', '480p', '360p')
FORMAT_ROWS = (('Mode', 'kind', ('Video', 'Audio')), ('Resolusi', 'res', RES),
               ('Container', 'container', ('mp4', 'mkv', 'webm')), ('Codec video', 'codec', tuple(CODECS)),
               ('Format audio', 'afmt', ('mp3', 'm4a', 'opus', 'flac', 'wav')),
               ('Bitrate MP3', 'abr', ('320', '256', '192', '128')),
               ('Sematkan thumbnail & metadata', 'embed', None), ('Subtitle Indonesia/Inggris', 'subs', None))
VIDEO_ONLY, AUDIO_ONLY = {'res', 'container', 'codec', 'subs'}, {'afmt', 'abr'}  # fields shown per Mode
def downloads_dir():  # the user's real Downloads folder (Windows lets it be moved, e.g. to D:\)
    with contextlib.suppress(OSError), winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as k:
        return os.path.expandvars(winreg.QueryValueEx(k, '{374DE290-123F-4565-9164-39C4925E467B}')[0])
    return os.path.join(os.path.expanduser('~'), 'Downloads')


DEFAULTS = {'kind': 'Video', 'res': 'Terbaik', 'container': 'mp4', 'codec': 'Auto', 'afmt': 'mp3', 'abr': '320',
            'embed': True, 'subs': False, 'folder': os.path.join(downloads_dir(), APP),  # created on first download
            'conn': '8', 'parallel': '3', 'clip': True, 'theme': 'Sistem', 'cookies': 'Otomatis', 'autoupdate': True,
            'rate': 'Tanpa batas', 'lang': 'English'}
RATES = {'Tanpa batas': None, '500 KB/s': 500 << 10, '1 MB/s': 1 << 20, '2 MB/s': 2 << 20, '5 MB/s': 5 << 20, '10 MB/s': 10 << 20}
COOKIES_TXT = 'File cookies.txt…'
BROWSERS = ('Otomatis', 'firefox', 'edge', 'chrome', 'helium', 'brave', 'opera', 'vivaldi', COOKIES_TXT)
AUTO = ('Otomatis', 'Tidak')  # 'Tidak' = value saved by older versions
APP_COOKIES = os.path.join(DATA, 'cookies.txt')  # the app's own copy of the YouTube login (youtube.com cookies only)
# Chromium forks yt-dlp doesn't know by name: (base browser, profile folder)
PROFILES = {'helium': ('chrome', os.path.expandvars(r'%LOCALAPPDATA%\imput\Helium\User Data\Default'))}
NAV = (('Dashboard', chr(0xE80F)), ('Unduhan', chr(0xE896)), ('Pengaturan', chr(0xE713)))  # Fluent: Home/Download/Settings
LANGS = ('English', 'Indonesia')
LANG = 'English'  # set from the settings in App.__init__, before any UI is built
# The UI is written in Indonesian; those strings are the keys. Setting values (e.g. 'Terbaik', 'Otomatis') stay
# Indonesian on disk and are only translated for display, so old settings files keep working.
EN = {
    # sidebar, pages, tray
    'Unduhan': 'Downloads', 'Pengaturan': 'Settings', 'Pantau clipboard': 'Watch clipboard',
    'Buka Donat Downloader': 'Open Donat Downloader', 'Keluar': 'Quit',
    # format options and their values
    'Resolusi': 'Resolution', 'Codec video': 'Video codec', 'Format audio': 'Audio format',
    'Sematkan thumbnail & metadata': 'Embed thumbnail & metadata', 'Subtitle Indonesia/Inggris': 'Indonesian/English subtitles',
    'Terbaik': 'Best', 'H.264 (paling kompatibel)': 'H.264 (most compatible)', 'Otomatis': 'Auto',
    'Tanpa batas': 'Unlimited', 'Sistem': 'System', 'Terang': 'Light', 'Gelap': 'Dark', 'File cookies.txt…': 'cookies.txt file…',
    # dashboard
    'Cari': 'Search', 'Judul': 'Title', 'Durasi': 'Duration', 'Ukuran': 'Size', 'Tanggal': 'Date',
    '{} unduhan  ·  {} total  ·  klik 2x untuk memutar, klik kanan untuk opsi':
        '{} downloads  ·  {} total  ·  double-click to play, right-click for options',
    'Buka file': 'Open file', 'Tampilkan di folder': 'Show in folder', 'Salin link': 'Copy link',
    'Unduh lagi (pilih format)…': 'Download again (choose format)…', 'Hapus dari riwayat': 'Remove from history',
    'Hapus file asli…': 'Delete original file…', 'File sudah dipindah atau dihapus:\n{}': 'The file was moved or deleted:\n{}',
    # downloads page
    'Tempel link video/musik (YouTube, TikTok, Instagram, X, SoundCloud, …) — format langsung bisa diatur':
        'Paste a video/music link (YouTube, TikTok, Instagram, X, SoundCloud, …) — set the format right away',
    'Unduh': 'Download', 'Potong (opsional)': 'Clip (optional)', 'dari': 'from', 'sampai': 'to',
    'contoh 1:30 — kosongkan untuk video penuh': 'e.g. 1:30 — leave empty for the full video',
    'Jeda': 'Pause', 'Lanjutkan': 'Resume', 'Batalkan': 'Cancel', 'Bersihkan yang selesai': 'Clear finished',
    'atau seret link ke jendela ini': 'or drag a link onto this window', 'Buka folder': 'Open folder',
    'Status': 'Status', 'Progres': 'Progress', 'Kecepatan': 'Speed', 'Sisa': 'Left',
    'Memuat info & format…': 'Loading info & formats…', 'memuat daftar resolusi…': 'loading resolutions…',
    'Gagal memuat: {}': 'Failed to load: {}',
    'Playlist/channel · {} item — Unduh akan meminta konfirmasi': 'Playlist/channel · {} items — Download will ask to confirm',
    'Unduh semua {} item dari playlist/channel ini?\nFile disimpan di subfolder bernama playlist di:\n{}':
        'Download all {} items of this playlist/channel?\nFiles go to a subfolder named after the playlist in:\n{}',
    'Waktu potong tidak dikenali: {}\nContoh: 90, 1:30, atau 1:02:03': 'Clip time not recognised: {}\nExamples: 90, 1:30 or 1:02:03',
    'Waktu "sampai" harus setelah waktu "dari".': 'The "to" time must be after the "from" time.',
    'potong': 'clip', 'akhir': 'end',
    # queue states
    'Menunggu': 'Queued', 'Mulai mengunduh…': 'Download started…', 'Dijeda': 'Paused', 'Menyiapkan…': 'Preparing…',
    'Dibatalkan': 'Cancelled', 'Gagal (klik 2x)': 'Failed (double-click)', 'Gagal: {}': 'Failed: {}', 'Memotong…': 'Clipping…',
    'Memakai login YouTube…': 'Using YouTube login…', 'Mencoba ulang…': 'Retrying…', 'Mengunduh {}': 'Downloading {}',
    'Memproses…': 'Processing…', 'Selesai ✓': 'Done ✓', 'Membatalkan…': 'Cancelling…', 'Menjeda…': 'Pausing…',
    'Buka folder unduhan': 'Open download folder', 'Coba lagi': 'Retry', 'Lihat error': 'View error',
    'Hapus dari daftar': 'Remove from list', '{} · {} unduhan · {}': '{} · {} downloads · {}',
    # notifications, dialogs
    'Selesai: tidak ada file baru': 'Done: no new files', 'Unduhan selesai  ·  {}  ·  {}': 'Download complete  ·  {}  ·  {}',
    '  ·  +{} lagi': '  ·  +{} more', 'Link {} terdeteksi': '{} link detected', 'Playlist/channel {} terdeteksi': '{} playlist/channel detected',
    'Download dengan Donat Downloader': 'Download with Donat Downloader', 'Lihat di Donat Downloader': 'View in Donat Downloader',
    'Tetap berjalan di tray. Salin link YouTube untuk mengunduh.': 'Still running in the tray. Copy a video link to download.',
    '{} unduhan masih berjalan. Batalkan dan keluar?': '{} downloads still running. Cancel them and quit?',
    'Terjadi error tak terduga. Detail: {}': 'An unexpected error occurred. Details: {}',
    '\n… dan {} lainnya': '\n… and {} more', 'Pindahkan {} file ke Recycle Bin?\n\n{}': 'Move {} file(s) to the Recycle Bin?\n\n{}',
    'Sebagian file tidak bisa dihapus (sedang dipakai aplikasi lain?).': 'Some files could not be deleted (in use by another app?).',
    'Potong': 'Cut', 'Salin': 'Copy', 'Tempel': 'Paste', 'Pilih semua': 'Select all',
    # settings
    'Format default': 'Default format', 'Umum': 'General', 'Koneksi paralel': 'Parallel connections',
    'Unduhan bersamaan*': 'Simultaneous downloads*', 'Login YouTube': 'YouTube login', 'Tema': 'Theme',
    'Batas kecepatan': 'Speed limit', 'Bahasa': 'Language', 'Folder unduhan': 'Download folder', 'Pilih…': 'Browse…',
    'Jalankan saat Windows mulai (langsung ke tray)': 'Start with Windows (straight to the tray)',
    '* berlaku setelah aplikasi dibuka ulang': '* takes effect after restarting the app', 'Pembaruan': 'Updates',
    'Perbarui yt-dlp (mesin unduh YouTube) otomatis saat aplikasi dibuka':
        'Update yt-dlp (the download engine) automatically when the app starts',
    'Memeriksa komponen…': 'Checking components…', 'Periksa sekarang': 'Check now', 'Memeriksa pembaruan…': 'Checking for updates…',
    'Pilih cookies.txt (format Netscape)': 'Choose cookies.txt (Netscape format)',
    'yt-dlp {} diunduh (aktif setelah aplikasi dibuka ulang)': 'yt-dlp {} downloaded (active after restarting the app)',
    'sudah versi terbaru': 'already up to date', 'gagal memeriksa pembaruan: {}': 'update check failed: {}',
    'tidak ada': 'missing', ' (diperbarui)': ' (updated)', '(bawaan exe)': '(built in)',
    'checksum {} tidak cocok': 'checksum of {} does not match',
    'Pemeriksaan dibatalkan': 'Check cancelled', 'Mulai ulang untuk menerapkan': 'Restart to apply',
    'Mengunduh pembaruan… {}': 'Downloading update… {}', 'Menerapkan pembaruan…': 'Applying update…',
    # errors
    'YouTube meminta login (anti-bot). Login YouTube di browser Anda lalu coba lagi, atau pilih sumbernya di Pengaturan → Login YouTube. ':
        'YouTube asks for a login (anti-bot). Sign in to YouTube in your browser and try again, or pick the source in Settings → YouTube login. ',
    'Cookies browser terkunci karena browser masih terbuka. Tutup browser sepenuhnya, atau pakai "{}" di Pengaturan. ':
        "The browser's cookies are locked because it is still open. Close it completely, or use \"{}\" in Settings. ",
    'YouTube meminta login (anti-bot). Tutup {} sebentar supaya login YouTube-nya bisa dipakai, lalu klik 2x baris ini → Coba lagi.':
        'YouTube asks for a login (anti-bot). Close {} for a moment so its YouTube login can be used, then double-click this row → Retry.',
    'YouTube meminta login (anti-bot). Login YouTube di browser Anda (Firefox/Edge/Chrome/Helium), lalu klik 2x baris ini → Coba lagi.':
        'YouTube asks for a login (anti-bot). Sign in to YouTube in your browser (Firefox/Edge/Chrome/Helium), then double-click this row → Retry.',
}


def T(s, *args):
    """UI text in the chosen language; args fill the {} slots."""
    s = EN.get(s, s) if LANG == 'English' else s
    return s.format(*args) if args else s


class QUIET:  # yt-dlp logger that swallows output (errors still arrive as exceptions); must be hashable
    debug = info = warning = error = staticmethod(lambda *a, **k: None)


def log(text):  # pythonw/.exe have no console: errors go to DATA/error.log
    with contextlib.suppress(OSError), open(os.path.join(DATA, 'error.log'), 'a', encoding='utf-8') as f:
        f.write(f'--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n')


def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, obj):  # write-then-rename: a crash never leaves a half-written history
    path = os.path.join(DATA, name)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(path + '.tmp', path)


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            break
        n /= 1024
    return f'{n:.1f} {unit}'


def clock(s): return time.strftime('%H:%M:%S' if s >= 3600 else '%M:%S', time.gmtime(s))
def errmsg(e):
    msg = re.sub(r'\x1b\[[\d;]*m|^ERROR: ', '', str(e) or type(e).__name__).strip()
    if 'not a bot' in msg:  # YouTube's IP-level bot check: only a logged-in browser session gets past it
        msg = T('YouTube meminta login (anti-bot). Login YouTube di browser Anda lalu coba lagi, '
                'atau pilih sumbernya di Pengaturan → Login YouTube. ') + msg
    elif 'Could not copy Chrome cookie database' in msg:  # Chromium browsers lock their cookie DB while running
        msg = T('Cookies browser terkunci karena browser masih terbuka. Tutup browser sepenuhnya, '
                'atau pakai "{}" di Pengaturan. ', T(COOKIES_TXT)) + msg
    return msg
def tkvars(values): return {k: (tk.BooleanVar if isinstance(v, bool) else tk.StringVar)(value=v) for k, v in values.items()}
def folder(o): return o['folder'] or DEFAULTS['folder']  # an emptied setting must not mean "current directory"
def cells(*vals): return tuple(f'  {v}' for v in vals)  # Tk 8.6 cells have no padding: align with headings, gap after cut-off text


def short_date(s):  # entries saved by older versions read '13 Oct 2026  04:11'
    with contextlib.suppress(ValueError):
        return datetime.strptime(s, '%d %b %Y  %H:%M').strftime('%Y/%m/%d %H:%M')
    return s


def yt_url(text):
    """Clean watch URL of the first YouTube video in `text` (drops tracking/playlist params), else None."""
    m = YT.search(text)
    return m and f"https://{'music' if 'music.youtube.com' in text else 'www'}.youtube.com/watch?v={m.group(1)}"


def warm_sites():  # first suitable() pass compiles ~1750 URL patterns (~0.9 s): do it once, off the UI thread
    sites = [ie for ie in ytdl().extractor.gen_extractor_classes() if ie.ie_key() != 'Generic']
    for i, ie in enumerate(sites):
        ie.suitable('https://warm.invalid/')
        if i % 4 == 3:  # regex compiling holds the GIL: pause often so the UI never stutters (~5 s total, UI unaffected)
            time.sleep(.01)
    SITES[:] = sites


def site_of(text):
    """(clean url, site name, is_list) for the first URL in `text` that yt-dlp supports: a single video/track, or a
    playlist/channel (is_list=True: downloaded only after the user confirms in the panel). Feeds etc. -> None."""
    m = URL.search(text)
    if not m:
        return None
    url = yt_url(m.group())
    if url:
        return url, 'youtube', False
    url = m.group()
    if re.match(r'https?://fb\.watch/\w', url):  # Facebook's share short link: yt-dlp follows the redirect
        return url, 'facebook', False
    youtube = re.match(r'https?://([^/]+\.)?(youtube\.com|youtu\.be)/', url)  # any non-video YouTube page is a list
    if youtube and not SITES:  # site patterns still warming up: play safe
        return url, 'youtube', True
    ie = next((ie for ie in SITES if ie.suitable(url)), None)
    if ie and (ie._RETURN_TYPE == 'playlist' or youtube and ie._RETURN_TYPE == 'any'):
        return url, ie.IE_NAME.split(':')[0], True
    if ie and ie._RETURN_TYPE in ('video', 'any') and not youtube:
        return url, ie.IE_NAME.split(':')[0], False


def parse_time(text):
    """'90', '1:30' or '1:02:03' -> seconds; '' -> None; anything else -> ValueError."""
    text = text.strip()
    if not text:
        return None
    if not re.fullmatch(r'\d+(:\d{1,2}){0,2}(\.\d+)?', text):
        raise ValueError(text)
    secs = 0.0
    for part in text.split(':'):
        secs = secs * 60 + float(part)
    return secs


def cut_range(cut): return f"{clock(cut[0])}–{clock(cut[1]) if cut[1] is not None else T('akhir')}"  # end None = to the end
def cut_tag(cut): return T('potong') + ' ' + cut_range(cut).replace(':', '.').replace('–', '-')


def cut_file(path, start, end=None):
    """Trim to [start, end] s (end None = to the end) by stream copy (the bundled ffmpeg has no video encoders, and copy
    is lossless): audio is cut on the frame, video from the keyframe at or just before `start`. FLAC is re-encoded
    (losslessly) because a copied FLAC stream keeps the old length in its header. Chapters no longer fit: dropped."""
    root, ext = os.path.splitext(path)
    tmp = f'{root}.cut{ext}'
    try:
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-ss', f'{start:.3f}', '-i', path,
                        *(['-t', f'{end - start:.3f}'] if end is not None else []), '-map', '0', '-c', 'copy',
                        *(['-c:a', 'flac'] if ext.lower() == '.flac' else []), '-map_chapters', '-1', '-map_metadata', '0', tmp],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def ffmpeg_version(exe):
    try:
        out = subprocess.run([exe, '-version'], capture_output=True, text=True, timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError, TypeError):
        return None
    m = re.match(r'ffmpeg version n?(\d[\d.]*)', out)
    return m and m.group(1)


def is_leftover(suffix):  # temp files of a download: .part/.ytdl/.f137.mp4 + not-yet-embedded cover/subs
    return bool(re.search(r'\.(part|ytdl|f[\w-]+\.\w+)|\.(webp|jpe?g|png|vtt|srt)$', suffix))


def remove_leftovers(stem, since):
    """Delete a cancelled download's temp files: leftover-looking AND written since the download started (file names
    carry no video id, so an older file of the same title is the user's). Retries: fragment threads may hold a file."""
    def ours(p):
        with contextlib.suppress(OSError):
            return is_leftover(p[len(stem):]) and os.path.getmtime(p) >= since - 2
    for _ in range(5):
        left = [p for p in glob.glob(glob.escape(stem) + '.*') if ours(p)]
        for p in left:
            with contextlib.suppress(OSError):
                os.remove(p)
        if not any(map(os.path.exists, left)):
            return
        time.sleep(1)


def height(label):
    m = re.match(r'\d+', label)
    return int(m.group()) if m else None


def thumb_url(info):  # YouTube's 16:9 mqdefault is small and fast; other sites give whatever they have
    return f"https://i.ytimg.com/vi/{info['id']}/mqdefault.jpg" if info.get('extractor_key') == 'Youtube' else info.get('thumbnail')


def fetch_thumb(key, url):
    """Cache a thumbnail as THUMBS/<key>.jpg; returns its path or None."""
    path = os.path.join(THUMBS, re.sub(r'[^\w-]', '_', key) + '.jpg')
    if not os.path.exists(path) and url:
        tmp = f'{path}.{threading.get_ident()}.tmp'  # per thread: popup and queue may fetch the same video at once
        try:
            urllib.request.urlretrieve(url, tmp)
            os.replace(tmp, path)
        except (OSError, ValueError):
            return None
    return path if os.path.exists(path) else None


def oembed(vid):  # title + channel in ~200 ms, without a full yt-dlp extraction
    try:
        with urllib.request.urlopen('https://www.youtube.com/oembed?format=json&url=' +
                                    urllib.parse.quote(f'https://www.youtube.com/watch?v={vid}'), timeout=8) as r:
            return json.load(r)
    except (OSError, ValueError):
        return {}


def res_labels(info):
    """'1080p60 · ~245.3 MB' per available height, best first. The size is what Unduh fetches at that height: yt-dlp
    hands formats sorted worst -> best for the chosen codec/container, so the last video of a height + the best audio
    (not the largest file there: a 388 MB VP9 and a 59 MB AV1 can both be 1080p)."""
    fs, dur = info.get('formats') or [], info.get('duration') or 0
    def size(f): return f.get('filesize') or f.get('filesize_approx') or (f.get('tbr') or 0) * dur * 125
    vids = [f for f in fs if f.get('height') and f.get('vcodec') != 'none']
    if 'youtube' in (info.get('extractor') or '') and any('m3u8' not in (f.get('protocol') or '') for f in vids):
        vids = [f for f in vids if 'm3u8' not in (f.get('protocol') or '')]  # as ydl_opts: YouTube's HLS is skipped
    audio = next((size(f) for f in reversed(fs) if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')), 0)
    best = {f['height']: f for f in vids}  # later = better
    return [f"{h}p{int(f['fps']) if (f.get('fps') or 0) > 30 else ''}" + (f' · ~{human(size(f) + audio)}' if size(f) else '')
            for h, f in sorted(best.items(), reverse=True)]


def ydl_opts(o, hook=None, url=''):
    """Option values (keys as in DEFAULTS) -> yt-dlp params."""
    opts = {'outtmpl': os.path.join(folder(o), '%(title)s.%(ext)s'), 'noplaylist': True, 'logger': QUIET,
            'progress_hooks': [hook] if hook else [], 'postprocessors': [],
            # an existing file with the same name is replaced, never trusted: yt-dlp would otherwise skip the download
            # and run the postprocessors on it (a broken leftover = "Invalid data found"). Finished .fNNN parts and
            # .part files still resume (continuedl), so Lanjutkan / 403 retries lose nothing
            'overwrites': True, 'cachedir': CACHE,
            # 'dashy' turns YouTube's plain HTTP streams into ranged fragments, so `conn` parallel connections apply
            'extractor_args': {'youtube': {'formats': ['dashy']}}, 'concurrent_fragment_downloads': int(o['conn'])}
    if o.get('playlist'):  # one subfolder per playlist; a deleted/private item is skipped instead of stopping the rest
        opts.update(outtmpl={'default': os.path.join(folder(o), '%(playlist_title|playlist)s', '%(title)s.%(ext)s'),
                             'pl_thumbnail': ''}, ignoreerrors=True,  # '' = don't save the playlist's own cover
                    overwrites=False)  # re-running a playlist skips what's already there instead of re-downloading all
    if o.get('cut'):  # own file name: the trimmed copy must never overwrite (or be mistaken for) a full download
        opts.update(outtmpl=os.path.join(folder(o), f"%(title)s [{cut_tag(o['cut'])}].%(ext)s"), overwrites=True)
    if RATES.get(o.get('rate')):  # yt-dlp limits each connection separately, so one connection = an exact limit
        opts.update(ratelimit=RATES[o['rate']], concurrent_fragment_downloads=1)
    if QJS:
        opts['js_runtimes'] = {'quickjs': {'path': QJS}}
    if FFMPEG:
        opts['ffmpeg_location'] = FFMPEG
    cookies = o.get('cookies', 'Tidak')  # logged-in session: passes bot checks, age/member-restricted videos
    if cookies.lower().endswith('.txt'):
        opts['cookiefile'] = cookies  # exported cookies.txt: works while the browser is open
    elif cookies not in AUTO:
        opts['cookiesfrombrowser'] = PROFILES.get(cookies, (cookies,))
    audio = o['kind'] == 'Audio'
    if audio:
        opts['format'] = 'ba[ext=m4a]/ba/b' if o['afmt'] == 'm4a' else 'ba/b'
        opts['postprocessors'].append({'key': 'FFmpegExtractAudio', 'preferredcodec': o['afmt'], 'preferredquality': o['abr']})
    else:
        h = height(o['res'])
        opts['format'] = 'bv*+ba/b'
        if re.search(r'youtu\.?be', url):  # YouTube's HLS copies (616 "Premium"…): 2-5x slower than DASH, no size known
            opts['format'] = 'bv*[protocol!*=m3u8]+ba/' + opts['format']  # live streams (HLS only) still fall through
        opts['format_sort'] = [s for s in (CODECS.get(o['codec']), h and f'res:{h}',
                                           {'mp4': 'ext:mp4:m4a', 'webm': 'ext:webm:webm'}.get(o['container'])) if s]
        opts['merge_output_format'] = o['container']
        if o['subs']:
            opts.update(writesubtitles=True, subtitleslangs=['id.*', 'en.*'])
            opts['postprocessors'].append({'key': 'FFmpegEmbedSubtitle'})
    if o['embed']:
        opts['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True})
        if (o['afmt'] if audio else o['container']) not in ('wav', 'webm'):  # yt-dlp can't embed covers into these
            opts['writethumbnail'] = True
            opts['postprocessors'].append({'key': 'EmbedThumbnail'})
    return opts


def grab_cookies():
    """Copy the YouTube login from the first browser that has one into APP_COOKIES (youtube.com cookies only).
    Returns (path or None, browsers whose cookie DB is locked because they are running)."""
    locked = []
    for name in BROWSERS[1:-1]:
        try:
            jar = ytdl().cookies.extract_cookies_from_browser(*PROFILES.get(name, (name,)), logger=QUIET)
        except Exception as e:  # not installed, locked while running, or undecryptable
            if 'Could not copy' in str(e):
                locked.append(name)
            continue
        yt = [c for c in jar if c.domain.endswith('youtube.com')]
        if any(c.name in ('SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO') for c in yt):
            out = ytdl().cookies.YoutubeDLCookieJar(APP_COOKIES)
            for c in yt:
                out.set_cookie(c)
            out.save()
            return APP_COOKIES, locked
    return None, locked


class UpdateCancelled(Exception):
    pass


def update_ytdlp(step=lambda stage, frac=None: None, cancelled=lambda: False):
    """Fetch the newest yt-dlp + the yt-dlp-ejs version it pins from PyPI into UPDATES (sha256-verified).
    Returns the new version, or None if already current. Takes effect on the next start (see use_updates).
    step(stage, frac) reports 'check' / 'download' (0..1) / 'install'; cancelled() is polled between 64 KB chunks
    and raises UpdateCancelled before anything is written, so a cancel never leaves a half-installed update."""
    def pypi(path):
        with urllib.request.urlopen(f'https://pypi.org/pypi/{path}/json', timeout=20) as r:
            return json.load(r)

    def stop():
        if cancelled():
            raise UpdateCancelled()
    step('check')
    # newest version from the 13 KB release feed: the project JSON lists every release (~1.2 MB, up to a minute)
    with urllib.request.urlopen('https://pypi.org/rss/project/yt-dlp/releases.xml', timeout=20) as r:
        titles = [e.text or '' for e in ET.fromstring(r.read()).iter('title')]
    latest = max((v for v in titles if re.fullmatch(r'\d+(\.\d+)+', v)), key=vtuple)
    if vtuple(latest) <= vtuple(ytdl().version.__version__):
        return None
    stop()
    meta = pypi(f'yt-dlp/{latest}')
    pins = [m.group(1) for r in meta['info'].get('requires_dist') or [] for m in [re.match(r'yt-dlp-ejs==([\w.]+)', r)] if m]
    wheels = [next(u for u in release['urls'] if u['packagetype'] == 'bdist_wheel')
              for release in [meta] + ([pypi(f'yt-dlp-ejs/{pins[0]}')] if pins else [])]
    total, got, files = sum(w['size'] for w in wheels) or 1, 0, []
    for wheel in wheels:
        chunks = []
        with urllib.request.urlopen(wheel['url'], timeout=60) as r:
            while chunk := r.read(1 << 16):
                stop()
                chunks.append(chunk)
                got += len(chunk)
                step('download', got / total)
        data = b''.join(chunks)
        if hashlib.sha256(data).hexdigest() != wheel['digests']['sha256']:  # code we will import: verify it
            raise ValueError(T('checksum {} tidak cocok', wheel['filename']))
        files.append((wheel['filename'], data))
    stop()
    step('install')
    # next to the current update, not over it: Windows locks the wheel this app has loaded (deleting the folder
    # failed silently and the next makedirs raised WinError 183). use_updates keeps the newest at the next start.
    os.makedirs(UPDATES, exist_ok=True)
    for name, data in files:
        path = os.path.join(UPDATES, name)
        if not os.path.exists(path):  # same name = same verified release (e.g. an unchanged yt-dlp-ejs pin, maybe loaded)
            with open(path + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(path + '.tmp', path)  # never a half-written wheel on sys.path
    return latest


def autostart_cmd(): return f'"{sys.executable}" --tray'


def autostart(enable=None):
    """Read (enable=None) or switch the 'start with Windows' Run entry; returns whether it is on.
    Only the built exe writes it: a dev/script run must never re-point the user's startup entry at itself."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if not getattr(sys, 'frozen', False):
            pass
        elif enable:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, autostart_cmd())
        elif enable is False:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(k, APP)
        try:
            return bool(winreg.QueryValueEx(k, APP)[0])
        except FileNotFoundError:
            return False


def reveal(path):  # Explorer with the file selected; just the folder when the file is gone
    if os.path.exists(path):
        subprocess.Popen(f'explorer /select,"{path}"')
    elif os.path.isdir(os.path.dirname(path)):
        os.startfile(os.path.dirname(path))


def recycle(path):
    """Move a file to the Recycle Bin (undoable, unlike os.remove). Returns True on success."""
    class OP(ctypes.Structure):
        _fields_ = [('hwnd', ctypes.wintypes.HWND), ('wFunc', ctypes.c_uint), ('pFrom', ctypes.c_wchar_p),
                    ('pTo', ctypes.c_wchar_p), ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', ctypes.c_int),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', ctypes.c_wchar_p)]
    op = OP(None, 3, os.path.abspath(path) + '\0', None, 0x0040 | 0x0010 | 0x0400 | 0x0004)  # FO_DELETE: undo, quiet
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path)


def add_cut(y, o, started=lambda: None):
    """Hook the clip cut into yt-dlp's post-processing. Audio: right after conversion, before cover/metadata are
    embedded (those streams don't survive a copy-cut into m4a/opus). Video: last, so embedded subtitles shift with it."""
    class Cut(ytdl().postprocessor.PostProcessor):
        def run(self, info):
            started()
            cut_file(info['filepath'], *o['cut'])
            return [], info
    pps = y._pps['post_process']  # ponytail: yt-dlp internal list; re-check on a yt-dlp major change
    pps.insert(1 if o['kind'] == 'Audio' else len(pps), Cut(y))


def track_parts(y, t):
    """Before each video downloads, note its parts' expected sizes (video and audio come one after the other),
    so the row shows one size and one 0-100% for the whole file instead of restarting per part."""
    class Plan(ytdl().postprocessor.PostProcessor):
        def run(self, info):
            dur = info.get('duration') or 0
            # (bytes, exact?): an exact size beats yt-dlp's per-fragment guess, which wanders while it downloads
            t.parts = {f.get('format_id'): (f['filesize'], True) if f.get('filesize') else
                       (f.get('filesize_approx') or (f.get('tbr') or 0) * dur * 125, False)
                       for f in info.get('requested_formats') or [info]}
            t.got, t.peak = {}, 0
            return [], info
    y.add_post_processor(Plan(y), when='before_dl')


def history_entry(t, info):
    """History row for one finished video (`info` = yt-dlp result of that video)."""
    dl, o = info['requested_downloads'][0], t.o
    path = dl.get('filepath') or ''
    fmt = (o['afmt'].upper() + (f" · {o['abr']} kbps" if o['afmt'] == 'mp3' else '') if o['kind'] == 'Audio'
           else f"{(dl.get('ext') or '').upper()} · {dl.get('height') or '?'}p") + (
        f" · {cut_range(o['cut'])}" if o.get('cut') else '')
    return {'url': info.get('webpage_url') or t.url, 'title': info.get('title') or t.url,
            'channel': info.get('uploader') or info.get('channel') or '', 'duration': info.get('duration') or 0,
            'fmt': fmt, 'path': path, 'size': os.path.getsize(path) if os.path.exists(path) else 0,
            'date': datetime.now().strftime('%Y/%m/%d %H:%M'), 'thumb': fetch_thumb(info.get('id', 'x'), thumb_url(info))}


def light_system():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] == 1
    except OSError:
        return True


def glyph(ch, size, color):
    """Segoe Fluent Icons glyph as an image (MDL2 font on Windows 10)."""
    try:
        font = ImageFont.truetype('SegoeIcons.ttf', size)
    except OSError:
        font = ImageFont.truetype('segmdl2.ttf', size)
    im = Image.new('RGBA', (size + 4, size + 4))
    ImageDraw.Draw(im).text((im.width / 2, im.height / 2), ch, font=font, fill=color, anchor='mm')
    return im


def donut(size):
    """App icon drawn at runtime (pink-glazed donut), so there are no asset files to ship."""
    s = size * 4  # draw big, downscale = smooth edges
    im = Image.new('RGBA', (s, s))
    d = ImageDraw.Draw(im)
    d.ellipse((s * .04, s * .1, s * .96, s * .96), fill='#C8803E')
    d.ellipse((s * .06, s * .04, s * .94, s * .86), fill='#F472B6')
    for x, y, c in ((.3, .22, '#FDE68A'), (.68, .2, '#93C5FD'), (.78, .5, '#FFFFFF'), (.22, .52, '#86EFAC'), (.52, .72, '#FDE68A')):
        d.ellipse((s * (x - .04), s * (y - .04), s * (x + .04), s * (y + .04)), fill=c)
    d.ellipse((s * .36, s * .32, s * .64, s * .56), fill=(0, 0, 0, 0))
    return im.resize((size, size), Image.LANCZOS)


def no_full_redraw(root):
    """Tk registers its window classes (TkTopLevel, TkChild) with CS_HREDRAW|CS_VREDRAW, so every resize step repaints
    EVERY widget (~1.2 ms each: a 100-widget page took ~120 ms per step = laggy drag-resize). Without the flags Windows
    repaints only what moved or was uncovered (~10 ms). Class-wide: covers widgets created later too."""
    u = ctypes.windll.user32
    u.GetClassLongPtrW.restype = u.SetClassLongPtrW.restype = ctypes.c_size_t
    u.GetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
    root.update_idletasks()
    for h in (u.GetParent(root.winfo_id()), root.winfo_id()):  # TkTopLevel wrapper, TkChild
        u.SetClassLongPtrW(h, -26, u.GetClassLongPtrW(h, -26) & ~0x3)  # GCL_STYLE &= ~(CS_HREDRAW | CS_VREDRAW)


class App:
    def __init__(self, lock):
        global LANG
        self.lock = lock
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Donat.Downloader')
        with contextlib.suppress(OSError):  # Windows notifications: "Donat Downloader" + donut, not the raw ID and no icon
            ico = os.path.join(DATA, 'donat.ico')
            if not os.path.exists(ico):
                donut(256).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\AppUserModelId\Donat.Downloader') as k:
                winreg.SetValueEx(k, 'DisplayName', 0, winreg.REG_SZ, APP)
                winreg.SetValueEx(k, 'IconUri', 0, winreg.REG_SZ, ico)
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        self.root = root = tk.Tk()
        no_full_redraw(root)
        tray = '--tray' in sys.argv  # started with Windows: straight to the tray
        if tray:
            root.withdraw()
        root.report_callback_exception = self.crash
        self.scale = root.winfo_fpixels('1i') / 96
        self.q, self.imgs, self.tasks, self.pop, self.hinted, self.crashed = queue.Queue(), {}, {}, None, tray, False
        self.hist_rows, self.own_copy, self.loaded, self.url_job, self.p_info = 0, '', None, None, None
        self.ctxw, self.icons = None, {}  # open context menu, menu icons
        self.p_count = 0  # items in the link box's playlist/channel (0 = single video)
        self.upd_job, self.upd_ready, self.upd_base = None, False, ''  # yt-dlp update: running job, restart pending
        self.cfg = {**DEFAULTS, **load('settings.json', {})}
        LANG = self.cfg['lang'] if self.cfg['lang'] in LANGS else 'English'
        self.v = tkvars(self.cfg)
        self.history = load('history.json', [])
        self.pool = ThreadPoolExecutor(int(self.cfg['parallel']))
        self.logo, self.icon_img = ImageTk.PhotoImage(donut(self.px(34))), ImageTk.PhotoImage(donut(64))
        root.iconphoto(True, self.icon_img)  # -default: only the class icon (dialogs, cards)...
        root.iconphoto(False, self.icon_img)  # ...the taskbar reads this window's own icon (WM_SETICON): else Tk's feather
        root.title(APP)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(self.px(1120), sw - self.px(40)), min(self.px(700), sh - self.px(90))  # fits 150% laptops too
        root.geometry(f'{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - self.px(20))}')
        root.minsize(min(self.px(900), w), min(self.px(560), h))
        root.protocol('WM_DELETE_WINDOW', self.hide)
        root.bind_class('TEntry', '<Button-3>', self.entry_menu)
        self.autostart = tk.BooleanVar(value=autostart() and autostart(True))  # re-point at this exe if it moved
        self.autostart.trace_add('write', lambda *_: autostart(self.autostart.get()))
        self.build()
        self.apply_theme()
        for var in self.v.values():
            var.trace_add('write', self.save_settings)
        self.v['theme'].trace_add('write', self.apply_theme)
        self.v['lang'].trace_add('write', self.lang_changed)
        self.url.trace_add('write', self.url_changed)

        self.icon = pystray.Icon('donat', donut(64), APP, pystray.Menu(
            pystray.MenuItem(T('Buka Donat Downloader'), lambda: self.ui(self.show), default=True),
            pystray.MenuItem(T('Pantau clipboard'), lambda: self.ui(lambda: self.v['clip'].set(not self.v['clip'].get())),
                             checked=lambda _: self.cfg['clip']),
            pystray.MenuItem(T('Keluar'), lambda: self.ui(self.quit))))
        handlers = self.icon._message_handlers  # ponytail: pystray 0.19 internals; re-check on a pystray upgrade
        key = next(k for k, f in handlers.items() if f == self.icon._on_notify)
        on_notify = handlers[key]
        handlers[key] = lambda w, l: (self.ui(self.show) if l == 0x405 else None, on_notify(w, l))[1]  # NIN_BALLOONUSERCLICK
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self.listen, args=(lock,), daemon=True).start()
        # site patterns are only needed when a non-YouTube link is copied: compile them after the window has settled
        root.after(1500, lambda: threading.Thread(target=warm_sites, daemon=True).start())
        self.check_update(startup=True)
        self.clip_seq = ctypes.windll.user32.GetClipboardSequenceNumber()  # no popup for what was copied before launch
        self.pump()
        self.poll_clip()

    # ---------- plumbing ----------
    def px(self, n): return round(n * self.scale)
    def ui(self, fn, *a, **kw): self.q.put((fn, a, kw))  # worker/tray threads -> Tk thread
    def opts(self): return {k: v.get() for k, v in self.v.items()}

    def pump(self):  # 50 ms while downloads run, 250 ms when idle (fewer wake-ups for a tray app)
        busy = not self.q.empty() or any(not t.end for t in self.tasks.values())
        self.root.after(50 if busy else 250, self.pump)
        while not self.q.empty():
            fn, a, kw = self.q.get_nowait()
            fn(*a, **kw)

    def crash(self, *exc):  # UI-thread errors: log instead of failing silently
        log(''.join(traceback.format_exception(*exc)))
        if not self.crashed:
            self.crashed = True
            self.notify(T('Terjadi error tak terduga. Detail: {}', os.path.join(DATA, 'error.log')))

    def listen(self, lock):  # a second launch connects here -> show this window
        with contextlib.suppress(OSError):  # closed by restart()
            while True:
                lock.accept()[0].close()
                self.ui(self.show)

    def notify(self, msg):
        with contextlib.suppress(Exception):  # tray may not be up yet; a missed toast is harmless
            self.icon.notify(msg, APP)

    def save_settings(self, *_):
        self.cfg = self.opts()
        save('settings.json', self.cfg)
        self.icon.update_menu()

    def titlebar(self, win):  # dark/light Windows 11 title bar to match the theme
        win.update_idletasks()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(win.winfo_id()), 20,
                                                   ctypes.byref(ctypes.c_int(self.dark)), 4)

    def menu(self, e, items):
        """Windows 11-style context menu: rounded borderless popup in the theme's colours, Fluent icons, hover rows.
        items: (icon glyph, label, action or None) or None for a separator; items without an action are hidden."""
        shown = []  # hide what doesn't apply, and the separators that would be left dangling
        for it in items:
            if it is None:
                if shown and shown[-1] is not None:
                    shown.append(None)
            elif it[2]:
                shown.append(it)
        if shown and shown[-1] is None:
            shown.pop()
        if self.ctxw and self.ctxw.winfo_exists():
            self.ctxw.destroy()
        bg, hover, line, fg = self.menu_colors
        self.ctxw = w = tk.Toplevel(self.root, bg=line)
        w.overrideredirect(True)
        w.attributes('-topmost', True)
        f = tk.Frame(w, bg=bg, padx=self.px(4), pady=self.px(4))
        f.pack(padx=1, pady=1)

        def close(*_):
            if w.winfo_exists():
                w.destroy()
            self.root.unbind_all('<ButtonPress>')
            self.root.unbind('<FocusOut>')
            self.root.unbind_all('<Escape>')

        def click(e):  # any click in the app outside the menu closes it
            if not str(e.widget).startswith(str(w)):
                close()

        def lost_focus(e):  # the app was left (another window clicked): close once focus is really gone
            self.root.after(1, lambda: self.root.focus_get() is None and close())
        for it in shown:
            if it is None:
                tk.Frame(f, bg=line, height=1).pack(fill='x', padx=self.px(6), pady=self.px(4))
                continue
            row = tk.Label(f, text=f'{it[1]}        ', image=self.ico(it[0]), compound='left', anchor='w', bg=bg, fg=fg,
                           font=('Segoe UI', 10), padx=self.px(4))
            row.pack(fill='x')
            row.bind('<Enter>', lambda e, r=row: r.configure(bg=hover))
            row.bind('<Leave>', lambda e, r=row: r.configure(bg=bg))
            row.bind('<ButtonRelease-1>', lambda e, a=it[2]: (close(), a()))
        w.update_idletasks()
        x = min(e.x_root, w.winfo_screenwidth() - w.winfo_reqwidth() - 4)
        y = e.y_root if e.y_root + w.winfo_reqheight() < w.winfo_screenheight() - 40 else e.y_root - w.winfo_reqheight()
        w.geometry(f'+{x}+{y}')
        with contextlib.suppress(Exception):  # Windows 11 rounded corners (DWMWA_WINDOW_CORNER_PREFERENCE = round small)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(w.winfo_id()), 33,
                                                       ctypes.byref(ctypes.c_int(3)), 4)
        # no focus_force (window activation cost ~30 ms per open): close on outside click, Esc, or leaving the app
        self.root.bind_all('<ButtonPress>', click)
        self.root.bind_all('<Escape>', close)
        self.root.bind('<FocusOut>', lost_focus)

    def ico(self, ch):  # Fluent glyph for menus, cached per theme
        key = (ch, self.dark)
        if key not in self.icons:
            im = Image.new('RGBA', (self.px(30), self.px(28)))  # icon + air: gives the roomy Windows 11 row height
            g = glyph(ch, self.px(14), '#e6e6e6' if self.dark else '#1c1c1c')
            im.paste(g, ((im.width - g.width) // 2, (im.height - g.height) // 2), g)
            self.icons[key] = ImageTk.PhotoImage(im)
        return self.icons[key]

    def entry_menu(self, e):  # right-click in any text box
        w = e.widget
        w.focus_set()
        self.menu(e, [('\ue8c6', T('Potong'), lambda: w.event_generate('<<Cut>>')),
                      ('\ue8c8', T('Salin'), lambda: w.event_generate('<<Copy>>')),
                      ('\ue77f', T('Tempel'), lambda: w.event_generate('<<Paste>>')), None,
                      ('\ue8b3', T('Pilih semua'), lambda: w.select_range(0, 'end'))])

    def apply_theme(self, *_):
        theme = self.v['theme'].get()
        self.dark = theme == 'Gelap' or (theme == 'Sistem' and not light_system())
        sv_ttk.set_theme('dark' if self.dark else 'light')
        for name in tkfont.names():  # sv_ttk sizes fonts in pixels, which ignore Windows display scaling; points don't
            f = tkfont.nametofont(name)
            if name.startswith('SunValley') and f.cget('size') < 0:
                f.configure(size=max(8, int(-f.cget('size') * .75 + .5) - 1))  # -1 pt: compact
        s = ttk.Style()
        bg, fg, hover, line, accent = (('#1c1c1c', '#fafafa', '#2d2d2d', '#383838', '#57c8ff') if self.dark else
                                            ('#fafafa', '#1c1c1c', '#ebebeb', '#dcdcdc', '#005fb8'))  # sv_ttk palette
        field, field_hover = ('#262626', '#2f2f2f') if self.dark else ('#ffffff', '#f3f3f3')
        s.layout('TCombobox', [('Flat.field', {'sticky': 'nswe', 'children': [('Combobox.arrow', {'side': 'right', 'sticky': 'ns'}),
            ('Combobox.padding', {'sticky': 'nswe', 'children': [('Combobox.textarea', {'sticky': 'nswe'})]})]})])
        s.configure('TCombobox', bordercolor=line, lightcolor=field, darkcolor=field, padding=(self.px(8), self.px(2), 0, self.px(2)))
        s.map('TCombobox', fieldbackground=[('hover', field_hover), ('', field)],
              lightcolor=[('hover', field_hover), ('', field)], darkcolor=[('hover', field_hover), ('', field)])
        s.layout('TEntry', [('Flat.field', {'sticky': 'nswe', 'children': [('Entry.padding', {'sticky': 'nswe', 'children': [
            ('Entry.textarea', {'sticky': 'nswe'})]})]})])
        s.configure('TEntry', fieldbackground=field, bordercolor=line, lightcolor=field, darkcolor=field, padding=(self.px(8), self.px(4)))
        s.map('TEntry', bordercolor=[('focus', accent), ('', line)], lightcolor=[('focus', accent), ('', field)])
        s.configure('Head.TLabel', font='SunValleyBodyStrongFont')
        # Buttons + scrollbars: flat elements too. sv_ttk's stretched images cost 7-11 ms each on every repaint, which
        # made window resizing crawl (Unduhan page: ~270 ms per step); these draw in ~2 ms.
        btn, btn_hover, btn_down = ('#2d2d2d', '#383838', '#262626') if self.dark else ('#fbfbfb', '#f0f0f0', '#e6e6e6')
        acc_fg, acc_hover, acc_down = ('#000000', '#4cbcf2', '#43a9da') if self.dark else ('#ffffff', '#1a6dc0', '#0b56a0')
        for name in ('TButton', 'Accent.TButton'):
            s.layout(name, [('Flat.field', {'sticky': 'nswe', 'children': [('Button.padding', {'sticky': 'nswe', 'children': [
                ('Button.label', {'sticky': 'nswe'})]})]})])
        for name, base, edge, text, hov, down in (('TButton', btn, line, fg, btn_hover, btn_down),
                                                  ('Accent.TButton', accent, accent, acc_fg, acc_hover, acc_down)):
            s.configure(name, fieldbackground=base, bordercolor=edge, lightcolor=base, darkcolor=base, foreground=text,
                        padding=(self.px(12), self.px(4)), anchor='center')
            states = [('pressed', down), ('active', hov), ('', base)]
            s.map(name, fieldbackground=states, lightcolor=states, darkcolor=states, foreground=[('', text)])
        thumb, thumb_hover = ('#4d4d4d', '#6b6b6b') if self.dark else ('#c4c4c4', '#a6a6a6')
        s.layout('Vertical.TScrollbar', [('DD.trough', {'sticky': 'ns', 'children': [('DD.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        s.configure('Vertical.TScrollbar', troughcolor=bg, background=thumb, bordercolor=bg, lightcolor=thumb, darkcolor=thumb,
                    arrowsize=self.px(8), gripcount=0)
        s.map('Vertical.TScrollbar', background=[('active', thumb_hover)], lightcolor=[('active', thumb_hover)],
              darkcolor=[('active', thumb_hover)])
        self.sep.configure(bg=line)
        # sv_ttk's 9-slice images tile a tiny centre: a full-width nav button repainted in ~300 ms and a big table in
        # ~37 ms (every progress tick). Flat elements for those two keep hover and progress updates instant.
        s.layout('Treeview', [('Flat.field', {'sticky': 'nswe', 'children': [('Treeview.padding', {'sticky': 'nswe', 'children': [
            ('Treeview.treearea', {'sticky': 'nswe'})]})]})])
        s.layout('Treeview.Heading', [('Flat.border', {'sticky': 'nswe', 'children': [('Treeheading.padding', {'sticky': 'nswe', 'children': [
            ('Treeheading.image', {'side': 'right', 'sticky': ''}), ('Treeheading.text', {'sticky': 'we'})]})]})])
        s.layout('Treeview.Item', [('Treeitem.padding', {'sticky': 'nswe', 'children': [  # flat list: no expander gap
            ('Treeitem.image', {'side': 'left', 'sticky': ''}), ('Treeitem.text', {'sticky': 'nswe'})]})])
        s.configure('Treeview.Heading', background=hover, relief='flat', borderwidth=0, padding=(self.px(8), self.px(3)))
        s.map('Treeview.Heading', background=[('active', hover)])
        s.layout('Nav.Toolbutton', [('Flat.border', {'sticky': 'nswe', 'children': [('Toolbutton.padding', {'sticky': 'nswe', 'children': [
            ('Toolbutton.label', {'sticky': 'nswe'})]})]})])
        s.configure('Treeview', rowheight=self.px(THUMB[1] + 8), bordercolor=line, lightcolor=line, darkcolor=line)
        s.configure('Nav.Toolbutton', anchor='w', padding=(self.px(12), self.px(8)), font=('Segoe UI Variable Text', 10),
                    background=bg, borderwidth=0, relief='flat')
        s.map('Nav.Toolbutton', background=[('selected', hover), ('active', hover)], foreground=[('selected', accent)])
        s.configure('Sub.TLabel', foreground='#a0a0a0' if self.dark else '#5c5c5c')
        self.menu_colors = ('#2c2c2c', '#3a3a3a', '#454545', '#f2f2f2') if self.dark else ('#f9f9f9', '#e9e9e9', '#e0e0e0', '#1c1c1c')
        self.nav_imgs = []
        for b, ch in self.nav:
            normal, selected = (ImageTk.PhotoImage(glyph(ch, self.px(16), c)) for c in (fg, accent))
            b.configure(image=(normal, 'selected', selected))
            self.nav_imgs += [normal, selected]
        self.titlebar(self.root)

    def photo(self, path, w=THUMB[0], h=THUMB[1]):
        key = (path, w, h)
        if key not in self.imgs:
            try:
                im = Image.open(path)
                im.draft('RGB', (self.px(w) * 2, self.px(h) * 2))  # JPEG decodes straight at reduced size
                self.imgs[key] = ImageTk.PhotoImage(ImageOps.fit(im.convert('RGB'), (self.px(w), self.px(h))))
            except (OSError, AttributeError, ValueError):  # missing/corrupt thumbnail -> no image
                return ''
        return self.imgs[key]

    def show(self):
        self.root.deiconify()
        self.titlebar(self.root)  # a window started hidden (--tray) gets its dark title bar on first show
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()
        if not self.hinted:
            self.hinted = True
            self.notify(T('Tetap berjalan di tray. Salin link YouTube untuk mengunduh.'))

    def quit(self):
        active = [t for t in self.tasks.values() if not t.end and not t.pause]
        if active and not messagebox.askyesno(APP, T('{} unduhan masih berjalan. Batalkan dan keluar?', len(active))):
            return False
        for t in active:
            t.cancel = True
        self.icon.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
        return True

    def lang_changed(self, *_):  # the whole UI is built in one language: restart to switch
        self.save_settings()  # (may run before the save trace: the new process must read the new language)
        self.root.after_idle(lambda: messagebox.askyesno(  # after this variable's other traces: they touch widgets
            APP, 'Mulai ulang aplikasi sekarang untuk mengganti bahasa?\nRestart the app now to change the language?')
            and self.restart())

    def restart(self):
        if self.quit():
            self.lock.close()  # free the single-instance port for the new process
            # PyInstaller >= 6.9: without this the new exe counts as a sub-process of this one and shares its unpacked
            # _MEI folder, which this process deletes as it exits -> "Can't find a usable tk.tcl" in the new one
            subprocess.Popen([sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, os.path.abspath(__file__)],
                             env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'})

    # ---------- layout ----------
    def build(self):
        root = self.root
        side = ttk.Frame(root, padding=(self.px(10), self.px(14)))
        side.pack(side='left', fill='y')
        self.sep = tk.Frame(root, width=1)  # a plain 1 px line: ttk.Separator's image cost ~7 ms per repaint (resizing)
        self.sep.pack(side='left', fill='y')
        main = ttk.Frame(root)
        main.pack(side='left', fill='both', expand=True)
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)

        brand = ttk.Frame(side)
        brand.pack(anchor='w', padx=self.px(6), pady=(0, self.px(14)))
        ttk.Label(brand, image=self.logo).pack(side='left', padx=(0, self.px(8)))
        names = ttk.Frame(brand)  # one column: the credit starts exactly where "Downloader" does
        names.pack(side='left')
        ttk.Label(names, text='Donat\nDownloader', font=('Segoe UI Variable Display Semibold', 11)).pack(anchor='w')
        ttk.Label(names, text='By : Dani Abdani', style='Sub.TLabel', font=('Segoe UI', 8)).pack(anchor='w')
        self.page, self.pages, self.nav = tk.StringVar(value='Dashboard'), {}, []
        for name, icon in NAV:
            b = ttk.Radiobutton(side, text=f'   {T(name)}', value=name, variable=self.page, style='Nav.Toolbutton',
                                compound='left', takefocus=False, command=self.show_page)
            b.pack(fill='x', pady=1)
            self.nav.append((b, icon))
            self.pages[name] = ttk.Frame(main, padding=(self.px(16), self.px(12)))
        ttk.Checkbutton(side, text=T('Pantau clipboard'), variable=self.v['clip'], style='Switch.TCheckbutton').pack(side='bottom', anchor='w')

        # Dashboard = history
        p = self.pages['Dashboard']
        top = ttk.Frame(p)
        top.pack(fill='x')
        ttk.Label(top, text='Dashboard', font=H1).pack(side='left')
        self.search = tk.StringVar()
        self.search.trace_add('write', self.filter_history)
        ttk.Entry(top, textvariable=self.search, width=30).pack(side='right')
        ttk.Label(top, text=T('Cari')).pack(side='right', padx=self.px(8))
        self.stats = ttk.Label(p, style='Sub.TLabel')
        self.stats.pack(anchor='w', pady=(0, self.px(6)))
        self.hist = self.tree(p, 320, (('channel', 'Channel', 130), ('dur', 'Durasi', 64), ('fmt', 'Format', 104),
                                       ('size', 'Ukuran', 76), ('date', 'Tanggal', 118)))
        self.hist.configure(yscrollcommand=self.visible_thumbs)
        def context(e):
            row = self.hist.identify_row(e.y)
            if not row:
                return
            if row not in self.hist.selection():
                self.hist.selection_set(row)
            exists = any(os.path.exists(h['path']) for h in self.h_sel())
            self.menu(e, [('\ue8e5', T('Buka file'), self.h_open if exists else None),
                          ('\ue838', T('Tampilkan di folder'), self.h_reveal),
                          ('\ue8c8', T('Salin link'), self.h_copy),
                          ('\ue896', T('Unduh lagi (pilih format)…'), lambda: self.prefill(self.h_sel()[0]['url'])), None,
                          ('\ue711', T('Hapus dari riwayat'), self.h_delete),
                          ('\ue74d', T('Hapus file asli…'), self.h_delete_files if exists else None)])
        self.hist.bind('<Button-3>', context)
        self.hist.bind('<Double-1>', lambda e: self.h_open())
        self.hist.bind('<Delete>', lambda e: self.h_delete())
        self.refresh_history()

        # Unduhan = link box (+ its format panel) + queue
        p = self.pages['Unduhan']
        ttk.Label(p, text=T('Unduhan'), font=H1).pack(anchor='w')
        ttk.Label(p, text=T('Tempel link video/musik (YouTube, TikTok, Instagram, X, SoundCloud, …) — format langsung bisa diatur'),
                  style='Sub.TLabel').pack(anchor='w')
        self.url_row = row = ttk.Frame(p)
        row.pack(fill='x', pady=self.px(8))
        self.url = tk.StringVar()
        self.entry = ttk.Entry(row, textvariable=self.url, font=('Segoe UI', 10))
        self.entry.pack(side='left', fill='x', expand=True)
        self.entry.bind('<Return>', lambda e: self.submit())
        ttk.Button(row, text=T('Unduh'), style='Accent.TButton', width=10, command=self.submit).pack(side='left', padx=(self.px(8), 0))
        self.panel = ttk.Frame(p)  # shown while a link is in the box
        self.p_thumb = ttk.Label(self.panel)
        self.p_thumb.grid(row=0, column=0, sticky='nw', padx=(0, self.px(12)))
        head = ttk.Frame(self.panel)
        head.grid(row=0, column=1, sticky='nw')
        self.p_title = ttk.Label(head, style='Head.TLabel', wraplength=self.px(640))
        self.p_title.pack(anchor='w')
        self.p_sub = ttk.Label(head, style='Sub.TLabel')
        self.p_sub.pack(anchor='w')
        self.panel.columnconfigure(1, weight=1)  # spare width goes right of the title, not to the thumbnail column
        self.p_form = ttk.Frame(self.panel)
        self.p_form.grid(row=1, column=0, columnspan=2, sticky='w', pady=(self.px(6), 0))
        self.p_cut = ttk.Frame(self.panel)  # optional clip range (single videos only)
        self.p_cut.grid(row=2, column=0, columnspan=2, sticky='w', pady=(self.px(4), 0))
        self.cut_from, self.cut_to = tk.StringVar(), tk.StringVar()
        ttk.Label(self.p_cut, text=T('Potong (opsional)')).pack(side='left', padx=(0, self.px(10)))
        ttk.Label(self.p_cut, text=T('dari')).pack(side='left')
        ttk.Entry(self.p_cut, textvariable=self.cut_from, width=9).pack(side='left', padx=self.px(6))
        ttk.Label(self.p_cut, text=T('sampai')).pack(side='left')
        ttk.Entry(self.p_cut, textvariable=self.cut_to, width=9).pack(side='left', padx=self.px(6))
        ttk.Label(self.p_cut, text=T('contoh 1:30 — kosongkan untuk video penuh'), style='Sub.TLabel').pack(side='left')
        acts = ttk.Frame(p)
        acts.pack(side='bottom', fill='x', pady=(self.px(8), 0))
        ttk.Button(acts, text=T('Jeda'), command=self.pause_sel).pack(side='left')
        ttk.Button(acts, text=T('Lanjutkan'), command=self.resume_sel).pack(side='left', padx=(self.px(8), 0))
        ttk.Button(acts, text=T('Batalkan'), command=self.cancel_sel).pack(side='left', padx=(self.px(8), 0))
        ttk.Button(acts, text=T('Bersihkan yang selesai'), command=self.clear_done).pack(side='left', padx=self.px(8))
        ttk.Label(acts, text=T('atau seret link ke jendela ini'), style='Sub.TLabel').pack(side='left')
        ttk.Button(acts, text=T('Buka folder'), command=lambda: os.startfile(self.mkfolder())).pack(side='right')
        self.dl = self.tree(p, 300, (('status', 'Status', 140), ('prog', 'Progres', 70), ('speed', 'Kecepatan', 96),
                                     ('eta', 'Sisa', 64), ('size', 'Ukuran', 84)))
        self.dl.bind('<Double-1>', lambda e: self.open_task(self.dl.focus()))
        self.dl.bind('<Button-3>', self.dl_menu)
        self.dl.bind('<Delete>', lambda e: self.dl_remove())

        # Pengaturan
        p = self.pages['Pengaturan']
        ttk.Label(p, text=T('Pengaturan'), font=H1).pack(anchor='w')

        def section(title):  # plain heading + frame: sv_ttk's image Labelframe cost ~12 ms of paint each
            ttk.Label(p, text=T(title), style='Head.TLabel').pack(anchor='w', pady=(self.px(16), self.px(4)))
            f = ttk.Frame(p)
            f.pack(fill='x')
            return f
        f1 = section('Format default')
        self.form(f1, self.v, FORMAT_ROWS, cols=2, pady=5)
        f2 = section('Umum')
        self.form(f2, self.v, (('Koneksi paralel', 'conn', ('1', '2', '4', '8', '12', '16')),
                               ('Unduhan bersamaan*', 'parallel', ('1', '2', '3', '4', '6')),
                               ('Login YouTube', 'cookies', BROWSERS), ('Tema', 'theme', ('Sistem', 'Terang', 'Gelap')),
                               ('Batas kecepatan', 'rate', tuple(RATES)), ('Bahasa', 'lang', LANGS)), cols=2, pady=5)
        self.v['cookies'].trace_add('write', self.pick_cookies)
        ttk.Label(f2, text=T('Folder unduhan')).grid(row=3, column=0, sticky='w', pady=self.px(5))
        row = ttk.Frame(f2)
        row.grid(row=3, column=1, columnspan=3, sticky='w', pady=self.px(5))
        ttk.Entry(row, textvariable=self.v['folder'], width=48).pack(side='left')
        ttk.Button(row, text=T('Pilih…'), command=lambda: self.v['folder'].set(
            filedialog.askdirectory(initialdir=self.mkfolder()) or self.v['folder'].get())).pack(side='left', padx=self.px(8))
        ttk.Checkbutton(f2, text=T('Jalankan saat Windows mulai (langsung ke tray)'), variable=self.autostart).grid(
            row=4, column=0, columnspan=4, sticky='w', pady=self.px(5))
        ttk.Label(f2, text=T('* berlaku setelah aplikasi dibuka ulang'), style='Sub.TLabel').grid(row=5, column=0, columnspan=4, sticky='w')
        f3 = section('Pembaruan')
        ttk.Checkbutton(f3, text=T('Perbarui yt-dlp (mesin unduh YouTube) otomatis saat aplikasi dibuka'),
                        variable=self.v['autoupdate']).pack(anchor='w', pady=self.px(5))
        row = ttk.Frame(f3)
        row.pack(fill='x', pady=(self.px(6), 0))
        self.upd = ttk.Label(row, style='Sub.TLabel', text=T('Memeriksa komponen…'), wraplength=self.px(640))  # long errors wrap
        self.upd.pack(side='left')
        self.upd_btn = ttk.Button(row, text=T('Periksa sekarang'), width=22, command=self.update_button)
        self.upd_btn.pack(side='right')
        self.upd_bar = ttk.Progressbar(row, length=self.px(160), maximum=1.0)  # packed only while working
        for f in (f1, f2):  # same label columns -> both sections' fields line up
            f.columnconfigure(0, minsize=self.px(150))
            f.columnconfigure(2, minsize=self.px(185))
        self.show_page('Dashboard')
        try:  # drop a link (dragged from the browser) anywhere on the window: OLE hands drops over child
            from tkinterdnd2 import TkinterDnD, DND_TEXT  # widgets to their nearest registered parent
            TkinterDnD._require(root)
            for w in (side, main):  # (tk.Tk itself lacks tkinterdnd2's methods; these two frames cover the window)
                w.drop_target_register(DND_TEXT)
                w.dnd_bind('<<Drop>>', self.dropped)
        except Exception:  # optional nicety: the app works without it
            log('drag & drop unavailable\n' + traceback.format_exc())

    def dropped(self, e):
        m = URL.search(e.data or '')
        if m:
            self.prefill(m.group())
        return e.action

    def show_page(self, name=None):
        """Map only the visible page: sv_ttk draws alpha-blended images, so every mapped widget costs paint time."""
        self.page.set(name or self.page.get())
        for n, p in self.pages.items():
            if n == self.page.get():
                p.grid(row=0, column=0, sticky='nsew')
            else:
                p.grid_remove()

    def tree(self, parent, title_w, cols):
        f = ttk.Frame(parent)
        f.pack(fill='both', expand=True)
        t = ttk.Treeview(f, columns=[c[0] for c in cols])
        t.heading('#0', text=T('Judul'), anchor='w')
        # every column stretchable: Tk only lets a separator move if a stretchable neighbour can give/take the width
        t.column('#0', width=self.px(title_w), minwidth=self.px(120))
        for key, text, w in cols:
            t.heading(key, text=T(text), anchor='w')
            t.column(key, width=self.px(w), minwidth=self.px(40))
        t.sb = sb = ttk.Scrollbar(f, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        return t

    def form(self, parent, v, rows, cols=1, width=24, pady=2):
        """Label + readonly combobox pairs, `cols` per line, then the checkboxes. Only fields that apply are shown:
        Audio hides resolution/container/codec/subtitles, Video hides the audio ones, bitrate only for mp3.
        `rows` may be a list changed later (e.g. real resolutions); returns the redraw function."""
        made, traces = [], []

        def shown(key):
            audio = v['kind'].get() == 'Audio'
            return not (key in VIDEO_ONLY and audio or key in AUDIO_ONLY and not audio or key == 'abr' and v['afmt'].get() != 'mp3')

        def draw(*_):
            while made:
                made.pop().destroy()
            while traces:
                var, tid = traces.pop()
                var.trace_remove('write', tid)
            combos = [r for r in rows if r[2] and shown(r[1])]
            checks = [r for r in rows if not r[2] and shown(r[1])]
            for i, (label, key, values) in enumerate(combos):
                r, c = divmod(i, cols)
                made.append(ttk.Label(parent, text=T(label)))
                made[-1].grid(row=r, column=c * 2, sticky='w', pady=self.px(pady), padx=(self.px(20) if c else 0, self.px(10)))
                box = ttk.Combobox(parent, values=[T(x) for x in values], state='readonly', width=width)
                box.set(T(v[key].get()))  # shows the translation; the variable keeps the stored (Indonesian) value
                box.bind('<<ComboboxSelected>>', lambda e, b=box, k=key, vals=values: v[k].set(vals[b.current()]))
                traces.append((v[key], v[key].trace_add('write', lambda *_, b=box, k=key: b.winfo_exists() and b.set(T(v[k].get())))))
                made.append(box)
                box.grid(row=r, column=c * 2 + 1, sticky='w')
                box.event_generate('<<ThemeChanged>>')  # sv_ttk sets the combobox font only on theme change
            first = -(-len(combos) // cols)
            for i, (label, key, _) in enumerate(checks):
                r, c = divmod(i, cols)
                made.append(ttk.Checkbutton(parent, text=T(label), variable=v[key]))
                made[-1].grid(row=first + r, column=c * 2, columnspan=2, sticky='w', pady=self.px(pady))
        draw()
        if any(r[1] in VIDEO_ONLY | AUDIO_ONLY for r in rows):
            v['kind'].trace_add('write', draw)
            v['afmt'].trace_add('write', draw)
        return draw

    def pick_cookies(self, *_):
        if self.v['cookies'].get() == COOKIES_TXT:
            self.v['cookies'].set(filedialog.askopenfilename(title=T('Pilih cookies.txt (format Netscape)'),
                                                             filetypes=[('cookies.txt', '*.txt')]) or 'Otomatis')

    def mkfolder(self):
        path = folder(self.opts())
        os.makedirs(path, exist_ok=True)
        return path

    def update_button(self):  # one button: check / cancel the running check / restart to apply a downloaded update
        if self.upd_job:
            self.upd_job.cancel = True
            self.upd_idle(T('Pemeriksaan dibatalkan'))
        elif self.upd_ready:
            self.restart()
        else:
            self.check_update()

    def upd_idle(self, note=''):
        self.upd_job = None
        self.upd_bar.stop()
        self.upd_bar.pack_forget()
        self.upd_btn.configure(text=T('Mulai ulang untuk menerapkan' if self.upd_ready else 'Periksa sekarang'))
        self.upd.configure(text=self.upd_base + (f'  —  {note}' if note else '') if self.upd_base else note)

    def upd_step(self, job, stage, frac=None):  # UI thread; reports from a cancelled job are ignored
        if job is not self.upd_job:
            return
        if stage == 'download':
            self.upd_bar.stop()
            self.upd_bar.configure(mode='determinate', value=frac)
            self.upd.configure(text=T('Mengunduh pembaruan… {}', f'{frac:.0%}'))
        else:
            self.upd_bar.configure(mode='indeterminate')
            self.upd_bar.start(12)
            self.upd.configure(text=T('Memeriksa pembaruan…' if stage == 'check' else 'Menerapkan pembaruan…'))

    def check_update(self, startup=False):
        """yt-dlp update check (startup: only if auto-update is on; button: always), cancellable, with progress.
        Failures keep the working version. ffmpeg and QuickJS are inside the exe and change only with a new build."""
        check = not startup or self.cfg['autoupdate']
        job = SimpleNamespace(cancel=False)
        if check:
            self.upd_job = job
            self.upd_btn.configure(text=T('Batalkan'))
            self.upd_bar.pack(side='right', padx=self.px(10))
            self.upd_step(job, 'check')

        def work():
            msg, new = '', None
            if check:
                try:
                    new = update_ytdlp(lambda stage, frac=None: self.ui(self.upd_step, job, stage, frac), lambda: job.cancel)
                    msg = T('yt-dlp {} diunduh (aktif setelah aplikasi dibuka ulang)', new) if new else T('sudah versi terbaru')
                except UpdateCancelled:
                    return
                except Exception as e:  # offline / PyPI hiccup
                    msg = T('gagal memeriksa pembaruan: {}', errmsg(e)[:80])
            ff = ffmpeg_version(FFMPEG) if FFMPEG else T('tidak ada')
            updated = T(' (diperbarui)') if ytdl().__file__.startswith(UPDATES) else ''
            base = f"yt-dlp {ytdl().version.__version__}{updated}  ·  ffmpeg {ff}  ·  QuickJS  {T('(bawaan exe)')}"
            self.ui(self.upd_done, job, base, msg, bool(new), check)
            if new or (msg and not startup):
                self.notify(msg)
        threading.Thread(target=work, daemon=True).start()

    def upd_done(self, job, base, msg, new, checked):
        self.upd_base = base
        if checked and job is not self.upd_job:  # cancelled meanwhile
            return
        self.upd_ready = self.upd_ready or new
        self.upd_idle(msg)

    # ---------- clipboard -> popup ----------
    def poll_clip(self):
        self.root.after(300, self.poll_clip)
        seq = ctypes.windll.user32.GetClipboardSequenceNumber()
        if seq == self.clip_seq:  # cheap check; the text is read only when the clipboard actually changed
            return
        if not ctypes.windll.user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT: images/files aren't links
            self.clip_seq = seq
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:  # another app holds the clipboard open: retry next tick
            return
        if not SITES and URL.search(text) and not yt_url(text):  # site list still warming up: look again next tick
            return
        self.clip_seq = seq
        hit = len(text) < 2000 and self.v['clip'].get() and text != self.own_copy and site_of(text)
        if hit:
            self.popup(*hit)

    def card(self, title, sub, buttons, thumb=None):
        """Bottom-right notification card (link copied / download finished), with the Windows notification sound.
        buttons: (text, action) pairs, the first one accented. Returns (title, sub, thumb label, re-place)."""
        if self.pop and self.pop.winfo_exists():
            self.pop.destroy()
        self.pop = w = tk.Toplevel(self.root, bg=self.menu_colors[2])
        w.overrideredirect(True)
        w.attributes('-topmost', True)
        f = ttk.Frame(w, padding=self.px(14))  # flat frame + 1 px border: sv_ttk's image card cost ~25 ms to paint
        f.pack(fill='both', expand=True, padx=1, pady=1)
        top = ttk.Frame(f)
        top.pack(fill='x')
        ttk.Label(top, text='  Donat Downloader', image=self.logo, compound='left', style='Sub.TLabel').pack(side='left')
        ttk.Button(top, text='✕', width=3, command=w.destroy).pack(side='right')
        body = ttk.Frame(f)
        body.pack(fill='x', pady=self.px(10))
        pic = ttk.Label(body, image=self.photo(thumb, 128, 72) if thumb else '')
        pic.pack(side='left')
        info = ttk.Frame(body)
        info.pack(side='left', padx=self.px(12), anchor='n')
        head = ttk.Label(info, text=title if len(title) < 90 else title[:87] + '…', wraplength=self.px(230),
                         font=('Segoe UI Semibold', 10))
        head.pack(anchor='w')
        foot = ttk.Label(info, text=sub, style='Sub.TLabel', wraplength=self.px(230))
        foot.pack(anchor='w', pady=(self.px(4), 0))
        btns = ttk.Frame(f)
        btns.pack(fill='x')
        for i, (text, action) in enumerate(buttons):
            ttk.Button(btns, text=text, style='TButton' if i else 'Accent.TButton', command=lambda a=action: (w.destroy(), a())).pack(
                side='left', fill='x', expand=True, padx=(self.px(8) if i else 0, 0))

        def place():  # bottom-right corner of the work area, above the taskbar
            w.update_idletasks()
            r = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)
            w.geometry(f'{self.px(440)}x{w.winfo_reqheight()}+{r.right - self.px(452)}+{r.bottom - w.winfo_reqheight() - self.px(12)}')
        place()
        with contextlib.suppress(Exception):  # Windows 11 rounded corners
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(w.winfo_id()), 33,
                                                       ctypes.byref(ctypes.c_int(2)), 4)
        close = w.after(15000, w.destroy)

        def hover(inside):  # pointer on the popup keeps it open; leaving gives a few more seconds
            nonlocal close
            w.after_cancel(close)
            if not inside:
                close = w.after(5000, w.destroy)
        w.bind('<Enter>', lambda e: hover(True))
        w.bind('<Leave>', lambda e: hover(False))
        winsound.PlaySound('Notification.Default', winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return head, foot, pic, place

    def popup(self, url, site, is_list=False):  # a supported link was copied
        head, foot, pic, place = self.card(url, T('Playlist/channel {} terdeteksi' if is_list else 'Link {} terdeteksi', site),
                                           [(T('Lihat di Donat Downloader' if is_list else 'Download dengan Donat Downloader'),
                                             lambda: self.prefill(url))])

        def meta(vid):  # YouTube: thumbnail + title in ~0.2 s (other sites show the link until the panel loads it)
            path, o = fetch_thumb(vid, f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg'), oembed(vid)
            self.ui(lambda: pic.winfo_exists() and (pic.configure(image=self.photo(path, 128, 72)),
                    head.configure(text=o.get('title', url)), foot.configure(text=o.get('author_name', '')), place()))
        if m := YT.search(url):
            threading.Thread(target=meta, args=(m.group(1),), daemon=True).start()

    # ---------- link box + format panel ----------
    def prefill(self, url):  # popup "Opsi…" / history "Unduh lagi": open the link box with its format panel
        self.show()
        self.show_page('Unduhan')
        self.url.set(url)
        self.entry.focus_set()

    def url_changed(self, *_):  # debounce typing/pasting
        if self.url_job:
            self.root.after_cancel(self.url_job)
        self.url_job = self.root.after(350, self.load_url)

    def load_url(self):
        """A link in the box -> show the format panel at once, then fill in real resolutions/sizes when extracted."""
        text = self.url.get().strip()
        url = yt_url(text) or text
        if not re.match(r'https?://\S+$', url):
            self.loaded = None
            return self.panel.pack_forget()
        if url == self.loaded:
            return
        self.loaded, self.p_info = url, None
        self.pv = tkvars(self.opts())
        if 'music.youtube.com' in url:
            self.pv['kind'].set('Audio')
        for w in self.p_form.winfo_children():
            w.destroy()
        self.p_rows = list(FORMAT_ROWS)
        self.p_draw = self.form(self.p_form, self.pv, self.p_rows, cols=4, width=13)  # video: all 4 dropdowns on one row
        self.p_thumb.configure(image='')
        self.p_count = 0  # > 0 once the link turns out to be a playlist/channel
        self.cut_from.set('')
        self.cut_to.set('')
        self.p_cut.grid()
        self.p_title.configure(text=T('Memuat info & format…'))
        self.p_sub.configure(text=url)
        self.panel.pack(fill='x', pady=(0, self.px(8)), after=self.url_row)
        m = YT.search(url)
        if m:  # title + thumbnail in ~0.2 s via oEmbed, while the full format list (seconds) loads
            def meta(vid=m.group(1)):
                o, path = oembed(vid), fetch_thumb(vid, f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg')
                self.ui(lambda: self.loaded == url and not self.p_thumb.cget('image') and (
                    self.p_thumb.configure(image=self.photo(path, 128, 72)), self.p_title.configure(text=o.get('title', url)),
                    self.p_sub.configure(text=f"{o.get('author_name', '')}  ·  {T('memuat daftar resolusi…')}")))
            threading.Thread(target=meta, daemon=True).start()

        opts = {**ydl_opts(self.opts()), 'extract_flat': 'in_playlist'}  # Tk variables: read here, on the UI thread

        def work():
            try:
                with ytdl().YoutubeDL(opts) as y:
                    info = y.extract_info(url, download=False)
                    jar = list(y.cookiejar)  # its stream links only work with this session's cookies
                self.ui(self.fill_panel, url, info, fetch_thumb(info['id'], thumb_url(info)), jar)
            except Exception as e:  # thread boundary: show the failure instead of dying silently
                msg = errmsg(e)
                self.ui(lambda: self.loaded == url and self.p_title.configure(text=T('Gagal memuat: {}', msg[:200])))
        threading.Thread(target=work, daemon=True).start()

    def fill_panel(self, url, info, path, jar=()):
        if url != self.loaded:  # the box changed meanwhile
            return
        self.p_info = (info, jar, time.monotonic())  # Unduh reuses it: the download starts without re-extracting (~5 s)
        self.p_thumb.configure(image=self.photo(path, 128, 72))
        self.p_title.configure(text=info.get('title') or url)
        if info.get('entries') is not None:
            self.p_count = len(list(info['entries'])) or 1
            self.p_sub.configure(text=T('Playlist/channel · {} item — Unduh akan meminta konfirmasi', self.p_count))
            self.p_cut.grid_remove()
        else:
            self.p_rows[:] = [(l, k, ('Terbaik', *res_labels(info)) if k == 'res' else vals) for l, k, vals in self.p_rows]
            self.p_draw()
            self.p_sub.configure(text=f"{info.get('uploader') or ''}  ·  {clock(info.get('duration') or 0)}")

    # ---------- downloads ----------
    def submit(self):  # link box: Enter / Unduh -> uses the format panel's choices
        text = self.url.get().strip()
        url = yt_url(text) or text
        if not (url and url == self.loaded):  # Enter before the panel loaded
            hit = site_of(url)
            if hit and hit[2]:  # a playlist/channel never skips the panel's confirmation
                return self.load_url()
            self.quick(url)
            return self.url.set('')
        o = {k: x.get() for k, x in self.pv.items()}
        if self.p_count:
            if not messagebox.askyesno(APP, T('Unduh semua {} item dari playlist/channel ini?\n'
                                              'File disimpan di subfolder bernama playlist di:\n{}', self.p_count, folder(o))):
                return
            o['playlist'] = True
        else:
            try:
                start, end = parse_time(self.cut_from.get()), parse_time(self.cut_to.get())
            except ValueError as e:
                return messagebox.showwarning(APP, T('Waktu potong tidak dikenali: {}\nContoh: 90, 1:30, atau 1:02:03', e))
            if start is not None or end is not None:
                start = start or 0
                if end is not None and end <= start:
                    return messagebox.showwarning(APP, T('Waktu "sampai" harus setelah waktu "dari".'))
                o['cut'] = (start, end)
        p = self.p_info
        self.enqueue(url, o, p[:2] if p and not self.p_count and time.monotonic() - p[2] < 1800 else None)
        self.url.set('')

    def quick(self, url):
        o = self.opts()
        if 'music.youtube.com' in url:
            o['kind'] = 'Audio'
        self.enqueue(url.strip(), o)

    def enqueue(self, url, o, info=None):
        if not url:
            return
        t = SimpleNamespace(url=url, o=o, cancel=False, end=False, last=0, stem=None, path=None, err=None, base=None, frac=0,
                            started=0, pause=False, running=False, info=info, parts={}, got={}, peak=0)
        t.iid = self.dl.insert('', 0, text=f'  {url}', values=cells(T('Menunggu'), '', '', '', ''))
        self.tasks[t.iid] = t
        m = YT.search(url)
        if m:
            def work():
                self.ui(self.set_thumb, t, fetch_thumb(m.group(1), f'https://i.ytimg.com/vi/{m.group(1)}/mqdefault.jpg'))
            threading.Thread(target=work, daemon=True).start()
        self.pool.submit(self.run, t)
        if self.root.state() == 'withdrawn':
            self.notify(T('Mulai mengunduh…'))
        else:
            self.show_page('Unduhan')

    def run(self, t):  # worker thread
        if t.pause and not t.cancel:  # paused while still queued
            return self.ui(self.row, t, status=T('Dijeda'))
        t.running = True
        try:
            if t.cancel:
                raise ytdl().utils.DownloadCancelled()
            self.ui(self.row, t, status=T('Menyiapkan…'))
            t.started = t.started or time.time()  # a resumed download keeps its start (cleanup age check)
            info = self.fetch(t)
            # a playlist/channel URL yields several videos; anything without a download (skipped) isn't history
            done = [e for e in (info.get('entries') or [info]) if e and e.get('requested_downloads')]
            entries = [history_entry(t, e) for e in done]
            t.end, t.path = True, entries[0]['path'] if entries else None
            return self.ui(self.finish, t, entries)
        except Exception as e:  # thread boundary: every failure lands on the row
            err = errmsg(e)
        finally:
            t.running = False
        if t.pause and not t.cancel:  # .part/.ytdl files stay: Lanjutkan resumes from there
            gc.collect()  # drop the traceback so yt-dlp's file handles close
            return self.ui(self.row, t, status=T('Dijeda'), speed='', eta='')
        t.end = True
        if t.cancel:
            gc.collect()  # outside `except`: frees the traceback so yt-dlp's open .part handle closes before cleanup
            if t.stem:
                remove_leftovers(t.stem, t.started)
            self.ui(self.row, t, status=T('Dibatalkan'), prog='', speed='', eta='')
        else:
            t.err = err
            log(f'{t.url}\n{err}')
            self.ui(self.row, t, status=T('Gagal (klik 2x)'), prog='', speed='', eta='')
            self.notify(T('Gagal: {}', err[:120]))

    def fetch(self, t):
        """Download, recovering once from each of:
        - HTTP 403 (YouTube invalidating stream URLs under load): re-extract and resume;
        - YouTube's bot check in Otomatis mode: retry with the user's YouTube login taken from their browser.
        Cookies are only used when YouTube demands them, so the account isn't exposed to every download."""
        o, retried, hint = t.o, set(), None
        while True:
            try:
                with ytdl().YoutubeDL(ydl_opts(o, lambda d: self.hook(t, d), t.url)) as y:
                    track_parts(y, t)
                    if o.get('cut'):
                        add_cut(y, o, lambda: self.ui(self.row, t, status=T('Memotong…')))
                    if t.info:  # the format panel's extraction: starts at once instead of extracting again
                        info, jar = t.info
                        for c in jar:
                            y.cookiejar.set_cookie(c)
                        return y.process_ie_result(dict(info), download=True)
                    return y.extract_info(t.url, download=True)
            except ytdl().utils.DownloadError as e:
                msg = str(e)
                if t.cancel or t.pause:
                    raise
                if t.info:  # its stream links may have expired: extract fresh (finished parts still resume)
                    t.info = None
                elif 'HTTP Error 403' in msg and '403' not in retried:
                    retried.add('403')
                elif 'not a bot' in msg and o['cookies'] in AUTO and 'bot' not in retried:
                    retried.add('bot')
                    self.ui(self.row, t, status=T('Memakai login YouTube…'))
                    path, locked = grab_cookies()
                    if locked and not path:  # a running Chromium browser keeps its cookies locked
                        hint = T('YouTube meminta login (anti-bot). Tutup {} sebentar supaya login YouTube-nya '
                                 'bisa dipakai, lalu klik 2x baris ini → Coba lagi.', ' / '.join(locked))
                    path = path or (APP_COOKIES if os.path.exists(APP_COOKIES) else None)  # last captured login
                    if not path:
                        raise RuntimeError(hint or T('YouTube meminta login (anti-bot). Login YouTube di browser Anda '
                                                     '(Firefox/Edge/Chrome/Helium), lalu klik 2x baris ini → Coba lagi.')) from None
                    o = dict(o, cookies=path)
                elif 'not a bot' in msg and hint:
                    raise RuntimeError(hint) from None
                else:
                    raise
            gc.collect()  # release the failed attempt's .part handles so the retry can resume/rename them
            self.ui(self.row, t, status=T('Mencoba ulang…'))

    def hook(self, t, d):  # yt-dlp progress, worker thread
        if t.cancel or t.pause:
            raise ytdl().utils.DownloadCancelled()
        t.stem = re.sub(r'(\.f[\w-]+)?\.\w+$', '', d['filename'])  # 'Judul [id].f137.mp4' -> 'Judul [id]'
        now = time.monotonic()
        if d['status'] == 'downloading' and now - t.last < .25:
            return
        t.last = now
        fid, fin = d['info_dict'].get('format_id'), d['status'] == 'finished'
        cur = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        if fid in t.parts:  # whole video: finished parts' real sizes + this one's + estimates of those still to come
            got = (d.get('total_bytes') if fin else 0) or d.get('downloaded_bytes') or 0  # "already there": no downloaded_bytes
            size, exact = t.parts[fid]
            t.got[fid] = (got, got if fin else size if exact else cur or size)
            done = sum(g[0] for g in t.got.values())
            total = sum(t.got.get(f, (0, s))[1] for f, (s, _) in t.parts.items())
        else:  # subtitles etc.
            done, total = d.get('downloaded_bytes') or 0, cur
        busy = d['status'] == 'downloading' or done < total  # a finished video part with the audio still to come
        frac = t.peak = max(done / total if total and busy else 1, t.peak)  # estimates wobble; the bar never goes back
        speed = d.get('speed') if d['status'] == 'downloading' else None
        part = 'audio' if d['info_dict'].get('vcodec') == 'none' else 'video'
        i, n = d['info_dict'].get('playlist_index'), d['info_dict'].get('n_entries')
        part += f' {i}/{n}' if i and n else ''
        self.ui(self.row, t, title=d['info_dict'].get('title'), status=T('Mengunduh {}', part) if busy else T('Memproses…'),
                frac=frac, speed=f'{human(speed)}/s' if speed else '',
                eta=clock((total - done) / speed) if speed and total > done else '', size=human(total))

    def set_thumb(self, t, path):
        try:
            t.base = ImageOps.fit(Image.open(path).convert('RGBA'), (self.px(THUMB[0]), self.px(THUMB[1])))
        except (OSError, AttributeError, ValueError):
            return
        self.row(t, frac=t.frac)

    def row(self, t, title=None, frac=None, **vals):
        if not self.dl.exists(t.iid):
            return
        if title:
            self.dl.item(t.iid, text=f'  {title}')
        if frac is not None:
            t.frac = frac = min(frac, 1)  # size estimates can undershoot
            vals['prog'] = f'{frac:.0%}'
            if t.base:  # translucent green fill over the lower half of the thumbnail
                ov = Image.new('RGBA', t.base.size)
                ImageDraw.Draw(ov).rectangle((0, ov.height // 2, round(ov.width * frac), ov.height), fill=(34, 197, 94, 150))
                t.photo = ImageTk.PhotoImage(Image.alpha_composite(t.base, ov))
                self.dl.item(t.iid, image=t.photo)
        for k, val in vals.items():
            self.dl.set(t.iid, k, cells(val)[0])
        self.tray_tip()

    def tray_tip(self):  # tray icon hover text: overall progress of running downloads
        active = [t for t in self.tasks.values() if not t.end and not t.pause]
        tip = T('{} · {} unduhan · {}', APP, len(active), f'{sum(t.frac for t in active) / len(active):.0%}') if active else APP
        if tip != self.icon.title:
            self.icon.title = tip

    def finish(self, t, entries):
        for e in entries:
            self.history.append(e)
            self.add_row(len(self.history) - 1)
        save('history.json', self.history)
        self.filter_history()
        self.row(t, status=T('Selesai ✓'), frac=1, speed='', eta='', size=human(sum(e['size'] for e in entries)))
        if not entries:  # everything was skipped (e.g. already downloaded)
            return self.notify(T('Selesai: tidak ada file baru'))
        e = entries[0]
        sub = T('Unduhan selesai  ·  {}  ·  {}', e['fmt'], human(e['size'])) + (T('  ·  +{} lagi', len(entries) - 1) if len(entries) > 1 else '')
        self.card(e['title'], sub, [(T('Buka folder'), lambda: reveal(e['path'])), (T('Buka file'), lambda: os.startfile(e['path']))],
                  thumb=e['thumb'])

    def cancel_sel(self):
        for iid in self.dl.selection():
            t = self.tasks[iid]
            if t.end:
                continue
            t.cancel = True
            if t.pause and not t.running:  # nothing running to notice the flag: clean up the kept partial files here
                t.end = True
                if t.stem:
                    threading.Thread(target=remove_leftovers, args=(t.stem, t.started), daemon=True).start()
                self.row(t, status=T('Dibatalkan'), prog='')
            else:
                self.row(t, status=T('Membatalkan…'))

    def pause_sel(self):
        for iid in self.dl.selection():
            t = self.tasks[iid]
            if not t.end and not t.pause:
                t.pause = True
                self.row(t, status=T('Menjeda…' if t.running else 'Dijeda'), speed='', eta='')

    def resume_sel(self):
        for iid in self.dl.selection():
            t = self.tasks[iid]
            if t.pause and not t.end and not t.cancel:
                t.pause = False
                if not t.running:  # (still running = the pause hadn't landed yet: it just carries on)
                    self.row(t, status=T('Menunggu'))
                    self.pool.submit(self.run, t)

    def clear_done(self):
        for iid, t in list(self.tasks.items()):
            if t.end:
                self.dl.delete(iid)
                del self.tasks[iid]

    def open_task(self, iid):
        t = self.tasks.get(iid)
        if t and t.path and os.path.exists(t.path):
            os.startfile(t.path)
        elif t and t.err and messagebox.askretrycancel(APP, t.err, icon='error'):
            self.retry(t)

    def retry(self, t):
        self.dl.delete(t.iid)
        del self.tasks[t.iid]
        self.enqueue(t.url, t.o)

    def dl_menu(self, e):
        row = self.dl.identify_row(e.y)
        if row and row not in self.dl.selection():
            self.dl.selection_set(row)
        ts = [self.tasks[i] for i in self.dl.selection() if i in self.tasks]
        files = [t.path for t in ts if t.path and os.path.exists(t.path)]
        running, ended = [t for t in ts if not t.end], [t for t in ts if t.end]
        failed = [t for t in ended if not t.path]
        paused = [t for t in running if t.pause]
        self.menu(e, [('\ue8e5', T('Buka file'), (lambda: [os.startfile(f) for f in files]) if files else None),
                      ('\ue838', T('Tampilkan di folder'), lambda: reveal(files[0])) if files else
                      ('\ue838', T('Buka folder unduhan'), lambda: os.startfile(self.mkfolder())),
                      ('\ue8c8', T('Salin link'), (lambda: self.copy_text('\n'.join(t.url for t in ts))) if ts else None), None,
                      ('\ue769', T('Jeda'), self.pause_sel if len(paused) < len(running) else None),
                      ('\ue768', T('Lanjutkan'), self.resume_sel if paused else None),
                      ('\ue711', T('Batalkan'), self.cancel_sel if running else None),
                      ('\ue72c', T('Coba lagi'), (lambda: [self.retry(t) for t in failed]) if failed else None),
                      ('\ue783', T('Lihat error'), (lambda: messagebox.showerror(APP, failed[0].err)) if failed and failed[0].err else None),
                      None,
                      ('\ue74d', T('Hapus dari daftar'), self.dl_remove if ended else None),
                      ('\ue74d', T('Hapus file asli…'), (lambda: self.delete_files(files)) if files and not running else None)])

    def dl_remove(self):  # finished/failed rows only; a running download is cancelled first
        for iid in self.dl.selection():
            if iid in self.tasks and self.tasks[iid].end:
                self.dl.delete(iid)
                del self.tasks[iid]

    def delete_files(self, paths):
        """Recycle-Bin the files after confirmation; drop their history entries and finished rows. True if done."""
        names = '\n'.join(os.path.basename(p) for p in paths[:5]) + (T('\n… dan {} lainnya', len(paths) - 5) if len(paths) > 5 else '')
        if not messagebox.askyesno(APP, T('Pindahkan {} file ke Recycle Bin?\n\n{}', len(paths), names), icon='warning'):
            return False
        gone = {p for p in paths if recycle(p)}
        if len(gone) < len(paths):
            messagebox.showwarning(APP, T('Sebagian file tidak bisa dihapus (sedang dipakai aplikasi lain?).'))
        if gone:
            self.history = [h for h in self.history if h['path'] not in gone]
            save('history.json', self.history)
            self.refresh_history()
            for iid, t in list(self.tasks.items()):
                if t.path in gone:
                    self.dl.delete(iid)
                    del self.tasks[iid]
        return True

    def copy_text(self, text):
        self.own_copy = text  # our own copy shouldn't trigger the popup
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ---------- history (row iid = index into self.history) ----------
    def refresh_history(self):
        """Rebuild every row (startup, delete). Thumbnails are decoded only for rows on screen (see visible_thumbs)."""
        self.hist.delete(*map(str, range(self.hist_rows)))  # includes rows the search filter detached
        self.hist_rows = 0
        for i in range(len(self.history)):
            self.add_row(i)
        self.filter_history()

    def add_row(self, i):
        h = self.history[i]
        self.hist.insert('', 0, iid=str(i), text=f"  {h['title']}", image=self.imgs.get((h['thumb'], *THUMB), ''),
                         values=cells(h['channel'], clock(h['duration']), h['fmt'], human(h['size']), short_date(h['date'])))
        self.hist_rows += 1

    def visible_thumbs(self, first, last):  # yscrollcommand: the view moved/changed -> fill in on-screen thumbnails
        self.hist.sb.set(first, last)
        rows = self.hist.get_children()
        for iid in rows[int(float(first) * len(rows)):int(float(last) * len(rows)) + 2]:
            if not self.hist.item(iid, 'image') and self.history[int(iid)]['thumb']:
                self.hist.item(iid, image=self.photo(self.history[int(iid)]['thumb']))

    def filter_history(self, *_):  # search: re-link matching rows (no rebuild)
        q = self.search.get().lower()
        self.hist.set_children('', *[str(i) for i in range(len(self.history) - 1, -1, -1)
                                     if q in f"{self.history[i]['title']} {self.history[i]['channel']}".lower()])
        self.stats.configure(text=T('{} unduhan  ·  {} total  ·  klik 2x untuk memutar, klik kanan untuk opsi',
                                    len(self.history), human(sum(h['size'] for h in self.history))))

    def h_sel(self): return [self.history[int(i)] for i in self.hist.selection()]

    def h_open(self):
        for h in self.h_sel():
            if os.path.exists(h['path']):
                os.startfile(h['path'])
            else:
                messagebox.showwarning(APP, T('File sudah dipindah atau dihapus:\n{}', h['path']))

    def h_reveal(self):
        reveal(self.h_sel()[0]['path'])

    def h_copy(self):
        self.copy_text('\n'.join(h['url'] for h in self.h_sel()))

    def h_delete_files(self):
        self.delete_files([h['path'] for h in self.h_sel() if os.path.exists(h['path'])])

    def h_delete(self):  # removes the entry only; the file stays on disk
        for i in sorted(map(int, self.hist.selection()), reverse=True):
            del self.history[i]
        save('history.json', self.history)
        self.refresh_history()


def selftest():
    global LANG
    for k, en in EN.items():  # every translation keeps the same {} slots as its key
        assert k.count('{}') == en.count('{}'), k
    assert T('Unduhan') == 'Downloads' and T('Gagal: {}', 'x') == 'Failed: x' and T('zzz') == 'zzz'
    LANG = 'Indonesia'
    assert T('Unduhan') == 'Unduhan' and T('Gagal: {}', 'x') == 'Gagal: x' and cut_tag((5, None)) == 'potong 00.05-akhir'
    LANG = 'English'
    assert YT.search('https://youtu.be/dQw4w9WgXcQ?si=x').group(1) == 'dQw4w9WgXcQ'
    assert YT.search('https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD').group(1) == 'dQw4w9WgXcQ'
    assert YT.search('https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ').group(1) == 'dQw4w9WgXcQ'
    assert YT.search('https://youtube.com/shorts/abcdefghijk').group(1) == 'abcdefghijk'
    assert not YT.search('https://example.com/watch?v=dQw4w9WgXcQ')
    assert yt_url('cek ini https://youtu.be/dQw4w9WgXcQ?si=abc keren') == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
    assert yt_url('https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD') == 'https://music.youtube.com/watch?v=dQw4w9WgXcQ'
    assert not yt_url('halo dunia')
    a = ydl_opts(dict(DEFAULTS, kind='Audio', afmt='wav'))
    assert a['format'] == 'ba/b' and 'writethumbnail' not in a and a['postprocessors'][0]['preferredcodec'] == 'wav'
    v = ydl_opts(dict(DEFAULTS, res='1080p60 · ~200.0 MB', codec='H.264 (paling kompatibel)'))
    assert v['format_sort'] == ['vcodec:h264', 'res:1080', 'ext:mp4:m4a'] and v['merge_output_format'] == 'mp4'
    assert ydl_opts(DEFAULTS)['format_sort'] == ['ext:mp4:m4a']
    assert ydl_opts(dict(DEFAULTS, folder=''))['outtmpl'].startswith(DEFAULTS['folder'])
    assert not {'cookiesfrombrowser', 'cookiefile'} & (set(ydl_opts(DEFAULTS)) | set(ydl_opts(dict(DEFAULTS, cookies='Tidak'))))
    assert ydl_opts(dict(DEFAULTS, cookies='firefox'))['cookiesfrombrowser'] == ('firefox',)
    assert ydl_opts(dict(DEFAULTS, cookies='helium'))['cookiesfrombrowser'][1].endswith(r'Helium\User Data\Default')
    assert ydl_opts(dict(DEFAULTS, cookies=r'C:\x\cookies.txt'))['cookiefile'] == r'C:\x\cookies.txt'
    assert errmsg(Exception('ERROR: [youtube] x: Sign in to confirm you’re not a bot.')).startswith('YouTube asks for a login')
    assert res_labels({'formats': [{'height': 1080, 'vcodec': 'avc1', 'fps': 60, 'filesize': 100 << 20},
                                   {'height': 720, 'vcodec': 'vp9', 'fps': 30},
                                   {'vcodec': 'none', 'acodec': 'opus', 'filesize': 5 << 20}]}) == ['1080p60 · ~105.0 MB', '720p']
    yt = {'extractor': 'youtube', 'duration': 100, 'formats': [  # sorted worst -> best, as yt-dlp hands them
        {'vcodec': 'none', 'acodec': 'opus', 'filesize': 1 << 20}, {'vcodec': 'none', 'acodec': 'mp4a', 'filesize': 2 << 20},
        {'height': 1080, 'vcodec': 'vp9', 'filesize': 300 << 20, 'protocol': 'https'},
        {'height': 1080, 'vcodec': 'av01', 'filesize': 50 << 20, 'protocol': 'https'},
        {'height': 1080, 'vcodec': 'vp09', 'tbr': 4000, 'protocol': 'm3u8_native'}]}
    assert res_labels(yt) == ['1080p · ~52.0 MB'], res_labels(yt)  # the AV1 that gets picked, not the 300 MB / HLS
    assert all(map(is_leftover, ('.f137.mp4', '.mp4.part', '.f251-drc.webm.part-Frag3', '.mp4.ytdl', '.webp', '.en.vtt')))
    assert not any(map(is_leftover, ('.mp4', '.flac', '.mp3', '.f4v', '.mkv', '.m4a')))
    assert short_date('13 Oct 2026  04:11') == '2026/10/13 04:11' and short_date('2026/10/13 04:11') == '2026/10/13 04:11'
    assert vtuple('2026.10.2') > vtuple('2026.9.30') > vtuple('2026.08.19.232813')
    assert autostart_cmd().endswith('--tray')
    assert site_of('https://youtu.be/dQw4w9WgXcQ?si=x') == ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'youtube', False)
    warm_sites()
    assert site_of('lihat https://www.tiktok.com/@u/video/7300000000000000000 ya')[1:] == ('TikTok', False)
    assert site_of('https://vimeo.com/76979871')[1:] == ('vimeo', False)
    for lst in ('https://www.youtube.com/playlist?list=PL1234567890', 'https://www.youtube.com/@channel',
                'https://soundcloud.com/artist/sets/album', 'https://www.tiktok.com/@scout2015'):
        assert site_of(lst)[2] is True, lst
    for no in ('https://www.google.com/search?q=x', 'https://github.com/yt-dlp', 'https://www.youtube.com/', 'teks biasa'):
        assert not site_of(no), no
    assert parse_time('90') == 90 and parse_time('1:30') == 90 and parse_time('1:02:03') == 3723 and parse_time(' ') is None
    for bad in ('1:3:4:5', 'abc', '-5', '1:99x'):
        try:
            parse_time(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert cut_tag((90, 150)) == 'clip 01.30-02.30' and cut_tag((5, None)) == 'clip 00.05-end'
    c = ydl_opts(dict(DEFAULTS, cut=(90, 150)))
    assert c['outtmpl'].endswith('%(title)s [clip 01.30-02.30].%(ext)s') and c['overwrites'] is True
    pl = ydl_opts(dict(DEFAULTS, playlist=True))
    assert '%(playlist_title|playlist)s' in pl['outtmpl']['default'] and pl['ignoreerrors'] is True
    assert ydl_opts(DEFAULTS)['overwrites'] is True and pl['overwrites'] is False
    assert ydl_opts(DEFAULTS, url='https://youtu.be/x')['format'].startswith('bv*[protocol!*=m3u8]+ba/')
    assert ydl_opts(DEFAULTS, url='https://x.com/a')['format'] == 'bv*+ba/b'
    assert socket.getaddrinfo('localhost', 80)[0][0] == socket.AF_INET  # IPv4 first
    ytdl()
    from yt_dlp.extractor.youtube.jsc._builtin import ejs
    assert ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE, 'QuickJS player cache not enabled'
    from yt_dlp.utils import update_url_query as uq
    from yt_dlp.extractor.youtube import _video
    assert _video.update_url_query is not uq, 'caption URL speed-up not applied'
    u = 'https://www.youtube.com/api/timedtext?v=x&ei=a%2Fb&caps=asr&xoaf=5&hl=en&sparams=ip,ipbits&lang=en&kind=asr&xosf=1&e=&kind=b'
    for q in ({'fmt': 'vtt', 'xosf': []}, {'fmt': 'srv3', 'xosf': [], 'tlang': 'id', 'pot': 'a b/+'}, {'lang': ['x', 'y'], 'n': 5}):
        assert _video.update_url_query(u, q) == uq(u, q), q
    # one size and one 0-100% across video + audio parts
    t, rows = SimpleNamespace(cancel=False, pause=False, last=0, parts={'v': (300, False), 'a': (100, True)}, got={}, peak=0), []
    me = SimpleNamespace(row=None, ui=lambda fn, t, **k: rows.append(k))

    def step(fid, status, **d):
        t.last = 0
        App.hook(me, t, {'status': status, 'filename': 'x.f1.mp4', 'info_dict': {'format_id': fid}, **d})
        return rows[-1]['frac'], rows[-1]['size'], rows[-1]['eta']
    assert step('v', 'downloading', downloaded_bytes=150, total_bytes=300, speed=50) == (150 / 400, human(400), '00:05')
    assert step('v', 'finished', downloaded_bytes=320, total_bytes=320)[:2] == (320 / 420, human(420))
    assert step('a', 'downloading', downloaded_bytes=50, total_bytes_estimate=150, speed=10)[:2] == (370 / 420, human(420))
    assert step('a', 'finished', total_bytes=100) == (1, human(420), '') and rows[-1]['status'] == T('Memproses…')
    r = ydl_opts(dict(DEFAULTS, rate='2 MB/s'))
    assert r['ratelimit'] == 2 << 20 and r['concurrent_fragment_downloads'] == 1 and 'ratelimit' not in ydl_opts(DEFAULTS)
    assert QJS, 'qjs.exe missing (exe: --add-binary, dev: run build.ps1 once)'
    assert FFMPEG and ffmpeg_version(FFMPEG), 'ffmpeg missing/broken (exe: --add-binary, dev: build-ffmpeg.sh)'
    if getattr(sys, 'frozen', False):  # the exe must use what it carries, not something on this PC's PATH
        assert TOOLS and FFMPEG.startswith(TOOLS) and QJS.startswith(TOOLS), (TOOLS, FFMPEG, QJS)
    with ytdl().YoutubeDL({'logger': QUIET}) as y:  # curl_cffi present: TikTok/Dailymotion-style sites work
        assert y._get_available_impersonate_targets(), 'no impersonation targets (curl_cffi missing)'
    print('selftest ok')


def main():
    os.makedirs(THUMBS, exist_ok=True)
    lock = socket.socket()
    try:
        lock.bind(('127.0.0.1', PORT))
    except OSError:  # already running: poke that instance so it shows its window
        socket.create_connection(('127.0.0.1', PORT), timeout=2).close()
        return
    lock.listen()
    app = App(lock)
    with contextlib.suppress(ImportError):  # single-file exe: drop the "unpacking" splash once the window is up
        import pyi_splash
        pyi_splash.close()
    app.root.mainloop()


if __name__ == '__main__':
    selftest() if '--test' in sys.argv else main()
