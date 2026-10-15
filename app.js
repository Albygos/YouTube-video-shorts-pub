const $ = id => document.getElementById(id);
let currentJob = null, pollTimer = null, sourceMode = 'youtube';

$('paste').addEventListener('click', async () => {
  try { $('url').value = await navigator.clipboard.readText(); }
  catch { $('url').focus(); $('url').placeholder = 'Paste your YouTube URL here'; }
});
$('youtubeMode').addEventListener('click', () => setSourceMode('youtube'));
$('uploadMode').addEventListener('click', () => setSourceMode('upload'));
$('start').addEventListener('click', startJob);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') startJob(); });

function setSourceMode(mode) {
  sourceMode = mode;
  const upload = mode === 'upload';
  $('youtubeSource').classList.toggle('hidden', upload);
  $('uploadSource').classList.toggle('hidden', !upload);
  $('youtubeMode').classList.toggle('active', !upload);
  $('uploadMode').classList.toggle('active', upload);
  $('youtubeMode').setAttribute('aria-pressed', String(!upload));
  $('uploadMode').setAttribute('aria-pressed', String(upload));
}
function escapeHtml(s = '') {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function clock(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
function showError(msg) {
  $('error').textContent = msg;
  $('error').classList.remove('hidden');
  $('stage').textContent = 'Could not finish';
  $('statusText').textContent = 'Review the error below. If YouTube blocked the server, switch to Upload video.';
}
function resetButton() {
  $('start').disabled = false;
  $('start').innerHTML = '<span>✦</span> Find moments & create reels <b>→</b>';
}
function renderResults(job) {
  $('resultsCard').classList.remove('hidden');
  $('zip').href = job.zip || '#';
  $('results').innerHTML = (job.results || []).map(x => `
    <article class="result"><div class="result-top"><span class="number">${String(x.index).padStart(2,'0')}</span><div>
    <h3>${escapeHtml(x.title)}</h3><p>${escapeHtml(x.hook)}</p>
    <div class="result-meta">${clock(x.start)} – ${clock(x.end)} · ${x.size_mb} MB · relevance score ${x.score}</div>
    </div></div><div class="result-actions"><a href="${x.video}" download>↓ Download MP4</a><a href="${x.captions}" download>↓ Subtitles (.srt)</a></div></article>`).join('');
  $('resultsCard').scrollIntoView({behavior:'smooth', block:'start'});
}
async function startJob() {
  const url = $('url').value.trim();
  const file = $('videoFile').files[0];
  if (sourceMode === 'youtube' && !url) { $('url').focus(); $('url').style.borderColor = '#ff778d'; return; }
  if (sourceMode === 'upload' && !file) { $('videoFile').focus(); showError('Choose a video file first.'); return; }
  if (sourceMode === 'upload' && file.size > 90 * 1024 * 1024) {
    showError('This Render Free build limits browser uploads to 90 MB to avoid proxy request-size failures. Use a smaller/compressed source or a YouTube URL if it is accessible.');
    return;
  }
  const btn = $('start');
  btn.disabled = true;
  btn.innerHTML = '<span>◌</span> Starting analysis…';
  $('progressCard').classList.remove('hidden');
  $('resultsCard').classList.add('hidden');
  $('error').classList.add('hidden');
  $('clipProgress').textContent = '';
  $('bar').style.width = '0%'; $('pct').textContent = '0%';
  $('stage').textContent = 'Queueing job'; $('statusText').textContent = 'Submitting your video…';
  $('progressCard').scrollIntoView({behavior:'smooth', block:'center'});
  try {
    let response;
    if (sourceMode === 'upload') {
      const form = new FormData();
      form.append('video', file);
      form.append('clips', $('count').value);
      form.append('clip_seconds', $('length').value);
      form.append('language', $('language').value);
      form.append('captions', $('captions').checked);
      form.append('smart_crop', $('crop').checked);
      response = await fetch('/api/jobs/upload', {method:'POST', body:form});
    } else {
      response = await fetch('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({youtube_url:url, clips:Number($('count').value), clip_seconds:Number($('length').value), language:$('language').value, captions:$('captions').checked, smart_crop:$('crop').checked})});
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not start job.');
    currentJob = data.id;
    await poll();
  } catch (error) { showError(error.message || 'Could not start processing.'); resetButton(); }
}
async function poll() {
  if (!currentJob) return;
  try {
    const response = await fetch(`/api/jobs/${currentJob}`);
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || 'Job status unavailable');
    $('stage').textContent = job.stage || 'Working';
    $('pct').textContent = `${job.progress || 0}%`;
    $('bar').style.width = `${job.progress || 0}%`;
    $('statusText').textContent = job.message || '';
    if (job.clips?.length) $('clipProgress').textContent = `${job.clips.length} moments selected · rendering vertical videos`;
    if (job.status === 'completed') { clearTimeout(pollTimer); renderResults(job); resetButton(); return; }
    if (job.status === 'failed') { clearTimeout(pollTimer); showError(job.message || 'Processing failed.'); resetButton(); return; }
    pollTimer = setTimeout(poll, 2500);
  } catch (error) {
    $('statusText').textContent = 'Connection interrupted; retrying status check…';
    pollTimer = setTimeout(poll, 5000);
  }
}
