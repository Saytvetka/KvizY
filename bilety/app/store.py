"""Настройки приложения в базе (таблица settings): провайдер ИИ, модель, ключи (зашифрованы),
палитра, настройки вопросов, цены, бюджет.

Порядок, откуда берётся значение: база → переменная окружения → значение по умолчанию.
"""
from __future__ import annotations

import os

from app import config, crypto
from app.db import OWNER_ID, SessionLocal, Setting, dumps, loads, now

# Поля провайдеров. secret — только для записи, хранятся зашифрованными, на экран не возвращаются.
PROVIDERS = {
    "yandex": {
        "title": "YandexGPT",
        "secret": {"api_key": "YANDEX_API_KEY"},
        "plain": {"folder_id": "YANDEX_FOLDER_ID", "model": "YANDEX_MODEL"},
        "labels": {"api_key": "API-ключ", "folder_id": "ID каталога (folder ID)"},
        "models": [["yandexgpt-lite/latest", "YandexGPT Lite — дёшево"], ["yandexgpt/latest", "YandexGPT Pro — умнее, дороже"]],
        "default_model": "yandexgpt-lite/latest",
    },
    "gigachat": {
        "title": "GigaChat",
        "secret": {"auth_key": "GIGACHAT_AUTH_KEY"},
        "plain": {"scope": "GIGACHAT_SCOPE", "model": "GIGACHAT_MODEL"},
        "labels": {"auth_key": "Ключ авторизации", "scope": "Scope"},
        "models": [["GigaChat-2", "GigaChat 2 Lite"], ["GigaChat-2-Pro", "GigaChat 2 Pro"],
                   ["GigaChat-2-Max", "GigaChat 2 Max"]],
        "default_model": "GigaChat-2-Pro",
    },
    "claude": {
        "title": "Claude",
        "secret": {"api_key": "ANTHROPIC_API_KEY"},
        "plain": {"model": "ANTHROPIC_MODEL"},
        "labels": {"api_key": "API-ключ"},
        "models": [["claude-haiku-5-5", "Claude Haiku 5.5"], ["claude-sonnet-5-5", "Claude Sonnet 5.5"],
                   ["claude-opus-5-5", "Claude Opus 5.5"]],
        "default_model": "claude-sonnet-5-5",
    },
    "openai": {
        "title": "OpenAI-совместимый (DeepSeek и др.)",
        "secret": {"api_key": "OPENAI_API_KEY"},
        "plain": {"base_url": "OPENAI_BASE_URL", "model": "OPENAI_MODEL"},
        "labels": {"api_key": "API-ключ", "base_url": "Адрес API"},
        "models": [],
        "default_model": "gpt-4o-mini",
    },
    "mock": {
        "title": "Пробный (без денег)",
        "secret": {},
        "plain": {"model": ""},
        "labels": {},
        "models": [["mock", "mock"]],
        "default_model": "mock",
    },
}

# Палитры: классика ч/б, спокойные и 5 необычных (цвета — в static/app.css).
PALETTES = ["classic", "dark", "beige", "bluegray", "sage", "terminal", "sakura", "desert", "deep", "neon"]


def get(key: str, default=None, user_id: str = OWNER_ID):
    with SessionLocal() as s:
        row = s.get(Setting, f"{user_id}:{key}")
        return loads(row.value, default) if row else default


def put(key: str, value, user_id: str = OWNER_ID) -> None:
    with SessionLocal() as s:
        sid = f"{user_id}:{key}"
        row = s.get(Setting, sid)
        if value is None:
            if row:
                s.delete(row)
        elif row:
            row.value = dumps(value)
            row.updated_at = now()
        else:
            s.add(Setting(id=sid, user_id=user_id, key=key, value=dumps(value), updated_at=now()))
        s.commit()


def get_many(prefix: str = "", user_id: str = OWNER_ID) -> dict:
    """Все настройки разом (одним запросом к базе)."""
    import sqlalchemy as sa

    with SessionLocal() as s:
        rows = s.scalars(sa.select(Setting).where(Setting.user_id == user_id)).all()
    return {r.key: loads(r.value) for r in rows if r.key.startswith(prefix)}


# --- Вопросы (карточки) -----------------------------------------------------------------

def quiz_settings(all_settings: dict | None = None) -> dict:
    st = all_settings if all_settings is not None else get_many()
    q = {**config.QUIZ_DEFAULTS, **(st.get("quiz") or {})}
    if q.get("difficulty") not in config.LEVELS:
        q["difficulty"] = config.LEVEL_DEFAULT
    return q


def save_quiz_settings(data: dict) -> dict:
    q = quiz_settings()
    if "count" in data:
        c = int(data["count"])
        if not 1 <= c <= config.MAX_QUESTIONS:
            raise ValueError(f"Число вопросов — от 1 до {config.MAX_QUESTIONS}")
        q["count"] = c
    if "difficulty" in data:
        if data["difficulty"] not in config.LEVELS:
            raise ValueError("Неизвестная сложность")
        q["difficulty"] = data["difficulty"]
    for k in ("timer", "hints"):
        if k in data:
            q[k] = bool(data[k])
    put("quiz", q)
    return q


# --- ИИ ------------------------------------------------------------------------------

def active_provider() -> str:
    name = (get("ai.provider") or config.settings.ai_provider or "yandex").lower()
    if name == "anthropic":
        name = "claude"
    return name if name in PROVIDERS else "yandex"


def provider_config(name: str, all_settings: dict | None = None) -> dict:
    """Готовые параметры провайдера (ключи — расшифрованные, только для сервера)."""
    st = all_settings if all_settings is not None else get_many()
    spec = PROVIDERS[name]
    cfg: dict = {}
    for field, env in spec["plain"].items():
        cfg[field] = (st.get(f"ai.{name}.{field}") or (os.environ.get(env, "").strip() if env else "") or "")
    for field, env in spec["secret"].items():
        enc = st.get(f"key.{name}.{field}")
        val = crypto.decrypt(enc) if enc else None
        cfg[field] = val or os.environ.get(env, "").strip()
        cfg[f"{field}__source"] = "db" if val else ("env" if cfg[field] else "")
        cfg[f"{field}__broken"] = bool(enc and not val)
    if not cfg.get("model"):
        cfg["model"] = spec["default_model"]
    return cfg


def public_ai_settings() -> dict:
    """То, что видит интерфейс. Ключи НИКОГДА не возвращаются — только «задан / не задан»."""
    st = get_many()
    out = {"provider": active_provider(), "providers": {}}
    for name, spec in PROVIDERS.items():
        cfg = provider_config(name, st)
        out["providers"][name] = {
            "title": spec["title"],
            "models": spec["models"],
            "labels": spec["labels"],
            "fields": {f: cfg.get(f, "") for f in spec["plain"]},
            "secrets": {f: {"set": bool(cfg.get(f)), "source": cfg.get(f"{f}__source"),
                            "broken": cfg.get(f"{f}__broken")} for f in spec["secret"]},
            "price": st.get(f"price.{name}"),
        }
    out["key_secret_set"] = bool(config.settings.key_secret)
    return out


def save_ai_settings(data: dict) -> None:
    name = data.get("provider")
    if name not in PROVIDERS:
        raise ValueError("Неизвестный провайдер")
    spec = PROVIDERS[name]
    secrets = {f: str(v or "").strip() for f, v in (data.get("secrets") or {}).items() if f in spec["secret"]}
    if any(secrets.values()) and not config.settings.key_secret:
        raise ValueError("На сервере не задан KEY_SECRET — ключ негде надёжно сохранить. "
                         "Задай ключ переменной окружения или добавь KEY_SECRET.")
    put("ai.provider", name)
    for field in spec["plain"]:
        if field in data.get("fields", {}):
            v = str(data["fields"][field] or "").strip()
            put(f"ai.{name}.{field}", v or None)
    for field, value in secrets.items():
        if value:
            put(f"key.{name}.{field}", crypto.encrypt(value))
    if "price" in data:
        p = data["price"]
        if p and (p.get("in") not in (None, "") or p.get("out") not in (None, "")):
            put(f"price.{name}", {"in": float(p.get("in") or 0), "out": float(p.get("out") or 0)})
        else:
            put(f"price.{name}", None)


def delete_secret(name: str, field: str) -> None:
    put(f"key.{name}.{field}", None)


def budget() -> float:
    v = get("budget")
    return float(v) if v not in (None, "") else config.settings.monthly_budget
