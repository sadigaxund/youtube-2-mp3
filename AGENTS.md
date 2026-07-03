# Youtify — Code map for AI agents

## Architecture

FastAPI app serving a single-page frontend from `static/`. Two modes: **Browser Download** (streams to browser) and **Server Save** (writes to disk with SQLite index).

## Static files (frontend)

All JS is **global-scope, no modules** — loaded via `<script>` tags in order
(app → pipeline → thumbnail → metadata → mixer → preview → search → library).
Functions freely cross-reference across files without import/export.

| File | Responsibility |
|---|---|
| `index.html` | HTML skeleton only |
| `style.css` | All styles (single file) |
| `app.js` | State vars, `els` DOM references, utils (`setLoading`, `showError`, `showToast`) |
| `pipeline.js` | Download progress steps, process flow |
| `thumbnail.js` | Cover uploader (`wireCover`) |
| `metadata.js` | Tags, multi-value chip inputs (artist/genre/album/custom), autocomplete + preset tag keys, copy-metadata picker, Output & technical panel (format flow, tag separator, custom filename) |
| `search.js` | YouTube search, file upload, `onSourceReady` |
| `mixer.js` | A/B compare snapshots |
| `preview.js` | Range slider, audio player, OS media session, `onEffectChange` |
| `library.js` | Library view, playlists, filter/sort, Browse-by facet grid + hero + editable facet covers, play queue, sleep timer, stats/favorites (IIFE-wrapped) |

**Key patterns:**
- `els.xxx` = cached `document.getElementById('xxx')`
- `setLoading(btn, bool)` / `showError(msg)` / `showToast(path)` — global utils
- `lucide.createIcons()` called after any HTML icon change
- `debounce(fn, ms)` for autocomplete

## Backend (`main.py`)

- `GET /` serves `static/index.html`
- `GET /stream` — live preview with effects (FLAC cached; `quality=fast` → 128k MP3)
- `POST /save` — download + process + save
- `POST /upload` — local file ingest (stages one file for the download form)
- `POST /library/import` — drop-zone batch ingest straight into the library as unresolved tracks
- `POST /library/batch-edit` — find-match-alter across the library (rename_key / delete_key / replace_value)
- `GET|POST|PATCH|DELETE /library/*` — saved tracks; `POST /library/{id}/played` + `PATCH /library/{id}/favorite` for stats (sidecar-backed, survive DB rebuilds)
- `GET|POST|PATCH|DELETE /playlists/*` — playlists
- `GET|PUT|DELETE /facets/{field}/{value}/cover` — custom Browse-by thumbnails (`.youtify/facets/`)

## Compatibility policy

- Sidecars (`.youtify/meta/*.json`) are the source of truth; `metadata.db` is a disposable index rebuilt from them on startup. Tracks are keyed by `track_id` = `youtube_id__<hash8(cut + filename-base)>` — identity is source video + cut range + name-deriving metadata, so a different title/artist/album/composer (or custom filename) makes a sibling track sharing one archived original, while a same-name re-save updates in place (and replaces the audio file). Legacy ids (`youtube_id` plain / `youtube_id__<hash8(cut)>`) remain valid via a read-time fallback in `resolve_track_id`; discovered/imported local files use `loc<hash12>`. `POST /save/peek` dry-runs the identity so the UI can warn before overwriting. Discovery of unindexed audio files is manual-only (`POST /library/discover`, top-level by default, `?recursive=true` opt-in) and adopts hits as `unresolved` stub sidecars.
- New sidecar keys are **additive** and read with `.get(...)` defaults; DB migrations are additive `try: ALTER TABLE` statements. Updates must never require re-downloading tracks.
- A breaking sidecar change bumps `schema_version` and ships a read-time migration.

## Docker / CI

- `Dockerfile` — `python:3.13-slim`, installs via `pip install .` (pyproject.toml)
- `.github/workflows/docker-image.yml` — builds on `v*.*.*` tag push
- Entrypoint (`entrypoint.sh`) handles UID/GID mapping for volume permissions

## Dependency management

- `pyproject.toml` (PEP 621) — single source of truth. `pip install .` to install.
- Key deps: `fastapi`, `yt-dlp`, `pydub`, `mutagen`, `Pillow`, `python-multipart`
