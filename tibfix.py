#!/usr/bin/env python3
"""
Универсальный конвертер не‑Unicode тибетских PDF → Unicode TXT или новый PDF.
Требует установки pytiblegenc (pip install -e .) и fpdf2 для вывода в PDF.
"""

import argparse
import re
import sys
from pathlib import Path
from io import StringIO

from pytiblegenc import (
    DuffedTextConverter,
    build_font_hash_index_from_csv,
    identify_pdf_fonts_from_db,
    get_glyph_db_path,
    build_glyph_lookup_tables,
)
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser


def parse_pages(pages_str):
    """Разбирает строку '1-5,7,9-12' в множество номеров страниц (1-based)."""
    pages = set()
    parts = pages_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start = int(a.strip())
                end = int(b.strip())
                pages.update(range(start, end + 1))
            except ValueError:
                print(f"Ошибка в диапазоне страниц: {part}", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                pages.add(int(part))
            except ValueError:
                print(f"Неверный номер страницы: {part}", file=sys.stderr)
                sys.exit(1)
    return pages


def extract_text(pdf_path, region=None, pages=None):
    """
    Извлекает текст из PDF, преобразуя не‑Unicode шрифты.
    Возвращает (строка_текста, словарь_статистики).
    """
    stats = {
        "unhandled_fonts": {},
        "handled_fonts": {},
        "unknown_characters": {},
        "error_characters": 0,
        "diffs_with_utfc": {},
        "nb_non_horizontal_removed": 0,
    }
    output_string = StringIO()

    with open(pdf_path, "rb") as f:
        parser = PDFParser(f)
        doc = PDFDocument(parser)

        # Загрузка таблиц конвертации
        glyph_db_path = get_glyph_db_path()
        glyph_index = build_font_hash_index_from_csv(str(glyph_db_path))
        font_normalization = identify_pdf_fonts_from_db(doc, glyph_index)
        glyph_lookup = build_glyph_lookup_tables(str(glyph_db_path))

        rsrcmgr = PDFResourceManager()
        device = DuffedTextConverter(
            rsrcmgr,
            output_string,
            stats,
            region=region,
            pbs="\n",               # без пометок страниц
            remove_non_hz=True,
            font_normalization=font_normalization,
            glyph_lookup=glyph_lookup,
            track_font_size=False,  # не вставлять <fs:…>
            error_chr_fun=None,     # стандартная обработка ошибок
        )
        interpreter = PDFPageInterpreter(rsrcmgr, device)

        for i, page in enumerate(PDFPage.create_pages(doc), start=1):
            if pages is not None and i not in pages:
                continue
            interpreter.process_page(page)

    text = output_string.getvalue()
    # Чистим: убираем повторы переводов строк и возможные теги размера (если вдруг остались)
    text = re.sub(r"\n\n+", "\n", text)
    text = re.sub(r"<fs:\d+>", "", text)
    return text, stats


def create_pdf(text, output_pdf, font_path):
    """Создаёт новый PDF с тибетским текстом, используя указанный шрифт."""
    try:
        from fpdf import FPDF
    except ImportError:
        print(
            "Ошибка: Для создания PDF нужна библиотека fpdf2. "
            "Установите её командой: pip install fpdf2",
            file=sys.stderr,
        )
        sys.exit(1)

    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("Tibetan", "", font_path, uni=True)
    pdf.set_font("Tibetan", size=12)
    # Сохраняем переносы строк
    for line in text.split("\n"):
        if line.strip() == "":
            pdf.ln(5)
        else:
            pdf.multi_cell(0, 6, line)
    pdf.output(output_pdf)
    print(f"PDF сохранён: {output_pdf}")


def find_system_tibetan_font():
    """Ищет тибетский .ttf шрифт в типичных местах."""
    candidates = [
        "/usr/share/fonts/truetype/jomolhari/Jomolhari.ttf",
        "/usr/share/fonts/tibetan-machine/TibetanMachineUni.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansTibetan-Regular.ttf",
        "Jomolhari.ttf",
    ]
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Конвертирует тибетский PDF со старыми шрифтами в Unicode TXT или новый PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры:
  tibfix.py input.pdf output.txt
  tibfix.py input.pdf output.pdf --pages 1-5 --region 100,50,400,700
  tibfix.py input.pdf tibetan.pdf --font /путь/к/Jomolhari.ttf
        """,
    )
    parser.add_argument("input_pdf", help="Входной PDF с тибетским текстом.")
    parser.add_argument("output", help="Выходной файл (.txt или .pdf).")
    parser.add_argument(
        "--output-type",
        choices=["txt", "pdf"],
        help="Принудительно задать тип выхода (обычно определяется по расширению).",
    )
    parser.add_argument(
        "--region",
        help="Координаты области страницы: x,y,w,h (например, 100,50,400,700).",
    )
    parser.add_argument(
        "--pages",
        help="Диапазоны страниц: 1-5 или 1,3,5-7. По умолчанию обрабатываются все.",
    )
    parser.add_argument(
        "--font",
        help="Путь к .ttf тибетскому шрифту для PDF. При отсутствии ищется системный.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Не выводить статистику конвертации."
    )
    args = parser.parse_args()

    # --- Определяем тип выходного файла ---
    out_path = Path(args.output)
    if args.output_type:
        out_type = args.output_type
    else:
        ext = out_path.suffix.lower()
        if ext == ".pdf":
            out_type = "pdf"
        else:
            out_type = "txt"  # всё остальное считаем текстом

    # --- Разбор region ---
    region = None
    if args.region:
        parts = args.region.split(",")
        if len(parts) != 4:
            print("Ошибка: --region требует 4 числа: x,y,w,h", file=sys.stderr)
            sys.exit(1)
        try:
            region = [int(p.strip()) for p in parts]
        except ValueError:
            print("Ошибка: координаты --region должны быть целыми числами.", file=sys.stderr)
            sys.exit(1)

    # --- Разбор страниц ---
    pages = None
    if args.pages:
        pages = parse_pages(args.pages)
        if not pages:
            print("Не выбрано ни одной страницы.", file=sys.stderr)
            sys.exit(1)

    # --- Извлечение текста ---
    print("Извлекаю тибетский текст...")
    text, stats = extract_text(args.input_pdf, region=region, pages=pages)

    if not args.quiet:
        print("\n--- Статистика конвертации ---")
        for key_name, desc in [
            ("handled_fonts", "Успешно обработанные шрифты"),
            ("unhandled_fonts", "Необработанные шрифты (обычно латиница)"),
            ("unknown_characters", "Неизвестные символы"),
            ("diffs_with_utfc", "Расхождения с таблицами UTFC"),
            ("error_characters", "Ошибочные символы"),
        ]:
            val = stats.get(key_name)
            if val:
                print(f"{desc}: {val}")
        print("-------------------------------\n")

    if not text.strip():
        print("Внимание: извлечённый текст пуст. Проверьте корректность страниц и region.", file=sys.stderr)
        sys.exit(1)

    # --- Сохранение ---
    if out_type == "txt":
        out_path.write_text(text, encoding="utf-8")
        print(f"Текст сохранён в {out_path}")
    else:  # pdf
        font_path = args.font
        if not font_path:
            font_path = find_system_tibetan_font()
            if not font_path:
                print(
                    "Не найден тибетский .ttf шрифт. Укажите его через --font.\n"
                    "Установите Jomolhari, например: sudo apt install fonts-jomolhari",
                    file=sys.stderr,
                )
                sys.exit(1)
            else:
                print(f"Использую найденный шрифт: {font_path}")
        create_pdf(text, args.output, font_path)


if __name__ == "__main__":
    main()