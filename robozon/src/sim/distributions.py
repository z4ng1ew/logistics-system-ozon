"""
Распределение потока по направлениям и балансировка сортеров.

По ТЗ: направлений 400, при этом на 20% направлений может приходиться
до 80% общего объёма (неравномерность типа Парето).

Моделируем веса направлений законом Ципфа (Zipf): w_d ~ 1/d^s,
где показатель s подбирается численно так, чтобы доля потока
на top-20% направлений была равна заданной (по умолчанию 80%).
"""

from __future__ import annotations

import numpy as np


def _top_share(weights: np.ndarray, top_frac: float) -> float:
    """Доля суммарного веса, приходящаяся на top_frac самых крупных направлений."""
    w = np.sort(weights)[::-1]
    k = max(1, int(round(len(w) * top_frac)))
    return float(w[:k].sum() / w.sum())


def make_direction_weights(
    n_directions: int = 400,
    top_frac: float = 0.20,
    top_share: float = 0.80,
    tol: float = 1e-4,
) -> np.ndarray:
    """
    Подбирает показатель Ципфа бинарным поиском и возвращает
    нормированные веса направлений (сумма = 1).
    """
    lo, hi = 0.0, 5.0
    ranks = np.arange(1, n_directions + 1, dtype=float)
    weights = ranks ** 0.0
    for _ in range(100):
        s = (lo + hi) / 2.0
        weights = ranks ** (-s)
        share = _top_share(weights, top_frac)
        if abs(share - top_share) < tol:
            break
        if share < top_share:
            lo = s  # нужно «острее» распределение
        else:
            hi = s
    return weights / weights.sum()


def balance_directions(
    weights: np.ndarray, n_sorters: int
) -> tuple[np.ndarray, np.ndarray]:
    """
    Раскладывает направления по сортерам жадным алгоритмом LPT
    (Longest Processing Time: самые тяжёлые направления первыми
    в наименее загруженный сортер).

    Возвращает:
      assignment — массив длины n_directions: номер сортера для направления
      loads      — суммарная доля потока на каждый сортер
    Балансировка нужна, потому что поток неравномерен (Парето):
    без неё один сортер захлебнётся, а остальные будут простаивать.
    v0.2: заменить на оптимум через Google OR-Tools (задача T-08).
    """
    n = len(weights)
    assignment = np.zeros(n, dtype=int)
    loads = np.zeros(n_sorters, dtype=float)
    for d in np.argsort(weights)[::-1]:
        s = int(np.argmin(loads))
        assignment[d] = s
        loads[s] += weights[d]
    return assignment, loads


def balance_directions_split(
    weights: np.ndarray, n_sorters: int, chunk_cap: float | None = None
) -> tuple[list[list[tuple[int, float]]], np.ndarray]:
    """
    Балансировка С ДРОБЛЕНИЕМ тяжёлых направлений между сортерами.

    Инженерная причина: при Парето 80/20 на 400 направлений вес топ-направления
    ~18.6% потока = ~18 600 товаров/час, что ПРЕВЫШАЕТ производительность
    одного сортера (10 000/час, ТЗ). Жёсткая привязка «направление → один
    сортер» физически нереализуема: тяжёлым направлениям нужны выходные
    скаты на нескольких сортерах (динамическое назначение скатов).

    Возвращает:
      route — для каждого направления список (сортер, доля потока направления)
      loads — итоговая доля общего потока на каждый сортер
    """
    if chunk_cap is None:
        chunk_cap = 1.0 / n_sorters  # кусок не крупнее средней загрузки сортера
    chunks: list[tuple[int, float]] = []
    for d, w in enumerate(weights):
        rem = float(w)
        while rem > chunk_cap + 1e-12:
            chunks.append((d, chunk_cap))
            rem -= chunk_cap
        if rem > 1e-15:
            chunks.append((d, rem))

    loads = np.zeros(n_sorters, dtype=float)
    route_raw: list[list[tuple[int, float]]] = [[] for _ in range(len(weights))]
    for d, w in sorted(chunks, key=lambda x: -x[1]):
        s = int(np.argmin(loads))
        loads[s] += w
        route_raw[d].append((s, w))

    route: list[list[tuple[int, float]]] = []
    for lst in route_raw:
        tot = sum(w for _, w in lst)
        route.append([(s, w / tot) for s, w in lst])
    return route, loads


def sample_directions(
    rng: np.random.Generator, weights: np.ndarray, n_items: int
) -> np.ndarray:
    """Направления для n_items товаров: массив счётчиков длины n_directions."""
    return rng.multinomial(n_items, weights)
