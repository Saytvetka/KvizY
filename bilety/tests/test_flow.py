"""Весь путь с пробным ИИ: вход → материал (PDF, Word, PowerPoint, фото, текст) → четыре функции →
История → удаление (вместе с текстом) → Настройки и расходы."""
import sqlalchemy as sa

import samples


def upload(client, name: str, data: bytes, part: int = 2 * 1024 * 1024) -> str:
    uid = ""
    for n, off in enumerate(range(0, max(1, len(data)), part)):
        r = client.post("/api/uploads", data={"upload_id": uid, "n": str(n), "filename": name},
                        files={"part": (name, data[off:off + part])})
        assert r.status_code == 200, r.text
        uid = r.json()["upload_id"]
    return uid


def make(client, func: str, **body) -> dict:
    r = client.post("/api/results", json={"func": func, **body})
    assert r.status_code == 200, r.text
    rid = r.json()["result_id"]
    r2 = client.post(f"/api/results/{rid}/run")
    assert r2.status_code == 200, r2.text
    d = client.get(f"/api/results/{rid}").json()
    assert d["status"] == "ready", d
    return d


def test_health_and_login(app_client):
    r = app_client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert app_client.get("/api/bootstrap").status_code == 401
    r = app_client.post("/api/login", json={"password": "wrong"})
    assert r.status_code == 401 and "Неверный пароль" in r.json()["detail"]
    assert app_client.post("/api/login", json={"password": "test-pass"}).status_code == 200
    b = app_client.get("/api/bootstrap").json()
    assert b["palette"] == "classic" and set(b["functions"]) == {"tickets", "summary", "answer", "questions"}


def test_tickets_two_files_have_topics(client):
    u1 = upload(client, "электролиз.pdf", samples.pdf_bytes(samples.LECTURE_1), part=1500)  # несколько кусков
    u2 = upload(client, "коррозия.docx", samples.docx_bytes(samples.LECTURE_2))
    d = make(client, "tickets", uploads=[u1, u2])
    c = d["content"]
    assert len(c["topics"]) == 2
    assert [t["n"] for t in c["tickets"]] == list(range(1, len(c["tickets"]) + 1))
    for t in c["tickets"]:
        assert 1 <= len(t["questions"]) <= 4
        for q in t["questions"]:
            assert q["topic"] in c["topics"]
    assert d["sources"][0]["name"] == "электролиз.pdf" and "электролиз" in d["title"]


def test_summary_single_topic(client):
    d = make(client, "summary", text=samples.LECTURE_1)
    c = d["content"]
    assert len(c["topics"]) == 1 and c["sections"] and c["sections"][0]["points"]


def test_answer_found_and_not_found(client):
    d = make(client, "answer", text=samples.LECTURE_1, note="Что происходит на катоде при электролизе?")
    c = d["content"]
    assert c["found"] is True and c["quotes"], c
    assert d["title"].startswith("Что происходит на катоде")
    d2 = make(client, "answer", text=samples.LECTURE_1, note="Кто написал роман Война и мир?")
    assert d2["content"]["found"] is False
    assert d2["content"]["answer"].startswith("В загруженных файлах ответа на этот вопрос нет")
    # ответ без вопроса — понятная ошибка
    r = client.post("/api/results", json={"func": "answer", "text": samples.LECTURE_1})
    assert r.status_code == 400 and "вопрос" in r.json()["detail"]


def test_questions_follow_settings_and_attempt(client):
    assert client.put("/api/settings/quiz", json={"count": 6, "difficulty": "hard", "timer": True, "hints": False}).status_code == 200
    up = upload(client, "слайды.pptx", samples.pptx_bytes())
    d = make(client, "questions", uploads=[up], text=samples.LECTURE_2)
    qs = d["content"]["questions"]
    assert len(qs) == 6 and d["params"]["difficulty"] == "hard"
    for q in qs:
        assert q["type"] in ("single", "multi", "tf") and q["explanation"] and q["hint"]
        if q["type"] == "single":
            assert len(q["options"]) == 4 and len(q["correct"]) == 1
    # ответы проверяет код
    answers = {qs[0]["id"]: qs[0]["correct"], qs[1]["id"]: [99]}
    r = client.post(f"/api/results/{d['id']}/attempt", json={"answers": answers, "duration_sec": 30})
    a = r.json()
    assert a["ok"] == 1 and a["total"] == 6 and a["answers"][qs[1]["id"]]["ok"] is False
    assert client.get(f"/api/results/{d['id']}").json()["last_attempt"]["ok"] == 1


def test_photo_uses_ocr(client):
    from app.work import ocr
    ocr.use(lambda data, mime: samples.LECTURE_2)
    try:
        up = upload(client, "конспект.jpg", samples.jpg_bytes())
        d = make(client, "summary", uploads=[up])
        assert "Коррозия" in " ".join(s["topic"] for s in d["content"]["sections"]) or d["content"]["sections"]
        # скан PDF тоже распознаётся, и об этом предупреждаем
        up = upload(client, "скан.pdf", samples.scan_pdf_bytes())
        r = client.post("/api/results", json={"func": "summary", "uploads": [up]})
        assert r.status_code == 200, r.text
        assert any("скан" in w for w in r.json()["warnings"])
    finally:
        ocr.use(None)
    # без настроенного распознавания — понятная ошибка
    up = upload(client, "фото.png", samples.jpg_bytes())
    r = client.post("/api/results", json={"func": "summary", "uploads": [up]})
    assert r.status_code == 400 and "Yandex Vision" in r.json()["detail"]


def test_bad_and_too_big_material(client):
    up = upload(client, "старый.doc", b"\xd0\xcf\x11\xe0 old word")
    r = client.post("/api/results", json={"func": "summary", "uploads": [up]})
    assert r.status_code == 400 and ".docx" in r.json()["detail"]
    from app import config
    old = config.MAX_TEXT_CHARS
    config.MAX_TEXT_CHARS = 300
    try:
        r = client.post("/api/results", json={"func": "summary", "text": samples.LECTURE_1 * 3})
        assert r.status_code == 400 and "слишком большой" in r.json()["detail"] and "откуда и докуда" in r.json()["detail"]
    finally:
        config.MAX_TEXT_CHARS = old
    r = client.post("/api/results", json={"func": "summary"})
    assert r.status_code == 400 and "Добавь материал" in r.json()["detail"]


def test_range_and_reuse(client):
    up1 = upload(client, "a.pdf", samples.pdf_bytes(samples.LECTURE_1))
    up2 = upload(client, "b.docx", samples.docx_bytes(samples.LECTURE_2))
    r = client.post("/api/results", json={"func": "summary", "uploads": [up1, up2], "source_range": "только 2-й файл"})
    assert r.status_code == 200 and "файлы 2" in r.json()["range_note"]
    rid = r.json()["result_id"]
    # другой результат по тому же материалу — файлы второй раз не нужны
    d = make(client, "answer", from_result=rid, note="Что такое коррозия?")
    assert d["content"]["found"] is True and d["sources"][1]["name"] == "b.docx"


def test_history_and_delete_removes_text(client):
    from app.db import ResultText, SessionLocal
    d = make(client, "tickets", text=samples.LECTURE_2)
    items = client.get("/api/results").json()["items"]
    assert any(i["id"] == d["id"] and i["func"] == "tickets" for i in items)
    assert client.delete(f"/api/results/{d['id']}").status_code == 200
    assert client.get(f"/api/results/{d['id']}").status_code == 404
    with SessionLocal() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(ResultText).where(ResultText.result_id == d["id"])) == 0


def test_settings_ai_key_never_returned_and_usage(client):
    r = client.put("/api/settings/ai", json={"provider": "yandex", "fields": {"folder_id": "b1gtest", "model": "yandexgpt/latest"},
                                             "secrets": {"api_key": "AQVN-secret-key"}})
    assert r.status_code == 200
    body = r.text
    assert "AQVN-secret-key" not in body and r.json()["providers"]["yandex"]["secrets"]["api_key"]["set"] is True
    from app.db import SessionLocal, Setting
    with SessionLocal() as s:
        stored = s.scalars(sa.select(Setting).where(Setting.key == "key.yandex.api_key")).one().value
    assert "AQVN-secret-key" not in stored
    # обратно на пробный
    client.put("/api/settings/ai", json={"provider": "mock"})
    u = client.get("/api/usage").json()
    assert u["budget"] == 500 and u["month"]["calls"] > 0 and u["month"]["tokens_in"] > 0
    assert client.put("/api/settings/palette", json={"palette": "neon"}).status_code == 200
    assert client.get("/api/bootstrap").json()["palette"] == "neon"
    assert client.put("/api/settings/palette", json={"palette": "rainbow"}).status_code == 400


def test_stale_job_shows_retry(client):
    import datetime as dt

    from app.db import Result, SessionLocal
    r = client.post("/api/results", json={"func": "summary", "text": samples.LECTURE_1}).json()
    with SessionLocal() as s:
        row = s.get(Result, r["result_id"])
        row.status, row.heartbeat_at = "running", dt.datetime(2020, 1, 1)
        s.commit()
    st = client.get(f"/api/results/{r['result_id']}/status").json()
    assert st["status"] == "error" and "Повторить" in st["error"]
    assert client.post(f"/api/results/{r['result_id']}/run").status_code == 200


def test_password_change_logs_out_others(client, app_client):
    from fastapi.testclient import TestClient

    from app.main import app
    other = TestClient(app)
    assert other.post("/api/login", json={"password": "test-pass"}).status_code == 200
    assert client.post("/api/password", json={"old": "test-pass", "new": "new-pass-1"}).status_code == 200
    assert client.get("/api/bootstrap").status_code == 200       # это устройство осталось внутри
    from app import auth
    auth.forget_cache()
    assert other.get("/api/bootstrap").status_code == 401        # другое — вышло
    assert client.post("/api/password", json={"old": "new-pass-1", "new": "test-pass"}).status_code == 200


def test_big_material_goes_in_parts(client):
    """Материал больше одного запроса: ИИ работает по частям, результат склеивается, вопросов ровно сколько заказано."""
    text = "\n".join(f"## Раздел {i}\n" + samples.LECTURE_1.replace("Электролиз", f"Процесс{i}") for i in range(200))
    assert len(text) > 90_000
    d = make(client, "tickets", text=text)
    assert len(d["content"]["tickets"]) > 3 and any("частям" in w for w in d["warnings"])
    client.put("/api/settings/quiz", json={"count": 12})
    d = make(client, "questions", text=text)
    assert len(d["content"]["questions"]) == 12
    d = make(client, "answer", text=text, note="Что применяют для получения алюминия?")
    assert d["content"]["partial"] is True and d["content"]["found"] is True
