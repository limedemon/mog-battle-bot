"""Подсчёт баллов баттла и подпись под карточкой.

Шкала абсолютная: балл зависит только от самого игрока, а не от соперника.
Если нормировать на лучшего из двоих, ведущий всегда получает ровно 10.00,
и вся таблица превращается в столбик одинаковых десяток.
"""
from __future__ import annotations

import math

# Ключ, подпись, ориентир на 10 баллов, тип шкалы.
#
# «log» — логарифм: первые успехи дают много баллов, дальше рост замедляется,
# так что новичок не выглядит нулём, а ветеран не упирается в потолок.
# «days» — тот же логарифм, но от суток. «percent» — доля 0..1 прямо в баллы.
METRICS = (
    ("msgs_today", "Сообщений сегодня", 120, "log"),
    ("msgs_week", "За неделю", 600, "log"),
    ("msgs_month", "За месяц", 2200, "log"),
    ("msgs_total", "Всего сообщений", 15000, "log"),
    ("culture", "Культурность", 0, "percent"),
    ("literacy", "Грамотность", 0, "percent"),
    ("age", "Время в чате", 365, "days"),
)

# Балл для показателя, который ещё не на чем посчитать (мало сообщений).
NEUTRAL = 5.0


def format_compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}кк".replace(".0", "")
    if value >= 1000:
        return f"{value / 1000:.1f}к".replace(".0", "")
    return str(value)


def format_age(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    if days:
        return f"{days} д"
    if hours:
        return f"{hours} ч"
    return f"{max(1, rest // 60)} мин"


def value_text(key: str, kind: str, stats: dict) -> str:
    value = stats.get(key)
    if kind == "percent":
        return "—" if value is None else f"{value * 100:.0f}%"
    if kind == "days":
        return format_age(int(value or 0))
    return format_compact(int(value or 0))


def metric_score(key: str, ref: int, kind: str, stats: dict) -> float:
    value = stats.get(key)
    if kind == "percent":
        return NEUTRAL if value is None else max(0.0, min(10.0, 10.0 * value))
    value = (value or 0) / 86400 if kind == "days" else (value or 0)
    if value <= 0:
        return 0.0
    return min(10.0, 10.0 * math.log1p(value) / math.log1p(ref))


def score_battle(left: dict, right: dict) -> tuple[list[dict], float, float]:
    """Каждый показатель оценивается сам по себе, соперник на балл не влияет.
    Итог — среднее по строкам."""
    rows, left_total, right_total = [], 0.0, 0.0
    for key, label, ref, kind in METRICS:
        score_a = metric_score(key, ref, kind, left)
        score_b = metric_score(key, ref, kind, right)
        left_total += score_a
        right_total += score_b
        rows.append({
            "label": label,
            "left_score": score_a, "right_score": score_b,
            "left_text": value_text(key, kind, left),
            "right_text": value_text(key, kind, right),
        })
    count = len(METRICS)
    return rows, left_total / count, right_total / count


def battle_caption(
    left_label: str, right_label: str, rows: list[dict],
    left_total: float, right_total: float,
) -> str:
    """Подпись под карточкой: кто кого, счёт, за счёт чего выиграл и насколько
    плотно. Названия показателей берём из строк баттла, чтобы не расходились."""
    diff = abs(left_total - right_total)
    if diff < 0.005:
        head = f"🤝 <b>{left_label}</b> и <b>{right_label}</b> — ничья"
    elif left_total > right_total:
        head = f"🏆 <b>{left_label}</b> mogged <b>{right_label}</b>"
    else:
        head = f"🏆 <b>{right_label}</b> mogged <b>{left_label}</b>"

    winner = "left" if left_total >= right_total else "right"
    loser = "right" if winner == "left" else "left"
    gaps = sorted(rows, key=lambda row: row[f"{winner}_score"] - row[f"{loser}_score"], reverse=True)
    strong = [row["label"].lower() for row in gaps
              if row[f"{winner}_score"] > row[f"{loser}_score"]][:2]

    if not strong:
        reason = "Ни один показатель не дал перевеса."
    elif len(strong) == 1:
        reason = f"Сильнее всего выглядит <b>{strong[0]}</b>."
    else:
        reason = f"Сильнее всего выглядят <b>{strong[0]}</b> и <b>{strong[1]}</b>."

    if diff < 0.005:
        verdict = "Равные соперники."
    elif diff < 0.75:
        verdict = "Плотный баттл."
    elif diff < 2.5:
        verdict = "Уверенная победа."
    else:
        verdict = "Разгром."

    return (
        f"{head}\n"
        f"<blockquote>📊 {max(left_total, right_total):.2f} vs "
        f"{min(left_total, right_total):.2f} · +{diff:.2f}</blockquote>\n"
        f"{reason} {verdict}"
    )
