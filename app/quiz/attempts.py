"""Прохождение квиза: попытки, ответы, итог; статистика и расходы. Всё — код, без ИИ
(кроме проверки открытого ответа, которая идёт через pipeline.check_open)."""
from __future__ import annotations

import datetime as dt

import sqlalchemy as sa

from app import config, store
from app.ai.base import AIError
from app.db import Answer, Attempt, Question, Quiz, SessionLocal, Subject, Usage, dumps, loads, now
from app.quiz import pipeline
from app.quiz.questions import FLAGS, TYPE_NAMES, hint_mode, level, public, snapshot
from app.quiz.scoring import describe_answer, describe_response, grade, pct, summarize


class AttemptError(RuntimeError):
    pass


def quiz_questions(quiz_id: str) -> list[Question]:
    with SessionLocal() as s:
        return list(s.scalars(sa.select(Question).where(Question.quiz_id == quiz_id).order_by(Question.position)).all())


def start(quiz_id: str, user_id: str) -> dict:
    with SessionLocal() as s:
        quiz = s.get(Quiz, quiz_id)
        if quiz is None or quiz.status != "ready":
            raise AttemptError("Квиз ещё не готов")
        att = Attempt(user_id=user_id, quiz_id=quiz_id, started_at=now())
        s.add(att)
        s.commit()
        params = loads(quiz.params, {})
    qs = quiz_questions(quiz_id)
    diff = params.get("difficulty", "medium")
    lv = level(diff)
    out = []
    for q in qs:
        pq = public(q)
        pq["seconds"] = lv["timer_open_sec"] if q.qtype == "open" else lv["timer_sec"]
        out.append(pq)
    return {
        "attempt_id": att.id, "quiz_id": quiz_id, "title": quiz.topic,
        "timer": bool(params.get("timer")), "level": level_info(diff),
        "hints": hint_mode(diff, bool(params.get("hints"))) != "none", "max_replaces": config.MAX_REPLACES,
        "questions": out,
    }


def level_info(difficulty: str | None) -> dict:
    """Уровень для экрана: название, множитель баллов, таймер, подсказки."""
    lv = level(difficulty)
    key = difficulty if difficulty in config.LEVELS else config.LEVEL_DEFAULT
    return {"key": key, "name": lv["name"], "multiplier": lv["multiplier"], "timer_sec": lv["timer_sec"],
            "timer_open_sec": lv["timer_open_sec"], "hints": lv["hints"]}


def answer(attempt_id: str, question_id: str, response: dict, time_sec: int | None, user_id: str) -> dict:
    """Записать ответ и сразу проверить. → {score, max, correct, status, note}"""
    with SessionLocal() as s:
        att = s.get(Attempt, attempt_id)
        q = s.get(Question, question_id)
        if att is None or q is None or q.quiz_id != att.quiz_id:
            raise AttemptError("Попытка или вопрос не найдены")
        if att.finished_at:
            raise AttemptError("Эта попытка уже закончена")
        quiz = s.get(Quiz, att.quiz_id)
    status, note = "checked", ""
    response = response or {}
    if response.get("timeout"):
        frac, status = 0.0, "timeout"
    elif q.qtype == "open":
        try:
            frac, note = pipeline.check_open(quiz, q, str(response.get("text", "")))
        except AIError as e:
            frac, status, note = 0.0, "unchecked", str(e)
    else:
        frac = grade(q.qtype, loads(q.options, []), loads(q.answer, {}), response)
    counted = q.verified is not False   # «не проверено» — отвечать можно, но в баллы не идёт
    mx = float(q.points or 1) if counted else 0.0
    score = round(frac * mx, 3)
    with SessionLocal() as s:
        old = s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id, Answer.question_id == question_id)).first()
        if old:
            s.delete(old)
        s.add(Answer(user_id=user_id, attempt_id=attempt_id, quiz_id=q.quiz_id, question_id=q.id,
                     response=dumps(response), score=score, max_score=mx, correct=frac >= 1.0,
                     status=status, note=note, time_sec=time_sec, snapshot=dumps(snapshot(q))))
        s.commit()
    return {"score": score, "max": mx, "correct": frac >= 1.0, "partial": 0 < frac < 1, "counted": counted,
            "status": status, "note": note if status == "unchecked" else ""}


def manual_open(attempt_id: str, question_id: str, accept: str) -> dict:
    """Если ИИ не смог проверить открытый ответ: человек сам решает «засчитать / наполовину / нет»."""
    frac = {"full": 1.0, "half": 0.5, "no": 0.0}.get(accept, 0.0)
    with SessionLocal() as s:
        a = s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id, Answer.question_id == question_id)).first()
        if a is None:
            raise AttemptError("Ответ не найден")
        a.score = round(frac * (a.max_score or 0), 3)
        a.correct = frac >= 1.0
        a.status = "checked"
        a.note = "засчитано вручную" if frac else "не засчитано (решил сам)"
        s.commit()
        return {"score": a.score, "max": a.max_score, "correct": a.correct, "partial": 0 < frac < 1, "status": "checked"}


def recheck_open(attempt_id: str, question_id: str, user_id: str) -> dict:
    with SessionLocal() as s:
        a = s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id, Answer.question_id == question_id)).first()
        if a is None:
            raise AttemptError("Ответ не найден")
        resp = loads(a.response, {})
    return answer(attempt_id, question_id, resp, None, user_id)


def finish(attempt_id: str, duration: int | None) -> dict:
    with SessionLocal() as s:
        att = s.get(Attempt, attempt_id)
        if att is None:
            raise AttemptError("Попытка не найдена")
        qs = list(s.scalars(sa.select(Question).where(Question.quiz_id == att.quiz_id)).all())
        answers = {a.question_id: a for a in s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id)).all()}
        rows = []
        total = mx = 0.0
        for q in qs:
            a = answers.get(q.id)
            sc = a.score if a else 0.0
            m = a.max_score if a else (0.0 if q.verified is False else (q.points or 1))
            m = m or 0.0
            snap = loads(a.snapshot, {}) if a else {}
            rows.append((snap.get("topic", q.topic), snap.get("subtopic", q.subtopic), sc, m))
            total += sc
            mx += m
        topics, subs = summarize(rows)
        att.score, att.max_score, att.pct = round(total, 2), mx, pct(total, mx)
        att.by_topic, att.by_subtopic = dumps(topics), dumps(subs)
        att.finished_at = now()
        att.duration_sec = duration
        quiz = s.get(Quiz, att.quiz_id)
        quiz.last_pct = att.pct
        quiz.best_pct = max(quiz.best_pct or 0, att.pct)
        quiz.attempts_count = (quiz.attempts_count or 0) + 1
        s.commit()
    return result(attempt_id)


def result(attempt_id: str) -> dict:
    with SessionLocal() as s:
        att = s.get(Attempt, attempt_id)
        if att is None:
            raise AttemptError("Попытка не найдена")
        quiz = s.get(Quiz, att.quiz_id)
        qs = list(s.scalars(sa.select(Question).where(Question.quiz_id == att.quiz_id).order_by(Question.position)).all())
        answers = {a.question_id: a for a in s.scalars(sa.select(Answer).where(Answer.attempt_id == attempt_id)).all()}
    cards = []
    for q in qs:
        a = answers.get(q.id)
        snap = loads(a.snapshot, {}) if a else snapshot(q)
        resp = loads(a.response, {}) if a else {}
        counted = snap.get("verified", True) is not False
        outside = (snap.get("flag") or "") == "outside"
        ex = snap.get("explain") or {}
        mx = a.max_score if a else (snap.get("points") or 1 if counted else 0)
        cards.append({
            "question_id": q.id, "text": snap["text"], "qtype": snap["qtype"], "type_name": TYPE_NAMES.get(snap["qtype"], ""),
            "topic": snap.get("topic"), "subtopic": snap.get("subtopic"), "flag_name": FLAGS.get(snap.get("flag") or "", ""),
            "your_answer": describe_response(snap["qtype"], snap["options"], resp) if a else "нет ответа",
            "right_answer": describe_answer(snap["qtype"], snap["options"], snap["answer"]),
            "score": a.score if a else 0, "max": mx or 0,
            "correct": bool(a and a.correct), "status": a.status if a else "missing",
            "note": (a.note or "") if a else "",
            "explanation": snap.get("explanation", ""),
            "why": ex.get("why", "") or ("" if ex else snap.get("explanation", "")),
            "trap": ex.get("trap", ""), "remember": ex.get("remember", ""),
            # Цитата показывается только проверенная кодом и только у вопросов по файлу.
            "quote": "" if outside else snap.get("quote", ""),
            "outside": outside, "counted": counted,
        })
    params = loads(quiz.params, {}) if quiz else {}
    chain = []
    if quiz is not None:
        chain = [{"quiz_id": q.id, "round": i + 1, "last_pct": q.last_pct, "current": q.id == quiz.id}
                 for i, q in enumerate(pipeline.family(quiz))]
    return {
        "attempt_id": att.id, "quiz_id": att.quiz_id, "title": quiz.topic if quiz else "",
        "kind": quiz.kind if quiz else "", "subject_id": quiz.subject_id if quiz else None,
        "finished": bool(att.finished_at), "score": att.score, "max": att.max_score, "pct": att.pct,
        "by_topic": loads(att.by_topic, {}), "by_subtopic": loads(att.by_subtopic, {}),
        "duration": att.duration_sec, "date": att.finished_at.isoformat() if att.finished_at else None,
        "cards": cards, "unchecked": sum(1 for c in cards if c["status"] == "unchecked"),
        "uncounted": sum(1 for c in cards if not c["counted"]),
        "review": loads(att.review, None), "mistakes": sum(1 for c in cards if not c["correct"] and c["counted"]),
        "level": level_info(params.get("difficulty")),
        "settings": {k: params.get(k) for k in ("count", "difficulty", "timer", "hints")},
        "retry_mistakes": len(pipeline.mistake_ids(att.id)) if quiz and quiz.kind == "normal" else 0,
        "retry_share": config.RETRY_MISTAKES_SHARE, "chain": chain if len(chain) > 1 else [],
    }


# --- Списки и статистика ------------------------------------------------------------

def _subjects_map(s) -> dict[str, Subject]:
    return {x.id: x for x in s.scalars(sa.select(Subject)).all()}


def quiz_list(user_id: str) -> list[dict]:
    with SessionLocal() as s:
        subs = _subjects_map(s)
        quizzes = s.scalars(sa.select(Quiz).where(Quiz.user_id == user_id).order_by(Quiz.created_at.desc())).all()
        out = []
        for q in quizzes:
            q = pipeline.check_stale(q)
            out.append({"id": q.id, "date": q.created_at.isoformat() if q.created_at else None,
                        "subject": subs[q.subject_id].name if q.subject_id in subs else "", "subject_id": q.subject_id,
                        "topic": q.topic, "kind": q.kind, "status": q.status, "best_pct": q.best_pct,
                        "last_pct": q.last_pct, "attempts": q.attempts_count or 0,
                        "parent_id": q.parent_id, "root_id": q.root_id or q.id,
                        "difficulty": loads(q.params, {}).get("difficulty")})
        last_att: dict[str, str] = {}
        for a in s.scalars(sa.select(Attempt).where(Attempt.user_id == user_id, Attempt.finished_at.is_not(None))
                           .order_by(Attempt.finished_at)).all():
            last_att[a.quiz_id] = a.id
    # Номер попытки темы: 1 — первый квиз, 2 — «ещё раз» от него и т. д.
    by_root: dict[str, list[dict]] = {}
    for item in sorted(out, key=lambda x: x["date"] or ""):
        by_root.setdefault(item["root_id"], []).append(item)
    for items in by_root.values():
        for i, item in enumerate(items, start=1):
            item["round"] = i
    for item in out:
        item["last_attempt_id"] = last_att.get(item["id"])
    return out


def quiz_detail(quiz_id: str) -> dict:
    with SessionLocal() as s:
        q = s.get(Quiz, quiz_id)
        if q is None:
            raise AttemptError("Квиз не найден")
        q = pipeline.check_stale(q)
        subj = s.get(Subject, q.subject_id)
        atts = s.scalars(sa.select(Attempt).where(Attempt.quiz_id == quiz_id).order_by(Attempt.started_at.desc())).all()
        n_q = s.scalar(sa.select(sa.func.count()).select_from(Question).where(Question.quiz_id == quiz_id)) or 0
    return {"id": q.id, "topic": q.topic, "subject": subj.name if subj else "", "subject_id": q.subject_id,
            "kind": q.kind, "status": q.status, "status_text": q.status_text, "error": q.error,
            "params": {k: v for k, v in loads(q.params, {}).items() if k not in ("old_ids", "mistake_ids")},
            "source_range": q.source_range, "range_note": q.range_note, "sources": loads(q.sources, []),
            "topics": loads(q.topics, []), "warnings": loads(q.warnings, []), "questions": n_q, "chars": q.chars,
            "date": q.created_at.isoformat() if q.created_at else None, "best_pct": q.best_pct, "last_pct": q.last_pct,
            "attempts": [{"id": a.id, "date": (a.finished_at or a.started_at).isoformat(), "pct": a.pct,
                          "score": a.score, "max": a.max_score, "finished": bool(a.finished_at),
                          "duration": a.duration_sec} for a in atts if a.finished_at],
            "level": level_info(loads(q.params, {}).get("difficulty")),
            "chain": [{"quiz_id": x.id, "round": i + 1, "last_pct": x.last_pct, "current": x.id == q.id,
                       "status": x.status} for i, x in enumerate(pipeline.family(q))]}


def _aggregate(user_id: str, quiz_filter=None) -> dict:
    """% по темам и подтемам — по ПОСЛЕДНЕЙ попытке каждого квиза (так видно, что знаешь сейчас)."""
    with SessionLocal() as s:
        subs = _subjects_map(s)
        quizzes = {q.id: q for q in s.scalars(sa.select(Quiz).where(Quiz.user_id == user_id)).all()}
        atts = [a for a in s.scalars(sa.select(Attempt).where(Attempt.user_id == user_id, Attempt.finished_at.is_not(None))
                                     .order_by(Attempt.finished_at)).all()]
    if quiz_filter:
        atts = [a for a in atts if a.quiz_id in quizzes and quiz_filter(quizzes[a.quiz_id])]
    last: dict[str, Attempt] = {}
    for a in atts:
        last[a.quiz_id] = a
    topics: dict[str, dict] = {}
    for a in last.values():
        q = quizzes.get(a.quiz_id)
        subj = subs.get(q.subject_id).name if q and q.subject_id in subs else ""
        for name, t in loads(a.by_topic, {}).items():
            key = f"{subj} · {name}" if subj else name
            d = topics.setdefault(key, {"subject": subj, "topic": name, "score": 0.0, "max": 0.0, "subs": {}})
            d["score"] += t.get("score", 0)
            d["max"] += t.get("max", 0)
        for _, st in loads(a.by_subtopic, {}).items():
            key = f"{subj} · {st['topic']}" if subj else st["topic"]
            d = topics.setdefault(key, {"subject": subj, "topic": st["topic"], "score": 0.0, "max": 0.0, "subs": {}})
            sd = d["subs"].setdefault(st["subtopic"], {"score": 0.0, "max": 0.0})
            sd["score"] += st.get("score", 0)
            sd["max"] += st.get("max", 0)
    out = []
    for key, d in topics.items():
        subs_list = [{"name": n, "pct": pct(v["score"], v["max"])} for n, v in d["subs"].items()]
        out.append({"key": key, "subject": d["subject"], "topic": d["topic"], "pct": pct(d["score"], d["max"]),
                    "subtopics": sorted(subs_list, key=lambda x: x["pct"]) if len(subs_list) >= 2 else []})
    out.sort(key=lambda x: x["pct"])
    history = []
    for a in reversed(atts):
        q = quizzes.get(a.quiz_id)
        history.append({"attempt_id": a.id, "quiz_id": a.quiz_id, "date": a.finished_at.isoformat(),
                        "title": q.topic if q else "удалённый квиз",
                        "subject": subs[q.subject_id].name if q and q.subject_id in subs else "", "pct": a.pct})
    growth = [{"date": a.finished_at.isoformat(), "pct": a.pct,
               "title": quizzes[a.quiz_id].topic if a.quiz_id in quizzes else ""} for a in atts]
    return {"topics": out, "history": history, "growth": growth}


def stats(user_id: str) -> dict:
    return _aggregate(user_id)


def subjects(user_id: str) -> list[dict]:
    with SessionLocal() as s:
        subs = s.scalars(sa.select(Subject).where(Subject.user_id == user_id).order_by(Subject.name)).all()
        quizzes = s.scalars(sa.select(Quiz).where(Quiz.user_id == user_id).order_by(Quiz.created_at.desc())).all()
    out = []
    for sb in subs:
        qs = [q for q in quizzes if q.subject_id == sb.id]
        out.append({"id": sb.id, "name": sb.name,
                    "quizzes": [{"id": q.id, "topic": q.topic, "kind": q.kind, "status": q.status,
                                 "last_pct": q.last_pct, "best_pct": q.best_pct} for q in qs]})
    return out


def subject_detail(subject_id: str) -> dict:
    with SessionLocal() as s:
        sb = s.get(Subject, subject_id)
        if sb is None:
            raise AttemptError("Предмет не найден")
        qs = s.scalars(sa.select(Quiz).where(Quiz.subject_id == subject_id).order_by(Quiz.created_at.desc())).all()
        normal_ids = [q.id for q in qs if q.kind == "normal" and q.status == "ready"]
        pool = s.scalar(sa.select(sa.func.count()).select_from(Question).where(Question.quiz_id.in_(normal_ids))) if normal_ids else 0
    agg = _aggregate(sb.user_id, lambda q: q.subject_id == subject_id)
    lasts = [q.last_pct for q in qs if q.last_pct is not None]
    chars = sum(q.chars or 0 for q in qs if q.kind == "normal")
    return {"id": sb.id, "name": sb.name,
            "quizzes": [{"id": q.id, "topic": q.topic, "kind": q.kind, "status": q.status, "last_pct": q.last_pct,
                         "best_pct": q.best_pct, "attempts": q.attempts_count or 0} for q in qs],
            "normal_count": len(normal_ids), "pool": pool or 0, "chars": chars,
            "avg_last_pct": round(sum(lasts) / len(lasts), 1) if lasts else None,
            "attempts": len(agg["history"]), "topics": agg["topics"], "history": agg["history"][:10]}


def usage_summary() -> dict:
    today = now()
    start = dt.datetime(today.year, today.month, 1)
    with SessionLocal() as s:
        rows = s.scalars(sa.select(Usage).where(Usage.created_at >= start)).all()
        quizzes = {q.id: q.topic for q in s.scalars(sa.select(Quiz)).all()}
    by_purpose: dict[str, dict] = {}
    by_quiz: dict[str, dict] = {}
    by_user: dict[str, dict] = {}
    from app.db import User
    with SessionLocal() as s:
        people = {x.id: x for x in s.scalars(sa.select(User)).all()}
    total = 0.0
    for u in rows:
        who = people.get(u.user_id)
        pu = by_user.setdefault(u.user_id or "?", {"name": (who.name or who.login) if who else "удалённый тестер",
                                                    "rub": 0.0, "calls": 0,
                                                    "limit": who.ai_limit_rub if who and who.role == "guest" else None})
        pu["rub"] += u.cost_rub or 0
        pu["calls"] += 1
        total += u.cost_rub or 0
        p = by_purpose.setdefault(u.purpose or "?", {"rub": 0.0, "calls": 0, "tokens": 0})
        p["rub"] += u.cost_rub or 0
        p["calls"] += 1
        p["tokens"] += (u.tokens_in or 0) + (u.tokens_out or 0)
        if u.quiz_id:
            title = quizzes.get(u.quiz_id) or (u.quiz_title or "") + " (удалён)"
            q = by_quiz.setdefault(u.quiz_id, {"title": title, "rub": 0.0, "calls": 0})
            q["rub"] += u.cost_rub or 0
            q["calls"] += 1
    names = {"quiz": "создание квиза", "replace": "замена вопросов", "check_open": "проверка открытых ответов",
             "subject_quiz": "квиз по предмету", "ping": "проверка связи",
             "review": "разбор ошибок"}
    return {"month": start.strftime("%Y-%m"), "total": round(total, 2), "budget": store.budget(),
            "by_purpose": [{"purpose": k, "name": names.get(k, k), "rub": round(v["rub"], 2), "calls": v["calls"],
                            "tokens": v["tokens"]} for k, v in sorted(by_purpose.items(), key=lambda x: -x[1]["rub"])],
            "by_quiz": sorted([{"quiz_id": k, **v, "rub": round(v["rub"], 2)} for k, v in by_quiz.items()],
                              key=lambda x: -x["rub"])[:30],
            "by_user": sorted([{"user_id": k, **v, "rub": round(v["rub"], 2)} for k, v in by_user.items()],
                              key=lambda x: -x["rub"])}
