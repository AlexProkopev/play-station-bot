"""Self-drawn access captcha. No third-party captcha package."""

from __future__ import annotations

import math
import os
import random
import re
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


LETTER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CAPTION_LETTERS = (
    "🔵 <b>Вход в PlayStation™Store</b>\n\n"
    "Напишите символы с картинки одним сообщением, без пробелов."
)
CAPTION_MATH = (
    "🔵 <b>Вход в PlayStation™Store</b>\n\n"
    "Посчитайте пример с картинки и пришлите ответ числом."
)

_HEADER_GREEN = (92, 164, 92)
_BORDER = (58, 58, 58)
_TEXT_GRAY = (108, 108, 108)
_NOISE_GRAY = (145, 145, 145)

_HEADER_FONT_CANDIDATES = [
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "verdanab.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_BODY_FONT_CANDIDATES = [
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "timesbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "georgiab.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "georgia.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "times.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CANDIDATES = _BODY_FONT_CANDIDATES


@dataclass(frozen=True)
class CaptchaChallenge:
    answer: str
    image_png: bytes
    kind: str
    caption: str


def normalize_answer(text: str | None) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", text or "").upper().replace("Ё", "Е")


def answers_match(entered: str | None, expected: str | None) -> bool:
    left = normalize_answer(entered)
    right = normalize_answer(expected)
    return bool(left) and left == right


def create_challenge(rng: random.Random | None = None, kind: str | None = None) -> CaptchaChallenge:
    rng = rng or random.Random()
    selected = kind or ("letters" if rng.random() < 0.55 else "math")
    if selected == "letters":
        text = "".join(rng.choice(LETTER_ALPHABET) for _ in range(5))
        return CaptchaChallenge(
            answer=text,
            image_png=_render_challenge_image(list(text), rng, letters=True),
            kind="letters",
            caption=CAPTION_LETTERS,
        )
    answer, glyphs = _build_math_glyphs(rng)
    return CaptchaChallenge(
        answer=answer,
        image_png=_render_challenge_image(glyphs, rng, letters=False),
        kind="math",
        caption=CAPTION_MATH,
    )


def _build_math_glyphs(rng: random.Random) -> tuple[str, list[str]]:
    op = rng.choice(["+", "-"])
    if op == "+":
        left = rng.randint(3, 18)
        right = rng.randint(2, 19)
        value = left + right
    else:
        left = rng.randint(8, 24)
        right = rng.randint(1, left - 1)
        value = left - right
    glyphs = list(str(left)) + [op] + list(str(right)) + ["=", "?"]
    return str(value), glyphs


def _font_paths(candidates: list[str]) -> list[str]:
    return [path for path in candidates if os.path.exists(path)]


def _load_font(candidates: list[str], size: int, rng: random.Random) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = _font_paths(candidates)
    rng.shuffle(paths)
    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _glyphs_for_display(glyphs: list[str], rng: random.Random, *, letters: bool) -> list[str]:
    if not letters:
        return glyphs
    shown: list[str] = []
    for ch in glyphs:
        if ch.isalpha():
            shown.append(ch.upper() if rng.random() < 0.65 else ch.lower())
        else:
            shown.append(ch)
    return shown


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _draw_squiggles(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rng: random.Random,
) -> None:
    left, top, right, bottom = box
    width = max(10, right - left)
    height = max(10, bottom - top)
    for curve in range(2):
        color = (rng.randint(75, 95), rng.randint(75, 95), rng.randint(75, 95))
        points: list[tuple[int, int]] = []
        start_x = left + rng.randint(0, width // 4)
        end_x = right - rng.randint(0, width // 4)
        steps = max(14, width // 12)
        amplitude = rng.randint(max(6, height // 6), max(12, height // 3))
        phase = rng.random() * math.pi * 2
        mid_y = top + height // 2 + rng.randint(-height // 5, height // 5)
        for i, x in enumerate(range(start_x, end_x + 1, max(1, (end_x - start_x) // steps))):
            t = i / max(steps, 1)
            y = mid_y + int(amplitude * math.sin(t * math.pi * 2.2 + phase + curve))
            points.append((x, y))
        if len(points) >= 2:
            draw.line(points, fill=color, width=1)


def _draw_local_noise(
    image: Image.Image,
    box: tuple[int, int, int, int],
    rng: random.Random,
    *,
    dots: int = 220,
) -> None:
    left, top, right, bottom = box
    pixels = image.load()
    for _ in range(dots):
        x = rng.randint(left, right)
        y = rng.randint(top, bottom)
        tone = rng.randint(100, 175)
        pixels[x, y] = (tone, tone, tone)


def _render_challenge_image(glyphs: list[str], rng: random.Random, *, letters: bool) -> bytes:
    card_w, header_h, body_h = 320, 42, 128
    card_h = header_h + body_h
    shadow = 3

    canvas = Image.new("RGB", (card_w + shadow, card_h + shadow), (210, 210, 210))
    card = Image.new("RGB", (card_w, card_h), (255, 255, 255))
    draw = ImageDraw.Draw(card)

    draw.rectangle((0, 0, card_w - 1, card_h - 1), outline=_BORDER, width=1)
    draw.rectangle((1, 1, card_w - 2, header_h), fill=_HEADER_GREEN)
    draw.line((1, header_h, card_w - 2, header_h), fill=_BORDER, width=1)

    header_font = _load_font(_HEADER_FONT_CANDIDATES, 22, rng)
    draw.text(
        (card_w // 2, header_h // 2 + 1),
        "CAPTCHA",
        font=header_font,
        fill=(255, 255, 255),
        anchor="mm",
    )

    body_top = header_h + 1
    body_box = (8, body_top + 8, card_w - 9, card_h - 9)
    display = _glyphs_for_display(glyphs, rng, letters=letters)
    text = " ".join(display) if not letters else "".join(display)
    font_size = 56 if letters else 44
    body_font = _load_font(_BODY_FONT_CANDIDATES, font_size, rng)

    tw, th = _text_size(draw, text, body_font)
    bx0, by0, bx1, by1 = body_box
    text_x = bx0 + max(0, (bx1 - bx0 - tw) // 2)
    text_y = by0 + max(0, (by1 - by0 - th) // 2) + rng.randint(-2, 2)
    text_box = (text_x - 8, text_y - 6, text_x + tw + 8, text_y + th + 6)

    draw.text((text_x, text_y), text, font=body_font, fill=_TEXT_GRAY)
    _draw_squiggles(draw, text_box, rng)
    _draw_local_noise(card, text_box, rng)

    canvas.paste(card, (shadow, shadow))
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()