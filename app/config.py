"""Все настройки сервера. Берутся из переменных окружения (в Yandex Cloud — у ревизии контейнера).

В коде нет ни одного ключа: если переменная не задана, значение пустое.
Здесь же собраны «ручки», которые удобно менять в коде (время на вопрос, лимиты).
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


# --- Ручки, которые меняются в коде -------------------------------------------------

# --- Уровни сложности: ВСЕ числа и правила уровней здесь (больше нигде) -------------
# timer_sec       — секунд на обычный вопрос, если таймер включён;
# timer_open_sec  — секунд на открытый вопрос (писать дольше, чем выбрать);
# hints           — у каких вопросов бывают подсказки: all (у всех) | outside (только «вне файла») | none;
# multiplier      — во сколько раз больше баллов за верный ответ;
# types           — доли видов вопросов (сумма 1). open — доля открытых ответов;
# outside_share   — доля вопросов «шире файла»;
# style / distractors / angles — что именно написать ИИ в запросе.
LEVELS = {
    "easy": {
        "name": "лёгкий",
        "timer_sec": 90, "timer_open_sec": 180,
        "hints": "all",
        "multiplier": 1.0,
        "types": {"single": 0.40, "tf": 0.25, "fill": 0.20, "match": 0.15},
        "outside_share": 0.0,
        "style": "лёгкий — вопросы на запоминание и узнавание: ответ прямо написан в материале, "
                 "одна мысль на вопрос, короткие формулировки",
        "distractors": "неверные варианты ЯВНО неверны: из другой темы или противоречат тексту; "
                       "человек, который читал материал, отбросит их сразу",
        "angles": ["найти факт: что прямо сказано в материале", "термин: что означает понятие из материала",
                   "узнать пример понятия из материала", "кто/что/где/когда — из материала",
                   "верно ли утверждение, повторяющее материал"],
    },
    "medium": {
        "name": "средний",
        "timer_sec": 45, "timer_open_sec": 120,
        "hints": "outside",
        "multiplier": 1.5,
        "types": {"single": 0.30, "multi": 0.15, "match": 0.15, "order": 0.10, "fill": 0.10, "tf": 0.10, "open": 0.10},
        "outside_share": 0.2,
        "style": "средний — сравнение, применение, связь двух фактов: ответа нет одной фразой в тексте, "
                 "его нужно сложить из двух мест материала или применить правило",
        "distractors": "варианты ПОХОЖИ друг на друга: путаница близких понятий, частично верные утверждения, "
                       "перепутанные причина и следствие",
        "angles": ["сравнение двух понятий: в чём разница", "применение: как правило работает в ситуации",
                   "связь двух фактов из разных мест материала", "причина и следствие",
                   "пример или контрпример", "вывод из двух фактов материала"],
    },
    "hard": {
        "name": "сложный",
        "timer_sec": 25, "timer_open_sec": 90,
        "hints": "none",
        "multiplier": 2.0,
        "types": {"multi": 0.30, "open": 0.30, "single": 0.20, "order": 0.10, "tf": 0.10},
        "outside_share": 0.25,
        "style": "сложный — логика и рассуждение: «что будет, если…», найди ошибку в рассуждении, "
                 "перенос на новую ситуацию, вывод в несколько шагов",
        "distractors": "неверные варианты ПРАВДОПОДОБНЫ: типичные ошибки рассуждения, верные сами по себе "
                       "утверждения, которые не отвечают на вопрос; отличить можно только поняв тему",
        "angles": ["что будет, если изменить условие", "найди ошибку в рассуждении",
                   "применение к новой ситуации, которой нет в тексте", "вывод в несколько шагов",
                   "контрпример: когда правило не работает", "сравнение и выбор: что лучше подходит и почему"],
    },
}
LEVEL_DEFAULT = "medium"

# Время на вопрос по уровню (для формы и старого кода; берётся из LEVELS).
TIMER_SECONDS = {k: v["timer_sec"] for k, v in LEVELS.items()}

# «Пройти тему ещё раз»: доля вопросов из тех, где была ошибка (остальное — новые).
RETRY_MISTAKES_SHARE = 0.5
# Новый вопрос считается повтором старого, если совпадает столько слов (0..1).
RETRY_SIMILARITY = 0.6

# Сколько раз можно заменить один вопрос.
MAX_REPLACES = 4

# Загрузка материала.
MAX_FILES = 5
MAX_FILE_MB = 15
# Файлы идут на сервер кусками: у Yandex Serverless Containers запрос не больше ~3,5 МБ.
UPLOAD_PART_BYTES = 2 * 1024 * 1024
MAX_URLS = 5

# Варианты числа вопросов в форме (и «своё» до MAX_QUESTIONS).
QUESTION_CHOICES = [5, 10, 15, 20]
MAX_QUESTIONS = 40

# Доля старых вопросов в квизе по предмету (остальное — новые от ИИ).
SUBJECT_OLD_SHARE = 0.5

# Если задача создания квиза не подавала признаков жизни столько секунд — считаем, что
# сервер перезапустился, и показываем «Повторить».
JOB_STALE_SECONDS = 120


@dataclass
class Settings:
    password: str = field(default_factory=lambda: _env("KVIZY_PASSWORD"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    key_secret: str = field(default_factory=lambda: _env("KEY_SECRET"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))

    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "yandex").lower())

    # База: пусто = локальный файл SQLite; адрес YDB (grpcs://...?database=...) = YDB;
    # sqlite:///... или postgresql+psycopg://... — тоже можно.
    # Вместо DATABASE_URL можно задать YDB_ENDPOINT (grpcs://ydb.serverless.yandexcloud.net:2135) и YDB_DATABASE (/ru-central1/...).
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL") or (
        f"{_env('YDB_ENDPOINT').rstrip('/')}/?database={_env('YDB_DATABASE')}" if _env("YDB_ENDPOINT") else ""))

    monthly_budget: float = field(default_factory=lambda: _float("MONTHLY_BUDGET_RUB", 500))

    @property
    def secret_ok(self) -> bool:
        return bool(self.session_secret)


settings = Settings()


def reload_settings() -> Settings:
    global settings
    settings = Settings()
    return settings
