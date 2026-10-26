"""Курсы ЦБ РФ (кэш 1 час) и номер КП."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import aiohttp

from .config import DATA_DIR, settings

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_TTL = 3600

_cache: dict[str, object] = {"ts": 0.0, "rates": {}}


async def fetch_rates() -> dict[str, float]:
    """{"RUB": 1.0, "CNY": 11.9, ...} — сколько рублей за единицу валюты."""
    now = time.time()
    if _cache["rates"] and now - float(_cache["ts"]) < CACHE_TTL:  # type: ignore[arg-type]
        return dict(_cache["rates"])  # type: ignore[arg-type]
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(CBR_URL) as resp:
                resp.raise_for_status()
                data = json.loads(await resp.text())
    except Exception:
        return dict(_cache["rates"])  # type: ignore[arg-type]
    out: dict[str, float] = {"RUB": 1.0}
    for code, item in (data.get("Valute") or {}).items():
        try:
            out[code] = float(item["Value"]) / float(item["Nominal"])
        except (KeyError, TypeError, ValueError):
            continue
    _cache["rates"] = out
    _cache["ts"] = now
    return dict(out)


def cross(rates: dict[str, float], src: str, dst: str) -> float | None:
    """Кросс-курс: сколько dst за единицу src."""
    if src == dst:
        return 1.0
    a, b = rates.get(src), rates.get(dst)
    if not a or not b:
        return None
    return round(a / b, 6)


async def cross_rate(src: str, dst: str) -> float | None:
    if src == dst:
        return 1.0
    return cross(await fetch_rates(), src, dst)


def _counter_path() -> Path:
    return DATA_DIR / "counter.json"


def next_number(today: date | None = None) -> str:
    """UT-ГГГГММДД-NNN, счётчик за день."""
    today = today or date.today()
    key = today.strftime("%Y%m%d")
    path = _counter_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    n = int(data.get(key, 0)) + 1
    data = {key: n}  # храним только текущий день
    try:
        path.write_text(json.dumps(data), "utf-8")
    except OSError:
        pass
    return f"{settings.brand.number_prefix}-{key}-{n:03d}"
