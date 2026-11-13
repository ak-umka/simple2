"""Пересчёт долей офферов в потоке.

Закреплённые (pinned) офферы сохраняют свою долю, остаток до 100%
делится поровну между незакреплёнными. Остаток от целочисленного деления
раздаётся по одному проценту первым офферам, чтобы сумма была ровно 100.
"""
from typing import List, Sequence


class WeightError(ValueError):
    pass


def distribute(pinned_shares: Sequence[int], unpinned_count: int) -> List[int]:
    """Возвращает доли для незакреплённых офферов."""
    pinned_total = sum(pinned_shares)
    if pinned_total > 100:
        raise WeightError(f"Сумма закреплённых долей {pinned_total}% больше 100%")
    if unpinned_count == 0:
        return []
    rest = 100 - pinned_total
    base, extra = divmod(rest, unpinned_count)
    return [base + (1 if i < extra else 0) for i in range(unpinned_count)]
