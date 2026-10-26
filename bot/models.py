"""Модель коммерческого предложения и расчёт цен."""
from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

# Многоязычный текст: {"zh": "...", "ru": "...", "en": "..."}
Text = dict[str, str]

LANGS = ("ru", "zh", "en")
BASE_ORDER = ["no", "name", "photo", "spec", "qty", "price", "total"]
MONEY_BASE = {"price", "total"}

CURRENCY_SYMBOL = {"CNY": "￥", "USD": "$", "RUB": "₽", "EUR": "€"}
# Формат чисел Excel для каждой валюты
CURRENCY_FORMAT = {
    "CNY": '"￥"#,##0.00',
    "USD": '"$"#,##0.00',
    "RUB": '#,##0.00" ₽"',
    "EUR": '"€"#,##0.00',
}

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]")


def new_id() -> str:
    """Короткий id — помещается в callback_data (≤64 байта)."""
    return uuid.uuid4().hex[:6]


def detect_lang(s: str) -> str:
    """Язык строки: в китайских КП попадаются русские заголовки, и наоборот."""
    if not s:
        return "en"
    cjk = len(_CJK.findall(s))
    cyr = len(_CYR.findall(s))
    lat = len(_LAT.findall(s))
    if cjk >= 2 and cjk >= cyr and cjk >= lat:
        return "zh"
    if cjk and not cyr and not lat:
        return "zh"
    if cyr:
        return "ru"
    return "en"


def mk(s: str, lang: str | None = None) -> Text:
    """Текст на одном языке (язык определяется автоматически, если не задан)."""
    s = (s or "").strip()
    if not s:
        return {}
    return {lang or detect_lang(s): s}


def t(text: Text | None, lang: str) -> str:
    """Текст на языке lang; иначе первый имеющийся (исходник)."""
    if not text:
        return ""
    v = text.get(lang)
    if v:
        return v
    for key in LANGS:
        if text.get(key):
            return text[key]
    for v in text.values():
        if v:
            return v
    return ""


def has_lang(text: Text | None, lang: str) -> bool:
    return bool(text and text.get(lang))


def round_step(value: float, step: float) -> float:
    """Округление по шагу: 0.01 / 1 / 10 / 100."""
    if not step or step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 2)


def is_number(v: Any) -> bool:
    return parse_number(v) is not None


def parse_number(v: Any) -> float | None:
    """Число из ячейки/строки: '1 234,56', '¥3 750', '26000' → float."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace(" ", " ").replace(" ", "")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


class Column(BaseModel):
    key: str
    kind: Literal["text", "money"] = "text"
    title: Text = Field(default_factory=dict)
    on: bool = True
    merged: bool = False          # одно значение на всю таблицу (как «Примечание»)
    markup: bool = False          # начислять ли наценку на эту денежную колонку
    in_total: bool = True         # входит ли денежная колонка в «Итого»

    @property
    def is_money(self) -> bool:
        return self.kind == "money"


class Option(BaseModel):
    id: str = Field(default_factory=new_id)
    text: Text = Field(default_factory=dict)
    qty: float = 1
    cost: float = 0               # цена в исходной валюте
    price_override: float | None = None   # своя цена в валюте КП


class Product(BaseModel):
    id: str = Field(default_factory=new_id)
    name: Text = Field(default_factory=dict)
    spec: Text = Field(default_factory=dict)
    photo: str | None = None
    qty: float = 1
    cost: float = 0
    price_override: float | None = None
    markup_override: float | None = None
    texts: dict[str, Text] = Field(default_factory=dict)
    money: dict[str, float] = Field(default_factory=dict)
    options: list[Option] = Field(default_factory=list)


class Line(BaseModel):
    """Строка под таблицей: доставка, образцы, прочие расходы."""
    id: str = Field(default_factory=new_id)
    label: Text = Field(default_factory=dict)
    amount: float = 0             # в исходной валюте
    markup: bool = False


class Quote(BaseModel):
    number: str = ""
    created: date = Field(default_factory=date.today)
    source_file: str = ""
    factory_name: str = ""
    src_lang: str = "zh"
    lang: str = "ru"
    source_currency: str = "CNY"
    currency: str = "CNY"
    rate: float = 1.0
    markup_pct: float = 0.0
    rounding: float = 1.0
    incoterms: str = ""
    columns: list[Column] = Field(default_factory=list)
    merged: dict[str, Text] = Field(default_factory=dict)
    products: list[Product] = Field(default_factory=list)
    lines: list[Line] = Field(default_factory=list)
    show_total: bool = False
    client: str = ""
    terms: dict[str, Text] = Field(default_factory=dict)

    # --- доступ к колонкам ---

    def column(self, key: str) -> Column | None:
        for c in self.columns:
            if c.key == key:
                return c
        return None

    def active_columns(self) -> list[Column]:
        return [c for c in self.columns if c.on]

    def has_column(self, key: str) -> bool:
        c = self.column(key)
        return bool(c and c.on)

    def extra_columns(self) -> list[Column]:
        return [c for c in self.columns if c.key not in BASE_ORDER]

    # --- расчёт ---

    def markup_for(self, p: Product) -> float:
        return self.markup_pct if p.markup_override is None else p.markup_override

    def convert(self, amount: float, markup_pct: float = 0.0) -> float:
        return round_step(amount * self.rate * (1 + markup_pct / 100.0), self.rounding)

    def price(self, p: Product) -> float:
        if p.price_override is not None:
            return p.price_override
        return self.convert(p.cost, self.markup_for(p))

    def total(self, p: Product) -> float:
        return round(self.price(p) * p.qty, 2)

    def option_price(self, p: Product, o: Option) -> float:
        """Опция считается с наценкой своей позиции."""
        if o.price_override is not None:
            return o.price_override
        return self.convert(o.cost, self.markup_for(p))

    def option_total(self, p: Product, o: Option) -> float:
        return round(self.option_price(p, o) * o.qty, 2)

    def money_value(self, p: Product, col: Column) -> float | None:
        """Значение денежной доп. колонки в валюте КП."""
        raw = p.money.get(col.key)
        if raw is None:
            return None
        return self.convert(raw, self.markup_for(p) if col.markup else 0.0)

    def line_amount(self, line: Line) -> float:
        return self.convert(line.amount, self.markup_pct if line.markup else 0.0)

    def subtotal(self) -> float:
        """Суммы позиций (без опций — это альтернативы) + денежные колонки с in_total."""
        total = sum(self.total(p) for p in self.products)
        for col in self.active_columns():
            if not col.is_money or col.key in MONEY_BASE or not col.in_total or col.merged:
                continue
            for p in self.products:
                v = self.money_value(p, col)
                if v:
                    total += v
        return round(total, 2)

    def grand_total(self) -> float:
        return round(self.subtotal() + sum(self.line_amount(ln) for ln in self.lines), 2)

    # --- структура ---

    def product(self, pid: str) -> Product | None:
        for p in self.products:
            if p.id == pid:
                return p
        return None

    def ensure_columns(self) -> None:
        """Добавляет недостающие базовые колонки на свои места."""
        from .render.labels import base_title

        present = {c.key for c in self.columns}
        had_no = "no" in present
        any_photo = any(p.photo for p in self.products)
        any_spec = any(p.spec for p in self.products)

        for key in BASE_ORDER:
            if key in present:
                continue
            col = Column(
                key=key,
                kind="money" if key in MONEY_BASE else "text",
                title=base_title(key),
                on={"no": had_no, "photo": any_photo, "spec": any_spec}.get(key, True),
            )
            # вставляем на своё место относительно уже существующих базовых колонок
            idx = len(self.columns)
            pos = BASE_ORDER.index(key)
            for i, c in enumerate(self.columns):
                if c.key in BASE_ORDER and BASE_ORDER.index(c.key) > pos:
                    idx = i
                    break
            self.columns.insert(idx, col)
            present.add(key)

        for c in self.columns:
            if not c.title:
                c.title = base_title(c.key) or mk(c.key)
        # фото нечего показывать — выключаем
        photo = self.column("photo")
        if photo and not any_photo:
            photo.on = False
        spec = self.column("spec")
        if spec and not any_spec:
            spec.on = False

    def all_texts(self) -> list[Text]:
        """Все многоязычные тексты КП — для перевода."""
        out: list[Text] = [c.title for c in self.columns]
        out += list(self.merged.values())
        out += list(self.terms.values())
        for ln in self.lines:
            out.append(ln.label)
        for p in self.products:
            out.append(p.name)
            out.append(p.spec)
            out += list(p.texts.values())
            for o in p.options:
                out.append(o.text)
        return [x for x in out if x]
