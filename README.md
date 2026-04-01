# FastHTML on Cloudflare Workers

Run [FastHTML](https://fastht.ml) on Cloudflare's edge network via Python Workers (Pyodide/WebAssembly).

Full stack: **FastHTML routing** + **FT components** + **HTMX** + **fastlite/MiniDataAPI** + **apsw SQLite (Wasm)**.

## Quickstart

```bash
# Prerequisites: uv, node
uv sync
uv run pywrangler sync
uv run pywrangler dev      # local dev
uv run pywrangler deploy   # deploy to Cloudflare edge (330+ locations)
```

## What works

- FastHTML native routing (`@rt('/')`)
- FT component rendering pipeline (`Titled`, `P`, `Ul`, `Li`, etc.)
- HTMX auto-loaded by FastHTML (interactive add/toggle/delete)
- fastlite + MiniDataAPI spec with apsw SQLite compiled to Wasm
- JSON API responses via FastHTML's `_resp` Mapping detection
- Full `<!DOCTYPE html>` page rendering with htmx.js, surreal.js

## What needed fixing

Six Workers-specific constraints discovered through trial and error:

| # | Blocker | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | `httptools` C extension | No Wasm wheel for Pyodide | `[tool.uv] override-dependencies = ["uvicorn>=0.30"]` — strips `[standard]` extra, uvicorn falls back to `h11` |
| 2 | Snapshot serialization | Vendored packages create JS refs | `python_dedicated_snapshot` compatibility flag in wrangler config |
| 3 | `os.urandom()` at startup | Entropy blocked outside request context | Pass `secret_key="..."` explicitly to avoid `uuid.uuid4()` |
| 4 | Session middleware | `SessionMiddleware` incompatible with Workers ASGI | `sess_cls=None` |
| 5 | Sync 404 handler | Workers ASGI requires async exception handlers | `async def _not_found(req, exc)` |
| 6 | Sync route handlers | `run_in_threadpool()` fails — Workers is single-threaded | **All handlers must be `async def`** |

## Architecture

```
Request → Cloudflare Edge (330+ locations)
        → Python Worker (Pyodide/WebAssembly isolate)
        → asgi.fetch() → FastHTML ASGI app
        → FT components → HTML response
        → fastlite/apsw → SQLite (in-memory, Wasm)
```

**Lazy initialization**: The FastHTML app is created on first request and cached
for the isolate's lifetime. This avoids snapshot serialization issues and lets
the memory snapshot capture only the import state.

**In-memory SQLite**: apsw is compiled to Wasm by Pyodide, giving you real SQLite
in a serverless environment. The database resets on cold start. For persistence,
use Cloudflare D1 (a binding-accessible SQLite database) or R2.

## Project structure

```
├── src/worker.py       # Single-file FastHTML app (Jeremy Howard style)
├── pyproject.toml      # Dependencies + uvicorn override trick
├── wrangler.jsonc      # Workers config with compat flags
└── README.md
```

## Key patterns for Workers

```python
# ✅ All handlers async
@rt('/')
async def home(): return Titled('Hello', P('World'))

# ❌ Sync handlers crash (run_in_threadpool fails)
@rt('/')
def home(): return Titled('Hello', P('World'))
```

```python
# ✅ Async exception handlers
async def _not_found(req, exc):
    return HTMLResponse('404', status_code=404)

# ❌ Sync exception handlers crash
def _not_found(req, exc):
    return HTMLResponse('404', status_code=404)
```

```python
# ✅ Workers-compatible FastHTML init
app = FastHTML(
    secret_key='...',      # explicit (no os.urandom at startup)
    sess_cls=None,         # no session middleware
    live=False,            # no live reload
    exception_handlers={404: _not_found},  # async handler
)
```

## Limitations

- **No `fast_app()`** — use `FastHTML()` directly with the fixes above
- **No session middleware** — use Workers KV, D1, or cookie-based auth instead
- **In-memory SQLite resets on cold start** — use D1 for persistence
- **No file system writes** — Workers is read-only outside of R2/KV/D1
- **Cold start ~2-3s** on first request (FastHTML + deps import via Pyodide)
- **Subsequent requests ~10-50ms** (isolate reuse with sharding)

## Credits

Discovered and tested March 2026. Uses:
- [FastHTML](https://fastht.ml) by Jeremy Howard / Answer.AI
- [Cloudflare Python Workers](https://developers.cloudflare.com/workers/languages/python/) (Pyodide + WebAssembly)
- [fastlite](https://github.com/AnswerDotAI/fastlite) / MiniDataAPI spec
- [apsw](https://github.com/nickmanning/apsw) SQLite compiled to Wasm by Pyodide
