"""Запуск сервера: /api/health, отдача интерфейса (PWA), при старте — подключение к базе и миграции.

Тяжёлые библиотеки (pypdf, python-docx, python-pptx, BeautifulSoup, SDK Claude) импортируются
только когда нужны — так контейнер быстрее просыпается.
"""
from __future__ import annotations

import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app import db, migrations

STATIC = Path(__file__).resolve().parent.parent / "static"
VERSION = "1.1.0"
_started = time.time()


_db = {"ready": False, "error": ""}


def ensure_db() -> str:
    """Подключиться к базе и выполнить миграции. Если не вышло — НЕ падаем, а запоминаем ошибку:
    её покажет /api/health и любой запрос к API (чтобы было видно, что чинить)."""
    if _db["ready"]:
        return ""
    try:
        if db.engine is None:
            db.init_engine()
        migrations.migrate(db.engine)
        _db.update(ready=True, error="")
    except Exception as e:
        traceback.print_exc()
        db.engine = None if not _db["ready"] else db.engine
        _db["error"] = f"{type(e).__name__}: {e}"[:1500]
    return _db["error"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_db()
    yield


app = FastAPI(title="KvizY", lifespan=lifespan, docs_url=None, redoc_url=None)

from app.api.routes import router  # noqa: E402

app.include_router(router)


@app.middleware("http")
async def need_db(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health" and not _db["ready"]:
        import anyio
        err = await anyio.to_thread.run_sync(ensure_db)
        if err:
            return JSONResponse({"detail": f"Нет связи с базой данных: {err}"}, status_code=503)
    return await call_next(request)


from app.ai.base import AIError  # noqa: E402


@app.exception_handler(AIError)
async def ai_down(request: Request, exc: AIError):
    """ИИ недоступен (ключ, деньги, лимит, связь) — 503 и причина словами."""
    return JSONResponse({"detail": f"ИИ недоступен: {exc}", "kind": exc.kind}, status_code=503)


import sqlalchemy.exc as _sa_exc  # noqa: E402


@app.exception_handler(_sa_exc.DBAPIError)
@app.exception_handler(_sa_exc.TimeoutError)
async def db_down(request: Request, exc: Exception):
    """База не ответила посреди запроса — 503 и причина (а не «Ошибка на сервере»)."""
    traceback.print_exc()
    return JSONResponse({"detail": f"База данных недоступна: {type(exc).__name__}: {str(exc)[:500]}"},
                        status_code=503)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    if type(exc).__module__.startswith("ydb"):  # ошибки драйвера YDB, не завёрнутые SQLAlchemy
        return await db_down(request, exc)
    traceback.print_exc()
    return JSONResponse({"detail": f"Ошибка на сервере: {type(exc).__name__}: {exc}"}, status_code=500)


@app.get("/api/health")
def health():
    """Проверка, что сервер и база живы. Без входа."""
    import sqlalchemy as sa

    t = time.time()
    err = ensure_db()
    if err:
        return JSONResponse({"ok": False, "db": err, "version": VERSION, "uptime_sec": round(time.time() - _started)},
                            status_code=503)
    try:
        with db.SessionLocal() as s:
            s.execute(sa.select(db.User.id).limit(1)).all()
        db_ok = True
    except Exception as e:  # база недоступна — скажем об этом прямо
        db_ok = f"{type(e).__name__}: {e}"[:300]
    body = {"ok": db_ok is True, "db": "ok" if db_ok is True else db_ok, "db_kind": db.engine.dialect.name,
            "db_ms": round((time.time() - t) * 1000), "ai": _ai_state(), "version": VERSION,
            "uptime_sec": round(time.time() - _started)}
    return body if db_ok is True else JSONResponse(body, status_code=503)


def _ai_state() -> dict:
    """Какой ИИ выбран и заданы ли для него настройки (без запроса к ИИ — он платный)."""
    try:
        from app import ai
        p = ai.get_provider()
        return {"provider": p.name, "model": p.model, "configured": bool(p.is_configured())}
    except Exception as e:
        return {"provider": "?", "configured": False, "error": f"{type(e).__name__}: {e}"[:200]}


# --- Интерфейс (PWA) ------------------------------------------------------------------

NO_CACHE = {"Cache-Control": "no-cache"}


def _file(name: str, media: str | None = None, headers: dict | None = None, request: Request | None = None):
    """Файл интерфейса. Браузер присылает If-None-Match — если файл не менялся, отвечаем 304 без тела
    (так «no-cache» не качает app.js заново при каждом запуске)."""
    path = (STATIC / name).resolve()
    if not str(path).startswith(str(STATIC)) or not path.is_file():
        return Response("Не найдено", status_code=404)
    st = path.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    h = {**(headers or NO_CACHE), "ETag": etag}
    if request is not None and etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=h)
    return FileResponse(path, media_type=media, headers=h)


@app.get("/")
def index(request: Request):
    return _file("index.html", "text/html; charset=utf-8", request=request)


@app.get("/sw.js")
def service_worker(request: Request):
    # Service worker должен лежать в корне, чтобы управлять всем приложением.
    return _file("sw.js", "application/javascript", {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
                 request=request)


@app.get("/manifest.json")
def manifest():
    return _file("manifest.json", "application/manifest+json")


@app.get("/static/{name:path}")
def static(name: str, request: Request):
    # Иконки и экраны запуска не меняются — пусть браузер держит их сутки.
    # app.js / app.css — «no-cache»: браузер каждый раз сверяет ETag и получает 304, если не менялись.
    if name.startswith("icons/"):
        return _file(name, headers={"Cache-Control": "public, max-age=86400"}, request=request)
    return _file(name, request=request)


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_icon():
    return _file("icons/apple-touch-icon.png", "image/png")


@app.get("/favicon.ico")
def favicon():
    return _file("icons/icon-192.png", "image/png")
