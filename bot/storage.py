"""Хранение на диске: data/users/<tg_id>/{quote.json, prefs.json, photos/}."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .models import Quote

DEFAULT_PREFS: dict[str, Any] = {
    "ui_lang": "ru",          # язык интерфейса бота: ru | zh
    "markup_pct": 6.0,
    "langs": ["ru"],          # языки итоговых файлов
    "currency": "CNY",
    "rate": None,             # свой курс (если задан вручную)
    "rounding": 1.0,
    "hidden_base": [],        # скрытые базовые колонки (по ключу)
    "hidden_titles": [],      # скрытые свои колонки (по заголовку)
    "state": None,            # ожидание ввода: {"kind": ..., ...}
    "card_id": None,          # message_id карточки
}


def user_dir(uid: int) -> Path:
    p = DATA_DIR / "users" / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def photos_dir(uid: int) -> Path:
    p = user_dir(uid) / "photos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def photo_path(uid: int, name: str) -> Path:
    return photos_dir(uid) / name


# --- настройки пользователя ---

def load_prefs(uid: int) -> dict[str, Any]:
    path = user_dir(uid) / "prefs.json"
    prefs = dict(DEFAULT_PREFS)
    try:
        prefs.update(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        pass
    if not prefs.get("langs"):
        prefs["langs"] = ["ru"]
    return prefs


def save_prefs(uid: int, prefs: dict[str, Any]) -> None:
    (user_dir(uid) / "prefs.json").write_text(
        json.dumps(prefs, ensure_ascii=False, indent=1), "utf-8")


# --- КП ---

def load_quote(uid: int) -> Quote | None:
    path = user_dir(uid) / "quote.json"
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Quote.model_validate(raw)
    except Exception:
        return None


def save_quote(uid: int, quote: Quote) -> None:
    (user_dir(uid) / "quote.json").write_text(
        quote.model_dump_json(indent=1), "utf-8")


def clear_quote(uid: int) -> None:
    for name in ("quote.json",):
        try:
            (user_dir(uid) / name).unlink()
        except OSError:
            pass
    shutil.rmtree(photos_dir(uid), ignore_errors=True)
    photos_dir(uid)


def save_photo(uid: int, name: str, data: bytes) -> str:
    path = photo_path(uid, name)
    path.write_bytes(data)
    return name


def out_dir(uid: int) -> Path:
    p = user_dir(uid) / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p
