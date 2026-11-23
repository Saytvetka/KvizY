"""Разбор ответов ИИ и проверки кодом: темы, вопросы в разном виде, поиск подходящих кусков, JSON."""
import samples
from app.ai import parse_json, unwrap
from app.work import pipeline
from app.work.material import QuoteIndex, apply_range, combine, split_text


def test_topics_merge_same_names():
    t = pipeline.Topics()
    assert t.add("Электролиз") == "Электролиз"
    assert t.add(" электролиз. ") == "Электролиз"
    assert t.add({"name": "Электролиз растворов"}) == "Электролиз"
    assert t.add("Коррозия") == "Коррозия"
    assert t.names == ["Электролиз", "Коррозия"]


def test_parse_question_synonyms():
    topics = pipeline.Topics()
    q = pipeline.parse_question({"вид": "один", "вопрос": "Что идёт на катоде при электролизе?",
                                 "варианты": ["окисление", "восстановление", "плавление", "ничего"],
                                 "ответ": "Б", "объяснение": "На катоде электроны присоединяются."}, topics, "Химия")
    assert q["type"] == "single" and q["correct"] == [1] and q["topic"] == "Химия"
    assert pipeline.problems(q) == []
    tf = pipeline.parse_question({"type": "tf", "text": "Ржавчина — продукт коррозии железа", "correct": "верно",
                                  "explanation": "Так в тексте."}, topics, "Химия")
    assert tf["options"] == ["Верно", "Неверно"] and tf["correct"] == [0] and not pipeline.problems(tf)
    multi = pipeline.parse_question({"type": "multi", "text": "Что защищает от коррозии?", "options": ["краска", "ингибитор", "вода", "кислота"],
                                     "correct": ["краска", "ингибитор"], "explanation": "…"}, topics, "")
    assert multi["correct"] == [0, 1] and not pipeline.problems(multi)
    bad = pipeline.parse_question({"type": "single", "text": "Два верных?", "options": ["а", "б", "в", "г"], "correct": [0, 1],
                                   "explanation": "x"}, topics, "")
    assert pipeline.problems(bad)


def test_hint_never_gives_answer_and_options_shuffled():
    q = {"type": "single", "text": "Где восстановление?", "options": ["на катоде", "на аноде", "в растворе", "нигде"],
         "correct": [0], "explanation": "x", "hint": "Ответ: на катоде", "quote": "На катоде идёт восстановление", "topic": ""}
    out = pipeline.finish_question(dict(q), QuoteIndex(samples.LECTURE_1))
    assert out["options"][out["correct"][0]] == "на катоде"
    assert "на катоде" not in out["hint"]
    assert out["quote"]  # цитата есть в тексте — остаётся
    q2 = dict(q, quote="Этого в тексте точно нет вообще никогда")
    assert pipeline.finish_question(q2, QuoteIndex(samples.LECTURE_1))["quote"] == ""


def test_ticket_questions_any_shape():
    assert [q["text"] for q in pipeline._ticket_questions(["Первый вопрос", "Второй вопрос"])] == ["Первый вопрос", "Второй вопрос"]
    qs = pipeline._ticket_questions({"тема": "Коррозия", "вопросы": [{"вопрос": "Что такое коррозия?"}]})
    assert qs == [{"text": "Что такое коррозия?", "topic": "Коррозия"}]


def test_relevant_parts_picks_matching_piece():
    filler = "Посторонний текст про погоду и облака. " * 400
    material = filler + "\nПротекторная защита: цинк разрушается вместо железа.\n" + filler
    ctx = pipeline.relevant_parts(material, "Как работает протекторная защита?", 2500)
    assert "Протекторная защита" in ctx and len(ctx) < len(material)


def test_json_tolerant():
    assert parse_json('```json\n{"a": [1, 2,],}\n```') == {"a": [1, 2]}
    assert unwrap({"result": {"tickets": []}}, ("tickets",)) == {"tickets": []}


def test_range_and_split():
    text = combine([("a.txt", samples.LECTURE_1), ("b.txt", samples.LECTURE_2)])
    cut, note, _ = apply_range(text, "только 2-й файл")
    assert "Коррозия" in cut and "Электролиз —" not in cut and note
    parts = split_text(samples.LECTURE_1 * 50, 1000)
    assert len(parts) > 1 and all(len(p) <= 1000 * 3.2 + 10 for p in parts)


def test_browser_pbkdf2_hash_accepted():
    """Хэш со страницы-генератора (PBKDF2-SHA256 из WebCrypto) подходит для входа."""
    import hashlib

    from app.auth import verify_hash
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    h = hashlib.pbkdf2_hmac("sha256", "мой-пароль".encode(), salt, 600_000).hex()
    stored = f"pbkdf2_sha256$600000${salt.hex()}${h}"
    assert verify_hash("мой-пароль", stored) and not verify_hash("чужой", stored)
    assert not verify_hash("мой-пароль", f"pbkdf2_sha256$10${salt.hex()}${h}")  # слишком мало итераций


def test_ydb_url_tolerates_copied_tails():
    """Секреты базы, скопированные «с хвостами», всё равно дают правильный адрес."""
    from app.config import ydb_url
    good = "grpcs://ydb.serverless.yandexcloud.net:2135/?database=/ru-central1/b1g/etn"
    db = "/ru-central1/b1g/etn"
    for ep, d in [("grpcs://ydb.serverless.yandexcloud.net:2135", db),
                  ("grpcs://ydb.serverless.yandexcloud.net:2135/", db),
                  ("grpcs://ydb.serverless.yandexcloud.net:2135database=", db),
                  ("grpcs://ydb.serverless.yandexcloud.net:2135/?database=", db),
                  ("grpcs://ydb.serverless.yandexcloud.net:2135/?database=/ru-central1/b1g/etn", ""),
                  ("grpcs://ydb.serverless.yandexcloud.net:2135", "database=/ru-central1/b1g/etn"),
                  ("grpcs://ydb.serverless.yandexcloud.net:2135", "ru-central1/b1g/etn/"),
                  ("grpcs://ydb.serverless.yandexcloud.net", db)]:
        assert ydb_url(ep, d) == good, (ep, d)
    assert ydb_url("", db) == ""
