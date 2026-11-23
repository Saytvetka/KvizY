"""Все настройки сервера BiletY. Берутся из переменных окружения (в Yandex Cloud — у ревизии контейнера).

В коде нет ни одного ключа: если переменная не задана, значение пустое.
Здесь же собраны «ручки», которые удобно менять в коде (лимиты, уровни сложности).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


# --- Функции (что можно сделать на странице «Создать») ------------------------------
FUNCTIONS = {
    "tickets": {"name": "Все возможные билеты", "short": "Билеты",
                "about": "ИИ делит материал на темы и составляет билеты по всем темам, в каждом несколько вопросов."},
    "summary": {"name": "Конспект", "short": "Конспект",
                "about": "Структурированный конспект: разделы, главное, термины."},
    "answer": {"name": "Ответ на вопрос", "short": "Ответ",
               "about": "Ответ строится только по твоим файлам. Если ответа в них нет, так и будет написано."},
    "questions": {"name": "Создать вопросы", "short": "Вопросы",
                  "about": "Вопросы для самопроверки карточками, по одному. Число, сложность, таймер и подсказки берутся из Настроек."},
}

# --- Уровни сложности для «Создать вопросы»: ВСЕ числа уровней здесь -----------------
# timer_sec — секунд на карточку, если таймер включён;
# types     — доли видов вопросов (single — один верный из 4, multi — несколько верных, tf — верно/неверно);
# style / distractors — что написать ИИ.
LEVELS = {
    "easy": {
        "name": "лёгкий", "timer_sec": 60,
        "types": {"single": 0.6, "tf": 0.4},
        "style": "лёгкий: вопросы на запоминание, ответ прямо написан в материале, короткие формулировки",
        "distractors": "неверные варианты явно неверны: из другой темы или противоречат тексту",
    },
    "medium": {
        "name": "средний", "timer_sec": 40,
        "types": {"single": 0.5, "multi": 0.25, "tf": 0.25},
        "style": "средний: сравнение, применение, связь двух фактов из разных мест материала",
        "distractors": "варианты похожи друг на друга: близкие понятия, частично верные утверждения",
    },
    "hard": {
        "name": "сложный", "timer_sec": 30,
        "types": {"single": 0.4, "multi": 0.4, "tf": 0.2},
        "style": "сложный: логика, «что будет, если…», найди ошибку, перенос на новую ситуацию",
        "distractors": "неверные варианты правдоподобны: отличить можно, только поняв тему",
    },
}
LEVEL_DEFAULT = "medium"

# Настройки карточек по умолчанию (меняются в приложении: Настройки → Вопросы).
QUIZ_DEFAULTS = {"count": 10, "difficulty": LEVEL_DEFAULT, "timer": False, "hints": True}
QUESTION_CHOICES = [5, 10, 15, 20]
MAX_QUESTIONS = 40

# Билеты: сколько вопросов в одном билете.
TICKET_QUESTIONS = 3

# Загрузка материала.
MAX_FILES = 5
MAX_FILE_MB = 15
MAX_URLS = 5
# Файлы идут на сервер кусками: у Yandex Serverless Containers запрос не больше ~3,5 МБ.
UPLOAD_PART_BYTES = 2 * 1024 * 1024
# Сколько текста можно обработать за один раз (≈ 220 страниц). Больше — просим сузить «откуда докуда».
MAX_TEXT_CHARS = 400_000
# Примерно символов на странице — только чтобы понятно написать человеку «≈ N страниц».
CHARS_PER_PAGE = 1800
# Сканы: сколько страниц PDF-скана распознаём за раз (каждая страница — платный запрос).
MAX_OCR_PAGES = 40
# Цена распознавания одной страницы/фото, ₽ (🟡 примерно; Yandex Vision OCR).
OCR_PRICE_RUB = 0.13

# Если задача не подавала признаков жизни столько секунд — считаем, что сервер перезапустился.
JOB_STALE_SECONDS = 120


@dataclass
class Settings:
    # Пароль: хэш (scripts/hash_password.py) — так он нигде не лежит открытым текстом.
    password_hash: str = field(default_factory=lambda: _env("BILETY_PASSWORD_HASH"))
    # Запасной вариант для запуска на своём компьютере: пароль открытым текстом (в базу не пишется).
    password: str = field(default_factory=lambda: _env("BILETY_PASSWORD"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    key_secret: str = field(default_factory=lambda: _env("KEY_SECRET"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))

    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "yandex").lower())

    # База: пусто = локальный файл SQLite; адрес YDB (grpcs://...?database=...) = YDB.
    # Вместо DATABASE_URL можно задать YDB_ENDPOINT и YDB_DATABASE.
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL") or (
        f"{_env('YDB_ENDPOINT').rstrip('/')}/?database={_env('YDB_DATABASE')}" if _env("YDB_ENDPOINT") else ""))

    monthly_budget: float = field(default_factory=lambda: _float("MONTHLY_BUDGET_RUB", 500))


settings = Settings()


def reload_settings() -> Settings:
    global settings
    settings = Settings()
    return settings
