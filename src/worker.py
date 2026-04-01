"""FastHTML on Cloudflare Workers — Todo app with HTMX + fastlite.

This is the reference implementation for running FastHTML on Cloudflare's
edge network via Python Workers (Pyodide/WebAssembly).

Four Workers-specific constraints and their fixes:
  1. All route handlers must be `async def` (no threadpool in Workers)
  2. Exception handlers must be `async def`
  3. Session middleware disabled (sess_cls=None) — use Workers KV/D1 instead
  4. App created lazily at first request (avoids snapshot issues)
"""
from workers import WorkerEntrypoint, Response
from starlette.responses import HTMLResponse
import traceback

# -- Lazy init (created on first request, cached for isolate lifetime) --------
_app = None

def get_app():
    global _app
    if _app is not None: return _app

    from fasthtml.core import FastHTML
    from fasthtml.common import *
    from fastlite import database
    from dataclasses import dataclass

    # In-memory SQLite via apsw (compiled to Wasm). Resets on cold start.
    # For persistence, swap to D1 binding or R2-backed SQLite.
    db = database(':memory:')

    @dataclass
    class Todo:
        id:    int = None
        title: str = ''
        done:  bool = False

    todos = db.create(Todo, pk='id')

    # Seed
    todos.insert(Todo(title='Deploy FastHTML on Workers', done=True))
    todos.insert(Todo(title='Add HTMX interactions', done=True))
    todos.insert(Todo(title='Wire up D1 for persistence'))

    # -- Helpers (Jeremy Howard style: small functions, FT returns) ------------
    def tid(id): return f'todo-{id}'

    def mk_todo(t):
        "Render a single todo as an <li> with toggle + delete via HTMX."
        done_cls = 'done' if t.done else ''
        return Li(
            A('✓', hx_put=f'/toggle/{t.id}', hx_target=f'#{tid(t.id)}',
              hx_swap='outerHTML', cls=f'toggle {done_cls}', href='#'),
            Span(t.title, cls=done_cls),
            A('✕', hx_delete=f'/todo/{t.id}', hx_target=f'#{tid(t.id)}',
              hx_swap='outerHTML', cls='delete', href='#'),
            id=tid(t.id),
        )

    def mk_input():
        return Form(
            Input(name='title', placeholder='What needs doing?', autofocus=True,
                  cls='todo-input'),
            Button('Add', type='submit'),
            hx_post='/todo', hx_target='#todo-list', hx_swap='beforeend',
            hx_on__after_request="this.reset()",
            cls='todo-form',
        )

    def mk_page(content):
        return Title('FastHTML + Workers'), Style(CSS), content

    CSS = """
    body { font-family: system-ui; max-width: 600px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.5rem; }
    ul { list-style: none; padding: 0; }
    li { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0;
         border-bottom: 1px solid #eee; }
    .toggle { text-decoration: none; font-size: 1.2rem; opacity: 0.3; }
    .toggle.done { opacity: 1; color: green; }
    .done { text-decoration: line-through; opacity: 0.5; }
    .delete { text-decoration: none; color: #c00; margin-left: auto; font-size: 0.9rem; }
    .todo-form { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
    .todo-input { flex: 1; padding: 0.4rem; font-size: 1rem; }
    button { padding: 0.4rem 1rem; }
    code { background: #f4f4f4; padding: 0.15rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }
    .meta { margin-top: 2rem; font-size: 0.85rem; color: #666; }
    """

    # -- Workers-compatible 404 (must be async) -------------------------------
    async def _not_found(req, exc):
        return HTMLResponse('404 Not Found', status_code=404)

    # -- App ------------------------------------------------------------------
    _app = FastHTML(
        secret_key='change-me-in-production',
        sess_cls=None,        # no session middleware (Workers compat)
        live=False,            # no live reload
        exception_handlers={404: _not_found},
    )
    rt = _app.route

    # -- Routes (all async — Workers has no threadpool) -----------------------
    @rt('/')
    async def home():
        items = [mk_todo(t) for t in todos()]
        return mk_page(
            Div(
                H1('FastHTML on Cloudflare Workers'),
                mk_input(),
                Ul(*items, id='todo-list'),
                Div(
                    P('Stack: ', Code('python-fasthtml'), ' + ', Code('fastlite'),
                      ' + ', Code('apsw (Wasm)'), ' + ', Code('HTMX')),
                    P('Runtime: Pyodide on Cloudflare Workers edge network'),
                    A('About', href='/about'), ' · ',
                    A('API', href='/api'),
                    cls='meta',
                ),
            )
        )

    @rt('/todo', methods=['post'])
    async def add_todo(title: str):
        if not title.strip(): return ''
        t = todos.insert(Todo(title=title.strip()))
        return mk_todo(t)

    @rt('/toggle/{id}', methods=['put'])
    async def toggle(id: int):
        t = todos[id]
        todos.update(Todo(id=id, title=t.title, done=not t.done))
        return mk_todo(todos[id])

    @rt('/todo/{id}', methods=['delete'])
    async def delete(id: int):
        todos.delete(id)
        return ''

    @rt('/about')
    async def about():
        return mk_page(
            Div(
                H1('About'),
                P('Full FastHTML running on Cloudflare Workers via Pyodide (WebAssembly).'),
                P('This template demonstrates:'),
                Ul(
                    Li('FastHTML native routing + FT rendering pipeline'),
                    Li('fastlite / MiniDataAPI spec with apsw SQLite (compiled to Wasm)'),
                    Li('HTMX interactions (add, toggle, delete — no page reloads)'),
                    Li('Lazy app initialization for Workers snapshot compatibility'),
                ),
                A('← Home', href='/'),
                cls='meta',
            )
        )

    @rt('/api')
    async def api():
        return {
            'status': 'ok',
            'framework': 'fasthtml',
            'runtime': 'cloudflare-workers-python',
            'todos': len(todos()),
        }

    return _app


# -- Workers entrypoint -------------------------------------------------------
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            import asgi
            return await asgi.fetch(get_app(), request.js_object, self.env)
        except BaseException as e:
            return Response(f'{type(e).__name__}: {e}\n{traceback.format_exc()}', status=500)
