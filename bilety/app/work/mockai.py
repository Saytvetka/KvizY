"""«Пробный ИИ» без интернета и денег: собирает ответы из предложений материала.

Нужен для автотестов и чтобы посмотреть приложение без ключа (провайдер «Пробный» в Настройках).
Качество — никакое, зато формат ответа ровно такой же, как просим у настоящего ИИ.
"""
from __future__ import annotations

import re

from app.work.material import norm_for_match


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """[(тема, [предложения])] — тема = имя файла (без расширения) или заголовок."""
    out: list[tuple[str, list[str]]] = []
    cur_topic, cur = "Материал", []
    for line in text.split("\n"):
        m = re.match(r"^\[Файл \d+: (.*?)\]$", line)
        if m:
            if cur:
                out.append((cur_topic, cur))
            cur_topic, cur = re.sub(r"\.\w{2,5}$", "", m.group(1)), []
            continue
        if line.startswith(("[Страница", "[Слайд")) or not line.strip():
            continue
        if line.startswith("## "):
            continue
        for s in re.split(r"(?<=[.!?…])\s+", line):
            s = s.strip()
            if len(s.split()) >= 4:
                cur.append(s)
    if cur:
        out.append((cur_topic, cur))
    return out or [("Материал", ["В материале мало текста для пробного ответа."])]


def _short(s: str, n: int = 12) -> str:
    words = s.rstrip(".!?…").split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def tickets(text: str, only: list[str] | None = None) -> dict:
    secs = [(t, ss) for t, ss in _sections(text) if not only or t in only]
    tickets_, topics = [], []
    for topic, sents in secs:
        topics.append(topic)
        qs = [{"text": f"Объясните: {_short(s)}", "topic": topic} for s in sents[:9]]
        for i in range(0, len(qs), 3):
            tickets_.append({"questions": qs[i:i + 3]})
    return {"topics": topics, "tickets": tickets_}


def summary(text: str) -> dict:
    secs = _sections(text)
    return {"topics": [t for t, _ in secs],
            "sections": [{"topic": t, "title": f"Главное: {t}", "points": ss[:6],
                          "terms": [{"term": ss[0].split()[0], "definition": _short(ss[0], 20)}]} for t, ss in secs]}


def answer(text: str, question: str) -> dict:
    qw = {w[:5] for w in norm_for_match(question).split() if len(w) > 3}
    best, score = "", 0
    for _, sents in _sections(text):
        for s in sents:
            sc = len(qw & {w[:5] for w in norm_for_match(s).split()})
            if sc > score:
                best, score = s, sc
    if score < 1:
        return {"found": False, "answer": "", "quotes": [], "topic": ""}
    return {"found": True, "answer": f"По материалу: **{best}**", "quotes": [best], "topic": _sections(text)[0][0]}


def questions(text: str, counts: dict[str, int], shift: int = 0) -> dict:
    secs = _sections(text)
    flat = [(t, s) for t, ss in secs for s in ss]
    out = []
    i = shift
    for qtype, k in counts.items():
        for _ in range(k):
            topic, s = flat[i % len(flat)]
            others = [x for _, x in flat if x != s] or ["Другое утверждение", "Третье", "Четвёртое"]
            opts = [_short(s, 8)] + [_short(others[(i + j) % len(others)], 8) + ("" if j else "") for j in range(1, 6)]
            opts = list(dict.fromkeys(opts))
            while len(opts) < 5:
                opts.append(f"Вариант {len(opts) + 1} (пробный)")
            q = {"type": qtype, "topic": topic, "explanation": f"Так сказано в материале: {s}",
                 "hint": "Вспомни начало раздела.", "quote": s}
            if qtype == "tf":
                q.update(text=f"Верно ли: «{_short(s, 14)}»?", options=["Верно", "Неверно"], correct=0)
            elif qtype == "multi":
                q.update(text=f"Что из этого есть в материале? ({_short(s, 4)})", options=opts[:5], correct=[0, 1])
            else:
                q.update(text=f"Какое утверждение из материала? ({_short(s, 4)})", options=opts[:4], correct=0)
            out.append(q)
            i += 1
    return {"topics": [t for t, _ in secs], "questions": out}
