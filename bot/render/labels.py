"""Подписи документа на ru/zh/en."""
from __future__ import annotations

from datetime import date

# Заголовки базовых колонок — как в образце kp_ru.xlsx / kp_cn.xlsx
BASE_TITLES: dict[str, dict[str, str]] = {
    "no": {"ru": "№", "zh": "序号", "en": "No."},
    "name": {"ru": "Наименование", "zh": "产品名称", "en": "Product"},
    "photo": {"ru": "ФОТО", "zh": "图片", "en": "Photo"},
    "spec": {"ru": "Параметры", "zh": "参数", "en": "Specifications"},
    "qty": {"ru": "Кол-во", "zh": "数量", "en": "Qty"},
    "price": {"ru": "Стоимость", "zh": "单价", "en": "Unit price"},
    "total": {"ru": "Общ.стоимость", "zh": "总价", "en": "Total"},
}

L: dict[str, dict[str, str]] = {
    "doc_title": {"ru": "Коммерческое предложение", "zh": "报价单", "en": "Commercial Offer"},
    "for_client": {"ru": "Для: {}", "zh": "致：{}", "en": "To: {}"},
    "date": {"ru": "Дата", "zh": "日期", "en": "Date"},
    "option": {"ru": "Опция", "zh": "选配", "en": "Option"},
    "subtotal": {"ru": "Итого:", "zh": "合计：", "en": "Subtotal:"},
    "grand_total": {"ru": "Total:", "zh": "总计：", "en": "Total:"},
    "total_cur": {"ru": "Итого {}:", "zh": "合计 {}：", "en": "Total {}:"},
    "pcs": {"ru": "шт", "zh": "台", "en": "pcs"},
    "payment": {"ru": "Условия оплаты:", "zh": "付款条件：", "en": "Payment terms:"},
    "lead": {"ru": "Срок производства оборудования:", "zh": "设备生产周期：", "en": "Lead time:"},
    "warranty": {"ru": "Гарантийные обязательства:", "zh": "质保条款：", "en": "Warranty:"},
    "extra": {"ru": "Дополнительно:", "zh": "补充说明：", "en": "Additional:"},
    "incoterms": {"ru": "Базис поставки:", "zh": "贸易条款：", "en": "Incoterms:"},
    "description": {"ru": "1. Описание:", "zh": "1. 产品说明：", "en": "1. Description:"},
    "tech_specs": {"ru": "2. Технические характеристики:", "zh": "2. 技术参数：", "en": "2. Technical data:"},
    "options_h": {"ru": "3. Опции:", "zh": "3. 选配：", "en": "3. Options:"},
    "param": {"ru": "Наименование", "zh": "名称", "en": "Parameter"},
    "value": {"ru": "Характеристика", "zh": "参数", "en": "Value"},
    "descr_col": {"ru": "Описание", "zh": "说明", "en": "Description"},
    "qty_short": {"ru": "Кол", "zh": "数量", "en": "Qty"},
}

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]

# В заголовках цены китайский юань принято писать RMB — как в образце
CURRENCY_LABEL = {"CNY": "RMB", "USD": "USD", "RUB": "RUB", "EUR": "EUR"}


def lb(key: str, lang: str) -> str:
    row = L.get(key, {})
    return row.get(lang) or row.get("en") or row.get("ru") or ""


def base_title(key: str) -> dict[str, str]:
    return dict(BASE_TITLES.get(key, {}))


def currency_label(currency: str) -> str:
    return CURRENCY_LABEL.get(currency, currency)


def money_header(key: str, lang: str, currency: str, incoterms: str = "") -> str:
    """«Стоимость\n(RMB/шт EXW)» — как в образце."""
    title = BASE_TITLES[key][lang]
    cur = currency_label(currency)
    if key == "price":
        inner = f"{cur}/{lb('pcs', lang)}"
    else:
        inner = cur
    if incoterms:
        inner = f"{inner} {incoterms}"
    return f"{title}\n({inner})"


def date_str(d: date, lang: str) -> str:
    if lang == "zh":
        return f"{d.year}年{d.month}月{d.day}日"
    if lang == "en":
        return f"{MONTHS_EN[d.month - 1]} {d.day}, {d.year}"
    return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year} г."


def money_str(value: float, currency: str, lang: str = "ru") -> str:
    """Для PDF: ￥78 100,00 / $1,234.56."""
    from ..models import CURRENCY_SYMBOL

    sym = CURRENCY_SYMBOL.get(currency, "")
    if lang == "ru":
        s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    else:
        s = f"{value:,.2f}"
    if currency == "RUB":
        return f"{s} {sym}"
    return f"{sym}{s}"


def qty_str(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
