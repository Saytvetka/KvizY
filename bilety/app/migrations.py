"""Простые свои миграции базы (работают и в SQLite, и в YDB).

Как добавить поле в таблицу:
1. Добавь поле в модель в app/db.py (обязательно необязательное: `Mapped[... | None]`).
2. Допиши сюда функцию-миграцию и добавь её в конец списка MIGRATIONS, например:

       def m002_result_color(engine):
           add_column(engine, "results", "color", "Utf8", "TEXT")

   Первый тип — для YDB, второй — для SQLite/Postgres.
3. При старте сервер сам выполнит новые миграции один раз.

Старые миграции не меняй и не удаляй: они уже выполнены на сервере.
"""
from __future__ import annotations

import sqlalchemy as sa

from app import db

schema_migrations = sa.Table(
    "schema_migrations", sa.MetaData(),
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("applied_at", sa.DateTime),
)


def _ddl(engine, sql: str) -> None:
    # sa.DDL помечает запрос как «изменение схемы»: YDB выполняет такие запросы вне транзакции
    # (обычный text() в YDB падает с ошибкой «Scheme operations cannot be executed inside transaction»).
    with engine.begin() as c:
        c.execute(sa.DDL(sql))


def add_column(engine, table: str, column: str, ydb_type: str, sql_type: str) -> None:
    cols = {c["name"] for c in sa.inspect(engine).get_columns(table)}
    if column in cols:
        return
    if db.is_ydb():
        _ddl(engine, f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ydb_type}")
    else:
        _ddl(engine, f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}')


def m001_init(engine):
    db.Base.metadata.create_all(engine, checkfirst=True)


MIGRATIONS = [
    ("001_init", m001_init),
]


def migrate(engine=None) -> list[str]:
    engine = engine or db.engine
    schema_migrations.create(engine, checkfirst=True)
    with engine.connect() as c:
        done = {r[0] for r in c.execute(sa.select(schema_migrations.c.id))}
    applied = []
    for mid, fn in MIGRATIONS:
        if mid in done:
            continue
        fn(engine)
        with engine.begin() as c:
            c.execute(schema_migrations.insert().values(id=mid, applied_at=db.now()))
        applied.append(mid)
    return applied
