"""FastHTML + DORM: Multi-tenant todo app on Cloudflare Workers.

Each tenant gets their own Durable Object with private 10GB SQLite.
No serialize/deserialize. No R2. No last-write-wins.
The database just lives there, always consistent.
"""
from workers import WorkerEntrypoint, Response
from starlette.responses import HTMLResponse
from dataclasses import dataclass
import traceback

# -- Data models (shared between Worker and DO) ------
@dataclass
class Todo:
    id:    int = None
    title: str = ''
    done:  bool = False


# -- The Durable Object (one per tenant) -------------
from dorm import DORM

class Tenant(DORM):
    tables = [Todo]


# -- FastHTML app (the front desk) --------------------
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
            A('✓', hx_put=f'/{tenant}/toggle/{t["id"]}',
              hx_target=f'#{tid(t["id"])}', hx_swap='outerHTML',
              cls=f'toggle {done_cls}', href='#'),
            Span(t['title'], cls=done_cls),
            A('✕', hx_delete=f'/{tenant}/todo/{t["id"]}',
              hx_target=f'#{tid(t["id"])}', hx_swap='outerHTML',
              cls='delete', href='#'),
            id=tid(t['id']),
        )

    def mk_input(tenant):
        return Form(
            Input(name='title', placeholder='What needs doing?',
                  autofocus=True, cls='todo-input'),
            Button('Add', type='submit'),
            hx_post=f'/{tenant}/todo', hx_target='#todo-list',
            hx_swap='beforeend', hx_on__after_request="this.reset()",
            cls='todo-form',
        )

    def get_stub(env, tenant):
        "Get the Durable Object stub for a tenant."
        do_id = env.TENANTS.idFromName(tenant)
        return env.TENANTS.get(do_id)

    @rt('/{tenant}')
    async def home(req, tenant: str):
        stub = get_stub(req.scope['env'], tenant)
        todos = await stub.table_list('todo')
        return Titled(f'Todos — {tenant}',
            mk_input(tenant),
            Ul(*[mk_todo(t, tenant) for t in todos], id='todo-list'),
            Div(
                P(Code(f'Tenant: {tenant}'), ' — private SQLite in Durable Object'),
                P('Each tenant URL gets its own 10GB database.'),
                cls='meta',
            ),
        )

    @rt('/{tenant}/todo', methods=['post'])
    async def add_todo(req, tenant: str, title: str):
        if not title.strip(): return ''
        stub = get_stub(req.scope['env'], tenant)
        t = await stub.table_insert('todo', {'title': title.strip()})
        return mk_todo(t, tenant)

    @rt('/{tenant}/toggle/{id}', methods=['put'])
    async def toggle(req, tenant: str, id: int):
        stub = get_stub(req.scope['env'], tenant)
        t = await stub.table_get('todo', id)
        t['done'] = not t['done']
        t = await stub.table_update('todo', t)
        return mk_todo(t, tenant)

    @rt('/{tenant}/todo/{id}', methods=['delete'])
    async def delete(req, tenant: str, id: int):
        stub = get_stub(req.scope['env'], tenant)
        await stub.table_delete('todo', id)
        return ''

    @rt('/')
    async def index():
        return Titled('DORM — Python Multi-Tenant',
            P('Each URL path is a tenant with its own private SQLite:'),
            Ul(
                Li(A('/acme', href='/acme'), ' — Acme Corp todos'),
                Li(A('/berens', href='/berens'), ' — Berens todos'),
                Li(A('/kinai', href='/kinai'), ' — KinAI todos'),
            ),
            P('Create any tenant by visiting ', Code('/{name}'),
              '. Each gets a separate Durable Object with 10GB SQLite.'),
        )

    return _app


# -- Workers entrypoint -------------------------------------------------------
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            import asgi
            # Inject env into ASGI scope so routes can access DO bindings
            app = get_app()
            scope_env = self.env
            original_call = app.__call__

            async def patched_call(scope, receive, send):
                scope['env'] = scope_env
                return await original_call(scope, receive, send)

            app.__call__ = patched_call
            return await asgi.fetch(app, request.js_object, self.env)
        except BaseException as e:
            return Response(f'{type(e).__name__}: {e}\n{traceback.format_exc()}', status=500)
