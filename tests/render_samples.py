"""Разбор образцов своим парсером и рендер Excel + PDF в out/.

Запуск:  python tests/render_samples.py examples/kp_cn.xlsx examples/ex3.xlsx
Без аргументов берёт все три образца.
PDF требует системных библиотек Pango (есть в Docker-образе); если их нет,
сохраняется HTML, чтобы вёрстку можно было посмотреть браузером.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import t  # noqa: E402
from bot.parsing.extract import extract  # noqa: E402
from bot.parsing.own_template import looks_like_own_format, parse_own  # noqa: E402
from bot.rates import next_number  # noqa: E402
from bot.render.pdf import render_html, render_pdf  # noqa: E402
from bot.render.xlsx import render_xlsx  # noqa: E402
from bot.service import materialize_photos  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
EXAMPLES = ROOT / "examples"


def process(path: Path) -> None:
    print("=" * 70)
    print(path.name)
    ex = extract(str(path))
    if not looks_like_own_format(ex):
        print("  !! не распознан как свой формат")
        return
    quote = parse_own(ex, path.name)
    if quote is None:
        print("  !! не разобрано")
        return
    quote.number = next_number()
    quote.markup_pct = 6.0
    quote.rounding = 1.0

    photo_dir = OUT / "photos" / path.stem
    materialize_photos(quote, ex, photo_dir)

    lang = quote.src_lang
    print(f"  язык={lang} позиций={len(quote.products)} "
          f"опций={sum(len(p.options) for p in quote.products)} "
          f"фото={sum(1 for p in quote.products if p.photo)} "
          f"итог={quote.grand_total()}")
    for p in quote.products:
        print(f"    - {t(p.name, lang)[:50]}  {quote.price(p)}")

    stem = f"{path.stem}_{lang}"
    xlsx_path = OUT / f"{stem}.xlsx"
    render_xlsx(quote, lang, xlsx_path, photo_dir)
    print("  Excel →", xlsx_path, f"{xlsx_path.stat().st_size // 1024} КБ")

    html_path = OUT / f"{stem}.html"
    html_path.write_text(render_html(quote, lang, photo_dir), "utf-8")
    print("  HTML  →", html_path, f"{html_path.stat().st_size // 1024} КБ")

    try:
        pdf_path = render_pdf(quote, lang, OUT / f"{stem}.pdf", photo_dir)
        print("  PDF   →", pdf_path, f"{pdf_path.stat().st_size // 1024} КБ")
    except OSError as exc:
        print("  PDF   пропущен (нет библиотек Pango):", str(exc)[:80])
    except ImportError as exc:
        print("  PDF   пропущен (WeasyPrint не установлен):", str(exc)[:80])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    paths = [Path(a) for a in args] if args else [
        EXAMPLES / "kp_cn.xlsx", EXAMPLES / "kp_ru.xlsx", EXAMPLES / "ex3.xlsx"]
    for p in paths:
        if not p.exists():
            p2 = EXAMPLES / p.name
            if p2.exists():
                p = p2
            else:
                print("нет файла:", p)
                continue
        process(p)
    print("\nГотово. Файлы в", OUT)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
