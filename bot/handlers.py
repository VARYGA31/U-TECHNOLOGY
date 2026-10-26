"""Телеграм: файлы, карточка с кнопками, ввод значений, отправка."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections import defaultdict
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaDocument, Message)

from .config import settings
from .i18n import FLAGS, tr
from .models import (BASE_ORDER, Column, Line, Option, Product, Quote,
                     has_lang, parse_number, t)
from .rates import cross_rate
from .render import labels as L
from .service import (apply_prefs, build_files, import_file, missing_langs,
                      remember_hidden, translate_to)
from .storage import (clear_quote, load_prefs, load_quote, out_dir, photos_dir,
                      save_prefs, save_quote)

log = logging.getLogger(__name__)
router = Router()

MAX_FILE_MB = 20
PAGE_SIZE = 8
MARKUPS = [0, 3, 5, 6, 8, 10, 12, 15, 20, 25, 30]
CURRENCIES = ["CNY", "USD", "RUB", "EUR"]
ROUNDINGS = [0.01, 1, 10, 100]
DOC_LANGS = ["ru", "zh", "en"]

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def lock_for(uid: int) -> asyncio.Lock:
    return _locks[uid]


# --------------------------------------------------------------- вспомогательное

def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def ui_lang(prefs: dict[str, Any]) -> str:
    return "zh" if prefs.get("ui_lang") == "zh" else "ru"


def allowed(uid: int) -> bool:
    return settings.allowed(uid)


async def deny(message: Message) -> None:
    await message.answer(tr("no_access", "ru", message.from_user.id))


def fmt_money(value: float, currency: str, lang: str) -> str:
    return L.money_str(value, currency, "ru" if lang != "zh" else "zh")


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id, message_id)


# --------------------------------------------------------------- карточка

def card_text(quote: Quote, prefs: dict[str, Any]) -> str:
    lang = ui_lang(prefs)
    esc = html.escape
    options = sum(len(p.options) for p in quote.products)
    rows = [
        tr("card_title", lang, esc(quote.number)),
        tr("card_source", lang, esc(quote.source_file or "—")),
        tr("card_items", lang, len(quote.products), options),
        tr("card_markup", lang, _num_str(quote.markup_pct)),
        tr("card_langs", lang, ", ".join(FLAGS.get(x, x) for x in prefs.get("langs", []))),
        tr("card_currency", lang, quote.currency, _num_str(round(quote.rate, 4))),
        tr("card_rounding", lang, _num_str(quote.rounding)),
        "",
    ]
    for i, product in enumerate(quote.products, 1):
        name = esc(t(product.name, lang)[:46] or "—")
        base = fmt_money(quote.convert(product.cost, 0.0), quote.currency, lang)
        now = fmt_money(quote.price(product), quote.currency, lang)
        rows.append(f"{i}. {name} — {base} → <b>{now}</b>")
        for option in product.options:
            otext = esc(t(option.text, lang)[:40])
            rows.append(f"   ∟ {otext} — {fmt_money(quote.option_price(product, option), quote.currency, lang)}")
    for line in quote.lines:
        rows.append(f"🚚 {esc(t(line.label, lang)[:40])} — "
                    f"{fmt_money(quote.line_amount(line), quote.currency, lang)}")
    if quote.show_total:
        rows.append("")
        rows.append("<b>" + tr("card_total", lang,
                               fmt_money(quote.grand_total(), quote.currency, lang)) + "</b>")
    rows.append("")
    rows.append(tr("card_hint", lang))
    return "\n".join(rows)


def _num_str(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def main_kb(prefs: dict[str, Any], quote: Quote) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    return kb([
        [btn(f"{tr('btn_markup', lang)}: {_num_str(quote.markup_pct)}%", "mk"),
         btn(f"{tr('btn_currency', lang)}: {quote.currency}", "cu")],
        [btn(tr("btn_lang", lang), "lg"),
         btn(f"{tr('btn_rounding', lang)}: {_num_str(quote.rounding)}", "rd")],
        [btn(tr("btn_products", lang), "pr"), btn(tr("btn_columns", lang), "co")],
        [btn(tr("btn_delivery", lang), "dl"), btn(tr("btn_client", lang), "cl")],
        [btn(tr("btn_get", lang), "go")],
        [btn(tr("btn_new", lang), "nw"), btn(tr("btn_ui", lang), "ui")],
    ])


async def show_card(bot: Bot, chat_id: int, uid: int) -> None:
    """Старая карточка удаляется, новая всегда внизу чата."""
    prefs = load_prefs(uid)
    quote = load_quote(uid)
    if quote is None:
        await bot.send_message(chat_id, tr("no_quote", ui_lang(prefs)))
        return
    await safe_delete(bot, chat_id, prefs.get("card_id"))
    msg = await bot.send_message(chat_id, card_text(quote, prefs),
                                 reply_markup=main_kb(prefs, quote),
                                 parse_mode="HTML")
    prefs["card_id"] = msg.message_id
    save_prefs(uid, prefs)


async def ask(bot: Bot, chat_id: int, uid: int, prompt: str,
              state: dict[str, Any]) -> None:
    prefs = load_prefs(uid)
    prefs["state"] = state
    save_prefs(uid, prefs)
    await bot.send_message(
        chat_id, prompt, parse_mode="HTML",
        reply_markup=kb([[btn(tr("btn_cancel", ui_lang(prefs)), "x")]]))


def clear_state(uid: int) -> None:
    prefs = load_prefs(uid)
    prefs["state"] = None
    save_prefs(uid, prefs)


# --------------------------------------------------------------- команды

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        await deny(message)
        return
    prefs = load_prefs(uid)
    tg_lang = (message.from_user.language_code or "ru").lower()
    prefs["ui_lang"] = "zh" if tg_lang.startswith("zh") else "ru"
    save_prefs(uid, prefs)
    await message.answer(
        tr("start", ui_lang(prefs)), parse_mode="HTML",
        reply_markup=kb([[btn("🇷🇺 Русский", "ui:ru"), btn("🇨🇳 中文", "ui:zh")]]))
    if not settings.ai_enabled:
        await message.answer(tr("no_ai", ui_lang(prefs)))


@router.message(Command("card"))
async def cmd_card(message: Message) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        await deny(message)
        return
    await show_card(message.bot, message.chat.id, uid)


@router.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    """Проверка настроек: ключ, модель, доступность Responses API, живой вызов."""
    uid = message.from_user.id
    if not allowed(uid):
        await deny(message)
        return
    lines = [tr("diag", ui_lang(load_prefs(uid))), ""]
    lines.append(f"OPENAI_API_KEY: {'задан' if settings.ai_enabled else 'НЕ задан'}")
    lines.append(f"Модель: {settings.openai_model}")
    lines.append(f"OPENAI_BASE_URL: {settings.openai_base_url or '—'}")

    try:
        import openai

        lines.append(f"openai: {openai.__version__}")
    except Exception as exc:
        lines.append(f"openai: ошибка импорта — {exc}")

    if settings.ai_enabled:
        from .parsing.ai import client

        has_api = hasattr(client(), "responses") and hasattr(client().responses, "parse")
        lines.append(f"Responses API: {'есть' if has_api else 'НЕТ — обновите openai'}")
        try:
            from .models import mk
            from .parsing.ai import translate_quote

            probe = Quote(products=[Product(name=mk("电动搬运车"))])
            await translate_quote(probe, "ru")
            got = t(probe.products[0].name, "ru")
            lines.append(f"Тестовый перевод «电动搬运车» → «{got}»")
            lines.append("Итог: перевод работает ✅" if got != "电动搬运车"
                         else "Итог: перевод НЕ работает ❌")
        except Exception as exc:
            lines.append(f"Тестовый перевод: ошибка — {str(exc)[:250]}")
            lines.append("Итог: перевод НЕ работает ❌")

    try:
        import importlib

        importlib.import_module("weasyprint")
        lines.append("PDF (WeasyPrint): готов ✅")
    except Exception as exc:
        lines.append(f"PDF (WeasyPrint): НЕ работает — {str(exc)[:120]}")

    await message.answer("\n".join(lines))


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        await deny(message)
        return
    clear_quote(uid)
    prefs = load_prefs(uid)
    prefs["state"] = None
    prefs["card_id"] = None
    save_prefs(uid, prefs)
    await message.answer(tr("send_file", ui_lang(prefs)))


# --------------------------------------------------------------- приём файла

@router.message(F.document | F.photo)
async def on_file(message: Message) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        await deny(message)
        return
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)
    state = prefs.get("state") or {}

    # ждём фото для позиции
    if state.get("kind") == "photo":
        await _receive_photo(message, uid, state)
        return

    async with lock_for(uid):
        file_id, filename, size = _file_info(message)
        if size and size > MAX_FILE_MB * 1024 * 1024:
            await message.answer(tr("file_too_big", lang))
            return

        status = await message.answer(tr("reading", lang))
        tmp = photos_dir(uid).parent / "src"
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / filename
        try:
            await message.bot.download(file_id, destination=path)
        except Exception as exc:
            await status.edit_text(tr("error", lang, html.escape(str(exc)[:120])))
            return

        from .parsing import own_template
        from .parsing.extract import extract

        ex = await asyncio.to_thread(extract, str(path), filename)
        if own_template.looks_like_own_format(ex):
            with contextlib.suppress(Exception):
                await status.edit_text(tr("own_format", lang))
        elif settings.ai_enabled:
            with contextlib.suppress(Exception):
                await status.edit_text(tr("ai_parsing", lang))
        else:
            await status.edit_text(tr("no_ai", lang))
            return

        try:
            quote, method = await import_file(str(path), filename, uid)
        except Exception as exc:
            log.exception("import failed")
            await status.edit_text(tr("error", lang, html.escape(str(exc)[:150])))
            return
        if quote is None:
            await status.edit_text(tr("parse_failed", lang))
            return

        await apply_prefs(quote, prefs)
        save_quote(uid, quote)
        await _deliver(message.bot, message.chat.id, uid, status)


def _file_info(message: Message) -> tuple[str, str, int]:
    if message.document:
        doc = message.document
        return doc.file_id, doc.file_name or "file.bin", doc.file_size or 0
    photo = message.photo[-1]
    return photo.file_id, f"photo_{photo.file_unique_id}.jpg", photo.file_size or 0


async def _receive_photo(message: Message, uid: int, state: dict[str, Any]) -> None:
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)
    quote = load_quote(uid)
    product = quote.product(state.get("pid", "")) if quote else None
    if quote is None or product is None:
        clear_state(uid)
        return
    file_id, filename, _size = _file_info(message)
    dest = photos_dir(uid) / f"u_{product.id}.jpg"
    try:
        await message.bot.download(file_id, destination=dest)
    except Exception as exc:
        await message.answer(tr("error", lang, html.escape(str(exc)[:120])))
        return
    from .render.common import prepare_photo

    with contextlib.suppress(OSError):
        dest.write_bytes(prepare_photo(dest.read_bytes()))
    product.photo = dest.name
    column = quote.column("photo")
    if column:
        column.on = True
    save_quote(uid, quote)
    clear_state(uid)
    await message.answer(tr("photo_saved", lang))
    await show_card(message.bot, message.chat.id, uid)


# --------------------------------------------------------------- отправка файлов

async def _deliver(bot: Bot, chat_id: int, uid: int, status: Message | None = None) -> None:
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)
    quote = load_quote(uid)
    if quote is None:
        return
    langs = [x for x in prefs.get("langs", ["ru"]) if x in DOC_LANGS] or ["ru"]

    # Перевод обязан быть заметным: если он не сработал, партнёр должен узнать
    # об этом здесь, а не обнаружить иероглифы в готовом КП.
    need = missing_langs(quote, langs)
    if need and not settings.ai_enabled:
        await bot.send_message(chat_id, tr("tr_no_key", lang))
    elif need:
        if status:
            with contextlib.suppress(Exception):
                await status.edit_text(tr("translating", lang))
        try:
            await translate_to(quote, need)
            save_quote(uid, quote)
        except Exception as exc:
            log.exception("translate failed")
            await bot.send_message(
                chat_id, tr("tr_failed", lang, html.escape(str(exc)[:200])))
        else:
            left = sum(1 for x in quote.all_texts()
                       for code in langs if not has_lang(x, code))
            if left:
                await bot.send_message(chat_id, tr("tr_partial", lang, left))

    if status:
        with contextlib.suppress(Exception):
            await status.edit_text(tr("building", lang))
    try:
        built = await build_files(quote, langs, out_dir(uid), photos_dir(uid))
    except Exception as exc:
        log.exception("build failed")
        if status:
            await status.edit_text(tr("error", lang, html.escape(str(exc)[:150])))
        else:
            await bot.send_message(chat_id, tr("error", lang, html.escape(str(exc)[:150])))
        return

    for doc_lang, xlsx_path, pdf_path in built:
        caption = tr("caption", lang, FLAGS.get(doc_lang, doc_lang), quote.number)
        media = [
            # подпись задаём в конструкторе: типы aiogram заморожены
            InputMediaDocument(media=FSInputFile(xlsx_path), caption=caption),
            InputMediaDocument(media=FSInputFile(pdf_path)),
        ]
        with contextlib.suppress(Exception):
            await bot.send_media_group(chat_id, media=media)

    await safe_delete(bot, chat_id, status.message_id if status else None)
    await show_card(bot, chat_id, uid)


# --------------------------------------------------------------- ввод текста

@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    uid = message.from_user.id
    if not allowed(uid):
        await deny(message)
        return
    prefs = load_prefs(uid)
    state = prefs.get("state") or {}
    if not state.get("kind"):
        await message.answer(tr("send_file", ui_lang(prefs)))
        return
    async with lock_for(uid):
        await handle_input(message.bot, message.chat.id, uid,
                           (message.text or "").strip(), state)


async def handle_input(bot: Bot, chat_id: int, uid: int, text: str,
                       state: dict[str, Any]) -> str:
    """Возвращает код результата: ok | bad_number | no_quote."""
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)
    quote = load_quote(uid)
    if quote is None:
        clear_state(uid)
        return "no_quote"
    kind = state.get("kind")
    product = quote.product(state.get("pid", "")) if state.get("pid") else None

    def bad() -> str:
        return "bad_number"

    async def fail() -> str:
        await bot.send_message(chat_id, tr("bad_number", lang))
        return bad()

    if kind == "markup":
        value = parse_number(text)
        if value is None:
            return await fail()
        quote.markup_pct = value
        prefs["markup_pct"] = value
        save_prefs(uid, prefs)

    elif kind == "rate":
        value = parse_number(text)
        if value is None or value <= 0:
            return await fail()
        quote.rate = value
        prefs["rate"] = value
        save_prefs(uid, prefs)

    elif kind == "client":
        quote.client = "" if text == "-" else text

    elif kind == "incoterms":
        quote.incoterms = "" if text == "-" else text

    elif kind in ("payment", "lead", "warranty", "extra"):
        if text == "-":
            quote.terms.pop(kind, None)
        else:
            quote.terms[kind] = _mk(text)

    elif kind == "add_line":
        label, _sep, amount_raw = text.rpartition("|")
        value = parse_number(amount_raw)
        if not label.strip() or value is None:
            return await fail()
        quote.lines.append(Line(label=_mk(label.strip()), amount=value / max(quote.rate, 1e-9)))
        quote.show_total = True

    elif kind == "add_column":
        if text.strip():
            keys = {c.key for c in quote.columns}
            i = 1
            while f"c{i}" in keys:
                i += 1
            quote.columns.append(Column(key=f"c{i}", kind="text", title=_mk(text.strip())))

    elif kind == "add_product":
        parts = [p.strip() for p in text.split("|")]
        if len(parts) >= 3:
            qty, price = parse_number(parts[1]), parse_number(parts[2])
        elif len(parts) == 2:
            qty, price = 1.0, parse_number(parts[1])
        else:
            return await fail()
        if not parts[0] or qty is None or price is None:
            return await fail()
        quote.products.append(Product(
            name=_mk(parts[0]), qty=qty,
            cost=price / max(quote.rate, 1e-9) / (1 + quote.markup_pct / 100.0)))

    elif kind == "add_option" and product is not None:
        label, _sep, price_raw = text.rpartition("|")
        value = parse_number(price_raw)
        if not label.strip() or value is None:
            return await fail()
        product.options.append(Option(
            text=_mk(label.strip()), qty=1,
            cost=value / max(quote.rate, 1e-9) / (1 + quote.markup_for(product) / 100.0)))

    elif product is not None and kind in ("name", "spec", "qty", "price", "markup_item"):
        if kind == "name":
            product.name = _mk(text)
        elif kind == "spec":
            product.spec = _mk(text)
            column = quote.column("spec")
            if column:
                column.on = True
        elif kind == "qty":
            value = parse_number(text)
            if value is None:
                return await fail()
            product.qty = value
        elif kind == "price":
            value = parse_number(text)
            if value is None:
                return await fail()
            product.price_override = value
        elif kind == "markup_item":
            if text == "-":
                product.markup_override = None
            else:
                value = parse_number(text)
                if value is None:
                    return await fail()
                product.markup_override = value

    elif kind == "field" and product is not None:
        key = state.get("col", "")
        column = quote.column(key)
        if column is not None:
            if column.is_money:
                value = parse_number(text)
                if value is None:
                    return await fail()
                product.money[key] = value / max(quote.rate, 1e-9)
            elif column.merged:
                quote.merged[key] = _mk(text)
            else:
                product.texts[key] = _mk(text)

    elif kind == "option_text" and product is not None:
        option = _option(product, state.get("oid", ""))
        if option:
            option.text = _mk(text)

    elif kind in ("option_qty", "option_price") and product is not None:
        option = _option(product, state.get("oid", ""))
        value = parse_number(text)
        if option is None:
            return "ok"
        if value is None:
            return await fail()
        if kind == "option_qty":
            option.qty = value
        else:
            option.price_override = value

    save_quote(uid, quote)
    clear_state(uid)
    await show_card(bot, chat_id, uid)
    return "ok"


def _mk(text: str):
    """Введённый вручную текст помечаем языком строки — остальные переведутся."""
    from .models import mk

    return mk(text)


def _option(product: Product, oid: str) -> Option | None:
    for option in product.options:
        if option.id == oid:
            return option
    return None


# --------------------------------------------------------------- кнопки

@router.callback_query()
async def on_callback(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if not allowed(uid):
        await call.answer(tr("no_access", "ru", uid), show_alert=True)
        return
    data = call.data or ""
    bot = call.bot
    chat_id = call.message.chat.id
    async with lock_for(uid):
        try:
            await route(bot, chat_id, uid, data, call)
        except Exception as exc:
            log.exception("callback failed: %s", data)
            with contextlib.suppress(Exception):
                await call.answer(str(exc)[:180], show_alert=True)
            return
    with contextlib.suppress(Exception):
        await call.answer()


async def route(bot: Bot, chat_id: int, uid: int, data: str,
                call: CallbackQuery) -> None:
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)
    quote = load_quote(uid)

    # --- язык интерфейса ---
    if data.startswith("ui:") or data == "ui":
        if data == "ui":
            prefs["ui_lang"] = "zh" if lang == "ru" else "ru"
        else:
            prefs["ui_lang"] = data.split(":", 1)[1]
        save_prefs(uid, prefs)
        await bot.send_message(chat_id, tr("ui_set", ui_lang(prefs)))
        if quote is None:
            await bot.send_message(chat_id, tr("send_file", ui_lang(prefs)))
        else:
            await show_card(bot, chat_id, uid)
        return

    if data == "x":
        clear_state(uid)
        await safe_delete(bot, chat_id, call.message.message_id)
        await bot.send_message(chat_id, tr("cancelled", lang))
        return

    if data == "nw":
        clear_quote(uid)
        prefs["state"] = None
        prefs["card_id"] = None
        save_prefs(uid, prefs)
        await bot.send_message(chat_id, tr("send_file", lang))
        return

    if quote is None:
        await bot.send_message(chat_id, tr("no_quote", lang))
        return

    edit = _editor(bot, call, lang)

    # --- наценка ---
    if data == "mk":
        rows = [[btn(f"{m}%", f"mk:{m}") for m in MARKUPS[i:i + 4]]
                for i in range(0, len(MARKUPS), 4)]
        rows.append([btn(tr("btn_custom", lang), "mk:c")])
        rows.append([btn(tr("btn_back", lang), "m")])
        await edit(tr("btn_markup", lang), kb(rows))
        return
    if data.startswith("mk:"):
        value = data.split(":", 1)[1]
        if value == "c":
            await ask(bot, chat_id, uid, tr("ask_markup", lang), {"kind": "markup"})
            return
        quote.markup_pct = float(value)
        prefs["markup_pct"] = quote.markup_pct
        save_prefs(uid, prefs)
        save_quote(uid, quote)
        await show_card(bot, chat_id, uid)
        return

    # --- языки файлов ---
    if data == "lg":
        chosen = prefs.get("langs", ["ru"])
        rows = [[btn(("✅ " if x in chosen else "▫️ ") + FLAGS[x], f"lg:{x}")]
                for x in DOC_LANGS]
        rows.append([btn(tr("btn_back", lang), "m")])
        await edit(tr("lang_menu", lang), kb(rows))
        return
    if data.startswith("lg:"):
        code = data.split(":", 1)[1]
        chosen = list(prefs.get("langs", ["ru"]))
        if code in chosen:
            if len(chosen) == 1:
                await call.answer(tr("lang_min", lang), show_alert=True)
                return
            chosen.remove(code)
        else:
            chosen.append(code)
        prefs["langs"] = [x for x in DOC_LANGS if x in chosen]
        save_prefs(uid, prefs)
        rows = [[btn(("✅ " if x in prefs["langs"] else "▫️ ") + FLAGS[x], f"lg:{x}")]
                for x in DOC_LANGS]
        rows.append([btn(tr("btn_back", lang), "m")])
        await edit(tr("lang_menu", lang), kb(rows))
        return

    # --- валюта ---
    if data == "cu":
        rows = [[btn(("✅ " if c == quote.currency else "") + c, f"cu:{c}")
                 for c in CURRENCIES]]
        rows.append([btn(tr("btn_custom_rate", lang), "cu:c")])
        rows.append([btn(tr("btn_back", lang), "m")])
        await edit(tr("currency_menu", lang), kb(rows))
        return
    if data.startswith("cu:"):
        code = data.split(":", 1)[1]
        if code == "c":
            await ask(bot, chat_id, uid,
                      tr("ask_rate", lang, quote.currency, quote.source_currency),
                      {"kind": "rate"})
            return
        rate = await cross_rate(quote.source_currency, code)
        prefs["currency"] = code
        prefs["rate"] = None
        save_prefs(uid, prefs)
        if rate is None:
            quote.currency = code
            save_quote(uid, quote)
            await ask(bot, chat_id, uid,
                      tr("rate_failed", lang, code, quote.source_currency),
                      {"kind": "rate"})
            return
        quote.currency, quote.rate = code, rate
        save_quote(uid, quote)
        await show_card(bot, chat_id, uid)
        return

    # --- округление ---
    if data == "rd":
        rows = [[btn(_num_str(r), f"rd:{r}") for r in ROUNDINGS],
                [btn(tr("btn_back", lang), "m")]]
        await edit(tr("rounding_menu", lang), kb(rows))
        return
    if data.startswith("rd:"):
        quote.rounding = float(data.split(":", 1)[1])
        prefs["rounding"] = quote.rounding
        save_prefs(uid, prefs)
        save_quote(uid, quote)
        await show_card(bot, chat_id, uid)
        return

    # --- позиции ---
    if data == "pr" or data.startswith("pr:p"):
        page = int(data[4:]) if data.startswith("pr:p") else 0
        await edit(tr("products_menu", lang), products_kb(quote, prefs, page))
        return
    if data.startswith("pi:"):
        pid = data.split(":", 1)[1]
        product = quote.product(pid)
        if product is None:
            return
        await edit(tr("product_card", lang,
                      quote.products.index(product) + 1,
                      html.escape(t(product.name, lang)[:60])),
                   product_kb(quote, product, prefs))
        return
    if data.startswith("pf:"):
        _p, pid, field = data.split(":", 2)
        await product_action(bot, chat_id, uid, quote, pid, field, call)
        return

    # --- опции ---
    if data.startswith("po:"):
        pid = data.split(":", 1)[1]
        product = quote.product(pid)
        if product is None:
            return
        await edit(tr("options_menu", lang, html.escape(t(product.name, lang)[:40])),
                   options_kb(quote, product, prefs))
        return
    if data.startswith("oi:"):
        _o, pid, oid = data.split(":", 2)
        product = quote.product(pid)
        option = _option(product, oid) if product else None
        if product is None or option is None:
            return
        await edit(tr("option_card", lang, html.escape(t(option.text, lang)[:60])),
                   option_kb(pid, option, prefs))
        return
    if data.startswith("of:"):
        _o, pid, oid, field = data.split(":", 3)
        await option_action(bot, chat_id, uid, quote, pid, oid, field)
        return

    # --- другие поля позиции ---
    if data.startswith("px:"):
        pid = data.split(":", 1)[1]
        product = quote.product(pid)
        if product is None:
            return
        await edit(tr("fields_menu", lang, html.escape(t(product.name, lang)[:40])),
                   fields_kb(quote, product, prefs))
        return
    if data.startswith("pv:"):
        _p, pid, key = data.split(":", 2)
        column = quote.column(key)
        title = t(column.title, lang) if column else key
        await ask(bot, chat_id, uid, tr("ask_field", lang, html.escape(title)),
                  {"kind": "field", "pid": pid, "col": key})
        return

    # --- колонки ---
    if data == "co":
        await edit(tr("columns_menu", lang), columns_kb(quote, prefs))
        return
    if data.startswith("ct:"):
        key = data.split(":", 1)[1]
        column = quote.column(key)
        if column and key not in ("name", "price"):
            column.on = not column.on
            remember_hidden(quote, prefs)
            save_prefs(uid, prefs)
            save_quote(uid, quote)
        await edit(tr("columns_menu", lang), columns_kb(quote, prefs))
        return
    if data == "ctr":
        quote.show_total = not quote.show_total
        save_quote(uid, quote)
        await edit(tr("columns_menu", lang), columns_kb(quote, prefs))
        return
    if data == "ca":
        await ask(bot, chat_id, uid, tr("ask_add_column", lang), {"kind": "add_column"})
        return

    # --- доставка и условия ---
    if data == "dl":
        await edit(tr("delivery_menu", lang), delivery_kb(quote, prefs))
        return
    if data == "di":
        await ask(bot, chat_id, uid, tr("ask_incoterms", lang), {"kind": "incoterms"})
        return
    if data.startswith("dt:"):
        key = data.split(":", 1)[1]
        await ask(bot, chat_id, uid, tr("ask_terms", lang), {"kind": key})
        return
    if data == "da":
        await ask(bot, chat_id, uid, tr("ask_line", lang), {"kind": "add_line"})
        return
    if data.startswith("dd:"):
        lid = data.split(":", 1)[1]
        quote.lines = [x for x in quote.lines if x.id != lid]
        save_quote(uid, quote)
        await edit(tr("delivery_menu", lang), delivery_kb(quote, prefs))
        return

    # --- клиент, сборка, возврат ---
    if data == "cl":
        await ask(bot, chat_id, uid, tr("ask_client", lang), {"kind": "client"})
        return
    if data == "go":
        status = await bot.send_message(chat_id, tr("building", lang))
        await _deliver(bot, chat_id, uid, status)
        return
    if data == "m":
        await show_card(bot, chat_id, uid)
        return


def _editor(bot: Bot, call: CallbackQuery, lang: str):
    async def edit(text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception as exc:
            # «message is not modified» — глушим
            if "not modified" not in str(exc):
                with contextlib.suppress(Exception):
                    await bot.send_message(call.message.chat.id, text,
                                           reply_markup=markup, parse_mode="HTML")
    return edit


# --------------------------------------------------------------- клавиатуры

def products_kb(quote: Quote, prefs: dict[str, Any], page: int) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    start = page * PAGE_SIZE
    chunk = quote.products[start:start + PAGE_SIZE]
    rows = [[btn(f"{start + i + 1}. {t(p.name, lang)[:34] or '—'}", f"pi:{p.id}")]
            for i, p in enumerate(chunk)]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"pr:p{page - 1}"))
    if start + PAGE_SIZE < len(quote.products):
        nav.append(btn("▶️", f"pr:p{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn(tr("btn_add_product", lang), "pf::add")])
    rows.append([btn(tr("btn_back", lang), "m")])
    return kb(rows)


def product_kb(quote: Quote, product: Product, prefs: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    pid = product.id
    price_now = fmt_money(quote.price(product), quote.currency, lang)
    markup = ("-" if product.markup_override is None
              else _num_str(product.markup_override))
    rows = [
        [btn(tr("btn_p_name", lang), f"pf:{pid}:name"),
         btn(tr("btn_p_spec", lang), f"pf:{pid}:spec")],
        [btn(f"{tr('btn_p_qty', lang)}: {_num_str(product.qty)}", f"pf:{pid}:qty"),
         btn(f"{tr('btn_p_price', lang)}: {price_now}", f"pf:{pid}:price")],
        [btn(tr("btn_p_price_auto", lang), f"pf:{pid}:auto"),
         btn(f"{tr('btn_p_markup', lang)}: {markup}", f"pf:{pid}:markup_item")],
        [btn(tr("btn_p_photo", lang), f"pf:{pid}:photo")],
    ]
    if product.photo:
        rows[-1].append(btn(tr("btn_p_photo_del", lang), f"pf:{pid}:photo_del"))
    rows.append([btn(f"{tr('btn_p_options', lang)} ({len(product.options)})", f"po:{pid}"),
                 btn(tr("btn_p_fields", lang), f"px:{pid}")])
    rows.append([btn(tr("btn_p_up", lang), f"pf:{pid}:up"),
                 btn(tr("btn_p_del", lang), f"pf:{pid}:del")])
    rows.append([btn(tr("btn_back", lang), "pr")])
    return kb(rows)


def options_kb(quote: Quote, product: Product, prefs: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    rows = [[btn(f"{t(o.text, lang)[:28] or '—'} · "
                 f"{fmt_money(quote.option_price(product, o), quote.currency, lang)}",
                 f"oi:{product.id}:{o.id}")] for o in product.options]
    rows.append([btn(tr("btn_add_option", lang), f"pf:{product.id}:add_option")])
    rows.append([btn(tr("btn_back", lang), f"pi:{product.id}")])
    return kb(rows)


def option_kb(pid: str, option: Option, prefs: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    oid = option.id
    return kb([
        [btn(tr("btn_o_text", lang), f"of:{pid}:{oid}:text")],
        [btn(f"{tr('btn_o_qty', lang)}: {_num_str(option.qty)}", f"of:{pid}:{oid}:qty"),
         btn(tr("btn_o_price", lang), f"of:{pid}:{oid}:price")],
        [btn(tr("btn_o_del", lang), f"of:{pid}:{oid}:del")],
        [btn(tr("btn_back", lang), f"po:{pid}")],
    ])


def fields_kb(quote: Quote, product: Product, prefs: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    rows = []
    for column in quote.columns:
        if column.key in BASE_ORDER:
            continue
        if column.merged:
            value = t(quote.merged.get(column.key), lang)
        elif column.is_money:
            value = _num_str(product.money.get(column.key, 0.0))
        else:
            value = t(product.texts.get(column.key), lang)
        title = t(column.title, lang)[:20]
        rows.append([btn(f"{title}: {value[:16] or '—'}", f"pv:{product.id}:{column.key}")])
    rows.append([btn(tr("btn_back", lang), f"pi:{product.id}")])
    return kb(rows)


def columns_kb(quote: Quote, prefs: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    rows = []
    for column in quote.columns:
        if column.key in ("price", "total"):
            title = L.BASE_TITLES[column.key][lang]
        else:
            title = t(column.title, lang) or L.BASE_TITLES.get(column.key, {}).get(lang, column.key)
        title = title.replace("\n", " ")[:28]
        if column.key in ("name", "price"):
            rows.append([btn(f"🔒 {title}", "co")])
        else:
            rows.append([btn(("✅ " if column.on else "▫️ ") + title, f"ct:{column.key}")])
    rows.append([btn(tr("btn_total_row", lang,
                        tr("on" if quote.show_total else "off", lang)), "ctr")])
    rows.append([btn(tr("btn_add_column", lang), "ca")])
    rows.append([btn(tr("btn_back", lang), "m")])
    return kb(rows)


def delivery_kb(quote: Quote, prefs: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = ui_lang(prefs)
    rows = [
        [btn(tr("btn_incoterms", lang, quote.incoterms or "—"), "di")],
        [btn(tr("btn_payment", lang), "dt:payment"),
         btn(tr("btn_lead", lang), "dt:lead")],
        [btn(tr("btn_warranty", lang), "dt:warranty"),
         btn(tr("btn_extra", lang), "dt:extra")],
    ]
    for line in quote.lines:
        rows.append([btn(f"🗑 {t(line.label, lang)[:26]} · "
                         f"{fmt_money(quote.line_amount(line), quote.currency, lang)}",
                         f"dd:{line.id}")])
    rows.append([btn(tr("btn_add_line", lang), "da")])
    rows.append([btn(tr("btn_back", lang), "m")])
    return kb(rows)


# --------------------------------------------------------------- действия

async def product_action(bot: Bot, chat_id: int, uid: int, quote: Quote,
                         pid: str, field: str, call: CallbackQuery) -> None:
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)

    if field == "add":
        await ask(bot, chat_id, uid, tr("ask_add_product", lang), {"kind": "add_product"})
        return

    product = quote.product(pid)
    if product is None:
        return

    if field == "add_option":
        await ask(bot, chat_id, uid, tr("ask_add_option", lang),
                  {"kind": "add_option", "pid": pid})
        return
    if field == "name":
        await ask(bot, chat_id, uid, tr("ask_name", lang), {"kind": "name", "pid": pid})
        return
    if field == "spec":
        current = html.escape(t(product.spec, lang)[:1500] or "—")
        await ask(bot, chat_id, uid, tr("ask_spec", lang, current),
                  {"kind": "spec", "pid": pid})
        return
    if field == "qty":
        await ask(bot, chat_id, uid, tr("ask_qty", lang), {"kind": "qty", "pid": pid})
        return
    if field == "price":
        await ask(bot, chat_id, uid, tr("ask_price", lang, quote.currency),
                  {"kind": "price", "pid": pid})
        return
    if field == "markup_item":
        await ask(bot, chat_id, uid, tr("ask_markup_item", lang),
                  {"kind": "markup_item", "pid": pid})
        return
    if field == "photo":
        await ask(bot, chat_id, uid, tr("ask_photo", lang), {"kind": "photo", "pid": pid})
        return

    if field == "auto":
        product.price_override = None
    elif field == "photo_del":
        product.photo = None
        if not any(p.photo for p in quote.products):
            column = quote.column("photo")
            if column:
                column.on = False
    elif field == "up":
        i = quote.products.index(product)
        if i > 0:
            quote.products[i - 1], quote.products[i] = (quote.products[i],
                                                        quote.products[i - 1])
    elif field == "del":
        quote.products = [p for p in quote.products if p.id != pid]

    save_quote(uid, quote)
    await show_card(bot, chat_id, uid)


async def option_action(bot: Bot, chat_id: int, uid: int, quote: Quote,
                        pid: str, oid: str, field: str) -> None:
    prefs = load_prefs(uid)
    lang = ui_lang(prefs)
    product = quote.product(pid)
    if product is None:
        return
    if field == "text":
        await ask(bot, chat_id, uid, tr("ask_option_text", lang),
                  {"kind": "option_text", "pid": pid, "oid": oid})
        return
    if field == "qty":
        await ask(bot, chat_id, uid, tr("ask_qty", lang),
                  {"kind": "option_qty", "pid": pid, "oid": oid})
        return
    if field == "price":
        await ask(bot, chat_id, uid, tr("ask_price", lang, quote.currency),
                  {"kind": "option_price", "pid": pid, "oid": oid})
        return
    if field == "del":
        product.options = [o for o in product.options if o.id != oid]
        save_quote(uid, quote)
        await show_card(bot, chat_id, uid)
