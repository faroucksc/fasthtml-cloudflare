# FastHTML on Cloudflare Workers

Run [FastHTML](https://fastht.ml) on Cloudflare's edge network via Python Workers (Pyodide/WebAssembly).

Full stack: **fast_app()** + **FT components** + **HTMX** + **fastlite/MiniDataAPI** + **apsw SQLite (Wasm)**.

## Quickstart

```bash
# Prerequisites: uv, node
uv sync
uv run pywrangler sync
uv run pywrangler dev      # local dev
uv run pywrangler deploy   # deploy to Cloudflare edge (330+ locations)
```

## What works

- `fast_app()` with Workers-compatible params
- FastHTML native routing (`@rt('/')`) and FT rendering pipeline
- FT components (`Titled`, `P`, `Ul`, `Li`, etc.) with full page shell
- HTMX auto-loaded by FastHTML (interactive add/toggle/delete)
- fastlite + MiniDataAPI spec with apsw SQLite compiled to Wasm
- JSON API responses via FastHTML's `_resp` Mapping detection

## What needed fixing

Six Workers-specific constraints:

| # | Blocker | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | `httptools` C extension | No Wasm wheel for Pyodide | `[tool.uv] override-dependencies = ["uvicorn>=0.30"]` — strips `[standard]`, uvicorn falls back to `h11` |
| 2 | Snapshot serialization | Vendored packages create JS refs | `python_dedicated_snapshot` compatibility flag in wrangler config |
| 3 | `os.urandom()` at startup | Entropy blocked outside request context | Pass `secret_key="..."` explicitly |
| 4 | Session middleware | `SessionMiddleware` incompatible with Workers ASGI | `sess_cls=None` |
| 5 | Sync 404 handler | Workers ASGI requires async exception handlers | Pass `exception_handlers={404: async_handler}` |
| 6 | Sync route handlers | `run_in_threadpool()` — Workers is single-threaded | **All handlers must be `async def`** |

## Architecture

```
Request → Cloudflare Edge (330+ locations)
        → Python Worker (Pyodide/WebAssembly isolate)
        → asgi.fetch() → FastHTML ASGI app
        → FT components → HTML response
        → fastlite/apsw → SQLite (in-memory, Wasm)
```

## Key patterns

### fast_app() works — with the right params

```python
async def _not_found(req, exc):
    return HTMLResponse('404', status_code=404)

app, rt = fast_app(
    secret_key='...',
    sess_cls=None,
    live=False,
    exception_handlers={404: _not_found},
    db=False,
)
```

### All handlers must be async

```python
# ✅ Works
@rt('/')
async def home(): return Titled('Hello', P('World'))

# ❌ Crashes (run_in_threadpool fails)
@rt('/')
def home(): return Titled('Hello', P('World'))
```

### Workers entrypoint with lazy init

```python
_app = None
def get_app():
    global _app
    if _app is not None: return _app
    # ... create app ...
    return _app

class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi
        return await asgi.fetch(get_app(), request.js_object, self.env)
```

## Limitations

- **No session middleware** — use Workers KV, D1, or signed cookies
- **In-memory SQLite resets on cold start** — use Cloudflare D1 for persistence
- **No file system writes** — Workers is read-only outside R2/KV/D1
- **Cold start ~2-3s** on first request (Pyodide importing FastHTML + deps)
- **Subsequent requests fast** while isolate is alive (singleton + Workers sharding)

## Credits

Discovered and tested March 2026. Uses [FastHTML](https://fastht.ml) by Jeremy Howard / Answer.AI, [Cloudflare Python Workers](https://developers.cloudflare.com/workers/languages/python/), [fastlite](https://github.com/AnswerDotAI/fastlite), and [apsw](https://github.com/rogerbinns/apsw) compiled to Wasm.
