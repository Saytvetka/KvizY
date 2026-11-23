"""Как делается каждый результат: билеты, конспект, ответ на вопрос, вопросы-карточки.

Общая схема (как в KvizY):
1. POST /api/results — текст из файлов/фото/ссылок/вставки → чистка → «откуда докуда» → сохраняем текст.
2. POST /api/results/{id}/run — долгий запрос: ИИ делает результат, экран раз в 1,5 с спрашивает /status.
   Большой материал режем на куски (split_text), чтобы каждый запрос влезал в модель.
3. Код проверяет и чинит ответ ИИ: темы, нумерация, дубли, цитаты (есть ли в тексте), варианты ответов.

ИИ пишет только содержание. Нумерация, подписи тем, проверки, хранение — код.
"""
from __future__ import annotations

import random
import re
import threading
from contextlib import contextmanager

import sqlalchemy as sa

from app import ai, config
from app.ai.base import AIError
from app.db import Result, ResultText, SessionLocal, dumps, loads, now
from app.work import mockai, prompts
from app.work.material import MaterialError, QuoteIndex, allocate, estimate_tokens, norm_for_match, split_text

TEXT_PART = 200_000  # символов в одной ячейке базы (у YDB есть предел на ячейку)


class WorkError(ValueError):
    """Понятная ошибка (показывается человеку)."""


# --- Текст материала ------------------------------------------------------------------

def save_text(result_id: str, text: str, user_id: str) -> None:
    with SessionLocal() as s:
        for i in range(0, max(1, len(text)), TEXT_PART):
            s.add(ResultText(user_id=user_id, result_id=result_id, part=i // TEXT_PART, text=text[i:i + TEXT_PART]))
        s.commit()


def load_text(result_id: str) -> str:
    with SessionLocal() as s:
        rows = s.scalars(sa.select(ResultText).where(ResultText.result_id == result_id).order_by(ResultText.part)).all()
    return "".join(r.text for r in rows)


def delete_result(result_id: str) -> None:
    """Удалить результат И извлечённый текст. Расходы (usage) остаются — по ним считается сумма за месяц."""
    with SessionLocal() as s:
        s.execute(sa.delete(ResultText).where(ResultText.result_id == result_id))
        s.execute(sa.delete(Result).where(Result.id == result_id))
        s.commit()


# --- Статус задачи ------------------------------------------------------------------

def set_status(result_id: str, status: str | None = None, text: str | None = None, error: str | None = None) -> None:
    with SessionLocal() as s:
        r = s.get(Result, result_id)
        if r is None:
            return
        if status:
            r.status = status
        if text is not None:
            r.status_text = text
        if error is not None:
            r.error = error
        r.heartbeat_at = now()
        s.commit()


@contextmanager
def heartbeat(result_id: str):
    """Пока идёт работа, раз в 20 секунд отмечаемся в базе. Упал сервер — через JOB_STALE_SECONDS «Повторить»."""
    stop = threading.Event()

    def beat():
        while not stop.wait(20):
            try:
                set_status(result_id)
            except Exception:  # база мигнула — отметимся в следующий раз
                pass

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


def check_stale(r: Result) -> Result:
    if r.status == "running" and r.heartbeat_at is not None:
        if (now() - r.heartbeat_at).total_seconds() > config.JOB_STALE_SECONDS:
            msg = "Сервер перезапускался во время работы. Нажми «Повторить»."
            set_status(r.id, "error", msg, msg)
            r.status, r.status_text, r.error = "error", msg, msg
    return r


# --- Объём ----------------------------------------------------------------------------

def limits(p) -> tuple[int, int]:
    """(токенов на ответ, токенов материала в одном запросе) для текущего провайдера."""
    out = min(p.max_output, 8000)
    return out, max(3000, p.context_tokens - out - 2500)


def chunks_for(material: str, p) -> list[str]:
    return split_text(material, limits(p)[1])


# --- Темы -----------------------------------------------------------------------------

def _s(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def file_names(material: str) -> list[str]:
    return re.findall(r"(?m)^\[Файл \d+: (.*?)\]$", material)


class Topics:
    """Список тем без дублей: «Электролиз», «электролиз » и «Электролиз.» — одна тема."""

    def __init__(self):
        self.names: list[str] = []
        self._norm: dict[str, str] = {}

    @staticmethod
    def norm(name: str) -> str:
        return norm_for_match(name)

    def add(self, name) -> str:
        if isinstance(name, dict):
            name = _first(name, "name", "title", "topic", "название", "тема", default="")
        name = _s(name).strip(" .:;—-")[:120]
        if not name:
            return ""
        k = self.norm(name)
        if k in self._norm:
            return self._norm[k]
        for nk, real in self._norm.items():  # «Электролиз» и «Электролиз растворов»
            if nk and k and (nk in k or k in nk) and min(len(nk), len(k)) >= 6:
                return real
        self._norm[k] = name
        self.names.append(name)
        return name

    def pick(self, name, fallback: str = "") -> str:
        return self.add(name) or fallback or (self.names[0] if self.names else "")


def default_topic(material: str, title: str) -> str:
    names = file_names(material)
    if len(names) == 1:
        return re.sub(r"\.\w{2,5}$", "", names[0])
    return title or "Материал"


# --- Билеты ---------------------------------------------------------------------------

def _ticket_questions(t) -> list[dict]:
    if isinstance(t, list):
        qs, ttopic = t, ""
    elif isinstance(t, dict):
        qs = _first(t, "questions", "вопросы", "items", default=[])
        ttopic = _first(t, "topic", "тема", default="")
        if isinstance(qs, str):
            qs = [qs]
    else:
        return []
    out = []
    for q in qs if isinstance(qs, list) else []:
        if isinstance(q, str):
            out.append({"text": _s(q), "topic": ttopic})
        elif isinstance(q, dict):
            out.append({"text": _s(_first(q, "text", "question", "вопрос", default="")),
                        "topic": _first(q, "topic", "тема", default=ttopic)})
    return [q for q in out if len(q["text"]) >= 5]


def make_tickets(material: str, note: str, rid: str, title: str, say) -> tuple[dict, list[str]]:
    p = ai.get_provider()
    out_tokens, _ = limits(p)
    parts = chunks_for(material, p)
    topics = Topics()
    tickets: list[list[dict]] = []
    seen: set[str] = set()
    chunk_topics: list[list[str]] = []
    warnings: list[str] = []
    fallback = default_topic(material, title)

    def take(data, idx: int):
        names = [topics.add(t) for t in (data.get("topics") or [])]
        if idx >= len(chunk_topics):
            chunk_topics.append([])
        chunk_topics[idx] += [n for n in names if n]
        for t in data.get("tickets") or data.get("билеты") or []:
            qs = []
            for q in _ticket_questions(t):
                key = norm_for_match(q["text"])
                if key in seen:
                    continue
                seen.add(key)
                q["topic"] = topics.pick(q["topic"], fallback)
                chunk_topics[idx].append(q["topic"])
                qs.append(q)
            # слишком длинный билет делим, пустые — пропускаем
            while len(qs) > config.TICKET_QUESTIONS + 1:
                tickets.append(qs[:config.TICKET_QUESTIONS])
                qs = qs[config.TICKET_QUESTIONS:]
            if qs:
                tickets.append(qs)

    for i, ch in enumerate(parts):
        say(f"составляю билеты… (часть {i + 1} из {len(parts)})" if len(parts) > 1 else "составляю билеты…")
        data = ai.ask_json("tickets", prompts.tickets(ch, note, (i + 1, len(parts))), expect=("tickets",),
                           result_id=rid, result_title=title, max_tokens=out_tokens, mock=lambda ch=ch: mockai.tickets(ch))
        take(data, i)

    # Темы без единого вопроса — один дополнительный запрос по той части, где тема нашлась.
    used = {q["topic"] for t in tickets for q in t}
    for i, ch in enumerate(parts):
        missing = [t for t in dict.fromkeys(chunk_topics[i] if i < len(chunk_topics) else []) if t not in used]
        if not missing:
            continue
        say("добавляю билеты по пропущенным темам…")
        try:
            data = ai.ask_json("tickets", prompts.tickets(ch, note, (i + 1, len(parts)), only_topics=missing),
                               expect=("tickets",), result_id=rid, result_title=title, max_tokens=out_tokens,
                               mock=lambda ch=ch, m=missing: mockai.tickets(ch, m))
            take(data, i)
        except AIError:
            pass
        used = {q["topic"] for t in tickets for q in t}
    left = [t for t in topics.names if t not in used]
    if left:
        warnings.append("Без билетов остались темы: " + ", ".join(left) + ". Можно создать билеты ещё раз с уточнением.")
    if not tickets:
        raise WorkError("ИИ не составил ни одного билета. Нажми «Повторить» или уточни, что нужно.")
    names = [t for t in topics.names if t in used] or [fallback]
    content = {"topics": names, "tickets": [{"n": i + 1, "questions": qs} for i, qs in enumerate(tickets)]}
    return content, warnings


# --- Конспект -------------------------------------------------------------------------

def _strs(v) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"\n+", v) if x.strip()]
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict):
            x = _first(x, "text", "point", "пункт", default="")
        x = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s+", "", _s(x))
        if x:
            out.append(x)
    return out


def make_summary(material: str, note: str, rid: str, title: str, say) -> tuple[dict, list[str]]:
    p = ai.get_provider()
    out_tokens, _ = limits(p)
    parts = chunks_for(material, p)
    topics = Topics()
    sections = []
    fallback = default_topic(material, title)
    for i, ch in enumerate(parts):
        say(f"пишу конспект… (часть {i + 1} из {len(parts)})" if len(parts) > 1 else "пишу конспект…")
        data = ai.ask_json("summary", prompts.summary(ch, note, (i + 1, len(parts))), expect=("sections",),
                           result_id=rid, result_title=title, max_tokens=out_tokens, mock=lambda ch=ch: mockai.summary(ch))
        for t in data.get("topics") or []:
            topics.add(t)
        for sec in data.get("sections") or data.get("разделы") or []:
            if not isinstance(sec, dict):
                continue
            terms = []
            for t in _first(sec, "terms", "термины", default=[]) or []:
                if isinstance(t, dict):
                    term = _s(_first(t, "term", "термин", "name", default=""))
                    d = _s(_first(t, "definition", "определение", "meaning", default=""))
                    if term and d:
                        terms.append({"term": term, "definition": d})
            points = _strs(_first(sec, "points", "пункты", "main", "главное", "text", default=[]))
            if not points and not terms:
                continue
            sections.append({"topic": topics.pick(_first(sec, "topic", "тема", default=""), fallback),
                             "title": _s(_first(sec, "title", "заголовок", "name", default="")) or "Раздел",
                             "points": points, "terms": terms})
    if not sections:
        raise WorkError("ИИ не написал конспект. Нажми «Повторить».")
    used = list(dict.fromkeys(s["topic"] for s in sections))
    return {"topics": used, "sections": sections}, []


# --- Ответ на вопрос ------------------------------------------------------------------

_STEM = re.compile(r"[а-яёa-z0-9]{3,}")


def _stems(text: str) -> set[str]:
    return {w[:5] for w in _STEM.findall(norm_for_match(text))}


def relevant_parts(material: str, question: str, max_tokens: int) -> str:
    """Материал не влезает в запрос: берём куски, где больше всего слов из вопроса (в исходном порядке)."""
    pieces = split_text(material, 1500)
    q = _stems(question)
    scored = []
    for i, piece in enumerate(pieces):
        st = _stems(piece)
        score = len(q & st) / (1 + len(q)) + (0.01 if i == 0 else 0)
        scored.append((score, i))
    chosen, used = [], 0
    for score, i in sorted(scored, reverse=True):
        t = estimate_tokens(pieces[i])
        if used + t > max_tokens:
            continue
        chosen.append(i)
        used += t
    return "\n\n[…]\n\n".join(pieces[i] for i in sorted(chosen))


NO_ANSWER = "В загруженных файлах ответа на этот вопрос нет."


def make_answer(material: str, question: str, rid: str, title: str, say) -> tuple[dict, list[str]]:
    question = question.strip()
    if not question:
        raise WorkError("Напиши вопрос: ответ строится по загруженным файлам.")
    p = ai.get_provider()
    out_tokens, budget = limits(p)
    partial = estimate_tokens(material) > budget
    ctx = relevant_parts(material, question, budget) if partial else material
    say("ищу ответ в материале…")
    data = ai.ask_json("answer", prompts.answer(ctx, question, partial), expect=("found", "answer"), result_id=rid,
                       result_title=title, max_tokens=min(out_tokens, 4000), mock=lambda: mockai.answer(ctx, question))
    found = data.get("found")
    if isinstance(found, str):
        found = found.strip().lower() in ("true", "да", "yes", "1")
    text = str(_first(data, "answer", "ответ", "text", default="") or "").strip()
    idx = QuoteIndex(material)
    quotes = [_s(q) for q in (data.get("quotes") or data.get("цитаты") or []) if isinstance(q, str)]
    quotes = [q for q in dict.fromkeys(quotes) if idx.contains(q)]
    if found is None:
        found = bool(text) and not re.search(r"\bнет\b.*\b(ответ|информаци|сведени)", text.lower())
    if not found:
        extra = text if text and NO_ANSWER.lower()[:20] not in text.lower() else ""
        text = NO_ANSWER + (f"\n\n{extra}" if extra else "")
        quotes = []
    warnings = ["Материал большой: ответ искал по самым подходящим к вопросу частям."] if partial else []
    content = {"question": question, "found": bool(found), "answer": text, "quotes": quotes,
               "topic": _s(_first(data, "topic", "тема", default="")), "source": _s(data.get("source") or ""),
               "partial": partial}
    return content, warnings


# --- Вопросы-карточки -----------------------------------------------------------------

LETTERS = "абвгдеabcdef"


def _correct_list(raw, options: list[str]) -> list[int]:
    """Правильный ответ в любом виде: 2, "2", "в", "Б", [0, 2], текст варианта, true/false для tf."""
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for it in items:
        if isinstance(it, bool):
            out.append(0 if it else 1)
        elif isinstance(it, (int, float)):
            out.append(int(it))
        elif isinstance(it, str):
            s = it.strip().lower().strip(".)")
            if re.fullmatch(r"\d+", s):
                out.append(int(s))
            elif len(s) == 1 and s in LETTERS:
                out.append(LETTERS.index(s) % 6)
            elif s in ("верно", "true", "да", "правда"):
                out.append(0)
            elif s in ("неверно", "false", "нет", "ложь"):
                out.append(1)
            else:
                for i, o in enumerate(options):
                    if norm_for_match(o) == norm_for_match(s):
                        out.append(i)
    return sorted({i for i in out if 0 <= i < len(options)})


def parse_question(q, topics: Topics, fallback: str) -> dict | None:
    if not isinstance(q, dict):
        return None
    qtype = str(_first(q, "type", "вид", "kind", default="single")).strip().lower()
    qtype = {"one": "single", "один": "single", "multiple": "multi", "несколько": "multi",
             "truefalse": "tf", "true_false": "tf", "bool": "tf", "верно/неверно": "tf"}.get(qtype, qtype)
    text = _s(_first(q, "text", "question", "вопрос", "statement", "утверждение", default=""))
    options = [_s(o.get("text") if isinstance(o, dict) else o) for o in (_first(q, "options", "варианты", "choices", default=[]) or [])]
    if qtype == "tf":
        options = ["Верно", "Неверно"]
    raw = _first(q, "correct", "answer", "правильный", "ответ", default=None)
    if raw is None and any(isinstance(o, dict) and o.get("correct") for o in (q.get("options") or [])):
        raw = [i for i, o in enumerate(q["options"]) if isinstance(o, dict) and o.get("correct")]
    correct = _correct_list(raw, options) if raw is not None else []
    # Нумерация с 1 вместо 0: «4» при четырёх вариантах.
    if raw is not None and not correct and isinstance(raw, (int, str)) and str(raw).isdigit() and int(raw) == len(options):
        correct = [len(options) - 1]
    return {"type": qtype, "topic": topics.pick(_first(q, "topic", "тема", default=""), fallback), "text": text,
            "options": options, "correct": correct,
            "explanation": str(_first(q, "explanation", "объяснение", "why", "пояснение", default="") or "").strip(),
            "hint": _s(_first(q, "hint", "подсказка", default="")), "quote": _s(q.get("quote") or q.get("цитата") or "")}


def problems(q: dict) -> list[str]:
    """Что не так с вопросом (пусто — всё хорошо)."""
    bad = []
    if q["type"] not in ("single", "multi", "tf"):
        bad.append("неизвестный вид")
    if len(q["text"]) < 8:
        bad.append("нет текста вопроса")
    opts = q["options"]
    if len({norm_for_match(o) for o in opts}) != len(opts) or any(not o for o in opts):
        bad.append("повторы или пустые варианты")
    if q["type"] == "single" and (len(opts) != 4 or len(q["correct"]) != 1):
        bad.append("нужно 4 варианта и один верный")
    if q["type"] == "multi" and not (4 <= len(opts) <= 5 and 2 <= len(q["correct"]) < len(opts)):
        bad.append("нужно 4–5 вариантов и 2+ верных, но не все")
    if q["type"] == "tf" and len(q["correct"]) != 1:
        bad.append("не указано, верно или неверно")
    if not q["explanation"]:
        bad.append("нет объяснения")
    return bad


def type_counts(n: int, level: dict) -> dict[str, int]:
    kinds = list(level["types"])
    return dict(zip(kinds, allocate(n, [int(level["types"][k] * 1000) for k in kinds])))


def code_hint(q: dict, quote_ok: bool) -> str:
    """Подсказка, если ИИ её не дал или она выдаёт ответ: убираем один неверный вариант."""
    if q["type"] in ("single", "multi"):
        wrong = [o for i, o in enumerate(q["options"]) if i not in q["correct"]]
        if wrong:
            return f"Точно не «{random.choice(wrong)}»."
    if quote_ok and q["quote"]:
        words = q["quote"].split()
        return "Вспомни место в материале: «" + " ".join(words[:7]) + ("…»" if len(words) > 7 else "»")
    return "Вспомни, что сказано об этом в материале."


def _gives_answer(hint: str, q: dict) -> bool:
    h = norm_for_match(hint)
    return any(len(norm_for_match(q["options"][i])) > 3 and norm_for_match(q["options"][i]) in h
               for i in q["correct"] if q["type"] != "tf")


def finish_question(q: dict, idx: QuoteIndex) -> dict:
    quote_ok = idx.contains(q["quote"])
    if q["type"] in ("single", "multi"):  # ИИ любит ставить верный первым — перемешиваем кодом
        order = list(range(len(q["options"])))
        random.shuffle(order)
        q["options"] = [q["options"][i] for i in order]
        q["correct"] = sorted(order.index(i) for i in q["correct"])
    if not q["hint"] or _gives_answer(q["hint"], q):
        q["hint"] = code_hint(q, quote_ok)
    q["quote"] = q["quote"] if quote_ok else ""
    return q


def make_questions(material: str, note: str, params: dict, rid: str, title: str, say) -> tuple[dict, list[str]]:
    n = int(params.get("count") or config.QUIZ_DEFAULTS["count"])
    level = config.LEVELS.get(params.get("difficulty"), config.LEVELS[config.LEVEL_DEFAULT])
    p = ai.get_provider()
    out_tokens, _ = limits(p)
    # Ответ ИИ с вопросами ≈ 250 токенов на вопрос: если много вопросов — делим и материал, и вопросы.
    per_answer = max(5, out_tokens // 260)
    parts = chunks_for(material, p)
    if n > per_answer * len(parts):
        parts = split_text(material, max(2000, estimate_tokens(material) // -(-n // per_answer) + 1))
    shares = allocate(n, [len(x) for x in parts])
    topics = Topics()
    fallback = default_topic(material, title)
    idx = QuoteIndex(material)
    good: list[dict] = []
    seen: set[str] = set()

    def collect(data, need: int) -> int:
        added = 0
        for t in data.get("topics") or []:
            topics.add(t)
        for raw in data.get("questions") or data.get("вопросы") or []:
            q = parse_question(raw, topics, fallback)
            if q is None or problems(q):
                continue
            key = norm_for_match(q["text"])
            if key in seen or added >= need:
                continue
            seen.add(key)
            good.append(finish_question(q, idx))
            added += 1
        return added

    for i, (ch, k) in enumerate(zip(parts, shares)):
        if not k:
            continue
        say(f"придумываю вопросы… (часть {i + 1} из {len(parts)})" if len(parts) > 1 else "придумываю вопросы…")
        counts = type_counts(k, level)
        data = ai.ask_json("questions", prompts.questions(ch, counts, level, note, (i + 1, len(parts))),
                           expect=("questions",), result_id=rid, result_title=title, max_tokens=out_tokens,
                           mock=lambda ch=ch, c=counts: mockai.questions(ch, c))
        got = collect(data, k)
        if got < k:  # неудачные и недостающие — одним запросом на замену
            say("заменяю неудачные вопросы…")
            counts2 = type_counts(k - got, level)
            try:
                data = ai.ask_json("questions", prompts.questions(ch, counts2, level, note, (i + 1, len(parts)),
                                                                  avoid=[q["text"] for q in good]),
                                   expect=("questions",), result_id=rid, result_title=title, max_tokens=out_tokens,
                                   mock=lambda ch=ch, c=counts2: mockai.questions(ch, c, shift=7))
                collect(data, k - got)
            except AIError:
                pass
    warnings = []
    if not good:
        raise WorkError("ИИ не прислал ни одного годного вопроса. Нажми «Повторить».")
    if len(good) < n:
        warnings.append(f"Получилось {len(good)} вопросов из {n}: остальные ИИ сделал с ошибками, их пришлось убрать.")
    for i, q in enumerate(good):
        q["id"] = f"q{i + 1}"
    used = list(dict.fromkeys(q["topic"] for q in good))
    return {"topics": used, "questions": good, "settings": {"count": n, "difficulty": params.get("difficulty")}}, warnings


# --- Запуск -----------------------------------------------------------------------------

def run(result_id: str) -> dict:
    """Сделать результат. Идёт внутри HTTP-запроса; экран тем временем спрашивает статус."""
    with SessionLocal() as s:
        r = s.get(Result, result_id)
        if r is None:
            raise WorkError("Не найдено")
        r = check_stale(r)
        if r.status == "running":
            raise WorkError("Уже делается. Подожди.")
        if r.status == "ready":
            return {"status": "ready"}
    material = load_text(result_id)
    if not material:
        raise WorkError("Текст материала не сохранился. Создай заново.")
    params = loads(r.params, {})
    set_status(result_id, "running", "готовлюсь…", "")
    try:
        with heartbeat(result_id):
            say = lambda text: set_status(result_id, text=text)  # noqa: E731
            note = r.note or ""
            if r.func == "tickets":
                content, w = make_tickets(material, note, result_id, r.title, say)
            elif r.func == "summary":
                content, w = make_summary(material, note, result_id, r.title, say)
            elif r.func == "answer":
                content, w = make_answer(material, note, result_id, r.title, say)
            elif r.func == "questions":
                content, w = make_questions(material, note, params, result_id, r.title, say)
            else:
                raise WorkError("Неизвестная функция")
            warnings = list(dict.fromkeys(loads(r.warnings, []) + w))
            with SessionLocal() as s:
                row = s.get(Result, result_id)
                row.content = dumps(content)
                row.topics = dumps(content.get("topics") or [])
                row.warnings = dumps(warnings)
                row.status, row.status_text, row.error = "ready", "готово", ""
                row.heartbeat_at = now()
                s.commit()
        return {"status": "ready", "warnings": warnings}
    except (AIError, WorkError, MaterialError) as e:
        set_status(result_id, "error", f"ошибка: {e}", str(e))
        raise
    except Exception as e:
        msg = f"Неожиданная ошибка: {type(e).__name__}: {e}"
        set_status(result_id, "error", msg, msg)
        raise WorkError(msg) from e
