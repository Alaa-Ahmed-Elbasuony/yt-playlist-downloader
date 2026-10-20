#!/usr/bin/env python3
"""Native messaging host for the "YT Playlist Downloader" browser extension.

The browser talks to this script over stdin/stdout. Every message is a 4-byte
little-endian length prefix followed by UTF-8 JSON. stdout is reserved for that
protocol, so diagnostics go to yt_host.log next to this file.

Requests (extension -> host):
  {"type": "check"}
  {"type": "info", "url": "...", "mode": "video|playlist"}
  {"type": "download", "url": "...", "mode": "video|playlist", "quality": "360|720|1080|best",
   "audioOnly": bool, "audioQuality": "best|320|192|128", "folder": "...", "items": [1, 4, 5] | null}
   (items are 1-based playlist positions from the info entries; null = all)
  {"type": "cancel"}
  {"type": "sizes", "url": "...", "mode": "video|playlist"}   (streams "size" events)
  {"type": "cancel_sizes"}
  {"type": "pick_folder", "initial": "..."}   -> {"type": "folder", "path": "..." | null}
  {"type": "open_path", "path": "...", "reveal": bool}   -> {"type": "opened"}  (Explorer)

Events (host -> extension):
  check, info, status, started, item, progress, item_done, item_failed, done, error,
  size, sizes_done
"""

import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlencode, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "yt_host.log")

log = logging.getLogger("yt_host")
log.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(process)d] %(levelname)s %(message)s"))
log.addHandler(_log_handler)

IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
QUALITIES = {"360", "720", "1080", "best"}
AUDIO_QUALITIES = {"best": "0", "320": "320K", "192": "192K", "128": "128K"}
MODES = {None, "video", "playlist"}
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "YouTube")
ARCHIVE_VIDEO = ".yt-dlp-archive-video.txt"
ARCHIVE_AUDIO = ".yt-dlp-archive-mp3.txt"
INFO_TIMEOUT = 300
PROGRESS_INTERVAL = 0.25
MAX_FAILED_REPORTED = 200
MAX_ATTEMPTS = 3   # YouTube sometimes answers 403 to a stream URL; a fresh run usually works
RETRY_DELAY = 3    # seconds, grows with each attempt

TAG_PROGRESS = "[ytx-progress]"
TAG_POST = "[ytx-post]"
TAG_START = "[ytx-start]"
TAG_DONE = "[ytx-done]"
ERROR_RE = re.compile(r"^ERROR:\s*(?:\[[^\]]+\]\s*)?(?:([A-Za-z0-9_-]{11}):\s*)?(.*)$")


def _json_template(tag, fields):
    # "%(field|null)j" renders the field as JSON, or a literal null when it is
    # missing, so every tagged line yt-dlp prints is parseable JSON.
    body = ",".join('"%s":%%(%s|null)j' % (key, field) for key, field in fields)
    return tag + "{" + body + "}"


PROGRESS_TEMPLATE = "download:" + _json_template(TAG_PROGRESS, [
    ("status", "progress.status"),
    ("downloaded", "progress.downloaded_bytes"),
    ("total", "progress.total_bytes"),
    ("totalEstimate", "progress.total_bytes_estimate"),
    ("speed", "progress.speed"),
    ("eta", "progress.eta"),
    ("fragIndex", "progress.fragment_index"),
    ("fragCount", "progress.fragment_count"),
])
POSTPROCESS_TEMPLATE = "postprocess:" + _json_template(TAG_POST, [
    ("status", "progress.status"),
    ("postprocessor", "progress.postprocessor"),
])
START_PRINT = "before_dl:" + _json_template(TAG_START, [
    ("index", "playlist_index"),
    ("id", "id"),
    ("title", "title"),
])
DONE_PRINT = "after_move:" + _json_template(TAG_DONE, [
    ("index", "playlist_index"),
    ("id", "id"),
    ("title", "title"),
    ("path", "filepath"),
])


class UserError(Exception):
    """An error whose message is shown to the user in the popup."""


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- protocol

_stdin = sys.stdin.buffer
_stdout = sys.stdout.buffer
_send_lock = threading.Lock()


def _set_binary_mode():
    if IS_WINDOWS:
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def _read_exact(size):
    buf = b""
    while len(buf) < size:
        chunk = _stdin.read(size - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_message():
    header = _read_exact(4)
    if header is None:
        return None
    (length,) = struct.unpack("<I", header)
    body = _read_exact(length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def send(message):
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    try:
        with _send_lock:
            _stdout.write(struct.pack("<I", len(data)))
            _stdout.write(data)
            _stdout.flush()
    except OSError as exc:  # browser side closed the pipe
        log.warning("could not send %s message: %s", message.get("type"), exc)


# ---------------------------------------------------------------- validation

def validate_url(url, mode=None):
    """Return (canonical_url, has_playlist) or raise UserError.

    mode "video" downloads just the video even from a watch?v=…&list=… link;
    mode "playlist" requires a playlist ID; None decides from the URL.
    The URL handed to yt-dlp is rebuilt from validated IDs, so nothing from the
    original string other than the IDs ever reaches the subprocess.
    """
    if mode not in MODES:
        raise UserError(f"Unknown mode: {mode}")
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise UserError("Please enter a YouTube URL.")
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        raise UserError("That is not a YouTube URL.")

    query = parse_qs(parsed.query)
    list_id = (query.get("list") or [None])[0]
    path_parts = [p for p in parsed.path.split("/") if p]
    if host == "youtu.be":
        video_id = path_parts[0] if path_parts else None
    elif parsed.path == "/watch":
        video_id = (query.get("v") or [None])[0]
    elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "live"):
        video_id = path_parts[1]
    elif parsed.path == "/playlist":
        video_id = None
    else:
        raise UserError("That is not a YouTube video or playlist URL.")

    if video_id is not None and not VIDEO_ID_RE.match(video_id):
        raise UserError("The video ID in this URL is not valid.")
    if list_id is not None and not LIST_ID_RE.match(list_id):
        raise UserError("The playlist ID in this URL is not valid.")
    if not video_id and not list_id:
        raise UserError("That URL has no video or playlist ID.")
    if mode == "video":
        if not video_id:
            raise UserError('This is a playlist link. Choose "Playlist", or paste a video link.')
        return "https://www.youtube.com/watch?" + urlencode({"v": video_id}), False
    if mode == "playlist" and not list_id:
        raise UserError('This link is a single video, not a playlist. Choose "Video", or paste a playlist link.')

    if video_id and list_id:
        return "https://www.youtube.com/watch?" + urlencode({"v": video_id, "list": list_id}), True
    if list_id:
        return "https://www.youtube.com/playlist?" + urlencode({"list": list_id}), True
    return "https://www.youtube.com/watch?" + urlencode({"v": video_id}), False


def _existing_dir(folder):
    """The folder itself, or its nearest existing parent, or the home folder."""
    path = os.path.expandvars(os.path.expanduser((folder or "").strip() or DEFAULT_FOLDER))
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path if path and os.path.isdir(path) else os.path.expanduser("~")


def pick_folder(initial=None):
    """Show the system "choose folder" dialog; returns the path or None if cancelled."""
    start = _existing_dir(initial if isinstance(initial, str) else None)
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return _pick_folder_powershell(start) if IS_WINDOWS else None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # the browser is in front; don't open behind it
    root.update()
    try:
        path = filedialog.askdirectory(parent=root, initialdir=start, mustexist=False,
                                       title="Choose where to save downloads")
    finally:
        root.destroy()
    return os.path.normpath(path) if path else None


def _pick_folder_powershell(start):
    # Fallback for Python builds without Tk. The start folder goes through an
    # environment variable, never into the script text.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = 'Choose where to save downloads'; $d.ShowNewFolderButton = $true;"
        "$d.SelectedPath = $env:YTX_START;"
        "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true};"
        "if ($d.ShowDialog($owner) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $d.SelectedPath }"
    )
    env = dict(os.environ, YTX_START=start)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, creationflags=NO_WINDOW)
    path = result.stdout.decode("utf-8", "replace").strip()
    return os.path.normpath(path) if path else None


def open_in_explorer(path, reveal=False):
    """Open a folder, or open its folder with the file selected. Never runs the file itself."""
    if not isinstance(path, str) or not path.strip():
        raise UserError("No path to open.")
    path = os.path.normpath(path.strip())
    if not os.path.isabs(path):
        raise UserError("Can only open full paths.")
    if reveal and not os.path.isfile(path):
        if not os.path.isdir(os.path.dirname(path)):
            raise UserError(f"This file no longer exists: {path}")
        path, reveal = os.path.dirname(path), False  # file was moved/renamed: show its folder
    if not reveal and not os.path.isdir(path):
        raise UserError(f"This folder no longer exists: {path}")

    if IS_WINDOWS:
        explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
        # explorer.exe parses its own command line, so build it as a string. Windows paths
        # cannot contain double quotes, so quoting the path is enough.
        command = f'"{explorer}" /select,"{path}"' if reveal else f'"{explorer}" "{path}"'
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:
        target = os.path.dirname(path) if reveal else path
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    log.info("opened %s%s", path, " (selected)" if reveal else "")


def resolve_folder(folder):
    folder = (folder or "").strip() if isinstance(folder, str) else ""
    folder = os.path.expandvars(os.path.expanduser(folder or DEFAULT_FOLDER))
    if not os.path.isabs(folder):
        raise UserError("The download folder must be a full path, e.g. C:\\Users\\you\\Videos")
    folder = os.path.normpath(folder)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise UserError(f"Cannot create download folder {folder}: {exc.strerror}") from exc
    return folder


# ---------------------------------------------------------------- dependencies

def find_ytdlp():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def check_dependencies():
    ytdlp = find_ytdlp()
    ffmpeg = shutil.which("ffmpeg")
    problems = []
    if not ytdlp:
        problems.append(
            "yt-dlp was not found on PATH. Install it (winget install yt-dlp.yt-dlp, "
            "or: py -m pip install -U yt-dlp), then fully restart the browser so it sees the new PATH.")
    if not ffmpeg:
        problems.append(
            "ffmpeg was not found on PATH (needed to merge video+audio and make mp3). "
            "Install it (winget install Gyan.FFmpeg), then fully restart the browser.")
    return ytdlp, ffmpeg, problems


def require_dependencies():
    ytdlp, ffmpeg, problems = check_dependencies()
    if problems:
        raise UserError(" ".join(problems))
    return ytdlp, ffmpeg


def ytdlp_version(ytdlp):
    try:
        result = subprocess.run(ytdlp + ["--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=30, creationflags=NO_WINDOW)
        return result.stdout.decode("utf-8", "replace").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------- subprocesses

def _child_env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def spawn(args, **kwargs):
    # Always an args list, never shell=True. stdin must not be inherited: it is
    # the browser's message pipe.
    kwargs.setdefault("stdout", subprocess.PIPE)
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, env=_child_env(),
                            creationflags=NO_WINDOW, start_new_session=not IS_WINDOWS, **kwargs)


def kill_tree(proc):
    """Stop yt-dlp and its children (ffmpeg) without leaving orphans."""
    try:
        if IS_WINDOWS:
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


def fetch_info(ytdlp, url, spawn_fn=spawn):
    args = ytdlp + ["--flat-playlist", "-J", "--yes-playlist", "--encoding", "utf-8", "--no-warnings", "--", url]
    log.info("fetching info: %s", url)
    proc = spawn_fn(args, stderr=subprocess.PIPE)
    if proc is None:
        raise Cancelled()
    try:
        out, err = proc.communicate(timeout=INFO_TIMEOUT)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        raise UserError("Timed out while fetching playlist info.")

    if proc.returncode != 0 or not out.strip():
        errors = [l for l in err.decode("utf-8", "replace").splitlines() if l.startswith("ERROR:")]
        reason = errors[-1][len("ERROR:"):].strip() if errors else f"yt-dlp exited with code {proc.returncode}"
        log.warning("info failed: %s", err.decode("utf-8", "replace")[-2000:])
        raise UserError(f"Could not read this URL: {reason}")

    data = json.loads(out.decode("utf-8", "replace"))
    if data.get("_type") == "playlist":
        # "index" is the 1-based playlist position that --playlist-items refers to.
        entries = [{"index": i, "id": e.get("id"), "title": e.get("title"), "duration": e.get("duration")}
                   for i, e in enumerate(data.get("entries") or [], start=1) if e]
        return {
            "isPlaylist": True,
            "title": data.get("title") or "Untitled playlist",
            "uploader": data.get("uploader") or data.get("channel"),
            "count": len(entries) or data.get("playlist_count") or 0,
            "entries": entries,
        }
    return {
        "isPlaylist": False,
        "title": data.get("title") or data.get("id"),
        "uploader": data.get("uploader") or data.get("channel"),
        "count": 1,
        "entries": [{"index": 1, "id": data.get("id"), "title": data.get("title"), "duration": data.get("duration")}],
    }


def select_entries(items, info):
    """Validate the requested playlist positions; None means the whole playlist."""
    if items is None or not info["isPlaylist"]:
        return info["entries"], None
    if not isinstance(items, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in items):
        raise UserError("Invalid video selection.")
    wanted = set(items)
    selected = [e for e in info["entries"] if e["index"] in wanted]
    if not selected:
        raise UserError("No videos selected.")
    if len(selected) == len(info["entries"]):
        return selected, None
    return selected, compress_ranges([e["index"] for e in selected])


def compress_ranges(numbers):
    """[1, 2, 3, 7, 9, 10] -> "1-3,7,9-10" (keeps --playlist-items short)."""
    parts = []
    start = prev = None
    for n in sorted(set(numbers)):
        if prev is not None and n == prev + 1:
            prev = n
            continue
        if start is not None:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    if start is not None:
        parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def read_archive_ids(path):
    ids = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 2 and parts[0] == "youtube":
                    ids.add(parts[1])
    except FileNotFoundError:
        pass
    return ids


def format_selector(quality):
    if quality == "best":
        return "bv*+ba/b"
    return f"bv*[height<={quality}]+ba/b[height<={quality}]"


# ---------------------------------------------------------------- size estimates

VCODEC_RANK = (("av01", 4), ("vp09", 3), ("vp9", 3), ("hev1", 2), ("hvc1", 2), ("avc1", 1))


def _format_size(fmt, duration):
    size = _num(fmt.get("filesize")) or _num(fmt.get("filesize_approx"))
    if not size and _num(fmt.get("tbr")) and duration:
        size = fmt["tbr"] * 1000 / 8 * duration
    return size


def _vcodec_rank(fmt):
    codec = (fmt.get("vcodec") or "").lower()
    return next((rank for prefix, rank in VCODEC_RANK if codec.startswith(prefix)), 0)


def estimate_sizes(info):
    """Approximate download size for each video quality from a full (-j) info dict.

    Mirrors format_selector(): the best video stream at or below the height cap
    (ranked like yt-dlp's default sort: resolution, fps, codec, bitrate) plus the
    best audio stream, falling back to the best single file with both.
    """
    duration = _num(info.get("duration"))
    formats = [f for f in info.get("formats") or []
               if "drc" not in str(f.get("format_id", "")) and f.get("protocol") != "mhtml"]

    def has(kind, f):
        return f.get(kind) not in (None, "none")

    video_only = [f for f in formats if has("vcodec", f) and not has("acodec", f) and _num(f.get("height"))]
    audio_only = [f for f in formats if has("acodec", f) and not has("vcodec", f)]
    combined = [f for f in formats if has("vcodec", f) and has("acodec", f) and _num(f.get("height"))]
    audio = max(audio_only, key=lambda f: (_num(f.get("abr")) or _num(f.get("tbr")) or 0), default=None)

    def best(pool, cap):
        return max((f for f in pool if cap is None or f["height"] <= cap), default=None,
                   key=lambda f: (f["height"], _num(f.get("fps")) or 0, _vcodec_rank(f),
                                  f.get("protocol") == "https", _num(f.get("tbr")) or 0))

    sizes = {}
    for quality in sorted(QUALITIES):
        cap = None if quality == "best" else int(quality)
        video = best(video_only, cap)
        if video and audio:
            parts = [_format_size(video, duration), _format_size(audio, duration)]
            total = sum(parts) if all(parts) else None
        else:
            single = best(combined, cap)
            total = _format_size(single, duration) if single else None
        sizes[quality] = round(total) if total else None
    return sizes


def build_download_args(ytdlp, ffmpeg, url, folder, quality, audio_only, is_playlist, archive,
                        playlist_items=None, audio_quality="best"):
    if is_playlist:
        name = os.path.join("%(playlist_title)s", "%(playlist_index)03d - %(title)s.%(ext)s")
    else:
        name = "%(title)s [%(id)s].%(ext)s"
    # A literal % in the folder path would be read as a template field.
    output = os.path.join(folder.replace("%", "%%"), name)

    args = ytdlp + [
        "--newline",
        "--progress",
        "--no-simulate",
        "--encoding", "utf-8",
        "--ignore-errors",
        "--yes-playlist" if is_playlist else "--no-playlist",
        "--download-archive", archive,
        "--ffmpeg-location", ffmpeg,
        "--progress-template", PROGRESS_TEMPLATE,
        "--progress-template", POSTPROCESS_TEMPLATE,
        "--print", START_PRINT,
        "--print", DONE_PRINT,
        "-o", output,
    ]
    if playlist_items:
        args += ["--playlist-items", playlist_items]
    if audio_only:
        args += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", AUDIO_QUALITIES[audio_quality]]
    else:
        args += ["-f", format_selector(quality), "--merge-output-format", "mp4"]
    return args + ["--", url]


def _parse_tagged(line, tag):
    payload = line[line.index(tag) + len(tag):]
    try:
        return json.loads(payload)
    except ValueError:
        log.warning("unparseable %s line: %s", tag, line)
        return None


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _is_forbidden(text):
    return "HTTP Error 403" in text


def _explain_error(reason):
    """Add what to do next to the yt-dlp errors people actually hit."""
    if _is_forbidden(reason):
        return (f"{reason}. YouTube kept refusing the download after {MAX_ATTEMPTS} tries. "
                "Wait a few minutes and try again; if it keeps happening, update yt-dlp (yt-dlp -U).")
    if "Sign in to confirm" in reason:
        return f"{reason}. YouTube is rate-limiting this computer; wait a while and try again."
    return reason


def _percent(data):
    done = _num(data.get("downloaded"))
    total = _num(data.get("total")) or _num(data.get("totalEstimate"))
    if done is not None and total:
        return min(100.0, done * 100.0 / total)
    frag, frags = _num(data.get("fragIndex")), _num(data.get("fragCount"))
    if frag is not None and frags:
        return min(100.0, frag * 100.0 / frags)
    return None


# ---------------------------------------------------------------- download job

class BackgroundTask:
    """A cancellable yt-dlp run on a worker thread, so the message loop stays free."""

    busy_message = "Already running."
    cancelled_message = {}

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.cancelled = False
        self.thread = None

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, request):
        if self.running():
            raise UserError(self.busy_message)
        self.cancelled = False
        self.thread = threading.Thread(target=self._run_safe, args=(request,), daemon=True)
        self.thread.start()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            log.info("cancelling pid %s", proc.pid)
            kill_tree(proc)

    def _spawn(self, args, **kwargs):
        with self.lock:
            if self.cancelled:
                return None
            self.proc = spawn(args, **kwargs)
            return self.proc

    def _run_safe(self, request):
        try:
            self._run(request)
        except Cancelled:
            send(self.cancelled_message)
        except UserError as exc:
            send({"type": "error", "error": str(exc)})
        except Exception:
            log.exception("%s failed", type(self).__name__)
            send({"type": "error", "error": f"Unexpected host error, see {LOG_PATH}"})
        finally:
            with self.lock:
                self.proc = None

    def _run(self, request):
        raise NotImplementedError


class SizeScan(BackgroundTask):
    """Streams an estimated size per video (one full -j extraction per video)."""

    busy_message = "Sizes are already being calculated."
    cancelled_message = {"type": "sizes_done", "cancelled": True}

    def _run(self, request):
        url, has_playlist = validate_url(request.get("url"), request.get("mode"))
        ytdlp, _ = require_dependencies()
        args = ytdlp + ["-j", "--no-warnings", "--ignore-errors", "--encoding", "utf-8",
                        "--yes-playlist" if has_playlist else "--no-playlist", "--", url]
        log.info("scanning sizes: %s", url)
        proc = self._spawn(args, stderr=subprocess.DEVNULL)
        if proc is None:
            raise Cancelled()
        position = 0
        for raw in proc.stdout:
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            position += 1
            send({"type": "size", "index": _num(data.get("playlist_index")) or position, "id": data.get("id"),
                  "duration": _num(data.get("duration")), "sizes": estimate_sizes(data)})
        proc.wait()
        if self.cancelled:
            raise Cancelled()
        send({"type": "sizes_done", "cancelled": False})


class DownloadJob(BackgroundTask):
    busy_message = "A download is already running."
    cancelled_message = {"type": "done", "cancelled": True}

    def _run(self, request):
        url, has_playlist = validate_url(request.get("url"), request.get("mode"))
        quality = str(request.get("quality") or "1080")
        if quality not in QUALITIES:
            raise UserError(f"Unknown quality: {quality}")
        audio_quality = str(request.get("audioQuality") or "best")
        if audio_quality not in AUDIO_QUALITIES:
            raise UserError(f"Unknown audio quality: {audio_quality}")
        audio_only = bool(request.get("audioOnly"))
        folder = resolve_folder(request.get("folder"))
        ytdlp, ffmpeg = require_dependencies()

        send({"type": "status", "message": "Fetching playlist info…"})
        info = fetch_info(ytdlp, url, self._spawn)
        if self.cancelled:
            raise Cancelled()

        is_playlist = info["isPlaylist"] and has_playlist
        selected, playlist_items = select_entries(request.get("items"), info)
        archive = os.path.join(folder, ARCHIVE_AUDIO if audio_only else ARCHIVE_VIDEO)
        archived = read_archive_ids(archive)
        titles = {e["id"]: e.get("title") for e in info["entries"] if e.get("id")}
        already = sum(1 for e in selected if e.get("id") in archived)
        total = len(selected)
        send({"type": "started", "title": info["title"], "total": total, "toDownload": total - already,
              "playlistCount": info["count"], "alreadyDone": already, "folder": folder,
              "isPlaylist": is_playlist})

        args = build_download_args(ytdlp, ffmpeg, url, folder, quality, audio_only, is_playlist, archive,
                                   playlist_items, audio_quality)
        log.info("running: %s", args)

        completed = 0
        failed = {}
        general_errors = []
        code = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                # A fresh run gets fresh stream URLs; the archive skips videos that already finished.
                log.info("HTTP 403 from YouTube, retrying (attempt %s/%s)", attempt, MAX_ATTEMPTS)
                send({"type": "retry", "attempt": attempt, "maxAttempts": MAX_ATTEMPTS,
                      "message": f"YouTube refused the download (403). Retrying {attempt}/{MAX_ATTEMPTS}…"})
                self._sleep(RETRY_DELAY * (attempt - 1))
                # Forget the refused videos; if they fail again they are reported again.
                failed = {k: v for k, v in failed.items() if not _is_forbidden(v["reason"])}
                general_errors = []
            proc = self._spawn(args, stderr=subprocess.STDOUT)
            if proc is None:
                raise Cancelled()
            completed, forbidden = self._consume(proc, titles, completed, failed, general_errors)
            code = proc.wait()
            log.info("yt-dlp exited with %s (completed=%s failed=%s)", code, completed, len(failed))
            if self.cancelled:
                raise Cancelled()
            if not forbidden:
                break

        for item in failed.values():
            item["reason"] = _explain_error(item["reason"])
        send({"type": "done", "cancelled": False, "downloaded": completed,
              "failed": list(failed.values())[:MAX_FAILED_REPORTED], "failedCount": len(failed),
              "alreadyDone": already, "total": total,
              "errors": [_explain_error(e) for e in general_errors[-5:]],
              "exitCode": code, "folder": folder})

    def _sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.cancelled:
                raise Cancelled()
            time.sleep(0.2)

    def _consume(self, proc, titles, completed, failed, general_errors):
        """Turn yt-dlp's output into protocol events. Returns (completed, saw_http_403)."""
        last_progress = 0.0
        in_item = False
        forbidden = False
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            if TAG_PROGRESS in line:
                data = _parse_tagged(line, TAG_PROGRESS)
                if not data:
                    continue
                now = time.monotonic()
                finished = data.get("status") == "finished"
                if finished or now - last_progress >= PROGRESS_INTERVAL:
                    last_progress = now
                    send({"type": "progress", "phase": "downloading",
                          "percent": 100.0 if finished else _percent(data),
                          "speed": _num(data.get("speed")), "eta": _num(data.get("eta")),
                          "downloaded": _num(data.get("downloaded")),
                          "total": _num(data.get("total")) or _num(data.get("totalEstimate"))})
            elif TAG_POST in line:
                data = _parse_tagged(line, TAG_POST)
                # Playlist-level postprocessors run after the last item; only report per-video ones.
                if data and data.get("status") == "started" and in_item:
                    send({"type": "progress", "phase": "processing", "percent": 100.0,
                          "postprocessor": data.get("postprocessor")})
            elif TAG_START in line:
                data = _parse_tagged(line, TAG_START)
                in_item = True
                if data:
                    send({"type": "item", "position": completed + len(failed) + 1,
                          "index": _num(data.get("index")), "id": data.get("id"),
                          "title": data.get("title")})
            elif TAG_DONE in line:
                data = _parse_tagged(line, TAG_DONE)
                in_item = False
                completed += 1
                send({"type": "item_done", "completed": completed,
                      "index": _num((data or {}).get("index")), "path": (data or {}).get("path")})
            elif line.startswith("ERROR:"):
                log.warning("%s", line)
                forbidden = forbidden or _is_forbidden(line)
                match = ERROR_RE.match(line)
                video_id, reason = (match.group(1), match.group(2).strip()) if match else (None, line)
                if video_id:
                    in_item = False
                    failed[video_id] = {"id": video_id, "title": titles.get(video_id) or video_id,
                                        "reason": reason[:300]}
                    send({"type": "item_failed", "failedCount": len(failed), **failed[video_id]})
                else:
                    general_errors.append(reason[:300])
            elif line.startswith("WARNING:"):
                log.info("%s", line)
        return completed, forbidden


# ---------------------------------------------------------------- main loop

def handle(message, job, scan):
    kind = message.get("type") if isinstance(message, dict) else None
    try:
        if kind == "check":
            ytdlp, ffmpeg, problems = check_dependencies()
            send({"type": "check", "ok": not problems, "problems": problems,
                  "ytdlp": " ".join(ytdlp) if ytdlp else None,
                  "ytdlpVersion": ytdlp_version(ytdlp) if ytdlp else None,
                  "ffmpeg": ffmpeg, "python": sys.version.split()[0], "defaultFolder": DEFAULT_FOLDER})
        elif kind == "info":
            url, _ = validate_url(message.get("url"), message.get("mode"))
            ytdlp, _ = require_dependencies()
            info = fetch_info(ytdlp, url)
            entries = [{"index": e["index"], "id": e["id"], "title": (e.get("title") or e["id"] or "")[:200],
                        "duration": _num(e.get("duration"))} for e in info["entries"]]
            send({"type": "info", "title": info["title"], "count": info["count"],
                  "isPlaylist": info["isPlaylist"], "uploader": info["uploader"],
                  "duration": None if info["isPlaylist"] else entries[0]["duration"],
                  "entries": entries if info["isPlaylist"] else [], "defaultFolder": DEFAULT_FOLDER})
        elif kind == "download":
            job.start(message)
        elif kind == "cancel":
            job.cancel()
        elif kind == "sizes":
            scan.start(message)
        elif kind == "cancel_sizes":
            scan.cancel()
        elif kind == "pick_folder":
            send({"type": "folder", "path": pick_folder(message.get("initial"))})
        elif kind == "open_path":
            open_in_explorer(message.get("path"), bool(message.get("reveal")))
            send({"type": "opened", "ok": True})
        else:
            send({"type": "error", "error": f"Unknown request type: {kind!r}"})
    except UserError as exc:
        send({"type": "error", "error": str(exc)})


def main():
    _set_binary_mode()
    sys.stdout = sys.stderr  # a stray print() must never corrupt the protocol stream
    log.info("host started (python %s) args=%s", sys.version.split()[0], sys.argv[1:])
    job = DownloadJob()
    scan = SizeScan()
    try:
        while True:
            message = read_message()
            if message is None:
                break
            log.info("request: %s", message.get("type") if isinstance(message, dict) else message)
            handle(message, job, scan)
    except Exception:
        log.exception("fatal error in message loop")
    finally:
        for task in (job, scan):
            if task.running():
                log.info("browser disconnected, stopping %s", type(task).__name__)
                task.cancel()
                task.thread.join(timeout=15)
        log.info("host exiting")


if __name__ == "__main__":
    main()
