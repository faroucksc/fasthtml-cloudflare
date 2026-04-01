"""FastHTML on Cloudflare Workers with R2-backed SQLite persistence.

Lifecycle:
  1. Cold start → GET db.sqlite from R2 → deserialize into memory
  2. Requests   → fastlite read/write against in-memory SQLite (fast)
  3. On write   → serialize → PUT back to R2 (durable)

Workers-specific rules:
  - All route handlers must be `async def`
  - Exception handlers must be `async def`
  - sess_cls=None (no session middleware)
  - Lazy app init (snapshot compat)
"""
from workers import WorkerEntrypoint, Response
from starlette.responses import HTMLResponse
import traceback

_app = None
_env = None  # Workers env for R2 binding access

# -- R2-backed SQLite ---------------------------------------------------------
async def load_db_from_r2(env):
    "Pull serialized SQLite bytes from R2, deserialize into fastlite database."
    from fastlite import database
    db = database(':memory:')
    try:
        obj = await env.DB_BUCKET.get('db.sqlite')
        if obj:
            buf = await obj.arrayBuffer()
            # Convert JS ArrayBuffer to Python bytes
            from js import Uint8Array
            arr = Uint8Array.new(buf)
            db_bytes = bytes(arr)
            if len(db_bytes) > 0:
                db.conn.deserialize('main', db_bytes)
    except Exception as e:
        print(f'R2 load skipped: {e}')  # first run, no db yet
    return db


async def save_db_to_r2(db, env):
    "Serialize in-memory SQLite and PUT to R2."
    try:
        db_bytes = db.conn.serialize('main')
        from js import Uint8Array
        arr = Uint8Array.new(len(db_bytes))
        for i, b in enumerate(db_bytes):
            arr[i] = b
        await env.DB_BUCKET.put('db.sqlite', arr)
    except Exception as e:
        print(f'R2 save failed: {e}')


# -- App factory --------------------------------------------------------------
def get_app():
    global _app
    if _app is not None: return _app

    from fasthtml.common import *
    from dataclasses import dataclass

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

    async def _not_found(req, exc):
        return HTMLResponse('404 Not Found', status_code=404)

    _app, rt = fast_app(
        secret_key='change-me-in-production',
        sess_cls=None, live=False,
        exception_handlers={404: _not_found},
        hdrs=[Style(CSS)],
        db=False,
    )

    @dataclass
    class Todo:
        id:    int = None
        title: str = ''
        done:  bool = False

    # -- DB holder (loaded from R2 on first request per isolate) -----
    _state = {}

    async def get_db():
        if 'db' not in _state:
            _state['db'] = await load_db_from_r2(_env)
            db = _state['db']
            # Ensure table exists (first run or empty DB)
            db.create(Todo, pk='id', if_not_exists=True)
        return _state['db']

    async def get_todos():
        db = await get_db()
        return db.t.todo

    # -- Helpers ---
    def tid(id): return f'todo-{id}'

    def mk_todo(t):
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
            hx_on__after_request="this.reset()", cls='todo-form',
        )

    # -- Routes (all async) -------------------------------------------
    @rt('/')
    async def home():
        todos = await get_todos()
        items = [mk_todo(t) for t in todos()]
        return Titled('FastHTML + R2 SQLite',
            mk_input(),
            Ul(*items, id='todo-list'),
            Div(P('SQLite persisted to R2 — survives cold starts.'), cls='meta'),
        )

    @rt('/todo', methods=['post'])
    async def add_todo(title: str):
        if not title.strip(): return ''
        todos = await get_todos()
        t = todos.insert(Todo(title=title.strip()))
        db = await get_db()
        await save_db_to_r2(db, _env)
        return mk_todo(t)

    @rt('/toggle/{id}', methods=['put'])
    async def toggle(id: int):
        todos = await get_todos()
        t = todos[id]
        todos.update(Todo(id=id, title=t.title, done=not t.done))
        db = await get_db()
        await save_db_to_r2(db, _env)
        return mk_todo(todos[id])

    @rt('/todo/{id}', methods=['delete'])
    async def delete(id: int):
        todos = await get_todos()
        todos.delete(id)
        db = await get_db()
        await save_db_to_r2(db, _env)
        return ''

    @rt('/api')
    async def api():
        todos = await get_todos()
        return {'status': 'ok', 'todos': len(todos()), 'persistence': 'r2'}

    return _app


# -- Workers entrypoint -------------------------------------------------------
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        global _env
        _env = self.env  # capture env for R2 binding access
        try:
            import asgi
            return await asgi.fetch(get_app(), request.js_object, self.env)
        except BaseException as e:
            return Response(f'{type(e).__name__}: {e}\n{traceback.format_exc()}', status=500)
