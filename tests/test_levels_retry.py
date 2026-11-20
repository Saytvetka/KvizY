"""Уровни сложности, пояснения после ответа и «Пройти тему ещё раз» — всё на пробном ИИ (mock), без денег."""
from __future__ import annotations

import pytest

from test_flow import play

TEXT = "\n".join([
    "## Электролиз",
    "Электролиз — это процесс разложения вещества электрическим током в растворе или расплаве.",
    "При электролизе на катоде идёт восстановление, а на аноде идёт окисление ионов.",
    "Катод подключают к отрицательному полюсу источника тока, а анод к положительному полюсу.",
    "Электролиз расплава хлорида натрия даёт металлический натрий и газообразный хлор.",
    "Электролиз воды с добавкой щёлочи даёт водород на катоде и кислород на аноде.",
    "Масса вещества на электроде пропорциональна прошедшему через раствор заряду.",
    "Гальваника покрывает изделия тонким слоем металла с помощью электролиза раствора соли.",
    "## Кислоты и основания",
    "Кислоты в водном растворе отдают ионы водорода и меняют цвет лакмуса на красный.",
    "Основания в растворе дают гидроксид-ионы и окрашивают фенолфталеин в малиновый цвет.",
    "Реакция кислоты с основанием называется нейтрализацией и даёт соль и воду.",
    "Сильные кислоты почти полностью распадаются на ионы даже в разбавленном растворе.",
    "Слабые кислоты распадаются на ионы лишь частично, поэтому проводят ток хуже сильных.",
    "Водородный показатель меньше семи означает кислую среду, а больше семи щелочную среду.",
    "Индикаторы меняют окраску в зависимости от кислотности раствора и помогают найти среду.",
    "## Скорость реакций",
    "Скорость реакции растёт при нагревании, потому что частицы движутся быстрее и чаще сталкиваются.",
    "Катализатор ускоряет реакцию, но сам после реакции остаётся неизменным по составу.",
    "Измельчение твёрдого вещества увеличивает поверхность и ускоряет реакцию с раствором.",
    "Повышение концентрации реагентов делает столкновения частиц чаще и ускоряет реакцию.",
    "Ингибитор замедляет реакцию и применяется для защиты металлов от коррозии в трубах.",
])


def build(client, difficulty: str, count: int = 10, hints: bool = True, timer: bool = True, topic: str = "") -> str:
    r = client.post("/api/quizzes", json={"subject": "Уровни", "topic": topic or f"Уровень {difficulty}", "count": count,
                                          "difficulty": difficulty, "timer": timer, "hints": hints, "text": TEXT})
    assert r.status_code == 200, r.text
    qid = r.json()["quiz_id"]
    r = client.post(f"/api/quizzes/{qid}/generate")
    assert r.status_code == 200, r.text
    return qid


def questions_of(quiz_id: str):
    from app.db import Question, SessionLocal
    with SessionLocal() as s:
        return s.query(Question).filter(Question.quiz_id == quiz_id).order_by(Question.position).all()


@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
def test_levels_differ(client, difficulty):
    """Квиз каждого уровня: видов вопросов, таймера, подсказок и баллов ровно столько, сколько задано в конфиге."""
    from app import config
    from app.quiz.questions import POINTS, type_plan
    lv = config.LEVELS[difficulty]
    qid = build(client, difficulty)
    qs = questions_of(qid)
    assert len(qs) == 10
    # 1) смесь видов — строго по плану уровня (и доля открытых ответов тоже)
    plan = type_plan(10, difficulty)
    got = {}
    for q in qs:
        got[q.qtype] = got.get(q.qtype, 0) + 1
    assert got == plan, (difficulty, got, plan)
    assert got.get("open", 0) == round(10 * lv["types"].get("open", 0))
    # 2) таймер — из конфига уровня
    att = client.post(f"/api/quizzes/{qid}/attempts").json()
    assert att["timer"] is True and att["level"]["multiplier"] == lv["multiplier"]
    for q in att["questions"]:
        assert q["seconds"] == (lv["timer_open_sec"] if q["qtype"] == "open" else lv["timer_sec"])
    # 3) подсказки: лёгкий — у всех, средний — только «вне файла», сложный — нет
    if lv["hints"] == "all":
        assert all(q.hint for q in qs)
    elif lv["hints"] == "outside":
        assert all(bool(q.hint) == (q.flag == "outside") for q in qs)
        assert any(q.flag == "outside" for q in qs)
    else:
        assert not any(q.hint for q in qs)
    assert sum(1 for q in qs if q.flag == "outside") == round(10 * lv["outside_share"])
    # 4) баллы: вид × множитель уровня
    for q in qs:
        assert q.points == POINTS[q.qtype] * lv["multiplier"]
    res = play(client, qid, right=True)
    assert res["pct"] == 100.0 and res["level"]["multiplier"] == lv["multiplier"]
    assert res["max"] == pytest.approx(sum(POINTS[q.qtype] for q in qs) * lv["multiplier"])


def test_levels_really_differ():
    """Числа уровней в конфиге растут/падают в нужную сторону — разница заметна."""
    from app import config
    e, m, h = (config.LEVELS[k] for k in ("easy", "medium", "hard"))
    assert e["timer_sec"] > m["timer_sec"] > h["timer_sec"]
    assert e["multiplier"] < m["multiplier"] < h["multiplier"]
    assert e["types"].get("open", 0) < m["types"].get("open", 0) < h["types"].get("open", 0)
    assert [e["hints"], m["hints"], h["hints"]] == ["all", "outside", "none"]
    assert "multi" not in e["types"] and h["types"]["multi"] >= 0.25


def test_hints_off_means_none(client):
    qid = build(client, "easy", count=5, hints=False)
    assert not any(q.hint for q in questions_of(qid))


def test_explanations_and_quotes(client):
    """Пояснение по частям; цитата показывается, только если правда есть в тексте; у «вне файла» цитаты нет."""
    from app.quiz.material import QuoteIndex
    qid = build(client, "medium")
    idx = QuoteIndex(TEXT)
    for q in questions_of(qid):
        assert q.explanation
        if q.flag == "outside":
            assert not q.quote
        elif q.quote:
            assert idx.contains(q.quote)
    res = play(client, qid, right=False)
    full = client.get(f"/api/attempts/{res['attempt_id']}").json()
    wrong = [c for c in full["cards"] if not c["correct"]]
    assert wrong and all(c["right_answer"] for c in wrong)
    assert all(c["why"] and c["remember"] for c in wrong if c["qtype"] in ("single", "multi", "tf", "fill"))
    assert any(c["outside"] and not c["quote"] for c in full["cards"])
    # открытый ответ оценён по образцу и написано, чего не хватает
    opens = [c for c in wrong if c["qtype"] == "open"]
    assert opens and all("Не хватает" in c["note"] for c in opens)


def test_fake_quote_is_hidden():
    from app.quiz.material import QuoteIndex
    from app.quiz.questions import finalize, normalize, problems
    idx = QuoteIndex(TEXT)
    q = normalize({"type": "tf", "text": "Катализатор расходуется в реакции.", "correct": False,
                   "quote": "Катализатор полностью расходуется в ходе реакции"})
    assert "quote" in problems(q, idx)  # выдуманной цитаты нет в тексте — код ловит
    q = normalize({"type": "tf", "flag": "outside", "text": "Вода кипит при 100 градусах на уровне моря.", "correct": True,
                   "quote": "что-то похожее"})
    assert finalize(q, "none")["quote"] == ""  # вопрос шире файла — цитату не показываем


def test_contradiction_check():
    from app.quiz.questions import CONTRADICTION, contradiction, normalize, problems
    base = {"type": "single", "text": "Что идёт на катоде?", "options": ["восстановление ионов", "окисление ионов",
                                                                      "растворение анода", "выделение тепла"]}
    ok = normalize({**base, "correct": 0, "explain": {"answer_text": "восстановление ионов", "why": "На катоде ионы получают электроны."}})
    assert contradiction(ok) == "" and CONTRADICTION not in problems(ok, None)
    bad = normalize({**base, "correct": 1, "explain": {"answer_text": "восстановление ионов"}})
    assert contradiction(bad) and CONTRADICTION in problems(bad, None)
    bad2 = normalize({**base, "correct": 2, "explanation": "Верно: восстановление ионов, потому что катод отдаёт электроны."})
    assert contradiction(bad2)
    tf = normalize({"type": "tf", "text": "Катод — плюс.", "correct": True, "explain": {"answer_text": "неверно"}})
    assert contradiction(tf)
    fill = normalize({"type": "fill", "text": "На катоде идёт ___.", "accepted": ["восстановление"],
                      "explain": {"answer_text": "окисление"}})
    assert contradiction(fill)


def _spoil(q: dict) -> dict:
    """Сломать согласованность: в пояснении назван другой вариант."""
    if q.get("type") == "single":
        q["explain"]["answer_text"] = q["options"][(q["correct"] + 1) % 4]
    elif q.get("type") == "tf":
        q["explain"]["answer_text"] = "неверно" if q["correct"] else "верно"
    return q


def test_contradiction_regenerated_once(client, monkeypatch):
    """ИИ прислал противоречие → вопрос перегенерирован один раз; второй раз нормальный → всё засчитывается."""
    from app.quiz import mockai
    real = mockai.quiz

    def quiz_with_lies(*a, **k):
        data = real(*a, **k)
        for q in data["questions"]:
            _spoil(q)
        return data

    monkeypatch.setattr(mockai, "quiz", quiz_with_lies)
    qid = build(client, "easy", count=6, topic="Противоречие раз")
    qs = questions_of(qid)
    assert len(qs) == 6 and all(q.verified is not False for q in qs)
    from app.db import SessionLocal, Usage
    with SessionLocal() as s:
        assert s.query(Usage).filter(Usage.quiz_id == qid, Usage.purpose == "replace").count() == 1


def test_contradiction_twice_marked_unverified(client, monkeypatch):
    """Противоречие и после повтора → «не проверено», в баллы не идёт."""
    from app.quiz import mockai
    real_q, real_r = mockai.quiz, mockai.replace

    def quiz_with_lies(*a, **k):
        data = real_q(*a, **k)
        for q in data["questions"]:
            _spoil(q)
        return data

    def replace_with_lies(*a, **k):
        data = real_r(*a, **k)
        for q in data["questions"]:
            _spoil(q)
        return data

    monkeypatch.setattr(mockai, "quiz", quiz_with_lies)
    monkeypatch.setattr(mockai, "replace", replace_with_lies)
    qid = build(client, "easy", count=6, topic="Противоречие два")
    qs = questions_of(qid)
    unv = [q for q in qs if q.verified is False]
    assert unv, [q.qtype for q in qs]
    assert any("не проверено" in w for w in client.get(f"/api/quizzes/{qid}").json()["warnings"])
    att = client.post(f"/api/quizzes/{qid}/attempts").json()
    assert any(q["verified"] is False for q in att["questions"])
    res = play(client, qid, right=True)
    counted = sum(q.points for q in qs if q.verified is not False)
    assert res["max"] == pytest.approx(counted)  # «не проверено» не входит в максимум
    assert res["pct"] == 100.0
    full = client.get(f"/api/attempts/{res['attempt_id']}").json()
    assert full["uncounted"] == len(unv)


def test_retry_topic(client):
    """«Пройти тему ещё раз»: тот же текст и диапазон, новые вопросы, половина — из ошибок, цепочка попыток."""
    from app import config
    from app.db import Quiz, SessionLocal
    from app.quiz.pipeline import load_text
    from app.quiz.questions import is_duplicate, key_of
    first = build(client, "medium", count=8, topic="Тема для повтора")
    old_texts = [key_of(q) for q in questions_of(first)]
    res = play(client, first, right=False)
    full = client.get(f"/api/attempts/{res['attempt_id']}").json()
    assert full["retry_mistakes"] == 8 and full["kind"] == "normal"

    r = client.post(f"/api/attempts/{res['attempt_id']}/retry", json={})
    assert r.status_code == 200, r.text
    new = r.json()["quiz_id"]
    assert r.json()["mistakes"] == round(8 * config.RETRY_MISTAKES_SHARE)
    assert client.post(f"/api/quizzes/{new}/generate").status_code == 200
    with SessionLocal() as s:
        nq, oq = s.get(Quiz, new), s.get(Quiz, first)
        assert nq.parent_id == first and nq.root_id == first and nq.status == "ready"
        assert oq.status == "ready" and oq.attempts_count == 1  # старый квиз не тронут
    assert load_text(new) == load_text(first)  # тот же текст
    qs = questions_of(new)
    assert len(qs) == 8
    copies = [q for q in qs if q.source_question_id]
    fresh = [q for q in qs if not q.source_question_id]
    assert len(copies) == 4 and len(fresh) == 4
    for q in fresh:  # новые не повторяют старые (код отсёк похожие)
        assert not is_duplicate(key_of(q), old_texts, config.RETRY_SIMILARITY), q.text
    # цепочка в истории
    lst = {q["id"]: q for q in client.get("/api/quizzes").json()}
    assert lst[new]["round"] == 2 and lst[new]["parent_id"] == first and lst[first]["round"] == 1
    assert lst[first]["last_attempt_id"] == res["attempt_id"]
    d = client.get(f"/api/quizzes/{new}").json()
    assert [c["quiz_id"] for c in d["chain"]] == [first, new]

    # 100 % → тоже работает, все вопросы новые; настройки можно поменять
    res2 = play(client, new, right=True)
    assert res2["pct"] == 100.0
    assert client.get(f"/api/attempts/{res2['attempt_id']}").json()["retry_mistakes"] == 0
    r = client.post(f"/api/attempts/{res2['attempt_id']}/retry", json={"count": 5, "difficulty": "hard", "hints": False})
    assert r.status_code == 200 and r.json()["mistakes"] == 0
    third = r.json()["quiz_id"]
    assert client.post(f"/api/quizzes/{third}/generate").status_code == 200
    with SessionLocal() as s:
        tq = s.get(Quiz, third)
        assert tq.parent_id == new and tq.root_id == first
    qs3 = questions_of(third)
    assert len(qs3) == 5 and not any(q.source_question_id for q in qs3)
    assert {q.difficulty for q in qs3} == {"hard"}
    assert len(client.get(f"/api/quizzes/{third}").json()["chain"]) == 3


def test_retry_errors_are_clear(client, monkeypatch):
    """Сбой сборки — понятная ошибка по-русски, старый квиз не тронут."""
    from app.ai.base import AIError
    from app.db import Quiz, SessionLocal
    from app.quiz import mockai
    first = build(client, "easy", count=4, topic="Повтор со сбоем")
    res = play(client, first, right=True)
    r = client.post(f"/api/attempts/{res['attempt_id']}/retry", json={"count": 999})
    assert r.status_code == 400 and "Число вопросов" in r.json()["detail"]
    new = client.post(f"/api/attempts/{res['attempt_id']}/retry", json={}).json()["quiz_id"]

    def down(*a, **k):
        raise AIError("сервис временно не отвечает", "network")

    monkeypatch.setattr(mockai, "quiz", down)
    r = client.post(f"/api/quizzes/{new}/generate")
    assert r.status_code == 503 and "ИИ недоступен" in r.json()["detail"]
    st = client.get(f"/api/quizzes/{new}/status").json()
    assert st["status"] == "error" and st["error"]
    with SessionLocal() as s:
        assert s.get(Quiz, first).status == "ready"
    assert len(questions_of(first)) == 4
    # квиз удалён — повтор не из чего, но ответ понятный
    assert client.delete(f"/api/quizzes/{first}").status_code == 200
    r = client.post(f"/api/attempts/{res['attempt_id']}/retry", json={})
    assert r.status_code == 404  # попытка удалена вместе с квизом


def test_health_and_api_503_when_db_down(app_client, monkeypatch):
    import sqlalchemy as sa

    from app import db
    real = db.SessionLocal

    class Broken:
        def __call__(self, *a, **k):
            raise sa.exc.OperationalError("SELECT 1", {}, Exception("база не отвечает"))

    monkeypatch.setattr(db, "SessionLocal", Broken())
    import app.main as main
    monkeypatch.setattr(main.db, "SessionLocal", db.SessionLocal)
    r = app_client.get("/api/health")
    assert r.status_code == 503 and "база не отвечает" in r.json()["db"]
    monkeypatch.setattr(db, "SessionLocal", real)
    assert app_client.get("/api/health").status_code == 200


def test_api_503_when_db_breaks_midway(client, monkeypatch):
    import sqlalchemy as sa

    from app.quiz import attempts

    def boom(*a, **k):
        raise sa.exc.OperationalError("SELECT", {}, Exception("connection reset"))

    monkeypatch.setattr(attempts, "quiz_list", boom)
    r = client.get("/api/quizzes")
    assert r.status_code == 503 and "База данных недоступна" in r.json()["detail"]


def test_static_cache(app_client):
    r = app_client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    r2 = app_client.get("/static/app.js", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304 and not r2.content
    assert "max-age" in app_client.get("/static/icons/icon-192.png").headers["cache-control"]
    assert app_client.get("/static/../app/config.py").status_code == 404
