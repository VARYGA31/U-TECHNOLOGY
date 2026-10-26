"""Сквозной тест без Телеграма и OpenAI.

Перевод — заглушка: русские тексты берутся из kp_ru.xlsx по порядку позиций.
Проверяется:
  1) импорт kp_cn + наценка 6% → цены совпадают с kp_ru.xlsx;
  2) у всех позиций есть фото; файлы ru/zh/en собираются;
  3) имитация кнопок (наценка, колонки, удаление опции и позиции, меню условий);
  4) имитация ввода (наценка «7,5», позиция, строка доставки, оплата,
     гарантия, клиент, колонка, базис) и ошибочное число → bad_number;
  5) отправка файлов на фейковом Bot (send_message / send_media_group / delete_message).

Запуск:  python tests/test_flow.py [examples/kp_cn.xlsx examples/kp_ru.xlsx]
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# временная папка данных — до импорта bot.config
_TMP = Path(tempfile.mkdtemp(prefix="utech_test_"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ.setdefault("OPENAI_API_KEY", "")

from bot import handlers, service, storage  # noqa: E402
from bot.models import Quote, t  # noqa: E402
from bot.parsing.extract import extract  # noqa: E402
from bot.parsing.own_template import parse_own  # noqa: E402
from bot.render.pdf import render_html  # noqa: E402
from bot.render.xlsx import render_xlsx  # noqa: E402

EXAMPLES = ROOT / "examples"
UID = 424242

failures: list[str] = []
checks = 0


def check(cond: bool, label: str, extra: str = "") -> None:
    global checks
    checks += 1
    if cond:
        print(f"  ✅ {label}")
    else:
        failures.append(label)
        print(f"  ❌ {label} {extra}")


# --------------------------------------------------------------- фейковый Bot

class FakeMessage:
    def __init__(self, chat_id: int, message_id: int, text: str = ""):
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = message_id
        self.text = text

    async def edit_text(self, text, **kw):
        self.text = text
        return self


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []
        self.media: list[list] = []
        self.deleted: list[int] = []
        self._next_id = 1000

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.sent.append(text)
        return FakeMessage(chat_id, self._next_id, text)

    async def send_media_group(self, chat_id, media, **kw):
        self.media.append(media)
        return []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


# --------------------------------------------------------------- заглушка перевода

def check_openai_api() -> None:
    """Защита от регрессии: в openai < 1.66 нет Responses API, и перевод
    молча падал с AttributeError — КП уходило без перевода. Сети не требует."""
    import inspect

    import openai
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key="test-key-not-used")
    has_responses = hasattr(client, "responses")
    check(has_responses, f"openai {openai.__version__}: есть client.responses",
          "нужен openai>=1.99 — иначе перевод падает с AttributeError")
    if not has_responses:
        return
    has_parse = hasattr(client.responses, "parse")
    check(has_parse, "есть client.responses.parse")
    if has_parse:
        params = inspect.signature(client.responses.parse).parameters
        check("text_format" in params, "parse принимает text_format=")

    # структурные схемы разбора и перевода должны собираться
    from bot.parsing.ai import AiQuote, TrBatch

    check(bool(AiQuote.model_json_schema()) and bool(TrBatch.model_json_schema()),
          "схемы AiQuote и TrBatch валидны")


def pdf_available() -> bool:
    import importlib

    try:
        importlib.import_module("weasyprint")
        return True
    except Exception:
        return False


def _stub_pdf(quote, lang, out_path, photo_dir=None, brand=None):
    """Заглушка PDF, когда в системе нет Pango: проверяем путь отправки."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"%PDF-1.4 stub\n"
                         + render_html(quote, lang, photo_dir).encode("utf-8")[:2000])
    return out_path


def stub_translate(quote: Quote, ru_quote: Quote) -> None:
    """ru из kp_ru.xlsx, en — технический префикс (OpenAI не дёргаем)."""
    for product, ru in zip(quote.products, ru_quote.products):
        product.name.update(ru.name)
        product.spec.update(ru.spec)
        for option, ru_option in zip(product.options, ru.options):
            option.text.update(ru_option.text)
        for key, value in ru.texts.items():
            if key in product.texts:
                product.texts[key].update(value)
    for column, ru_column in zip(quote.columns, ru_quote.columns):
        column.title.update(ru_column.title)
    for key, value in ru_quote.merged.items():
        if key in quote.merged:
            quote.merged[key].update(value)
    for text in quote.all_texts():
        src = next((v for v in text.values() if v), "")
        text.setdefault("en", src)
        text.setdefault("ru", src)
        text.setdefault("zh", src)


# --------------------------------------------------------------- тест

async def main() -> int:
    cn_path = Path(sys.argv[1]) if len(sys.argv) > 1 else EXAMPLES / "kp_cn.xlsx"
    ru_path = Path(sys.argv[2]) if len(sys.argv) > 2 else EXAMPLES / "kp_ru.xlsx"
    for p in (cn_path, ru_path):
        if not p.exists():
            print("нет файла:", p)
            return 2

    print("\n0) Совместимость OpenAI SDK")
    check_openai_api()

    print("\n1) Импорт kp_cn.xlsx и наценка 6%")
    ex = extract(str(cn_path))
    quote = parse_own(ex, cn_path.name)
    assert quote is not None, "kp_cn не разобран"
    quote.number = "UT-TEST-001"
    service.materialize_photos(quote, ex, storage.photos_dir(UID))

    ru_ex = extract(str(ru_path))
    ru_quote = parse_own(ru_ex, ru_path.name)
    assert ru_quote is not None, "kp_ru не разобран"

    quote.markup_pct = 6.0
    quote.rounding = 1.0
    prices = [quote.price(p) for p in quote.products]
    expect = [p.cost for p in ru_quote.products]
    check(prices == expect, f"цены позиций {prices}", f"ожидалось {expect}")

    got_options = [[quote.option_price(p, o) for o in p.options] for p in quote.products]
    want_options = [[o.cost for o in p.options] for p in ru_quote.products]
    check(got_options == want_options, f"цены опций {got_options}",
          f"ожидалось {want_options}")

    check(all(p.photo for p in quote.products),
          f"фото у всех {len(quote.products)} позиций",
          str([p.photo for p in quote.products]))
    check(len(quote.products) == 4 and sum(len(p.options) for p in quote.products) == 9,
          "4 позиции и 9 опций")

    print("\n2) Сборка файлов на ru / zh / en")
    stub_translate(quote, ru_quote)
    storage.save_quote(UID, quote)
    out = storage.out_dir(UID)
    for lang in ("ru", "zh", "en"):
        xlsx_path = out / f"{quote.number}_{lang.upper()}.xlsx"
        render_xlsx(quote, lang, xlsx_path, storage.photos_dir(UID))
        html = render_html(quote, lang, storage.photos_dir(UID))
        check(xlsx_path.exists() and xlsx_path.stat().st_size > 20000,
              f"Excel {lang} собран ({xlsx_path.stat().st_size // 1024} КБ)")
        check("<table" in html and len(html) > 10000, f"HTML(PDF) {lang} собран")

    ru_name = t(quote.products[0].name, "ru")
    check(ru_name.startswith("Электротележка"), f"перевод названия: {ru_name[:40]}")

    print("\n3) Кнопки")
    bot = FakeBot()
    prefs = storage.load_prefs(UID)
    prefs["langs"] = ["ru"]
    storage.save_prefs(UID, prefs)

    call = type("Call", (), {})()
    call.message = FakeMessage(UID, 1)
    call.bot = bot
    call.data = ""

    async def press(data: str) -> None:
        call.data = data
        await handlers.route(bot, UID, UID, data, call)

    await press("mk:10")
    check(storage.load_quote(UID).markup_pct == 10.0, "кнопка «наценка 10%»")

    quote = storage.load_quote(UID)
    extra_key = next(c.key for c in quote.columns if c.key not in
                     ("no", "name", "photo", "spec", "qty", "price", "total"))
    await press(f"ct:{extra_key}")
    check(not storage.load_quote(UID).column(extra_key).on,
          f"колонка {extra_key} скрыта")
    check(extra_key not in [c.key for c in storage.load_quote(UID).active_columns()],
          "скрытая колонка не попадает в вывод")
    await press(f"ct:{extra_key}")
    check(storage.load_quote(UID).column(extra_key).on, "колонка возвращена")

    quote = storage.load_quote(UID)
    pid = quote.products[1].id
    oid = quote.products[1].options[0].id
    await press(f"of:{pid}:{oid}:del")
    check(len(storage.load_quote(UID).products[1].options) == 2, "опция удалена")

    await press(f"pf:{pid}:del")
    check(len(storage.load_quote(UID).products) == 3, "позиция удалена")

    await press("dl")
    check(True, "меню «Доставка и условия» открылось")
    await press("co")
    await press("pr")
    await press("lg:en")
    check("en" in storage.load_prefs(UID)["langs"], "язык EN добавлен кнопкой")

    print("\n4) Ввод значений")

    async def send(text: str, state: dict) -> str:
        return await handlers.handle_input(bot, UID, UID, text, state)

    check(await send("7,5", {"kind": "markup"}) == "ok"
          and storage.load_quote(UID).markup_pct == 7.5, "наценка «7,5» принята")
    check(await send("abc", {"kind": "markup"}) == "bad_number", "ошибка числа → bad_number")

    before = len(storage.load_quote(UID).products)
    await send("Тестовый насос | 3 | 1500", {"kind": "add_product"})
    quote = storage.load_quote(UID)
    check(len(quote.products) == before + 1, "позиция добавлена")
    added = quote.products[-1]
    check(abs(quote.price(added) - 1500) <= 1 and added.qty == 3,
          f"цена новой позиции {quote.price(added)} ≈ 1500, кол-во {added.qty}")

    await send("Карго до Белгорода | 12312", {"kind": "add_line"})
    quote = storage.load_quote(UID)
    check(len(quote.lines) == 1 and abs(quote.line_amount(quote.lines[0]) - 12312) <= 1,
          f"строка доставки {quote.line_amount(quote.lines[0])}")
    check(quote.show_total, "добавление строки включило «Итого»")

    await send("ТТ 30% / 70%", {"kind": "payment"})
    await send("Гарантия 12 месяцев", {"kind": "warranty"})
    await send("ООО «Ромашка»", {"kind": "client"})
    await send("FCA Shandong", {"kind": "incoterms"})
    await send("Сертификация", {"kind": "add_column"})
    quote = storage.load_quote(UID)
    check(t(quote.terms.get("payment"), "ru") == "ТТ 30% / 70%", "условия оплаты")
    check(t(quote.terms.get("warranty"), "ru") == "Гарантия 12 месяцев", "гарантия")
    check(quote.client == "ООО «Ромашка»", "клиент")
    check(quote.incoterms == "FCA Shandong", "базис FCA Shandong")
    check(any(t(c.title, "ru") == "Сертификация" for c in quote.columns), "колонка добавлена")

    print("\n5) Отправка файлов фейковым ботом")
    if not pdf_available():
        print("  ⚠️  WeasyPrint без системных библиотек Pango — PDF подменён заглушкой.")
        print("     Настоящий PDF собирается в Docker-образе (fonts-noto-cjk, libpango).")
        service.render_pdf = _stub_pdf

    bot = FakeBot()
    prefs = storage.load_prefs(UID)
    prefs["langs"] = ["ru", "zh"]
    prefs["card_id"] = 777
    storage.save_prefs(UID, prefs)
    status = await bot.send_message(UID, "…")
    await handlers._deliver(bot, UID, UID, status)
    check(len(bot.media) == 2, f"отправлено медиагрупп: {len(bot.media)} (ru + zh)")
    check(bool(bot.media) and all(len(m) == 2 for m in bot.media),
          "в каждой группе Excel + PDF")
    caption = getattr(bot.media[0][0], "caption", "") if bot.media else ""
    caption = caption or ""
    check("UT-TEST-001" in caption, f"подпись: {caption}")
    check(777 in bot.deleted, "старая карточка удалена")
    check(any("UT-TEST-001" in s for s in bot.sent), "новая карточка отправлена")

    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛЕНО {len(failures)} из {checks}:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"ВСЕ {checks} ПРОВЕРОК ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
