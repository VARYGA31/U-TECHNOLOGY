"""Импорт файла → применение настроек → перевод → сборка файлов."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .config import settings
from .models import Quote, has_lang
from .parsing import own_template
from .parsing.extract import Extracted, extract
from .rates import cross_rate, next_number
from .render.common import out_name, prepare_photo
from .render.pdf import render_pdf
from .render.xlsx import render_xlsx
from .storage import photos_dir

PHOTO_EXT = "jpg"


# --------------------------------------------------------------- импорт

def materialize_photos(quote: Quote, ex: Extracted, target_dir: Path) -> None:
    """Метки p<N>.img → реальные файлы фото на диске."""
    target_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[int, str | None] = {}

    def resolve(marker: str | None) -> str | None:
        if not marker or not marker.startswith("p") or not marker.endswith(".img"):
            return marker
        try:
            n = int(marker[1:-4])
        except ValueError:
            return None
        if n in cache:
            return cache[n]
        data = ex.photos.get(n)
        if not data:
            cache[n] = None
            return None
        name = f"p{n}.{PHOTO_EXT}"
        try:
            (target_dir / name).write_bytes(prepare_photo(data))
        except OSError:
            cache[n] = None
            return None
        cache[n] = name
        return name

    for product in quote.products:
        product.photo = resolve(product.photo)


async def import_file(path: str, filename: str, uid: int,
                      on_step: Callable[[str], Awaitable[None]] | None = None
                      ) -> tuple[Quote | None, str]:
    """Возвращает (КП, способ разбора): 'own' | 'ai' | ''."""
    ex = await asyncio.to_thread(extract, path, filename)
    if ex.error and not ex.text and not ex.images_for_ai:
        return None, ""

    async def step(name: str) -> None:
        if on_step:
            with suppress(Exception):
                await on_step(name)

    quote: Quote | None = None
    method = ""
    if own_template.looks_like_own_format(ex):
        quote = await asyncio.to_thread(own_template.parse_own, ex, filename)
        if quote:
            method = "own"

    if quote is None and settings.ai_enabled:
        from .parsing.ai import match_photos_ai, parse_quote_ai

        quote = await parse_quote_ai(ex.text, ex.images_for_ai, filename)
        if quote:
            method = "ai"
            # листы каталога привязываем отдельным проходом: мелкие
            # параллельные запросы вместо одного тяжёлого
            pages = [(m, d) for m, d in ex.images_for_ai if m]
            if pages and any(not p.photo for p in quote.products):
                await step("photos")
                await match_photos_ai(quote, ex.images_for_ai)

    if quote is None:
        return None, ""

    quote.number = next_number()
    quote.source_file = filename
    materialize_photos(quote, ex, photos_dir(uid))
    quote.ensure_columns()
    return quote, method


# --------------------------------------------------------------- настройки

def apply_hidden(quote: Quote, prefs: dict[str, Any]) -> None:
    """Скрытые колонки запоминаются и применяются к следующим КП."""
    from .models import BASE_ORDER
    from .models import t as text_of

    hidden_base = set(prefs.get("hidden_base") or [])
    hidden_titles = {s.strip().lower() for s in (prefs.get("hidden_titles") or []) if s}
    for col in quote.columns:
        if col.key in ("name", "price"):
            continue  # эти колонки не выключаются
        if col.key in BASE_ORDER:
            if col.key in hidden_base:
                col.on = False
        else:
            titles = {text_of(col.title, lang).strip().lower()
                      for lang in ("ru", "zh", "en")}
            if titles & hidden_titles:
                col.on = False


def remember_hidden(quote: Quote, prefs: dict[str, Any]) -> None:
    from .models import BASE_ORDER
    from .models import t as text_of

    hidden_base = []
    hidden_titles = []
    for col in quote.columns:
        if col.on:
            continue
        if col.key in BASE_ORDER:
            hidden_base.append(col.key)
        else:
            title = text_of(col.title, quote.src_lang).strip()
            if title:
                hidden_titles.append(title)
    prefs["hidden_base"] = sorted(set(hidden_base))
    prefs["hidden_titles"] = sorted(set(hidden_titles))


async def apply_prefs(quote: Quote, prefs: dict[str, Any]) -> None:
    quote.markup_pct = float(prefs.get("markup_pct") or 0.0)
    quote.rounding = float(prefs.get("rounding") or 1.0)
    currency = prefs.get("currency") or quote.source_currency
    quote.currency = currency
    if prefs.get("rate"):
        quote.rate = float(prefs["rate"])
    else:
        rate = await cross_rate(quote.source_currency, currency)
        quote.rate = rate if rate else 1.0
    apply_hidden(quote, prefs)


def missing_langs(quote: Quote, langs: Iterable[str]) -> list[str]:
    out = []
    for lang in langs:
        if any(not has_lang(x, lang) for x in quote.all_texts()):
            out.append(lang)
    return out


async def translate_to(quote: Quote, langs: Iterable[str]) -> None:
    if not settings.ai_enabled:
        return
    from .parsing.ai import translate_quote

    for lang in langs:
        await translate_quote(quote, lang)


# --------------------------------------------------------------- сборка

def _build_one(quote: Quote, lang: str, out_dir: Path, photo_dir: Path) -> tuple[Path, Path]:
    xlsx_path = out_dir / out_name(quote.number, lang, "xlsx")
    pdf_path = out_dir / out_name(quote.number, lang, "pdf")
    render_xlsx(quote, lang, xlsx_path, photo_dir)
    render_pdf(quote, lang, pdf_path, photo_dir)
    return xlsx_path, pdf_path


async def build_files(quote: Quote, langs: Iterable[str], out_dir: Path,
                      photo_dir: Path) -> list[tuple[str, Path, Path]]:
    """Тяжёлая сборка — в отдельном потоке, чтобы не блокировать бота."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result: list[tuple[str, Path, Path]] = []
    for lang in langs:
        xlsx_path, pdf_path = await asyncio.to_thread(
            _build_one, quote, lang, out_dir, photo_dir)
        result.append((lang, xlsx_path, pdf_path))
    return result
