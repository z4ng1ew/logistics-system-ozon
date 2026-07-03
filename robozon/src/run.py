"""
Запуск имитационной модели сортировочного центра.

Примеры:
  python -m src.run --config config.yaml --out data/out
  python -m src.run --hours 2 --seed 7          # быстрый смоук-тест
  python -m src.run --set sorters.count=10      # сценарий «без резерва»
"""

from __future__ import annotations

import argparse
import json
import time

import yaml

from src.sim.model import SortingCenterModel


def apply_overrides(cfg: dict, overrides: list[str]) -> None:
    """--set a.b=value (число/строка) — точечные изменения конфига."""
    for item in overrides:
        path, _, raw = item.partition("=")
        keys = path.strip().split(".")
        node = cfg
        for k in keys[:-1]:
            node = node[k]
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        node[keys[-1]] = val


def main() -> None:
    p = argparse.ArgumentParser(description="Имитационная модель СЦ (Робозон, Задача 1)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--out", default="data/out")
    p.add_argument("--hours", type=float, default=None, help="переопределить горизонт")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--set", action="append", default=[], metavar="key.path=value")
    args = p.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if args.hours is not None:
        cfg["simulation"]["hours"] = args.hours
    if args.seed is not None:
        cfg["simulation"]["seed"] = args.seed
    apply_overrides(cfg, args.set)

    t0 = time.time()
    model = SortingCenterModel(cfg).build()
    summary = model.run()
    horizon = cfg["simulation"]["hours"] * 3600.0
    model.m.export(args.out, horizon, extra_summary={"config": cfg, **summary})
    dt = time.time() - t0

    b = summary["balance"]
    print(f"Готово за {dt:.1f} с. Горизонт: {cfg['simulation']['hours']} ч.")
    print(f"Товаров вошло:   {b['items_in']:>10,}")
    print(f"Отсортировано:   {b['items_sorted']:>10,}")
    print(f"Nonsort:         {b['items_nonsort']:>10,}")
    print(f"Отгружено:       {b['items_shipped']:>10,}")
    print(f"WIP после сортировки: {summary['wip_items_after_sorting']:,}")
    check = b["items_in"] - b["items_sorted"] - b["items_nonsort"]
    print(f"Сверка (вход − сортировка − nonsort, ожидаем ~0 + очереди): {check:,}")
    util = model.m.utilization(horizon)
    top = sorted(util.items(), key=lambda kv: -kv[1])[:5]
    print("Топ-5 загрузки ресурсов:", ", ".join(f"{k}={v:.0%}" for k, v in top))
    print(f"Метрики выгружены в: {args.out}/")


if __name__ == "__main__":
    main()
