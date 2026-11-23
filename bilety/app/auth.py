"""Вход по одному паролю владельца.

- Пароль хранится только хэшем (scrypt): переменная BILETY_PASSWORD_HASH (сделать — scripts/hash_password.py).
  Если пароль сменили в Настройках, новый хэш лежит в базе и он важнее переменной.
  Для запуска на своём компьютере можно задать BILETY_PASSWORD открытым текстом (в базу он не пишется).
- После входа устройство получает подписанную cookie на год (HMAC, секрет SESSION_SECRET):
  пароль вводится один раз на устройстве.
- В подпись входит «отпечаток» пароля и номер версии сессии: сменили пароль — все старые cookie
  перестают подходить (текущее устройство сразу получает новую).
- 5 ошибок за 5 минут с одного адреса — пауза.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from datetime import timedelta

import sqlalchemy as sa
from fastapi import HTTPException, Request

from app import config
from app.db import OWNER_ID, LoginFailure, SessionLocal, User, now

COOKIE = "bilety_session"
TTL = 60 * 60 * 24 * 365  # год
MAX_FAILS = 5
FAIL_WINDOW = 300

_cache: dict[str, tuple[float, tuple[str, int]]] = {}
_lock = threading.Lock()


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + h.hex()


def verify_hash(pw: str, stored: str) -> bool:
    """Два вида хэша:
    - scrypt$соль$хэш — делает scripts/hash_password.py и смена пароля в Настройках;
    - pbkdf2_sha256$итерации$соль$хэш — делает страница-генератор прямо в браузере (WebCrypto умеет только PBKDF2)."""
    try:
        parts = stored.strip().split("$")
        if parts[0] == "pbkdf2_sha256":
            _, iters, salt, h = parts
            if int(iters) < 100_000:
                return False
            calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters))
        else:
            _, salt, h = parts
            calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h.lower())
    except Exception:
        return False


def _owner() -> User:
    with SessionLocal() as s:
        u = s.get(User, OWNER_ID)
        if u is None:
            u = User(id=OWNER_ID, session_version=1)
            s.add(u)
            s.commit()
        return u


def _stored_secret() -> str:
    """Чем проверяется пароль: хэш из базы → хэш из переменной → (только локально) открытый пароль."""
    u = _owner()
    if u.password_hash:
        return u.password_hash
    if config.settings.password_hash:
        return config.settings.password_hash
    if config.settings.password:
        return "plain:" + hashlib.sha256(config.settings.password.encode()).hexdigest()
    return ""


def _state() -> tuple[str, int]:
    """(отпечаток пароля, версия сессии). Кэш на 10 секунд, чтобы не читать базу на каждый запрос."""
    with _lock:
        hit = _cache.get(OWNER_ID)
        if hit and hit[0] > time.time():
            return hit[1]
    u = _owner()
    fp = hashlib.sha256(_stored_secret().encode()).hexdigest()[:16]
    state = (fp, u.session_version or 1)
    with _lock:
        _cache[OWNER_ID] = (time.time() + 10, state)
    return state


def forget_cache() -> None:
    with _lock:
        _cache.clear()


def _key() -> bytes:
    base = config.settings.session_secret or ("bilety-session:" + _stored_secret())
    return hashlib.sha256(base.encode()).digest()


def _sign(payload: str) -> str:
    fp, version = _state()
    return hmac.new(_key(), f"{payload}|{fp}|{version}".encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    payload = f"{OWNER_ID}|{int(time.time()) + TTL}"
    raw = f"{payload}|{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def read_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, exp, sig = raw.rsplit("|", 2)
        if int(exp) < time.time() or user_id != OWNER_ID:
            return None
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(f"{user_id}|{exp}")):
        return None
    return user_id


def password_ok(pw: str) -> bool:
    stored = _stored_secret()
    if not stored:
        raise HTTPException(500, "На сервере не задан пароль: впиши BILETY_PASSWORD_HASH в переменные контейнера "
                                 "(как его сделать — docs/yandex_cloud.md).")
    if stored.startswith("plain:"):
        return hmac.compare_digest(pw.encode(), config.settings.password.encode())
    return verify_hash(pw, stored)


def check_password(pw: str, client: str) -> str:
    """Возвращает user_id или бросает 401/429."""
    since = now() - timedelta(seconds=FAIL_WINDOW)
    with SessionLocal() as s:
        fails = s.scalar(sa.select(sa.func.count()).select_from(LoginFailure)
                         .where(LoginFailure.client == client, LoginFailure.at >= since)) or 0
    if fails >= MAX_FAILS:
        raise HTTPException(429, "Слишком много ошибок. Подожди 5 минут и попробуй снова.")
    if password_ok(pw):
        with SessionLocal() as s:
            s.execute(sa.delete(LoginFailure).where(LoginFailure.client == client))
            s.commit()
        return OWNER_ID
    with SessionLocal() as s:
        s.add(LoginFailure(client=client))
        s.commit()
    left = MAX_FAILS - fails - 1
    raise HTTPException(401, "Неверный пароль" + (f". Осталось попыток: {left}" if left > 0 else ". Теперь пауза 5 минут."))


def change_password(old: str, new: str) -> None:
    if not password_ok(old):
        raise HTTPException(400, "Старый пароль не подошёл")
    if len(new) < 6:
        raise HTTPException(400, "Новый пароль слишком короткий (нужно хотя бы 6 символов)")
    with SessionLocal() as s:
        u = s.get(User, OWNER_ID)
        u.password_hash = hash_password(new)
        u.session_version = (u.session_version or 1) + 1
        s.commit()
    forget_cache()


def client_id(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))[:64]


def current_user(request: Request) -> str:
    user_id = read_token(request.cookies.get(COOKIE))
    if not user_id:
        raise HTTPException(401, "Нужно войти")
    return user_id
