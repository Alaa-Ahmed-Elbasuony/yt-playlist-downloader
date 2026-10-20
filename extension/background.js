// Service worker: owns the native messaging port and the persisted job state.
// The popup only renders chrome.storage.local.job and sends commands here, so it
// can be closed and reopened at any time. An open native port keeps this worker alive.

const HOST = 'com.ytplaylist.downloader';
const ACTIVE = new Set(['starting', 'downloading', 'cancelling']);
const SAVE_INTERVAL_MS = 300;
const CANCEL_TIMEOUT_MS = 15000;

let port = null;
let job = { status: 'idle' };
let saveTimer = null;
let cancelTimer = null;

const ready = (async () => {
  const { job: stored } = await chrome.storage.local.get('job');
  if (stored) job = stored;
  // A fresh worker has no port. If the stored job still looks active, the host
  // went away with the previous worker (browser restart, extension reload, crash).
  if (ACTIVE.has(job.status)) {
    job = {
      ...job,
      status: 'error',
      error: 'The download was interrupted (browser or extension restarted). Start it again: finished videos are skipped.',
      finishedAt: Date.now(),
    };
    await persist();
  }
  updateActionIcon();
})();

function persist() {
  clearTimeout(saveTimer);
  saveTimer = null;
  updateActionIcon();
  return chrome.storage.local.set({ job });
}

// Progress events arrive several times a second; batch those writes.
function update(patch, { throttle = false } = {}) {
  job = { ...job, ...patch };
  updateActionIcon();
  if (!throttle) return persist();
  if (!saveTimer) saveTimer = setTimeout(persist, SAVE_INTERVAL_MS);
  return Promise.resolve();
}

// ------------------------------------------------------------- toolbar icon
// While downloading, the icon becomes a progress ring; afterwards a badge shows the result
// (✓ done, ! error) until the next download or "New download".

const DEFAULT_ICON = { 16: 'icons/icon16.png', 32: 'icons/icon32.png', 48: 'icons/icon48.png', 128: 'icons/icon128.png' };
const RING_COLOR = '#d93025';
const TRACK_COLOR = 'rgba(128, 128, 128, 0.45)';
let iconKey = null;

// Whole-job progress: finished/skipped videos plus the fraction of the current one.
function overallPercent(j = job) {
  const total = j.toDownload || j.total || 0;
  if (!total) return 0;
  const current = j.phase === 'finished' ? 0 : (j.percent || 0) / 100;
  return Math.min(100, ((j.completed || 0) + (j.failedCount || 0) + current) / total * 100);
}

function drawRing(size, percent) {
  const canvas = new OffscreenCanvas(size, size);
  const ctx = canvas.getContext('2d');
  const c = size / 2;
  const width = Math.max(2, size * 0.15);
  const radius = c - width / 2;
  ctx.lineWidth = width;
  ctx.strokeStyle = TRACK_COLOR;
  ctx.beginPath();
  ctx.arc(c, c, radius, 0, Math.PI * 2);
  ctx.stroke();
  ctx.strokeStyle = RING_COLOR;
  ctx.lineCap = 'round';
  ctx.beginPath();
  ctx.arc(c, c, radius, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.max(percent, 2) / 100);
  ctx.stroke();
  // small download arrow in the middle
  ctx.lineWidth = Math.max(1.5, size * 0.11);
  ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(c, size * 0.3);
  ctx.lineTo(c, size * 0.66);
  ctx.moveTo(size * 0.35, size * 0.5);
  ctx.lineTo(c, size * 0.68);
  ctx.lineTo(size * 0.65, size * 0.5);
  ctx.stroke();
  return ctx.getImageData(0, 0, size, size);
}

function updateActionIcon() {
  const active = ACTIVE.has(job.status);
  const percent = Math.floor(overallPercent());
  const key = active ? `ring:${percent}` : `${job.status}:${job.failedCount ? 'issues' : ''}`;
  if (key === iconKey) return;
  iconKey = key;

  if (active) {
    chrome.action.setIcon({ imageData: { 16: drawRing(16, percent), 32: drawRing(32, percent) } });
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setTitle({ title: `Downloading… ${percent}%` });
    return;
  }
  chrome.action.setIcon({ path: DEFAULT_ICON });
  const badge = {
    done: job.failedCount || job.errors?.length ? ['✓', '#9a6700', 'Download finished (some videos skipped)'] : ['✓', '#1a7f37', 'Download finished'],
    error: ['!', '#cf222e', 'Download failed'],
  }[job.status];
  chrome.action.setBadgeText({ text: badge ? badge[0] : '' });
  if (badge) {
    chrome.action.setBadgeBackgroundColor({ color: badge[1] });
    chrome.action.setBadgeTextColor?.({ color: '#ffffff' });
  }
  chrome.action.setTitle({ title: badge ? badge[2] : 'Download YouTube video or playlist' });
}

// ------------------------------------------------------------- native host

function explainNativeError(message = '') {
  if (/not found/i.test(message)) {
    return `${message} The native host is not registered: run install.cmd with this extension's ID (${chrome.runtime.id}). See README.`;
  }
  if (/forbidden/i.test(message)) {
    return `${message} The host manifest does not list this extension's ID (${chrome.runtime.id}). Re-run install.cmd with it.`;
  }
  if (/exited/i.test(message)) {
    return `${message} The host crashed or Python could not start. Check host\\yt_host.log.`;
  }
  return message || 'Native host error.';
}

function nativeRequest(payload) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, payload, (response) => {
      const err = chrome.runtime.lastError;
      if (err) resolve({ type: 'error', error: explainNativeError(err.message) });
      else resolve(response ?? { type: 'error', error: 'Empty response from native host.' });
    });
  });
}

async function startDownload({ url, mode, quality, audioOnly, audioQuality, folder, items = null }) {
  if (port || ACTIVE.has(job.status)) return { ok: false, error: 'A download is already running.' };

  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(onHostDisconnect);
  port.postMessage({ type: 'download', url, mode, quality, audioOnly, audioQuality, folder, items });

  job = {
    status: 'starting', message: 'Starting…', url, quality, audioOnly,
    playlistTitle: null, total: 0, toDownload: 0, playlistCount: 0, alreadyDone: 0, folder: null,
    position: null, index: null, title: null, percent: null, speed: null, eta: null, phase: null,
    completed: 0, failedCount: 0, failed: [], errors: [], error: null,
    startedAt: Date.now(), finishedAt: null,
  };
  await persist();
  return { ok: true };
}

async function cancelDownload() {
  if (!port) {
    if (ACTIVE.has(job.status)) await update({ status: 'cancelled', finishedAt: Date.now() });
    return { ok: true };
  }
  port.postMessage({ type: 'cancel' });
  await update({ status: 'cancelling' });
  // If the host doesn't confirm, drop the port; the browser then terminates it.
  clearTimeout(cancelTimer);
  cancelTimer = setTimeout(() => { if (port) finish({ status: 'cancelled' }); }, CANCEL_TIMEOUT_MS);
  return { ok: true };
}

function finish(patch) {
  clearTimeout(cancelTimer);
  const p = port;
  port = null;
  p?.disconnect();
  return update({ ...patch, phase: null, speed: null, eta: null, finishedAt: Date.now() });
}

function onHostMessage(msg) {
  switch (msg.type) {
    case 'status':
      update({ message: msg.message });
      break;
    case 'started':
      update({
        status: job.status === 'cancelling' ? 'cancelling' : 'downloading',
        message: null, playlistTitle: msg.title, total: msg.total, toDownload: msg.toDownload,
        playlistCount: msg.playlistCount, alreadyDone: msg.alreadyDone, folder: msg.folder,
      });
      break;
    case 'retry':
      update({ retryMessage: msg.message, percent: null, speed: null, eta: null });
      break;
    case 'item':
      update({
        position: msg.position, index: msg.index, title: msg.title,
        percent: 0, speed: null, eta: null, phase: 'downloading',
      });
      break;
    case 'progress':
      update({
        percent: msg.percent, speed: msg.speed ?? null, eta: msg.eta ?? null, phase: msg.phase, retryMessage: null,
      }, { throttle: true });
      break;
    case 'item_done':
      update({
        completed: msg.completed, percent: 100, phase: 'finished', speed: null, eta: null,
        lastPath: msg.path || job.lastPath || null,
      });
      break;
    case 'item_failed':
      update({ failedCount: msg.failedCount });
      break;
    case 'done':
      finish(msg.cancelled
        ? { status: 'cancelled' }
        : {
            status: 'done',
            completed: msg.downloaded,
            alreadyDone: msg.alreadyDone,
            failed: msg.failed || [],
            failedCount: msg.failedCount || 0,
            errors: msg.errors || [],
          });
      break;
    case 'error':
      finish({ status: 'error', error: msg.error });
      break;
  }
}

function onHostDisconnect() {
  const message = chrome.runtime.lastError?.message;
  port = null;
  clearTimeout(cancelTimer);
  if (job.status === 'cancelling') {
    update({ status: 'cancelled', finishedAt: Date.now() });
  } else if (ACTIVE.has(job.status)) {
    update({
      status: 'error',
      error: explainNativeError(message || 'The native host disconnected unexpectedly. Check host\\yt_host.log.'),
      finishedAt: Date.now(),
    });
  }
}

// The folder dialog takes focus, which closes the popup. So the worker runs the dialog,
// saves the chosen folder, and reopens the popup, which restores its draft from storage.session.
async function pickFolder({ current, draft }) {
  if (draft) await chrome.storage.session.set({ draft });
  const resp = await nativeRequest({ type: 'pick_folder', initial: current });
  if (resp?.type === 'folder' && resp.path) {
    const { settings = {} } = await chrome.storage.local.get('settings');
    await chrome.storage.local.set({ settings: { ...settings, folder: resp.path } });
  }
  try {
    await chrome.action.openPopup();
  } catch {
    // popup still open (dialog didn't take focus) or no focused window: the user reopens it
  }
  return resp;
}

async function handle(msg) {
  await ready;
  switch (msg?.cmd) {
    case 'info':
      return nativeRequest({ type: 'info', url: msg.url, mode: msg.mode });
    case 'pickFolder':
      return pickFolder(msg);
    case 'openPath':
      return nativeRequest({ type: 'open_path', path: msg.path, reveal: !!msg.reveal });
    case 'download':
      return startDownload(msg);
    case 'cancel':
      return cancelDownload();
    case 'clear':
      if (!ACTIVE.has(job.status)) {
        job = { status: 'idle' };
        await persist();
      }
      return { ok: true };
    default:
      return { ok: false, error: `Unknown command: ${msg?.cmd}` };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  handle(msg).then(sendResponse, (err) => sendResponse({ ok: false, type: 'error', error: String(err?.message || err) }));
  return true;
});
