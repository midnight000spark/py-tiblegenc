import re
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================

# Убедись, что этот файл лежит рядом со скриптом!
FONT_TIBETAN = 'NotoSansTibetan-Regular.ttf'
FONT_RUSSIAN = '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf'
FONT_BOLD    = '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf'
FONT_ITALIC  = '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf'

OUTPUT_FILE = 'mantras_MIXED_FIXED.pdf'
INPUT_FILE  = 'начало.txt'

def register_fonts():
    try:
        pdfmetrics.registerFont(TTFont('TibetanMixed', FONT_TIBETAN))
        pdfmetrics.registerFont(TTFont('RussianMain', FONT_RUSSIAN))
        pdfmetrics.registerFont(TTFont('RussianBold', FONT_BOLD))
        pdfmetrics.registerFont(TTFont('RussianItalic', FONT_ITALIC))
        print("✅ Шрифты загружены.")
    except Exception as e:
        print(f"❌ Ошибка шрифтов: {e}")
        exit()

# ==========================================
# 2. МАГИЯ СМЕШИВАНИЯ ШРИФТОВ
# ==========================================

def wrap_tibetan(text):
    """
    Находит тибетские символы в тексте и оборачивает их в HTML-тег шрифта.
    Остальной текст остается в текущем шрифте абзаца.
    """
    # 1. Экранируем спецсимволы XML
    safe_text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # 2. Разбиваем текст по тибетским символам, сохраняя их
    parts = re.split(r'([\u0F00-\u0FFF]+)', safe_text)

    result = ""
    for part in parts:
        # Если часть содержит тибетские символы — оборачиваем в тег шрифта
        if re.search(r'[\u0F00-\u0FFF]', part):
            result += f'<font name="TibetanMixed">{part}</font>'
        else:
            result += part

    return result

# ==========================================
# 3. ЛОГИКА И СТИЛИ
# ==========================================

def detect_line_type(line):
    stripped = line.strip()
    if not stripped:
        return 'empty'

    # Тибетский (если есть хоть один тибетский символ)
    if re.search(r'[\u0F00-\u0FFF]', stripped):
        return 'tibetan'

    # Сноски
    if re.match(r'^\d+[\.\)]', stripped):
        return 'footnote'

    # Примечания
    if stripped.startswith('[') and stripped.endswith(']'):
        return 'note'

    # Транслитерация (ЗАГЛАВНЫЕ)
    letters = [c for c in stripped if c.isalpha()]
    if letters:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.7 and not re.search(r'[.!?]$', stripped):
            return 'translit'

    # Если строка короткая и без знаков препинания — транслитерация
    if len(stripped) < 40 and not re.search(r'[.!?]$', stripped):
         if re.match(r'^[А-Яа-яA-Za-z\s]+$', stripped):
             return 'translit'

    return 'russian'

def create_styles():
    styles = {}
    base = getSampleStyleSheet()['Normal']

    # Стиль для чисто тибетских строк (использует тибетский шрифт целиком)
    styles['tibetan'] = ParagraphStyle(
        'Tibetan', parent=base,
        fontName='TibetanMixed', fontSize=16, leading=24,
        alignment=TA_CENTER, spaceAfter=10, textColor='#222222'
    )

    # Стиль для транслитерации
    styles['translit'] = ParagraphStyle(
        'Translit', parent=base,
        fontName='RussianBold', fontSize=13, leading=19,
        alignment=TA_CENTER, spaceAfter=8, textColor='#000000'
    )

    # Стиль для русского текста (использует DejaVu, тибетский будет подставлен тегами)
    styles['russian'] = ParagraphStyle(
        'Russian', parent=base,
        fontName='RussianMain', fontSize=11, leading=16,
        alignment=TA_LEFT, spaceAfter=12, textColor='#333333'
    )

    styles['footnote'] = ParagraphStyle(
        'Footnote', parent=base,
        fontName='RussianMain', fontSize=9, leading=12,
        alignment=TA_LEFT, spaceAfter=2, spaceBefore=4,
        textColor='#666666'
    )

    styles['note'] = ParagraphStyle(
        'Note', parent=base,
        fontName='RussianItalic', fontSize=10, leading=14,
        alignment=TA_CENTER, spaceBefore=4, spaceAfter=8,
        textColor='#444444'
    )

    return styles

# ==========================================
# 4. ГЕНЕРАЦИЯ
# ==========================================

def main():
    register_fonts()
    styles = create_styles()
    story = []

    print(f"📖 Чтение: {INPUT_FILE}")
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    print(f"⚙️ Обработка {len(lines)} строк...")
    for i, raw_line in enumerate(lines):
        clean = re.sub(r'\s+', ' ', raw_line).strip()
        if not clean:
            story.append(Spacer(1, 0.3*cm))
            continue

        ltype = detect_line_type(clean)

        # Если строка русская или содержит микс (тибетский в скобках),
        # мы применяем функцию wrap_tibetan, чтобы вставить шрифт точечно.
        if ltype in ['russian', 'note', 'footnote']:
             # Для сносок и примечаний тоже может быть тибетский, обрабатываем их
             processed_text = wrap_tibetan(clean)
             story.append(Paragraph(processed_text, styles[ltype]))
        elif ltype == 'translit':
             # Транслитерация обычно не содержит тибетский, но на всякий случай
             story.append(Paragraph(clean.replace('&', '&amp;'), styles['translit']))
        else:
             # Чисто тибетская строка
             story.append(Paragraph(clean.replace('&', '&amp;'), styles['tibetan']))

    doc = SimpleDocTemplate(
        OUTPUT_FILE, pagesize=A4,
        rightMargin=2.2*cm, leftMargin=2.2*cm,
        topMargin=2.5*cm, bottomMargin=2.5*cm
    )

    try:
        doc.build(story)
        print(f"✅ Готово: {OUTPUT_FILE}")
    except Exception as e:
        print(f"❌ Ошибка генерации: {e}")

if __name__ == '__main__':
    main()
