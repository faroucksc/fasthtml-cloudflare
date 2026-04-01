# FastHTML + Python DORM on Cloudflare Workers

Full-stack FastHTML with per-tenant Durable Object SQLite — the Jeremy Howard stack running on Cloudflare's edge.

## What this is

Each tenant (user, client, sub-account) gets their own **Durable Object** with a private **10GB SQLite database**. Your FastHTML Worker routes requests to the right DO. Data is persistent, consistent, and isolated — no R2 serialize/deserialize, no last-write-wins.

**pydorm** is a MiniDataAPI-compatible wrapper for Durable Object SQLite. Same interface as fastlite:

```python
from pydorm import DormDB
from dataclasses import dataclass

@dataclass
class Todo:
    id:    int = None
    title: str = ''
    done:  bool = False

db = DormDB(ctx.storage.sql)
todos = db.create(Todo, pk='id')

todos.insert(Todo(title='Buy milk'))
todos[1]                            # → Todo(id=1, ...)
todos()                             # → [Todo(...), ...]
todos.update(Todo(id=1, done=True))
todos.delete(1)
```

## Architecture

```
Browser → FastHTML Worker (edge, any location, stateless)
        → Durable Object "tenant:acme" (single location, owns SQLite)
        → pydorm (MiniDataAPI) → ctx.storage.sql (10GB, persistent, consistent)

/acme    → DO "tenant:acme"    → acme's private SQLite
/berens  → DO "tenant:berens"  → berens' private SQLite
/client-47 → DO "tenant:client-47" → client-47's private SQLite
```

## Quickstart

```bash
uv sync
uv run pywrangler sync
uv run pywrangler dev      # local dev
uv run pywrangler deploy   # deploy globally
```

## Project structure

```
src/
├── worker.py     # FastHTML Worker + TenantDB Durable Object
└── pydorm.py     # MiniDataAPI for DO SQLite (drop-in fastlite replacement)
```

## Workers-specific rules

All handlers must be `async def`. Exception handlers must be `async def`. No session middleware (`sess_cls=None`). Use `fast_app()` with the right params.

## What needed fixing (from the original FastHTML-on-Workers discovery)

| Fix | Why |
|-----|-----|
| `override-dependencies = ["uvicorn>=0.30"]` | Strip httptools C extension |
| `python_dedicated_snapshot` compat flag | Snapshot serialization |
| `secret_key="..."` explicit | No os.urandom at startup |
| `sess_cls=None` | Session middleware incompatible |
| `async def` everything | Workers has no threadpool |
| `async def _not_found` | Workers ASGI needs async exception handlers |

## pydorm vs fastlite

| | fastlite | pydorm |
|---|---------|--------|
| Backend | apsw (local SQLite) | ctx.storage.sql (DO SQLite) |
| Persistence | File on disk or memory | Durable, survives restarts |
| Max size | Disk space | 10GB per DO |
| Multi-tenant | One DB | Unlimited DOs, one per tenant |
| Interface | MiniDataAPI | MiniDataAPI (same) |
| PITR | No | 30-day point-in-time recovery built in |

## Credits

FastHTML by Jeremy Howard / Answer.AI. Cloudflare Python Workers + Durable Objects. Discovered March 2026.
