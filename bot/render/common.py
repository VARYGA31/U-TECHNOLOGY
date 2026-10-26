"""Общие функции для Excel и PDF."""
from __future__ import annotations

import io
import math
import os
import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageChops

MAX_PHOTO_PX = 1000
EXCEL_MAX_ROW_PT = 409.0
SAFE_ROW_PT = 400.0
LINE_PT = 17.0          # высота строки текста при 12 pt с переносом
OPTION_ROW_PT = 50.0

OPTION_WORDS = ("опция", "选配", "option")


def display_width(s: str) -> int:
    """Ширина строки в «символах Excel»: иероглиф ≈ 2 символа."""
    width = 0
    for ch in s:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def wrapped_lines(text: str, col_chars: float) -> int:
    """Сколько визуальных строк займёт текст в колонке такой ширины."""
    if not text:
        return 1
    usable = max(col_chars - 1.0, 4.0)
    total = 0
    for raw in str(text).split("\n"):
        w = display_width(raw)
        total += max(1, math.ceil(w / usable))
    return max(total, 1)


def text_height_pt(text: str, col_chars: float, line_pt: float = LINE_PT) -> float:
    return wrapped_lines(text, col_chars) * line_pt + 6.0


def split_height(total_pt: float, limit: float = SAFE_ROW_PT) -> list[float]:
    """Excel не даёт строку выше 409 pt — раскладываем на несколько строк."""
    total_pt = max(total_pt, 20.0)
    if total_pt <= limit:
        return [total_pt]
    n = math.ceil(total_pt / limit)
    return [round(total_pt / n, 1)] * n


def col_width_to_px(width_chars: float) -> float:
    return width_chars * 7.0 + 5.0


def pt_to_px(pt: float) -> float:
    return pt * 96.0 / 72.0


def starts_with_option_word(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(low.startswith(w) for w in OPTION_WORDS)


def strip_option_word(text: str) -> str:
    s = (text or "").strip()
    for w in OPTION_WORDS:
        if s.lower().startswith(w):
            return s[len(w):].lstrip(" :：-—").strip()
    return s


# --------------------------------------------------------------- картинки

def prepare_photo(data: bytes, max_px: int = MAX_PHOTO_PX) -> bytes:
    """Обрезаем белые поля и ужимаем — у заводов фото по 2–5 МБ."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return data
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
    else:
        im = im.convert("RGB")

    im = _trim_white(im)
    if max(im.size) > max_px:
        ratio = max_px / float(max(im.size))
        im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _trim_white(im: Image.Image) -> Image.Image:
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg)
    diff = ImageChops.add(diff, diff, 2.0, -24)
    box = diff.getbbox()
    if not box:
        return im
    pad = 2
    left = max(box[0] - pad, 0)
    top = max(box[1] - pad, 0)
    right = min(box[2] + pad, im.width)
    bottom = min(box[3] + pad, im.height)
    if right - left < 8 or bottom - top < 8:
        return im
    return im.crop((left, top, right, bottom))


def photo_bytes(photo_dir: Path | str | None, name: str | None) -> bytes | None:
    if not name or not photo_dir:
        return None
    path = Path(photo_dir) / name
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def fit_box(img_w: int, img_h: int, box_w: float, box_h: float) -> tuple[float, float]:
    """Вписать с сохранением пропорций."""
    if img_w <= 0 or img_h <= 0:
        return box_w, box_h
    ratio = min(box_w / img_w, box_h / img_h)
    return img_w * ratio, img_h * ratio


# --------------------------------------------------------------- прочее

def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    return name.strip("_") or "file"


def out_name(number: str, lang: str, ext: str) -> str:
    return f"{number}_{lang.upper()}.{ext}"


def spec_pairs(spec: str) -> list[tuple[str, str]]:
    """Строки «Параметр: значение» → пары для таблицы характеристик в PDF."""
    pairs: list[tuple[str, str]] = []
    for raw in (spec or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(.{1,80}?)\s*[:：]\s*(.+)$", line)
        if m:
            pairs.append((m.group(1).strip(), m.group(2).strip()))
        else:
            pairs.append((line, ""))
    return pairs


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def file_size_mb(path: str | Path) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0
