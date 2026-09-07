"""Отрисовка карточек баттла (Pillow).

Всё синхронное — вызывать только через asyncio.to_thread, иначе Pillow вешает
бота на каждый вызов инлайна.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).with_name("fonts")
W = 900

BG = (12, 20, 34)
PANEL = (22, 33, 54)
LINE = (38, 52, 78)
TEXT = (226, 234, 246)
DIM = (128, 145, 172)
WIN = (74, 222, 128)
LOSE = (248, 113, 113)


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # Шрифт лежит рядом с кодом: на хостинге системных с кириллицей может не быть.
    path = FONT_DIR / ("Bold.ttf" if bold else "Regular.ttf")
    return ImageFont.truetype(str(path), size)


def fit(text: str, source: ImageFont.FreeTypeFont, max_width: int) -> str:
    if source.getlength(text) <= max_width:
        return text
    while text and source.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def circle_avatar(path: Path | None, size: int, initial: str) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    if path is not None:
        try:
            photo = Image.open(path).convert("RGB")
            side = min(photo.size)
            left, top = (photo.width - side) // 2, (photo.height - side) // 2
            photo = photo.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
            canvas.paste(photo, (0, 0), mask)
            return canvas
        except OSError:
            pass
    plug = Image.new("RGB", (size, size), (58, 74, 102))
    ImageDraw.Draw(plug).text((size // 2, size // 2), initial, font=font(size // 2, bold=True),
                              fill=(200, 214, 236), anchor="mm")
    canvas.paste(plug, (0, 0), mask)
    return canvas


def _panel(image, draw, x, y, width, height, name, username, avatar,
           total, rows, side, accent, mogged) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=18, fill=PANEL,
                           outline=accent, width=2)
    image.paste(avatar, (x + 16, y + 16), avatar)
    draw.text((x + 92, y + 20), fit(name, font(21, bold=True), width - 200),
              font=font(21, bold=True), fill=TEXT)
    draw.text((x + 92, y + 48), fit(username, font(15), width - 200), font=font(15), fill=DIM)
    draw.text((x + width - 18, y + 26), f"{total:.2f}", font=font(34, bold=True),
              fill=accent, anchor="ra")

    row_y = y + 96
    for row in rows:
        score = row[f"{side}_score"]
        draw.text((x + 16, row_y), row["label"], font=font(14), fill=DIM)
        # Справа два числа: сам показатель и сколько баллов он принёс.
        draw.text((x + width - 18, row_y), f"+{score:.2f}", font=font(14, bold=True),
                  fill=accent, anchor="ra")
        draw.text((x + width - 92, row_y), row[f"{side}_text"], font=font(14, bold=True),
                  fill=TEXT, anchor="ra")
        bar_y = row_y + 22
        draw.rounded_rectangle((x + 16, bar_y, x + width - 18, bar_y + 9), radius=5, fill=LINE)
        filled = int((width - 34) * max(score, 0.4) / 10)
        draw.rounded_rectangle((x + 16, bar_y, x + 16 + filled, bar_y + 9), radius=5, fill=accent)
        row_y += 40

    if mogged:
        stamp = Image.new("RGBA", (300, 90), (0, 0, 0, 0))
        stamp_draw = ImageDraw.Draw(stamp)
        stamp_draw.rounded_rectangle((4, 4, 296, 86), radius=10, outline=LOSE, width=5)
        stamp_draw.text((150, 45), "MOGGED", font=font(44, bold=True), fill=LOSE, anchor="mm")
        stamp = stamp.rotate(14, expand=True, resample=Image.BICUBIC)
        image.paste(stamp, (x + (width - stamp.width) // 2, y + (height - stamp.height) // 2), stamp)


def battle_card(left_name, left_username, left_avatar,
                right_name, right_username, right_avatar,
                rows, left_total, right_total) -> bytes:
    # Высота считается от числа строк, а не константой: показателей может стать
    # больше. Между низом панелей и полосой победителя оставлен воздух —
    # вплотную они сливаются в одно пятно.
    panel_top = 92
    panel_h = 96 + len(rows) * 40 - 6
    footer_top = panel_top + panel_h + 28
    height = footer_top + 46 + 20

    image = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, W - 14, height - 14), radius=22, outline=LINE, width=2)
    draw.text((W // 2, 40), "MOG BATTLE", font=font(30, bold=True), fill=TEXT, anchor="mm")
    draw.text((W // 2, 66), "RESULT", font=font(13), fill=DIM, anchor="mm")

    left_won = left_total >= right_total
    width = 386
    _panel(image, draw, 34, panel_top, width, panel_h, left_name, left_username,
           circle_avatar(left_avatar, 64, left_name[:1].upper() or "?"),
           left_total, rows, "left", WIN if left_won else LOSE, not left_won)
    _panel(image, draw, W - 34 - width, panel_top, width, panel_h, right_name, right_username,
           circle_avatar(right_avatar, 64, right_name[:1].upper() or "?"),
           right_total, rows, "right", WIN if not left_won else LOSE, left_won)
    draw.text((W // 2, panel_top + panel_h // 2), "VS", font=font(22, bold=True), fill=DIM, anchor="mm")

    winner = left_name if left_won else right_name
    middle = footer_top + 23
    draw.rounded_rectangle((34, footer_top, W - 34, footer_top + 46), radius=14, fill=PANEL)
    draw.text((52, middle), f"WINNER  {fit(winner, font(16, bold=True), 260)}",
              font=font(16, bold=True), fill=WIN, anchor="lm")
    draw.text((W // 2, middle), f"{left_total:.2f}  VS  {right_total:.2f}",
              font=font(17, bold=True), fill=TEXT, anchor="mm")
    draw.text((W - 52, middle), f"Разрыв  +{abs(left_total - right_total):.2f}",
              font=font(15), fill=DIM, anchor="rm")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def challenge_card(name: str, username: str, avatar: Path | None,
                   target: str | None = None) -> bytes:
    image = Image.new("RGB", (W, 300), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, W - 14, 286), radius=22, outline=LINE, width=2)
    draw.text((W // 2, 52), "MOG BATTLE", font=font(30, bold=True), fill=TEXT, anchor="mm")

    face = circle_avatar(avatar, 96, name[:1].upper() or "?")
    image.paste(face, (250, 100), face)
    draw.text((298, 214), fit(name, font(18, bold=True), 220), font=font(18, bold=True),
              fill=TEXT, anchor="mm")
    draw.text((298, 238), fit(username, font(14), 220), font=font(14), fill=DIM, anchor="mm")

    draw.ellipse((554, 100, 650, 196), fill=PANEL, outline=LINE, width=2)
    draw.text((602, 148), "?", font=font(48, bold=True), fill=DIM, anchor="mm")
    draw.text((602, 214), fit(f"@{target}" if target else "любой игрок", font(18, bold=True), 240),
              font=font(18, bold=True), fill=DIM, anchor="mm")

    draw.text((W // 2, 148), "VS", font=font(22, bold=True), fill=DIM, anchor="mm")
    footer = f"Вызов для @{target} — принять может только он" if target else "Кто готов принять вызов?"
    draw.text((W // 2, 272), fit(footer, font(15), W - 80), font=font(15), fill=DIM, anchor="mm")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()
