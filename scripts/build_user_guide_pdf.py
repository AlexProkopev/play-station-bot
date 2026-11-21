"""Build a user-facing PDF guide with annotated Telegram-style screens."""

from __future__ import annotations

import math
import os
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs"
OUT_PDF = OUT_DIR / "Инструкция_для_пользователя.pdf"

WIN_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_REG = str(WIN_FONTS / "arial.ttf")
FONT_BOLD = str(WIN_FONTS / "arialbd.ttf")

# Telegram-like palette
CHAT_BG = (14, 22, 33)
HEADER = (23, 33, 43)
BUBBLE = (24, 37, 51)
BUBBLE_USER = (43, 82, 120)
BTN = (42, 47, 54)
BTN_TEXT = (255, 255, 255)
MUTED = (138, 150, 163)
WHITE = (255, 255, 255)
ACCENT = (255, 140, 50)
BADGE = (232, 93, 4)
GREEN = (82, 196, 107)
INPUT_BG = (23, 33, 43)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    return ImageFont.truetype(path, size)


def rounded(draw: ImageDraw.ImageDraw, box, radius: int, fill) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def new_phone(w=760, h=1280) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), CHAT_BG)
    d = ImageDraw.Draw(img)
    rounded(d, (0, 0, w, 88), 0, HEADER)
    d.text((28, 22), "Дружный Мир", font=font(28, True), fill=WHITE)
    d.text((28, 56), "бот  •  онлайн", font=font(16), fill=MUTED)
    return img, d


def bubble(d: ImageDraw.ImageDraw, x, y, w, text: str, user=False) -> int:
    fnt = font(22)
    lines = []
    for para in text.split("\n"):
        lines.extend(wrap(d, para, fnt, w - 36) if para else [""])
    h = 24 + len(lines) * 28
    fill = BUBBLE_USER if user else BUBBLE
    rounded(d, (x, y, x + w, y + h), 18, fill)
    ty = y + 12
    for line in lines:
        d.text((x + 18, ty), line, font=fnt, fill=WHITE)
        ty += 28
    return y + h + 14


def inline_btn(d, box, text: str, fnt=None) -> None:
    fnt = fnt or font(20)
    rounded(d, box, 12, BTN)
    x1, y1, x2, y2 = box
    tw = d.textlength(text, font=fnt)
    d.text(((x1 + x2 - tw) / 2, y1 + (y2 - y1 - 24) / 2), text, font=fnt, fill=BTN_TEXT)


def reply_bar(d, w, h, rows: list[list[str]], y_from_bottom=18) -> dict[str, tuple]:
    """Draw reply keyboard; return map label -> center (x,y)."""
    centers = {}
    row_h = 56
    gap = 8
    pad = 16
    total_h = len(rows) * row_h + (len(rows) - 1) * gap + 20
    top = h - total_h - y_from_bottom
    rounded(d, (0, top - 10, w, h), 0, HEADER)
    cy = top
    for row in rows:
        n = len(row)
        btn_w = (w - pad * 2 - gap * (n - 1)) / n
        cx = pad
        for label in row:
            box = (cx, cy, cx + btn_w, cy + row_h)
            inline_btn(d, box, label, font(18))
            centers[label] = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            cx += btn_w + gap
        cy += row_h + gap
    return centers


def badge(d, x, y, n: int) -> None:
    r = 16
    d.ellipse((x - r, y - r, x + r, y + r), fill=BADGE)
    fnt = font(18, True)
    t = str(n)
    tw = d.textlength(t, font=fnt)
    d.text((x - tw / 2, y - 11), t, font=fnt, fill=WHITE)


def arrow(d, x1, y1, x2, y2, color=ACCENT) -> None:
    d.line((x1, y1, x2, y2), fill=color, width=5)
    ang = math.atan2(y2 - y1, x2 - x1)
    for da in (2.5, -2.5):
        ax = x2 - 18 * math.cos(ang + da * 0.18)
        ay = y2 - 18 * math.sin(ang + da * 0.18)
        d.line((x2, y2, ax, ay), fill=color, width=5)


def legend_block(d, items: list[tuple[int, str]], x, y, w) -> int:
    fnt_n = font(20, True)
    fnt = font(20)
    for n, text in items:
        badge(d, x + 16, y + 14, n)
        lines = wrap(d, text, fnt, w - 50)
        ty = y
        for i, line in enumerate(lines):
            d.text((x + 40, ty), line, font=fnt if i else fnt, fill=WHITE)
            ty += 26
        y = max(y + 36, ty + 8)
    return y


def screen_start_city() -> Image.Image:
    img, d = new_phone()
    y = 110
    y = bubble(
        d,
        24,
        y,
        700,
        "Добро пожаловать!\nНачните поиск своего Города (одним словом).\nПример: Белгород",
    )
    y = bubble(d, 220, y, 500, "Белгород", user=True)
    centers = reply_bar(
        d,
        760,
        1280,
        [
            ["👤 Профиль", "📞 Контакты"],
            ["🎟 Активировать купон"],
            ["💬 Отзывы по всем городам"],
            ["❓ Поддержка и правила", "❌ Завершить и открыть меню"],
        ],
    )
    order = [
        ("👤 Профиль", 1),
        ("📞 Контакты", 2),
        ("🎟 Активировать купон", 3),
        ("💬 Отзывы по всем городам", 4),
        ("❓ Поддержка и правила", 5),
        ("❌ Завершить и открыть меню", 6),
    ]
    for label, n in order:
        cx, cy = centers[label]
        badge(d, cx - 90 if n in (2, 4, 8) else cx + 90, cy, n)
    return img


def screen_catalog() -> Image.Image:
    img, d = new_phone()
    y = 110
    y = bubble(
        d,
        24,
        y,
        710,
        "Город найден: Белгород\nНиже доступны позиции:",
    )
    buttons = [
        (["🛒 Товар A", "💬 Отзывы"], 1, 2),
        (["🛒 Товар B", "💬 Отзывы"], 1, 2),
        (["🧺 Мини-опт -%"], 3),
        (["🏠 В начало"], 4),
        (["🔄 Выбрать другой город"], 5),
        (["💬 Все отзывы по текущему городу"], 6),
    ]
    x, w = 24, 712
    for row in buttons:
        labels = row[0]
        h = 52
        if len(labels) == 2:
            mid = x + w / 2 - 4
            inline_btn(d, (x, y, mid, y + h), labels[0], font(18))
            inline_btn(d, (mid + 8, y, x + w, y + h), labels[1], font(18))
            badge(d, x + 18, y + 8, 1)
            badge(d, mid + 26, y + 8, 2)
        else:
            inline_btn(d, (x, y, x + w, y + h), labels[0], font(18))
            badge(d, x + 22, y + 8, row[1])
        y += h + 10
    return img


def screen_districts() -> Image.Image:
    img, d = new_phone()
    y = 110
    bubble(d, 24, y, 710, "Районы города Белгород\nВыберите район кнопкой ниже:")
    centers = reply_bar(
        d,
        760,
        1280,
        [
            ["Центр", "Южный"],
            ["Восточный", "Западный"],
            ["🔙 Назад", "❌ Завершить и открыть меню"],
        ],
    )
    badge(d, centers["Центр"][0], centers["Центр"][1] - 28, 1)
    badge(d, centers["🔙 Назад"][0] - 70, centers["🔙 Назад"][1], 2)
    badge(d, centers["❌ Завершить и открыть меню"][0] + 80, centers["❌ Завершить и открыть меню"][1], 3)
    return img


def screen_payment() -> Image.Image:
    img, d = new_phone()
    y = 110
    y = bubble(
        d,
        24,
        y,
        710,
        "Выбор оплаты\nДоступно 4 варианта:\n• Баланс  • Карта РФ\n• Bitcoin  • Litecoin",
    )
    rows = [
        ("💰 Оплатить с баланса", 1),
        ("💳 Оплатить картой РФ", 2),
        ("₿ Оплатить Bitcoin (BTC)", 3),
        ("Ł Оплатить Litecoin (LTC)", 4),
        ("🌍 Зарубежная карта", 5),
        ("🔙 Назад", 6),
        ("❌ Завершить и открыть меню", 7),
    ]
    for text, n in rows:
        inline_btn(d, (24, y, 736, y + 52), text, font(20))
        badge(d, 48, y + 10, n)
        y += 62
    return img


def screen_profile() -> Image.Image:
    img, d = new_phone()
    y = 110
    y = bubble(
        d,
        24,
        y,
        710,
        "Ваш профиль\nID: 123456789\nПокупок: 3\nСкидка: 0%\nРефералов: 1\nБаланс: 1500 руб.\nРеферальная ссылка: t.me/bot?start=123",
    )
    for text, n in [
        ("➕ Пополнить баланс", 1),
        ("🎟 Активировать купон", 2),
        ("❌ Завершить и открыть меню", 3),
    ]:
        inline_btn(d, (24, y, 736, y + 52), text, font(20))
        badge(d, 48, y + 10, n)
        y += 62
    return img


def screen_reviews() -> Image.Image:
    img, d = new_phone()
    y = 110
    y = bubble(d, 24, y, 710, "Отзыв 2/14\nГород: Белгород\n«Всё быстро, описание совпало.»")
    inline_btn(d, (24, y, 250, y + 52), "⬅️", font(22))
    inline_btn(d, (262, y, 498, y + 52), "2/14", font(20))
    inline_btn(d, (510, y, 736, y + 52), "➡️", font(22))
    badge(d, 48, y + 8, 1)
    badge(d, 534, y + 8, 2)
    y += 66
    inline_btn(d, (24, y, 370, y + 52), "🛒 Оформить", font(20))
    inline_btn(d, (382, y, 736, y + 52), "↩️ К каталогу", font(20))
    badge(d, 48, y + 8, 3)
    badge(d, 406, y + 8, 4)
    y += 66
    inline_btn(d, (24, y, 736, y + 52), "❌ Завершить и открыть меню", font(20))
    badge(d, 48, y + 8, 5)
    return img


def screen_captcha() -> Image.Image:
    img, d = new_phone()
    y = 110
    rounded(d, (90, y, 670, y + 180), 16, (245, 247, 250))
    d.text((250, y + 55), "7  4  1  9", font=font(54, True), fill=(30, 30, 30))
    y += 200
    bubble(d, 24, y, 710, "Проверка входа\nВведите 4 цифры с картинки одним сообщением.")
    bubble(d, 420, y + 130, 300, "7419", user=True)
    return img


def flow_image() -> Image.Image:
    img = Image.new("RGB", (1400, 420), (18, 26, 38))
    d = ImageDraw.Draw(img)
    steps = [
        "1. /start\nкапча",
        "2. Город",
        "3. Каталог\nтовар",
        "4. Район",
        "5. Формат\nвыдачи",
        "6. Оплата",
        "7. Чек или\nхеш / 1 час",
    ]
    fnt = font(22, True)
    for i, text in enumerate(steps):
        x = 30 + i * 195
        rounded(d, (x, 90, x + 170, 280), 18, BUBBLE)
        lines = text.split("\n")
        ty = 140
        for line in lines:
            tw = d.textlength(line, font=fnt)
            d.text((x + (170 - tw) / 2, ty), line, font=fnt, fill=WHITE)
            ty += 32
        if i < len(steps) - 1:
            arrow(d, x + 176, 185, x + 190, 185)
    d.text((30, 30), "Путь заказа от входа до получения", font=font(28, True), fill=WHITE)
    return img


def register_pdf_fonts() -> tuple[str, str]:
    pdfmetrics.registerFont(TTFont("Guide", FONT_REG))
    pdfmetrics.registerFont(TTFont("Guide-Bold", FONT_BOLD))
    return "Guide", "Guide-Bold"


def draw_header_bar(c, title: str, page: int, pages: int, regular: str, bold: str) -> None:
    c.setFillColorRGB(0.09, 0.14, 0.20)
    c.rect(0, A4[1] - 52, A4[0], 52, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, 13)
    c.drawString(28, A4[1] - 32, title)
    c.setFont(regular, 9)
    c.setFillColorRGB(0.7, 0.75, 0.8)
    c.drawRightString(A4[0] - 28, A4[1] - 32, f"{page} / {pages}")


def draw_footer(c, regular: str) -> None:
    c.setFillColorRGB(0.55, 0.6, 0.65)
    c.setFont(regular, 8)
    c.drawString(28, 18, "Инструкция для пользователя  •  Дружный Мир")


def put_image(c, img: Image.Image, x, y, max_w, max_h) -> tuple[float, float]:
    w, h = img.size
    scale = min(max_w / w, max_h / h)
    nw, nh = w * scale, h * scale
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    c.drawImage(ImageReader(buf), x, y, width=nw, height=nh, mask="auto")
    return nw, nh


def text_block(c, x, y, width, lines: list[tuple[str, str]], regular: str, bold: str) -> float:
    """lines: (style, text) style=h|b|p. Returns last y."""
    for style, text in lines:
        if style == "h":
            c.setFont(bold, 12)
            c.setFillColorRGB(0.12, 0.14, 0.18)
            c.drawString(x, y, text)
            y -= 16
        elif style == "b":
            c.setFont(bold, 10)
            c.setFillColorRGB(0.90, 0.36, 0.02)
            c.drawString(x, y, text)
            y -= 14
        else:
            c.setFont(regular, 9.5)
            c.setFillColorRGB(0.18, 0.2, 0.24)
            for wrapped in _wrap_pdf(c, text, regular, 9.5, width):
                c.drawString(x, y, wrapped)
                y -= 13
            y -= 4
    return y


def _wrap_pdf(c, text: str, font_name: str, size: float, width: float) -> list[str]:
    words = text.split()
    out, cur = [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if c.stringWidth(trial, font_name, size) <= width:
            cur = trial
        else:
            if cur:
                out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out or [""]


def numbered(c, x, y, n: int, text: str, width: float, regular: str, bold: str) -> float:
    c.setFillColorRGB(0.91, 0.36, 0.02)
    c.circle(x + 8, y + 3, 8, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, 8)
    c.drawCentredString(x + 8, y, str(n))
    c.setFillColorRGB(0.18, 0.2, 0.24)
    c.setFont(regular, 9.5)
    first = True
    for line in _wrap_pdf(c, text, regular, 9.5, width - 24):
        c.drawString(x + 22, y if first else y, line)
        y -= 13
        first = False
    return y - 6


PAGES = []


def add_page(builder):
    PAGES.append(builder)


def build() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    regular, bold = register_pdf_fonts()
    c = pdf_canvas.Canvas(str(OUT_PDF), pagesize=A4)
    W, H = A4
    total = 11

    # 1 cover
    c.setFillColorRGB(0.09, 0.14, 0.20)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColorRGB(0.91, 0.36, 0.02)
    c.rect(0, H - 8, W, 8, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, 26)
    c.drawString(48, H - 140, "Инструкция для пользователя")
    c.setFont(regular, 14)
    c.setFillColorRGB(0.75, 0.8, 0.85)
    c.drawString(48, H - 170, "Бот «Дружный Мир»  •  что нажимать и что за что отвечает")
    bullets = [
        "Как войти и выбрать город",
        "Нижнее меню и кнопки в каталоге",
        "Как оформить заказ и оплатить (карта, BTC, LTC)",
        "Профиль, отзывы, рулетка, поддержка",
    ]
    c.setFont(regular, 12)
    y = H - 240
    for item in bullets:
        c.setFillColorRGB(0.91, 0.36, 0.02)
        c.circle(58, y + 3, 4, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        c.drawString(74, y, item)
        y -= 28
    c.setFillColorRGB(0.55, 0.6, 0.65)
    c.setFont(regular, 9)
    c.drawString(48, 40, "Ориентируйтесь на кнопки на экране. Команды руками почти не нужны.")
    c.showPage()

    # 2 flow
    draw_header_bar(c, "1. Как устроен заказ", 2, total, regular, bold)
    y = H - 78
    y = text_block(
        c,
        28,
        y,
        W - 56,
        [
            ("p", "Весь путь — это несколько экранов подряд. Стрелки на схеме ниже — порядок шагов."),
        ],
        regular,
        bold,
    )
    put_image(c, flow_image(), 20, y - 175, W - 40, 165)
    y = y - 190
    notes = [
        ("h", "Коротко по шагам"),
        ("p", "После /start бот показывает картинку с 4 цифрами. Их нужно ввести одним сообщением."),
        ("p", "Затем напишите город полностью, одним словом или через дефис: Белгород, Петропавловск-Камчатский."),
        ("p", "Если городов несколько, бот покажет список с регионом — выберите свой кнопкой."),
        ("p", "Если ваш город не найден, попробуйте указать ближайший крупный город."),
        ("p", "Дальше: товар → район → формат выдачи → оплата. Карта РФ — 12 минут. Крипта (BTC/LTC) — 1 час, после перевода пришлите хеш."),
    ]
    y = text_block(c, 28, y, W - 56, notes, regular, bold)
    draw_footer(c, regular)
    c.showPage()

    # 3 captcha + start
    draw_header_bar(c, "2. Вход: /start и капча", 3, total, regular, bold)
    img = screen_captcha()
    put_image(c, img, 28, 90, 250, 680)
    y = H - 80
    y = text_block(
        c,
        300,
        y,
        270,
        [
            ("h", "Что делать"),
            ("p", "Напишите боту /start."),
            ("p", "Придёт картинка с 4 цифрами. Введите их в чат, без пробелов."),
            ("p", "Если цифры не совпали — придёт новая картинка. Нужно ровно 4 цифры."),
            ("p", "После успешной проверки появится приветствие и нижнее меню. Бот попросит город."),
            ("h", "Если не пускает"),
            ("p", "При частых отменах заказов доступ может быть закрыт на 12 часов. Тогда бот напишет до какого времени блок."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 4 bottom menu
    draw_header_bar(c, "3. Нижнее меню — что за что отвечает", 4, total, regular, bold)
    img = screen_start_city()
    put_image(c, img, 18, 70, 255, 700)
    y = H - 78
    y = text_block(
        c,
        290,
        y,
        280,
        [("h", "Кнопки внизу экрана"), ("p", "Это постоянное меню. Оно нужно, когда бот ждёт город или вы в главном экране.")],
        regular,
        bold,
    )
    items = [
        (1, "Профиль — баланс, скидка, число покупок, реферальная ссылка, пополнение и купон."),
        (2, "Контакты — служебный текст магазина (как связаться)."),
        (3, "Активировать купон — ввести код от администрации, сумма падает на баланс."),
        (4, "Отзывы по всем городам — лента отзывов без привязки к вашему городу."),
        (5, "Поддержка и правила — график, диспут, ненаход, претензии. Отсюда можно написать оператору."),
        (6, "Завершить и открыть меню — сброс текущего шага, снова ввод города. Не путать с отменой уже созданной оплаты."),
    ]
    for n, t in items:
        y = numbered(c, 290, y, n, t, 280, regular, bold)
    draw_footer(c, regular)
    c.showPage()

    # 5 catalog
    draw_header_bar(c, "4. Каталог города", 5, total, regular, bold)
    img = screen_catalog()
    put_image(c, img, 18, 90, 255, 680)
    y = H - 78
    y = text_block(
        c,
        290,
        y,
        280,
        [
            ("h", "Кнопки под сообщением каталога"),
            ("p", "После выбора города нижнее меню обычно не нужно: всё важное — прямо под списком товаров."),
        ],
        regular,
        bold,
    )
    for n, t in [
        (1, "Купить — начать оформление этой позиции: район, формат выдачи, оплата."),
        (2, "Отзывы — отзывы именно по этому товару в текущем городе."),
        (3, "Мини-опт — отдельный прайс со скидкой 15%. Назад в обычный каталог — кнопка «Обычный каталог»."),
        (4, "В начало — полный сброс, снова ввод города."),
        (5, "Выбрать другой город — смена города без полного выхода из бота."),
        (6, "Все отзывы по текущему городу — лента по выбранному городу, не по одному товару."),
    ]:
        y = numbered(c, 290, y, n, t, 280, regular, bold)
    y = text_block(
        c,
        290,
        y - 4,
        280,
        [("p", "Пока висит неоплаченный заказ, новый купить нельзя: сначала оплатите или отмените текущий.")],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 6 districts
    draw_header_bar(c, "5. Район и формат выдачи", 6, total, regular, bold)
    img = screen_districts()
    put_image(c, img, 18, 90, 255, 680)
    y = H - 78
    y = text_block(
        c,
        290,
        y,
        280,
        [
            ("h", "После «Купить»"),
            ("p", "Бот показывает районы, где позиция доступна. Это уже нижние кнопки, не инлайн."),
        ],
        regular,
        bold,
    )
    for n, t in [
        (1, "Название района — выбрать район выдачи. Дальше бот спросит формат (как будет выдан заказ)."),
        (2, "Назад — вернуться в каталог этого города, без смены города."),
        (3, "Завершить и открыть меню — выйти из оформления и снова ввести город."),
    ]:
        y = numbered(c, 290, y, n, t, 280, regular, bold)
    y = text_block(
        c,
        290,
        y,
        280,
        [
            ("h", "Формат выдачи"),
            ("p", "Следующий экран — варианты выдачи (например разные типы клада). Выберите один кнопкой."),
            ("p", "Снова есть «Назад» (к районам) и «Завершить и открыть меню»."),
            ("p", "После формата открывается оплата."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 7 payment
    draw_header_bar(c, "6. Оплата", 7, total, regular, bold)
    img = screen_payment()
    put_image(c, img, 18, 90, 255, 680)
    y = H - 78
    for n, t in [
        (1, "С баланса — списание с вашего баланса в боте. Если денег не хватает, бот покажет текущий остаток."),
        (2, "Картой РФ — номер карты, сумма, номер заявки. 12 минут. После перевода — скриншот или PDF чека."),
        (3, "Bitcoin (BTC) — сумма в BTC по дневному курсу. Перевод на кошелёк магазина, затем хеш в чат. 1 час."),
        (4, "Litecoin (LTC) — то же, что Bitcoin, только в LTC."),
        (5, "Зарубежная карта — сейчас недоступна."),
        (6, "Назад — вернуться к выбору района."),
        (7, "Отмена — отказаться от оформления. Если заявка уже создана, это отмена заказа."),
    ]:
        y = numbered(c, 290, y, n, t, 280, regular, bold)
    y = text_block(
        c,
        290,
        y,
        280,
        [
            ("h", "Важно про карту РФ"),
            ("p", "30 отмен подряд дают бан на 12 часов. Нельзя иметь два активных заказа сразу."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 8 crypto
    draw_header_bar(c, "6б. Оплата Bitcoin и Litecoin", 8, total, regular, bold)
    y = H - 78
    y = text_block(
        c,
        28,
        y,
        W - 56,
        [
            ("h", "Как проходит крипто-оплата"),
            ("p", "Бот берёт цену товара в рублях и пересчитывает в BTC или LTC по курсу CoinGecko (обновляется раз в сутки). На экране будут: сумма в ₽, курс, точная сумма в крипте и кошелёк."),
            ("p", "Переведите именно эту сумму на указанный адрес. Заявка живёт 1 час. Если за час оплаты нет, она закрывается сама."),
            ("h", "Что отправить боту"),
            ("p", "После перевода пришлите хеш транзакции (txid) одним сообщением. Это длинный код из букв и цифр из приложения кошелька или обозревателя. Скриншота недостаточно — нужен именно хеш."),
            ("p", "Хеш уходит оператору. Дальше заявка либо подтверждается, либо отменяется. Пока ждёте, другие кнопки меню недоступны: можно только отменить заявку или прислать хеш."),
            ("h", "Если кнопка крипты не открывается"),
            ("p", "Кошелёк ещё не настроен или курс временно недоступен. Выберите карту РФ или баланс, либо попробуйте позже."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 8 profile
    draw_header_bar(c, "7. Профиль, баланс, купон, рефералы", 9, total, regular, bold)
    img = screen_profile()
    put_image(c, img, 18, 90, 255, 680)
    y = H - 78
    y = text_block(
        c,
        290,
        y,
        280,
        [
            ("h", "Что видно в профиле"),
            ("p", "ID, число покупок, персональная скидка, число рефералов, баланс и ссылка для друзей."),
        ],
        regular,
        bold,
    )
    for n, t in [
        (1, "Пополнить баланс — ввести сумму, затем «Карта через оператора» и прислать чек. Оператор зачислит вручную."),
        (2, "Активировать купон — ввести код. При успехе деньги сразу на баланс."),
        (3, "Отмена — закрыть профиль и вернуться к вводу города / началу."),
    ]:
        y = numbered(c, 290, y, n, t, 280, regular, bold)
    y = text_block(
        c,
        290,
        y,
        280,
        [
            ("h", "Рефералка"),
            ("p", "Ссылка вида t.me/бот?start=ВАШ_ID. Друг должен запустить бота по ней и пройти капчу. Бонусы начисляет магазин."),
            ("p", "Купон можно активировать и из нижнего меню, не заходя в профиль."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 9 reviews + roulette
    draw_header_bar(c, "8. Отзывы и рулетка", 10, total, regular, bold)
    img = screen_reviews()
    put_image(c, img, 18, 200, 240, 560)
    y = H - 78
    y = text_block(
        c,
        280,
        y,
        290,
        [("h", "Отзывы")],
        regular,
        bold,
    )
    for n, t in [
        (1, "Стрелка влево — предыдущий отзыв."),
        (2, "Стрелка вправо — следующий отзыв. Цифра в центре — какой отзыв сейчас."),
        (3, "Оформить — сразу купить этот товар."),
        (4, "К каталогу — вернуться к списку товаров города."),
        (5, "Отмена — в начало, снова город."),
    ]:
        y = numbered(c, 280, y, n, t, 290, regular, bold)
    y = text_block(
        c,
        280,
        y,
        290,
        [
            ("p", "В общей ленте может быть «Поиск по городу»: вводите название и смотрите отзывы другого города."),
            ("h", "Рулетка"),
            ("p", "Кнопка «🎰 Рулетка» показывает активные игры."),
            ("p", "✅ Участвовать — запись в розыгрыш. Иногда нужна подписка на канал — бот предупредит."),
            ("p", "👥 Список участников и 🏆 Результаты — кто записан и чем закончилась игра."),
            ("p", "Если набор закрыт, участвовать уже нельзя, можно только смотреть итоги."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    # 10 support + tips
    draw_header_bar(c, "9. Поддержка, правила и частые ситуации", 11, total, regular, bold)
    y = H - 78
    y = text_block(
        c,
        28,
        y,
        W - 56,
        [
            ("p", "Кнопка «📘 Инструкция по использованию бота» в нижнем меню присылает этот PDF. Нажмите на файл в чате — он откроется в Telegram. На компьютере скачайте и откройте двойным щелчком."),
            ("h", "Поддержка и правила"),
            ("p", "Кнопка в нижнем меню открывает правила магазина: график операторов (обычно 10:00–22:00 МСК, воскресенье свободный), как открывать диспут, какие фото нужны при ненаходе, недовесе и претензии по качеству."),
            ("p", "Внизу правил: «Написать нам» — после этого опишите проблему одним сообщением и при необходимости приложите фото. «Закрыть» просто убирает это сообщение."),
            ("h", "Что нельзя делать"),
            ("p", "Не спамьте несуществующими городами — система может ограничить доступ."),
            ("p", "Не отменяйте заявки на карту пачками: 6 отмен подряд = бан на 12 часов."),
            ("p", "Не начинайте второй заказ, пока первый не оплачен и не закрыт."),
            ("h", "Если пропали кнопки внизу"),
            ("p", "В Telegram их можно свернуть иконкой из четырёх точек рядом с полем ввода. Нажмите её ещё раз — меню вернётся. В каталоге нужные действия и так висят под сообщением."),
            ("h", "Команды на всякий случай"),
            ("p", "/start — начать заново (капча). /help — короткая подсказка. /profile — профиль. /roulette — рулетка."),
            ("h", "Оператор"),
            ("p", "Контакт поддержки всегда дублируется в приветствии после капчи. Пока ждёте ответа, пишите по делу: фото, время, номер заявки."),
        ],
        regular,
        bold,
    )
    draw_footer(c, regular)
    c.showPage()

    c.save()
    print(OUT_PDF)


if __name__ == "__main__":
    build()
