import os
import re
import hashlib
import tempfile
import uuid
import shutil
import threading
import datetime
import traceback
import logging
import base64
from typing import Optional, Dict

# Match uvicorn's log line style AND colors so app + server logs are uniform
# (e.g. a green "INFO:     Library index: ...").
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
try:
    from uvicorn.logging import DefaultFormatter
    for _h in logging.getLogger().handlers:
        _h.setFormatter(DefaultFormatter("%(levelprefix)s %(message)s"))
except Exception:
    for _h in logging.getLogger().handlers:
        _h.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
log = logging.getLogger("youtify")

YOUTIFY_BANNER = r"""
██╗   ██╗ ██████╗ ██╗   ██╗████████╗██╗███████╗██╗   ██╗
╚██╗ ██╔╝██╔═══██╗██║   ██║╚══██╔══╝██║██╔════╝╚██╗ ██╔╝
 ╚████╔╝ ██║   ██║██║   ██║   ██║   ██║█████╗   ╚████╔╝
  ╚██╔╝  ██║   ██║██║   ██║   ██║   ██║██╔══╝    ╚██╔╝
   ██║   ╚██████╔╝╚██████╔╝   ██║   ██║██║        ██║
   ╚═╝    ╚═════╝  ╚═════╝    ╚═╝   ╚═╝╚═╝        ╚═╝
"""


def print_startup_banner(*, mode, save_dir, originals_dir, cache_root, host_url, warning=None):
    """One-time startup banner: ASCII logo + a colorized config summary."""
    import sys
    if sys.stdout.isatty():
        MAG, DIM, CYAN, WHITE, RST = (
            "\033[38;5;205m", "\033[2m", "\033[36m", "\033[97m", "\033[0m")
    else:
        MAG = DIM = CYAN = WHITE = RST = ""
    rows = [
        ("Mode", mode),
        ("Save dir", save_dir or "— (temporary, streamed to browser)"),
        ("Archive", originals_dir or "—"),
        ("Cache + DB", cache_root),
        ("Listening", host_url),
    ]
    label_w = max(len(l) for l, _ in rows)
    # Widths computed on plain text (ANSI codes are zero-width on screen).
    plain = [f"  {l.ljust(label_w)}   {v}" for l, v in rows]
    inner = max(len(p) for p in plain) + 2
    bar = "─" * inner

    print(f"{MAG}{YOUTIFY_BANNER}{RST}")
    print(f"{DIM}┌{bar}┐{RST}")
    for (label, value), p in zip(rows, plain):
        pad = " " * (inner - len(p))
        print(f"{DIM}│{RST}  {CYAN}{label.ljust(label_w)}{RST}   {WHITE}{value}{RST}{pad}{DIM}│{RST}")
    print(f"{DIM}└{bar}┘{RST}")
    if warning:
        log.warning(warning)

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Body, Response, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json

import argparse
import glob
import sqlite3
from youtube_downloader import (
    validate_youtube_url, download_youtube_audio, get_video_info,
    archive_original, retag_mp3_in_place, reprocess_from_original,
    find_original, get_audio_duration, read_cover, normalize_cover,
    fetch_channel_avatar,
)

# Import the database module
from database import AudioMetadataDB
from contextlib import asynccontextmanager


def cleanup_stale_sidecars():
    """Remove sidecars/playlists/originals whose MP3 file no longer exists."""
    if BROWSER_DOWNLOAD_MODE:
        return
    # 1. Remove stale meta sidecars. Collect the youtube_ids still referenced
    #    by live sidecars — needed in step 3 because sidecar filenames are
    #    track_ids, which no longer always equal the video id.
    live_video_ids = set()
    for path in glob.glob(os.path.join(META_DIR, "*.json")):
        base = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path) as f:
                sc = json.load(f)
            rel = sc.get("rel_path")
            if rel and not os.path.exists(os.path.join(DOWNLOAD_DIR, rel)):
                os.remove(path)
            else:
                live_video_ids.add(sc.get("youtube_id") or base)
        except Exception:
            # Unreadable sidecar: keep it, and keep its original too.
            live_video_ids.add(base)
    # 2. Clean up playlists: drop missing track_ids; delete if empty.
    existing_ids = {os.path.splitext(f)[0] for f in os.listdir(META_DIR) if f.endswith(".json")}
    for path in glob.glob(os.path.join(PLAYLISTS_DIR, "*.json")):
        try:
            with open(path) as f:
                pl = json.load(f)
            keep = [tid for tid in pl.get("track_ids", []) if tid in existing_ids]
            if not keep:
                os.remove(path)
                cover = os.path.join(PLAYLISTS_DIR, f"{pl['id']}.jpg")
                if os.path.exists(cover):
                    os.remove(cover)
            elif len(keep) < len(pl.get("track_ids", [])):
                pl["track_ids"] = keep
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(pl, f, indent=2)
                os.replace(tmp, path)
        except Exception:
            pass
    # 3. Remove orphaned originals (no sidecar references their video id).
    for fname in os.listdir(ORIGINALS_DIR):
        vid = os.path.splitext(fname)[0]
        if vid not in live_video_ids:
            try:
                os.remove(os.path.join(ORIGINALS_DIR, fname))
            except OSError:
                pass
    # 4. Prune DB entries that no longer have sidecars.
    db.prune_stale(META_DIR, PLAYLISTS_DIR)


def compute_cache_plan() -> list:
    """
    Which tracks deserve a cached copy: favorites first, then most played,
    then most recently played, greedy-filled into the byte budget. One policy
    shared by the SSD hot tier and the client cache-plan endpoint.
    """
    if BROWSER_DOWNLOAD_MODE:
        return []
    items = [it for it in db.get_library()
             if it.get("rel_path") and not it.get("unresolved")]
    # Chained stable sorts: last tiebreak applied first.
    items.sort(key=lambda it: it.get("last_played") or "", reverse=True)
    items.sort(key=lambda it: it.get("play_count") or 0, reverse=True)
    items.sort(key=lambda it: 0 if it.get("favorite") else 1)
    plan, used = [], 0
    for it in items:
        try:
            size = os.path.getsize(_abs(it["rel_path"]))
        except OSError:
            continue
        if used + size > HOT_CACHE_BYTES:
            continue   # too big for what's left; smaller tracks may still fit
        plan.append({"id": it["id"], "track_id": it["track_id"],
                     "rel_path": it["rel_path"], "size": size,
                     "updated_at": it.get("updated_at")})
        used += size
    return plan


def refresh_hot_cache() -> dict:
    """Sync the SSD hot tier to the current plan: copy new/stale, evict dropped."""
    if BROWSER_DOWNLOAD_MODE:
        return {"tracks": 0, "used": 0, "budget": HOT_CACHE_BYTES}
    plan = compute_cache_plan()
    want = {}
    for p in plan:
        src = _abs(p["rel_path"])
        want[p["track_id"] + os.path.splitext(src)[1]] = src
    used = count = 0
    with HOT_LOCK:
        for fname in os.listdir(HOT_DIR):
            if fname not in want:
                try:
                    os.remove(os.path.join(HOT_DIR, fname))
                except OSError:
                    pass
        for fname, src in want.items():
            dst = os.path.join(HOT_DIR, fname)
            try:
                s = os.stat(src)
                if (not os.path.exists(dst) or os.path.getsize(dst) != s.st_size
                        or os.path.getmtime(dst) < s.st_mtime):
                    tmp = dst + ".tmp"
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
                used += s.st_size
                count += 1
            except OSError as e:
                log.warning("hot cache: failed to sync %s: %s", fname, e)
    return {"tracks": count, "used": used, "budget": HOT_CACHE_BYTES}


def _hot_cache_loop():
    """Background refresh so the hot set follows listening habits."""
    import time
    while True:
        time.sleep(1800)
        try:
            refresh_hot_cache()
        except Exception as e:
            log.warning("hot cache refresh failed: %s", e)


def adopt_file(abs_path: str, rel_path: str) -> str:
    """
    Write an unresolved stub sidecar for an audio file that has no metadata
    entry, prefilled from its embedded tags. The track then shows up in the
    library's unresolved drop-zone until the user resolves it via the
    metadata editor. Deterministic id: re-discovering the same path is an
    update, not a duplicate.
    """
    from youtube_downloader import read_audio_tags
    track_id = "loc" + hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]
    try:
        tags = read_audio_tags(abs_path) or {}
    except Exception:
        tags = {}
    now = datetime.datetime.now().isoformat()
    try:
        created = datetime.datetime.fromtimestamp(os.path.getmtime(abs_path)).isoformat()
    except OSError:
        created = now
    prev = read_sidecar(track_id) or {}
    sidecar = {
        "schema_version": 2,
        "track_id": track_id,
        "youtube_id": track_id,   # no known source; the local id doubles as one
        "source_url": f"file:{rel_path}",
        "rel_path": rel_path,
        "filename": os.path.basename(abs_path),
        "original_rel": None,
        "duration": get_audio_duration(abs_path),
        "effects": {},
        "metadata": {
            "title": tags.get("title") or os.path.splitext(os.path.basename(abs_path))[0],
            "album": tags.get("album"),
            "albums": [tags["album"]] if tags.get("album") else [],
            "year": tags.get("year"),
            "composer": tags.get("composer"),
            "artists": split_multi(tags.get("artist")),
            "genres": split_multi(tags.get("genre")),
            "delimiter": "|",
            "custom_tags": [],
        },
        "stats": prev.get("stats") or {"play_count": 0, "last_played": None},
        "favorite": bool(prev.get("favorite", False)),
        "unresolved": True,
        "created_at": prev.get("created_at") or created,
        "updated_at": now,
    }
    write_sidecar(track_id, sidecar)
    return track_id


def discover_unindexed(recursive: bool = False) -> int:
    """
    Adopt audio files in the save dir that no sidecar references. Manual-only
    (triggered by the Discover button, never by rebuild/startup) so stray
    files in nested folders aren't pulled in unasked: default scans only the
    save dir's top level; recursive scanning is opt-in. Each hit becomes an
    unresolved stub track; nothing is moved or modified on disk.
    """
    if BROWSER_DOWNLOAD_MODE:
        return 0
    referenced = set()
    for path in glob.glob(os.path.join(META_DIR, "*.json")):
        try:
            with open(path) as f:
                rel = json.load(f).get("rel_path")
            if rel:
                referenced.add(os.path.normpath(rel))
        except Exception:
            pass
    count = 0
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        # .youtify holds the archive/sidecars/covers, not library audio.
        dirs[:] = [d for d in dirs if d != ".youtify"] if recursive else []
        for fname in files:
            if os.path.splitext(fname)[1].lower().lstrip(".") not in ALLOWED_UPLOAD_EXTS:
                continue
            abs_path = os.path.join(root, fname)
            rel = os.path.normpath(os.path.relpath(abs_path, DOWNLOAD_DIR))
            if rel in referenced:
                continue
            try:
                track_id = adopt_file(abs_path, rel)
                db.upsert_from_sidecar(read_sidecar(track_id), f"{track_id}.json")
                count += 1
            except Exception as e:
                log.warning("discovery: failed to adopt %s: %s", rel, e)
    if count:
        log.info("Discovery: adopted %d unindexed file(s) as unresolved.", count)
    return count

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rebuild the DB index from the on-disk sidecars so the DB is fully
    # disposable. Globals below are defined later at module load but resolved
    # here at startup time.
    if not BROWSER_DOWNLOAD_MODE:
        try:
            cleanup_stale_sidecars()
            n = db.rebuild_from_sidecars(META_DIR, DOWNLOAD_DIR)
            p = db.rebuild_playlists_from_sidecars(PLAYLISTS_DIR)
            log.info("Library index: %d track(s), %d playlist(s) loaded from sidecars.", n, p)
        except Exception as e:
            log.warning("Startup library rebuild failed: %s", e)
        # SSD hot tier: initial sync + periodic refresh, off the request path.
        def _hot_start():
            try:
                st = refresh_hot_cache()
                log.info("Hot cache: %d track(s), %.1f MB.", st["tracks"], st["used"] / 1e6)
            except Exception as e:
                log.warning("Hot cache initial sync failed: %s", e)
            _hot_cache_loop()
        threading.Thread(target=_hot_start, daemon=True).start()
    yield


app = FastAPI(
    title="Youtify",
    description="High-quality YouTube Audio Downloader",
    version="2.2.4",
    lifespan=lifespan,
)

# CORS: Allow Chrome extension and other origins to access the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static file directory
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# Serve static files (CSS, JS, assets)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Without explicit cache headers browsers heuristically cache the JS/CSS for
# days (10% of file age), so UI fixes don't reach clients until a hard reload.
# no-cache = revalidate every load; unchanged files still answer as cheap 304s.
@app.middleware("http")
async def static_revalidate(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

# Store progress in-memory (simple session-based)
# In a production app, use Redis or similar
#   download_progress: keyed by session_id, tracks the /save job (cache + process phases)
#   cache_progress:    keyed by video_id, tracks background pre-caching from /search
download_progress: Dict[str, dict] = {}
cache_progress: Dict[str, dict] = {}

# Handle Configuration (CLI > ENV > DEFAULT)
#   --save-dir  : permanent library on (typically) HDD. Holds the MP3s plus a
#                 .youtify/ archive (originals + per-track metadata sidecars).
#   --cache-dir : working + index store on (typically) SSD. Holds the preview/
#                 download cache (work/) and the rebuildable metadata.db.
def get_config():
    parser = argparse.ArgumentParser(description="YT2MP3 Backend Server")
    parser.add_argument("--save-dir", type=str, help="Directory to save MP3 files")
    parser.add_argument("--cache-dir", type=str, help="Working cache + DB directory")
    parser.add_argument("--turbo", action="store_true",
                        help="Default Turbo Render ON: cache lossless WAV checkpoints "
                             "to speed up re-rendering preview combos (uses disk).")
    args, unknown = parser.parse_known_args()

    save_dir = args.save_dir or os.getenv("SAVE_DIRECTORY")
    cache_dir = (args.cache_dir or os.getenv("CACHE_DIRECTORY")
                 or os.path.expanduser("~/.cache/youtify"))
    # Turbo default: CLI flag OR truthy env. Off unless explicitly enabled.
    turbo = args.turbo or os.getenv("TURBO_PREVIEW", "").strip().lower() in ("1", "true", "yes", "on")
    return save_dir, cache_dir, turbo

ENV_SAVE_DIR, ENV_CACHE_DIR, TURBO_DEFAULT = get_config()

# Cache root (SSD): working files under work/, DB at the root so cleanup_cache
# (which only scans work/) can never touch it.
CACHE_ROOT = os.path.abspath(os.path.expanduser(ENV_CACHE_DIR))
CACHE_DIR = os.path.join(CACHE_ROOT, "work")
DB_PATH = os.path.join(CACHE_ROOT, "metadata.db")
os.makedirs(CACHE_DIR, exist_ok=True)

# SSD hot tier: copies of the most-listened tracks live under the cache root
# so playback rarely touches the HDD save dir. Budget via HOT_CACHE_GB env.
HOT_DIR = os.path.join(CACHE_ROOT, "hot")
os.makedirs(HOT_DIR, exist_ok=True)
try:
    HOT_CACHE_BYTES = int(float(os.getenv("HOT_CACHE_GB", "2")) * (1024 ** 3))
except ValueError:
    HOT_CACHE_BYTES = 2 * 1024 ** 3
HOT_LOCK = threading.Lock()

db = AudioMetadataDB(DB_PATH)

# If no save directory configured, we'll stream downloads directly to browser
BROWSER_DOWNLOAD_MODE = ENV_SAVE_DIR is None
DOWNLOAD_DIR = None
ORIGINALS_DIR = None
META_DIR = None
PLAYLISTS_DIR = None
FACETS_DIR = None
_startup_warning = None

if not BROWSER_DOWNLOAD_MODE:
    # Expand user and resolve to absolute path for reliability
    DOWNLOAD_DIR = os.path.abspath(os.path.expanduser(ENV_SAVE_DIR))

    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception:
        # Fallback to a safe temp directory if provided path is unwritable (common in Docker)
        fallback = os.path.join(tempfile.gettempdir(), "yt2mp3_fallback")
        os.makedirs(fallback, exist_ok=True)
        _startup_warning = f"Could not use {ENV_SAVE_DIR}; falling back to {fallback}"
        DOWNLOAD_DIR = fallback

    # Archive lives with the library (HDD): originals for reprocessing +
    # per-track JSON sidecars that can rebuild the DB if it's lost.
    ORIGINALS_DIR = os.path.join(DOWNLOAD_DIR, ".youtify", "originals")
    META_DIR = os.path.join(DOWNLOAD_DIR, ".youtify", "meta")
    PLAYLISTS_DIR = os.path.join(DOWNLOAD_DIR, ".youtify", "playlists")
    FACETS_DIR = os.path.join(DOWNLOAD_DIR, ".youtify", "facets")
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    os.makedirs(META_DIR, exist_ok=True)
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    os.makedirs(FACETS_DIR, exist_ok=True)



print_startup_banner(
    mode="Server Save" if not BROWSER_DOWNLOAD_MODE else "Browser Download (temporary)",
    save_dir=DOWNLOAD_DIR,
    originals_dir=ORIGINALS_DIR,
    cache_root=CACHE_ROOT,
    host_url="http://localhost:8000",
    warning=_startup_warning,
)
if BROWSER_DOWNLOAD_MODE:
    log.info("No save directory set. Use --save-dir or SAVE_DIRECTORY to keep files + build a library.")


def cleanup_cache():
    """
    Removes cached files older than 2 hours to prevent disk bloat.
    Runs periodically as a background task.
    """
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=2)).timestamp()
    for f in os.listdir(CACHE_DIR):
        fpath = os.path.join(CACHE_DIR, f)
        # Skip directories (e.g. the ckpt/ checkpoint store) — os.remove() on a
        # dir raises and used to abort the whole sweep. ckpt is size-managed by
        # prune_checkpoints + cleared per-video on a new search.
        if not os.path.isfile(fpath):
            continue
        try:
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
        except OSError as e:
            log.warning("Cache cleanup error on %s: %s", f, e)


def clear_other_video_cache(keep_video_id: str):
    """
    On a new search, drop preview renders (prev_*.mp3) and WAV checkpoints that
    belong to OTHER videos, so the cache stays focused on the current track. The
    current video's checkpoints are kept, so re-previewing it stays fast.
    """
    import glob
    try:
        ckpt_dir = os.path.join(CACHE_DIR, "ckpt")
        targets = (glob.glob(os.path.join(CACHE_DIR, "prev_*"))
                   + glob.glob(os.path.join(ckpt_dir, "*.wav")))
        for f in targets:
            if keep_video_id and keep_video_id in os.path.basename(f):
                continue
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception as e:
        log.warning("clear_other_video_cache error: %s", e)


def cleanup_session(session_id: str):
    """Cleanup session progress but KEEP the file"""
    try:
        if session_id in download_progress:
            del download_progress[session_id]
    except Exception as e:
        log.warning("Error cleaning up session %s: %s", session_id, e)

def get_unique_path(directory: str, filename: str) -> str:
    """
    Generates a unique file path by appending a counter if the file already exists.
    Example: song.mp3 -> song_copy1.mp3 -> song_copy2.mp3
    """
    base, ext = os.path.splitext(filename)
    path = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{base}_copy{counter}{ext}")
        counter += 1
    return path

# Memoize silence analysis per (video_id, threshold) so toggling Trim Silence
# or nudging the threshold doesn't re-run an ffmpeg scan every time.
silence_cache: Dict[str, dict] = {}


def sanitize(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum() or c in "._- ,'&").strip()


def split_multi(value: Optional[str], delimiter: str = "|") -> list:
    """Split a delimiter-joined tag string into a clean list."""
    if not value:
        return []
    return [v.strip() for v in value.split(delimiter) if v.strip()]


def build_filename(title, album, artist, composer, delimiter="|", ext="mp3") -> str:
    """
    Build the library filename: "Title (Album) - Artist (Composer).<ext>".
    Shared by /save and the library metadata editor so renames stay consistent.
    """
    title = sanitize(title) or "audio"
    artist = sanitize(artist.replace(delimiter, ', ')) if artist else None
    album = sanitize(album) if album else None
    composer = sanitize(composer) if composer else None

    parts = [title]
    if album:
        parts[0] = f"{title} ({album})"
    if artist or composer:
        right = artist or ''
        if composer:
            right = f"{right} ({composer})" if right else composer
        parts.append(right)
    return " - ".join(parts) + "." + ext.lstrip(".")


def save_filename_base(video_id: str, is_upload: bool, url: Optional[str],
                       custom_filename: Optional[str], meta_title: Optional[str],
                       meta_album_first: Optional[str], meta_artist: Optional[str],
                       composer: Optional[str], delimiter: str) -> str:
    """
    Filename base (no extension) a save will produce — shared by /save and
    /save/peek so the pre-save overwrite warning matches reality, and used
    as the name component of the track identity.
    """
    if custom_filename and custom_filename.strip():
        return sanitize(custom_filename) or "audio"
    title_for_name = meta_title
    if not title_for_name:
        title_for_name = get_video_info(url).get('title', video_id) if not is_upload else video_id
    return os.path.splitext(build_filename(title_for_name, meta_album_first,
                                           meta_artist, composer, delimiter))[0]


def resolve_source(url: Optional[str], source_id: Optional[str]):
    """
    Resolve an input to (video_id, source_path). For an upload/cached source
    (source_id set) returns the cached file path; for a YouTube url returns
    (video_id, None) and the caller downloads as usual.
    """
    from youtube_downloader import find_cache_file
    if source_id:
        path = find_cache_file(CACHE_DIR, source_id)
        if not path:
            raise HTTPException(status_code=404, detail="Uploaded source not found (cache may have been cleared — re-upload).")
        return source_id, path
    if not url:
        raise HTTPException(status_code=400, detail="Provide a YouTube url or a source_id.")
    return validate_youtube_url(url), None


def _segment_key(start_time: Optional[float], end_time: Optional[float]) -> str:
    if start_time is None and end_time is None:
        return ""
    return "%s-%s" % ("" if start_time is None else round(float(start_time), 3),
                      "" if end_time is None else round(float(end_time), 3))


def track_id_for(video_id: str, start_time: Optional[float] = None,
                 end_time: Optional[float] = None) -> str:
    """
    LEGACY track identity (source + cut only) — kept so resolve_track_id can
    find tracks saved before names joined the identity, and old sidecars
    (plain video_id / video_id__<hash8(cut)>) stay valid without renames.
    """
    seg = _segment_key(start_time, end_time)
    if not seg:
        return video_id
    return f"{video_id}__{hashlib.sha1(seg.encode()).hexdigest()[:8]}"


def resolve_track_id(video_id: str, start_time: Optional[float],
                     end_time: Optional[float], filename_base: str) -> str:
    """
    Track identity = source video + cut range + name-deriving metadata (the
    filename base, i.e. title/album/artist/composer or a custom filename).
    Re-saving with the same name updates the track in place; a different
    name creates a sibling track sharing the archived original. Falls back
    to a track's legacy (pre-name-aware) id when its stored filename still
    matches, so existing libraries update instead of duplicating.
    """
    name = (filename_base or "").strip().casefold()
    key = f"{_segment_key(start_time, end_time)}|{name}"
    new_id = f"{video_id}__{hashlib.sha1(key.encode()).hexdigest()[:8]}"
    if read_sidecar(new_id) is not None:
        return new_id
    legacy = track_id_for(video_id, start_time, end_time)
    sc = read_sidecar(legacy)
    if sc is not None:
        stored = os.path.splitext(sc.get("filename") or "")[0]
        if stored.strip().casefold() == name:
            return legacy
    return new_id


# Serializes sidecar read-modify-write cycles (play stats, favorite) so two
# clients hitting the same track concurrently can't drop each other's update.
# Writes themselves are already atomic (write-tmp + os.replace).
SIDECAR_LOCK = threading.Lock()


def sidecar_path_for(track_id: str) -> str:
    return os.path.join(META_DIR, f"{track_id}.json")


def write_sidecar(track_id: str, data: dict):
    """Atomically write the per-track sidecar JSON."""
    path = sidecar_path_for(track_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_sidecar(track_id: str) -> Optional[dict]:
    path = sidecar_path_for(track_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        log.warning("Failed to read sidecar %s: %s", path, e)
        return None


def playlist_sidecar_path(pid: str) -> str:
    return os.path.join(PLAYLISTS_DIR, f"{pid}.json")


# --- Facet covers (custom thumbnails for Browse-by Album/Artist/Genre/Year) ---
# Stored as <save-dir>/.youtify/facets/<field>/<slug>.jpg with a per-field
# index.json mapping slug -> original value (the frontend never computes slugs).

FACET_FIELDS = ("album", "artist", "genre", "year")
# Custom metadata keys can be pinned as browse facets; their covers live in
# a directory named after the key, so the name must stay path-safe.
_FACET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")


def _facet_slug(value: str) -> str:
    import hashlib
    base = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or "x"
    return f"{base[:60]}-{hashlib.md5(value.encode('utf-8')).hexdigest()[:6]}"


def _facet_dir(field: str) -> str:
    if field not in FACET_FIELDS and not _FACET_NAME_RE.fullmatch(field):
        raise HTTPException(status_code=422, detail="invalid facet field name")
    d = os.path.join(FACETS_DIR, field)
    os.makedirs(d, exist_ok=True)
    return d


def _facet_index_read(field: str) -> dict:
    path = os.path.join(_facet_dir(field), "index.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _facet_index_write(field: str, index: dict):
    path = os.path.join(_facet_dir(field), "index.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def save_facet_cover(field: str, value: str, jpeg_bytes: bytes):
    """Write a facet cover image + register it in the field's index."""
    slug = _facet_slug(value)
    with open(os.path.join(_facet_dir(field), f"{slug}.jpg"), "wb") as fh:
        fh.write(jpeg_bytes)
    idx = _facet_index_read(field)
    idx[slug] = value
    _facet_index_write(field, idx)


def facet_cover_path(field: str, value: str) -> str:
    return os.path.join(_facet_dir(field), f"{_facet_slug(value)}.jpg")


def playlist_cover_path(pid: str) -> str:
    return os.path.join(PLAYLISTS_DIR, f"{pid}.jpg")


def write_playlist_sidecar(pid: str, data: dict):
    path = playlist_sidecar_path(pid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_playlist_sidecar(pid: str) -> Optional[dict]:
    path = playlist_sidecar_path(pid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        log.warning("Failed to read playlist sidecar %s: %s", path, e)
        return None


def precache_with_progress(url: str):
    """
    Background task launched by /search: downloads bestaudio into the cache
    while recording real-time progress in cache_progress[video_id], so the UI
    can show a live caching bar instead of a binary 'cached / not cached'.
    """
    from youtube_downloader import download_to_cache
    try:
        video_id = validate_youtube_url(url)
    except Exception:
        return
    cache_progress[video_id] = {"status": "caching", "progress": 0.0}

    def cb(pct):
        cache_progress[video_id] = {
            "status": "done" if pct >= 100 else "caching",
            "progress": round(pct, 1),
        }

    try:
        download_to_cache(url, CACHE_DIR, progress_cb=cb)
        cache_progress[video_id] = {"status": "done", "progress": 100.0}
    except Exception as e:
        cache_progress[video_id] = {"status": "error", "progress": 0.0, "message": str(e)}

@app.get("/")
async def serve_ui():
    """Serves the main UI"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": "YouTube Audio Downloader API is running",
        "usage": "GET /stream?url=YOUR_YT_URL",
        "ui_status": "index.html not found in static folder"
    }

@app.get("/config")
async def get_config_endpoint():
    """Get server configuration - tells frontend if browser download mode is enabled"""
    return {
        "browser_download_mode": BROWSER_DOWNLOAD_MODE,
        "save_directory": DOWNLOAD_DIR,
        "turbo_default": TURBO_DEFAULT,
    }

@app.get("/info")
def video_info(url: str = Query(..., description="The YouTube URL")):
    """Get metadata for a video"""
    try:
        info = get_video_info(url)
        return info
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/yt-search")
def yt_search(q: str = Query(..., description="Free-text search query")):
    """Search YouTube and return up to 30 pickable results (for non-URL input)."""
    from youtube_downloader import search_youtube
    try:
        return {"results": search_youtube(q, 30)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.get("/progress/{session_id}")
async def get_progress(session_id: str):
    """Get progress for a specific session"""
    return download_progress.get(session_id, {"status": "not_started", "progress": 0})


@app.get("/search")
def search_video(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="The YouTube URL to search")
):
    """
    Validates URL, extracts info, and triggers pre-caching in the background.
    """
    try:
        # 1. Validate
        video_id = validate_youtube_url(url)

        # 2. Get Info (Speedy metadata extraction)
        info = get_video_info(url)

        # 3. Limit check (30 minutes = 1800 seconds)
        if info.get('duration', 0) > 1800:
            info['can_preview'] = False
            info['limit_reason'] = "Video longer than 30 minutes. Preview disabled for performance."
        else:
            info['can_preview'] = True
        # Pre-cache in the background regardless of preview limit, so a later
        # /save can reuse it (the only gate is whether we *stream* a preview).
        background_tasks.add_task(precache_with_progress, url)

        # New search -> clear other videos' preview renders + checkpoints (keeps
        # this video's, so re-previewing it stays fast).
        background_tasks.add_task(clear_other_video_cache, video_id)
        background_tasks.add_task(cleanup_cache)
        
        # Pass upload_date to frontend for year pre-population
        if info.get('upload_date'):
            info['upload_date'] = info['upload_date']  # YYYYMMDD format
        
        return info

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Provide meaningful error messages for search/cache failures
        error_msg = str(e)
        if "Invalid data found" in error_msg:
             error_msg = "Corrupted audio data received from YouTube. Please try again."
        elif "ffprobe" in error_msg.lower():
             error_msg = "ffmpeg/ffprobe analysis failed. The video format might be unsupported."
             
        raise HTTPException(status_code=500, detail=f"Search failed: {error_msg}")


ALLOWED_UPLOAD_EXTS = {"mp3", "flac", "wav", "m4a", "aac", "ogg", "oga",
                       "opus", "aiff", "aif", "alac", "wma", "mp4"}


@app.post("/upload")
async def upload_source(file: UploadFile = File(...)):
    """
    Ingest a manually-uploaded audio file as a generic source: save it into the
    working cache under a non-YouTube id, probe it (duration + lossless), and
    read its embedded tags + cover so the form can be pre-filled. The returned
    shape mirrors /search so the frontend treats it like any other source.
    """
    import secrets
    from youtube_downloader import probe_audio, get_audio_duration, read_audio_tags, normalize_cover

    ext = os.path.splitext(file.filename or "")[1].lower().lstrip(".")
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '.{ext}'. Upload an audio file.")

    source_id = "up" + secrets.token_hex(6)   # deliberately not an 11-char YT id
    dest = os.path.join(CACHE_DIR, f"{source_id}.{ext}")
    try:
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        await file.close()

    duration = get_audio_duration(dest)
    if not duration:
        try: os.remove(dest)
        except OSError: pass
        raise HTTPException(status_code=400, detail="Could not read that file as audio.")

    probe = probe_audio(dest)
    tags = read_audio_tags(dest)

    thumbnail = None
    cover = tags.get("cover")
    if cover:
        try:
            data, mime = normalize_cover(cover[0])
            if data:
                thumbnail = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())
        except Exception:
            thumbnail = None

    return {
        "video_id": source_id,
        "is_upload": True,
        "title": tags.get("title") or os.path.splitext(file.filename or "")[0],
        "author": tags.get("artist"),
        "album": tags.get("album"),
        "year": tags.get("year"),
        "genre": tags.get("genre"),
        "composer": tags.get("composer"),
        "duration": duration,
        "thumbnail": thumbnail,
        "lossless": probe.get("lossless", False),
        "src_ext": ext,
    }


@app.post("/save")
def save_audio(
    background_tasks: BackgroundTasks,
    url: Optional[str] = Query(None, description="The YouTube URL to download audio from (or use source_id)"),
    source_id: Optional[str] = Query(None, description="Id of an already-cached source (e.g. an upload) — use instead of url"),
    output_format: str = Query("auto", description="Export format: auto|mp3|flac|wav"),
    custom_filename: Optional[str] = Query(None, description="Override the auto-generated filename (no extension)"),
    start_time: Optional[float] = Query(None, description="Start time in seconds"),
    end_time: Optional[float] = Query(None, description="End time in seconds"),
    trim_silence: bool = Query(True, description="Trim leading/trailing silence"),
    silence_thresh: float = Query(-40.0, description="Silence threshold in dBFS (-60 to -20, higher = more aggressive)"),
    eq_preset: Optional[str] = Query(None, description="Equalizer preset"),
    mbc_preset: Optional[str] = Query(None, description="Multiband compressor preset"),
    normalize: bool = Query(True, description="Apply loudness normalization"),
    normalize_i: float = Query(-16.0, description="Target loudness in LUFS"),
    enhance_mode: Optional[str] = Query(None, description="Enhancement mode: Restore/Vocal/Crisp/Warmth"),
    enhance_intensity: float = Query(1.5, description="Enhancement intensity"),
    original: bool = Query(False, description="Bypass all processing"),
    session_id: Optional[str] = Query(None, description="Optional session ID for progress tracking"),
    meta_title: Optional[str] = Query(None, description="Title metadata tag"),
    meta_artist: Optional[str] = Query(None, description="Artist metadata tag"),
    meta_album: Optional[str] = Query(None, description="Album metadata tag"),
    meta_genre: Optional[str] = Query(None, description="Genre metadata tag"),
    meta_year: Optional[str] = Query(None, description="Year metadata tag"),
    meta_composer: Optional[str] = Query(None, description="Composer metadata tag"),
    delimiter: str = Query("|", description="Delimiter used between artist/genre tags"),
    # Sent in the POST body (not the query string): a base64 cover image can be
    # large and would blow past URL/header length limits if put in the URL.
    metadata_json: Optional[str] = Body(None, embed=True, description="JSON string with custom_tags and thumbnail_base64")
):
    """
    Downloads and saves audio directly to /mnt/Apps.
    """
    try:
        # 1. Resolve the source: YouTube url OR an already-cached upload.
        from youtube_downloader import resolve_output_format
        video_id, source_path = resolve_source(url, source_id)
        is_upload = source_path is not None
        out_fmt = resolve_output_format(output_format, source_path) if is_upload \
            else (output_format if output_format in ("mp3", "flac", "wav") else "mp3")

        # 2. Setup session
        if not session_id:
            session_id = uuid.uuid4().hex[:8]
        
        download_progress[session_id] = {"status": "starting", "phase": "cache", "progress": 0}

        # Unified progress: the downloader reports (phase, percent) for both the
        # cache download and the single FFmpeg processing pass.
        def on_progress(phase: str, percent: float):
            download_progress[session_id] = {
                "status": "caching" if phase == "cache" else "processing",
                "phase": phase,
                "progress": round(percent, 1),
            }

        # 3. Parse extra metadata (custom tags + cover) once.
        custom_tags = []
        thumbnail_base64 = None
        if metadata_json:
            try:
                extra = json.loads(metadata_json)
                custom_tags = extra.get('custom_tags', []) or []
                thumbnail_base64 = extra.get('thumbnail_base64')
            except Exception as e:
                log.warning("Failed to parse metadata_json: %s", e)

        composer_from_json = next(
            (t.get('value') for t in custom_tags
             if t.get('key', '').lower() == 'composer'), None)
        composer = meta_composer or composer_from_json

        # Album is multi-value: the first one is canonical (filename, DB column,
        # standard ALBUM tag — Jellyfin-compatible); the full list goes to the
        # sidecar + an extra ALBUMS tag (handled by the embedders).
        albums = split_multi(meta_album, delimiter)
        meta_album_first = albums[0] if albums else None

        # 4. Build filename "Title (Album) - Artist (Composer).<fmt>", or use the
        #    user's custom override. The base doubles as the track-identity key.
        filename_base = save_filename_base(video_id, is_upload, url, custom_filename,
                                           meta_title, meta_album_first, meta_artist,
                                           composer, delimiter)
        filename_to_use = filename_base + "." + out_fmt

        # 5. Determine output directory based on mode
        if BROWSER_DOWNLOAD_MODE:
            # Use temp directory for browser downloads
            output_dir = tempfile.mkdtemp(prefix="yt2mp3_")
            final_path = os.path.join(output_dir, filename_to_use)
        else:
            # Handle duplicates for server save mode
            final_path = get_unique_path(DOWNLOAD_DIR, filename_to_use)
            output_dir = DOWNLOAD_DIR

        final_filename = os.path.basename(final_path)
        output_filename_base = os.path.splitext(final_filename)[0]

        # 6. Build user metadata dict for ID3 embedding
        user_metadata = {'delimiter': delimiter}
        if meta_title: user_metadata['title'] = meta_title
        if meta_artist: user_metadata['artist'] = meta_artist
        if meta_album: user_metadata['album'] = meta_album
        if meta_genre: user_metadata['genre'] = meta_genre
        if meta_year: user_metadata['year'] = meta_year
        if meta_composer: user_metadata['composer'] = meta_composer
        if custom_tags: user_metadata['custom_tags'] = custom_tags
        if thumbnail_base64: user_metadata['thumbnail_base64'] = thumbnail_base64

        # 7. Download (or use cached upload) and Process
        output_path = download_youtube_audio(
            url=url,
            source_path=source_path,
            output_format=out_fmt,
            output_dir=output_dir,
            filename=output_filename_base,
            start_time=start_time,
            end_time=end_time,
            trim_silence_flag=False if original else trim_silence,
            silence_thresh=silence_thresh,
            eq_preset=None if original else eq_preset,
            mbc_preset=None if original else mbc_preset,
            enhance_mode=None if original else enhance_mode,
            enhance_intensity=enhance_intensity,
            normalize=False if original else normalize,
            normalize_i=normalize_i,
            original=original,
            user_metadata=user_metadata if user_metadata else None,
            cache_dir=CACHE_DIR,
            on_progress=on_progress,
        )
        
        # Archive + index (save-dir mode only): keep a permanent copy of the
        # source audio, write a sidecar describing the effects/metadata, and
        # upsert the DB index. The sidecar is the source of truth; the DB can
        # be rebuilt from it.
        if not BROWSER_DOWNLOAD_MODE:
            try:
                # Identity = source + cut + name-deriving metadata: same name
                # updates in place, different name is a sibling track. The
                # archived original stays keyed by video_id (all variants
                # share the one source download).
                track_id = resolve_track_id(video_id, start_time, end_time, filename_base)
                original_dest = archive_original(CACHE_DIR, video_id, ORIGINALS_DIR)
                original_rel = (os.path.relpath(original_dest, DOWNLOAD_DIR)
                                if original_dest else None)
                duration = get_audio_duration(final_path)

                effects = {
                    "start_time": start_time, "end_time": end_time,
                    "trim_silence": False if original else trim_silence,
                    "silence_thresh": silence_thresh,
                    "eq_preset": None if original else eq_preset,
                    "mbc_preset": None if original else mbc_preset,
                    "enhance_mode": None if original else enhance_mode,
                    "enhance_intensity": enhance_intensity,
                    "normalize": False if original else normalize,
                    "normalize_i": normalize_i,
                    "original": original,
                }
                artists = split_multi(meta_artist, delimiter)
                genres = split_multi(meta_genre, delimiter)
                # Re-saving the same track (same video + same cut) must not
                # reset its stats, favorite flag, or date-added — carry them
                # over from any prior sidecar.
                prev = read_sidecar(track_id) or {}
                stats = prev.get("stats") or {"play_count": 0, "last_played": None}
                favorite = bool(prev.get("favorite", False))
                created_at = prev.get("created_at") or datetime.datetime.now().isoformat()
                # A re-save REPLACES the track's audio: without this, the old
                # file survives as an orphan (get_unique_path had dodged it
                # with a _copyN name) that discovery later adopts as a bogus
                # unresolved duplicate. Delete it, then reclaim the clean
                # filename the _copyN suffix was avoiding.
                prev_rel = prev.get("rel_path")
                if prev_rel and os.path.normpath(prev_rel) != \
                        os.path.normpath(os.path.relpath(final_path, DOWNLOAD_DIR)):
                    try:
                        old_abs = os.path.join(DOWNLOAD_DIR, prev_rel)
                        if os.path.exists(old_abs):
                            os.remove(old_abs)
                        desired_abs = os.path.join(DOWNLOAD_DIR, filename_to_use)
                        if final_path != desired_abs and not os.path.exists(desired_abs):
                            os.rename(final_path, desired_abs)
                            final_path = desired_abs
                            output_path = desired_abs
                            final_filename = filename_to_use
                    except OSError as e:
                        log.warning("re-save cleanup failed: %s", e)
                sidecar = {
                    "schema_version": 2,
                    "track_id": track_id,
                    "youtube_id": video_id,
                    "source_url": url or f"upload:{video_id}",
                    "rel_path": os.path.relpath(final_path, DOWNLOAD_DIR),
                    "filename": final_filename,
                    "original_rel": original_rel,
                    "duration": duration,
                    "effects": effects,
                    "metadata": {
                        "title": meta_title, "album": meta_album_first, "albums": albums,
                        "year": meta_year,
                        "composer": meta_composer, "artists": artists, "genres": genres,
                        "delimiter": delimiter, "custom_tags": custom_tags,
                    },
                    "stats": stats,
                    "favorite": favorite,
                    "created_at": created_at,
                    "updated_at": datetime.datetime.now().isoformat(),
                }
                write_sidecar(track_id, sidecar)

                db.upsert_audio(
                    track_id=track_id,
                    youtube_id=video_id, title=meta_title, album=meta_album_first,
                    year=meta_year, duration=duration,
                    rel_path=sidecar["rel_path"], filename=final_filename,
                    sidecar_path=f"{track_id}.json", effects=effects,
                    artists=artists, genres=genres, albums=albums,
                    custom_fields={t["key"]: t.get("value")
                                   for t in custom_tags if t.get("key")},
                    play_count=stats.get("play_count", 0),
                    last_played=stats.get("last_played"),
                    favorite=favorite,
                    created_at=created_at.replace("T", " ")[:19],
                )
            except Exception as e:
                log.warning("Failed to archive/index metadata: %s", e)

            # Default artist image from the channel pfp (background, best-effort).
            first_artist = (split_multi(meta_artist, delimiter) or [None])[0]
            if url and first_artist:
                background_tasks.add_task(fetch_artist_pfp_task, url, first_artist)

        # 7. Final progress update
        download_progress[session_id] = {
            "status": "finished", 
            "progress": 100, 
            "path": output_path,
            "filename": final_filename,
            "browser_download": BROWSER_DOWNLOAD_MODE
        }
        
        if BROWSER_DOWNLOAD_MODE:
            return {
                "status": "success",
                "message": "Ready for download",
                "browser_download": True,
                "download_path": output_path,
                "filename": final_filename
            }
        else:
            return {
                "status": "success",
                "message": f"Saved to {final_path}",
                "browser_download": False,
                "path": final_path,
                "filename": final_filename
            }


    except Exception as e:
        if session_id in download_progress:
            download_progress[session_id] = {"status": "error", "message": str(e)}
        raise HTTPException(status_code=500, detail=str(e))

    except ValueError as e:
        if session_id in download_progress:
            download_progress[session_id] = {"status": "error", "message": str(e)}
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/save/peek")
def save_peek(
    url: Optional[str] = Query(None),
    source_id: Optional[str] = Query(None),
    start_time: Optional[float] = Query(None),
    end_time: Optional[float] = Query(None),
    custom_filename: Optional[str] = Query(None),
    meta_title: Optional[str] = Query(None),
    meta_artist: Optional[str] = Query(None),
    meta_album: Optional[str] = Query(None),
    meta_composer: Optional[str] = Query(None),
    delimiter: str = Query("|"),
    metadata_json: Optional[str] = Body(None, embed=True),
):
    """
    Dry-run of /save's identity resolution: reports whether this save would
    update an existing track, so the UI can warn before overwriting. Mirrors
    /save's filename/identity derivation exactly (same helpers).
    """
    if BROWSER_DOWNLOAD_MODE:
        return {"exists": False}
    try:
        video_id, source_path = resolve_source(url, source_id)
    except HTTPException:
        return {"exists": False}
    custom_tags = []
    if metadata_json:
        try:
            custom_tags = json.loads(metadata_json).get("custom_tags") or []
        except Exception:
            pass
    composer = meta_composer or next(
        (t.get("value") for t in custom_tags
         if t.get("key", "").lower() == "composer"), None)
    albums = split_multi(meta_album, delimiter)
    base = save_filename_base(video_id, source_path is not None, url,
                              custom_filename, meta_title,
                              albums[0] if albums else None, meta_artist,
                              composer, delimiter)
    track_id = resolve_track_id(video_id, start_time, end_time, base)
    sc = read_sidecar(track_id)
    return {"exists": sc is not None, "track_id": track_id,
            "title": (sc or {}).get("metadata", {}).get("title"),
            "filename": (sc or {}).get("filename")}


@app.get("/download-file")
async def download_file(
    path: str = Query(..., description="Path to the file to download"),
    filename: str = Query(..., description="Filename for the download"),
    background_tasks: BackgroundTasks = None
):
    """
    Stream a file to browser for download (used in browser download mode).
    Cleans up temp file after download.
    """
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Security check - only allow files from temp directory
    if not path.startswith(tempfile.gettempdir()):
        raise HTTPException(status_code=403, detail="Access denied")
    
    def cleanup_temp():
        try:
            parent_dir = os.path.dirname(path)
            if parent_dir.startswith(tempfile.gettempdir()) and os.path.exists(parent_dir):
                shutil.rmtree(parent_dir)
        except Exception as e:
            log.warning("Failed to cleanup temp dir: %s", e)
    
    # Schedule cleanup after response is sent
    if background_tasks:
        background_tasks.add_task(cleanup_temp)
    
    import urllib.parse
    encoded_filename = urllib.parse.quote(filename)
    mime = {"flac": "audio/flac", "wav": "audio/wav", "mp3": "audio/mpeg"}.get(
        os.path.splitext(path)[1].lower().lstrip("."), "audio/mpeg")

    return FileResponse(
        path,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )

@app.get("/stream")
def stream_audio(
    url: Optional[str] = Query(None, description="The YouTube URL to stream (or use source_id)"),
    source_id: Optional[str] = Query(None, description="Id of an already-cached source (upload)"),
    eq_preset: Optional[str] = Query(None),
    mbc_preset: Optional[str] = Query(None),
    normalize: bool = Query(True),
    normalize_i: float = Query(-16.0),
    enhance_mode: Optional[str] = Query(None),
    enhance_intensity: float = Query(1.5),
    original: bool = Query(False),
    turbo: bool = Query(False),
    quality: str = Query("hq", description="Preview quality: hq (lossless FLAC) | fast (128k MP3)"),
):
    """
    Preview the FULL track with the chosen effects applied — but WITHOUT range
    clipping or silence trimming. Those are mechanical cuts applied only on
    export; keeping the preview full-length lets the browser seek freely and the
    playhead map 1:1 to the timeline.

    Renders once to a per-effect cached MP3 and serves it via FileResponse, which
    supports HTTP range requests (seekable) and makes A/B switching instant.
    """
    from youtube_downloader import download_to_cache, render_preview_checkpointed
    import hashlib
    try:
        video_id, source_path = resolve_source(url, source_id)

        # Hash the effect set so identical settings reuse the same rendered file.
        # Fast previews get their own cache file (different codec/extension).
        quality = quality if quality in ("hq", "fast") else "hq"
        key = f"{eq_preset}|{mbc_preset}|{enhance_mode}|{enhance_intensity}|{normalize}|{normalize_i}|{original}"
        h = hashlib.md5(key.encode()).hexdigest()[:10]
        out = os.path.join(CACHE_DIR,
                           f"prev_{video_id}_{h}_fast.mp3" if quality == "fast"
                           else f"prev_{video_id}_{h}.flac")
        media_type = "audio/mpeg" if quality == "fast" else "audio/flac"

        # Fast path: already rendered — serve it straight away.
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return FileResponse(out, media_type=media_type)

        # First render: ensure the source is cached, then gate on duration.
        try:
            cache_file = source_path or download_to_cache(url, CACHE_DIR)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not cache audio. {str(e)}")

        if (get_audio_duration(cache_file) or 0) > 1800:
            raise HTTPException(status_code=403, detail="Preview restricted to videos under 30 minutes.")

        # Render to a temp file (preserve .flac extension so FFmpeg detects the
        # output format), then atomically rename so a concurrent reader never
        # sees a partial file (prevents Content-Length mismatch errors).
        #
        # The tmp name is unique per request: the dual-element preview can fire
        # several /stream calls for the SAME combo at once, and a shared tmp made
        # them clobber each other (one renamed it away, the rest hit
        # FileNotFoundError on os.replace). Each renders to its own tmp; whoever
        # finishes first publishes `out`, the rest just reuse it.
        base, ext = os.path.splitext(out)
        tmp = f"{base}.rendering.{os.getpid()}.{uuid.uuid4().hex[:8]}{ext}"
        try:
            render_preview_checkpointed(
                cache_file, tmp, video_id, os.path.join(CACHE_DIR, "ckpt"),
                eq_preset=eq_preset, mbc_preset=mbc_preset,
                enhance_mode=enhance_mode, enhance_intensity=enhance_intensity,
                normalize=normalize, normalize_i=normalize_i, original=original,
                use_checkpoints=turbo, quality=quality,
            )
            # If a concurrent request already published `out`, drop ours; else
            # atomically publish. os.replace is atomic, so a late writer simply
            # overwrites with identical bytes — never a missing-file error.
            if os.path.exists(out) and os.path.getsize(out) > 1024:
                try: os.remove(tmp)
                except OSError: pass
            else:
                os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass

        return FileResponse(out, media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        error_msg = str(e)
        if "Invalid data found" in error_msg:
            error_msg = "Invalid audio data in cache. Please refresh the page and try searching again."
        raise HTTPException(status_code=500, detail=error_msg)


@app.get("/cache-status")
async def cache_status(url: str = Query(..., description="The YouTube URL to check cache for")):
    """Check if audio is cached for this URL, plus live download progress."""
    import glob
    try:
        video_id = validate_youtube_url(url)
        existing = glob.glob(os.path.join(CACHE_DIR, f"{video_id}.*"))
        cached = any(os.path.getsize(f) > 1024 for f in existing if os.path.exists(f))
        prog = cache_progress.get(video_id, {})
        progress = 100.0 if cached else float(prog.get("progress", 0.0))
        return {
            "cached": cached,
            "progress": progress,
            "status": prog.get("status", "done" if cached else "idle"),
        }
    except Exception:
        return {"cached": False, "progress": 0.0, "status": "idle"}


@app.get("/silence-info")
def silence_info(
    url: Optional[str] = Query(None, description="The YouTube URL to analyze (or use source_id)"),
    source_id: Optional[str] = Query(None, description="Id of an already-cached source (upload)"),
    silence_thresh: float = Query(-40.0)
):
    """
    Returns leading and trailing silence offsets for the cached audio.
    Returns defaults (0, 0) if analysis fails to avoid blocking playback.
    """
    from youtube_downloader import download_to_cache, get_silence_offsets
    import traceback
    try:
        video_id, source_path = resolve_source(url, source_id)
        ckey = f"{video_id}|{silence_thresh}"
        if ckey in silence_cache:
            return silence_cache[ckey]
        cache_file = source_path or download_to_cache(url, CACHE_DIR)
        start, end = get_silence_offsets(cache_file, silence_thresh=silence_thresh)
        result = {"leading_silence": start, "trailing_silence": end}
        silence_cache[ckey] = result
        return result
    except Exception as e:
        # Log the error but return defaults so playback can continue
        log.warning("silence-info failed for %s: %s", url, e)
        traceback.print_exc()
        return {"leading_silence": 0, "trailing_silence": 0}


# ---------------------------------------------------------------------------
# Library / archive endpoints (save-dir mode only) + tag suggestions.
# ---------------------------------------------------------------------------

def _require_library():
    if BROWSER_DOWNLOAD_MODE:
        raise HTTPException(status_code=404,
                            detail="Library is only available in save-directory mode.")


def _abs(rel_path: str) -> str:
    return os.path.join(DOWNLOAD_DIR, rel_path)


@app.get("/library")
def library_list():
    """List every saved track (newest first)."""
    _require_library()
    return {"items": db.get_library()}


@app.get("/library/{audio_id}")
def library_detail(audio_id: int):
    _require_library()
    detail = db.get_audio_detail(audio_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Track not found")
    return detail


@app.post("/library/rebuild")
def library_rebuild():
    """Re-index the DB from the on-disk sidecars (stale entries are purged first)."""
    _require_library()
    cleanup_stale_sidecars()
    n = db.rebuild_from_sidecars(META_DIR, DOWNLOAD_DIR)
    db.rebuild_playlists_from_sidecars(PLAYLISTS_DIR)
    return {"indexed": n}


@app.post("/library/discover")
def library_discover(recursive: bool = Query(False)):
    """
    Manually adopt unindexed audio files in the save dir as unresolved tracks.
    Default looks only at the save dir's top level; recursive is opt-in so
    nested folders aren't swept up unasked.
    """
    _require_library()
    return {"discovered": discover_unindexed(recursive=recursive)}


@app.post("/library/import")
async def library_import(files: list[UploadFile] = File(...)):
    """
    Drop-zone ingest: save uploaded audio files straight into the library as
    unresolved tracks (stub sidecar + index) for later manual resolution —
    unlike /upload, which stages a single file for the download form.
    """
    _require_library()
    imported, errors = [], []
    for file in files:
        name = os.path.basename(file.filename or "")
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        if ext not in ALLOWED_UPLOAD_EXTS:
            errors.append({"filename": name, "error": f"unsupported type '.{ext}'"})
            continue
        base = sanitize(os.path.splitext(name)[0]) or "import"
        dest = get_unique_path(DOWNLOAD_DIR, f"{base}.{ext}")
        try:
            with open(dest, "wb") as out:
                shutil.copyfileobj(file.file, out)
        finally:
            await file.close()
        if not get_audio_duration(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
            errors.append({"filename": name, "error": "not readable as audio"})
            continue
        rel = os.path.normpath(os.path.relpath(dest, DOWNLOAD_DIR))
        track_id = adopt_file(dest, rel)
        audio_id = db.upsert_from_sidecar(read_sidecar(track_id), f"{track_id}.json")
        imported.append({"filename": os.path.basename(dest), "track_id": track_id,
                         "id": audio_id})
    return {"imported": imported, "errors": errors}


@app.patch("/library/{audio_id}")
def library_patch(audio_id: int, payload: dict = Body(...)):
    """
    Edit metadata only: re-tag the MP3 in place (no re-download / no FFmpeg),
    rename the file if the name-deriving fields changed, and update the sidecar
    + DB index.
    """
    _require_library()
    detail = db.get_audio_detail(audio_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Track not found")
    return apply_metadata_edit(detail, payload)


def apply_metadata_edit(detail: dict, payload: dict) -> dict:
    """Single-track metadata edit core, shared by PATCH and batch edits."""
    audio_id = detail["id"]
    video_id = detail["youtube_id"]
    track_id = detail.get("track_id") or video_id
    sidecar = read_sidecar(track_id) or {}
    delimiter = payload.get("delimiter") or sidecar.get("metadata", {}).get("delimiter", "|")

    title = payload.get("title", detail.get("title"))
    # Albums: prefer the multi-value list; fall back to the single `album`.
    albums = payload.get("albums")
    if albums is None:
        single = payload.get("album", detail.get("album"))
        albums = detail.get("albums") if "album" not in payload else ([single] if single else [])
    albums = [a for a in (albums or []) if str(a).strip()]
    album = albums[0] if albums else None
    year = payload.get("year", detail.get("year"))
    artists = payload.get("artists", detail.get("artists", []))
    genres = payload.get("genres", detail.get("genres", []))
    custom_tags = payload.get("custom_tags")
    if custom_tags is None:
        custom_tags = [{"key": k, "value": v} for k, v in detail.get("custom_fields", {}).items()]
    composer = next((t.get("value") for t in custom_tags
                     if t.get("key", "").lower() == "composer"), None)

    artist_str = delimiter.join(artists) if artists else None
    genre_str = delimiter.join(genres) if genres else None

    old_rel = detail.get("rel_path")
    old_abs = _abs(old_rel) if old_rel else None

    # Rename if the filename-deriving fields changed. Keep the file's real
    # extension — build_filename defaults to .mp3, which would mislabel
    # FLAC/WAV/imported files.
    new_filename = build_filename(title, album, artist_str, composer, delimiter,
                                  ext=os.path.splitext(old_rel or "")[1].lstrip(".") or "mp3")
    new_abs = old_abs
    if old_abs and os.path.basename(old_abs) != new_filename:
        new_abs = get_unique_path(DOWNLOAD_DIR, new_filename)
        try:
            os.rename(old_abs, new_abs)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Rename failed: {e}")
    new_filename = os.path.basename(new_abs) if new_abs else new_filename
    new_rel = os.path.relpath(new_abs, DOWNLOAD_DIR) if new_abs else old_rel

    # Re-tag in place. Cover: only supply a new one if the client sent it;
    # otherwise the existing embedded cover is preserved.
    user_metadata = {"delimiter": delimiter}
    if title: user_metadata["title"] = title
    if artist_str: user_metadata["artist"] = artist_str
    if albums: user_metadata["album"] = delimiter.join(albums)   # embedders split: first -> ALBUM, all -> ALBUMS
    if genre_str: user_metadata["genre"] = genre_str
    if year: user_metadata["year"] = year
    if composer: user_metadata["composer"] = composer
    if custom_tags: user_metadata["custom_tags"] = custom_tags
    if payload.get("thumbnail_base64"): user_metadata["thumbnail_base64"] = payload["thumbnail_base64"]

    eff = sidecar.get("effects", detail.get("effects", {})) or {}
    if new_abs and os.path.exists(new_abs):
        try:
            retag_mp3_in_place(
                new_abs, source_url=sidecar.get("source_url", ""),
                user_metadata=user_metadata,
                eq_preset=eff.get("eq_preset"), mbc_preset=eff.get("mbc_preset"),
                normalize=eff.get("normalize", False), normalize_i=eff.get("normalize_i", -16.0),
                enhance_mode=eff.get("enhance_mode"), trim_silence=eff.get("trim_silence", False),
                original=eff.get("original", False),
            )
        except Exception as e:
            log.warning("re-tag failed: %s", e)

    # Persist sidecar + DB.
    sidecar.setdefault("youtube_id", video_id)
    sidecar.setdefault("track_id", track_id)
    sidecar["rel_path"] = new_rel
    sidecar["filename"] = new_filename
    sidecar["metadata"] = {
        "title": title, "album": album, "albums": albums, "year": year,
        "composer": composer,
        "artists": artists, "genres": genres, "delimiter": delimiter,
        "custom_tags": custom_tags,
    }
    sidecar["updated_at"] = datetime.datetime.now().isoformat()
    # Saving metadata is what "resolving" a discovered/imported track means.
    sidecar.pop("unresolved", None)
    write_sidecar(track_id, sidecar)

    db.upsert_audio(
        track_id=track_id,
        youtube_id=video_id, title=title, album=album, year=year,
        duration=sidecar.get("duration", detail.get("duration")),
        rel_path=new_rel, filename=new_filename, sidecar_path=f"{track_id}.json",
        effects=eff, artists=artists, genres=genres, albums=albums,
        custom_fields={t["key"]: t.get("value") for t in custom_tags if t.get("key")},
        unresolved=False,
    )
    return db.get_audio_detail(audio_id)


@app.post("/library/batch-edit")
def library_batch_edit(payload: dict = Body(...)):
    """
    Find-and-alter metadata across the whole library in one call.

    Ops:
      rename_key:    {op, key, new_key}          — rename a custom-tag key everywhere
      delete_key:    {op, key}                   — drop a custom-tag key everywhere
      replace_value: {op, field, match, replace} — swap one value for another on
                     artist/genre/album/composer/<custom key>; an empty replace
                     removes the value. Matching is exact per value/token,
                     case-insensitive.

    Each affected track runs through the same edit core as a manual PATCH
    (re-tag + rename + sidecar + DB), so files and index never drift.
    """
    _require_library()
    op = payload.get("op")
    affected = []

    def norm(s):
        return str(s or "").strip().lower()

    def key_of(cf, key):
        return next((k for k in cf if norm(k) == norm(key)), None)

    if op in ("rename_key", "delete_key"):
        key = (payload.get("key") or "").strip()
        new_key = (payload.get("new_key") or "").strip()
        if not key or (op == "rename_key" and not new_key):
            raise HTTPException(status_code=422, detail="key (and new_key) required")
        for it in db.get_library():
            if it.get("unresolved"):
                continue   # limbo tracks wait for manual resolution
            cf = it.get("custom_fields") or {}
            hit = key_of(cf, key)
            if hit is None:
                continue
            tags, seen = [], set()
            for k, v in cf.items():
                if k == hit:
                    if op == "delete_key":
                        continue
                    k = new_key
                if norm(k) in seen:   # rename collided with an existing key
                    continue
                seen.add(norm(k))
                tags.append({"key": k, "value": v})
            apply_metadata_edit(db.get_audio_detail(it["id"]), {"custom_tags": tags})
            affected.append(it["id"])

    elif op == "replace_value":
        field = (payload.get("field") or "").strip()
        match = (payload.get("match") or "").strip()
        replace = (payload.get("replace") or "").strip()
        if not field or not match:
            raise HTTPException(status_code=422, detail="field and match required")
        m = norm(match)
        list_fields = {"artist": "artists", "genre": "genres", "album": "albums"}
        for it in db.get_library():
            if it.get("unresolved"):
                continue   # limbo tracks wait for manual resolution
            patch = None
            if field in list_fields:
                src = it.get(list_fields[field]) or []
                if any(norm(v) == m for v in src):
                    new_list = []
                    for v in src:
                        v2 = replace if norm(v) == m else v
                        if v2 and norm(v2) not in {norm(x) for x in new_list}:
                            new_list.append(v2)
                    patch = {list_fields[field]: new_list}
            else:
                # Composer / custom keys store delimiter-joined tokens
                # (e.g. "Sad|Angry") — replace at token level.
                cf = it.get("custom_fields") or {}
                hit = key_of(cf, "Composer" if field.lower() == "composer" else field)
                if hit is not None:
                    toks = [t.strip() for t in re.split(r"[|,;]", str(cf[hit] or "")) if t.strip()]
                    if any(norm(t) == m for t in toks):
                        new_toks = []
                        for t in toks:
                            t2 = replace if norm(t) == m else t
                            if t2 and norm(t2) not in {norm(x) for x in new_toks}:
                                new_toks.append(t2)
                        tags = [{"key": k, "value": ("|".join(new_toks) if k == hit else v)}
                                for k, v in cf.items()
                                if not (k == hit and not new_toks)]
                        patch = {"custom_tags": tags}
            if patch is not None:
                apply_metadata_edit(db.get_audio_detail(it["id"]), patch)
                affected.append(it["id"])

    else:
        raise HTTPException(status_code=422,
                            detail="op must be rename_key|delete_key|replace_value")
    return {"affected": len(affected), "ids": affected}


@app.post("/library/{audio_id}/played")
def library_played(audio_id: int):
    """
    Record one play: bump the DB counters and mirror them into the sidecar
    (the sidecar is the source of truth, so stats survive a DB rebuild).
    Deliberately does NOT touch updated_at — that drives cover cache-busting.
    """
    _require_library()
    stats = db.bump_play(audio_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Track not found")
    with SIDECAR_LOCK:
        sidecar = read_sidecar(stats["track_id"])
        if sidecar is not None:
            sidecar["stats"] = {"play_count": stats["play_count"],
                                "last_played": stats["last_played"]}
            write_sidecar(stats["track_id"], sidecar)
    return {"play_count": stats["play_count"], "last_played": stats["last_played"]}


@app.patch("/library/{audio_id}/favorite")
def library_favorite(audio_id: int, payload: dict = Body(...)):
    """Set/unset favorite; mirrored into the sidecar like play stats."""
    _require_library()
    fav = bool(payload.get("favorite"))
    track_id = db.set_favorite(audio_id, fav)
    if not track_id:
        raise HTTPException(status_code=404, detail="Track not found")
    with SIDECAR_LOCK:
        sidecar = read_sidecar(track_id)
        if sidecar is not None:
            sidecar["favorite"] = fav
            write_sidecar(track_id, sidecar)
    return {"favorite": fav}


@app.post("/library/{audio_id}/reprocess")
def library_reprocess(audio_id: int, payload: dict = Body(...)):
    """
    Rebuild the MP3 from the archived original with a new effect set, keeping
    the existing metadata. Requires the archived original to exist.
    """
    _require_library()
    detail = db.get_audio_detail(audio_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Track not found")

    video_id = detail["youtube_id"]
    track_id = detail.get("track_id") or video_id
    original_path = find_original(ORIGINALS_DIR, video_id)
    if not original_path:
        raise HTTPException(status_code=409,
                            detail="No archived original — cannot reprocess this track.")

    sidecar = read_sidecar(track_id) or {}
    prev_eff = sidecar.get("effects", detail.get("effects", {})) or {}
    # Merge incoming effect overrides over the stored set.
    effects = {**prev_eff, **{k: v for k, v in payload.items() if k in (
        "start_time", "end_time", "trim_silence", "silence_thresh", "eq_preset",
        "mbc_preset", "enhance_mode", "enhance_intensity", "normalize",
        "normalize_i", "original")}}

    meta = sidecar.get("metadata", {})
    delimiter = meta.get("delimiter", "|")
    artists = meta.get("artists", detail.get("artists", []))
    genres = meta.get("genres", detail.get("genres", []))
    custom_tags = meta.get("custom_tags",
                           [{"key": k, "value": v} for k, v in detail.get("custom_fields", {}).items()])
    composer = next((t.get("value") for t in custom_tags
                     if t.get("key", "").lower() == "composer"), meta.get("composer"))

    user_metadata = {"delimiter": delimiter}
    if meta.get("title") or detail.get("title"):
        user_metadata["title"] = meta.get("title") or detail.get("title")
    if artists: user_metadata["artist"] = delimiter.join(artists)
    if meta.get("album") or detail.get("album"):
        user_metadata["album"] = meta.get("album") or detail.get("album")
    if genres: user_metadata["genre"] = delimiter.join(genres)
    if meta.get("year") or detail.get("year"):
        user_metadata["year"] = meta.get("year") or detail.get("year")
    if composer: user_metadata["composer"] = composer
    if custom_tags: user_metadata["custom_tags"] = custom_tags

    target = _abs(detail["rel_path"])
    try:
        reprocess_from_original(
            original_path, target, source_url=sidecar.get("source_url", ""),
            effects=effects, user_metadata=user_metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reprocess failed: {e}")

    sidecar["effects"] = effects
    sidecar["updated_at"] = datetime.datetime.now().isoformat()
    write_sidecar(track_id, sidecar)
    db.upsert_audio(
        track_id=track_id,
        youtube_id=video_id, title=detail.get("title"), album=detail.get("album"),
        year=detail.get("year"), duration=detail.get("duration"),
        rel_path=detail["rel_path"], filename=detail.get("filename"),
        sidecar_path=f"{track_id}.json", effects=effects,
        artists=artists, genres=genres,
        custom_fields={t["key"]: t.get("value") for t in custom_tags if t.get("key")},
    )
    return db.get_audio_detail(audio_id)


@app.delete("/library/{audio_id}")
def library_delete(audio_id: int, purge_original: bool = Query(False)):
    """Delete a track: removes the MP3 + sidecar (+ original if purge_original)."""
    _require_library()
    row = db.delete_audio(audio_id)
    if not row:
        raise HTTPException(status_code=404, detail="Track not found")
    video_id = row["youtube_id"]
    track_id = row.get("track_id") or video_id
    for path in filter(None, [
        _abs(row["rel_path"]) if row.get("rel_path") else None,
        sidecar_path_for(track_id),
    ]):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            log.warning("Failed to delete %s: %s", path, e)
    # Only purge the shared original once no other segment of the video
    # still references it.
    if purge_original and db.count_tracks_for_video(video_id) == 0:
        orig = find_original(ORIGINALS_DIR, video_id)
        if orig and os.path.exists(orig):
            try:
                os.remove(orig)
            except Exception as e:
                log.warning("Failed to delete original %s: %s", orig, e)
    return {"deleted": True}


@app.get("/library/{audio_id}/cover")
def library_cover(audio_id: int):
    """Serve a track's embedded front cover (for the library list/editor)."""
    _require_library()
    detail = db.get_audio_detail(audio_id)
    if not detail or not detail.get("rel_path"):
        raise HTTPException(status_code=404, detail="Track not found")
    cover = read_cover(_abs(detail["rel_path"]))
    if not cover:
        raise HTTPException(status_code=404, detail="No cover")
    data, mime = cover
    # URL is versioned with ?v=updated_at, so cache hard (avoids the Media
    # Session artwork re-fetching the cover repeatedly during playback).
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/library/{audio_id}/audio")
def library_audio(audio_id: int):
    """Stream a saved track for in-library playback (seekable). Library files may
    be MP3/FLAC/WAV now, so pick the media type from the extension."""
    _require_library()
    detail = db.get_audio_detail(audio_id)
    if not detail or not detail.get("rel_path"):
        raise HTTPException(status_code=404, detail="Track not found")
    path = _abs(detail["rel_path"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File missing")
    # Prefer a fresh SSD hot-tier copy so playback leaves the HDD idle.
    hot = os.path.join(HOT_DIR, (detail.get("track_id") or "") + os.path.splitext(path)[1])
    try:
        if (detail.get("track_id") and os.path.exists(hot)
                and os.path.getsize(hot) == os.path.getsize(path)
                and os.path.getmtime(hot) >= os.path.getmtime(path)):
            path = hot
    except OSError:
        pass
    mime = {"flac": "audio/flac", "wav": "audio/wav", "mp3": "audio/mpeg"}.get(
        os.path.splitext(path)[1].lower().lstrip("."), "audio/mpeg")
    return FileResponse(path, media_type=mime)


@app.get("/cache/status")
def cache_status():
    """Current on-disk state of the SSD hot tier."""
    _require_library()
    used = count = 0
    for f in os.listdir(HOT_DIR):
        try:
            used += os.path.getsize(os.path.join(HOT_DIR, f))
            count += 1
        except OSError:
            pass
    return {"budget": HOT_CACHE_BYTES, "used": used, "tracks": count}


@app.post("/cache/refresh")
def cache_refresh():
    """Recompute the plan and sync the SSD hot tier now."""
    _require_library()
    return refresh_hot_cache()


@app.get("/cache-plan")
def cache_plan():
    """
    Tracks a client should keep cached locally (service worker prefetch);
    same favorites/most-played/recent policy as the server hot tier. The v
    param carries updated_at so re-processed tracks get re-fetched.
    """
    _require_library()
    return {"tracks": [
        {"id": p["id"],
         "url": f"/library/{p['id']}/audio?v={p['updated_at'] or ''}",
         "size": p["size"]}
        for p in compute_cache_plan()]}


@app.get("/sw.js")
def service_worker():
    """Service worker must be served from the root path to get '/' scope."""
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.post("/preview-cache/clear")
def clear_preview_cache(url: Optional[str] = Query(None)):
    """
    Drop cached preview renders (the per-effect 'mix' MP3s). Called on unload so
    a session's experiments don't linger on the SSD. With ?url=, clears just that
    video's mixes; otherwise clears all. The source cache is left intact.
    """
    import glob
    removed = 0
    try:
        if url:
            try:
                vid = validate_youtube_url(url)
            except Exception:
                return {"removed": 0}
            patterns = [os.path.join(CACHE_DIR, f"prev_{vid}_*"),
                        os.path.join(CACHE_DIR, "ckpt", f"base_{vid}.wav"),
                        os.path.join(CACHE_DIR, "ckpt", f"ck_{vid}_*.wav")]
        else:
            patterns = [os.path.join(CACHE_DIR, "prev_*"),
                        os.path.join(CACHE_DIR, "ckpt", "*.wav")]
        for pattern in patterns:
            for f in glob.glob(pattern):
                try:
                    os.remove(f)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Playlists (save-dir mode). Definitions persist as JSON sidecars under
# .youtify/playlists/ so they survive a DB rebuild; the DB is just the index.
# ---------------------------------------------------------------------------

def _save_playlist(pid, *, name, kind, filters, sort, track_ids, has_cover, position=None):
    """Write the sidecar + upsert the DB index together. Position is preserved
    unless explicitly given (new playlists append at the end)."""
    if position is None:
        existing = db.get_playlist(pid)
        position = existing["position"] if existing else len(db.list_playlists())
    data = {
        "id": pid, "name": name, "kind": kind,
        "filters": filters or [], "sort": sort or {},
        "track_ids": track_ids or [], "has_cover": bool(has_cover),
        "position": position,
        "updated_at": datetime.datetime.now().isoformat(),
    }
    write_playlist_sidecar(pid, data)
    db.upsert_playlist(id=pid, name=name, kind=kind, filters=filters, sort=sort,
                       has_cover=has_cover, track_ids=track_ids, position=position)
    return data


@app.post("/playlists/reorder")
def playlists_reorder(payload: dict = Body(...)):
    _require_library()
    ids = payload.get("ids") or []
    for i, pid in enumerate(ids):
        pl = db.get_playlist(pid)
        if not pl:
            continue
        _save_playlist(pid, name=pl["name"], kind=pl["kind"], filters=pl["filters"],
                       sort=pl["sort"], track_ids=pl["track_ids"], has_cover=pl["has_cover"],
                       position=i)
    return {"ok": True}


@app.get("/playlists")
def playlists_list():
    _require_library()
    return {"items": db.list_playlists()}


@app.get("/playlists/{pid}")
def playlist_detail(pid: str):
    _require_library()
    pl = db.get_playlist(pid)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return pl


@app.post("/playlists")
def playlist_create(payload: dict = Body(...)):
    _require_library()
    pid = uuid.uuid4().hex[:12]
    name = (payload.get("name") or "Untitled").strip() or "Untitled"
    kind = "dynamic" if payload.get("kind") == "dynamic" else "manual"
    filters = payload.get("filters") or []
    sort = payload.get("sort") or {}
    has_cover = False
    if payload.get("cover_base64"):
        try:
            data, _ = normalize_cover(base64.b64decode(payload["cover_base64"]), max_side=600)
            if data:
                with open(playlist_cover_path(pid), "wb") as fh:
                    fh.write(data)
                has_cover = True
        except Exception as e:
            log.warning("playlist cover save failed: %s", e)
    return _save_playlist(pid, name=name, kind=kind, filters=filters, sort=sort,
                          track_ids=[], has_cover=has_cover)


@app.patch("/playlists/{pid}")
def playlist_update(pid: str, payload: dict = Body(...)):
    _require_library()
    pl = db.get_playlist(pid)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    name = (payload.get("name") if payload.get("name") is not None else pl["name"]).strip() or pl["name"]
    kind = payload.get("kind") or pl["kind"]
    filters = payload.get("filters") if payload.get("filters") is not None else pl["filters"]
    sort = payload.get("sort") if payload.get("sort") is not None else pl["sort"]
    has_cover = pl["has_cover"]
    if payload.get("cover_base64"):
        try:
            data, _ = normalize_cover(base64.b64decode(payload["cover_base64"]), max_side=600)
            if data:
                with open(playlist_cover_path(pid), "wb") as fh:
                    fh.write(data)
                has_cover = True
        except Exception as e:
            log.warning("playlist cover save failed: %s", e)
    return _save_playlist(pid, name=name, kind=kind, filters=filters, sort=sort,
                          track_ids=pl["track_ids"], has_cover=has_cover)


@app.delete("/playlists/{pid}")
def playlist_delete(pid: str):
    _require_library()
    if not db.delete_playlist(pid):
        raise HTTPException(status_code=404, detail="Playlist not found")
    for p in (playlist_sidecar_path(pid), playlist_cover_path(pid)):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception as e:
            log.warning("Failed to delete %s: %s", p, e)
    return {"deleted": True}


@app.post("/playlists/{pid}/tracks")
def playlist_add_track(pid: str, payload: dict = Body(...)):
    _require_library()
    pl = db.get_playlist(pid)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    tid = payload.get("track_id") or payload.get("youtube_id")
    if not tid:
        raise HTTPException(status_code=422, detail="track_id required")
    ids = pl["track_ids"]
    if tid not in ids:
        ids.append(tid)
    return _save_playlist(pid, name=pl["name"], kind=pl["kind"], filters=pl["filters"],
                          sort=pl["sort"], track_ids=ids, has_cover=pl["has_cover"])


@app.delete("/playlists/{pid}/tracks/{track_id}")
def playlist_remove_track(pid: str, track_id: str):
    _require_library()
    pl = db.get_playlist(pid)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    ids = [x for x in pl["track_ids"] if x != track_id]
    return _save_playlist(pid, name=pl["name"], kind=pl["kind"], filters=pl["filters"],
                          sort=pl["sort"], track_ids=ids, has_cover=pl["has_cover"])


@app.get("/playlists/{pid}/cover")
def playlist_cover(pid: str):
    _require_library()
    path = playlist_cover_path(pid)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No cover")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


# --- Facet cover endpoints (custom Browse-by thumbnails) ---

@app.get("/facets")
def facets_list():
    """Which facet values have a custom cover: {field: [value, ...]}."""
    _require_library()
    fields = set(FACET_FIELDS)
    if FACETS_DIR and os.path.isdir(FACETS_DIR):
        fields.update(d for d in os.listdir(FACETS_DIR)
                      if os.path.isdir(os.path.join(FACETS_DIR, d)))
    return {f: sorted(_facet_index_read(f).values()) for f in sorted(fields)}


@app.get("/facets/{field}/{value}/cover")
def facet_cover(field: str, value: str):
    _require_library()
    path = facet_cover_path(field, value)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No cover")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.put("/facets/{field}/{value}/cover")
def facet_cover_put(field: str, value: str, payload: dict = Body(...)):
    _require_library()
    b64 = payload.get("cover_base64")
    if not b64:
        raise HTTPException(status_code=422, detail="cover_base64 required")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid base64 image")
    data, _mime = normalize_cover(raw)
    if not data:
        raise HTTPException(status_code=422, detail="Could not decode image")
    save_facet_cover(field, value, data)
    return {"status": "ok"}


@app.delete("/facets/{field}/{value}/cover")
def facet_cover_delete(field: str, value: str):
    _require_library()
    slug = _facet_slug(value)
    path = os.path.join(_facet_dir(field), f"{slug}.jpg")
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
    idx = _facet_index_read(field)
    if slug in idx:
        idx.pop(slug)
        _facet_index_write(field, idx)
    return {"status": "ok"}


def fetch_artist_pfp_task(url: str, artist: str):
    """
    Background task at save time: if the saved track's first artist IS the
    YouTube channel uploader and that artist has no facet cover yet, pull the
    channel avatar as the artist's default image. Best-effort — any failure
    just means the facet keeps its track-cover fallback.
    """
    try:
        if os.path.exists(facet_cover_path("artist", artist)):
            return
        info = get_video_info(url)
        if (info.get("author") or "").strip().lower() != artist.strip().lower():
            return  # saved artist isn't the channel — don't guess
        data = fetch_channel_avatar(info.get("channel_id"), max_side=600)
        if data:
            save_facet_cover("artist", artist, data)
            log.info("Saved channel avatar as default image for artist %r", artist)
    except Exception as e:
        log.warning("artist pfp fetch failed: %s", e)


@app.get("/suggestions")
def suggestions(kind: Optional[str] = Query(None), field: Optional[str] = Query(None),
                q: str = Query("")):
    """
    Autocomplete sourced from the library.
      kind=artist|genre              -> normalized tags
      field=album|year|composer|<k>  -> distinct values for that field/custom key
      field=__keys__                 -> distinct custom-tag key names
    """
    if kind in ("artist", "genre"):
        return {"suggestions": db.suggest_tags(kind, q)}
    if field in ("artist", "genre"):
        return {"suggestions": db.suggest_tags(field, q)}
    if field == "album":
        # Union of multi-value album tags and the legacy single-album column.
        out, seen = [], set()
        for v in db.suggest_tags("album", q) + db.suggest_values("album", q):
            if v.lower() not in seen:
                seen.add(v.lower()); out.append(v)
        return {"suggestions": out[:10]}
    if field == "__keys__":
        return {"suggestions": db.suggest_custom_keys(q)}
    if field:
        return {"suggestions": db.suggest_values(field, q)}
    raise HTTPException(status_code=422, detail="provide kind=artist|genre or field=<name>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
