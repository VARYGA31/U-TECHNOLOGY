"""Настройки: .env + branding/company.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or (ROOT / "data"))
TEMPLATES_DIR = ROOT / "templates"
BRANDING_DIR = ROOT / "branding"


def _ids(raw: str | None) -> list[int]:
    out: list[int] = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


@dataclass(slots=True)
class Brand:
    name: str = "U-Technology Group Limited"
    logo: str = "branding/logo.png"
    header_color: str = "#9DC3E6"
    bar_color: str = "#4474A0"
    brand_color: str = "#3E8F8D"
    font_title: str = "Cambria"
    font_text: str = "微软雅黑"
    number_prefix: str = "UT"
    footer_ru: str = ""
    footer_zh: str = ""
    footer_en: str = ""
    letterhead_name: str = "U-Technology Group Co., Ltd"
    email: str = ""
    website: str = ""
    phone: str = ""

    @property
    def logo_path(self) -> Path:
        p = Path(self.logo)
        return p if p.is_absolute() else ROOT / p

    def footer(self, lang: str) -> str:
        return {"ru": self.footer_ru, "zh": self.footer_zh, "en": self.footer_en}.get(lang, self.footer_en)


def load_brand() -> Brand:
    path = BRANDING_DIR / "company.toml"
    if not path.exists():
        return Brand()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    known = {f.name for f in Brand.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return Brand(**{k: v for k, v in raw.items() if k in known})


@dataclass(slots=True)
class Settings:
    bot_token: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_base_url: str | None = None
    allowed_user_ids: list[int] = field(default_factory=list)
    brand: Brand = field(default_factory=load_brand)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    def allowed(self, user_id: int) -> bool:
        return not self.allowed_user_ids or user_id in self.allowed_user_ids


def load_settings() -> Settings:
    return Settings(
        bot_token=(os.getenv("BOT_TOKEN") or "").strip(),
        openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip(),
        openai_model=(os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip(),
        openai_base_url=(os.getenv("OPENAI_BASE_URL") or "").strip() or None,
        allowed_user_ids=_ids(os.getenv("ALLOWED_USER_IDS")),
        brand=load_brand(),
    )


settings = load_settings()
