"""pydorm — MiniDataAPI for Cloudflare Durable Object SQLite.

Drop-in replacement for fastlite that uses ctx.storage.sql instead of apsw.
Same dataclass-to-table pattern, same CRUD interface, runs inside a DO.

Usage inside a Durable Object:
    from pydorm import DormDB

    @dataclass
    class Todo:
        id:   int = None
        title: str = ''
        done:  bool = False

    db = DormDB(ctx.storage.sql)
    todos = db.create(Todo, pk='id')

    todos.insert(Todo(title='Buy milk'))
    todos[1]                          # → Todo(id=1, title='Buy milk', done=False)
    todos()                           # → [Todo(...), ...]
    todos.update(Todo(id=1, done=True))
    todos.delete(1)
"""
from dataclasses import dataclass, fields, asdict
from typing import get_type_hints

__all__ = ['DormDB', 'DormTable']

# Python type → SQLite type
_TYPE_MAP = {int: 'INTEGER', float: 'REAL', str: 'TEXT', bool: 'INTEGER', bytes: 'BLOB'}

def _sql_type(py_type):
    "Map Python type annotation to SQLite column type."
    return _TYPE_MAP.get(py_type, 'TEXT')

def _to_py(val, py_type):
    "Convert JS/SQL value to Python type."
    if val is None: return None
    if py_type == bool: return bool(val)
    return py_type(val)


class DormTable:
    "MiniDataAPI-compatible table backed by Durable Object SQLite."
    def __init__(self, sql, cls, pk='id'):
        self.sql, self.cls, self.pk, self.name = sql, cls, pk, cls.__name__.lower()
        self._fields = fields(cls)
        self._hints = get_type_hints(cls)
        self._cols = [f.name for f in self._fields]
        self._non_pk = [f.name for f in self._fields if f.name != pk]

    def _row_to_obj(self, row):
        "Convert a JS row proxy to a dataclass instance."
        d = {}
        for f in self._fields:
            val = getattr(row, f.name, None)
            d[f.name] = _to_py(val, self._hints.get(f.name, str))
        return self.cls(**d)

    def _rows_to_list(self, cursor):
        "Convert JS cursor to list of dataclass instances."
        return [self._row_to_obj(r) for r in cursor.toArray()]

    def insert(self, obj=None, **kwargs):
        "Insert a row, return dataclass with pk populated."
        if obj is None: obj = self.cls(**kwargs)
        d = asdict(obj)
        cols = [c for c in self._non_pk if d.get(c) is not None]
        vals = [d[c] for c in cols]
        if d.get(self.pk) is not None:
            cols = [self.pk] + cols
            vals = [d[self.pk]] + vals
        placeholders = ','.join('?' * len(vals))
        col_str = ','.join(cols)
        self.sql.exec(f'INSERT INTO [{self.name}] ({col_str}) VALUES ({placeholders})', *vals)
        # Get the inserted row
        row = self.sql.exec(f'SELECT * FROM [{self.name}] WHERE rowid = last_insert_rowid()').one()
        return self._row_to_obj(row)

    def update(self, obj=None, **kwargs):
        "Update a row by pk."
        if obj is None: obj = self.cls(**kwargs)
        d = asdict(obj)
        pk_val = d[self.pk]
        sets = [f'{c}=?' for c in self._non_pk]
        vals = [d[c] for c in self._non_pk] + [pk_val]
        self.sql.exec(f'UPDATE [{self.name}] SET {",".join(sets)} WHERE {self.pk}=?', *vals)
        return self[pk_val]

    def delete(self, pk_val):
        "Delete a row by pk."
        self.sql.exec(f'DELETE FROM [{self.name}] WHERE {self.pk}=?', pk_val)

    def get(self, pk_val, default=None):
        "Get a row by pk, return default if not found."
        try: return self[pk_val]
        except: return default

    def __getitem__(self, pk_val):
        "Get a row by pk. Raises if not found."
        row = self.sql.exec(f'SELECT * FROM [{self.name}] WHERE {self.pk}=?', pk_val).one()
        return self._row_to_obj(row)

    def __call__(self, where=None, order_by=None, limit=None):
        "Query rows. No args = all rows."
        q = f'SELECT * FROM [{self.name}]'
        if where: q += f' WHERE {where}'
        if order_by: q += f' ORDER BY {order_by}'
        if limit: q += f' LIMIT {limit}'
        return self._rows_to_list(self.sql.exec(q))

    @property
    def rows(self):
        "Iterator over all rows as dicts."
        for obj in self(): yield asdict(obj)

    @property
    def count(self):
        return self.sql.exec(f'SELECT COUNT(*) as c FROM [{self.name}]').one().c

    def __repr__(self):
        cols = ','.join(self._cols)
        return f'<Table {self.name} ({cols})>'


class DormDB:
    "MiniDataAPI-compatible database backed by Durable Object SQLite."
    def __init__(self, sql):
        self.sql = sql
        self._tables = {}

    def create(self, cls, pk='id', if_not_exists=True):
        "Create a table from a dataclass, return DormTable."
        name = cls.__name__.lower()
        hints = get_type_hints(cls)
        flds = fields(cls)
        col_defs = []
        for f in flds:
            col_type = _sql_type(hints.get(f.name, str))
            pk_str = ' PRIMARY KEY' if f.name == pk else ''
            col_defs.append(f'[{f.name}] {col_type}{pk_str}')
        exists = ' IF NOT EXISTS' if if_not_exists else ''
        self.sql.exec(f'CREATE TABLE{exists} [{name}] ({", ".join(col_defs)})')
        t = DormTable(self.sql, cls, pk)
        self._tables[name] = t
        return t

    @property
    def t(self):
        "Access tables by name: db.t.todo"
        return type('Tables', (), {
            **self._tables,
            '__repr__': lambda s: str(list(self._tables.values())),
            '__iter__': lambda s: iter(self._tables.values()),
        })()
