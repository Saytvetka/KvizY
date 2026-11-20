"""Тексты всех запросов к ИИ. Общие правила (понимание, русский, без выдумок, дословная цитата,
только JSON) добавляются автоматически в app/ai/__init__.py (SYSTEM_RULES).

Хочешь поменять, какие вопросы делает ИИ, — правь здесь.
"""
from __future__ import annotations

import json

from app.quiz.questions import DIFFICULTIES, TYPE_NAMES, level

SCHEMA = """{
  "topics": [{"name": "Тема", "subtopics": ["Подтема 1", "Подтема 2"]}],
  "questions": [
    {"type": "single", "topic": "Тема", "subtopic": "Подтема 1", "flag": "", "text": "Вопрос?",
     "options": ["вариант", "вариант", "вариант", "вариант"], "correct": 2,
     "explain": {"answer_text": "текст правильного варианта дословно",
                 "why": "почему верный ответ верен (1–2 предложения)",
                 "trap": "почему самый соблазнительный неверный вариант неверен (1 предложение)",
                 "remember": "что запомнить (1 короткое предложение)"},
     "quote": "дословный кусок из материала", "hint": ""},
    {"type": "multi", ..., "options": ["...", "...", "...", "...", "..."], "correct": [0, 3],
     "explain": {"answer_text": "вариант; вариант", ...}},
    {"type": "tf", ..., "text": "Утверждение.", "correct": true, "explain": {"answer_text": "верно", ...}},
    {"type": "open", ..., "text": "Объясни, почему ...", "reference": "образец ответа", "key_points": ["мысль 1", "мысль 2"]},
    {"type": "match", ..., "text": "Соотнеси ...", "pairs": [{"left": "термин", "right": "что это"}, ... 3–5 пар]},
    {"type": "order", ..., "text": "Расставь по порядку ...", "items": ["шаг 1", "шаг 2", "шаг 3", "шаг 4"]},
    {"type": "fill", ..., "text": "Предложение с пропуском ___ внутри.", "accepted": ["слово", "синоним"],
     "explain": {"answer_text": "слово", ...}}
  ]
}"""

TYPE_RULES = """Правила по видам:
- single: ровно 4 варианта, ровно 1 правильный, correct — номер правильного (с 0).
- multi: 4–6 вариантов, правильных от 2 и не все; correct — список номеров (с 0).
- tf: утверждение, correct — true или false. Делай и верные, и неверные утверждения.
- open: вопрос, на который отвечают 1–3 предложениями своими словами; ОБЯЗАТЕЛЬНО reference — образец ответа, key_points — 2–4 главные мысли.
- match: 3–5 пар, items в left разные, в right разные.
- order: 3–6 шагов процесса в ПРАВИЛЬНОМ порядке (перемешает программа). Только если в материале есть последовательность.
- fill: одно предложение с ОДНИМ пропуском ___, accepted — правильное слово и синонимы (другие падежи и число программа засчитает сама).
Поле flag: "outside" — вопрос шире материала (общеизвестное рядом с темой, ответа в файле нет; quote тогда ""),
"logic" — вопрос на рассуждение, "" — обычный."""

EXPLAIN_RULES = """Пояснение explain — коротко, без воды и без повтора вопроса:
- answer_text — правильный ответ словами (для single/multi — текст верных вариантов ДОСЛОВНО, для tf — «верно» или «неверно»);
  он обязан совпадать с correct, программа это проверяет;
- why — почему верный ответ верен; trap — почему самый соблазнительный неверный вариант неверен;
- remember — одна мысль, которую стоит запомнить.
Не вставляй в пояснение цитаты из материала: цитата — только в поле quote, ДОСЛОВНО из материала.
Если вопрос шире материала (flag "outside"), в why так и напиши: «В материале этого нет, это общее знание», quote — ""."""


def _plan_text(plan: dict[str, int]) -> str:
    return ", ".join(f"{TYPE_NAMES[t]} ({t}) — {n}" for t, n in plan.items())


def hints_rule(mode: str) -> str:
    if mode == "all":
        return "hint — подсказка в 1 предложение у КАЖДОГО вопроса: наводит, но не называет ответ."
    if mode == "outside":
        return "hint — подсказка в 1 предложение ТОЛЬКО у вопросов с flag \"outside\" (не называет ответ). У остальных hint пустой."
    return "Подсказки не нужны: hint везде пустой."


def level_text(difficulty: str) -> str:
    """Что уровень требует от вопросов — явно, для ИИ."""
    lv = level(difficulty)
    return "\n".join([
        f"Уровень: {lv['style']}.",
        f"Неправильные варианты: {lv['distractors']}.",
    ])


def question_slots(n: int, plan: dict[str, int], difficulty: str, seed: int = 0) -> list[tuple[str, str]]:
    """План на каждый вопрос: (вид, подход). Виды перемешаны, подходы уровня идут по кругу."""
    import random
    rnd = random.Random(seed)
    types = [t for t, c in plan.items() for _ in range(c)]
    rnd.shuffle(types)
    angles = list(level(difficulty)["angles"])
    rnd.shuffle(angles)
    return [(t, angles[i % len(angles)]) for i, t in enumerate(types[:n])]


def quiz_prompt(*, title: str, subject: str, n: int, difficulty: str, plan: dict[str, int], hints: str | bool,
                material: str, instruction: str = "", part: tuple[int, int] | None = None,
                known_topics: list[str] | None = None, avoid: list[str] | None = None, seed: int = 0,
                retry: bool = False) -> str:
    lv = level(difficulty)
    mode = hints if isinstance(hints, str) else ("outside" if hints else "none")
    outside = round(n * lv["outside_share"])
    slots = question_slots(n, plan, difficulty, seed)
    slot_lines = "\n".join(f"{i}) вид {t} ({TYPE_NAMES[t]}); подход — {a}" for i, (t, a) in enumerate(slots, start=1))
    open_n = plan.get("open", 0)
    lines = [
        f"Сделай квиз по материалу ниже. Предмет: «{subject}». Тема квиза: «{title}».",
        f"Нужно РОВНО {n} вопросов.",
        level_text(difficulty),
        f"Смесь видов (СТРОГО): {_plan_text(plan)}. Открытых ответов (open) — ровно {open_n}.",
        "План вопросов по порядку — вид и подход у каждого:\n" + slot_lines,
        "Вопросы должны быть РАЗНЫМИ: разные факты, разные стороны темы, разные формулировки. "
        "Не начинай больше двух вопросов одинаково (например, «Что такое…»).",
        "Сначала сам раздели материал на темы (2–6) и, где тема большая, на подтемы (2–4). "
        "Каждый вопрос привяжи к теме и подтеме из этого списка. Вопросы распредели по всем темам.",
        (f"Ровно {outside} вопрос(а) сделай шире материала (flag \"outside\"), остальные — строго по материалу."
         if outside else "Все вопросы — строго по материалу: ответ есть в тексте (flag \"outside\" не используй)."),
        hints_rule(mode),
        EXPLAIN_RULES,
    ]
    if part:
        lines.append(f"Это часть {part[0]} из {part[1]} большого материала — делай вопросы только по этой части.")
    if known_topics:
        lines.append("Уже есть темы (используй их названия, если подходят): " + "; ".join(known_topics[:12]) + ".")
    if instruction:
        lines.append(f"Указание человека, какую часть материала брать: «{instruction}». Делай вопросы только по этой части.")
    if avoid:
        head = ("Человек уже проходил эту тему и видел вопросы ниже. Сделай ДРУГИЕ: другие факты, другие формулировки, "
                "другой подход (если там было «что такое», спроси «почему» или «что будет, если»). Похожие вопросы "
                "программа выбросит:" if retry else "НЕ повторяй эти вопросы (и не делай очень похожих):")
        lines.append(head + "\n" + "\n".join(f"- {a}" for a in avoid[:60]))
    lines += [TYPE_RULES, "Формат ответа — JSON:", SCHEMA, "Материал:", "<<<", material, ">>>"]
    return "\n".join(lines)


def replace_prompt(*, slots: list[dict], material: str, hints: str | bool, subject: str, title: str) -> str:
    """slots: [{slot, topic, subtopic, type, difficulty, flag, avoid: [...]}] — по одному новому вопросу на слот.
    type = "any" + not_type — «любой вид, кроме этого» (кнопка «заменить вопрос»)."""
    def kind(s):
        if s["type"] == "any":
            return f"любой вид, КРОМЕ {s.get('not_type')} ({TYPE_NAMES.get(s.get('not_type'), '')}); выбери тот, что лучше подходит к материалу"
        return s["type"]
    want = [{"slot": s["slot"], "type": kind(s), "topic": s.get("topic", ""), "subtopic": s.get("subtopic", ""),
             "difficulty": DIFFICULTIES.get(s.get("difficulty", "medium"), "средний"), "flag": s.get("flag", ""),
             "не_похож_на": s.get("avoid", [])[:25]} for s in slots]
    mode = hints if isinstance(hints, str) else ("outside" if hints else "none")
    diff = slots[0].get("difficulty", "medium") if slots else "medium"
    return "\n".join([
        f"Нужны новые вопросы для квиза «{title}» (предмет «{subject}»). Сделай по одному вопросу на каждый слот ниже.",
        level_text(diff),
        "Новый вопрос — той же темы, подтемы и сложности, что указаны в слоте, вида — как сказано в поле type слота, "
        "и НЕ похожий на вопросы из «не_похож_на» (другой факт или другая сторона темы).",
        "В ответе поле type — один из кодов: single, multi, tf, open, match, order, fill.",
        "У open обязательно заполни reference (образец ответа) и key_points.",
        hints_rule(mode),
        "Слоты:", json.dumps(want, ensure_ascii=False, indent=1),
        TYPE_RULES, EXPLAIN_RULES,
        'Формат ответа — JSON: {"questions": [{"slot": "…", "type": "…", "topic": "…", "subtopic": "…", "flag": "…", '
        '"text": "…", …поля по виду…, "explain": {"answer_text": "…", "why": "…", "trap": "…", "remember": "…"}, '
        '"quote": "дословный кусок материала", "hint": ""}]}',
        "Материал:", "<<<", material, ">>>",
    ])


def review_prompt(*, mistakes: list[dict], material: str, subject: str, title: str) -> str:
    """Разбор ошибок после квиза: по каждой теме, где человек ошибся, — понятное объяснение."""
    items = [{"тема": m["topic"], "подтема": m.get("subtopic", ""), "вопрос": m["text"], "ответ_ученика": m["your"],
              "правильный_ответ": m["right"], "цитата": m.get("quote", "")} for m in mistakes]
    return "\n".join([
        f"Ты — терпеливый репетитор. Ученик прошёл квиз «{title}» (предмет «{subject}») и ошибся в вопросах ниже.",
        "Сделай разбор по ТЕМАМ (вопросы одной темы объедини). Для каждой темы:",
        "- explain: объясни суть темы простыми словами, 4–7 предложений, опираясь на материал; "
        "именно ту часть, где ученик ошибся;",
        "- key_points: 2–4 главные мысли, которые нужно запомнить;",
        "- mistake: в чём была ошибка ученика и как не путаться (по его ответам), 1–3 предложения;",
        "- example: короткий понятный пример или аналогия, 1–3 предложения;",
        "- reread: дословный кусок материала, который стоит перечитать (10–40 слов).",
        "Ошибки ученика:", json.dumps(items, ensure_ascii=False, indent=1),
        'Формат ответа — JSON: {"topics": [{"topic": "…", "explain": "…", "key_points": ["…"], "mistake": "…", '
        '"example": "…", "reread": "…"}]}',
        "Материал:", "<<<", material, ">>>",
    ])


def check_open_prompt(*, question: str, reference: str, key_points: list[str], answer: str) -> str:
    return "\n".join([
        "Проверь ответ ученика на открытый вопрос ПО ОБРАЗЦУ ОТВЕТА ниже. Оценивай смысл, а не слова: "
        "синонимы и свои формулировки — нормально. Сравни ответ с каждой главной мыслью образца.",
        "Оценка: 1 — ответ полностью верный (есть главное), 0.5 — частично (верно, но не хватает важного или есть неточность), "
        "0 — неверно, не по теме или пусто.",
        f"Вопрос: {question}",
        f"Образец ответа: {reference}",
        ("Главные мысли: " + "; ".join(key_points)) if key_points else "",
        f"Ответ ученика: «{answer}»",
        'Формат ответа — JSON: {"score": 0 | 0.5 | 1, "comment": "1 предложение: что в ответе верно", '
        '"missing": ["главная мысль образца, которой нет в ответе", "…"]} (missing — пустой список, если всё есть)',
    ])
