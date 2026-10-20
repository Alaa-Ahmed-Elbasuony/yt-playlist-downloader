// Popup: a view over chrome.storage.local.job plus the "new download" form.
// All real work happens in background.js; closing the popup never stops a download.

const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(['starting', 'downloading', 'cancelling']);
const FINISHED = new Set(['done', 'cancelled', 'error']);
const YT_HOSTS = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be']);

const QUALITY_OPTIONS = {
  video: [
    ['1080', '1080p (Full HD)'],
    ['720', '720p (HD)'],
    ['360', '360p (small file)'],
    ['best', 'Best available (up to 4K)'],
  ],
  audio: [
    ['best', 'Best quality'],
    ['320', '320 kbps'],
    ['192', '192 kbps'],
    ['128', '128 kbps (small file)'],
  ],
};
const DEFAULT_SETTINGS = { kind: 'video', videoQuality: '1080', audioQuality: 'best', folder: '' };
const HOST = 'com.ytplaylist.downloader';
// mp3 size is bitrate × length; "best" is VBR V0, which averages about 245 kbps.
const MP3_KBPS = { best: 245, 320: 320, 192: 192, 128: 128 };

const DOWNLOAD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
  + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M6.5 10 12 15.5 17.5 10M5 20h14"/></svg>';

let settings = { ...DEFAULT_SETTINGS };
let mode = 'video';       // what the user wants: one video or the playlist
let info = null;          // fetched info for the current link + mode
let infoSeq = 0;          // ignores responses for links that were since edited
let urlTimer = null;

// Size scan: a native port opened by the popup itself, so it stops when the popup closes.
let scanPort = null;
let scanState = 'idle';   // idle | running | done | failed
let sizes = new Map();    // playlist index (1 for a single video) -> { quality: bytes }

// { video, list } IDs of a YouTube link, or null. Mirrors validate_url() in host/yt_host.py.
function parseUrl(raw) {
  let u;
  try { u = new URL(raw.trim()); } catch { return null; }
  if (!/^https?:$/.test(u.protocol) || !YT_HOSTS.has(u.hostname.toLowerCase())) return null;
  const list = u.searchParams.get('list');
  const parts = u.pathname.split('/').filter(Boolean);
  let video = null;
  if (u.hostname === 'youtu.be') video = parts[0];
  else if (u.pathname === '/watch') video = u.searchParams.get('v');
  else if (['shorts', 'live'].includes(parts[0])) video = parts[1];
  else if (u.pathname !== '/playlist') return null;
  if (video && !/^[\w-]{11}$/.test(video)) return null;
  if (list && !/^[\w-]{2,64}$/.test(list)) return null;
  return video || list ? { video, list } : null;
}

const send = (cmd, extra = {}) => chrome.runtime.sendMessage({ cmd, ...extra });

// ------------------------------------------------------------- formatting

function fmtBytes(n) {
  if (n == null) return '';
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtTime(s) {
  if (s == null) return '';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

function fmtSize(n) {
  if (!n) return '';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1000 && i < units.length - 1);
  return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

// ------------------------------------------------------------- sizes

function startSizeScan() {
  stopSizeScan();
  if (!info) return;
  sizes = new Map();
  scanState = 'running';
  const port = chrome.runtime.connectNative(HOST);
  scanPort = port;
  port.onMessage.addListener((msg) => {
    if (port !== scanPort) return;
    if (msg.type === 'size') {
      sizes.set(info.isPlaylist ? msg.index : 1, msg.sizes || {});
    } else if (msg.type === 'sizes_done' || msg.type === 'error') {
      scanState = msg.type === 'error' ? 'failed' : 'done';
      stopSizeScan();
    }
    updateButtons();
  });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError; // reported by the info request already
    if (port !== scanPort) return;
    scanPort = null;
    if (scanState === 'running') scanState = 'failed';
    updateButtons();
  });
  port.postMessage({ type: 'sizes', url: info.url, mode: info.mode });
}

function stopSizeScan() {
  const port = scanPort;
  scanPort = null;
  port?.disconnect();
}

// Estimated bytes for one video at the given format; null if not known (yet).
function sizeOf(index, duration, kind = settings.kind, quality = currentQuality(kind)) {
  if (kind === 'audio') return duration ? duration * MP3_KBPS[quality] * 125 : null;
  return sizes.get(index)?.[quality] ?? null;
}

function currentQuality(kind = settings.kind) {
  return kind === 'audio' ? settings.audioQuality : settings.videoQuality;
}

// { total, known, count } across the whole playlist (or the single video).
function totalSize(kind = settings.kind, quality = currentQuality(kind)) {
  const items = info.isPlaylist ? info.entries.map((e) => [e.index, e.duration]) : [[1, info.duration]];
  let total = 0, known = 0;
  for (const [index, duration] of items) {
    const s = sizeOf(index, duration, kind, quality);
    if (s) { total += s; known++; }
  }
  return { total, known, count: items.length };
}

function sizeText(index, duration) {
  const s = sizeOf(index, duration);
  if (s) return `≈ ${fmtSize(s)}`;
  return settings.kind === 'video' && scanState === 'running' ? 'size…' : '';
}

// ------------------------------------------------------------- segmented controls

function setSeg(id, value) {
  for (const b of $(id).querySelectorAll('button')) b.classList.toggle('on', b.dataset.value === value);
}

function onSeg(id, handler) {
  $(id).addEventListener('click', (e) => {
    const b = e.target.closest('button[data-value]');
    if (b) handler(b.dataset.value);
  });
}

// ------------------------------------------------------------- settings

function loadSettings(stored = {}) {
  settings = { ...DEFAULT_SETTINGS, ...stored };
  // Older versions stored { format } or { quality, audioOnly }.
  if (stored.format === 'audio' || stored.audioOnly) settings.kind = 'audio';
  else if (stored.format) settings.videoQuality = stored.format;
  else if (stored.quality && !stored.videoQuality) settings.videoQuality = stored.quality;
  if (!QUALITY_OPTIONS[settings.kind]) settings.kind = 'video';
}

function saveSettings() {
  const { kind, videoQuality, audioQuality, folder } = settings;
  chrome.storage.local.set({ settings: { kind, videoQuality, audioQuality, folder } });
}

function renderFormat() {
  setSeg('kind', settings.kind);
  const key = settings.kind === 'audio' ? 'audioQuality' : 'videoQuality';
  const options = QUALITY_OPTIONS[settings.kind];
  if (!options.some(([v]) => v === settings[key])) settings[key] = options[0][0];
  $('quality').replaceChildren(...options.map(([value, label]) => {
    const option = new Option(label, value);
    option.dataset.label = label; // size gets appended by updateButtons()
    return option;
  }));
  $('quality').value = settings[key];
  updateButtons();
}

// ------------------------------------------------------------- mode + info

function setMode(next) {
  mode = next;
  setSeg('mode', mode);
  $('url-label').textContent = mode === 'playlist' ? 'Paste the playlist link' : 'Paste the video link';
}

function showInfo(title, meta = '', kind = '') {
  const box = $('info');
  box.hidden = !title && !meta;
  box.className = `info ${kind}`;
  $('info-title').textContent = title;
  $('info-meta').textContent = meta;
}

function resetInfo() {
  stopSizeScan();
  scanState = 'idle';
  sizes = new Map();
  info = null;
  $('playlist').hidden = true;
  $('download').hidden = mode === 'playlist';
  $('video-list').replaceChildren();
  $('filter').value = '';
  updateButtons();
}

async function fetchInfo() {
  const url = $('url').value.trim();
  const seq = ++infoSeq;
  resetInfo();
  if (!url) return showInfo('');
  const ids = parseUrl(url);
  if (!ids) return showInfo('This is not a YouTube video or playlist link.', '', 'error');
  if (mode === 'playlist' && !ids.list) {
    return showInfo('This link is a single video, not a playlist.', 'Choose "Video" in step 1, or paste a playlist link.', 'error');
  }
  if (mode === 'video' && !ids.video) {
    return showInfo('This is a playlist link.', 'Choose "Playlist" in step 1, or paste a video link.', 'error');
  }

  showInfo(mode === 'playlist' ? 'Loading playlist…' : 'Loading video…',
    mode === 'playlist' ? 'Big playlists can take a few seconds.' : '');
  const resp = await send('info', { url, mode });
  if (seq !== infoSeq) return;
  if (resp?.type !== 'info') return showInfo(resp?.error || 'Could not load this link.', '', 'error');

  info = { url, mode, currentVideo: ids.video, ...resp };
  showLoadedInfo();
  startSizeScan();
  updateButtons();
}

function showLoadedInfo() {
  if (info.defaultFolder) $('folder').placeholder = `Default: ${info.defaultFolder}`;
  info.meta = info.isPlaylist
    ? [plural(info.count, 'video'), info.uploader].filter(Boolean).join(' · ')
    : [info.uploader, info.duration ? fmtTime(info.duration) : ''].filter(Boolean).join(' · ');
  showInfo(info.title, info.meta);
  if (info.isPlaylist) renderVideoList();
}

// ------------------------------------------------------------- folder picker

const DRAFT_MAX_AGE_MS = 5 * 60 * 1000;

// Opening the system dialog closes the popup, so hand the current state to the
// worker; it reopens the popup afterwards and restoreDraft() picks it back up.
async function browseFolder() {
  $('browse').disabled = true;
  const draft = { url: $('url').value.trim(), mode, info, sizes: [...sizes], scanState, savedAt: Date.now() };
  const resp = await send('pickFolder', { current: $('folder').value.trim() || settings.folder, draft });
  // Only reached when the popup stayed open during the dialog.
  $('browse').disabled = false;
  chrome.storage.session.remove('draft');
  if (resp?.type === 'folder') {
    if (resp.path) {
      settings.folder = resp.path;
      $('folder').value = resp.path;
    }
  } else if (/unknown command/i.test(resp?.error || '')) {
    reloadStaleExtension();
  } else {
    showInfo(resp?.error || 'Could not open the folder picker.', '', 'error');
  }
}

// Popup files load fresh from disk, but the background worker keeps running old code
// until the extension is reloaded. The form is only visible when no download is running,
// so reloading here can't interrupt anything.
function reloadStaleExtension() {
  showInfo('Updating the extension…', 'The popup will close. Click the extension icon again in a moment.');
  setTimeout(() => chrome.runtime.reload(), 1500);
}

function restoreDraft(draft) {
  $('url').value = draft.url || '';
  setMode(draft.mode || 'video');
  if (!draft.info) return fetchInfo();
  info = draft.info;
  showLoadedInfo();
  if (draft.scanState === 'done') {
    sizes = new Map(draft.sizes);
    scanState = 'done';
  } else {
    startSizeScan();
  }
  updateButtons();
}

function renderVideoList() {
  const entries = info.entries || [];
  const rows = entries.map((e) => {
    const li = document.createElement('li');
    li.dataset.search = (e.title || '').toLowerCase();
    li.dataset.index = e.index;
    li.dataset.duration = e.duration ?? '';
    if (e.id && e.id === info.currentVideo) li.classList.add('current');

    const idx = Object.assign(document.createElement('span'), { className: 'idx', textContent: e.index });
    const name = document.createElement('span');
    name.className = 'name';
    const title = Object.assign(document.createElement('div'), { textContent: e.title || e.id, title: e.title || e.id });
    name.append(title, document.createElement('small')); // length · size, filled by updateButtons()

    const get = document.createElement('button');
    get.type = 'button';
    get.className = 'get';
    get.title = 'Download only this video';
    get.innerHTML = DOWNLOAD_ICON; // static markup, no user data
    get.addEventListener('click', () => startDownload([e.index]));

    li.append(idx, name, get);
    return li;
  });
  if (rows.length === 0) {
    rows.push(Object.assign(document.createElement('li'), { className: 'empty', textContent: 'This playlist has no available videos.' }));
  }
  $('video-list').replaceChildren(...rows);
  $('playlist').hidden = false;
  $('download').hidden = true;
  // Opened from a video inside the playlist: show that row.
  $('video-list').querySelector('li.current')?.scrollIntoView({ block: 'center' });
}

function applyFilter() {
  const q = $('filter').value.trim().toLowerCase();
  for (const li of $('video-list').children) {
    if (li.dataset.search !== undefined) li.hidden = q !== '' && !li.dataset.search.includes(q);
  }
}

// Refreshes everything that depends on the link, the format and the size scan.
function updateButtons() {
  const what = settings.kind === 'audio' ? 'mp3' : 'video';
  const label = settings.kind === 'audio' ? 'Download mp3' : 'Download video';
  $('download').disabled = !info || info.isPlaylist;
  $('download').textContent = label;
  $('download-all').disabled = true;
  $('download-all').textContent = 'Download all';
  $('total-size').textContent = '';
  for (const option of $('quality').options) option.textContent = option.dataset.label;
  if (!info) return;

  // Size next to each quality option (whole playlist for playlists).
  for (const option of $('quality').options) {
    const { total, known, count } = totalSize(settings.kind, option.value);
    if (known) option.textContent = `${option.dataset.label}  ·  ≈ ${fmtSize(total)}${known < count ? '+' : ''}`;
  }

  const { total, known, count } = totalSize();
  if (!info.isPlaylist) {
    const size = sizeText(1, info.duration);
    $('download').textContent = size ? `${label}  (${size})` : label;
    showInfo(info.title, [info.meta, size].filter(Boolean).join(' · '));
    return;
  }

  $('download-all').disabled = count === 0;
  $('download-all').textContent = `Download all ${plural(count, what)}`;
  if (known === count && count) {
    $('total-size').textContent = `Total ≈ ${fmtSize(total)}`;
  } else if (scanState === 'running' && settings.kind === 'video') {
    $('total-size').textContent = `Calculating sizes… ${known} of ${count}` + (known ? ` (≈ ${fmtSize(total)} so far)` : '');
  } else if (known) {
    $('total-size').textContent = `Total ≈ ${fmtSize(total)} (${count - known} unknown)`;
  }

  for (const li of $('video-list').children) {
    if (li.dataset.index === undefined) continue;
    const duration = Number(li.dataset.duration) || null;
    li.querySelector('small').textContent =
      [duration ? fmtTime(duration) : '', sizeText(Number(li.dataset.index), duration)].filter(Boolean).join('  ·  ');
  }
}

// items: playlist positions to download, or null for everything.
async function startDownload(items = null) {
  if (!info) return;
  const audioOnly = settings.kind === 'audio';
  stopSizeScan();
  for (const b of document.querySelectorAll('#download, #download-all, .get')) b.disabled = true;
  const resp = await send('download', {
    url: info.url,
    mode: info.mode,
    quality: settings.videoQuality,
    audioOnly,
    audioQuality: settings.audioQuality,
    folder: settings.folder,
    items,
  });
  if (!resp?.ok) {
    for (const b of document.querySelectorAll('.get')) b.disabled = false;
    updateButtons();
    showInfo(resp?.error || 'Could not start the download.', '', 'error');
  }
}

// ------------------------------------------------------------- job view

function setBadge(text, kind = '') {
  const b = $('badge');
  b.hidden = !text;
  b.textContent = text;
  b.className = `badge ${kind}`;
}

function render(job) {
  lastJob = job;
  const active = ACTIVE.has(job.status);
  const finished = FINISHED.has(job.status);
  $('form').hidden = active || finished;
  $('job').hidden = !(active || finished);

  if (!active && !finished) { setBadge(''); return; }

  $('job-title').textContent = job.playlistTitle || 'Preparing download…';
  $('cancel').hidden = !active;
  $('cancel').disabled = job.status === 'cancelling';
  $('again').hidden = active;
  $('result').hidden = active;
  $('progress').hidden = finished;
  $('open-row').hidden = true; // renderFinished shows it when there is something to open
  $('job-video').hidden = finished;

  if (active) renderActive(job);
  else renderFinished(job);
}

function renderActive(job) {
  const bar = $('bar');
  const stats = [];
  let line = '';

  if (job.status === 'starting') {
    setBadge('Starting');
    line = job.message || 'Starting…';
  } else if (job.status === 'cancelling') {
    setBadge('Cancelling', 'warn');
    line = 'Stopping yt-dlp…';
  } else {
    setBadge('Downloading', 'ok');
    const parts = [];
    if (job.position && (job.toDownload ?? job.total) > 1) {
      let pos = `Video ${job.position} of ${job.toDownload ?? job.total}`;
      if (job.index && job.playlistCount > 1) pos += ` (#${job.index} in playlist)`;
      parts.push(pos);
    }
    if ((job.total ?? 0) > 1) parts.push(`${job.completed} downloaded`);
    if (job.alreadyDone) parts.push(`${job.alreadyDone} already had`);
    if (job.failedCount) parts.push(`${job.failedCount} skipped`);
    line = parts.join(' · ');
  }
  if (job.retryMessage && job.status === 'downloading') line = [line, job.retryMessage].filter(Boolean).join(' · ');
  $('job-line').textContent = line;
  $('job-video').textContent = job.title && job.title !== job.playlistTitle ? job.title : '';

  // Current video: big percentage + bar. Unknown progress (starting) animates instead.
  const downloading = job.status === 'downloading';
  const processing = downloading && job.phase === 'processing';
  const percent = processing || job.phase === 'finished' ? 100 : job.percent;
  const known = downloading && percent != null;
  bar.classList.toggle('indeterminate', !known);
  $('bar-fill').style.width = known ? `${percent.toFixed(1)}%` : '';
  $('pct').textContent = known ? `${Math.floor(percent)}%` : '';

  if (processing) stats.push(job.audioOnly ? 'Converting to mp3…' : 'Merging video + audio…');
  else if (known) {
    if (job.speed) stats.push(`${fmtBytes(job.speed)}/s`);
    if (job.eta != null) stats.push(`${fmtTime(job.eta)} left`);
  }
  $('job-stats').textContent = stats.join(' · ') || ' ';

  // Whole playlist: only worth a second bar when more than one video is downloading.
  const total = job.toDownload || job.total || 0;
  $('overall').hidden = !(downloading && total > 1);
  if (total > 1) {
    const overall = overallPercent(job);
    $('overall-fill').style.width = `${overall.toFixed(1)}%`;
    $('overall-pct').textContent = `${Math.floor(overall)}%`;
  }
}

// Same formula as background.js (drives the toolbar ring).
function overallPercent(job) {
  const total = job.toDownload || job.total || 0;
  if (!total) return 0;
  const current = job.phase === 'finished' ? 0 : (job.percent || 0) / 100;
  return Math.min(100, ((job.completed || 0) + (job.failedCount || 0) + current) / total * 100);
}

function renderFinished(job) {
  $('job-line').textContent = '';
  const summary = $('result-summary');
  const skipped = job.failed || [];
  const errors = job.errors || [];

  if (job.status === 'done') {
    const parts = [`Downloaded ${plural(job.completed, job.audioOnly ? 'mp3' : 'video')}`];
    if (job.alreadyDone) parts.push(`${job.alreadyDone} already downloaded`);
    if (job.failedCount) parts.push(`${job.failedCount} skipped`);
    summary.textContent = parts.join(' · ') + '.';
    setBadge(job.failedCount || errors.length ? 'Done with issues' : 'Done', job.failedCount || errors.length ? 'warn' : 'ok');
  } else if (job.status === 'cancelled') {
    summary.textContent = 'Cancelled. Partial files resume next time; finished videos are skipped.';
    setBadge('Cancelled', 'warn');
  } else {
    summary.textContent = job.error || 'Something went wrong.';
    setBadge('Error', 'error');
  }
  summary.className = job.status === 'error' ? 'errors' : '';

  $('skipped').hidden = skipped.length === 0;
  $('skipped-summary').textContent = `${plural(job.failedCount || skipped.length, 'video')} skipped (unavailable/private)`;
  $('skipped-list').replaceChildren(...skipped.map((v) => {
    const li = document.createElement('li');
    li.textContent = v.title || v.id;
    const reason = document.createElement('small');
    reason.textContent = v.reason;
    li.append(reason);
    return li;
  }));

  $('errors').hidden = errors.length === 0;
  $('errors').replaceChildren(...errors.map((e) => {
    const li = document.createElement('li');
    li.textContent = e;
    return li;
  }));

  // Files land in <folder>\<playlist title>\ for playlists, so open the folder of the last file.
  const target = openTarget(job);
  $('result-folder').textContent = target ? `Saved to ${target}` : '';
  $('open-row').hidden = job.status === 'error' || !target;
  // "Show file" only makes sense when exactly one file came out of it.
  $('open-file').hidden = !(job.lastPath && job.completed === 1);
}

function openTarget(job) {
  return job.lastPath ? dirnameOf(job.lastPath) : job.folder || null;
}

function dirnameOf(path) {
  return path.replace(/[\\/][^\\/]*$/, '');
}

let lastJob = null;

async function openPath(reveal) {
  const path = reveal ? lastJob?.lastPath : openTarget(lastJob || {});
  if (!path) return;
  const resp = await send('openPath', { path, reveal });
  if (resp?.type === 'error') {
    $('result-folder').textContent = resp.error;
  } else if (/unknown command/i.test(resp?.error || '')) {
    $('result-folder').textContent = 'Updating the extension… click the icon again in a moment.';
    setTimeout(() => chrome.runtime.reload(), 1500);
  }
}

// ------------------------------------------------------------- init

function useUrl(url) {
  const ids = parseUrl(url);
  if (ids) setMode(ids.list ? 'playlist' : 'video'); // pick the step-1 option that fits the link
  fetchInfo();
}

async function init() {
  const { settings: stored, job } = await chrome.storage.local.get(['settings', 'job']);
  const { draft } = await chrome.storage.session.get('draft');
  if (draft) chrome.storage.session.remove('draft');
  loadSettings(stored);
  $('folder').value = settings.folder;
  setMode('video');
  renderFormat();
  resetInfo();
  render(job || { status: 'idle' });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.job) render(changes.job.newValue || { status: 'idle' });
  });

  onSeg('mode', (value) => { setMode(value); fetchInfo(); });
  onSeg('kind', (value) => { settings.kind = value; renderFormat(); saveSettings(); });
  $('quality').addEventListener('change', () => {
    settings[settings.kind === 'audio' ? 'audioQuality' : 'videoQuality'] = $('quality').value;
    saveSettings();
    updateButtons();
  });
  $('folder').addEventListener('change', () => { settings.folder = $('folder').value.trim(); saveSettings(); });
  $('browse').addEventListener('click', browseFolder);
  $('url').addEventListener('input', () => {
    clearTimeout(urlTimer);
    urlTimer = setTimeout(() => useUrl($('url').value), 600);
  });
  $('filter').addEventListener('input', applyFilter);
  $('download').addEventListener('click', () => startDownload(null));
  $('download-all').addEventListener('click', () => startDownload(null));
  $('cancel').addEventListener('click', () => send('cancel'));
  $('open-folder').addEventListener('click', () => openPath(false));
  $('open-file').addEventListener('click', () => openPath(true));
  $('again').addEventListener('click', async () => {
    await send('clear');
    useUrl($('url').value);
  });

  const idle = !ACTIVE.has(job?.status) && !FINISHED.has(job?.status);
  if (idle && draft && Date.now() - draft.savedAt < DRAFT_MAX_AGE_MS) return restoreDraft(draft);

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.url && parseUrl(tab.url)) {
    $('url').value = tab.url;
    if (idle) useUrl(tab.url);
  }
}

init();
