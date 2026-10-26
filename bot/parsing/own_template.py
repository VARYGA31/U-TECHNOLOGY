"""Точный разбор КП в формате U-Technology — без ИИ.

Если в первых 30 строках есть строка-шапка с «Наименование» и «Стоимость/Цена»
и хотя бы одной из «Параметры / ФОТО / Кол-во», таблица разбирается
детерминированно: позиции, опции, фото, свои колонки, строки под таблицей.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Column, Line, Option, Product, Quote, detect_lang, mk, parse_number
from .extract import Extracted

MAX_HEADER_ROW = 30

# Ключевые слова заголовков на ru/zh/en
W_NO = {"no", "no.", "№", "n", "#", "序号", "编号"}
W_NAME = {"наименование", "название", "продукция", "товар", "product", "item",
          "产品名称", "品名", "名称", "产品"}
W_PHOTO = {"фото", "изображение", "photo", "picture", "image", "图片", "照片", "图"}
W_SPEC = {"параметры", "параметр", "характеристики", "технические характеристики",
          "spec", "specs", "specification", "specifications", "参数", "规格", "技术参数"}
W_QTY = {"кол-во", "колво", "количество", "qty", "quantity", "数量", "台数"}
W_PRICE = {"стоимость", "цена", "price", "unit price", "单价", "价格", "单位价格"}
W_TOTAL = {"общ.стоимость", "общая стоимость", "общ стоимость", "сумма", "итого",
           "total", "amount", "总价", "金额", "合计", "总金额", "总计"}

# Слово «стоимость/цена» в заголовке своей колонки → колонка денежная
MONEY_HINT = ("стоимость", "цена", "费", "价", "金额", "cost", "price", "доставк", "расход")

TOTAL_ROW_WORDS = ("итого", "всего", "total", "合计", "总计", "总额")

INCOTERMS = ("EXW", "FCA", "FOB", "CIF", "CFR", "CPT", "CIP", "DAP", "DDP", "DPU", "FAS")

CURRENCY_BY_TOKEN = [
    (("rmb", "cny", "¥", "￥", "元", "юан", "yuan"), "CNY"),
    (("usd", "$", "доллар", "dollar", "美元"), "USD"),
    (("rub", "₽", "руб", "卢布"), "RUB"),
    (("eur", "€", "евро", "euro", "欧元"), "EUR"),
]


def _first_line(s: str) -> str:
    return (s or "").split("\n")[0].strip()


def _strip_brackets(s: str) -> str:
    return re.sub(r"[(（\[][^)）\]]*[)）\]]", " ", s or "")


def _norm(s: str) -> str:
    """Заголовок → сравнимый вид: без скобок, базиса, валюты и знаков."""
    s = _strip_brackets(_first_line(s)).lower()
    for inc in INCOTERMS:
        s = re.sub(rf"\b{inc.lower()}\b", " ", s)
    for tokens, _cur in CURRENCY_BY_TOKEN:
        for tok in tokens:
            s = s.replace(tok, " ")
    s = re.sub(r"[\s,;:/\\\-–—_*]+", " ", s)
    return s.strip(" .。、")


def _cell_text(ws, row: int, col: int) -> str:
    v = ws.cell(row=row, column=col).value
    return "" if v is None else str(v).strip()


@dataclass
class _Header:
    row: int
    mapping: dict[int, str]       # номер колонки (с 1) → ключ
    titles: dict[int, str]        # номер колонки → исходный заголовок
    first_col: int
    last_col: int


def _classify(title: str) -> str | None:
    n = _norm(title)
    if not n:
        return None
    # «Кол-во» после нормализации становится «кол во» — сверяем и слитный вариант
    variants = {n, n.replace(" ", ""), n.replace(".", " ").strip(), n.replace(".", "")}
    for words, key in ((W_TOTAL, "total"), (W_PRICE, "price"), (W_NO, "no"),
                       (W_NAME, "name"), (W_PHOTO, "photo"), (W_SPEC, "spec"),
                       (W_QTY, "qty")):
        normalized = {w.replace("-", " ").replace("-", "") for w in words} | words
        if variants & normalized:
            return key
    return None


def _find_header(ws) -> _Header | None:
    for row in range(1, min(ws.max_row, MAX_HEADER_ROW) + 1):
        mapping: dict[int, str] = {}
        titles: dict[int, str] = {}
        for col in range(1, ws.max_column + 1):
            text = _cell_text(ws, row, col)
            if not text:
                continue
            titles[col] = text
            key = _classify(text)
            if key and key not in mapping.values():
                mapping[col] = key
        keys = set(mapping.values())
        if "name" not in keys:
            continue
        if not ({"price", "total"} & keys):
            continue
        if not ({"spec", "photo", "qty"} & keys):
            continue
        cols = sorted(titles)
        return _Header(row, mapping, titles, cols[0], cols[-1])
    return None


def _currency_and_incoterms(ws, header: _Header) -> tuple[str, str]:
    currency, incoterms = "", ""
    price_cols = [c for c, k in header.mapping.items() if k in ("price", "total")]
    blobs = [header.titles.get(c, "") for c in price_cols]
    for col in price_cols:
        cell = ws.cell(row=header.row + 1, column=col)
        blobs.append(str(cell.number_format or ""))
    for blob in blobs:
        low = blob.lower()
        if not currency:
            for tokens, cur in CURRENCY_BY_TOKEN:
                if any(tok in low for tok in tokens):
                    currency = cur
                    break
        if not incoterms:
            for inc in INCOTERMS:
                m = re.search(rf"\b{inc}\b([ \t]+[A-Za-zА-Яа-я]+)?", blob, re.IGNORECASE)
                if m:
                    incoterms = m.group(0).strip()
                    break
    return currency or "CNY", incoterms


def _merge_map(ws) -> dict[tuple[int, int], tuple[int, int]]:
    """(row, col) начала объединения → (max_row, max_col)."""
    out = {}
    for rng in ws.merged_cells.ranges:
        out[(rng.min_row, rng.min_col)] = (rng.max_row, rng.max_col)
    return out


def looks_like_own_format(ex: Extracted) -> bool:
    if ex.kind not in ("xlsx", "xlsm") or ex.workbook is None:
        return False
    for ws in ex.workbook.worksheets:
        if _find_header(ws) is not None:
            return True
    return False


def parse_own(ex: Extracted, source_file: str = "") -> Quote | None:
    if ex.workbook is None:
        return None
    for ws in ex.workbook.worksheets:
        header = _find_header(ws)
        if header is None:
            continue
        quote = _parse_sheet(ws, header, ex, source_file)
        if quote and quote.products:
            return quote
    return None


def _parse_sheet(ws, header: _Header, ex: Extracted, source_file: str) -> Quote | None:
    merges = _merge_map(ws)
    col_of = {k: c for c, k in header.mapping.items()}
    no_col = col_of.get("no")
    name_col = col_of.get("name")
    spec_col = col_of.get("spec")
    qty_col = col_of.get("qty")
    price_col = col_of.get("price")
    total_col = col_of.get("total")
    currency, incoterms = _currency_and_incoterms(ws, header)

    extra_cols = [c for c in sorted(header.titles)
                  if c not in header.mapping and header.titles.get(c)]

    # --- разметка строк: позиции / опции / строки под таблицей ---
    @dataclass
    class _Row:
        row: int
        kind: str  # product | option | line | skip
        label: str = ""
        amount: float | None = None

    def num(row: int, col: int | None) -> float | None:
        if not col:
            return None
        return parse_number(ws.cell(row=row, column=col).value)

    rows: list[_Row] = []
    last_row = ws.max_row
    cur_product_row: int | None = None
    spec_merge_end: int = 0
    table_closed = False

    for r in range(header.row + 1, last_row + 1):
        name_txt = _cell_text(ws, r, name_col) if name_col else ""
        price_v = num(r, price_col)
        total_v = num(r, total_col)
        qty_v = num(r, qty_col)
        no_v = num(r, no_col) if no_col else None

        is_line = False
        if name_txt:
            low = name_txt.lower().strip(" :：")
            if any(w in low for w in TOTAL_ROW_WORDS) and price_v is None:
                is_line = True
            elif total_v is not None and price_v is None and qty_v is None and no_v is None:
                is_line = True
        if is_line:
            table_closed = True
            rows.append(_Row(r, "line", name_txt, total_v))
            cur_product_row = None
            continue
        if table_closed:
            continue

        if no_col is not None:
            starts = no_v is not None
        else:
            starts = bool(name_txt) and price_v is not None
        if starts:
            cur_product_row = r
            spec_merge_end = merges.get((r, spec_col), (r, 0))[0] if spec_col else r
            rows.append(_Row(r, "product"))
            continue

        if cur_product_row is not None and price_v is not None and r > spec_merge_end:
            text = _cell_text(ws, r, spec_col) if spec_col else ""
            if not text and name_col:
                text = _cell_text(ws, r, name_col)
            rows.append(_Row(r, "option", text))
            continue

        rows.append(_Row(r, "skip"))

    product_rows = [x.row for x in rows if x.kind == "product"]
    if not product_rows:
        return None

    def span_of(idx: int) -> tuple[int, int]:
        start = product_rows[idx]
        if idx + 1 < len(product_rows):
            end = product_rows[idx + 1] - 1
        else:
            end = max((x.row for x in rows if x.kind in ("product", "option", "skip")),
                      default=start)
            for x in rows:
                if x.kind == "line" and x.row > start:
                    end = min(end, x.row - 1)
                    break
        return start, max(start, end)

    # --- фото: самая крупная картинка в строках позиции (с запасом на строку вверх) ---
    def photo_for(start: int, end: int) -> int | None:
        best, best_area = None, 0.0
        for a in ex.anchors:
            if a.sheet != ws.title:
                continue
            row1 = a.row + 1  # якорь с нуля
            if start - 1 <= row1 <= end and a.area >= best_area:
                best, best_area = a.n, a.area
        return best

    quote = Quote(
        source_file=source_file,
        source_currency=currency,
        currency=currency,
        incoterms=incoterms,
        rate=1.0,
        markup_pct=0.0,
    )

    # --- колонки ---
    columns: list[Column] = []
    for col in sorted(header.titles):
        key = header.mapping.get(col)
        title = header.titles[col]
        if key:
            columns.append(Column(key=key, kind="money" if key in ("price", "total") else "text",
                                  title=mk(title.strip()) if key not in ("price", "total") else {},
                                  on=True))
        elif col in extra_cols:
            columns.append(Column(key=f"c{col}", kind="text", title=mk(title.strip()), on=True))
    # базовым денежным колонкам заголовок выставит ensure_columns/рендер
    for c in columns:
        if c.key in ("price", "total"):
            c.title = {}
    quote.columns = columns

    # --- позиции ---
    extra_values: dict[str, list[tuple[int, str]]] = {}
    for idx, start in enumerate(product_rows):
        start, end = span_of(idx)
        name_txt = _cell_text(ws, start, name_col) if name_col else ""
        spec_txt = _cell_text(ws, start, spec_col) if spec_col else ""
        qty_v = num(start, qty_col) or 1
        price_v = num(start, price_col) or 0.0

        product = Product(
            name=mk(name_txt),
            spec=mk(spec_txt),
            qty=qty_v,
            cost=price_v,
        )
        n = photo_for(start, end)
        if n:
            product.photo = f"p{n}.img"

        for col in extra_cols:
            key = f"c{col}"
            raw = _cell_text(ws, start, col)
            if not raw:
                continue
            span = merges.get((start, col))
            if span and span[0] > end:
                # объединено вниз через несколько позиций → одно значение на всё КП
                quote.merged.setdefault(key, mk(raw))
                column = next((c for c in quote.columns if c.key == key), None)
                if column:
                    column.merged = True
                continue
            extra_values.setdefault(key, []).append((len(quote.products), raw))

        for x in rows:
            if x.kind == "option" and start < x.row <= end:
                product.options.append(Option(
                    text=mk(x.label),
                    qty=num(x.row, qty_col) or 1,
                    cost=num(x.row, price_col) or 0.0,
                ))
        quote.products.append(product)

    # --- свои колонки: текст или деньги ---
    for key, values in extra_values.items():
        column = next((c for c in quote.columns if c.key == key), None)
        if column is None or column.merged:
            continue
        title = next(iter(column.title.values()), "").lower()
        nums = [parse_number(v) for _i, v in values]
        money = (any(h in title for h in MONEY_HINT)
                 and bool(nums) and all(n is not None for n in nums))
        if money:
            column.kind = "money"
            column.markup = False
            column.in_total = True
            for i, (pi, v) in enumerate(values):
                quote.products[pi].money[key] = nums[i] or 0.0
        else:
            for pi, v in values:
                quote.products[pi].texts[key] = mk(v)

    # --- строки под таблицей ---
    for x in rows:
        if x.kind != "line":
            continue
        low = x.label.lower().strip(" :：")
        if any(w in low for w in TOTAL_ROW_WORDS):
            quote.show_total = True
            continue
        if x.amount is None:
            continue
        quote.lines.append(Line(label=mk(x.label.strip()), amount=x.amount))
    if quote.lines:
        quote.show_total = True

    # язык исходника — по названиям позиций
    blob = " ".join(next(iter(p.name.values()), "") for p in quote.products)
    quote.src_lang = detect_lang(blob)
    quote.lang = quote.src_lang
    quote.ensure_columns()
    return quote
