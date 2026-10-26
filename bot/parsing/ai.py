"""OpenAI: разбор КП заводов любого вида и перевод."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from ..config import settings
from ..models import (Column, Line, Option, Product, Quote, Text, detect_lang,
                      has_lang, mk)

log = logging.getLogger(__name__)

CHUNK_CHARS = 9000
MAX_TRANSLATE_RETRIES = 2

# Картинки в основном разборе — только когда текста нет совсем (скан).
# Листы каталога разбираются отдельными мелкими запросами: один большой
# запрос с полутора десятком картинок думается минутами.
MAX_SCAN_IMAGES = 6
MAX_PHOTO_MATCH = 16

# Таймауты: без них openai ждёт 600 с и делает ещё две попытки — бот «висит»
# больше получаса, а партнёр видит только «⏳ Распознаю…».
PARSE_TIMEOUT = 150.0
SMALL_TIMEOUT = 60.0

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=PARSE_TIMEOUT,
            max_retries=1,
        )
    return _client


def _tuning() -> dict:
    """gpt-5 и o-серия — рассуждающие модели: без ограничения усилий они
    обдумывают разбор таблицы минутами. Для извлечения данных хватает low."""
    model = (settings.openai_model or "").lower()
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"reasoning": {"effort": "low"}}
    return {}


# --------------------------------------------------------------- схема разбора

class AiColumn(BaseModel):
    title: str = Field(description="Заголовок колонки ровно как в исходнике")
    kind: Literal["text", "money"]
    whole_quote: bool = Field(
        description="true, если у колонки одно значение на всё КП (ячейка объединена на все позиции)")


class AiWholeValue(BaseModel):
    column: str
    text: str


class AiValue(BaseModel):
    column: str = Field(description="Заголовок колонки из списка columns")
    text: str = Field(description="Текстовое значение, если колонка текстовая, иначе пусто")
    amount: float = Field(description="Число, если колонка денежная, иначе 0")


class AiOption(BaseModel):
    text: str
    qty: float
    unit_price: float


class AiProduct(BaseModel):
    name: str
    spec: str = Field(description="Параметры, каждый с новой строки в виде «Параметр: значение»")
    photo: int = Field(description="Номер из метки [ФОТО #n] или 0, если фото нет")
    qty: float
    unit_price: float = Field(description="Цена за единицу")
    values: list[AiValue]
    options: list[AiOption]


class AiLine(BaseModel):
    label: str
    amount: float


class AiQuote(BaseModel):
    currency: Literal["CNY", "USD", "RUB", "EUR"]
    incoterms: str = Field(description="Базис поставки с местом, например «FCA Shandong», иначе пусто")
    factory_name: str
    columns: list[AiColumn]
    whole_quote_values: list[AiWholeValue]
    products: list[AiProduct]
    lines: list[AiLine]
    has_total: bool
    payment_terms: str
    lead_time: str
    warranty: str


PARSE_PROMPT = """Ты разбираешь коммерческое предложение (КП) завода в структурированные данные.

Правила:
- Все тексты переноси на языке исходника, без перевода и без сокращений.
- Параметры позиции (spec) — каждый с новой строки в формате «Параметр: значение».
- Варианты с отдельной ценой (选配, другая мачта, другая АКБ, другой размер) — это ОПЦИИ позиции,
  а не отдельные позиции.
- «Итого», «Всего», налоги, скидки — НЕ позиции.
- Доставку, образцы, прочие расходы отдельной строкой под таблицей — в lines.
- unit_price — цена за ЕДИНИЦУ товара, не сумма.
- Логотипы, печати, подписи — НЕ фото товара; photo = 0.
- Фото привязывай по метке [ФОТО #n], стоящей рядом с позицией в тексте.
  Если метки идут подряд в конце (это листы каталога без текста) — ставь
  photo = 0, фото к ним подберут отдельно.
- Прайс-лист, где каждая строка таблицы — отдельная модель с ценой,
  разбирай по строкам: одна строка = одна позиция. Колонки конфигурации
  со значками ● (标配, в базе) и ○ (选配, опция) переноси в spec строками
  вида «Название колонки: ● в базе» / «Название колонки: ○ опция».
- Сохрани ВСЕ прочие колонки исходника (срок, вес, объём, доставка, технология,
  пресс-формы, примечание, сведения о заводе) как columns с их заголовками.
- Если значение колонки одно на всё КП (объединённая ячейка) — whole_quote = true
  и значение в whole_quote_values.
- has_total = true, если под таблицей есть строка «Итого/Total/合计».
"""


def _image_part(data: bytes) -> dict:
    b64 = base64.b64encode(data).decode()
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}


async def parse_quote_ai(text: str, images: list[tuple[int, bytes]] | None = None,
                         source_file: str = "") -> Quote | None:
    """Разбор КП завода любого вида.

    Картинки сюда идут только если текста нет совсем (скан или фото таблицы).
    Листы каталога к позициям подбирает match_photos_ai — отдельными мелкими
    запросами, иначе один запрос с 16 картинками думается минутами.
    """
    has_text = bool(text.strip())
    content: list[dict] = []
    if has_text:
        content.append({"type": "input_text", "text": text[:180000]})
    else:
        for _mark, data in (images or [])[:MAX_SCAN_IMAGES]:
            content.append(_image_part(data))
    if not content:
        return None

    started = time.monotonic()
    log.info("ИИ-разбор: %d симв текста, %d картинок, модель %s",
             len(text), 0 if has_text else len(images or []), settings.openai_model)
    resp = await client().responses.parse(
        model=settings.openai_model,
        input=[{"role": "system", "content": PARSE_PROMPT},
               {"role": "user", "content": content}],
        text_format=AiQuote,
        **_tuning(),
    )
    log.info("ИИ-разбор занял %.1f с", time.monotonic() - started)
    ai = resp.output_parsed
    if ai is None or not ai.products:
        return None
    return _to_quote(ai, source_file)


# --------------------------------------------------------- подбор фото к позициям

class AiPhotoMatch(BaseModel):
    product_index: int = Field(
        description="Номер позиции из списка (с 1), к которой относится лист. 0 — ни к одной")
    model_name: str = Field(description="Модель или название, написанное на листе")


PHOTO_PROMPT = """На картинке — лист каталога оборудования.
Определи, какой позиции из списка он соответствует: сравни модель/название
на листе с названиями позиций. Отвечай номером позиции из списка.
Если лист общий (обложка, содержание, реклама) — верни 0."""


async def _match_one(mark: int, data: bytes, names: str) -> tuple[int, int]:
    try:
        resp = await client().responses.parse(
            model=settings.openai_model,
            input=[{"role": "system", "content": PHOTO_PROMPT},
                   {"role": "user", "content": [
                       {"type": "input_text", "text": f"Позиции КП:\n{names}"},
                       _image_part(data)]}],
            text_format=AiPhotoMatch,
            timeout=SMALL_TIMEOUT,
            **_tuning(),
        )
    except Exception as exc:
        log.warning("подбор фото для [ФОТО #%s] не удался: %s", mark, exc)
        return mark, 0
    parsed = resp.output_parsed
    return mark, (parsed.product_index if parsed else 0)


async def match_photos_ai(quote: Quote, images: list[tuple[int, bytes]]) -> int:
    """Привязывает листы каталога к позициям. Возвращает число привязанных фото."""
    pages = [(m, d) for m, d in images if m][:MAX_PHOTO_MATCH]
    if not pages or not quote.products:
        return 0
    names = "\n".join(
        f"{i}. {next(iter(p.name.values()), '')}"
        for i, p in enumerate(quote.products, 1))

    started = time.monotonic()
    results = await asyncio.gather(
        *(_match_one(m, d, names) for m, d in pages), return_exceptions=True)
    matched = 0
    for item in results:
        if not isinstance(item, tuple):
            continue
        mark, index = item
        if 1 <= index <= len(quote.products):
            product = quote.products[index - 1]
            if not product.photo:          # найденное в тексте не перебиваем
                product.photo = f"p{mark}.img"
                matched += 1
    log.info("подбор фото: %d листов, привязано %d, %.1f с",
             len(pages), matched, time.monotonic() - started)
    return matched


def _to_quote(ai: AiQuote, source_file: str) -> Quote:
    quote = Quote(
        source_file=source_file,
        factory_name=ai.factory_name or "",
        source_currency=ai.currency,
        currency=ai.currency,
        incoterms=ai.incoterms or "",
        rate=1.0,
        show_total=ai.has_total,
    )

    # свои колонки: заголовок → ключ
    key_by_title: dict[str, str] = {}
    for i, col in enumerate(ai.columns, 1):
        title = (col.title or "").strip()
        if not title:
            continue
        key = f"c{i}"
        key_by_title[title] = key
        quote.columns.append(Column(
            key=key,
            kind=col.kind,
            title=mk(title),
            merged=col.whole_quote,
            markup=False,
            in_total=col.kind == "money",
        ))

    for wv in ai.whole_quote_values:
        key = key_by_title.get((wv.column or "").strip())
        if key and wv.text.strip():
            quote.merged[key] = mk(wv.text.strip())

    for p in ai.products:
        product = Product(
            name=mk(p.name.strip()),
            spec=mk(p.spec.strip()),
            qty=p.qty or 1,
            cost=p.unit_price or 0.0,
            photo=f"p{p.photo}.img" if p.photo else None,
        )
        for v in p.values:
            key = key_by_title.get((v.column or "").strip())
            if not key:
                continue
            col = next((c for c in quote.columns if c.key == key), None)
            if col and col.merged:
                continue
            if col and col.is_money:
                if v.amount:
                    product.money[key] = v.amount
            elif v.text.strip():
                product.texts[key] = mk(v.text.strip())
        for o in p.options:
            product.options.append(Option(
                text=mk(o.text.strip()),
                qty=o.qty or 1,
                cost=o.unit_price or 0.0,
            ))
        quote.products.append(product)

    for ln in ai.lines:
        if ln.label.strip():
            quote.lines.append(Line(label=mk(ln.label.strip()), amount=ln.amount or 0.0))
    if quote.lines:
        quote.show_total = True

    for key, value in (("payment", ai.payment_terms), ("lead", ai.lead_time),
                       ("warranty", ai.warranty)):
        if value and value.strip():
            quote.terms[key] = mk(value.strip())

    blob = " ".join(next(iter(p.name.values()), "") for p in quote.products)
    quote.src_lang = detect_lang(blob)
    quote.lang = quote.src_lang
    quote.ensure_columns()
    return quote


# --------------------------------------------------------------- перевод

class TrItem(BaseModel):
    key: str
    text: str


class TrBatch(BaseModel):
    items: list[TrItem]


GLOSSARY = {
    "ru": """Глоссарий (китайский → русский):
选配 → Опция; 门架 → мачта; 电瓶/蓄电池 → АКБ; 锂电池 → литиевая АКБ;
铅酸电池 → свинцово-кислотная АКБ; 搬运车 → электротележка (паллетная);
堆垛车 → штабелёр; 步行式 → сопровождаемый (оператор идёт рядом);
站驾式 → с платформой оператора; 货叉 → вилы; 聚氨酯 → полиуретан;
HS CODE → Код ТН ВЭД (HS CODE).
Единицы: кг, мм, км/ч, кВт, В, Ач, дБ(A). Десятичный разделитель — запятая.
Пример: «选配24v/40AH锂电池» → «Опция: литиевая АКБ 24 В / 40 Ач».""",
    "zh": """术语表（俄语/英语 → 中文）：
Опция/Option → 选配；мачта/mast → 门架；АКБ/battery → 电瓶；
литиевая АКБ → 锂电池；свинцово-кислотная АКБ → 铅酸电池；
электротележка → 搬运车；штабелёр/stacker → 堆垛车；
сопровождаемый/pedestrian → 步行式；с платформой оператора → 站驾式；
вилы/forks → 货叉；полиуретан → 聚氨酯。小数点用「.」。""",
    "en": """Glossary (Chinese/Russian → English):
选配/Опция → Option; 门架/мачта → mast; 电瓶/АКБ → battery;
锂电池 → lithium battery; 铅酸电池 → lead-acid battery;
搬运车 → electric pallet truck; 堆垛车/штабелёр → stacker;
步行式 → pedestrian; 站驾式 → stand-on platform; 货叉 → forks;
聚氨酯 → polyurethane. Units: kg, mm, km/h, kW, V, Ah, dB(A). Decimal point is «.».""",
}

LANG_NAME = {"ru": "русский", "zh": "中文 (упрощённый)", "en": "English"}


def _tr_prompt(lang: str) -> str:
    return f"""Переведи значения на {LANG_NAME.get(lang, lang)}.

Ответ — items: для каждого входного ключа один объект {{key, text}}.
Правила:
- Сохраняй разбиение на строки и формат «Параметр: значение».
- НЕ переводи модели, артикулы и обозначения: Q, h3, b11, Ast, Wa, S2, S3,
  SL15, CL1530E-B, HS CODE-номера и подобные.
- Числа и коды без букв оставляй как есть.
- Это технический документ — деловой стиль, без пояснений и комментариев.
{GLOSSARY.get(lang, GLOSSARY['en'])}"""


async def _translate_chunk(items: dict[str, str], lang: str) -> dict[str, str]:
    resp = await client().responses.parse(
        model=settings.openai_model,
        input=[{"role": "system", "content": _tr_prompt(lang)},
               {"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
        text_format=TrBatch,
        **_tuning(),
    )
    parsed = resp.output_parsed
    if parsed is None:
        return {}
    return {it.key: it.text for it in parsed.items if it.text}


def _chunks(items: dict[str, str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    size = 0
    for key, value in items.items():
        if cur and size + len(value) > CHUNK_CHARS:
            out.append(cur)
            cur, size = {}, 0
        cur[key] = value
        size += len(value)
    if cur:
        out.append(cur)
    return out


def _needs_translation(text: Text, lang: str) -> bool:
    if not text or has_lang(text, lang):
        return False
    src = next((v for v in text.values() if v), "")
    if not src.strip():
        return False
    # числа и коды без букв не переводим
    return any(ch.isalpha() for ch in src)


async def translate_quote(quote: Quote, lang: str) -> None:
    """Добавляет перевод на lang во все тексты, где его нет. Кэш — в самой модели."""
    if not settings.ai_enabled:
        return
    texts = [x for x in quote.all_texts() if _needs_translation(x, lang)]
    if not texts:
        return

    by_key: dict[str, Text] = {}
    payload: dict[str, str] = {}
    for i, text in enumerate(texts):
        key = f"t{i}"
        by_key[key] = text
        payload[key] = next(v for v in text.values() if v)

    pending = payload
    for _attempt in range(MAX_TRANSLATE_RETRIES + 1):
        if not pending:
            break
        results = await asyncio.gather(
            *(_translate_chunk(c, lang) for c in _chunks(pending)),
            return_exceptions=True,
        )
        got: dict[str, str] = {}
        for r in results:
            if isinstance(r, dict):
                got.update(r)
        for key, value in got.items():
            if key in by_key and value.strip():
                by_key[key][lang] = value.strip()
        pending = {k: v for k, v in pending.items() if k not in got}
