"""Self-drawn access captcha. No third-party captcha package."""

from __future__ import annotations

import math
import os
import random
import re
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont


LETTER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CAPTION_LETTERS = (
    "🔵 <b>Вход в PlayStation™Store</b>\n\n"
    "Напишите символы с картинки одним сообщением, без пробелов."
)
CAPTION_MATH = (
    "🔵 <b>Вход в PlayStation™Store</b>\n\n"
    "Посчитайте пример с картинки и пришлите ответ числом."
)
_INK = [
    (36, 42, 86),
    (92, 28, 48),
    (18, 78, 72),
    (78, 46, 18),
    (58, 28, 92),
    (22, 58, 108),
]
_FONT_CANDIDATES = [
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "NotoSans-Bold.ttf")),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "comicbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "georgia.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "georgiab.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "courbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "impact.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "timesbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "verdanab.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
]


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
            image_png=_render_challenge_image(list(text), rng),
            kind="letters",
            caption=CAPTION_LETTERS,
        )
    answer, glyphs = _build_math_glyphs(rng)
    return CaptchaChallenge(
        answer=answer,
        image_png=_render_challenge_image(glyphs, rng),
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


def _available_font_paths() -> list[str]:
    found = [path for path in _FONT_CANDIDATES if os.path.exists(path)]
    return found or [""]


def _load_font(size: int, rng: random.Random) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = _available_font_paths()
    rng.shuffle(paths)
    for path in paths:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _paper_background(width: int, height: int, rng: random.Random) -> Image.Image:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    pixels = image.load()
    for y in range(height):
        mix = y / max(height - 1, 1)
        base = (
            int(238 - mix * 18 + rng.randint(-6, 6)),
            int(232 - mix * 16 + rng.randint(-6, 6)),
            int(218 - mix * 22 + rng.randint(-6, 6)),
        )
        for x in range(width):
            speck = rng.randint(-12, 10)
            pixels[x, y] = (
                max(160, min(255, base[0] + speck)),
                max(150, min(255, base[1] + speck)),
                max(130, min(255, base[2] + speck)),
            )
    return image


def _draw_clutter(draw: ImageDraw.ImageDraw, width: int, height: int, rng: random.Random) -> None:
    for _ in range(8):
        color = (rng.randint(150, 200), rng.randint(150, 195), rng.randint(155, 205), rng.randint(35, 70))
        x0 = rng.randint(-20, width)
        y0 = rng.randint(-20, height)
        x1 = x0 + rng.randint(50, 170)
        y1 = y0 + rng.randint(24, 90)
        draw.arc((x0, y0, x1, y1), start=rng.randint(0, 180), end=rng.randint(181, 360), fill=color, width=1)
    for _ in range(3):
        color = rng.choice(_INK) + (rng.randint(40, 75),)
        points = [
            (rng.randint(10, width - 10), rng.randint(18, height - 18))
            for _ in range(3)
        ]
        draw.line(points, fill=color, width=1)
    decoy_font = _load_font(rng.randint(16, 24), rng)
    for _ in range(5):
        ch = rng.choice(LETTER_ALPHABET)
        color = (rng.randint(180, 210), rng.randint(175, 205), rng.randint(165, 200), rng.randint(35, 65))
        draw.text(
            (rng.randint(8, width - 28), rng.randint(8, height - 28)),
            ch,
            font=decoy_font,
            fill=color,
        )


def _glyph_layer(symbol: str, rng: random.Random, font_size: int) -> Image.Image:
    font = _load_font(font_size, rng)
    canvas_w = max(72, font_size + 28)
    canvas_h = max(90, font_size + 42)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), symbol, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (canvas_w - tw) // 2 - bbox[0]
    y = (canvas_h - th) // 2 - bbox[1]
    ink = rng.choice(_INK)
    shadow = (ink[0] // 3, ink[1] // 3, ink[2] // 3, 90)
    draw.text((x + 2, y + 3), symbol, font=font, fill=shadow)
    draw.text((x, y), symbol, font=font, fill=ink + (255,))
    angle = rng.randint(-22, 22)
    warped = canvas.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shear = rng.uniform(-0.10, 0.10)
    width, height = warped.size
    extra = max(8, int(abs(shear) * height) + 8)
    sheared = warped.transform(
        (width + extra, height),
        Image.Transform.AFFINE,
        (1, shear, -extra // 2, 0, 1, 0),
        resample=Image.Resampling.BICUBIC,
    )
    return sheared


def _wave_image(image: Image.Image, rng: random.Random) -> Image.Image:
    width, height = image.size
    amplitude = rng.randint(3, 5)
    period = rng.randint(36, 56)
    phase = rng.random() * math.pi * 2
    result = Image.new(image.mode, image.size)
    for y in range(height):
        shift = int(amplitude * math.sin((2 * math.pi * y / period) + phase))
        row = image.crop((0, y, width, y + 1))
        result.paste(row, (shift, y))
        if shift > 0:
            result.paste(row.crop((width - shift, 0, width, 1)), (0, y))
        elif shift < 0:
            result.paste(row.crop((0, 0, -shift, 1)), (width + shift, y))
    return result.filter(ImageFilter.SMOOTH)


def _render_challenge_image(glyphs: list[str], rng: random.Random) -> bytes:
    width, height = 520, 196
    font_size = 68 if len(glyphs) <= 5 else 52
    image = _paper_background(width, height, rng).convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_clutter(draw, width, height, rng)

    layers = [_glyph_layer(symbol, rng, font_size) for symbol in glyphs]
    gap = 4
    total_w = sum(layer.width for layer in layers) + gap * max(0, len(layers) - 1)
    strip = Image.new("RGBA", (max(total_w, 8), height), (0, 0, 0, 0))
    cursor = 0
    baseline = height // 2
    for index, layer in enumerate(layers):
        paste_y = baseline - layer.height // 2 + int(5 * math.sin(index * 0.9)) + rng.randint(-3, 3)
        strip.paste(layer, (cursor, paste_y), layer)
        cursor += layer.width + gap
    fit_w = width - 36
    if strip.width > fit_w:
        ratio = fit_w / strip.width
        strip = strip.resize((fit_w, max(40, int(strip.height * ratio))), Image.Resampling.BICUBIC)
    paste_x = max(18, (width - strip.width) // 2)
    paste_y = max(8, (height - strip.height) // 2)
    image.paste(strip, (paste_x, paste_y), strip)

    color = rng.choice(_INK) + (80,)
    y_mid = int(height * 0.64) + rng.randint(-4, 4)
    period = rng.randint(22, 34)
    points = [
        (x, y_mid + int(7 * math.sin(x / period)))
        for x in range(16, width - 16, 8)
    ]
    draw.line(points, fill=color, width=2)

    image = _wave_image(image, rng)
    rgb = image.convert("RGB")
    speck = rgb.load()
    for _ in range(380):
        x = rng.randint(0, width - 1)
        y = rng.randint(0, height - 1)
        noise = rng.randint(-14, 14)
        pixel = speck[x, y]
        speck[x, y] = (
            max(0, min(255, pixel[0] + noise)),
            max(0, min(255, pixel[1] + noise)),
            max(0, min(255, pixel[2] + noise)),
        )

    border = ImageDraw.Draw(rgb)
    border.rectangle((2, 2, width - 3, height - 3), outline=(92, 78, 58), width=2)
    buffer = BytesIO()
    rgb.save(buffer, format="PNG")
    return buffer.getvalue()
