#!/usr/bin/env bash
# Minimal static ffmpeg/ffprobe for Donat Downloader, cross-compiled for Windows in WSL/Ubuntu:
#   wsl -u root -- bash build-ffmpeg.sh        (output: build/ffmpeg-min/{ffmpeg,ffprobe}.exe)
# Only what yt-dlp needs here: stream-copy merge (mp4/mkv/webm), audio convert (mp3/m4a/opus/flac/wav),
# cover art (jpg/png/webp), subtitles (srt/vtt/ass -> mov_text/webvtt), metadata/chapters, HLS (mpegts) + flv inputs.
set -euo pipefail
FF=9.0.2 LAME=3.100 OPUS=1.5.2
OUT="$(cd "$(dirname "$0")" && pwd)/build/ffmpeg-min"
W=/root/ffbuild P=/root/ffbuild/prefix H=x86_64-w64-mingw32
export DEBIAN_FRONTEND=noninteractive PKG_CONFIG_LIBDIR=$P/lib/pkgconfig
apt-get -qq update && apt-get -qq install -y mingw-w64 nasm make pkg-config libz-mingw-w64-dev curl xz-utils >/dev/null
mkdir -p $W $P && cd $W
fetch() { [ -d "$2" ] || { curl -fsSL "$1" -o src.tmp && tar x${3:-z}f src.tmp && rm src.tmp; }; }
fetch https://deb.debian.org/debian/pool/main/l/lame/lame_$LAME.orig.tar.gz lame-$LAME
fetch https://downloads.xiph.org/releases/opus/opus-$OPUS.tar.gz opus-$OPUS
fetch https://ffmpeg.org/releases/ffmpeg-$FF.tar.xz ffmpeg-$FF J

cd $W/lame-$LAME && [ -f $P/lib/libmp3lame.a ] || {
  ./configure -q --host=$H --prefix=$P --disable-shared --enable-static --disable-frontend --disable-decoder CFLAGS=-O2
  make -s -j"$(nproc)" install >/dev/null; }
cd $W/opus-$OPUS && [ -f $P/lib/libopus.a ] || {
  ./configure -q --host=$H --prefix=$P --disable-shared --enable-static --disable-doc --disable-extra-programs CFLAGS=-O2
  make -s -j"$(nproc)" install >/dev/null; }

cd $W/ffmpeg-$FF
./configure --prefix=$P --target-os=mingw32 --arch=x86_64 --cross-prefix=$H- --pkg-config=pkg-config --pkg-config-flags=--static \
  --extra-cflags="-I$P/include" --extra-ldflags="-L$P/lib -static -s" \
  --disable-everything --disable-autodetect --disable-debug --disable-doc --disable-network --disable-ffplay \
  --enable-static --disable-shared --enable-zlib --enable-libmp3lame --enable-libopus --enable-w32threads \
  --enable-protocol=file,pipe \
  --enable-demuxer=mov,matroska,mpegts,flv,mp3,ogg,flac,wav,aac,image2,image_jpeg_pipe,image_png_pipe,image_webp_pipe,webvtt,srt,ass,ffmetadata,concat \
  --enable-muxer=mp4,mov,ipod,matroska,webm,mp3,ogg,opus,flac,wav,adts,image2,mjpeg,webvtt,srt,ass,ffmetadata,null \
  --enable-decoder=aac,aac_latm,opus,vorbis,mp3,mp3float,flac,alac,pcm_s16le,pcm_s24le,pcm_f32le,mjpeg,png,webp,vp8,vp9,bmp,webvtt,subrip,srt,ass,ssa,movtext \
  --enable-encoder=libmp3lame,aac,libopus,flac,pcm_s16le,mjpeg,png,movtext,webvtt,srt,subrip,ass \
  --enable-parser=h264,hevc,vp8,vp9,av1,aac,aac_latm,opus,vorbis,mpegaudio,flac,mjpeg,png,webp \
  --enable-bsf=aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,vp9_superframe,vp9_superframe_split,av1_frame_split,extract_extradata,setts,null \
  --enable-filter=aresample,aformat,anull,format,null,scale,copy,crop,pad,setsar \
  >/dev/null
make -s -j"$(nproc)" >/dev/null
mkdir -p "$OUT" && cp ffmpeg.exe ffprobe.exe "$OUT/"
ls -la "$OUT"
