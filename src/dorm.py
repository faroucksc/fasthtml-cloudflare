"""DORM: MiniDataAPI for Durable Object SQLite.

A thin, Jeremy Howard-style wrapper that gives you fastlite patterns
over a Durable Object's built-in SQLite. Each DO is a tenant, each
tenant gets 10GB of private, strongly-consistent SQLite on the edge.

Usage in a DO:

    class Tenant(DORM):
        tables = [Todo, User]   # dataclasses → tables

    # From your FastHTML Worker:
    stub = env.TENANTS.get(env.TENANTS.idFromName("acme"))
    todos = await stub.query("SELECT * FROM todo")
    await stub.run("INSERT INTO todo (title) VALUES (?)", ["Buy milk"])
"""
from workers import DurableObject
from dataclasses import fields, dataclass
from typing import get_type_hints

# -- Type mapping (Python types → SQLite) ------------------------------------
_type_map = {
    int: 'INTEGER', float: 'REAL', str: 'TEXT',
    bool: 'INTEGER', bytes: 'BLOB',
}

def _sql_type(py_type):
    return _type_map.get(py_type, 'TEXT')


# -- DOTable: MiniDataAPI over sql.exec() ------------------------------------
class DOTable:
    "MiniDataAPI-style table backed by a DO's SQLite."
    def __init__(self, sql, dc):
        self.sql, self.dc, self.name = sql, dc, dc.__name__.lower()
        self.fields = {f.name: f for f in fields(dc)}
        self.pk = next((f.name for f in fields(dc)
                        if f.name in ('id','pk','_id')), fields(dc)[0].name)
        self._ensure()

    def _ensure(self):
        cols = []
        for f in fields(self.dc):
            col = f'{f.name} {_sql_type(f.type)}'
            if f.name == self.pk: col += ' PRIMARY KEY'
            cols.append(col)
        self.sql.exec(
            f'CREATE TABLE IF NOT EXISTS {self.name} ({", ".join(cols)})'
        )

    def _to_dict(self, obj):
        if isinstance(obj, dict): return obj
        return {f.name: getattr(obj, f.name) for f in fields(self.dc)}

    def _from_row(self, row):
        "Convert a JS cursor row to a dataclass instance."
        d = {k: row[k] for k in self.fields}
        return self.dc(**d)

    # -- MiniDataAPI methods ---
    def insert(self, obj):
        d = self._to_dict(obj)
        # Skip None pk (auto-increment)
        if d.get(self.pk) is None:
            d = {k:v for k,v in d.items() if k != self.pk}
        cols = ', '.join(d.keys())
        phs = ', '.join('?' for _ in d)
        self.sql.exec(f'INSERT INTO {self.name} ({cols}) VALUES ({phs})', *d.values())
        # Get the inserted row
        row_id = list(self.sql.exec('SELECT last_insert_rowid() as id'))[0]['id']
        return self[row_id]

    def update(self, obj):
        d = self._to_dict(obj)
        pk_val = d.pop(self.pk)
        sets = ', '.join(f'{k}=?' for k in d)
        self.sql.exec(f'UPDATE {self.name} SET {sets} WHERE {self.pk}=?', *d.values(), pk_val)
        return self[pk_val]

    def delete(self, pk_val):
        self.sql.exec(f'DELETE FROM {self.name} WHERE {self.pk}=?', pk_val)

    def __getitem__(self, pk_val):
        rows = list(self.sql.exec(
            f'SELECT * FROM {self.name} WHERE {self.pk}=?', pk_val
        ))
        if not rows: raise KeyError(pk_val)
        return self._from_row(rows[0])

    def __call__(self, where=None, vals=None, order_by=None, limit=None):
        "List rows. todos() returns all, todos('done=?', [1]) filters."
        sql = f'SELECT * FROM {self.name}'
        args = list(vals or [])
        if where: sql += f' WHERE {where}'
        if order_by: sql += f' ORDER BY {order_by}'
        if limit: sql += f' LIMIT {limit}'
        return [self._from_row(r) for r in self.sql.exec(sql, *args)]

    @property
    def rows(self):
        return self()

    @property
    def count(self):
        return list(self.sql.exec(f'SELECT count(*) as n FROM {self.name}'))[0]['n']


# -- DORM: the Durable Object base class ------------------------------------
class DORM(DurableObject):
    """Base class for tenant Durable Objects with MiniDataAPI tables.

    Subclass and set `tables` to a list of dataclasses:

        class Tenant(DORM):
            tables = [Todo, User]

    Access tables as attributes: self.t.todo, self.t.user
    """
    tables = []  # override in subclass

    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.sql = ctx.storage.sql

        # Create DOTable for each dataclass
        class _Tables:
            pass
        self.t = _Tables()
        for dc in self.tables:
            tbl = DOTable(self.sql, dc)
            setattr(self.t, tbl.name, tbl)

    # -- RPC methods callable from Worker ------------------------------------
    async def query(self, sql, *args):
        "Run SELECT, return list of dicts."
        return [dict(r) for r in self.sql.exec(sql, *args)]

    async def run(self, sql, *args):
        "Run INSERT/UPDATE/DELETE."
        self.sql.exec(sql, *args)

    async def table_insert(self, table_name, data):
        "Insert a dict into a named table, return the row."
        tbl = getattr(self.t, table_name)
        dc = tbl.dc(**data)
        result = tbl.insert(dc)
        return self._to_dict(result)

    async def table_update(self, table_name, data):
        tbl = getattr(self.t, table_name)
        dc = tbl.dc(**data)
        result = tbl.update(dc)
        return self._to_dict(result)

    async def table_delete(self, table_name, pk_val):
        getattr(self.t, table_name).delete(pk_val)

    async def table_get(self, table_name, pk_val):
        tbl = getattr(self.t, table_name)
        return self._to_dict(tbl[pk_val])

    async def table_list(self, table_name, where=None, vals=None):
        tbl = getattr(self.t, table_name)
        return [self._to_dict(r) for r in tbl(where, vals)]

    def _to_dict(self, obj):
        if isinstance(obj, dict): return obj
        return {f.name: getattr(obj, f.name) for f in fields(obj)}
