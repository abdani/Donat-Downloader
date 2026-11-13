# Builds "dist\Donat Downloader.exe": ONE standalone file, copy it anywhere - nothing else to install or download.
# Inside: Python, yt-dlp (+curl_cffi for sites that need browser impersonation, e.g. TikTok/Dailymotion),
# QuickJS (JS runtime for YouTube, ~1 MB) and a minimal static ffmpeg/ffprobe (build-ffmpeg.sh, ~16 MB raw).
# yt-dlp updates itself at runtime; ffmpeg/QuickJS change only with a new build.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
# Keep this file pure ASCII: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI and mangles other characters.
$ErrorActionPreference = 'Stop'
if ([IO.File]::ReadAllText($PSCommandPath) -match '[^\x00-\x7F]') { throw 'build.ps1 must stay pure ASCII (see note above)' }
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
$py = "$PWD\.venv\Scripts\python.exe"
& $py -m pip install -q -r requirements.txt pyinstaller
New-Item -ItemType Directory -Force build | Out-Null

# QuickJS-ng, pinned + sha256-checked (bump both together)
$qjs = "$PWD\build\qjs.exe"
$qjsSha = '2AEABF0092C3262D6B2609824418F7DD7ED1F1DF939F73B2B15645230CAC0D77'
if (-not (Test-Path $qjs) -or (Get-FileHash $qjs -Algorithm SHA256).Hash -ne $qjsSha) {
    Invoke-WebRequest 'https://github.com/quickjs-ng/quickjs/releases/download/v0.17.0/qjs-windows-x86_64.exe' -OutFile $qjs
    if ((Get-FileHash $qjs -Algorithm SHA256).Hash -ne $qjsSha) { Remove-Item $qjs; throw 'qjs.exe checksum mismatch' }
}

# minimal ffmpeg: cross-compiled once in WSL (Ubuntu) by build-ffmpeg.sh
if (-not (Test-Path build\ffmpeg-min\ffmpeg.exe)) { wsl -u root -- bash build-ffmpeg.sh }  # WSL starts in this folder
if (-not (Test-Path build\ffmpeg-min\ffprobe.exe)) { throw 'build-ffmpeg.sh did not produce build\ffmpeg-min' }

# tools.zip: ffmpeg + ffprobe + qjs, LZMA. The app unpacks it once to LOCALAPPDATA (see unpack_tools)
& $py -c @'
import zipfile
with zipfile.ZipFile('build/tools.zip', 'w', zipfile.ZIP_LZMA) as z:
    for p in ('build/ffmpeg-min/ffmpeg.exe', 'build/ffmpeg-min/ffprobe.exe', 'build/qjs.exe'):
        z.write(p, p.rsplit('/', 1)[1])
'@
if ($LASTEXITCODE) { throw 'build step failed' }
& $py -c @'
import importlib.machinery as m
from PIL import Image, ImageDraw, ImageFont
d = m.SourceFileLoader('d', 'donat_downloader.pyw').load_module()
d.donut(256).save('build/donat.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(256,256)])
s = Image.new('RGB', (380, 150), '#1c1c1c')  # splash: shown while the single exe unpacks itself
s.paste(d.donut(96), (24, 27), d.donut(96))
ImageDraw.Draw(s).text((136, 52), 'Donat Downloader', fill='#fafafa', font=ImageFont.truetype('segoeuib.ttf', 24))
ImageDraw.Draw(s).text((137, 86), 'Loading\u2026', fill='#a0a0a0', font=ImageFont.truetype('segoeui.ttf', 15))
ImageDraw.Draw(s).text((137, 112), 'By : Dani Abdani', fill='#7a7a7a', font=ImageFont.truetype('segoeui.ttf', 12))
s.save('build/splash.png')
'@
if ($LASTEXITCODE) { throw 'build step failed' }
Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
# Left out: PIL AVIF (7.5 MB) and colour/maths/morph modules (thumbnails are only JPEG/WebP/PNG), and Cryptodome:
# yt-dlp has a pure-Python AES fallback, and its 42 .pyd files cost ~1 s per start (onefile re-extracts them on
# every launch and Windows Defender scans each fresh binary).
# 1) spec from the options, 2) drop Tcl's time-zone/locale data (735 files the app never uses; a onefile exe
# re-extracts every file on every launch), 3) build from the spec.
& $py -m PyInstaller.utils.cliutils.makespec --windowed --onefile --name 'Donat Downloader' --icon "$PWD\build\donat.ico" `
    --splash "$PWD\build\splash.png" --specpath build `
    --collect-data sv_ttk --collect-data yt_dlp_ejs --hidden-import pystray._win32 `
    --copy-metadata yt-dlp --copy-metadata yt-dlp-ejs `
    --exclude-module PIL._avif --exclude-module PIL.AvifImagePlugin --exclude-module PIL._imagingcms `
    --exclude-module PIL.ImageCms --exclude-module PIL._imagingmath --exclude-module PIL._imagingmorph `
    --exclude-module Cryptodome `
    --add-data "$PWD\build\tools.zip;." `
    donat_downloader.pyw | Out-Null
& $py -c @'
p = 'build/Donat Downloader.spec'
s = open(p, encoding='utf-8').read()
cut = '''a.datas = [d for d in a.datas if not d[0].replace(chr(92), '/').startswith(('_tcl_data/tzdata', '_tcl_data/msgs', '_tk_data/msgs'))]''' + chr(10)
open(p, 'w', encoding='utf-8').write(s.replace('pyz = PYZ(', cut + 'pyz = PYZ(', 1))
'@
if ($LASTEXITCODE) { throw 'build step failed' }
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\work 'build\Donat Downloader.spec'
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
Remove-Item -Recurse -Force build\work

$p = Start-Process 'dist\Donat Downloader.exe' -ArgumentList '--test' -Wait -PassThru
if ($p.ExitCode) { throw "selftest inside the exe failed ($($p.ExitCode))" }
"OK: dist\Donat Downloader.exe  ($([math]::Round((Get-Item 'dist\Donat Downloader.exe').Length / 1MB)) MB)"
