"""Local YouTube downloader.

Paste a video link and it is saved into Downloads.
Paste a playlist link and every video is saved into a folder named after the playlist.
Paste a channel link and the channel is saved into its own folder: every upload in
upload order, plus a folder for each playlist.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, render_template, request

import yt_dlp

ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
LOCAL_BIN = ROOT / "bin"

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CHANNEL_TABS = {
    "videos",
    "shorts",
    "streams",
    "playlists",
    "featured",
    "about",
    "community",
    "podcasts",
    "channels",
    "posts",
}
# YouTube generates these. They repeat the videos, shorts, or mixes tabs.
AUTO_PLAYLIST_PREFIXES = ("UU", "RD", "LL", "WL", "FL")


class _QuietLog:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if not nvm.is_dir():
        return None
    bins = sorted(path for path in nvm.glob("v*/bin/node") if path.is_file())
    return str(bins[-1]) if bins else None


_cookie_browser: str | None = None
_cookie_browser_checked = False


def cookie_browser() -> str | None:
    """A local browser whose YouTube cookies yt-dlp can read."""
    global _cookie_browser, _cookie_browser_checked
    if _cookie_browser_checked:
        return _cookie_browser
    _cookie_browser_checked = True
    for name in ("firefox", "chrome", "brave", "chromium"):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "cookiesfrombrowser": (name,)}) as ydl:
                if any(True for _cookie in ydl.cookiejar):
                    _cookie_browser = name
                    return _cookie_browser
        except Exception:
            continue
    return None


def youtube_access() -> dict:
    """Options that get past YouTube's bot check and unlock real formats."""
    options: dict = {"remote_components": ["ejs:github"]}
    node = find_node()
    if node:
        options["js_runtimes"] = {"node": {"path": node}}
    browser = cookie_browser()
    if browser:
        options["cookiesfrombrowser"] = (browser,)
    return options


FLAT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
    "ignoreerrors": True,
    "logger": _QuietLog(),
    "extractor_args": {"youtubetab": {"approximate_date": ["yes"]}},
}

app = Flask(__name__)


class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.status = "idle"
        self.kind = None
        self.title = ""
        self.folder = str(DOWNLOADS)
        self.message = ""
        self.detail = ""
        self.current = 0
        self.total = 0
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.error = ""
        self.items: list[dict] = []

    def snapshot(self) -> dict:
        ffmpeg_ready = find_ffmpeg() is not None
        with self.lock:
            return {
                "running": self.running,
                "status": self.status,
                "kind": self.kind,
                "title": self.title,
                "folder": self.folder,
                "folder_display": display_path(self.folder),
                "message": self.message,
                "detail": self.detail,
                "current": self.current,
                "total": self.total,
                "percent": self.percent,
                "speed": self.speed,
                "eta": self.eta,
                "error": self.error,
                "items": [dict(item) for item in self.items],
                "ffmpeg": ffmpeg_ready,
            }


job = Job()


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def safe_name(name: str, fallback: str) -> str:
    cleaned = INVALID_CHARS.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    return (cleaned[:150] or fallback)


def is_youtube(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.lower().split(":")[0]
    return host in YOUTUBE_HOSTS


def channel_root(url: str) -> str | None:
    """Return the channel base URL, or None when this is not a channel link."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if host not in YOUTUBE_HOSTS or "youtu.be" in host:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if parts[0].startswith("@"):
        if len(parts) == 1 or (len(parts) == 2 and parts[1] in CHANNEL_TABS):
            kept = parts[:1]
        else:
            return None
    elif parts[0] in {"channel", "c", "user"} and len(parts) >= 2:
        if len(parts) == 2 or (len(parts) == 3 and parts[2] in CHANNEL_TABS):
            kept = parts[:2]
        else:
            return None
    else:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(kept)}"


def is_playlist_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    if "youtu.be" in host:
        return False
    if path == "/playlist" or path.startswith("/playlist/"):
        return True
    if path.startswith(("/watch", "/shorts", "/live", "/embed", "/v")):
        return False
    return "list" in query and "v" not in query


_ffmpeg_location: str | None = None
_ffmpeg_checked = False


def _ffmpeg_dir(directory: Path) -> str | None:
    ffmpeg = directory / "ffmpeg"
    ffprobe = directory / "ffprobe"
    if os_access(ffmpeg) and os_access(ffprobe):
        return str(directory)
    return None


def find_ffmpeg() -> str | None:
    """Return a directory with both ffmpeg and ffprobe, which merging needs."""
    global _ffmpeg_location, _ffmpeg_checked
    if _ffmpeg_checked:
        return _ffmpeg_location
    _ffmpeg_checked = True
    local = _ffmpeg_dir(LOCAL_BIN)
    if local:
        _ffmpeg_location = local
        return _ffmpeg_location
    system_ffmpeg = shutil.which("ffmpeg")
    system_probe = shutil.which("ffprobe")
    if system_ffmpeg and system_probe:
        _ffmpeg_location = str(Path(system_ffmpeg).resolve().parent)
        return _ffmpeg_location
    return None


def os_access(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o111


def format_speed(speed) -> str:
    if not speed:
        return ""
    speed = float(speed)
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.0f} KB/s"
    return f"{speed:.0f} B/s"


def format_eta(eta) -> str:
    if eta is None:
        return ""
    total = max(0, int(eta))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def friendly_error(exc: BaseException) -> str:
    text = str(exc).strip() or "Download failed."
    lower = text.lower()
    if "private video" in lower:
        return "This video is private."
    if "video unavailable" in lower or "has been removed" in lower or "account associated" in lower:
        return "This video is unavailable."
    if "sign in to confirm" in lower or "not a bot" in lower:
        return "YouTube blocked this video. Sign in to YouTube in Firefox, then press Download again."
    if "confirm your age" in lower or "age-restricted" in lower or "age restricted" in lower:
        return "This video is age-restricted and can’t be downloaded from here."
    if "unsupported url" in lower:
        return "That link doesn’t look like a YouTube video, playlist, or channel."
    if "ffmpeg" in lower and "not found" in lower:
        return "ffmpeg is missing, so the best quality can’t be saved."
    line = text.splitlines()[0]
    return line[:280]


def download_detail(info: dict) -> str:
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    if vcodec in (None, "none") and acodec not in (None, "none"):
        return "Downloading audio…"
    if acodec in (None, "none") and vcodec not in (None, "none"):
        return "Downloading video…"
    return "Downloading…"


def make_hook(index: int):
    def hook(progress: dict) -> None:
        status = progress.get("status")
        if status != "downloading":
            return
        total = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
        done = progress.get("downloaded_bytes") or 0
        if total:
            percent = done / total * 100
        elif progress.get("fragment_count"):
            percent = (progress.get("fragment_index") or 0) / progress["fragment_count"] * 100
        else:
            percent = 0
        info = progress.get("info_dict") or {}
        with job.lock:
            job.current = index
            job.percent = round(min(percent, 100), 1)
            job.speed = format_speed(progress.get("speed"))
            job.eta = format_eta(progress.get("eta"))
            job.detail = download_detail(info)
            if 0 <= index - 1 < len(job.items):
                job.items[index - 1]["status"] = "downloading"

    return hook


def make_postprocessor_hook():
    def hook(progress: dict) -> None:
        name = progress.get("postprocessor") or ""
        if progress.get("status") == "started" and "Merger" in name:
            with job.lock:
                job.detail = "Merging video and audio…"
                job.percent = 100
                job.speed = ""
                job.eta = ""

    return hook


def ydl_options(outtmpl: str, hook) -> dict:
    ffmpeg = find_ffmpeg()
    options = {
        "format": "bv*+ba/b" if ffmpeg else "b",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "windowsfilenames": False,
        "continuedl": True,
        "overwrites": False,
        "progress_hooks": [hook],
        "postprocessor_hooks": [make_postprocessor_hook()],
        **youtube_access(),
    }
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg
    return options


def outtmpl_for(folder: Path, stem: str) -> str:
    escaped = stem.replace("%", "%%")
    return str(folder / f"{escaped}.%(ext)s")


def unique_stem(folder: Path, title: str, used: dict[str, int]) -> str:
    base = safe_name(title, "video")
    key = base.casefold()
    count = used.get(key, 0) + 1
    used[key] = count
    if count == 1:
        return base
    number = count
    while True:
        candidate = f"{base} ({number})"
        if not any(path.name.startswith(candidate + ".") for path in folder.iterdir() if path.is_file()):
            used[key] = number
            return candidate
        number += 1


def fetch_playlist(url: str) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": False,
        **youtube_access(),
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Couldn’t read that playlist.")
    entries = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        video_id = entry.get("id")
        title = entry.get("title") or "Untitled"
        if not video_id or video_id in {"N/A", "NA"}:
            continue
        live = entry.get("live_status") in {"is_live", "is_upcoming"}
        unavailable = title in {"[Deleted video]", "[Private video]"}
        entries.append(
            {
                "id": video_id,
                "title": title,
                "skip": live or unavailable,
                "reason": "Live videos are skipped." if live else "Unavailable" if unavailable else "",
            }
        )
    if not entries:
        raise RuntimeError("That playlist has no videos.")
    return {"title": info.get("title") or "playlist", "entries": entries}


def mark_item(index: int, status: str, error: str = "") -> None:
    with job.lock:
        if 0 <= index - 1 < len(job.items):
            job.items[index - 1]["status"] = status
            job.items[index - 1]["error"] = error


def saved_file(folder: Path, stem: str) -> Path | None:
    prefix = stem + "."
    matches = []
    for path in folder.iterdir():
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        rest = path.name[len(prefix):]
        if re.match(r"f\d+\.", rest):
            continue
        if path.name.endswith((".part", ".ytdl", ".temp")):
            continue
        matches.append(path)
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def is_bot_block(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not a bot" in text or "sign in to confirm" in text


def download_one(url: str, folder: Path, title: str, index: int, used: dict[str, int]) -> Path | None:
    stem = unique_stem(folder, title, used)
    existing = saved_file(folder, stem)
    if existing:
        mark_item(index, "saved")
        with job.lock:
            job.percent = 100
            job.detail = "Already saved"
            job.speed = ""
            job.eta = ""
        return existing
    options = ydl_options(outtmpl_for(folder, stem), make_hook(index))
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if not is_bot_block(exc) or attempt == 2:
                raise
            with job.lock:
                job.detail = "YouTube asked for a pause. Retrying…"
                job.speed = ""
                job.eta = ""
            time.sleep(8 * (attempt + 1))
    if last_error:
        raise last_error
    path = saved_file(folder, stem)
    mark_item(index, "saved")
    with job.lock:
        job.percent = 100
        job.detail = ""
        job.speed = ""
        job.eta = ""
    return path


def link_copy(src: Path, folder: Path, title: str, used: dict[str, int]) -> None:
    """Put the same file in another folder without downloading it again."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = unique_stem(folder, title, used)
    dest = folder / f"{stem}{src.suffix}"
    if dest.exists():
        return
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def download_entries(entries: list[dict], folder: Path) -> tuple[int, int, dict[str, Path]]:
    used: dict[str, int] = {}
    paths: dict[str, Path] = {}
    saved = 0
    failed = 0
    for index, entry in enumerate(entries, 1):
        with job.lock:
            job.current = index
            job.percent = 0
            job.speed = ""
            job.eta = ""
            job.detail = "Starting…"
            job.message = entry["title"]
            if 0 <= index - 1 < len(job.items):
                job.items[index - 1]["status"] = "downloading"
        if entry.get("skip"):
            mark_item(index, "failed", entry.get("reason") or "Skipped")
            failed += 1
            continue
        watch = f"https://www.youtube.com/watch?v={entry['id']}"
        if index > 1:
            time.sleep(2)
        try:
            path = download_one(watch, folder, entry["title"], index, used)
            if path:
                paths[entry["id"]] = path
            saved += 1
        except Exception as exc:
            mark_item(index, "failed", friendly_error(exc))
            failed += 1
    return saved, failed, paths


def finish_job(saved: int, failed: int, total: int, message: str | None = None) -> None:
    with job.lock:
        job.running = False
        job.speed = ""
        job.eta = ""
        job.detail = ""
        job.percent = 100
        if saved == 0:
            job.status = "error"
            job.error = "None of the videos could be downloaded."
            job.message = job.error
        else:
            job.status = "done"
            if message:
                job.message = message
            else:
                noun = "video" if saved == 1 else "videos"
                if failed:
                    job.message = f"Saved {saved} of {total} {noun}."
                else:
                    job.message = f"Saved {saved} {noun}."


def flat_extract(url: str) -> dict | None:
    with yt_dlp.YoutubeDL({**FLAT_OPTS, **youtube_access()}) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except Exception as exc:
            message = str(exc).lower()
            if "does not have" in message or "no videos" in message:
                return None
            raise


def video_record(entry: dict, newest_rank: int) -> dict | None:
    url = entry.get("url") or ""
    if entry.get("ie_key") == "YoutubeTab" or "list=" in url:
        return None
    video_id = entry.get("id")
    if not video_id or video_id in {"N/A", "NA"}:
        return None
    title = entry.get("title") or "Untitled"
    live = entry.get("live_status") in {"is_live", "is_upcoming"}
    unavailable = title in {"[Deleted video]", "[Private video]"}
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = None
    return {
        "id": video_id,
        "title": title,
        "timestamp": timestamp,
        "newest_rank": newest_rank,
        "skip": live or unavailable,
        "reason": "Live videos are skipped." if live else "Unavailable" if unavailable else "",
    }


def collect_tab(info: dict | None) -> list[dict]:
    if not info:
        return []
    videos = []
    for index, entry in enumerate(entry for entry in (info.get("entries") or []) if entry):
        video = video_record(entry, index)
        if video:
            videos.append(video)
    return videos


def remember_video(store: dict[str, dict], video: dict) -> None:
    current = store.get(video["id"])
    if current is None:
        store[video["id"]] = video
        return
    if current["timestamp"] is None and video["timestamp"] is not None:
        current["timestamp"] = video["timestamp"]
    if current["title"] in {"", "Untitled"} and video["title"]:
        current["title"] = video["title"]
    if video["skip"] and not current["skip"]:
        current["skip"] = True
        current["reason"] = video["reason"]


def by_upload(video: dict) -> tuple:
    """Oldest upload first. YouTube lists a tab newest-first, so a higher rank is older."""
    rank = video.get("newest_rank")
    rank_key = 0 if rank is None else -rank
    timestamp = video.get("timestamp")
    if timestamp is None:
        return (1, rank_key)
    return (0, timestamp, rank_key)


def playlist_id_of(entry: dict) -> str | None:
    url = entry.get("url") or ""
    if "list=" in url:
        found = parse_qs(urlparse(url).query).get("list") or []
        if found:
            return found[0]
    video_id = entry.get("id") or ""
    if video_id.startswith(("PL", "OL")):
        return video_id
    return None


def channel_name_from(*infos: dict | None) -> str:
    for info in infos:
        if not info:
            continue
        name = info.get("channel") or info.get("uploader")
        if name:
            return name
        title = info.get("title") or ""
        for suffix in (" - Videos", " - Shorts", " - Playlists", " - Live", " - Streams"):
            if title.endswith(suffix):
                return title[: -len(suffix)]
        if title:
            return title
    return "channel"


def plan_channel(url: str) -> dict:
    root = channel_root(url)
    if not root:
        raise RuntimeError("That link doesn’t look like a YouTube channel.")
    with job.lock:
        job.message = "Reading the channel…"
    videos_info = flat_extract(root + "/videos")
    shorts_info = flat_extract(root + "/shorts")
    streams_info = flat_extract(root + "/streams")
    playlists_info = flat_extract(root + "/playlists")
    name = channel_name_from(videos_info, shorts_info, playlists_info, streams_info)
    with job.lock:
        job.title = name
    store: dict[str, dict] = {}
    for info in (videos_info, shorts_info, streams_info):
        for video in collect_tab(info):
            remember_video(store, video)

    playlists = []
    playlist_entries = [entry for entry in ((playlists_info or {}).get("entries") or []) if entry]
    for index, entry in enumerate(playlist_entries, 1):
        playlist_id = playlist_id_of(entry)
        if not playlist_id or playlist_id.startswith(AUTO_PLAYLIST_PREFIXES):
            continue
        with job.lock:
            job.message = f"Reading playlists… {index} of {len(playlist_entries)}"
        info = flat_extract(f"https://www.youtube.com/playlist?list={playlist_id}")
        if not info:
            continue
        videos = collect_tab(info)
        if not videos:
            continue
        for video in videos:
            if video["id"] not in store:
                video["newest_rank"] = None
            remember_video(store, video)
        playlists.append(
            {
                "title": info.get("title") or entry.get("title") or "playlist",
                "ids": [video["id"] for video in videos],
            }
        )
    ordered = sorted(store.values(), key=by_upload)
    if not ordered:
        raise RuntimeError("That channel has no videos.")
    for playlist in playlists:
        playlist["ids"] = sorted(playlist["ids"], key=lambda video_id: by_upload(store[video_id]))
    return {"title": name, "videos": ordered, "playlists": playlists}


def run_channel(url: str) -> None:
    plan = plan_channel(url)
    channel_folder = DOWNLOADS / safe_name(plan["title"], "channel")
    videos_folder = channel_folder / "Videos"
    videos_folder.mkdir(parents=True, exist_ok=True)
    used_folder_names = {"videos"}
    for playlist in plan["playlists"]:
        base_name = safe_name(playlist["title"], "playlist")
        folder_name = base_name
        suffix = 2
        while folder_name.casefold() in used_folder_names:
            folder_name = f"{base_name} ({suffix})"
            suffix += 1
        used_folder_names.add(folder_name.casefold())
        playlist_dir = channel_folder / folder_name
        playlist_dir.mkdir(parents=True, exist_ok=True)
        playlist["folder"] = playlist_dir
    videos = plan["videos"]
    with job.lock:
        job.title = plan["title"]
        job.folder = str(channel_folder)
        job.total = len(videos)
        job.status = "downloading"
        job.kind = "channel"
        job.detail = "Oldest upload first"
        job.message = plan["title"]
        job.items = [
            {"title": video["title"], "status": "waiting", "error": ""}
            for video in videos
        ]
    saved, failed, paths = download_entries(videos, videos_folder)
    titles = {video["id"]: video["title"] for video in videos}
    for playlist in plan["playlists"]:
        used: dict[str, int] = {}
        for video_id in playlist["ids"]:
            src = paths.get(video_id)
            if src:
                link_copy(src, playlist["folder"], titles[video_id], used)
    playlist_count = len(plan["playlists"])
    if playlist_count:
        folder_word = "playlist folder" if playlist_count == 1 else "playlist folders"
        summary = f"Saved {saved} videos, oldest upload first, plus {playlist_count} {folder_word}."
    else:
        summary = f"Saved {saved} videos, oldest upload first."
    if failed and saved:
        summary = f"Saved {saved} of {len(videos)} videos, oldest upload first."
    finish_job(saved, failed, len(videos), summary)


def run_download(url: str) -> None:
    try:
        channel = channel_root(url)
        playlist = is_playlist_url(url)
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        with job.lock:
            job.running = True
            job.status = "fetching"
            job.kind = "channel" if channel else "playlist" if playlist else "video"
            job.message = "Reading the channel…" if channel else "Reading the link…"
            job.detail = ""
            job.folder = str(DOWNLOADS)
            job.error = ""

        if channel:
            run_channel(url)
            return

        if playlist:
            meta = fetch_playlist(url)
            folder = DOWNLOADS / safe_name(meta["title"], "playlist")
            folder.mkdir(parents=True, exist_ok=True)
            entries = meta["entries"]
            with job.lock:
                job.title = meta["title"]
                job.folder = str(folder)
                job.total = len(entries)
                job.status = "downloading"
                job.message = meta["title"]
                job.items = [
                    {"title": entry["title"], "status": "waiting", "error": ""}
                    for entry in entries
                ]
            saved, failed, _paths = download_entries(entries, folder)
            finish_job(saved, failed, len(entries))
            return

        probe = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            **youtube_access(),
        }
        with yt_dlp.YoutubeDL(probe) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") == "playlist":
            raise RuntimeError("Paste a video, playlist, or channel link.")
        if info.get("is_live"):
            raise RuntimeError("Live streams can’t be downloaded here.")
        title = info.get("title") or "video"
        with job.lock:
            job.title = title
            job.folder = str(DOWNLOADS)
            job.total = 1
            job.current = 1
            job.status = "downloading"
            job.message = title
            job.items = [{"title": title, "status": "downloading", "error": ""}]
        download_one(url, DOWNLOADS, title, 1, {})
        with job.lock:
            job.running = False
            job.status = "done"
            job.percent = 100
            job.detail = ""
            job.speed = ""
            job.eta = ""
            job.message = "Saved to Downloads."
    except Exception as exc:
        with job.lock:
            job.running = False
            job.status = "error"
            job.error = friendly_error(exc)
            job.message = job.error
            job.detail = ""
            job.speed = ""
            job.eta = ""


@app.get("/")
def index():
    return render_template("index.html", downloads=display_path(str(DOWNLOADS)))


@app.get("/api/health")
def health():
    return jsonify(
        ffmpeg=find_ffmpeg() is not None,
        cookies=cookie_browser() is not None,
        downloads=display_path(str(DOWNLOADS)),
    )


@app.post("/api/download")
def start_download():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    if not is_youtube(url):
        return jsonify(error="Paste a YouTube video, playlist, or channel link."), 400
    with job.lock:
        if job.running:
            return jsonify(error="A download is already running."), 409
        job.reset()
        job.running = True
        job.status = "fetching"
        job.message = "Starting…"
    threading.Thread(target=run_download, args=(url,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    return jsonify(job.snapshot())


@app.post("/api/open-folder")
def open_folder():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("path") or ""
    try:
        path = Path(raw).expanduser().resolve()
        path.relative_to(DOWNLOADS.resolve())
    except ValueError:
        return jsonify(error="That folder isn’t inside Downloads."), 400
    except OSError:
        return jsonify(error="Couldn’t open that folder."), 400
    if not path.is_dir():
        return jsonify(error="Folder not found."), 404
    try:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return jsonify(error="Couldn’t open the folder."), 500
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
