"""Фирменный PDF на бланке (Jinja2 → WeasyPrint) — как ex4.pdf / ex5.pdf."""
from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import TEMPLATES_DIR, Brand, settings
from ..models import BASE_ORDER, Quote, t
from . import labels as L
from .common import (photo_bytes, prepare_photo, spec_pairs)

# Длинные текстовые колонки выносим на странице позиции отдельными разделами
LONG_TEXT_CHARS = 60


def _data_uri(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _logo_uri(brand: Brand) -> str:
    path = brand.logo_path
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        return _data_uri(path.read_bytes(), mime)
    except OSError:
        return ""


def _photo_uri(photo_dir, name: str | None, max_px: int = 700) -> str:
    data = photo_bytes(photo_dir, name)
    if not data:
        return ""
    return _data_uri(prepare_photo(data, max_px=max_px))


def build_context(quote: Quote, lang: str, photo_dir=None,
                  brand: Brand | None = None) -> dict:
    brand = brand or settings.brand
    money = lambda v: L.money_str(v, quote.currency, lang)  # noqa: E731
    cur = L.currency_label(quote.currency)
    basis = f" ({quote.incoterms})" if quote.incoterms else ""

    show_no = quote.has_column("no")
    show_photo = quote.has_column("photo") and any(p.photo for p in quote.products)

    rows: list[dict] = []
    for index, product in enumerate(quote.products, 1):
        rows.append({
            "kind": "product",
            "no": index,
            "photo": _photo_uri(photo_dir, product.photo, 320) if show_photo else "",
            "descr": t(product.name, lang),
            "qty": L.qty_str(product.qty),
            "price": money(quote.price(product)),
            "total": money(quote.total(product)),
        })
        for option in product.options:
            text = t(option.text, lang)
            if not text.lower().startswith(("опция", "选配", "option")):
                text = f"{L.lb('option', lang)}: {text}"
            rows.append({
                "kind": "option", "no": "", "photo": "", "descr": text,
                "qty": L.qty_str(option.qty),
                "price": money(quote.option_price(product, option)),
                "total": money(quote.option_total(product, option)),
            })
        # денежные доп. колонки (пресс-формы и т. п.) — отдельной строкой
        for col in quote.active_columns():
            if not col.is_money or col.key in BASE_ORDER or col.merged:
                continue
            value = quote.money_value(product, col)
            if not value:
                continue
            rows.append({
                "kind": "extra", "no": "", "photo": "",
                "descr": t(col.title, lang), "qty": "",
                "price": "", "total": money(value),
            })

    for line in quote.lines:
        rows.append({
            "kind": "extra", "no": "", "photo": "",
            "descr": t(line.label, lang), "qty": "", "price": "",
            "total": money(quote.line_amount(line)),
        })

    total_line = None
    if quote.show_total or len(quote.products) == 1:
        total_line = {
            "label": L.lb("total_cur", lang).format(cur),
            "value": money(quote.grand_total()),
        }

    terms = []
    if quote.incoterms:
        terms.append({"label": L.lb("incoterms", lang), "value": quote.incoterms})
    for key in ("payment", "lead", "warranty", "extra"):
        text = t(quote.terms.get(key), lang)
        if text:
            terms.append({"label": L.lb(key, lang), "value": text})

    merged_blocks = []
    for col in quote.active_columns():
        if not col.merged:
            continue
        value = t(quote.merged.get(col.key), lang)
        if value:
            merged_blocks.append({"label": t(col.title, lang), "value": value})

    pages = []
    if quote.has_column("spec"):
        for product in quote.products:
            pages.append(_product_page(quote, product, lang, photo_dir))

    return {
        "brand": brand,
        "quote": quote,
        "lang": lang,
        "logo": _logo_uri(brand),
        "doc_title": L.lb("doc_title", lang),
        "date_str": L.date_str(quote.created, lang),
        "client_line": L.lb("for_client", lang).format(quote.client),
        "show_no": show_no,
        "show_photo": show_photo,
        "price_head": f"{L.BASE_TITLES['price'][lang]}{basis}",
        "total_head": f"{L.BASE_TITLES['total'][lang]}{basis}",
        "rows": rows,
        "total_line": total_line,
        "terms": terms,
        "merged_blocks": merged_blocks,
        "pages": pages,
        "lb": {
            "no": L.BASE_TITLES["no"][lang],
            "photo": L.BASE_TITLES["photo"][lang],
            "qty": L.lb("qty_short", lang),
            "price": L.BASE_TITLES["price"][lang],
            "total": L.BASE_TITLES["total"][lang],
            "descr": L.lb("descr_col", lang),
            "date": L.lb("date", lang),
            "description": L.lb("description", lang),
            "tech_specs": L.lb("tech_specs", lang),
            "options_h": L.lb("options_h", lang),
            "param": L.lb("param", lang),
            "value": L.lb("value", lang),
        },
    }


def _product_page(quote: Quote, product, lang: str, photo_dir) -> dict:
    money = lambda v: L.money_str(v, quote.currency, lang)  # noqa: E731
    pairs = spec_pairs(t(product.spec, lang))
    specs = [{"name": n, "value": v} for n, v in pairs if v]
    descr = "\n".join(n for n, v in pairs if not v)

    # короткие свои колонки (срок, вес, объём) — в таблицу характеристик,
    # длинные (технология, примечание) — отдельными разделами
    sections = []
    for col in quote.active_columns():
        if col.key in BASE_ORDER or col.merged or col.is_money:
            continue
        value = t(product.texts.get(col.key), lang)
        if not value:
            continue
        title = t(col.title, lang)
        if len(value) > LONG_TEXT_CHARS:
            sections.append({"label": title, "value": value})
        else:
            specs.append({"name": title, "value": value})

    return {
        "name": t(product.name, lang),
        "photo": _photo_uri(photo_dir, product.photo, 900),
        "descr": descr,
        "specs": specs,
        "options": [{
            "text": t(o.text, lang),
            "qty": L.qty_str(o.qty),
            "price": money(quote.option_price(product, o)),
            "total": money(quote.option_total(product, o)),
        } for o in product.options],
        "sections": sections,
    }


def render_html(quote: Quote, lang: str, photo_dir=None,
                brand: Brand | None = None) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("quote.html")
    return template.render(**build_context(quote, lang, photo_dir, brand))


def render_pdf(quote: Quote, lang: str, out_path: str | Path, photo_dir=None,
               brand: Brand | None = None) -> Path:
    from weasyprint import HTML  # импорт здесь: нужны системные библиотеки Pango

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(quote, lang, photo_dir, brand)
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(out_path))
    return out_path


def render_pdf_bytes(quote: Quote, lang: str, photo_dir=None,
                     brand: Brand | None = None) -> bytes:
    from weasyprint import HTML

    html = render_html(quote, lang, photo_dir, brand)
    buf = io.BytesIO()
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(buf)
    return buf.getvalue()
