"""Как делается квиз (всё, где участвует ИИ).

generate(quiz_id):
    1. берём сохранённый текст материала;
    2. если не влезает в один запрос — делим на куски, вопросы делим пропорционально;
    3. запрос `quiz` (по куску — один запрос), ИИ сам делит на темы и подтемы;
    4. код проверяет каждый вопрос (цитата есть в тексте, число правильных, дубли, общее число);
    5. все плохие вопросы и недостающие — одним запросом `replace`;
    6. сохраняем.
Для квиза по предмету: часть — старые вопросы предмета (сначала те, где ошибался), часть — новые от ИИ.
"""
from __future__ import annotations

import math
import random
import re
import threading
from contextlib import contextmanager

import sqlalchemy as sa

from app import ai, config
from app.ai.base import AIError
from app.db import (OWNER_ID, Answer, Attempt, Question, Quiz, QuizText, SessionLocal, Subject, dumps, loads,
                    new_id, now)
from app.quiz import mockai, prompts
from app.quiz.material import (MaterialError, QuoteIndex, allocate, estimate_tokens, nice_subject, norm_for_match,
                               norm_subject, split_text)
from app.quiz.questions import (CONTRADICTION, TYPES, finalize, hint_mode, is_duplicate, key_of, level, normalize,
                                problems, public, snapshot, type_plan)

TEXT_PART = 200_000          # символов в одной строке quiz_texts (YDB любит небольшие ячейки)
TOKENS_PER_QUESTION = 450    # сколько токенов ответа закладываем на один вопрос
PROMPT_OVERHEAD = 2200       # системные правила + инструкции + схема


class QuizError(RuntimeError):
    """Понятная ошибка для человека."""


# --- Предметы и тексты --------------------------------------------------------------

def get_or_create_subject(name: str, user_id: str = OWNER_ID) -> Subject:
    norm = norm_subject(name)
    if not norm:
        raise QuizError("Впиши предмет")
    with SessionLocal() as s:
        subj = s.scalars(sa.select(Subject).where(Subject.user_id == user_id, Subject.norm == norm)).first()
        if subj:
            return subj
        subj = Subject(user_id=user_id, name=nice_subject(name), norm=norm)
        s.add(subj)
        s.commit()
        return subj


def save_text(quiz_id: str, text: str, user_id: str = OWNER_ID) -> None:
    with SessionLocal() as s:
        s.execute(sa.delete(QuizText).where(QuizText.quiz_id == quiz_id))
        for i in range(0, max(1, len(text)), TEXT_PART):
            s.add(QuizText(user_id=user_id, quiz_id=quiz_id, part=i // TEXT_PART, text=text[i:i + TEXT_PART]))
        s.commit()


def load_text(quiz_id: str) -> str:
    with SessionLocal() as s:
        rows = s.scalars(sa.select(QuizText).where(QuizText.quiz_id == quiz_id).order_by(QuizText.part)).all()
    return "".join(r.text for r in rows)


def subject_material(subject_id: str) -> str:
    """Все сохранённые тексты обычных квизов предмета (чем больше квизов — тем больше материала)."""
    with SessionLocal() as s:
        quizzes = s.scalars(sa.select(Quiz).where(Quiz.subject_id == subject_id, Quiz.kind == "normal")
                            .order_by(Quiz.created_at)).all()
    parts = []
    for q in quizzes:
        t = load_text(q.id)
        if t:
            parts.append(f"## Квиз «{q.topic}»\n{t}")
    return "\n\n".join(parts)


def quiz_material(quiz: Quiz) -> str:
    return subject_material(quiz.subject_id) if quiz.kind == "subject" else load_text(quiz.id)


# --- Статус задачи ------------------------------------------------------------------

def set_status(quiz_id: str, status: str | None = None, text: str | None = None, error: str | None = None) -> None:
    with SessionLocal() as s:
        q = s.get(Quiz, quiz_id)
        if q is None:
            return
        if status:
            q.status = status
        if text is not None:
            q.status_text = text
        if error is not None:
            q.error = error
        q.heartbeat_at = now()
        s.commit()


@contextmanager
def heartbeat(quiz_id: str):
    """Пока идёт работа, раз в 20 секунд отмечаемся в базе. Если сервер упадёт, отметки прекратятся,
    и через JOB_STALE_SECONDS экран покажет ошибку с кнопкой «Повторить»."""
    stop = threading.Event()

    def beat():
        while not stop.wait(20):
            try:
                set_status(quiz_id)
            except Exception:
                pass

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


def check_stale(quiz: Quiz) -> Quiz:
    """Задача «зависла» (сервер перезапустился посреди работы)? Тогда помечаем ошибкой."""
    if quiz.status == "running" and quiz.heartbeat_at is not None:
        age = (now() - quiz.heartbeat_at).total_seconds()
        if age > config.JOB_STALE_SECONDS:
            msg = "Сервер перезапускался во время создания квиза. Нажми «Повторить»."
            set_status(quiz.id, "error", msg, msg)
            quiz.status, quiz.status_text, quiz.error = "error", msg, msg
    return quiz


# --- Создание вопросов ----------------------------------------------------------------

def _limits(p) -> tuple[int, int]:
    """(сколько вопросов влезает в один ответ, сколько токенов материала в один запрос)"""
    per_request = max(3, (p.max_output - 600) // TOKENS_PER_QUESTION)
    return per_request, max(2000, p.context_tokens - PROMPT_OVERHEAD - p.max_output)


def plan_requests(material: str, n: int, p=None) -> list[tuple[str, int]]:
    """Разбиваем работу на запросы: [(кусок текста, сколько вопросов)]."""
    p = p or ai.get_provider()
    per_request, text_budget = _limits(p)
    text_budget = min(text_budget, 60000)  # даже у больших моделей не шлём больше — дорого
    chunks = split_text(material, text_budget)
    need = max(len(chunks), math.ceil(n / per_request))
    if len(chunks) < need and estimate_tokens(material) > 1500:
        chunks = split_text(material, max(1000, estimate_tokens(material) // need + 1))
    counts = allocate(n, [len(c) for c in chunks])
    # если кусков меньше, чем нужно запросов (маленький текст, много вопросов) — дробим число вопросов
    jobs: list[tuple[str, int]] = []
    for c, k in zip(chunks, counts):
        while k > 0:
            take = min(k, per_request)
            jobs.append((c, take))
            k -= take
    return jobs


def is_big(material: str, n: int) -> bool:
    try:
        return len(plan_requests(material, n)) > 1
    except AIError:
        return False


def _excerpt(material: str, near: list[str], budget_tokens: int) -> str:
    """Кусок материала для замены вопроса: весь, если влезает, иначе кусок с нужной цитатой."""
    if estimate_tokens(material) <= budget_tokens:
        return material
    chunks = split_text(material, budget_tokens)
    for quote in near:
        nq = norm_for_match(quote)[:80]
        if not nq:
            continue
        for c in chunks:
            if nq in norm_for_match(c):
                return c
    return chunks[0]


def _replace_round(quiz: Quiz, slots: list[dict], material: str, p, hints: bool, subject: str,
                   status_cb=None) -> dict[str, dict]:
    """Один запрос `replace` на все слоты (если материал большой — по запросу на кусок)."""
    if not slots:
        return {}
    _, budget = _limits(p)
    budget = min(budget, 60000)
    groups: dict[str, list[dict]] = {}
    for s in slots:
        ex = _excerpt(material, [s.get("near_quote", "")], budget)
        groups.setdefault(ex, []).append(s)
    out: dict[str, dict] = {}
    for ex, group in groups.items():
        if status_cb:
            status_cb()
        data = ai.ask_json(
            "replace", prompts.replace_prompt(slots=group, material=ex, hints=hints, subject=subject, title=quiz.topic),
            expect=("questions",), quiz_id=quiz.id, quiz_title=quiz.topic, user_id=quiz.user_id,
            max_tokens=min(p.max_output, 600 + TOKENS_PER_QUESTION * len(group)),
            mock=lambda ex=ex, group=group: mockai.replace(ex, group))
        items = data.get("questions") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = [items]
        for i, raw in enumerate(items or []):
            if not isinstance(raw, dict):
                continue
            slot = str(raw.get("slot") or (group[i]["slot"] if i < len(group) else ""))
            sd = next((g for g in group if g["slot"] == slot), None)
            if sd is None:
                continue
            any_type = sd["type"] == "any"
            q = normalize(raw, {"topic": sd.get("topic", ""), "subtopic": sd.get("subtopic", ""),
                                "qtype": "" if any_type else sd["type"], "difficulty": sd.get("difficulty", "medium")})
            if any_type:
                if q["qtype"] == sd.get("not_type"):
                    q["_wrong_type"] = True
            elif q["qtype"] != sd["type"] and sd["type"] in TYPES:
                q["_wrong_type"] = True
            out[slot] = q
    return out


def plan_minus(plan: dict[str, int], taken: list[str]) -> dict[str, int]:
    """План уровня минус виды, которые уже заняты (вопросы-ошибки из прошлой попытки)."""
    plan = dict(plan)
    for t in taken:
        if plan.get(t, 0) <= 0:
            t = max(plan, key=lambda x: plan[x])
        plan[t] -= 1
    return {t: c for t, c in plan.items() if c > 0}


def split_plan(plan: dict[str, int], counts: list[int]) -> list[dict[str, int]]:
    """Общий план видов → план на каждый запрос (виды равномерно перемешаны между запросами)."""
    order = sorted(((i + 0.5) / c, t) for t, c in plan.items() for i in range(c))
    types = [t for _, t in order]
    out, pos = [], 0
    for k in counts:
        part: dict[str, int] = {}
        for t in types[pos:pos + k]:
            part[t] = part.get(t, 0) + 1
        out.append(part)
        pos += k
    return out


def make_questions(quiz: Quiz, material: str, n: int, difficulty: str, hints: bool, subject: str,
                   instruction: str = "", avoid: list[str] | None = None, status_cb=None,
                   known_topics: list[str] | None = None, similarity: float = 0.8,
                   retry: bool = False, taken: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Сделать n проверенных вопросов по материалу. → (темы, вопросы, предупреждения).
    Виды вопросов — строго по плану уровня (лишние виды заменяются). Вопрос, у которого ответ
    не сходится с пояснением, перегенерируется один раз; не сошлось снова — «не проверено»."""
    p = ai.get_provider()
    jobs = plan_requests(material, n, p)
    plan_all = plan_minus(type_plan(n + len(taken or []), difficulty), taken or [])
    plans = split_plan(plan_all, [c for _, c in jobs])
    mode = hint_mode(difficulty, hints)
    quotes = QuoteIndex(material)
    topics: list[dict] = []
    topic_names: dict[str, str] = {}
    good: list[dict] = []
    bad: list[dict] = []
    warnings: list[str] = []
    avoid = list(avoid or [])
    say = status_cb or (lambda *_: None)

    def dup(q: dict) -> bool:
        k = key_of(q)
        return is_duplicate(k, avoid, similarity) or is_duplicate(k, [key_of(g) for g in good])

    def add_topics(items):
        for t in items or []:
            if isinstance(t, str):
                t = {"name": t, "subtopics": []}
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or t.get("topic") or t.get("тема") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key not in topic_names:
                topic_names[key] = name
                topics.append({"name": name, "subtopics": []})
            entry = next(x for x in topics if x["name"].lower() == key)
            subs = t.get("subtopics") or t.get("подтемы") or []
            for sname in subs if isinstance(subs, list) else []:
                sname = str(sname.get("name") if isinstance(sname, dict) else sname).strip()
                if sname and sname.lower() not in [x.lower() for x in entry["subtopics"]]:
                    entry["subtopics"].append(sname)

    for k, ((chunk, cnt), plan) in enumerate(zip(jobs, plans), start=1):
        say("делаю вопросы…" if len(jobs) == 1 else f"делаю вопросы… (часть {k} из {len(jobs)})")
        prev = avoid + [key_of(q) for q in good]
        data = ai.ask_json(
            "subject_quiz" if quiz.kind == "subject" else "quiz",
            prompts.quiz_prompt(title=quiz.topic, subject=subject, n=cnt, difficulty=difficulty, plan=plan,
                                hints=mode, material=chunk, instruction=instruction,
                                part=(k, len(jobs)) if len(jobs) > 1 else None,
                                known_topics=[t["name"] for t in topics] or known_topics,
                                avoid=prev if (prev and (len(jobs) > 1 or avoid)) else None,
                                seed=hash((quiz.id, k)) & 0xFFFF, retry=retry),
            expect=("questions",), quiz_id=quiz.id, quiz_title=quiz.topic, user_id=quiz.user_id,
            max_tokens=min(p.max_output, 600 + TOKENS_PER_QUESTION * cnt),
            mock=lambda chunk=chunk, cnt=cnt, plan=plan, k=k, prev=prev: mockai.quiz(
                chunk, cnt, plan, offset=k * 31, avoid=prev, outside=round(cnt * level(difficulty)["outside_share"])))
        if isinstance(data, list):
            data = {"questions": data}
        add_topics(data.get("topics") or data.get("темы"))
        raw_qs = data.get("questions") or data.get("вопросы") or []
        for raw in raw_qs[:cnt + 3]:
            q = normalize(raw, {"difficulty": difficulty})
            q["difficulty"] = difficulty
            probs = problems(q, quotes)
            if q["qtype"] in TYPES and q["qtype"] not in plan_all:
                probs.append("вид не для этого уровня")
            is_dup = dup(q)
            if not probs and not is_dup:
                good.append(q)
            else:
                q["_problems"] = probs + (["дубль"] if is_dup else [])
                bad.append(q)

    say("проверяю…")
    # Виды — строго по плану уровня: лишние вопросы вида откладываем (возьмём, если замена не выйдет).
    have: dict[str, int] = {}
    kept, extra = [], []
    for q in good:
        if have.get(q["qtype"], 0) < plan_all.get(q["qtype"], 0):
            have[q["qtype"]] = have.get(q["qtype"], 0) + 1
            kept.append(q)
        else:
            extra.append(q)
    good = kept
    missing_types = [t for t, c in plan_all.items() for _ in range(c - have.get(t, 0))]
    # Слоты на замену: плохие вопросы + недостающие до плана. Вид слота — тот, которого не хватает.
    slots: list[dict] = []
    names = [t["name"] for t in topics] or ["Основное"]
    for t in missing_types:
        src = next((b for b in bad if b["qtype"] == t and not b.get("_slot")), None) or \
            next((b for b in bad if not b.get("_slot")), None)
        if src is not None:
            src["_slot"] = True
        top = (src or {}).get("topic") or names[len(slots) % len(names)]
        slots.append({"slot": f"s{len(slots) + 1}", "type": t, "topic": top,
                      "subtopic": (src or {}).get("subtopic", ""), "difficulty": difficulty,
                      "flag": (src or {}).get("flag", ""),
                      "near_quote": (src or {}).get("quote", ""),
                      "avoid": ([src.get("text", "")] if src else []) + [g["text"] for g in good][:10] + avoid[:15]})
    quote_only: list[dict] = []
    unverified: list[dict] = []
    if slots:
        say("заменяю неудачные вопросы…")
        repl = _replace_round(quiz, slots, material, p, mode, subject, status_cb=None)
        for s_ in slots:
            q = repl.get(s_["slot"])
            if not q:
                continue
            q["difficulty"] = difficulty
            probs = problems(q, quotes)
            if q.get("_wrong_type"):
                probs.append("вид не тот")
            if dup(q):
                continue
            if not probs:
                good.append(q)
            elif probs == ["quote"]:
                quote_only.append(q)
            elif probs == [CONTRADICTION]:
                unverified.append(q)  # второй раз не сошлось — «не проверено»
    # Не хватает — запасные варианты, по очереди: лишние виды → без цитаты → «не проверено».
    for q in extra:
        if len(good) >= n:
            break
        if not dup(q):
            good.append(q)
    for q in quote_only + [b for b in bad if b.get("_problems") == ["quote"]]:
        if len(good) >= n:
            break
        if not dup(q):
            q["quote"] = ""
            q["_no_quote"] = True
            good.append(q)
    for q in unverified + [b for b in bad if b.get("_problems") == [CONTRADICTION]]:
        if len(good) >= n:
            break
        if not dup(q):
            q["verified"] = False
            good.append(q)
    good = good[:n]
    no_quote = sum(1 for q in good if q.get("_no_quote"))
    if no_quote:
        warnings.append(f"У {no_quote} вопр. ИИ не дал точную цитату из материала — цитату не показываю.")
    n_unv = sum(1 for q in good if q.get("verified") is False)
    if n_unv:
        warnings.append(f"{n_unv} вопр. помечены «не проверено»: ответ ИИ дважды не сошёлся с пояснением. "
                        "Их можно пройти, но в баллы они не идут.")
    if len(good) < n:
        warnings.append(f"Получилось {len(good)} вопросов из {n}: ИИ не смог сделать остальные без ошибок.")
    if not good:
        raise QuizError("ИИ не смог сделать ни одного правильного вопроса. Нажми «Повторить» или поменяй материал.")
    # Темы вопросов, которых нет в списке тем, — добавляем.
    for q in good:
        if not q.get("topic"):
            q["topic"] = names[0]
        add_topics([{"name": q["topic"], "subtopics": [q["subtopic"]] if q.get("subtopic") else []}])
        q["topic"] = topic_names.get(q["topic"].lower(), q["topic"])
        finalize(q, mode, difficulty)
    return topics, good, warnings


def _save_questions(quiz_id: str, user_id: str, qs: list[dict], start: int = 0) -> None:
    with SessionLocal() as s:
        for i, q in enumerate(qs):
            s.add(Question(user_id=user_id, quiz_id=quiz_id, position=start + i, topic=q.get("topic"),
                           subtopic=q.get("subtopic") or "", qtype=q["qtype"], difficulty=q.get("difficulty"),
                           flag=q.get("flag") or "", text=q["text"], options=dumps(q.get("options")),
                           answer=dumps(q.get("answer")), explanation=q.get("explanation") or "",
                           explain=dumps(q.get("explain") or {}), verified=q.get("verified", True) is not False,
                           quote=q.get("quote") or "", points=q.get("points", 1), hint=q.get("hint") or "",
                           replace_count=0, replace_history=dumps([]),
                           source_question_id=q.get("source_question_id")))
        s.commit()


def generate(quiz_id: str) -> dict:
    """Сделать вопросы квиза. Идёт внутри HTTP-запроса; экран тем временем спрашивает статус."""
    with SessionLocal() as s:
        quiz = s.get(Quiz, quiz_id)
        if quiz is None:
            raise QuizError("Квиз не найден")
        quiz = check_stale(quiz)
        if quiz.status == "running":
            raise QuizError("Квиз уже делается. Подожди.")
        if quiz.status == "ready":
            return {"status": "ready"}
        subj = s.get(Subject, quiz.subject_id)
        s.execute(sa.delete(Question).where(Question.quiz_id == quiz_id))  # если это «Повторить»
        s.commit()
    params = loads(quiz.params, {})
    n = int(params.get("count", 10))
    difficulty = params.get("difficulty", "medium")
    hints = bool(params.get("hints"))
    set_status(quiz_id, "running", "готовлюсь…", "")
    try:
        with heartbeat(quiz_id):
            say = lambda text: set_status(quiz_id, text=text)  # noqa: E731
            warnings = loads(quiz.warnings, [])
            if quiz.kind == "subject":
                topics, qs, w = _subject_questions(quiz, subj, params, n, difficulty, hints, say)
            elif quiz.parent_id:
                topics, qs, w = _retry_questions(quiz, subj, params, n, difficulty, hints, say)
            else:
                material = load_text(quiz_id)
                if not material:
                    raise QuizError("Текст материала не сохранился. Создай квиз заново.")
                topics, qs, w = make_questions(quiz, material, n, difficulty, hints, subj.name if subj else "",
                                               instruction=params.get("range_rest", ""), status_cb=say)
            warnings += w
            _save_questions(quiz_id, quiz.user_id, qs)
            with SessionLocal() as s:
                qz = s.get(Quiz, quiz_id)
                qz.topics = dumps(topics)
                qz.warnings = dumps(list(dict.fromkeys(warnings)))
                qz.status, qz.status_text, qz.error = "ready", "готово", ""
                qz.heartbeat_at = now()
                s.commit()
        return {"status": "ready", "warnings": warnings}
    except (AIError, QuizError, MaterialError) as e:
        set_status(quiz_id, "error", f"ошибка: {e}", str(e))
        raise
    except Exception as e:
        msg = f"Неожиданная ошибка: {type(e).__name__}: {e}"
        set_status(quiz_id, "error", msg, msg)
        raise QuizError(msg) from e


# --- Квиз по предмету -----------------------------------------------------------------

def _pool(subject_id: str) -> list[Question]:
    with SessionLocal() as s:
        quiz_ids = [q.id for q in s.scalars(sa.select(Quiz).where(
            Quiz.subject_id == subject_id, Quiz.kind == "normal", Quiz.status == "ready")).all()]
        if not quiz_ids:
            return []
        return list(s.scalars(sa.select(Question).where(Question.quiz_id.in_(quiz_ids))).all())


def _last_results(question_ids: list[str]) -> dict[str, bool]:
    """Последний результат по каждому вопросу (учитываем и ответы на его копии в квизах по предмету)."""
    if not question_ids:
        return {}
    with SessionLocal() as s:
        copies = s.execute(sa.select(Question.id, Question.source_question_id)
                           .where(Question.source_question_id.in_(question_ids))).all()
        src = {cid: sid for cid, sid in copies}
        ids = list(question_ids) + list(src)
        rows = s.execute(sa.select(Answer.question_id, Answer.correct, Answer.created_at)
                         .where(Answer.question_id.in_(ids))).all()
    last: dict[str, tuple] = {}
    for qid, correct, at in rows:
        key = src.get(qid, qid)
        if key not in last or (at and last[key][1] and at > last[key][1]):
            last[key] = (bool(correct), at)
    return {k: v[0] for k, v in last.items()}


def pick_old(subject_id: str, count: int, difficulty: str) -> list[str]:
    """Старые вопросы для квиза по предмету: сначала где ошибался, потом новые для тебя, потом остальные."""
    pool = _pool(subject_id)
    res = _last_results([q.id for q in pool])
    rnd = random.Random()

    def prio(q: Question):
        r = res.get(q.id)
        return (0 if r is False else 1 if r is None else 2, 0 if q.difficulty == difficulty else 1, rnd.random())

    return [q.id for q in sorted(pool, key=prio)[:count]]


def create_subject_quiz(subject_id: str, params: dict, user_id: str = OWNER_ID) -> str:
    with SessionLocal() as s:
        subj = s.get(Subject, subject_id)
        if subj is None:
            raise QuizError("Предмет не найден")
        has = s.scalar(sa.select(sa.func.count()).select_from(Quiz).where(
            Quiz.subject_id == subject_id, Quiz.kind == "normal", Quiz.status == "ready")) or 0
    if not has:
        raise QuizError("В предмете пока нет готовых квизов — сначала создай обычный квиз.")
    n = int(params.get("count", 10))
    pool_size = len(_pool(subject_id))
    n_old = min(pool_size, round(n * config.SUBJECT_OLD_SHARE))
    old_ids = pick_old(subject_id, n_old, params.get("difficulty", "medium"))
    params = dict(params, old_ids=old_ids)
    qid = new_id()
    with SessionLocal() as s:
        s.add(Quiz(id=qid, user_id=user_id, subject_id=subject_id, kind="subject",
                   topic=f"{subj.name}: квиз по предмету", params=dumps(params), status="new",
                   status_text="жду запуска", sources=dumps([]), warnings=dumps([])))
        s.commit()
    return qid


def _copy_question(o: Question, difficulty: str, hints: bool) -> dict:
    """Старый вопрос в новый квиз: та же суть, баллы и подсказки — по уровню нового квиза."""
    q = {"topic": o.topic, "subtopic": o.subtopic, "qtype": o.qtype, "difficulty": difficulty,
         "flag": o.flag or "", "text": o.text, "options": loads(o.options), "answer": loads(o.answer),
         "explanation": o.explanation, "explain": loads(o.explain, {}) or {}, "quote": o.quote,
         "hint": o.hint or "", "source_question_id": o.source_question_id or o.id,
         "verified": o.verified is not False}
    return finalize(q, hint_mode(difficulty, hints), difficulty)


def _topics_of(qs) -> list[dict]:
    topics: list[dict] = []
    for o in qs:
        t = next((x for x in topics if x["name"] == o.topic), None)
        if t is None:
            t = {"name": o.topic, "subtopics": []}
            topics.append(t)
        if o.subtopic and o.subtopic not in t["subtopics"]:
            t["subtopics"].append(o.subtopic)
    return topics


def _subject_questions(quiz: Quiz, subj: Subject, params: dict, n: int, difficulty: str, hints: bool, say):
    say("беру старые вопросы…")
    old_ids = params.get("old_ids") or []
    with SessionLocal() as s:
        olds = list(s.scalars(sa.select(Question).where(Question.id.in_(old_ids))).all()) if old_ids else []
    olds.sort(key=lambda q: old_ids.index(q.id))
    qs = [_copy_question(o, difficulty, hints) for o in olds]
    topics = _topics_of(olds)
    warnings: list[str] = []
    n_new = n - len(qs)
    if n_new > 0:
        material = subject_material(subj.id)
        if not material:
            raise QuizError("У предмета нет сохранённых текстов.")
        p = ai.get_provider()
        _, budget = _limits(p)
        budget = min(budget, 60000)
        chunks = split_text(material, budget)
        if len(chunks) > 1:
            # Берём столько кусков, сколько нужно на новые вопросы, из разных мест материала.
            per_request, _ = _limits(p)
            k = min(len(chunks), max(1, math.ceil(n_new / per_request)))
            start = random.randrange(len(chunks))
            step = len(chunks) / k
            material = "\n\n".join(chunks[int(start + i * step) % len(chunks)] for i in range(k))
        pool_texts = [key_of(q) for q in _pool(subj.id)]
        new_topics, new_qs, warnings = make_questions(
            quiz, material, n_new, difficulty, hints, subj.name, avoid=pool_texts, status_cb=say,
            known_topics=[t["name"] for t in topics])
        for t in new_topics:
            ex = next((x for x in topics if x["name"].lower() == t["name"].lower()), None)
            if ex is None:
                topics.append(t)
            else:
                ex["subtopics"] += [s for s in t["subtopics"] if s not in ex["subtopics"]]
        qs += new_qs
    random.shuffle(qs)
    return topics, qs, warnings


# --- «Пройти тему ещё раз» -------------------------------------------------------------

def family(quiz: Quiz) -> list[Quiz]:
    """Все квизы цепочки попыток темы (первый и все «ещё раз» от него), по времени."""
    root = quiz.root_id or quiz.id
    with SessionLocal() as s:
        rows = s.scalars(sa.select(Quiz).where(sa.or_(Quiz.id == root, Quiz.root_id == root))).all()
    return sorted(rows, key=lambda q: q.created_at or now())


def mistake_ids(attempt_id: str) -> list[str]:
    """Вопросы попытки, где была ошибка (частично верно и «время вышло» — тоже ошибка).
    Вопросы «не проверено» и заменённые после ответа не берём."""
    with SessionLocal() as s:
        answers = s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id)
                            .order_by(Answer.created_at)).all()
        qs = {q.id: q for q in s.scalars(sa.select(Question).where(
            Question.id.in_([a.question_id for a in answers] or ["-"]))).all()}
    out = []
    for a in answers:
        q = qs.get(a.question_id)
        snap = loads(a.snapshot, {})
        if a.correct or q is None or q.verified is False or snap.get("verified") is False:
            continue
        if snap.get("text") and snap["text"] != q.text:
            continue
        out.append(q.id)
    return out


def create_retry(attempt_id: str, opts: dict, user_id: str) -> dict:
    """Новый квиз по той же теме: тот же текст и диапазон, новые вопросы; часть — из ошибок попытки.
    Старый квиз не меняется. → {"quiz_id", "mistakes"}"""
    with SessionLocal() as s:
        att = s.get(Attempt, attempt_id)
        if att is None:
            raise QuizError("Попытка не найдена")
        old = s.get(Quiz, att.quiz_id)
    if old is None:
        raise QuizError("Квиз этой попытки удалён — пройти тему ещё раз не из чего.")
    if old.kind != "normal":
        raise QuizError("Это квиз по предмету: новый такой квиз собирается на странице предмета.")
    text = load_text(old.id)
    if not text:
        raise QuizError("Текст материала этого квиза не найден — создай квиз заново.")
    p_old = loads(old.params, {})
    n = int(opts.get("count") or p_old.get("count", 10))
    use = opts.get("use_mistakes", True)
    mist = mistake_ids(attempt_id) if use else []
    mist = mist[:round(n * config.RETRY_MISTAKES_SHARE)]
    params = {"count": n, "difficulty": opts.get("difficulty") or p_old.get("difficulty", "medium"),
              "timer": bool(opts.get("timer", p_old.get("timer"))), "hints": bool(opts.get("hints", p_old.get("hints"))),
              "range_rest": p_old.get("range_rest", ""), "mistake_ids": mist}
    qid = new_id()
    with SessionLocal() as s:
        s.add(Quiz(id=qid, user_id=user_id, subject_id=old.subject_id, topic=old.topic, kind="normal",
                   params=dumps(params), source_range=old.source_range, range_note=old.range_note,
                   sources=old.sources, chars=old.chars, status="new", status_text="жду запуска",
                   warnings=dumps([]), parent_id=old.id, root_id=old.root_id or old.id, from_attempt_id=attempt_id))
        s.commit()
    save_text(qid, text, user_id)
    return {"quiz_id": qid, "mistakes": len(mist)}


def _retry_questions(quiz: Quiz, subj: Subject | None, params: dict, n: int, difficulty: str, hints: bool, say):
    say("беру вопросы, где были ошибки…")
    ids = params.get("mistake_ids") or []
    with SessionLocal() as s:
        olds = list(s.scalars(sa.select(Question).where(Question.id.in_(ids))).all()) if ids else []
        fam_ids = [q.id for q in family(quiz) if q.id != quiz.id]
        seen = list(s.scalars(sa.select(Question).where(Question.quiz_id.in_(fam_ids or ["-"]))).all())
    olds.sort(key=lambda q: ids.index(q.id))
    qs = [_copy_question(o, difficulty, hints) for o in olds][:n]
    topics = _topics_of(olds)
    # Что человек уже видел в этой теме: все вопросы цепочки и их прошлые варианты (до замены).
    avoid = [key_of(q) for q in seen] + [key_of(h) for q in seen for h in loads(q.replace_history, [])]
    warnings: list[str] = []
    n_new = n - len(qs)
    if n_new > 0:
        material = load_text(quiz.id)
        if not material:
            raise QuizError("Текст материала не сохранился. Создай квиз заново.")
        new_topics, new_qs, warnings = make_questions(
            quiz, material, n_new, difficulty, hints, subj.name if subj else "",
            instruction=params.get("range_rest", ""), avoid=[a for a in avoid if a], status_cb=say,
            known_topics=[t["name"] for t in topics], similarity=config.RETRY_SIMILARITY, retry=True,
            taken=[q["qtype"] for q in qs])
        for t in new_topics:
            ex = next((x for x in topics if x["name"].lower() == t["name"].lower()), None)
            if ex is None:
                topics.append(t)
            else:
                ex["subtopics"] += [x for x in t["subtopics"] if x not in ex["subtopics"]]
        qs += new_qs
    random.Random(quiz.id).shuffle(qs)
    return topics, qs, warnings


# --- Замена одного вопроса ----------------------------------------------------------

def replace_question(question_id: str) -> dict:
    """Кнопка «заменить вопрос». Новый вопрос — той же темы и сложности, но ДРУГОГО вида.
    Если ИИ прислал неудачный вопрос — одна повторная попытка, потом честное сообщение.
    → {"changed": bool, "message": str, "question": {...}}"""
    with SessionLocal() as s:
        q = s.get(Question, question_id)
        if q is None:
            raise QuizError("Вопрос не найден")
        quiz = s.get(Quiz, q.quiz_id)
        subj = s.get(Subject, quiz.subject_id) if quiz else None
        others = [o.text for o in s.scalars(sa.select(Question).where(Question.quiz_id == q.quiz_id,
                                                                      Question.id != q.id)).all()]
    if (q.replace_count or 0) >= config.MAX_REPLACES:
        return {"changed": False, "message": f"Этот вопрос уже заменён {config.MAX_REPLACES} раза — больше нельзя.",
                "question": public(q)}
    history = loads(q.replace_history, [])
    material = quiz_material(quiz)
    if not material:
        raise QuizError("Текст материала не найден — заменить вопрос не из чего.")
    params = loads(quiz.params, {})
    difficulty = params.get("difficulty") or q.difficulty or "medium"
    mode = hint_mode(difficulty, bool(params.get("hints")))
    p = ai.get_provider()
    quotes = QuoteIndex(material)
    avoid = [h.get("text", "") for h in history] + [q.text]
    reason = "ИИ не прислал новый вопрос"
    got = None
    for _attempt in range(2):
        slot = {"slot": "s1", "type": "any", "not_type": q.qtype, "topic": q.topic, "subtopic": q.subtopic,
                "difficulty": q.difficulty, "flag": q.flag or "", "near_quote": q.quote or "",
                "avoid": avoid + others[:20]}
        cand = _replace_round(quiz, [slot], material, p, mode, subj.name if subj else "").get("s1")
        if not cand:
            reason = "ИИ не прислал новый вопрос"
            continue
        if is_duplicate(cand.get("text", ""), avoid):
            reason = "ИИ прислал такой же вопрос, как раньше"
            continue
        if is_duplicate(cand.get("text", ""), others):
            reason = "ИИ прислал вопрос, который уже есть в квизе"
            continue
        probs = problems(cand, quotes)
        if cand.get("_wrong_type"):
            probs.append("вид вопроса не поменялся")
        if probs:
            reason = "новый вопрос не прошёл проверку (" + ", ".join(
                "цитаты нет в материале" if x == "quote" else x for x in probs) + ")"
            continue
        if cand.get("qtype") not in level(difficulty)["types"]:
            reason = "ИИ прислал вид вопроса, которого нет на этом уровне"
            continue
        got = cand
        break
    if got is None:
        return {"changed": False, "message": f"{reason[:1].upper()}{reason[1:]}. Замена не засчитана — попробуй ещё раз.",
                "question": public(q)}
    got["difficulty"] = difficulty
    finalize(got, mode, difficulty)
    with SessionLocal() as s:
        q = s.get(Question, question_id)
        history.append(snapshot(q))
        q.qtype = got["qtype"]
        q.text, q.options, q.answer = got["text"], dumps(got["options"]), dumps(got["answer"])
        q.explanation, q.quote, q.hint = got.get("explanation", ""), got.get("quote", ""), got.get("hint", "")
        q.explain, q.verified = dumps(got.get("explain") or {}), True
        q.subtopic = got.get("subtopic") or q.subtopic
        q.flag = got.get("flag") or ""
        q.points = got["points"]
        q.replace_count = (q.replace_count or 0) + 1
        q.replace_history = dumps(history)
        s.commit()
        left = config.MAX_REPLACES - q.replace_count
        return {"changed": True, "message": f"Вопрос заменён. Осталось замен: {left}.", "question": public(q)}


# --- Проверка открытого ответа ------------------------------------------------------

def check_open(quiz: Quiz, question: Question, text: str) -> tuple[float, str]:
    """→ (доля 0 / 0.5 / 1, комментарий). Бросает AIError — тогда экран спросит «засчитать?»."""
    ans = loads(question.answer, {})
    if not text.strip():
        return 0.0, "Ответ пустой."
    data = ai.ask_json(
        "check_open", prompts.check_open_prompt(question=question.text, reference=ans.get("reference", ""),
                                                key_points=ans.get("key_points", []), answer=text[:3000]),
        expect=("score",), quiz_id=quiz.id, quiz_title=quiz.topic, user_id=quiz.user_id, max_tokens=400,
        mock=lambda: mockai.check_open(ans.get("reference", ""), text, ans.get("key_points", [])))
    raw = data.get("score", data.get("баллы", data.get("оценка"))) if isinstance(data, dict) else None
    verdict = str(data.get("verdict", "")).lower() if isinstance(data, dict) else ""
    score = ai.to_number(raw, -1)
    if score < 0:
        score = 1.0 if "full" in verdict or "полност" in verdict else 0.5 if "part" in verdict or "частич" in verdict else -1
    if score < 0:
        raise AIError("ИИ не поставил оценку.", "format")
    if score > 1:  # прислал в баллах из 2 или в процентах
        score = score / 100 if score > 2 else score / 2
    score = 1.0 if score >= 0.75 else 0.5 if score >= 0.25 else 0.0
    comment = str(data.get("comment") or data.get("explanation") or data.get("комментарий") or "").strip()
    miss = data.get("missing", data.get("не_хватает", []))
    if isinstance(miss, str):
        miss = [x for x in re.split(r";|\n", miss) if x.strip()]
    miss = [str(x).strip() for x in miss or [] if str(x).strip()]
    if score < 1 and not miss:
        # ИИ не сказал, чего не хватает — считаем кодом: главные мысли образца, слов которых нет в ответе.
        miss = missing_points(ans, text)
    return score, open_note(comment, miss if score < 1 else [])


def missing_points(ans: dict, text: str) -> list[str]:
    """Главные мысли образца, которых нет в ответе (по совпадению слов; без ИИ)."""
    words = {w for w in norm_for_match(text).split() if len(w) > 3}
    points = ans.get("key_points") or [ans.get("reference", "")]
    out = []
    for kp in points:
        kw = {w for w in norm_for_match(kp).split() if len(w) > 3}
        if kw and len(kw & {w2 for w in words for w2 in kw if w[:5] == w2[:5]}) / len(kw) < 0.5:
            out.append(str(kp).strip())
    return out


def open_note(comment: str, missing: list[str]) -> str:
    parts = [comment.strip()] if comment.strip() else []
    if missing:
        parts.append("Не хватает: " + "; ".join(missing[:4]))
    return "\n".join(parts)


# --- Разбор ошибок -------------------------------------------------------------------

def review_attempt(attempt_id: str, force: bool = False) -> dict:
    """Кнопка «Разобрать ошибки» в итоге: один запрос к ИИ по всем ошибкам, разбор по темам.
    Готовый разбор хранится в попытке — второй раз не запрашиваем (экономия)."""
    from app.db import Answer, Attempt
    from app.quiz.scoring import describe_answer, describe_response

    with SessionLocal() as s:
        att = s.get(Attempt, attempt_id)
        if att is None:
            raise QuizError("Попытка не найдена")
        if att.review and not force:
            return loads(att.review, {})
        quiz = s.get(Quiz, att.quiz_id)
        subj = s.get(Subject, quiz.subject_id) if quiz else None
        answers = s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id)).all()
    mistakes = []
    for a in answers:
        if a.correct:
            continue
        snap = loads(a.snapshot, {})
        if not snap:
            continue
        mistakes.append({"topic": snap.get("topic") or "Без темы", "subtopic": snap.get("subtopic") or "",
                         "text": snap["text"], "quote": snap.get("quote", ""),
                         "your": describe_response(snap["qtype"], snap["options"], loads(a.response, {})),
                         "right": describe_answer(snap["qtype"], snap["options"], snap["answer"])})
    if not mistakes:
        return {"topics": [], "message": "Ошибок нет — разбирать нечего 🎉"}
    material = quiz_material(quiz) if quiz else ""
    p = ai.get_provider()
    _, budget = _limits(p)
    budget = min(budget, 40000) - 1500
    ex = _excerpt(material, [m["quote"] for m in mistakes], budget) if material else ""
    data = ai.ask_json("review", prompts.review_prompt(mistakes=mistakes[:15], material=ex,
                                                       subject=subj.name if subj else "", title=quiz.topic if quiz else ""),
                       expect=("topics",), quiz_id=quiz.id if quiz else None, quiz_title=quiz.topic if quiz else None,
                       user_id=att.user_id, max_tokens=min(p.max_output, 700 + 500 * len({m["topic"] for m in mistakes})),
                       mock=lambda: mockai.review(mistakes))
    items = data.get("topics") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = [items]
    out = []
    for t in items or []:
        if not isinstance(t, dict):
            continue
        kp = t.get("key_points") or t.get("главное") or []
        out.append({"topic": str(t.get("topic") or t.get("тема") or "").strip(),
                    "explain": str(t.get("explain") or t.get("explanation") or t.get("объяснение") or "").strip(),
                    "key_points": [str(x) for x in kp] if isinstance(kp, list) else [str(kp)],
                    "mistake": str(t.get("mistake") or t.get("ошибка") or "").strip(),
                    "example": str(t.get("example") or t.get("пример") or "").strip(),
                    "reread": str(t.get("reread") or t.get("quote") or t.get("цитата") or "").strip()})
    out = [t for t in out if t["explain"]]
    if not out:
        raise QuizError("ИИ не прислал разбор. Нажми «Повторить».")
    review = {"topics": out}
    with SessionLocal() as s:
        att = s.get(Attempt, attempt_id)
        att.review = dumps(review)
        s.commit()
    return review


# --- Удаление -------------------------------------------------------------------------

def delete_quiz(quiz_id: str) -> None:
    """Стираем квиз, его текст, попытки, ответы и вопросы (они же уходят из пула предмета).
    Расходы остаются в статистике (там сохранено название квиза)."""
    with SessionLocal() as s:
        quiz = s.get(Quiz, quiz_id)
        if quiz is None:
            raise QuizError("Квиз не найден")
        subject_id = quiz.subject_id
        s.execute(sa.delete(Answer).where(Answer.quiz_id == quiz_id))
        s.execute(sa.delete(Attempt).where(Attempt.quiz_id == quiz_id))
        s.execute(sa.delete(Question).where(Question.quiz_id == quiz_id))
        s.execute(sa.delete(QuizText).where(QuizText.quiz_id == quiz_id))
        s.delete(quiz)
        s.commit()
        left = s.scalar(sa.select(sa.func.count()).select_from(Quiz).where(Quiz.subject_id == subject_id)) or 0
        if not left:
            subj = s.get(Subject, subject_id)
            if subj:
                s.delete(subj)
                s.commit()
