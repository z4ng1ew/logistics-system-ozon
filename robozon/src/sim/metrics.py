"""
Сбор и экспорт метрик модели.

Критерий 5.1 (до 15 баллов): «метрики по каждой операции и по системе
в целом... на различных временных интервалах, в том числе за 1 минуту,
1 час, 12 часов и 24 часа». Поэтому базовый бакет — 1 минута,
агрегаты 1ч/12ч/24ч считаются из минутных.

Модуль не зависит от SimPy — его можно тестировать отдельно.
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict


AGG_WINDOWS_MIN = {"1min": 1, "1h": 60, "12h": 720, "24h": 1440}


class Metrics:
    def __init__(self) -> None:
        # counts[op][minute] = сколько единиц прошло через операцию за минуту
        self.counts: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        # queues[name] = список (minute, длина очереди) — периодические замеры
        self.queues: dict[str, list[tuple[int, float]]] = defaultdict(list)
        # busy[resource] = суммарные занятые ресурсо-секунды
        self.busy: dict[str, float] = defaultdict(float)
        self.capacity: dict[str, int] = {}
        # произвольные наблюдения (например, заполненность закрытых КТЯ)
        self.observations: dict[str, list[float]] = defaultdict(list)

    # ---------- запись ----------
    def count(self, op: str, n: float, t_sec: float) -> None:
        self.counts[op][int(t_sec // 60)] += n

    def sample_queue(self, name: str, length: float, t_sec: float) -> None:
        self.queues[name].append((int(t_sec // 60), float(length)))

    def add_busy(self, resource: str, sec: float) -> None:
        self.busy[resource] += sec

    def set_capacity(self, resource: str, cap: int) -> None:
        self.capacity[resource] = cap

    def observe(self, name: str, value: float) -> None:
        self.observations[name].append(float(value))

    # ---------- расчёты ----------
    def utilization(self, horizon_sec: float) -> dict[str, float]:
        out = {}
        for res, busy in self.busy.items():
            cap = self.capacity.get(res, 1)
            out[res] = busy / (cap * horizon_sec) if horizon_sec > 0 else 0.0
        return out

    def totals(self) -> dict[str, float]:
        return {op: sum(mins.values()) for op, mins in self.counts.items()}

    # ---------- экспорт ----------
    def export(self, outdir: str, horizon_sec: float, extra_summary: dict | None = None) -> None:
        os.makedirs(outdir, exist_ok=True)

        # 1) Поминутные счётчики по операциям
        with open(os.path.join(outdir, "operations_minutely.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["minute", "operation", "count"])
            for op in sorted(self.counts):
                for minute in sorted(self.counts[op]):
                    w.writerow([minute, op, round(self.counts[op][minute], 3)])

        # 2) Агрегаты по окнам 1мин/1ч/12ч/24ч (сумма и средний темп в час)
        with open(os.path.join(outdir, "operations_agg.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["window", "window_index", "operation", "count", "rate_per_hour"])
            for wname, wmin in AGG_WINDOWS_MIN.items():
                for op in sorted(self.counts):
                    bucket: dict[int, float] = defaultdict(float)
                    for minute, c in self.counts[op].items():
                        bucket[minute // wmin] += c
                    for idx in sorted(bucket):
                        cnt = bucket[idx]
                        w.writerow([wname, idx, op, round(cnt, 3), round(cnt * 60.0 / wmin, 1)])

        # 3) Очереди (поминутно: среднее по замерам внутри минуты)
        with open(os.path.join(outdir, "queues.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["minute", "queue", "avg_length", "max_length"])
            for name, samples in sorted(self.queues.items()):
                per_min: dict[int, list[float]] = defaultdict(list)
                for minute, ln in samples:
                    per_min[minute].append(ln)
                for minute in sorted(per_min):
                    vals = per_min[minute]
                    w.writerow([minute, name, round(sum(vals) / len(vals), 2), max(vals)])

        # 4) Утилизация ресурсов
        util = self.utilization(horizon_sec)
        with open(os.path.join(outdir, "utilization.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["resource", "capacity", "busy_resource_sec", "utilization"])
            for res in sorted(util):
                w.writerow([res, self.capacity.get(res, 1),
                            round(self.busy[res], 1), round(util[res], 4)])

        # 5) Сводка + наблюдения
        summary = {
            "horizon_sec": horizon_sec,
            "totals": self.totals(),
            "utilization": {k: round(v, 4) for k, v in util.items()},
            "observations_avg": {
                k: (round(sum(v) / len(v), 3) if v else None)
                for k, v in self.observations.items()
            },
        }
        if extra_summary:
            summary.update(extra_summary)
        with open(os.path.join(outdir, "summary.json"), "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
