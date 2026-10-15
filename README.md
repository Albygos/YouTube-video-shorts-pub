# ReelForge Malayalam — full source (root-level files)

All project files are directly in the repository root and ZIP root.

## YouTube downloader: pytubefix

The project uses **pytubefix** instead of yt-dlp. It tries the documented `WEB` client first (which can use pytubefix's Node.js-based PoToken generation where supported), then tries `ANDROID_VR` as a compatibility fallback. It selects a progressive MP4 stream when available, checks the duration/size, and verifies the downloaded file with FFprobe before processing it.

**Important limitation:** no third-party YouTube downloader library can guarantee that YouTube will never request verification or block a cloud-hosted Render IP. pytubefix itself documents PoToken/bot-detection cases. If both clients are blocked, use the direct-upload option below; the app reports a clear error instead of claiming the download succeeded. Do not use account cookies or tokens you are not authorized to use.

Official references: https://pytubefix.readthedocs.io/en/latest/user/quickstart.html and https://pytubefix.readthedocs.io/en/latest/user/po_token.html

## Direct upload (recommended when YouTube blocks Render)

Select **Upload video** on the site and choose an MP4, MOV, MKV, WEBM, M4V, or AVI file up to `MAX_UPLOAD_MB` (default 90 MB). The backend validates the file using FFprobe, transcribes Malayalam speech, scores moments, detects scene cuts, renders vertical reels, and returns MP4/SRT/transcript/manifest downloads. Only upload files you own or are authorized to edit.

## Features

- FastAPI web application; YouTube URL or direct file upload.
- Malayalam-first Faster-Whisper transcription with VAD, word timestamps, beam search and Malayalam prompt.
- Conservative punctuation/spacing cleanup; no fabricated or translated transcript.
- Transcript-based clip ranking, question/hook cues, phrase boundaries, scene-change candidates, and overlap diversification.
- Vertical 9:16 MP4 output, optional burned-in Malayalam captions, SRT files, transcript JSON/text, manifest, and ZIP bundle.

## Deploy on Render

1. Extract the ZIP; confirm `main.py`, `index.html`, `app.js`, `style.css`, `requirements.txt`, `Dockerfile`, and `render.yaml` are at the repository root.
2. Push all files to GitHub.
3. In Render, choose **New → Blueprint**, then select the repository.
4. Wait for the Docker build to finish; first-time model download can take time.
5. Visit `/health` and expect JSON with `status: ok`.
6. Use YouTube URLs where accessible, or upload the video directly.

The Docker image installs FFmpeg, FFprobe, Noto fonts, and Node.js for pytubefix token support. The service runs one job at a time to reduce memory pressure.

## Local Docker test

```bash
docker build -t reelforge-ml .
docker run --rm -p 10000:10000 -e PORT=10000 reelforge-ml
```

Open http://localhost:10000.

## Environment variables

- `WHISPER_MODEL`: `tiny`, `base` (default), `small`, or `medium`. `small` may improve Malayalam accuracy but may exceed Render Free memory/time limits.
- `MAX_CONCURRENT_JOBS`: default `1`.
- `MAX_VIDEO_MINUTES`: default `240`; actual processing may time out on Render Free for long podcasts.
- `MAX_UPLOAD_MB`: default `90`. Keep uploads smaller if your Render request/network limits require it.
- `WORK_DIR`: temporary work directory, default `/tmp/malayalam-reels`.

## Honest limitations

No downloader can guarantee that YouTube will serve every video to a cloud server. Cookies may expire and datacenter IPs may remain blocked; direct upload avoids that dependency. Render Free has limited memory/CPU, ephemeral disk, and request/job lifetime constraints. A successful ZIP build and syntax test do not replace a real live deployment test. Speech-to-text can still mishear dialect, names, code-switching, or noisy speech; review the transcript before publishing. Scene detection detects visual cuts, not human emotion or semantic meaning.


## Downloader implementation and verification limits

This project uses `pytubefix==11.2.0` with its documented `WEB` client. The official Pytubefix PoToken guide says this client can automatically generate a PoToken when Node.js is available. The Dockerfile installs Node.js. The app checks the downloaded file with `ffprobe` before processing it.

This does **not** bypass every YouTube restriction. Pytubefix's own PoToken FAQ says bot detection can continue even with a PoToken, particularly when a server IP is blocked. If that happens on Render, use the direct-upload path for media you own or are authorized to process. No third-party downloader library can honestly guarantee that all YouTube URLs will work without verification.
