from __future__ import annotations
import asyncio, json, os, re, shutil, subprocess, time, uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
WORK = Path(os.getenv('WORK_DIR', '/tmp/malayalam-reels'))
WORK.mkdir(parents=True, exist_ok=True)
MAX_JOBS = int(os.getenv('MAX_CONCURRENT_JOBS', '1'))
MAX_VIDEO_MINUTES = int(os.getenv('MAX_VIDEO_MINUTES', '240'))
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '90'))
MAX_CLIPS = 5
jobs: dict[str, dict[str, Any]] = {}
job_lock = asyncio.Semaphore(MAX_JOBS)
app = FastAPI(title='Malayalam Podcast Reel Studio', version='1.0.0')

class CreateRequest(BaseModel):
    youtube_url: str = Field(min_length=8, max_length=500)
    clips: int = Field(default=5, ge=1, le=5)
    clip_seconds: int = Field(default=45, ge=30, le=60)
    language: str = Field(default='ml', pattern='^(ml|auto)$')
    captions: bool = True
    smart_crop: bool = True
    max_download_mb: int = Field(default=700, ge=100, le=1500)


def valid_youtube_url(raw: str) -> str:
    try:
        u = urlparse(raw.strip())
        host = (u.hostname or '').lower()
        allowed = {'youtube.com','www.youtube.com','m.youtube.com','youtu.be','www.youtube-nocookie.com'}
        if host not in allowed or u.scheme not in ('https','http'):
            raise ValueError
        if host.endswith('youtu.be'):
            video_id = u.path.strip('/').split('/')[0]
        else:
            video_id = parse_qs(u.query).get('v', [''])[0]
            if not video_id and u.path.startswith('/shorts/'):
                video_id = u.path.split('/')[2]
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError
        return f'https://www.youtube.com/watch?v={video_id}'
    except Exception:
        raise HTTPException(400, 'Enter a valid public YouTube video URL.')


def run_cmd(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def require_binary(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f'Required program {name} was not found. Use the included Dockerfile deployment.')
    return p


def probe(path: Path) -> dict[str, Any]:
    p = run_cmd([require_binary('ffprobe'), '-v','error','-show_entries','format=duration,size:stream=codec_type,width,height','-of','json',str(path)], 90)
    if p.returncode != 0:
        raise RuntimeError('Could not inspect downloaded video: ' + p.stderr[-400:])
    data = json.loads(p.stdout)
    duration = float(data.get('format', {}).get('duration', 0))
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    if duration <= 0 or not video:
        raise RuntimeError('The downloaded file does not contain a valid video stream.')
    if duration > MAX_VIDEO_MINUTES * 60:
        raise RuntimeError(f'Video is longer than the configured limit ({MAX_VIDEO_MINUTES} minutes).')
    return {'duration': duration, 'width': int(video.get('width', 0)), 'height': int(video.get('height', 0))}


def download_video(url: str, outdir: Path, max_mb: int) -> tuple[Path, dict[str, Any]]:
    """Download a public YouTube video using pytubefix's documented WEB client.

    Pytubefix documents automatic PoToken generation for the WEB client when a
    working Node.js runtime is available. This is a supported extraction path,
    not a guarantee against YouTube blocking a cloud-hosted IP.
    """
    require_binary('ffmpeg')
    require_binary('ffprobe')
    require_binary('node')
    try:
        from pytubefix import YouTube
        from importlib.metadata import version
        pytubefix_version = version('pytubefix')
    except ImportError as exc:
        raise RuntimeError(
            'pytubefix is missing. Redeploy using the included Dockerfile and requirements.txt.'
        ) from exc

    # Remove partial output from a previous attempt; never treat it as a success.
    for old in outdir.glob('source*'):
        try:
            if old.is_file():
                old.unlink()
        except OSError:
            pass

    try:
        # Official pytubefix PoToken documentation recommends the WEB client for
        # automatic token generation. Do not rotate through undocumented clients.
        yt = YouTube(url, client='WEB')
        title = str(getattr(yt, 'title', '') or 'YouTube podcast')
        channel = str(getattr(yt, 'author', '') or '')
        duration = int(getattr(yt, 'length', 0) or 0)
        if duration <= 0:
            raise RuntimeError('YouTube did not provide a valid video duration.')
        if duration > MAX_VIDEO_MINUTES * 60:
            raise RuntimeError(f'Video is longer than the configured limit ({MAX_VIDEO_MINUTES} minutes).')

        # Prefer a combined MP4 stream to avoid a second request for audio.
        streams = yt.streams.filter(progressive=True, file_extension='mp4')
        stream = streams.order_by('resolution').desc().first()
        if stream is None:
            # DASH streams may require merging; FFmpeg is present in the container.
            stream = yt.streams.get_highest_resolution()
        if stream is None:
            raise RuntimeError('Pytubefix did not return a downloadable stream for this video.')

        estimated_size = getattr(stream, 'filesize', None) or getattr(stream, 'filesize_approx', None)
        if estimated_size and estimated_size > max_mb * 1024 * 1024:
            raise RuntimeError(f'The selected stream exceeds the configured {max_mb} MB limit.')

        downloaded = stream.download(
            output_path=str(outdir),
            filename='source.mp4',
            skip_existing=False,
            max_retries=3,
        )
        path = Path(downloaded) if downloaded else outdir / 'source.mp4'
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            candidates = [x for x in outdir.glob('source*') if x.is_file() and x.stat().st_size > 0]
            if not candidates:
                raise RuntimeError('Pytubefix did not produce a valid video file.')
            path = max(candidates, key=lambda x: x.stat().st_size)
        if path.stat().st_size > max_mb * 1024 * 1024:
            path.unlink(missing_ok=True)
            raise RuntimeError(f'Download exceeds the configured {max_mb} MB limit.')

        metadata = probe(path)
        return path, {
            'title': title,
            'channel': channel,
            'duration': metadata['duration'],
            'webpage_url': url,
            'downloader': 'pytubefix',
            'pytubefix_version': pytubefix_version,
            'client': 'WEB',
        }
    except Exception as exc:
        for partial in outdir.glob('source*'):
            try:
                if partial.is_file():
                    partial.unlink()
            except OSError:
                pass
        message = str(exc).lower()
        if any(term in message for term in ('longer than the configured limit', 'exceeds the configured')):
            raise RuntimeError(str(exc)) from exc
        if any(term in message for term in ('bot', 'po token', 'potoken', 'sign in', 'verification', '403', '429')):
            raise RuntimeError(
                'YouTube rejected the pytubefix WEB-client request (playback-token, sign-in, or IP restriction). '
                'Pytubefix supports automatic PoToken generation, but its documentation states this cannot guarantee '
                'access when YouTube detects a bot or blocks the server IP. No alternate library can guarantee access. '
                'Use the Upload video option for a video you own or are authorized to process. '
                f'Original error: {str(exc)[-500:]}'
            ) from exc
        raise RuntimeError(
            f'Pytubefix WEB-client download failed. Confirm the video is public and try again; '
            f'if it persists, use direct upload. Original error: {str(exc)[-700:]}'
        ) from exc

def validate_local_video(path: Path, max_mb: int) -> tuple[Path, dict[str, Any]]:
    """Validate a user-uploaded video with ffprobe before processing it."""
    require_binary('ffmpeg'); require_binary('ffprobe')
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError('The uploaded video is empty or unavailable. Please upload it again.')
    if path.stat().st_size > max_mb * 1024 * 1024:
        raise RuntimeError(f'Uploaded video exceeds the {max_mb} MB limit.')
    meta = probe(path)
    return path, {'title': path.stem[:150] or 'Uploaded podcast', 'channel': 'Uploaded video', 'duration': meta['duration'], 'webpage_url': 'uploaded-file'}


def transcribe(path: Path, outdir: Path, language: str) -> list[dict[str, Any]]:
    # CPU-friendly configuration for Render's limited memory; no model files are stored permanently.
    from faster_whisper import WhisperModel
    model_size = os.getenv('WHISPER_MODEL', 'base').strip().lower()
    if model_size not in {'tiny', 'base', 'small', 'medium'}:
        model_size = 'base'
    model = WhisperModel(model_size, device='cpu', compute_type='int8', cpu_threads=max(1, min(4, os.cpu_count() or 2)), num_workers=1, download_root=str(WORK / 'models'))
    segments, info = model.transcribe(str(path), language=None if language == 'auto' else language,
        beam_size=5, best_of=5, patience=1.0, vad_filter=True,
        vad_parameters={'min_silence_duration_ms': 450, 'speech_pad_ms': 250},
        word_timestamps=True, condition_on_previous_text=True,
        initial_prompt=('മലയാളം പോഡ്കാസ്റ്റ് സംഭാഷണം. മലയാളം വാക്കുകളും വ്യക്തികളുടെ പേരുകളും സ്ഥലനാമങ്ങളും '
                        'ഇംഗ്ലീഷ് വാക്കുകളും ഉള്ളതുപോലെ കൃത്യമായി എഴുതുക. വാക്കുകൾ വിവർത്തനം ചെയ്യരുത്. '
                        'വ്യക്തമായി കേൾക്കാത്ത വാക്കുകൾ ഊഹിച്ച് ചേർക്കരുത്.'))
    result = []
    for seg in segments:
        words = []
        for w in (getattr(seg, 'words', None) or []):
            words.append({'start': round(float(w.start),3), 'end': round(float(w.end),3), 'word': w.word.strip()})
        text = clean_transcript_text(seg.text)
        if text:
            result.append({'start': round(float(seg.start),3), 'end': round(float(seg.end),3), 'text': text, 'words': words})
    (outdir / 'transcript.json').write_text(json.dumps({'language': getattr(info,'language',language), 'segments': result}, ensure_ascii=False, indent=2), encoding='utf-8')
    del model
    return result


def clean_transcript_text(text: str) -> str:
    """Conservative Malayalam transcript cleanup: whitespace and punctuation only."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = re.sub(r"([,;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"([.!?…।]){2,}", r"\1", text)
    return text


# Rank moments using transcript signals + speech completeness + sentence boundaries.
# This is intentionally transparent, deterministic, and does not invent a summary or alter spoken words.
HOOKS = ['പക്ഷേ','എന്നാൽ','കാരണം','സത്യത്തിൽ','അറിയാമോ','ശ്രദ്ധിക്കുക','പ്രശ്നം','വിജയം','പരാജയം','പണം','ജീവിതം','ഒരിക്കലും','ആദ്യമായി','അവസാനം','രഹസ്യം','തെറ്റ്','ശരി','എന്തുകൊണ്ട്','എങ്ങനെ','പഠിച്ചു','അനുഭവം','ഞാൻ','നിങ്ങൾ','അതുകൊണ്ട്','പക്ഷെ','പ്രധാനപ്പെട്ട','വലിയ','ചെറിയ','actually','but','because','secret','mistake','money','life','success','failure','why','how']
QUESTION = re.compile(r'[?？]|എന്ത്|എങ്ങനെ|എന്തുകൊണ്ട്|ആണോ|എന്താണ്')
ENDING = re.compile(r'[.!?…।॥]$')

def score_window(segs: list[dict[str, Any]], start: float, end: float) -> tuple[float,str]:
    items = [s for s in segs if s['end'] > start and s['start'] < end]
    text = ' '.join(s['text'] for s in items).strip()
    if not text:
        return -100, ''
    words = re.findall(r'\S+', text)
    score = min(len(words) / 22, 1.7) * 1.4
    low = text.lower()
    score += min(sum(1 for h in HOOKS if h.lower() in low) * 0.28, 1.8)
    if QUESTION.search(text): score += 0.55
    if any(x in text for x in ['!', '?!', 'അല്ല', 'പക്ഷേ', 'എന്നാൽ']): score += 0.3
    # Prefer clips that begin close to a sentence/segment boundary and end on a complete phrase.
    first = min(items, key=lambda s: abs(s['start']-start))
    last = max(items, key=lambda s: s['end'])
    if abs(first['start'] - start) <= 2.0: score += 0.45
    if ENDING.search(last['text'].strip()): score += 0.5
    duration = max(1, end-start)
    density = len(words)/duration
    if density < 0.8: score -= 0.7
    if density > 5.5: score -= 0.25
    if len(words) < 20: score -= 1.2
    # Penalize windows that start or end in the middle of a long segment.
    if first['start'] < start - 4: score -= 0.45
    if last['end'] > end + 4: score -= 0.45
    return score, text


def detect_scene_changes(video: Path, duration: float) -> list[float]:
    """Use FFmpeg's scene-change detector on a tiny proxy stream; return timestamps."""
    # Low-resolution, low-frame-rate analysis keeps memory modest on free instances.
    vf = "fps=2,scale=256:-2,select='gt(scene,0.32)',showinfo"
    p = run_cmd([require_binary('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-i', str(video), '-an', '-vf', vf, '-f', 'null', '-'], timeout=max(180, int(duration * 1.2)))
    # FFmpeg may return non-zero on some container/codec edge cases; transcript scoring still works.
    times = []
    for line in p.stderr.splitlines():
        if 'showinfo' not in line or 'pts_time:' not in line:
            continue
        m = re.search(r'pts_time:([0-9]+(?:\.[0-9]+)?)', line)
        if m:
            t = float(m.group(1))
            if 0 < t < duration:
                times.append(t)
    return sorted(set(round(t, 2) for t in times))


def identify_clips(segments: list[dict[str, Any]], duration: float, count: int, clip_seconds: int, scene_times: list[float] | None = None) -> list[dict[str, Any]]:
    if not segments:
        raise RuntimeError('Speech recognition returned no speech. Try another video or language setting.')
    # Candidate starts are aligned to transcript segments. Slide windows across the full recording.
    candidates = []
    step = max(5, min(10, clip_seconds // 5))
    max_start = max(0, duration - clip_seconds)
    starts = set(float(s['start']) for s in segments if s['start'] <= max_start)
    # Scene boundaries provide extra candidates, improving cuts when the camera/edit changes.
    for scene_t in (scene_times or []):
        for aligned in (scene_t, scene_t - 1.0, scene_t - 2.0):
            if 0 <= aligned <= max_start:
                starts.add(round(aligned, 2))
    t = 0.0
    while t <= max_start:
        starts.add(t); t += step
    for start in sorted(starts):
        start = max(0.0, min(float(start), max_start))
        end = min(duration, start + clip_seconds)
        if end-start < 25: continue
        score, text = score_window(segments, start, end)
        if scene_times:
            # Favor candidates with a visual scene transition near the opening, without forcing a bad cut.
            if any(abs(st - start) <= 1.5 for st in scene_times): score += 0.35
            if any(abs(st - end) <= 1.5 for st in scene_times): score += 0.15
        if score > -10:
            candidates.append({'start': start, 'end': end, 'score': score, 'transcript': text})
    candidates.sort(key=lambda c: c['score'], reverse=True)
    chosen = []
    # Diversify clips: avoid nearly duplicate sections and favor different moments in the episode.
    min_gap = max(clip_seconds * 0.72, 22)
    for c in candidates:
        if all(abs(c['start']-x['start']) >= min_gap for x in chosen):
            chosen.append(c)
            if len(chosen) >= count: break
    if len(chosen) < count:
        for c in candidates:
            if c not in chosen:
                chosen.append(c)
                if len(chosen) >= count: break
    chosen.sort(key=lambda c: c['start'])
    for i, c in enumerate(chosen, 1):
        c['index'] = i
        c['title'] = make_title(c['transcript'])
        c['hook'] = make_hook(c['transcript'])
    return chosen


def make_title(text: str) -> str:
    clean = re.sub(r'\s+', ' ', text).strip(' .!?।')
    words = clean.split()
    return (' '.join(words[:8]) + ('…' if len(words)>8 else ''))[:90] or 'Malayalam Podcast Highlight'

def make_hook(text: str) -> str:
    clean = re.sub(r'\s+', ' ', text).strip()
    # Use verbatim transcript text, never fabricate a quote.
    words = clean.split()
    return ' '.join(words[:12]) + ('…' if len(words)>12 else '')


def make_ass(path: Path, segs: list[dict[str, Any]], start: float, end: float) -> Path:
    def ts(v: float) -> str:
        cs = max(0, round(v*100)); h, rem = divmod(cs, 360000); m, rem = divmod(rem, 6000); s, c = divmod(rem, 100)
        return f'{h}:{m:02d}:{s:02d}.{c:02d}'
    def esc(s: str) -> str:
        return s.replace('\\','\\\\').replace('{','\\{').replace('}','\\}').replace('\n',' ')
    lines = [
        '[Script Info]','ScriptType: v4.00+','PlayResX: 1080','PlayResY: 1920','WrapStyle: 2','ScaledBorderAndShadow: yes','',
        '[V4+ Styles]','Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        'Style: Default,Noto Sans Malayalam,58,&H00FFFFFF,&H0000D7FF,&H00111111,&H80000000,1,0,1,4,1,2,80,80,250,1','',
        '[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for seg in segs:
        a, b = max(start, seg['start']), min(end, seg['end'])
        text = seg['text'].strip()
        if b <= a or not text: continue
        # Keep lines readable on mobile; retain Malayalam unicode.
        chunks=[]; line=''
        for word in text.split():
            if len(line)+len(word)+1 > 30 and line:
                chunks.append(line); line=word
            else: line=(line+' '+word).strip()
        if line: chunks.append(line)
        txt='\\N'.join(chunks)
        lines.append(f'Dialogue: 0,{ts(a-start)},{ts(b-start)},Default,,0,0,0,,{{\\an2}}{esc(txt)}')
    ass = path.with_suffix('.ass')
    ass.write_text('\n'.join(lines), encoding='utf-8-sig')
    return ass


def render_clip(video: Path, outdir: Path, clip: dict[str, Any], segs: list[dict[str, Any]], captions: bool, smart_crop: bool) -> Path:
    idx=clip['index']; out=outdir/f'reel_{idx:02d}.mp4'
    duration=clip['end']-clip['start']
    # Scale-to-fill 9:16. Center crop is deterministic and safe on CPU; face-aware crop is not falsely claimed.
    if smart_crop:
        vf="scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:(iw-ow)/2:(ih-oh)/2,setsar=1,fps=30,format=yuv420p"
    else:
        vf="scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p"
    ass=None
    if captions:
        ass=make_ass(out, segs, clip['start'], clip['end'])
        # ffmpeg filter paths must escape colons and apostrophes.
        p=str(ass.resolve()).replace('\\','/').replace(':','\\:').replace("'", "\\'")
        vf += f",subtitles='{p}'"
    args=[require_binary('ffmpeg'),'-hide_banner','-loglevel','error','-y','-ss',f"{clip['start']:.3f}",'-i',str(video),'-t',f'{duration:.3f}','-vf',vf,
          '-map','0:v:0','-map','0:a:0?','-c:v','libx264','-preset','ultrafast','-crf','24','-c:a','aac','-b:a','128k','-movflags','+faststart','-shortest',str(out)]
    p=run_cmd(args, timeout=max(600,int(duration*30)))
    if p.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f'FFmpeg could not render reel {idx}: {p.stderr[-700:]}')
    if ass and ass.exists(): ass.unlink()
    return out


def make_srt(path: Path, segs: list[dict[str, Any]], start: float, end: float) -> None:
    def t(v):
        ms=max(0,round(v*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000); return f'{h:02}:{m:02}:{s:02},{ms:03}'
    out=[]; n=1
    for s in segs:
        a=max(start,s['start']); b=min(end,s['end'])
        if b<=a: continue
        out += [str(n),f'{t(a-start)} --> {t(b-start)}',s['text'],'']; n+=1
    path.write_text('\n'.join(out),encoding='utf-8')


def do_job(job_id: str, req: dict[str, Any]) -> None:
    folder=WORK/job_id; folder.mkdir(parents=True,exist_ok=True)
    def update(**kw): jobs[job_id].update(kw)
    try:
        if req.get('source_path'):
            update(status='processing',stage='Validating uploaded video',progress=5,message='Checking the uploaded podcast file…')
            video, meta = validate_local_video(Path(req['source_path']), req.get('max_download_mb', MAX_UPLOAD_MB))
        else:
            update(status='processing',stage='Downloading video',progress=5,message='Fetching the YouTube video…')
            video, meta=download_video(req['youtube_url'],folder,req['max_download_mb'])
        info=probe(video)
        update(stage='Transcribing Malayalam speech',progress=20,message='Running speech recognition. Long podcasts can take several minutes.',title=meta['title'],duration=info['duration'])
        segs=transcribe(video,folder,req['language'])
        update(stage='Analyzing scene changes',progress=48,message='Detecting visual cuts and aligning them with the Malayalam transcript…')
        scene_times=detect_scene_changes(video,info['duration'])
        update(stage='Finding the strongest moments',progress=53,message='Scoring hooks, complete phrases, speech density, scene boundaries, and duplicate overlap…')
        clips=identify_clips(segs,info['duration'],req['clips'],req['clip_seconds'],scene_times)
        if not clips: raise RuntimeError('Could not identify usable speech moments in this video.')
        update(stage='Rendering vertical reels',progress=58,message=f'Rendering {len(clips)} vertical clips with synchronized Malayalam subtitles…',clips=[{k:v for k,v in c.items() if k!='transcript'} for c in clips])
        results=[]
        for n,c in enumerate(clips):
            path=render_clip(video,folder,c,segs,req['captions'],req['smart_crop'])
            srt=folder/f'reel_{c["index"]:02d}.srt'; make_srt(srt,segs,c['start'],c['end'])
            results.append({'index':c['index'],'title':c['title'],'hook':c['hook'],'start':c['start'],'end':c['end'],'score':round(c['score'],2),'video':f'/download/{job_id}/{path.name}','captions':f'/download/{job_id}/{srt.name}','size_mb':round(path.stat().st_size/1024/1024,2)})
            update(progress=58+int(37*(n+1)/len(clips)),message=f'Rendered {n+1} of {len(clips)} reels…',results=results)
        (folder/'reel_manifest.json').write_text(json.dumps({'source':meta,'clips':results},ensure_ascii=False,indent=2),encoding='utf-8')
        # Human-readable transcript preserves recognized Malayalam wording; no automatic grammar rewrite.
        transcript_lines = []
        for seg in segs:
            mm, ss = divmod(int(seg['start']), 60)
            transcript_lines.append(f'[{mm:02d}:{ss:02d}] {seg["text"]}')
        (folder/'transcript_ml.txt').write_text('\n'.join(transcript_lines), encoding='utf-8')
        # ZIP contains MP4, SRT, transcript and manifest; source video is intentionally excluded.
        zip_path=WORK/f'{job_id}_reels.zip'
        import zipfile
        with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z:
            for f in folder.iterdir():
                if f.is_file() and f.name != video.name and f.suffix.lower() in {'.mp4','.srt','.json','.txt'}:
                    z.write(f,arcname=f.name)
        update(status='completed',stage='Done',progress=100,message='Your reels are ready.',results=results,zip=f'/download/{job_id}/bundle.zip',finished_at=time.time())
    except Exception as e:
        update(status='failed',stage='Stopped',progress=100,message=str(e)[:1200],finished_at=time.time())
    finally:
        # Remove original source and model cache is shared; job outputs remain temporarily for downloads.
        try:
            for pattern in ('source.*', 'uploaded_source.*'):
                for f in folder.glob(pattern):
                    if f.is_file(): f.unlink()
        except Exception: pass

async def run_job(job_id: str, req: dict[str, Any]):
    async with job_lock:
        await asyncio.to_thread(do_job,job_id,req)

@app.get('/', response_class=HTMLResponse)
async def home():
    return FileResponse(BASE / 'index.html', media_type='text/html; charset=utf-8')

@app.get('/style.css')
def style_file():
    return FileResponse(BASE / 'style.css', media_type='text/css; charset=utf-8')

@app.get('/app.js')
def script_file():
    return FileResponse(BASE / 'app.js', media_type='application/javascript; charset=utf-8')

@app.get('/health')
def health():
    return {'status':'ok','service':'Malayalam Podcast Reel Studio','max_video_minutes':MAX_VIDEO_MINUTES,'max_clips':MAX_CLIPS,'max_upload_mb':MAX_UPLOAD_MB ,'youtube_downloader':'pytubefix'}

@app.post('/api/jobs')
async def create_job(body: CreateRequest, background: BackgroundTasks):
    url=valid_youtube_url(body.youtube_url)
    job_id=uuid.uuid4().hex[:12]
    req=body.model_dump(); req['youtube_url']=url
    jobs[job_id]={'id':job_id,'status':'queued','stage':'Queued','progress':0,'message':'Waiting for the processing worker…','created_at':time.time(),'results':[]}
    # BackgroundTasks runs after response but remains in this single process. One worker prevents memory spikes.
    background.add_task(run_job,job_id,req)
    return {'id':job_id,'status':'queued'}

@app.post('/api/jobs/upload')
async def create_upload_job(
    background: BackgroundTasks,
    video: UploadFile = File(...),
    clips: int = Form(5),
    clip_seconds: int = Form(45),
    language: str = Form('ml'),
    captions: bool = Form(True),
    smart_crop: bool = Form(True),
):
    """Process a source file directly; this is the reliable fallback when YouTube blocks cloud IPs."""
    allowed = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'}
    suffix = Path(video.filename or '').suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, 'Upload a video file in MP4, MOV, MKV, WEBM, M4V, or AVI format.')
    if not 1 <= clips <= MAX_CLIPS or not 30 <= clip_seconds <= 60 or language not in {'ml','auto'}:
        raise HTTPException(400, 'Invalid clip count, clip duration, or language setting.')
    job_id = uuid.uuid4().hex[:12]
    folder = WORK / job_id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f'uploaded_source{suffix}'
    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    try:
        with dest.open('wb') as out:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f'Video is larger than the {MAX_UPLOAD_MB} MB upload limit.')
                out.write(chunk)
    finally:
        await video.close()
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, 'The uploaded file is empty.')
    req = {'source_path': str(dest), 'clips': clips, 'clip_seconds': clip_seconds,
           'language': language, 'captions': captions, 'smart_crop': smart_crop,
           'max_download_mb': MAX_UPLOAD_MB}
    jobs[job_id] = {'id':job_id,'status':'queued','stage':'Queued','progress':0,
                    'message':'Waiting for the processing worker…','created_at':time.time(),'results':[]}
    background.add_task(run_job, job_id, req)
    return {'id':job_id,'status':'queued','source':'upload'}


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    j=jobs.get(job_id)
    if not j: raise HTTPException(404,'Job not found. Jobs are temporary and disappear after service restart or cleanup.')
    return JSONResponse({k:v for k,v in j.items() if k not in {'created_at','finished_at'}})

@app.get('/download/{job_id}/{filename}')
def download(job_id: str, filename: str):
    if not re.fullmatch(r'[a-f0-9]{12}',job_id): raise HTTPException(400,'Invalid job id.')
    folder=WORK/job_id
    if filename=='bundle.zip': path=WORK/f'{job_id}_reels.zip'
    else:
        if Path(filename).name != filename or filename.startswith('.'): raise HTTPException(400,'Invalid filename.')
        path=folder/filename
    if not path.exists() or not path.is_file(): raise HTTPException(404,'This download has expired or is unavailable.')
    media='application/zip' if path.suffix=='.zip' else ('video/mp4' if path.suffix=='.mp4' else 'text/plain; charset=utf-8')
    return FileResponse(path,media_type=media,filename=path.name)

async def cleanup_old_jobs_once():
    # Expire completed outputs after 2 hours; the filesystem is deliberately ephemeral.
    now=time.time()
    for p in WORK.iterdir():
        try:
            if p.is_dir() and now-p.stat().st_mtime > 7200: shutil.rmtree(p,ignore_errors=True)
            elif p.is_file() and now-p.stat().st_mtime > 7200: p.unlink(missing_ok=True)
        except Exception: pass

@app.on_event('startup')
async def cleanup_old_jobs():
    await cleanup_old_jobs_once()
    async def cleaner():
        while True:
            await asyncio.sleep(600)
            await cleanup_old_jobs_once()
    asyncio.create_task(cleaner())
