#!/usr/bin/env python3
"""
TibFix — Конвертер тибетских PDF из legacy-шрифтов в Unicode.

Назначение:
    Преобразование тибетских текстов из старых PDF-документов, использующих
    проприетарные шрифты (не-Unicode), в современный Unicode-текст или новый PDF.

Возможности:
    - Автоматическое распознавание ~20 популярных тибетских шрифтов (Dedris, Monlam, etc.)
    - Извлечение текста с сохранением структуры строк
    - Поддержка выборки по диапазонам страниц и областям (region)
    - Создание нового PDF с использованием современных Unicode-шрифтов
    - Детальная статистика конвертации

Архитектура:
    1. Input Layer: Парсинг аргументов командной строки и валидация путей
    2. Analysis Layer: Сканирование PDF, идентификация шрифтов через glyph_db.csv
    3. Processing Layer: Постраничная обработка через DuffedTextConverter
    4. Output Layer: Сохранение в TXT или генерация нового PDF

Примеры использования:
    # Извлечь весь текст в TXT
    tibfix.py input.pdf output.txt

    # Извлечь текст со страниц 1-5 и 10 в PDF с указанием шрифта
    tibfix.py input.pdf output.pdf --pages 1-5,10 --font /path/to/Jomolhari.ttf

    # Извлечь текст из конкретной области страницы
    tibfix.py input.pdf output.txt --region 100,50,400,700

Требования:
    - pytiblegenc (устанавливается через pip install -e .)
    - fpdf2 (опционально, только для вывода в PDF)
    - Python 3.8+

Автор: py-tiblegenc contributors
Лицензия: См. LICENSE
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Сторонние зависимости
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

# Локальные импорты из pytiblegenc
from pytiblegenc import (
    DuffedTextConverter,
    build_font_hash_index_from_csv,
    build_glyph_lookup_tables,
    get_glyph_db_path,
    identify_pdf_fonts_from_db,
)

# =============================================================================
# КОНСТАНТЫ И НАСТРОЙКИ
# =============================================================================

# Пути к системным тибетским шрифтам (кроссплатформенные)
SYSTEM_FONT_CANDIDATES: List[str] = [
    # Linux
    "/usr/share/fonts/truetype/jomolhari/Jomolhari.ttf",
    "/usr/share/fonts/tibetan-machine/TibetanMachineUni.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansTibetan-Regular.ttf",
    
    # macOS
    "/Library/Fonts/Jomolhari.ttf",
    os.path.expanduser("~/Library/Fonts/Jomolhari.ttf"),
    
    # Windows
    "C:/Windows/Fonts/Jomolhari.ttf",
    "C:/Windows/Fonts/NotoSansTibetan-Regular.ttf",
    
    # Локальный поиск (текущая директория)
    "Jomolhari.ttf",
    "NotoSansTibetan-Regular.ttf",
]

# Магические числа для очистки текста
MAX_CONSECUTIVE_NEWLINES: int = 1  # Максимальное количество подряд идущих \n
FONT_SIZE_TAG_PATTERN: str = r"<fs:\d+>"  # Паттерн для удаления тегов размера шрифта

# Настройки логирования
LOG_FORMAT: str = "%(levelname)s: %(message)s"
LOG_LEVEL_DEFAULT: int = logging.INFO


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def setup_logging(verbose: bool = False) -> None:
    """
    Настраивает систему логирования для приложения.

    Почему это важно:
        Замена print() на logging позволяет гибко управлять уровнем детализации,
        отключать вывод в production и сохранять логи в файл при необходимости.

    Args:
        verbose: Если True, устанавливает уровень DEBUG, иначе INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)


def parse_pages(pages_str: str) -> Set[int]:
    """
    Разбирает строку с диапазонами страниц в множество номеров (1-based).

    Примеры формата:
        "1-5" → {1, 2, 3, 4, 5}
        "1,3,5-7" → {1, 3, 5, 6, 7}
        "10" → {10}

    Почему 1-based нумерация:
        Пользователи привыкли считать страницы с 1, в то время как
        внутренние структуры данных часто используют 0-based. Эта функция
        служит мостом между пользовательским вводом и внутренней логикой.

    Args:
        pages_str: Строка вида "1-5,7,9-12".

    Returns:
        Множество номеров страниц для обработки.

    Raises:
        SystemExit: При некорректном формате диапазона или номера страницы.
    """
    pages: Set[int] = set()
    parts = pages_str.split(",")

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            # Обработка диапазона (например, "1-5")
            range_parts = part.split("-", 1)
            try:
                start = int(range_parts[0].strip())
                end = int(range_parts[1].strip())
                if start > end:
                    logging.warning(
                        f"Начало диапазона ({start}) больше конца ({end}). "
                        f"Диапазон будет проигнорирован."
                    )
                    continue
                pages.update(range(start, end + 1))
            except ValueError:
                logging.error(f"Ошибка в диапазоне страниц: '{part}'. Ожидался формат 'N-M'.")
                sys.exit(1)
        else:
            # Обработка одиночного номера
            try:
                pages.add(int(part))
            except ValueError:
                logging.error(f"Неверный номер страницы: '{part}'. Ожидалось целое число.")
                sys.exit(1)

    return pages


def parse_region(region_str: str) -> Optional[List[int]]:
    """
    Разбирает строку с координатами области извлечения (x,y,w,h).

    Почему важен формат x,y,w,h:
        Это стандартный формат для описания прямоугольных областей в PDF:
        - x, y: координаты левого нижнего угла (или верхнего, в зависимости от системы)
        - w, h: ширина и высота области в пунктах (1/72 дюйма)

    Args:
        region_str: Строка вида "100,50,400,700".

    Returns:
        Список из 4 целых чисел [x, y, w, h] или None, если строка пустая.

    Raises:
        SystemExit: При некорректном формате или отрицательных значениях.
    """
    if not region_str:
        return None

    parts = region_str.split(",")
    if len(parts) != 4:
        logging.error(
            f"Ошибка: --region требует ровно 4 числа (x,y,w,h), получено {len(parts)}."
        )
        sys.exit(1)

    try:
        coords = [int(p.strip()) for p in parts]
    except ValueError:
        logging.error("Ошибка: координаты --region должны быть целыми числами.")
        sys.exit(1)

    # Валидация на отрицательные значения
    if any(c < 0 for c in coords):
        logging.error("Ошибка: координаты региона не могут быть отрицательными.")
        sys.exit(1)

    # Валидация на нулевые размеры
    if coords[2] <= 0 or coords[3] <= 0:
        logging.error("Ошибка: ширина и высота региона должны быть положительными числами.")
        sys.exit(1)

    return coords


# =============================================================================
# ОСНОВНЫЕ ФУНКЦИИ
# =============================================================================

def extract_text(
    pdf_path: str,
    region: Optional[List[int]] = None,
    pages: Optional[Set[int]] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    Извлекает текст из PDF, преобразуя legacy-шрифты в Unicode.

    Почему эта функция критична:
        Это ядро всего конвертера. Она загружает таблицы маппинга глифов,
        идентифицирует шрифты в PDF и постранично извлекает текст с помощью
        DuffedTextConverter, который выполняет фактическую конвертацию символов.

    Этапы работы:
        1. Загрузка glyph_db.csv — базы данных соответствий глифов
        2. Построение индекса шрифтов для быстрого поиска
        3. Идентификация шрифтов в целевом PDF
        4. Построение таблиц прямого и обратного маппинга глифов
        5. Инициализация DuffedTextConverter с найденными таблицами
        6. Постраничная обработка с фильтрацией по страницам и региону
        7. Пост-обработка: удаление дублирующихся переносов строк

    Args:
        pdf_path: Путь к входному PDF-файлу.
        region: Опционально, список [x, y, w, h] для ограничения области извлечения.
        pages: Опционально, множество номеров страниц для обработки (1-based).

    Returns:
        Кортеж из:
            - text: Извлечённый Unicode-текст
            - stats: Словарь статистики конвертации

    Raises:
        FileNotFoundError: Если файл PDF не существует.
        Exception: При ошибках загрузки таблиц или парсинга PDF.
    """
    # Инициализация словаря статистики
    # Почему мы собираем статистику: для отладки и оценки качества конвертации
    stats: Dict[str, Any] = {
        "unhandled_fonts": {},
        "handled_fonts": {},
        "unknown_characters": {},
        "error_characters": 0,
        "diffs_with_utfc": {},
        "nb_non_horizontal_removed": 0,
    }

    output_string = StringIO()

    logging.info(f"Открытие PDF файла: {pdf_path}")
    with open(pdf_path, "rb") as f:
        parser = PDFParser(f)
        doc = PDFDocument(parser)

        # Загрузка таблиц конвертации
        # Почему это делается здесь: таблицы нужны для маппинга глифов конкретного PDF
        logging.debug("Загрузка базы данных глифов...")
        try:
            glyph_db_path = get_glyph_db_path()
            logging.debug(f"Путь к базе глифов: {glyph_db_path}")
        except Exception as e:
            logging.error(f"Не удалось получить путь к базе глифов: {e}")
            raise

        logging.debug("Построение индекса шрифтов из CSV...")
        glyph_index = build_font_hash_index_from_csv(str(glyph_db_path))

        logging.debug("Идентификация шрифтов в PDF...")
        font_normalization = identify_pdf_fonts_from_db(doc, glyph_index)

        logging.debug("Построение таблиц маппинга глифов...")
        glyph_lookup = build_glyph_lookup_tables(str(glyph_db_path))

        # Логирование найденных шрифтов
        if font_normalization:
            logging.info(f"Найдено шрифтов для конвертации: {len(font_normalization)}")
            for font_name, font_info in font_normalization.items():
                logging.debug(f"  - {font_name}: {font_info}")
        else:
            logging.warning("Не найдено шрифтов для конвертации. Возможно, PDF использует стандартные шрифты.")

        # Инициализация конвертера
        rsrcmgr = PDFResourceManager()
        device = DuffedTextConverter(
            rsrcmgr,
            output_string,
            stats,
            region=region,
            pbs="\n",               # Разделитель страниц (newline)
            remove_non_hz=True,     # Удалять не-горизонтальный текст
            font_normalization=font_normalization,
            glyph_lookup=glyph_lookup,
            track_font_size=False,  # Не вставлять теги размера <fs:…>
            error_chr_fun=None,     # Использовать стандартную обработку ошибок
        )
        interpreter = PDFPageInterpreter(rsrcmgr, device)

        # Постраничная обработка
        total_pages = len(list(PDFPage.create_pages(doc)))
        logging.info(f"Всего страниц в PDF: {total_pages}")

        processed_count = 0
        for i, page in enumerate(PDFPage.create_pages(doc), start=1):
            # Фильтрация по номерам страниц
            if pages is not None and i not in pages:
                logging.debug(f"Пропуск страницы {i} (не входит в указанный диапазон)")
                continue

            logging.debug(f"Обработка страницы {i}/{total_pages}...")
            try:
                interpreter.process_page(page)
                processed_count += 1
            except Exception as e:
                logging.error(f"Ошибка при обработке страницы {i}: {e}")
                # Продолжаем обработку следующих страниц
                continue

        logging.info(f"Обработано страниц: {processed_count}")

    text = output_string.getvalue()

    # Пост-обработка текста
    logging.debug("Выполнение пост-обработки текста...")

    # Удаление повторяющихся переносов строк
    # Почему MAX_CONSECUTIVE_NEWLINES = 1: оставляем только один \n между строками
    original_newline_count = text.count('\n')
    text = re.sub(r"\n\n+", "\n", text)
    removed_newlines = original_newline_count - text.count('\n')
    if removed_newlines > 0:
        logging.debug(f"Удалено {removed_newlines} лишних переносов строк")

    # Удаление тегов размера шрифта (если они остались)
    text = re.sub(FONT_SIZE_TAG_PATTERN, "", text)

    final_char_count = len(text)
    logging.info(f"Извлечено символов: {final_char_count}")

    return text, stats


def create_pdf(text: str, output_pdf: str, font_path: str, lines_per_page: int = 50) -> None:
    """
    Создаёт многостраничный PDF с тибетским Unicode-текстом, используя указанный шрифт.

    Почему используется fpdf2:
        Это лёгкая библиотека для генерации PDF без внешних зависимостей.
        Она поддерживает Unicode-шрифты через параметр uni=True.

    Особенности:
        - Автоматическая разбивка на страницы (lines_per_page строк на страницу)
        - Поддержка пустых строк как разделителей абзацев
        - multi_cell автоматически переносит длинные строки

    Args:
        text: Unicode-текст для размещения в PDF.
        output_pdf: Путь к выходному PDF-файлу.
        font_path: Путь к .ttf файлу тибетского шрифта.
        lines_per_page: Количество строк на странице (по умолчанию 50).

    Raises:
        SystemExit: Если библиотека fpdf2 не установлена.
        FileNotFoundError: Если файл шрифта не найден.
    """
    # Импорт fpdf с проверкой установки
    try:
        from fpdf import FPDF
    except ImportError:
        logging.error(
            "Библиотека fpdf2 не установлена. "
            "Установите её командой: pip install fpdf2"
        )
        sys.exit(1)

    # Валидация пути к шрифту
    font_path_obj = Path(font_path)
    if not font_path_obj.exists():
        logging.error(f"Файл шрифта не найден: {font_path}")
        sys.exit(1)

    logging.info(f"Создание PDF с использованием шрифта: {font_path}")

    pdf = FPDF()
    
    # Настройка страницы: A4, портретная ориентация
    pdf = FPDF(orientation='P', format='A4')
    
    # Разбиение текста на строки
    lines = text.split("\n")
    total_lines = len(lines)
    page_count = 0
    
    # Обработка первой страницы
    pdf.add_page()
    page_count += 1
    pdf.add_font("Tibetan", "", font_path, uni=True)
    pdf.set_font("Tibetan", size=14)
    
    # Установка левого маргинала для тибетского текста
    pdf.set_left_margin(15)
    pdf.set_right_margin(15)
    
    line_index = 0
    while line_index < total_lines:
        # Определяем диапазон строк для текущей страницы
        end_index = min(line_index + lines_per_page, total_lines)
        
        # Запись строк на текущую страницу
        for i in range(line_index, end_index):
            line = lines[i]
            if line.strip() == "":
                # Пустая строка — увеличиваем межстрочный интервал
                pdf.ln(7)
            else:
                # cell с последующим ln для простого построчного вывода
                # avoid multi_cell issues with Tibetan script
                pdf.cell(0, 7, line, new_x="LEFT", new_y="NEXT")
        
        line_index = end_index
        
        # Если есть ещё строки, добавляем новую страницу
        if line_index < total_lines:
            pdf.add_page()
            page_count += 1
            # Шрифт уже зарегистрирован, нужно только установить
            pdf.set_font("Tibetan", size=14)

    # Сохранение файла
    pdf.output(output_pdf)
    logging.info(f"PDF сохранён: {output_pdf} ({page_count} страниц, {total_lines} строк)")


def find_system_tibetan_font() -> Optional[str]:
    """
    Ищет тибетский .ttf шрифт в типичных системных расположениях.

    Почему поиск важен:
        Пользователи часто не знают, где установлены шрифты, или не хотят
        указывать путь вручную. Эта функция автоматически находит доступный
        тибетский шрифт в системе.

    Порядок поиска:
        1. Стандартные пути Linux (/usr/share/fonts/...)
        2. Локальная директория (Jomolhari.ttf)
        3. Пути macOS и Windows могут быть добавлены в будущем

    Returns:
        Путь к найденному шрифту или None, если ни один не найден.
    """
    logging.debug("Поиск системного тибетского шрифта...")

    for cand in SYSTEM_FONT_CANDIDATES:
        cand_path = Path(cand)
        if cand_path.exists():
            logging.info(f"Найден шрифт: {cand}")
            return cand

    logging.warning("Системный тибетский шрифт не найден.")
    return None



# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

def main() -> None:
    """
    Главная функция приложения — точка входа CLI.

    Почему эта функция важна:
        Она связывает все компоненты вместе: парсинг аргументов, валидацию,
        извлечение текста и сохранение результата. Это «дирижёр» всего оркестра.

    Поток выполнения:
        1. Парсинг аргументов командной строки
        2. Настройка логирования
        3. Валидация входных данных (файлы, регионы, страницы)
        4. Извлечение текста через extract_text()
        5. Вывод статистики (если не --quiet)
        6. Сохранение результата (TXT или PDF)

    Raises:
        SystemExit: При критических ошибках (файл не найден, пустой текст, etc.)
    """
    # Парсинг аргументов командной строки
    parser = argparse.ArgumentParser(
        description=(
            "TibFix — конвертер тибетских PDF из legacy-шрифтов в Unicode.\n"
            "Поддерживает ~20 популярных тибетских шрифтов (Dedris, Monlam, TibetanMachine и др.)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры использования:
  # Извлечь весь текст в TXT
  tibfix.py input.pdf output.txt

  # Извлечь текст с конкретных страниц в PDF
  tibfix.py input.pdf output.pdf --pages 1-5,10

  # Извлечь текст из области страницы
  tibfix.py input.pdf output.txt --region 100,50,400,700

  # Использовать конкретный шрифт для выходного PDF
  tibfix.py input.pdf tibetan.pdf --font /path/to/Jomolhari.ttf

  # Тихий режим (без статистики)
  tibfix.py input.pdf output.txt --quiet

  # Подробное логирование
  tibfix.py input.pdf output.txt -v
        """,
    )
    parser.add_argument(
        "input_pdf",
        help="Входной PDF с тибетским текстом (legacy-шрифт)."
    )
    parser.add_argument(
        "output",
        help="Выходной файл (.txt или .pdf). Расширение определяет формат."
    )
    parser.add_argument(
        "--output-type",
        choices=["txt", "pdf"],
        help=(
            "Принудительно задать тип выхода. По умолчанию определяется "
            "по расширению файла (.pdf → pdf, остальное → txt)."
        ),
    )
    parser.add_argument(
        "--region",
        metavar="X,Y,W,H",
        help=(
            "Координаты области извлечения: x,y,w,h (в пунктах). "
            "Пример: 100,50,400,700"
        ),
    )
    parser.add_argument(
        "--pages",
        metavar="N-M,K,L-P",
        help=(
            "Диапазоны страниц для обработки. Примеры: 1-5, 1,3,5-7, 10. "
            "По умолчанию обрабатываются все страницы."
        ),
    )
    parser.add_argument(
        "--font",
        metavar="PATH",
        help=(
            "Путь к .ttf файлу тибетского Unicode-шрифта для создания PDF. "
            "Если не указан, производится поиск системного шрифта."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Не выводить статистику конвертации."
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Включить подробное логирование (DEBUG уровень)."
    )

    args = parser.parse_args()

    # Настройка логирования
    setup_logging(verbose=args.verbose)

    logging.info("Запуск TibFix")
    logging.debug(f"Аргументы: {args}")

    # =========================================================================
    # ВАЛИДАЦИЯ ВХОДНЫХ ДАННЫХ
    # =========================================================================

    # Проверка существования входного файла
    input_path = Path(args.input_pdf)
    if not input_path.exists():
        logging.error(f"Входной файл не найден: {args.input_pdf}")
        sys.exit(1)
    if not input_path.is_file():
        logging.error(f"Указанный путь не является файлом: {args.input_pdf}")
        sys.exit(1)
    logging.debug(f"Входной файл: {input_path.absolute()}")

    # Проверка возможности записи в выходной файл
    out_path = Path(args.output)
    out_parent = out_path.parent
    if out_parent and not out_parent.exists():
        logging.error(f"Директория для выходного файла не существует: {out_parent}")
        sys.exit(1)

    # Определение типа выходного файла
    if args.output_type:
        out_type = args.output_type
        logging.debug(f"Тип выхода задан явно: {out_type}")
    else:
        ext = out_path.suffix.lower()
        if ext == ".pdf":
            out_type = "pdf"
        else:
            out_type = "txt"
        logging.debug(f"Тип выхода определён по расширению '{ext}': {out_type}")

    # Парсинг и валидация региона
    region = None
    if args.region:
        logging.debug(f"Парсинг региона: {args.region}")
        region = parse_region(args.region)
        # parse_region уже вызывает sys.exit() при ошибке
        logging.info(f"Регион извлечения: x={region[0]}, y={region[1]}, w={region[2]}, h={region[3]}")

    # Парсинг и валидация страниц
    pages = None
    if args.pages:
        logging.debug(f"Парсинг страниц: {args.pages}")
        pages = parse_pages(args.pages)
        if not pages:
            logging.error("Не выбрано ни одной страницы для обработки.")
            sys.exit(1)
        logging.info(f"Выбрано страниц для обработки: {len(pages)}")
        logging.debug(f"Номера страниц: {sorted(pages)}")

    # =========================================================================
    # ИЗВЛЕЧЕНИЕ ТЕКСТА
    # =========================================================================

    logging.info("Начало извлечения текста...")
    try:
        text, stats = extract_text(str(input_path), region=region, pages=pages)
    except FileNotFoundError as e:
        logging.error(f"Файл не найден: {e}")
        sys.exit(1)
    except Exception as e:
        logging.exception(f"Критическая ошибка при извлечении текста: {e}")
        sys.exit(1)

    logging.info("Извлечение текста завершено")

    # =========================================================================
    # ВЫВОД СТАТИСТИКИ
    # =========================================================================

    if not args.quiet:
        print("\n" + "=" * 50)
        print("📊 СТАТИСТИКА КОНВЕРТАЦИИ")
        print("=" * 50)

        stat_items = [
            ("handled_fonts", "✅ Успешно обработанные шрифты"),
            ("unhandled_fonts", "⚠️ Необработанные шрифты (обычно латиница)"),
            ("unknown_characters", "❓ Неизвестные символы"),
            ("diffs_with_utfc", "🔀 Расхождения с таблицами UTFC"),
            ("error_characters", "❌ Ошибочные символы"),
        ]

        has_stats = False
        for key_name, desc in stat_items:
            val = stats.get(key_name)
            if val:
                # Форматирование словарей для читаемости
                if isinstance(val, dict):
                    if val:
                        print(f"{desc}:")
                        # Сортировка по количеству (самые частые первыми)
                        sorted_items = sorted(val.items(), key=lambda x: x[1], reverse=True)
                        for k, v in sorted_items[:20]:  # Показываем первые 20
                            print(f"    {k}: {v}")
                        if len(val) > 20:
                            print(f"    ... и ещё {len(val) - 20} (всего {len(val)})")
                        has_stats = True
                else:
                    print(f"{desc}: {val}")
                    has_stats = True

        if not has_stats:
            print("Статистика пуста (возможно, текст был извлечён идеально)")

        print("=" * 50 + "\n")
    else:
        logging.debug("Статистика скрыта (--quiet)")

    # =========================================================================
    # ПРОВЕРКА РЕЗУЛЬТАТА
    # =========================================================================

    if not text.strip():
        logging.error(
            "Извлечённый текст пуст!\n"
            "Возможные причины:\n"
            "  - Неправильно указаны номера страниц (--pages)\n"
            "  - Неправильно задан регион (--region)\n"
            "  - PDF не содержит тибетского текста\n"
            "  - Шрифт PDF не распознан в базе glyph_db.csv"
        )
        sys.exit(1)

    char_count = len(text)
    line_count = text.count('\n') + 1
    logging.info(f"Результат: {char_count} символов, {line_count} строк")

    # =========================================================================
    # СОХРАНЕНИЕ РЕЗУЛЬТАТА
    # =========================================================================

    if out_type == "txt":
        # Сохранение в TXT
        logging.info(f"Сохранение текста в {out_path}...")
        try:
            out_path.write_text(text, encoding="utf-8")
            logging.info(f"✅ Текст успешно сохранён: {out_path.absolute()}")
        except IOError as e:
            logging.error(f"Ошибка записи файла: {e}")
            sys.exit(1)

    else:  # pdf
        # Создание PDF
        logging.info("Создание PDF...")

        font_path = args.font
        if not font_path:
            logging.debug("Шрифт не указан, поиск системного...")
            font_path = find_system_tibetan_font()
            if not font_path:
                logging.warning(
                    "Тибетский шрифт не найден!\n"
                    "Результат будет сохранён в TXT вместо PDF.\n"
                    "Для создания PDF укажите --font или установите системный шрифт:\n"
                    "  Linux: sudo apt install fonts-jomolhari\n"
                    "  macOS: brew install --cask jomolhari\n"
                    "  Windows: скачайте с https://www.thlib.org/tools/fonts/"
                )
                # Fallback на TXT
                out_path_txt = out_path.with_suffix('.txt')
                try:
                    out_path_txt.write_text(text, encoding="utf-8")
                    logging.info(f"✅ Текст сохранён в TXT: {out_path_txt.absolute()}")
                except IOError as e:
                    logging.error(f"Ошибка записи файла: {e}")
                    sys.exit(1)
                sys.exit(0)
            else:
                logging.info(f"Используется найденный шрифт: {font_path}")
        else:
            logging.debug(f"Используется указанный шрифт: {font_path}")

        try:
            create_pdf(text, str(out_path), font_path)
            logging.info(f"✅ PDF успешно создан: {out_path.absolute()}")
        except Exception as e:
            logging.exception(f"Ошибка создания PDF: {e}")
            logging.warning("Сохраняем результат в TXT вместо PDF...")
            # Fallback на TXT при ошибке создания PDF
            out_path_txt = out_path.with_suffix('.txt')
            try:
                out_path_txt.write_text(text, encoding="utf-8")
                logging.info(f"✅ Текст сохранён в TXT: {out_path_txt.absolute()}")
            except IOError as ex:
                logging.error(f"Ошибка записи файла: {ex}")
                sys.exit(1)

    logging.info("Работа завершена успешно")


if __name__ == "__main__":
    main()