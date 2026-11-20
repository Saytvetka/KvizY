"""Вопросы: виды, разбор ответа ИИ (терпимо), проверка кодом, баллы.

Как вопрос хранится в базе (поля options / answer — JSON):
    single  один из 4      options: [4 строки]            answer: {"correct": [i]}
    multi   несколько      options: [3–6 строк]           answer: {"correct": [i, j, ...]}
    tf      верно/неверно  options: ["Верно","Неверно"]   answer: {"value": true|false}
    open    своими словами options: []                    answer: {"reference": "...", "key_points": [...]}
    match   соответствие   options: {"left": [...], "right": [...]}  (left[i] ↔ right[i])
                                                          answer: {"pairs": [[l, r], ...]}
    order   порядок шагов  options: {"items": [...]} (правильный порядок; на экране перемешиваем)
                                                          answer: {"order": [...]}
    fill    вставить слово текст с «___»; options: []     answer: {"accepted": ["слово", "синоним"]}

Пояснение (explain, JSON): {"why": почему верный ответ верен, "trap": почему ловушка неверна,
"remember": что запомнить, "answer_text": правильный ответ словами — по нему код ловит противоречия}.
verified = False — ИИ дважды прислал противоречие (ответ не сходится с пояснением): вопрос
помечается «не проверено» и в баллы не идёт.
"""
from __future__ import annotations

import hashlib
import random
import re

from app import config
from app.ai import to_number
from app.quiz.material import QuoteIndex, norm_for_match

TYPES = ["single", "multi", "tf", "open", "match", "order", "fill"]
TYPE_NAMES = {
    "single": "один вариант", "multi": "несколько вариантов", "tf": "верно / неверно",
    "open": "открытый ответ", "match": "соответствие", "order": "порядок шагов", "fill": "вставить пропущенное",
}
# Баллы за вопрос по виду. Меняй здесь.
POINTS = {"single": 1, "tf": 1, "fill": 1, "multi": 2, "match": 2, "order": 2, "open": 2}
# Доли видов вопросов и всё остальное про уровни — в app/config.py → LEVELS.
DIFFICULTIES = {k: v["name"] for k, v in config.LEVELS.items()}
CONTRADICTION = "противоречие: ответ не сходится с пояснением"


def level(difficulty: str | None) -> dict:
    return config.LEVELS.get(difficulty or "", config.LEVELS[config.LEVEL_DEFAULT])


def hint_mode(difficulty: str | None, hints_on: bool) -> str:
    """all | outside | none — у каких вопросов будут подсказки (решает уровень, выключатель в форме — тоже)."""
    return level(difficulty)["hints"] if hints_on else "none"


def points_for(qtype: str, difficulty: str | None) -> float:
    """Баллы за вопрос: за вид (POINTS) × множитель уровня."""
    return round(float(POINTS.get(qtype, 1)) * float(level(difficulty)["multiplier"]), 2)
FLAGS = {"": "", "outside": "вне файла", "logic": "на логику"}

_TYPE_SYN = {
    "single": ["single", "one", "single_choice", "singlechoice", "mcq", "choice", "один", "один вариант", "один из 4", "radio"],
    "multi": ["multi", "multiple", "multiple_choice", "multichoice", "multi_choice", "checkbox", "несколько", "несколько вариантов"],
    "tf": ["tf", "true_false", "truefalse", "true/false", "bool", "boolean", "yesno", "верно/неверно", "верно-неверно", "да/нет"],
    "open": ["open", "free", "free_text", "text", "essay", "short_answer", "открытый", "открытый ответ", "своими словами"],
    "match": ["match", "matching", "pairs", "соответствие", "сопоставление", "mapping"],
    "order": ["order", "ordering", "sequence", "sort", "порядок", "последовательность", "порядок шагов"],
    "fill": ["fill", "gap", "cloze", "blank", "fill_blank", "fill_in", "fill_in_the_blank", "пропуск", "вставить", "вставить пропущенное"],
}
_DIFF_SYN = {"easy": ["easy", "легкий", "лёгкий", "простой", "low"],
             "medium": ["medium", "средний", "normal", "mid"],
             "hard": ["hard", "сложный", "на размышление", "трудный", "high", "difficult"]}
_FLAG_SYN = {"outside": ["outside", "вне файла", "вне материала", "external", "beyond", "out"],
             "logic": ["logic", "на логику", "логика", "reasoning", "логический"]}


def _syn(value, table: dict, default: str) -> str:
    v = str(value or "").strip().lower().replace("ё", "е")
    for key, words in table.items():
        if v == key or v in [w.replace("ё", "е") for w in words]:
            return key
    return default


def _get(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def type_plan(n: int, difficulty: str | None = None, allowed: list[str] | None = None) -> dict[str, int]:
    """Сколько вопросов каждого вида просить у ИИ — по долям уровня (config.LEVELS[...]["types"]).
    Делим «по остаткам», поэтому сумма всегда ровно n и результат одинаковый при каждом вызове."""
    share = level(difficulty)["types"]
    allowed = [t for t in (allowed or TYPES) if share.get(t)]
    shares = {t: share[t] for t in allowed}
    s = sum(shares.values()) or 1
    raw = {t: n * v / s for t, v in shares.items()}
    plan = {t: int(v) for t, v in raw.items()}
    for t in sorted(raw, key=lambda t: (raw[t] - plan[t], shares[t]), reverse=True)[: n - sum(plan.values())]:
        plan[t] += 1
    return {t: c for t, c in plan.items() if c}


_OPT_MARK = re.compile(r"^\s*(?:[A-FА-Еa-fа-е]|\d{1,2})[.)]\s+")


def _clean_opt(s) -> str:
    if isinstance(s, dict):
        s = _get(s, "text", "option", "value", "label", "вариант", default="")
    return _OPT_MARK.sub("", str(s or "")).strip()


def _letters_to_index(v, options: list[str]) -> int | None:
    """Правильный ответ мог прийти как индекс, буква («B», «Б»), номер или текст варианта."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    letters_lat, letters_cyr = "abcdef", "абвгде"
    low = s.lower().rstrip(").")
    if len(low) == 1 and low in letters_lat:
        return letters_lat.index(low)
    if len(low) == 1 and low in letters_cyr:
        return letters_cyr.index(low)
    cleaned = _clean_opt(s).lower()
    for i, o in enumerate(options):
        if o.lower() == cleaned:
            return i
    return None


def _indices(raw, options: list[str]) -> list[int]:
    items = raw if isinstance(raw, list) else [raw]
    idx = [_letters_to_index(v, options) for v in items]
    idx = [i for i in idx if i is not None]
    ints_given = all(isinstance(v, (int, float)) or re.fullmatch(r"\d+", str(v).strip()) for v in items)
    # Индексы с 0 или с 1? Если 0 нигде нет, а максимум = числу вариантов — значит с 1.
    if ints_given and idx and 0 not in idx and max(idx) == len(options):
        idx = [i - 1 for i in idx]
    return sorted({i for i in idx if 0 <= i < len(options)})


def _bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "верно", "да", "yes", "1", "правда", "истина", "t", "в"):
        return True
    if s in ("false", "неверно", "нет", "no", "0", "ложь", "f", "н"):
        return False
    return None


def normalize(raw: dict, defaults: dict | None = None) -> dict:
    """Приводим вопрос от ИИ к нашему виду. Понимает синонимы полей и форматов."""
    d = defaults or {}
    if not isinstance(raw, dict):
        return {"qtype": "?", "text": str(raw)}
    q: dict = {
        "slot": raw.get("slot") or raw.get("id"),
        "topic": str(_get(raw, "topic", "тема", "theme", "section", default=d.get("topic", "")) or "").strip(),
        "subtopic": str(_get(raw, "subtopic", "подтема", "subtheme", "sub_topic", default=d.get("subtopic", "")) or "").strip(),
        "qtype": _syn(_get(raw, "type", "qtype", "kind", "тип", default=d.get("qtype", "")), _TYPE_SYN, "?"),
        "difficulty": _syn(_get(raw, "difficulty", "сложность", "level", default=d.get("difficulty", "medium")),
                           _DIFF_SYN, d.get("difficulty", "medium")),
        "flag": _syn(_get(raw, "flag", "mark", "пометка", "tag", "label", default=""), _FLAG_SYN, ""),
        "text": str(_get(raw, "text", "question", "вопрос", "prompt", "stem", default="") or "").strip(),
        "explanation": "",
        "quote": str(_get(raw, "quote", "citation", "цитата", "source_quote", "evidence", default="") or "").strip().strip("«»\"“”"),
        "hint": str(_get(raw, "hint", "подсказка", "clue", default="") or "").strip(),
    }
    q["explain"] = _explain(raw)
    q["explanation"] = explain_text(q["explain"])
    t = q["qtype"]
    opts_raw = _get(raw, "options", "variants", "choices", "варианты", "answers_options", default=[])
    correct_raw = _get(raw, "correct", "correct_answer", "correct_answers", "correct_index", "correct_indices",
                       "right", "правильный", "правильный_ответ", "answer", "answers", default=None)

    if t in ("single", "multi"):
        opts = opts_raw if isinstance(opts_raw, list) else []
        # варианты вида [{"text": "...", "correct": true}]
        flagged = [i for i, o in enumerate(opts) if isinstance(o, dict) and _bool(o.get("correct", o.get("is_correct"))) is True]
        options = [_clean_opt(o) for o in opts]
        correct = flagged if flagged else _indices(correct_raw, options) if correct_raw is not None else []
        q["options"], q["answer"] = options, {"correct": correct}
    elif t == "tf":
        val = _bool(correct_raw if correct_raw is not None else raw.get("value"))
        q["options"], q["answer"] = ["Верно", "Неверно"], {"value": val}
    elif t == "open":
        # Образец ответа ИИ называет по-разному и кладёт куда попало — собираем из всех мест.
        ref = _get(raw, "reference", "reference_answer", "model_answer", "sample_answer", "example_answer",
                   "expected_answer", "ideal_answer", "образец", "эталон", "образец_ответа", default="")
        if not ref:
            ref = correct_raw
        if isinstance(ref, dict):
            ref = _get(ref, "text", "answer", "reference", "value", default="")
        if isinstance(ref, list):
            ref = "; ".join(str(x) for x in ref if x)
        kp = _get(raw, "key_points", "keypoints", "key_ideas", "ключевые_мысли", "главные_мысли", "criteria", default=[])
        if not str(ref or "").strip():
            # нет образца — собираем его из главных мыслей или объяснения
            ref = "; ".join(str(x) for x in kp) if isinstance(kp, list) and kp else q["explanation"]
        q["options"], q["answer"] = [], {"reference": str(ref or "").strip(),
                                         "key_points": [str(x) for x in kp] if isinstance(kp, list) else [str(kp)]}
    elif t == "match":
        pairs_raw = _get(raw, "pairs", "matches", "соответствия", "mapping", default=None)
        pairs: list[list[str]] = []
        if isinstance(pairs_raw, dict):
            pairs = [[str(k), str(v)] for k, v in pairs_raw.items()]
        elif isinstance(pairs_raw, list):
            for p in pairs_raw:
                if isinstance(p, dict):
                    left = _get(p, "left", "term", "a", "слева", "key")
                    right = _get(p, "right", "definition", "b", "справа", "value")
                    if left is not None and right is not None:
                        pairs.append([str(left), str(right)])
                elif isinstance(p, (list, tuple)) and len(p) == 2:
                    pairs.append([str(p[0]), str(p[1])])
        pairs = [[_clean_opt(a), _clean_opt(b)] for a, b in pairs]
        q["options"] = {"left": [a for a, _ in pairs], "right": [b for _, b in pairs]}
        q["answer"] = {"pairs": pairs}
    elif t == "order":
        items = _get(raw, "items", "steps", "sequence", "шаги", "order", default=None)
        if items is None and isinstance(opts_raw, list):
            items = opts_raw
            if isinstance(correct_raw, list) and all(isinstance(x, (int, float)) or str(x).isdigit() for x in correct_raw):
                idx = [int(x) for x in correct_raw]
                if idx and min(idx) == 1:
                    idx = [i - 1 for i in idx]
                if sorted(idx) == list(range(len(items))):
                    items = [items[i] for i in idx]
        items = [_clean_opt(x) for x in (items or [])]
        q["options"], q["answer"] = {"items": items}, {"order": items}
    elif t == "fill":
        acc = _get(raw, "accepted", "answers", "correct", "answer", "correct_answer", "правильный", default=[])
        acc = acc if isinstance(acc, list) else [acc]
        acc = [str(a).strip() for a in acc if str(a).strip()]
        text = q["text"]
        if "___" not in text:
            text = re.sub(r"_{2,}|\.{4,}|…{2,}|\[\s*\]|\(\s*\)", "___", text)
        q["text"] = text
        q["options"], q["answer"] = [], {"accepted": acc}
    else:
        q["options"], q["answer"] = [], {}
    q["points"] = float(POINTS.get(t, 1))
    if raw.get("points") is not None and to_number(raw.get("points"), 0) > 0:
        q["points"] = float(POINTS.get(t, 1))  # баллы ставит код, а не ИИ
    return q


_EXPL_FIELDS = {
    "why": ("why", "why_right", "why_correct", "почему_верно", "почему", "reason"),
    "trap": ("trap", "why_wrong", "distractor", "ловушка", "почему_неверно", "почему_ловушка_неверна"),
    "remember": ("remember", "takeaway", "key_idea", "запомни", "что_запомнить", "запомнить"),
}


def _explain(raw: dict) -> dict:
    """Пояснение ИИ → {why, trap, remember, answer_text}. Понимает и строку, и объект, и поля сверху."""
    ex = _get(raw, "explain", "explanation", "объяснение", "пояснение", "rationale", default="")
    src = dict(raw)
    if isinstance(ex, dict):
        src.update(ex)
        ex = ""
    out = {k: str(_get(src, *names, default="") or "").strip() for k, names in _EXPL_FIELDS.items()}
    if not out["why"] and ex:
        out["why"] = str(ex).strip()
    at = _get(src, "answer_text", "correct_text", "right_answer_text", "ответ_словами", "правильный_ответ_словами", default="")
    if isinstance(at, list):
        at = "; ".join(str(x) for x in at if x)
    out["answer_text"] = str(at or "").strip()
    return out


def explain_text(ex: dict) -> str:
    """Пояснение одной строкой (для старых экранов и для поиска)."""
    parts = [ex.get("why", "")]
    if ex.get("trap"):
        parts.append("Ловушка: " + ex["trap"])
    if ex.get("remember"):
        parts.append("Запомни: " + ex["remember"])
    return " ".join(p.strip() for p in parts if p and p.strip())


def _close(a: str, b: str) -> float:
    """Насколько похожи два коротких текста (0..1): равны, один внутри другого, общие слова."""
    na, nb = norm_for_match(a), norm_for_match(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if f" {na} " in f" {nb} " or f" {nb} " in f" {na} ":
        return 0.85
    wa, wb = set(na.split()), set(nb.split())
    return len(wa & wb) / len(wa | wb)


def _mentions(text: str, option: str) -> bool:
    """Вариант ответа целиком встречается в тексте (для вариантов от 2 слов или от 6 букв)."""
    no, nt = norm_for_match(option), norm_for_match(text)
    if not no or (len(no.split()) < 2 and len(no) < 6):
        return False
    return f" {no} " in f" {nt} "


def contradiction(q: dict) -> str:
    """Код сверяет правильный ответ с пояснением. Пустая строка — противоречий не видно.
    Сверяем «ответ словами» (answer_text) с отмеченным вариантом и то, какой вариант
    назван в «почему верно». Если данных для сверки нет — противоречием это не считаем."""
    t = q.get("qtype")
    ex = q.get("explain") or {}
    at = ex.get("answer_text", "")
    a = q.get("answer") or {}
    opts = q.get("options") or []
    if t == "single" and len(opts) >= 2 and a.get("correct"):
        c = a["correct"][0]
        if at:
            sc = [_close(at, o) for o in opts]
            best = max(range(len(opts)), key=lambda i: sc[i])
            if sc[best] >= 0.5 and best != c and sc[best] > sc[c] + 0.15:
                return f"ответ отмечен «{opts[c]}», а в пояснении — «{opts[best]}»"
        why = ex.get("why", "")
        named = [i for i, o in enumerate(opts) if _mentions(why, o)]
        if len(named) == 1 and named[0] != c and not _mentions(why, opts[c]):
            return f"в пояснении верным назван «{opts[named[0]]}», а отмечен «{opts[c]}»"
    elif t == "multi" and at and opts:
        items = [x for x in re.split(r";|\n", at) if x.strip()]
        picked = set()
        for it in items:
            sc = [_close(it, o) for o in opts]
            best = max(range(len(opts)), key=lambda i: sc[i])
            if sc[best] >= 0.5:
                picked.add(best)
        if picked and not picked <= set(a.get("correct", [])):
            return "в пояснении верными названы другие варианты"
    elif t == "tf" and at and a.get("value") is not None:
        low = norm_for_match(at)
        said = (False if re.match(r"(неверно|ложь|нет|false|не верно)\b", low)
                else True if re.match(r"(верно|правда|да|true|истина)\b", low) else None)
        if said is not None and said != a["value"]:
            return "в пояснении сказано «" + ("верно" if said else "неверно") + "», а отмечено обратное"
    elif t == "fill" and at and a.get("accepted"):
        from app.quiz.scoring import fill_ok
        if not fill_ok(at, a["accepted"]) and max(_close(at, x) for x in a["accepted"]) < 0.5:
            return f"в пояснении правильное слово «{at}», а принимается «{a['accepted'][0]}»"
    return ""


def problems(q: dict, quotes: QuoteIndex | None) -> list[str]:
    """Что не так с вопросом. Пустой список — вопрос годится.
    Отдельная проблема "quote" — цитаты нет в тексте."""
    p: list[str] = []
    t = q.get("qtype")
    if t not in TYPES:
        p.append("неизвестный вид вопроса")
        return p
    if len(q.get("text", "")) < 8:
        p.append("нет текста вопроса")
    a = q.get("answer") or {}
    if t == "single":
        if len(q["options"]) != 4 or len(set(o.lower() for o in q["options"])) != 4 or any(not o for o in q["options"]):
            p.append("нужно ровно 4 разных варианта")
        if len(a.get("correct", [])) != 1:
            p.append("нужен ровно 1 правильный вариант")
    elif t == "multi":
        n = len(q["options"])
        if n < 4 or n > 6 or len(set(o.lower() for o in q["options"])) != n:
            p.append("нужно 4–6 разных вариантов")
        c = len(a.get("correct", []))
        if c < 2 or c >= n:
            p.append("нужно от 2 правильных и хотя бы 1 неправильный")
    elif t == "tf":
        if a.get("value") is None:
            p.append("не указано, верно или неверно")
    elif t == "open":
        if len(a.get("reference", "")) < 5:
            p.append("нет образца ответа")
    elif t == "match":
        pairs = a.get("pairs", [])
        if len(pairs) < 3 or len(pairs) > 6:
            p.append("нужно 3–6 пар")
        if len({x[0].lower() for x in pairs}) != len(pairs) or len({x[1].lower() for x in pairs}) != len(pairs):
            p.append("пары повторяются")
    elif t == "order":
        items = a.get("order", [])
        if len(items) < 3 or len(items) > 7 or len({i.lower() for i in items}) != len(items):
            p.append("нужно 3–7 разных шагов")
    elif t == "fill":
        if q["text"].count("___") != 1:
            p.append("в тексте должен быть ровно один пропуск ___")
        if not a.get("accepted"):
            p.append("нет правильного слова")
    if contradiction(q):
        p.append(CONTRADICTION)
    if quotes is not None:
        if q.get("quote"):
            if not quotes.contains(q["quote"]) and q.get("flag") != "outside":
                p.append("quote")
        elif q.get("flag") != "outside":
            p.append("quote")
    return p


def fingerprint(text: str) -> set[str]:
    return {w for w in norm_for_match(text).split() if len(w) > 2}


def question_key(qtype: str, text: str, options) -> str:
    """Текст для сравнения вопросов «похож или нет». У соответствий и порядка формулировка часто одна
    («Расставь по порядку…»), а разница — в пунктах, поэтому пункты добавляем к тексту."""
    if qtype == "match" and isinstance(options, dict):
        return f"{text} " + " ".join(options.get("left", []))
    if qtype == "order" and isinstance(options, dict):
        return f"{text} " + " ".join(options.get("items", []))
    return text


def key_of(q) -> str:
    """question_key для вопроса-словаря или строки базы."""
    from app.db import loads
    if isinstance(q, dict):
        return question_key(q.get("qtype", ""), q.get("text", ""), q.get("options"))
    return question_key(q.qtype, q.text, loads(q.options, []))


def is_duplicate(text: str, others: list[str], threshold: float = 0.8) -> bool:
    """Похожий вопрос уже есть? (совпадение слов ≥ threshold, по умолчанию 80 %)"""
    a = fingerprint(text)
    if not a:
        return False
    for o in others:
        b = fingerprint(o)
        if not b:
            continue
        if norm_for_match(text) == norm_for_match(o) or len(a & b) / len(a | b) >= threshold:
            return True
    return False


def code_hint(q: dict) -> str:
    """Подсказка без ИИ (запасной вариант): наводит, но ответ не выдаёт."""
    t, a, o = q.get("qtype"), q.get("answer") or {}, q.get("options") or []
    try:
        if t == "single":
            wrong = [x for i, x in enumerate(o) if i not in a.get("correct", [])]
            return f"Точно не «{wrong[len(q.get('text', '')) % len(wrong)]}»." if wrong else ""
        if t == "multi":
            return f"Верных вариантов здесь {len(a.get('correct', []))}."
        if t == "tf":
            return "Сверь утверждение с материалом: подвох обычно в одном слове."
        if t == "fill":
            w = (a.get("accepted") or [""])[0]
            return f"Слово начинается на «{w[:1]}», в нём {len(w)} букв." if w else ""
        if t == "match":
            pairs = a.get("pairs") or []
            return f"Одна пара: «{pairs[0][0]}» → «{pairs[0][1]}»." if pairs else ""
        if t == "order":
            items = a.get("order") or []
            return f"Первый шаг: «{items[0]}»." if items else ""
        if t == "open":
            kp = a.get("key_points") or []
            return f"Начни с мысли: {' '.join(str(kp[0]).split()[:4])}…" if kp else "Ответь 1–3 предложениями: что это и почему."
    except (IndexError, KeyError, TypeError, ZeroDivisionError):
        pass
    return ""


def _hint_gives_answer(q: dict) -> bool:
    h = q.get("hint", "")
    a = q.get("answer") or {}
    if q.get("qtype") in ("single", "multi"):
        return any(_mentions(h, q["options"][i]) for i in a.get("correct", []) if i < len(q.get("options", [])))
    if q.get("qtype") == "fill":
        return any(norm_for_match(x) and norm_for_match(x) in norm_for_match(h).split() for x in a.get("accepted", []))
    return False


def finalize(q: dict, hints: str | bool, difficulty: str | None = None) -> dict:
    """Последние правки перед сохранением (всё кодом):
    - подсказки по режиму уровня: all — у всех, outside — только «вне файла», none — нет;
      если ИИ подсказку не дал или она выдаёт ответ — подсказка от кода;
    - у вопросов «шире файла» цитаты нет (чтобы не ссылаться на то, чего в файле нет);
    - варианты перемешиваются; баллы = вид × множитель уровня."""
    mode = hints if isinstance(hints, str) else ("outside" if hints else "none")
    need = mode == "all" or (mode == "outside" and q.get("flag") == "outside")
    if not need:
        q["hint"] = ""
    elif not q.get("hint") or _hint_gives_answer(q):
        q["hint"] = code_hint(q)
    if q.get("flag") == "outside":
        q["quote"] = ""
    if q["qtype"] in ("single", "multi"):
        # ИИ любит ставить правильный ответ первым — перемешиваем (одинаково при каждом сохранении).
        seed = int(hashlib.md5(q["text"].encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        idx = list(range(len(q["options"])))
        rnd.shuffle(idx)
        correct = set(q["answer"]["correct"])
        q["options"] = [q["options"][i] for i in idx]
        q["answer"] = {"correct": sorted(n for n, i in enumerate(idx) if i in correct)}
    q["points"] = points_for(q["qtype"], difficulty or q.get("difficulty"))
    q.setdefault("verified", True)
    return q


def public(question, with_answer: bool = False) -> dict:
    """Вопрос для экрана. Без правильного ответа (его покажем только в итоге)."""
    from app.db import loads

    opts = loads(question.options, [])
    out = {
        "id": question.id, "position": question.position, "topic": question.topic, "subtopic": question.subtopic,
        "qtype": question.qtype, "type_name": TYPE_NAMES.get(question.qtype, ""), "difficulty": question.difficulty,
        "flag": question.flag or "", "flag_name": FLAGS.get(question.flag or "", ""), "text": question.text,
        "points": question.points, "hint": question.hint or "", "replace_count": question.replace_count or 0,
        "verified": question.verified is not False,
    }
    if question.qtype == "match":
        rnd = random.Random(question.id)
        right = list(opts.get("right", []))
        rnd.shuffle(right)
        out["options"] = {"left": opts.get("left", []), "right": right}
    elif question.qtype == "order":
        rnd = random.Random(question.id + "o")
        items = list(opts.get("items", []))
        for _ in range(5):
            rnd.shuffle(items)
            if items != opts.get("items", []):
                break
        out["options"] = {"items": items}
    else:
        out["options"] = opts
    if with_answer:
        out["answer"] = loads(question.answer, {})
        out["explanation"] = question.explanation or ""
        out["explain"] = loads(question.explain, {}) or {}
        out["quote"] = question.quote or ""
        out["correct_options"] = opts
    return out


def snapshot(question) -> dict:
    """Копия вопроса на момент ответа (вопрос потом могут заменить)."""
    from app.db import loads

    return {"text": question.text, "qtype": question.qtype, "options": loads(question.options, []),
            "answer": loads(question.answer, {}), "explanation": question.explanation or "",
            "explain": loads(question.explain, {}) or {}, "verified": question.verified is not False,
            "quote": question.quote or "", "topic": question.topic, "subtopic": question.subtopic,
            "points": question.points, "flag": question.flag or "", "difficulty": question.difficulty}
