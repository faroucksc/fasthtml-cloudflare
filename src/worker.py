"""FastHTML + Python DORM — per-tenant SQLite on the edge.

Each tenant gets their own Durable Object with a private 10GB SQLite DB.
FastHTML Worker routes requests to the right DO by tenant ID.
MiniDataAPI-compatible CRUD via pydorm.

Architecture:
  Request → FastHTML Worker (edge, any location)
          → Durable Object (single location, owns the SQLite)
          → pydorm (MiniDataAPI) → ctx.storage.sql
"""
from workers import WorkerEntrypoint, DurableObject, Response
from starlette.responses import HTMLResponse
from dataclasses import dataclass
import traceback, json

# -- Durable Object: the "stable manager" per tenant -------------------------
class TenantDB(DurableObject):
    """Each instance owns a private SQLite DB for one tenant."""

    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.sql = ctx.storage.sql
        self._init_tables()

    def _init_tables(self):
        from pydorm import DormDB
        self.db = DormDB(self.sql)

        @dataclass
        class Todo:
            id:    int = None
            title: str = ''
            done:  bool = False

        self.todos = self.db.create(Todo, pk='id')

    # -- RPC methods (called from Worker) --
    async def list_todos(self):
        from pyodide.ffi import to_js
        rows = [{'id': t.id, 'title': t.title, 'done': t.done} for t in self.todos()]
        return to_js(rows, dict_converter=js.Object.fromEntries)

    async def add_todo(self, title):
        from pyodide.ffi import to_js
        t = self.todos.insert(title=title)
        return to_js({'id': t.id, 'title': t.title, 'done': t.done},
                     dict_converter=js.Object.fromEntries)

    async def toggle_todo(self, id):
        from pyodide.ffi import to_js
        t = self.todos[int(id)]
        from dataclasses import dataclass
        @dataclass
        class Todo:
            id: int = None
            title: str = ''
            done: bool = False
        self.todos.update(Todo(id=t.id, title=t.title, done=not t.done))
        t = self.todos[int(id)]
        return to_js({'id': t.id, 'title': t.title, 'done': t.done},
                     dict_converter=js.Object.fromEntries)

    async def delete_todo(self, id):
        self.todos.delete(int(id))

    async def count(self):
        return self.todos.count


# -- FastHTML Worker: the "front desk" ----------------------------------------
_app = None

def get_app():
    global _app
    if _app is not None: return _app

    from fasthtml.common import *

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

    def tid(id): return f'todo-{id}'

    def mk_todo(t, tenant):
        done_cls = 'done' if t['done'] else ''
        return Li(
            A('✓', hx_put=f'/{tenant}/toggle/{t["id"]}', hx_target=f'#{tid(t["id"])}',
              hx_swap='outerHTML', cls=f'toggle {done_cls}', href='#'),
            Span(t['title'], cls=done_cls),
            A('✕', hx_delete=f'/{tenant}/todo/{t["id"]}', hx_target=f'#{tid(t["id"])}',
              hx_swap='outerHTML', cls='delete', href='#'),
            id=tid(t['id']),
        )

    @rt('/{tenant}')
    async def home(req, tenant: str):
        stub = get_stub(req, tenant)
        todos_js = await stub.list_todos()
        todos = list(todos_js)
        items = [mk_todo(t, tenant) for t in todos]
        return Titled(f'Tenant: {tenant}',
            Form(
                Input(name='title', placeholder='What needs doing?', autofocus=True, cls='todo-input'),
                Button('Add', type='submit'),
                hx_post=f'/{tenant}/todo', hx_target='#todo-list', hx_swap='beforeend',
                hx_on__after_request="this.reset()", cls='todo-form',
            ),
            Ul(*items, id='todo-list'),
            Div(
                P('Each tenant gets its own Durable Object with private SQLite (up to 10GB).'),
                P('Try: ', A('/acme', href='/acme'), ' · ', A('/berens', href='/berens'),
                  ' · ', A('/client-47', href='/client-47'), ' — each is isolated.'),
                cls='meta',
            ),
        )

    @rt('/{tenant}/todo', methods=['post'])
    async def add_todo(req, tenant: str, title: str):
        stub = get_stub(req, tenant)
        t = await stub.add_todo(title)
        return mk_todo(t, tenant)

    @rt('/{tenant}/toggle/{id}', methods=['put'])
    async def toggle(req, tenant: str, id: int):
        stub = get_stub(req, tenant)
        t = await stub.toggle_todo(id)
        return mk_todo(t, tenant)

    @rt('/{tenant}/todo/{id}', methods=['delete'])
    async def delete(req, tenant: str, id: int):
        stub = get_stub(req, tenant)
        await stub.delete_todo(id)
        return ''

    @rt('/')
    async def index():
        return Titled('Python DORM — Multi-tenant FastHTML',
            P('Each tenant gets their own Durable Object with private SQLite.'),
            Ul(
                Li(A('Tenant: acme', href='/acme')),
                Li(A('Tenant: berens', href='/berens')),
                Li(A('Tenant: client-47', href='/client-47')),
            ),
            Div(P(Code('pydorm'), ' — MiniDataAPI for Durable Object SQLite. ',
                  'Same interface as fastlite, runs inside a DO.'), cls='meta'),
        )

    return _app


def get_stub(req, tenant):
    "Get the Durable Object stub for a tenant."
    env = req.scope['env']
    do_id = env.TENANT_DB.idFromName(tenant)
    return env.TENANT_DB.get(do_id)


# -- Entrypoint ---------------------------------------------------------------
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            import asgi
            return await asgi.fetch(get_app(), request.js_object, self.env)
        except BaseException as e:
            return Response(f'{type(e).__name__}: {e}\n{traceback.format_exc()}', status=500)
