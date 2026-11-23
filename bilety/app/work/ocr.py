"""Распознавание текста на фото и сканах.

Основной путь — Yandex Vision OCR (тот же API-ключ и каталог, что у YandexGPT; у сервисного аккаунта
нужна роль ai.vision.user). Если YandexGPT не настроен, а выбран Claude с ключом — фото читает Claude
(визуальный ввод). Каждое распознавание пишется в расходы.

Для тестов распознавание можно подменить: use(fn), где fn(data, mime) → текст.
"""
from __future__ import annotations

import base64

import httpx

from app import config, store
from app.ai.base import AIError, http_error

URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"  # 🟡 синхронный режим, 1 картинка за запрос

_override = None


def use(fn) -> None:
    """Подменить распознавание (для тестов)."""
    global _override
    _override = fn


class OcrUnavailable(RuntimeError):
    pass


def _mime_name(mime: str) -> str:
    return {"image/png": "PNG", "application/pdf": "PDF"}.get(mime, "JPEG")


def _yandex(data: bytes, mime: str) -> str:
    from app.ai.yandex import _metadata_token

    cfg = store.provider_config("yandex")
    folder = cfg.get("folder_id")
    key = cfg.get("api_key")
    auth = f"Api-Key {key}" if key else (f"Bearer {t}" if (t := _metadata_token()) else "")
    if not folder or not auth:
        raise OcrUnavailable("yandex")
    body = {"mimeType": _mime_name(mime), "languageCodes": ["ru", "en"], "model": "page",
            "content": base64.b64encode(data).decode()}
    try:
        r = httpx.post(URL, json=body, timeout=120, headers={"Authorization": auth, "x-folder-id": folder})
    except httpx.HTTPError as e:
        raise AIError(f"Нет связи с распознаванием текста Яндекса: {type(e).__name__}", "network") from e
    if r.status_code != 200:
        raise http_error("Распознавание текста (Yandex Vision)", r.status_code, r.text)
    d = r.json()
    ann = (d.get("result") or d).get("textAnnotation") or {}
    text = ann.get("fullText") or ""
    if not text and ann.get("blocks"):
        lines = []
        for b in ann["blocks"]:
            for ln in b.get("lines", []):
                lines.append(ln.get("text") or " ".join(w.get("text", "") for w in ln.get("words", [])))
        text = "\n".join(lines)
    from app.ai import log_usage
    log_usage("yandex", "vision-ocr", "ocr", 0, 0, config.OCR_PRICE_RUB, None, None)
    return text


def _claude(data: bytes, mime: str) -> str:
    cfg = store.provider_config("claude")
    if not cfg.get("api_key"):
        raise OcrUnavailable("claude")
    import anthropic

    from app import ai
    from app.ai.claude import ClaudeProvider

    p = ClaudeProvider(cfg)
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=300)
    media = mime if mime in ("image/png", "image/jpeg", "image/webp", "image/gif") else "image/jpeg"
    try:
        resp = client.messages.create(model=p.model, max_tokens=8000, messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": "Перепиши весь текст с этого изображения дословно, сохраняя абзацы и списки. "
                                     "Только текст, без комментариев. Формулы — обычным текстом."}]}])
    except anthropic.APIError as e:
        raise AIError(f"Claude не смог прочитать фото: {e}", "other") from e
    text = "".join(b.text for b in resp.content if b.type == "text")
    ai.log_usage(p.name, p.model, "ocr", resp.usage.input_tokens, resp.usage.output_tokens,
                 ai.price_of(p, resp.usage.input_tokens, resp.usage.output_tokens), None, None)
    return text


def recognize(data: bytes, mime: str = "image/jpeg") -> str:
    """Текст с картинки. Бросает AIError (понятная причина) или OcrUnavailable (ничего не настроено)."""
    if _override is not None:
        return _override(data, mime)
    order = [_yandex, _claude] if store.active_provider() != "claude" else [_claude, _yandex]
    for fn in order:
        try:
            return fn(data, mime)
        except OcrUnavailable:
            continue
    raise OcrUnavailable("Фото и сканы читаются через Yandex Vision (нужны ключ и ID каталога YandexGPT в Настройках) "
                         "или через Claude. Сейчас ни то, ни другое не настроено.")
