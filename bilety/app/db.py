"""База данных BiletY: таблицы и подключение.

- пусто в DATABASE_URL → локальный файл SQLite (data/bilety.db) — для тестов и запуска на компьютере;
- адрес YDB (grpcs://ydb.serverless.yandexcloud.net:2135/?database=/ru-central1/...) → YDB Serverless;
- любой адрес SQLAlchemy (sqlite:///..., postgresql+psycopg://...) — тоже можно.

Правила для YDB (как в KvizY): ключи — строки, без внешних ключей, JSON — текстом,
большой текст режем на части (таблица result_texts).

Исходные файлы НЕ хранятся. Хранится только извлечённый текст и результаты.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app import config

OWNER_ID = "owner"


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> dt.datetime:
    """Время всегда в UTC и без часового пояса — так одинаково в SQLite и YDB."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def loads(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class Base(DeclarativeBase):
    pass


Str = sa.String
Txt = sa.Text


class User(Base):
    """Владелец. Пароль — из переменной BILETY_PASSWORD_HASH; если сменили в Настройках — хэш здесь."""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(Str, primary_key=True)
    password_hash: Mapped[str | None] = mapped_column(Txt)
    session_version: Mapped[int | None] = mapped_column(sa.Integer, default=1)
    created_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime, default=now)


class Setting(Base):
    """Настройки «ключ → значение (JSON)». id = user_id + ':' + key."""
    __tablename__ = "settings"
    id: Mapped[str] = mapped_column(Str, primary_key=True)
    user_id: Mapped[str] = mapped_column(Str)
    key: Mapped[str] = mapped_column(Str)
    value: Mapped[str | None] = mapped_column(Txt)
    updated_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime, default=now)


class Result(Base):
    """Один результат: билеты, конспект, ответ или вопросы."""
    __tablename__ = "results"
    id: Mapped[str] = mapped_column(Str, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(Str, default=OWNER_ID)
    func: Mapped[str] = mapped_column(Str)                 # tickets | summary | answer | questions
    title: Mapped[str | None] = mapped_column(Txt)          # как показывать в Истории
    note: Mapped[str | None] = mapped_column(Txt)           # «уточни, что нужно» (для «Ответа» — сам вопрос)
    params: Mapped[str | None] = mapped_column(Txt)         # JSON: настройки вопросов на момент создания и т. п.
    source_range: Mapped[str | None] = mapped_column(Txt)   # «откуда и докуда», как написал человек
    range_note: Mapped[str | None] = mapped_column(Txt)     # что обрезал код
    sources: Mapped[str | None] = mapped_column(Txt)        # JSON: имена файлов/ссылок (без содержимого)
    topics: Mapped[str | None] = mapped_column(Txt)         # JSON: ["тема", ...]
    content: Mapped[str | None] = mapped_column(Txt)        # JSON: сам результат
    chars: Mapped[int | None] = mapped_column(sa.Integer, default=0)
    status: Mapped[str | None] = mapped_column(Str, default="new")  # new | running | ready | error
    status_text: Mapped[str | None] = mapped_column(Txt)
    error: Mapped[str | None] = mapped_column(Txt)
    warnings: Mapped[str | None] = mapped_column(Txt)       # JSON: список предупреждений
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime)
    last_attempt: Mapped[str | None] = mapped_column(Txt)   # JSON: последнее прохождение вопросов
    created_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime, default=now)


class ResultText(Base):
    """Извлечённый текст материала, частями. Удаляется вместе с результатом."""
    __tablename__ = "result_texts"
    id: Mapped[str] = mapped_column(Str, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(Str, default=OWNER_ID)
    result_id: Mapped[str] = mapped_column(Str)
    part: Mapped[int] = mapped_column(sa.Integer)
    text: Mapped[str] = mapped_column(Txt)


class Usage(Base):
    """Каждый платный вызов: ИИ и распознавание фото. Остаётся и после удаления результата (для расходов)."""
    __tablename__ = "usage"
    id: Mapped[str] = mapped_column(Str, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(Str, default=OWNER_ID)
    result_id: Mapped[str | None] = mapped_column(Str)
    result_title: Mapped[str | None] = mapped_column(Txt)
    provider: Mapped[str | None] = mapped_column(Str)
    model: Mapped[str | None] = mapped_column(Txt)
    purpose: Mapped[str | None] = mapped_column(Str)
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer, default=0)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer, default=0)
    cost_rub: Mapped[float | None] = mapped_column(sa.Double, default=0)
    created_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime, default=now)


class UploadPart(Base):
    """Временные куски загружаемых файлов. Удаляются сразу после извлечения текста."""
    __tablename__ = "upload_parts"
    id: Mapped[str] = mapped_column(Str, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(Str, default=OWNER_ID)
    upload_id: Mapped[str] = mapped_column(Str)
    n: Mapped[int] = mapped_column(sa.Integer)
    filename: Mapped[str | None] = mapped_column(Txt)
    data: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    created_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime, default=now)


class LoginFailure(Base):
    __tablename__ = "login_failures"
    id: Mapped[str] = mapped_column(Str, primary_key=True, default=new_id)
    client: Mapped[str | None] = mapped_column(Str)
    at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime, default=now)


# --- Подключение -------------------------------------------------------------------

def _ydb_credentials():
    """В Yandex Cloud контейнер берёт токен своего сервисного аккаунта из «метаданных» сам."""
    import os

    import ydb
    import ydb.iam

    if os.environ.get("YDB_ANONYMOUS_CREDENTIALS") == "1":
        return ydb.AnonymousCredentials()
    sa_file = os.environ.get("YDB_SERVICE_ACCOUNT_KEY_FILE", "").strip()
    if sa_file:
        return ydb.iam.ServiceAccountCredentials.from_file(sa_file)
    return ydb.iam.MetadataUrlCredentials()


def make_engine(url: str = ""):
    url = (url or "").strip()
    if not url:
        config.settings.data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{config.settings.data_dir / 'bilety.db'}"
    if url.startswith(("grpc://", "grpcs://")):
        u = urlparse(url)
        database = (parse_qs(u.query).get("database") or [""])[0] or u.path
        return sa.create_engine(
            f"yql+ydb://{u.hostname}:{u.port or 2135}{database}",
            connect_args={"protocol": u.scheme, "credentials": _ydb_credentials()},
            _add_declare_for_yql_stmt_vars=True,
            pool_pre_ping=True,
            pool_size=5,
        )
    if url.startswith("sqlite"):
        return sa.create_engine(url, connect_args={"check_same_thread": False})
    return sa.create_engine(url, pool_pre_ping=True)


engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def init_engine(url: str | None = None):
    """Создать подключение (при старте сервера и в тестах)."""
    global engine
    engine = make_engine(config.settings.database_url if url is None else url)
    SessionLocal.configure(bind=engine)
    return engine


def is_ydb() -> bool:
    return engine is not None and engine.dialect.name in ("yql", "ydb")
