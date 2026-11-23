"""Все адреса API. Каждый ответ с ошибкой — {"detail": "понятный текст"} и код 4xx/5xx,
экран показывает его с кнопкой «Повторить»."""
from __future__ import annotations

import datetime as dt
import re

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app import ai, auth, config, store
from app.ai.base import AIError
from app.db import Result, SessionLocal, UploadPart, Usage, dumps, loads, new_id, now
from app.work import pipeline
from app.work.material import (MaterialError, apply_range, clean_text, combine, estimate_tokens, extract_file,
                               fetch_url, too_big_message)

router = APIRouter(prefix="/api")
User = Depends(auth.current_user)


def _err(e: Exception, code: int = 400):
    raise HTTPException(code, str(e)) from e


def _own(result_id: str, user_id: str) -> Result:
    with SessionLocal() as s:
        row = s.get(Result, result_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(404, "Не найдено (возможно, уже удалено)")
    return row


# --- Вход ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    auth.check_password(body.password, auth.client_id(request))
    _set_cookie(request, response)
    return {"ok": True}


def _set_cookie(request: Request, response: Response) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, auth.make_token(), max_age=auth.TTL, httponly=True, samesite="lax", secure=secure)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


class PasswordIn(BaseModel):
    old: str
    new: str


@router.post("/password")
def change_password(body: PasswordIn, request: Request, response: Response, user_id: str = User):
    auth.change_password(body.old, body.new)
    _set_cookie(request, response)  # это устройство остаётся внутри, остальные выходят
    return {"ok": True}


@router.get("/bootstrap")
def bootstrap(user_id: str = User):
    """Всё, что нужно экрану при запуске, одним запросом."""
    st = store.get_many(user_id=user_id)
    return {"ok": True, "palette": st.get("palette") or "classic", "quiz": store.quiz_settings(st),
            "provider": store.active_provider(), "functions": config.FUNCTIONS,
            "limits": {"max_files": config.MAX_FILES, "max_file_mb": config.MAX_FILE_MB,
                       "part_bytes": config.UPLOAD_PART_BYTES, "max_urls": config.MAX_URLS,
                       "choices": config.QUESTION_CHOICES, "max_questions": config.MAX_QUESTIONS,
                       "levels": {k: {"name": v["name"], "timer_sec": v["timer_sec"]} for k, v in config.LEVELS.items()}}}


# --- Загрузка файлов кусками ---------------------------------------------------------

@router.post("/uploads")
async def upload_part(upload_id: str = Form(""), n: int = Form(0), filename: str = Form(...),
                      part: UploadFile = File(...), user_id: str = User):
    """Файл приходит кусками по ~2 МБ (у Yandex Serverless Containers запрос ограничен ~3,5 МБ).
    Куски временно лежат в базе и удаляются сразу после извлечения текста."""
    data = await part.read()
    if len(data) > config.UPLOAD_PART_BYTES + 1024:
        raise HTTPException(413, "Слишком большой кусок файла")
    upload_id = upload_id or new_id()
    with SessionLocal() as s:
        prev = list(s.scalars(sa.select(UploadPart).where(UploadPart.upload_id == upload_id)))
        if any(p.user_id != user_id for p in prev):
            raise HTTPException(404, "Не найдено")
        total = sum(len(p.data or b"") for p in prev)
        if total + len(data) > config.MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(413, f"«{filename}» больше {config.MAX_FILE_MB} МБ. Раздели файл на части "
                                     f"или сохрани PDF в «уменьшенном размере».")
        s.add(UploadPart(user_id=user_id, upload_id=upload_id, n=n, filename=filename, data=data))
        s.commit()
    return {"upload_id": upload_id}


def _take_upload(upload_id: str, user_id: str) -> tuple[str, bytes]:
    with SessionLocal() as s:
        parts = s.scalars(sa.select(UploadPart).where(UploadPart.upload_id == upload_id, UploadPart.user_id == user_id)
                          .order_by(UploadPart.n)).all()
        if not parts:
            raise MaterialError("Файл не дошёл до сервера. Выбери его ещё раз.")
        name = parts[0].filename or "file"
        data = b"".join(p.data or b"" for p in parts)
        s.execute(sa.delete(UploadPart).where(UploadPart.upload_id == upload_id))
        s.commit()
    return name, data


def _cleanup_uploads() -> None:
    """Забытые куски старше часа — удаляем."""
    with SessionLocal() as s:
        s.execute(sa.delete(UploadPart).where(UploadPart.created_at < now() - dt.timedelta(hours=1)))
        s.commit()


# --- Создание -------------------------------------------------------------------------

class ResultIn(BaseModel):
    func: str
    note: str = ""            # «уточни, что нужно»; для «Ответа на вопрос» — сам вопрос
    source_range: str = ""
    text: str = ""
    urls: list[str] = Field(default_factory=list)
    uploads: list[str] = Field(default_factory=list)
    from_result: str = ""     # взять текст из прошлого результата (файлы второй раз не грузить)


def _title(func: str, names: list[dict], note: str) -> str:
    if func == "answer" and note.strip():
        q = re.sub(r"\s+", " ", note.strip())
        return q[:80] + ("…" if len(q) > 80 else "")
    files = [re.sub(r"\.\w{2,5}$", "", n["name"]) for n in names if n.get("name")]
    if not files:
        return config.FUNCTIONS[func]["short"]
    return files[0][:60] + (f" + ещё {len(files) - 1}" if len(files) > 1 else "")


@router.post("/results")
def create_result(body: ResultIn, user_id: str = User):
    """Шаг 1: достаём текст, обрезаем «откуда докуда», сохраняем. Сам результат — шаг 2 (/run)."""
    if body.func not in config.FUNCTIONS:
        raise HTTPException(400, "Выбери функцию")
    if body.func == "answer" and not body.note.strip():
        raise HTTPException(400, "Напиши вопрос: ответ строится по загруженным файлам.")
    if len(body.uploads) > config.MAX_FILES:
        raise HTTPException(400, f"Не больше {config.MAX_FILES} файлов")
    urls = [u.strip() for u in body.urls if u.strip()]
    if len(urls) > config.MAX_URLS:
        raise HTTPException(400, f"Не больше {config.MAX_URLS} ссылок")
    warnings: list[str] = []
    names: list[dict] = []
    try:
        if body.from_result:
            old = _own(body.from_result, user_id)
            material = pipeline.load_text(old.id)
            if not material:
                raise MaterialError("Текст того материала не сохранился. Загрузи файлы заново.")
            names = loads(old.sources, [])
            note, rest = old.range_note or "", ""
            if body.source_range.strip():
                material, note, rest = apply_range(material, body.source_range)
        else:
            sources: list[tuple[str, str]] = []
            for up in body.uploads:
                name, data = _take_upload(up, user_id)
                sources.append((name, extract_file(name, data, warnings)))
                names.append({"kind": "file", "name": name, "size": len(data)})
            for u in urls:
                title, text = fetch_url(u)
                sources.append((title, text))
                names.append({"kind": "url", "name": title, "url": u})
            if body.text.strip():
                sources.append(("вставленный текст", clean_text(body.text)))
                names.append({"kind": "text", "name": "вставленный текст", "size": len(body.text)})
            if not sources:
                raise MaterialError("Добавь материал: файл, фото, текст или ссылку.")
            material = combine(sources)
            material, note, rest = apply_range(material, body.source_range)
        if len(material) > config.MAX_TEXT_CHARS:
            raise MaterialError(too_big_message(len(material)))
    except MaterialError as e:
        _err(e)
    finally:
        _cleanup_uploads()
    params = {}
    if body.func == "questions":
        params = store.quiz_settings()
    if rest and body.func != "answer":
        body.note = (body.note + "\n" if body.note.strip() else "") + f"Читать только: {rest}"
    tokens = estimate_tokens(material)
    try:
        one_request = pipeline.limits(ai.get_provider())[1]
    except AIError:
        one_request = 20_000
    if tokens > one_request and body.func != "answer":
        warnings.append("Материал большой: ИИ будет работать по частям, это дольше и дороже.")
    rid = new_id()
    with SessionLocal() as s:
        s.add(Result(id=rid, user_id=user_id, func=body.func, title=_title(body.func, names, body.note),
                     note=body.note.strip(), params=dumps(params), source_range=body.source_range, range_note=note,
                     sources=dumps(names), chars=len(material), status="new", status_text="текст готов",
                     warnings=dumps(warnings)))
        s.commit()
    pipeline.save_text(rid, material, user_id)
    return {"result_id": rid, "warnings": warnings, "range_note": note, "range_rest": rest,
            "chars": len(material), "tokens": tokens}


@router.post("/results/{result_id}/run")
def run(result_id: str, user_id: str = User):
    """Шаг 2: результат от ИИ. Запрос долгий; экран параллельно спрашивает /status."""
    _own(result_id, user_id)
    try:
        return pipeline.run(result_id)
    except AIError:
        raise
    except (pipeline.WorkError, MaterialError) as e:
        _err(e)


@router.get("/results/{result_id}/status")
def status(result_id: str, user_id: str = User):
    r = pipeline.check_stale(_own(result_id, user_id))
    return {"status": r.status, "status_text": r.status_text, "error": r.error, "warnings": loads(r.warnings, [])}


def _cost(result_ids: list[str]) -> dict[str, float]:
    if not result_ids:
        return {}
    with SessionLocal() as s:
        rows = s.execute(sa.select(Usage.result_id, sa.func.sum(Usage.cost_rub)).where(Usage.result_id.in_(result_ids))
                         .group_by(Usage.result_id)).all()
    return {r[0]: float(r[1] or 0) for r in rows}


def _brief(r: Result, cost: float = 0.0) -> dict:
    last = loads(r.last_attempt)
    return {"id": r.id, "func": r.func, "title": r.title, "created_at": r.created_at.isoformat() if r.created_at else None,
            "status": r.status, "error": r.error, "topics": loads(r.topics, []), "sources": loads(r.sources, []),
            "cost_rub": round(cost, 2), "last_pct": last.get("pct") if last else None}


@router.get("/results")
def list_results(user_id: str = User):
    with SessionLocal() as s:
        rows = s.scalars(sa.select(Result).where(Result.user_id == user_id).order_by(Result.created_at.desc())).all()
    costs = _cost([r.id for r in rows])
    return {"items": [_brief(r, costs.get(r.id, 0)) for r in rows]}


@router.get("/results/{result_id}")
def result_detail(result_id: str, user_id: str = User):
    r = pipeline.check_stale(_own(result_id, user_id))
    out = _brief(r, _cost([r.id]).get(r.id, 0))
    out.update(note=r.note, source_range=r.source_range, range_note=r.range_note, chars=r.chars,
               warnings=loads(r.warnings, []), content=loads(r.content), params=loads(r.params, {}),
               last_attempt=loads(r.last_attempt))
    return out


@router.delete("/results/{result_id}")
def delete_result(result_id: str, user_id: str = User):
    _own(result_id, user_id)
    pipeline.delete_result(result_id)
    return {"ok": True}


class AttemptIn(BaseModel):
    answers: dict = Field(default_factory=dict)   # {question_id: [выбранные номера]}
    duration_sec: int | None = None


@router.post("/results/{result_id}/attempt")
def save_attempt(result_id: str, body: AttemptIn, user_id: str = User):
    """Прохождение вопросов: код сам проверяет ответы и сохраняет последнюю попытку (для Истории)."""
    r = _own(result_id, user_id)
    content = loads(r.content, {})
    qs = content.get("questions") or []
    if r.func != "questions" or not qs:
        raise HTTPException(400, "Здесь нет вопросов")
    res, ok = {}, 0
    for q in qs:
        given = body.answers.get(q["id"])
        chosen = sorted(int(x) for x in given) if isinstance(given, list) else None
        good = chosen is not None and chosen == sorted(q["correct"])
        ok += good
        res[q["id"]] = {"chosen": chosen, "ok": good}
    attempt = {"answers": res, "ok": ok, "total": len(qs), "pct": round(ok * 100 / len(qs), 1),
               "duration_sec": body.duration_sec, "at": now().isoformat()}
    with SessionLocal() as s:
        row = s.get(Result, result_id)
        row.last_attempt = dumps(attempt)
        s.commit()
    return attempt


# --- Настройки ------------------------------------------------------------------------

class QuizIn(BaseModel):
    count: int | None = None
    difficulty: str | None = None
    timer: bool | None = None
    hints: bool | None = None


@router.put("/settings/quiz")
def save_quiz(body: QuizIn, user_id: str = User):
    try:
        return store.save_quiz_settings({k: v for k, v in body.model_dump().items() if v is not None})
    except ValueError as e:
        _err(e)


class PaletteIn(BaseModel):
    palette: str


@router.put("/settings/palette")
def save_palette(body: PaletteIn, user_id: str = User):
    if body.palette not in store.PALETTES:
        raise HTTPException(400, "Нет такой палитры")
    store.put("palette", body.palette, user_id=user_id)
    return {"ok": True}


@router.get("/settings/ai")
def get_ai(user_id: str = User):
    return store.public_ai_settings()


class AIIn(BaseModel):
    provider: str
    fields: dict = Field(default_factory=dict)
    secrets: dict = Field(default_factory=dict)
    price: dict | None = None


@router.put("/settings/ai")
def save_ai(body: AIIn, user_id: str = User):
    try:
        store.save_ai_settings(body.model_dump(exclude_unset=True))
    except ValueError as e:
        _err(e)
    return store.public_ai_settings()


@router.delete("/settings/ai/{provider}/{field}")
def delete_key(provider: str, field: str, user_id: str = User):
    if provider not in store.PROVIDERS or field not in store.PROVIDERS[provider]["secret"]:
        raise HTTPException(404, "Нет такого ключа")
    store.delete_secret(provider, field)
    return store.public_ai_settings()


@router.post("/settings/ai/{provider}/check")
def check_ai(provider: str, user_id: str = User):
    if provider not in store.PROVIDERS:
        raise HTTPException(404, "Нет такого провайдера")
    return ai.check_connection(provider)


class BudgetIn(BaseModel):
    budget: float


@router.put("/settings/budget")
def save_budget(body: BudgetIn, user_id: str = User):
    if body.budget < 0:
        raise HTTPException(400, "Бюджет не может быть меньше нуля")
    store.put("budget", body.budget)
    return {"ok": True}


@router.get("/usage")
def usage(user_id: str = User):
    """Расходы: этот месяц (токены, ₽, по видам и по моделям), всё время, последние вызовы."""
    n = now()
    start = dt.datetime(n.year, n.month, 1)
    with SessionLocal() as s:
        def agg(*where):
            q = sa.select(sa.func.coalesce(sa.func.sum(Usage.tokens_in), 0), sa.func.coalesce(sa.func.sum(Usage.tokens_out), 0),
                          sa.func.coalesce(sa.func.sum(Usage.cost_rub), 0), sa.func.count()).where(*where)
            a, b, c, k = s.execute(q).one()
            return {"tokens_in": int(a), "tokens_out": int(b), "rub": round(float(c), 2), "calls": int(k)}
        month = agg(Usage.created_at >= start)
        total = agg(sa.true())
        by_purpose = [{"purpose": p, "rub": round(float(c or 0), 2), "tokens": int(t or 0)} for p, c, t in s.execute(
            sa.select(Usage.purpose, sa.func.sum(Usage.cost_rub), sa.func.sum(Usage.tokens_in + Usage.tokens_out))
            .where(Usage.created_at >= start).group_by(Usage.purpose)).all()]
        by_model = [{"provider": p, "model": m, "rub": round(float(c or 0), 2), "tokens": int(t or 0)} for p, m, c, t in s.execute(
            sa.select(Usage.provider, Usage.model, sa.func.sum(Usage.cost_rub), sa.func.sum(Usage.tokens_in + Usage.tokens_out))
            .where(Usage.created_at >= start).group_by(Usage.provider, Usage.model)).all()]
        last = s.scalars(sa.select(Usage).order_by(Usage.created_at.desc()).limit(15)).all()
    return {"month": month, "total": total, "budget": store.budget(), "by_purpose": by_purpose, "by_model": by_model,
            "last": [{"at": u.created_at.isoformat() if u.created_at else None, "purpose": u.purpose, "model": u.model,
                      "title": u.result_title, "tokens": (u.tokens_in or 0) + (u.tokens_out or 0),
                      "rub": round(u.cost_rub or 0, 2)} for u in last]}
