<p align="center">
  <img src="docs/images/splash.png" alt="Donat Downloader" width="560">
</p>

<p align="center">
  A fast, modern downloader for video and music from YouTube and 1,700+ other sites.<br>
  Copy a link - Notif on tray, pick a format, done.
</p>

<p align="center">
  <a href="../../releases/latest"><b>Download the latest release</b></a> ·
  single <code>.exe</code>, nothing else to install ·
  <a href="https://abdani.github.io/Donat-Downloader/">Website</a>
</p>

![Dashboard](docs/images/dashboard.png)

## Features

- **Copy a link and it's ready.** Donat watches the clipboard. When you copy a supported link, a card pops up above the tray. One click opens the format panel, where you confirm before anything downloads.
- **YouTube and 1,700+ sites**, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp): TikTok, Instagram, X, SoundCloud, Bandcamp and many more. Playlists and channels download into their own subfolder, after a confirmation.
- **Choose the exact format**
  - Video: MP4, MKV or WebM, resolutions up to 2160p, codec H.264, VP9 or AV1.
  - Audio: MP3 (128–320 kbps), M4A, Opus, FLAC or WAV.
  - Size estimates in the format panel match the file that actually downloads.
- **Clips.** Download only part of a video, from `1:30` to `2:45`.
- **Fast.** Parallel connections per download, several downloads at once, and pause/resume that continues where it stopped. An optional speed limit is there when you need the bandwidth for something else.
- **History dashboard** with thumbnails and search. Right-click any row to open the file, show it in its folder, copy the link, download it again in another format, or move the file to the Recycle Bin.
- **Embeds** the thumbnail, metadata and (optionally) Indonesian/English subtitles into the file.
- **Stays out of the way.** It can start with Windows straight into the tray, and the tray icon shows overall progress. Drag a link from the browser onto the window to add it.
- **YouTube login only when needed.** If YouTube asks for a sign-in (its anti-bot check), Donat can reuse the login from your browser, or from a `cookies.txt` file.
- **Light, dark or system theme**, in English or Indonesian.
- **Self-contained.** One ~33 MB exe with FFmpeg and a JavaScript runtime (QuickJS) built in. The yt-dlp engine updates itself from PyPI, and every update is checksum-verified.

## Screenshots

| Format panel and downloads | Copied a link? |
| --- | --- |
| ![Downloads](docs/images/downloads.png) | ![Clipboard card](docs/images/clipboard-card.png) |

| Right-click menu | Settings |
| --- | --- |
| ![Context menu](docs/images/context-menu.png) | ![Settings](docs/images/settings.png) |

<details>
<summary>Light theme</summary>

![Dashboard, light theme](docs/images/dashboard-light.png)

</details>

## Usage

1. Download `Donat Downloader.exe` from [Releases](../../releases/latest) and run it. It needs Windows 10 or 11, 64-bit.
2. Copy a video link anywhere. You can also paste it into **Downloads**, or drag it onto the window.
3. Pick mode, resolution and format, then press **Download**.

Files are saved to `Downloads\Donat Downloader` by default. You can change the folder in **Settings**.

On the first launch Windows SmartScreen may warn about an unknown publisher, because the exe isn't code-signed. Choose **More info → Run anyway**.

## Build from source

You need Python 3.12 on Windows, and WSL (Ubuntu) for the one-time FFmpeg build.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python donat_downloader.pyw          # run from source
.\.venv\Scripts\python donat_downloader.pyw --test   # self-test
.\build.ps1                                          # -> dist\Donat Downloader.exe
```

`build.ps1` does the following:
- cross-compiles a minimal FFmpeg with `build-ffmpeg.sh` (only on the first run);
- fetches a pinned, checksum-verified QuickJS;
- bundles everything into a single exe with PyInstaller;
- runs the self-test inside the finished exe.

## Built with

| Component | Purpose | License |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Download engine | Unlicense |
| [FFmpeg](https://ffmpeg.org) | Merging, conversion, clips | LGPL 2.1+ (with LAME: LGPL, Opus: BSD) |
| [QuickJS-ng](https://github.com/quickjs-ng/quickjs) | Solves YouTube's JavaScript challenges | MIT |
| [Sun Valley ttk](https://github.com/rdbende/Sun-Valley-ttk-theme) | Modern look for Tkinter | MIT |
| [Pillow](https://python-pillow.org), [pystray](https://github.com/moses-palmer/pystray), [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) | Thumbnails, tray icon, drag & drop | MIT-CMU, LGPL-3.0, MIT |

The bundled FFmpeg is built from unmodified sources by [`build-ffmpeg.sh`](build-ffmpeg.sh), without `--enable-gpl`.

## Disclaimer

Download only content you have the right to download, and respect each site's terms of service. The screenshots show Blender Studio open movies (CC BY).

## License

[MIT](LICENSE) © 2026 Dani Abdani
