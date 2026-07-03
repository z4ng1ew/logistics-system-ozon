"""
Имитационная модель сортировочного центра (СЦ) — ядро v0.1.

Единица моделирования — КТЯ (картонный тарный ящик), товары учитываются
счётчиками внутри КТЯ. Это осознанный компромисс производительности:
100 000 товаров/час = ~3 700 КТЯ/час, сутки моделируются за десятки секунд.
(см. docs/assumptions.md, A-01)

Цепочка процессов (по ТЗ, раздел «Модель должна отражать полный набор...»):

  ворота приёмки → буфер приёмки (185 палет/ч)
    → депаллетизация (стрейч, лента, снятие КТЯ)
    → транспортировка КТЯ конвейером
    → вскрытие КТЯ
    → инфид: перекладка товаров в сортировочную систему
        ├─ отщеп nonsort-потока → ручная сортировка → роллкейджи
        └─ пустая тара: 80% в оборот / 20% брак (+ машина новых коробов)
    → автосортировка по 400 направлениям (N сортеров, балансировка Парето)
    → скаты: наполнение выходных КТЯ (закрытие по 27 шт или таймауту)
    → заклейка → транспортировка → буфер отгрузки
    → палетизация (20 КТЯ/палета)
    → ворота отгрузки (24 шт, машина: 16 палет КТЯ + 16 роллкейджей, 2 ч)
"""

from __future__ import annotations

import numpy as np
import simpy

from .distributions import (
    balance_directions,
    balance_directions_split,
    make_direction_weights,
    sample_directions,
)
from .metrics import Metrics

# Человекочитаемые подписи операций (для дашборда и отчёта)
OPERATION_LABELS = {
    "receiving_pallets": "Приёмка палет в буфер",
    "depalletizing_pallets": "Депаллетизация (вскрытие палет)",
    "transport_in_boxes": "Транспортировка КТЯ к сортировке",
    "box_opening": "Вскрытие КТЯ",
    "infeed_boxes": "Подача КТЯ на инфид",
    "infeed_items": "Перекладка товаров в сортировку",
    "nonsort_items": "Отщеп nonsort-потока",
    "sorted_items": "Автосортировка товаров",
    "outbound_boxes_closed": "Закрытие выходных КТЯ",
    "boxes_closed_by_timeout": "Закрытие КТЯ по таймауту",
    "tare_reused": "Тара: повторное использование",
    "tare_new": "Тара: новые короба",
    "tare_scrapped": "Тара: брак (вывоз)",
    "sealing_boxes": "Заклейка КТЯ",
    "transport_out_boxes": "Транспортировка к отгрузке",
    "palletizing_boxes": "Палетизация КТЯ",
    "pallets_built": "Сформировано палет",
    "trucks_loaded": "Погружено машин",
    "shipped_boxes": "Отгружено КТЯ",
    "shipped_items": "Отгружено товаров",
    "manual_sorted_items": "Ручная сортировка nonsort",
    "rollcages_built": "Сформировано роллкейджей",
}


class SortingCenterModel:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.env = simpy.Environment()
        self.rng = np.random.default_rng(cfg["simulation"]["seed"])
        self.m = Metrics()

        f = cfg["flow"]
        self.items_per_box = int(f["items_per_box"])

        # --- направления: веса Парето и балансировка по сортерам ---
        self.weights = make_direction_weights(
            f["directions"], f["pareto_top_frac"], f["pareto_top_share"]
        )
        self.n_sorters = int(cfg["sorters"]["count"])
        self.assignment_mode = cfg["sorters"].get("assignment_mode", "split")
        if self.assignment_mode == "split":
            # направление может обслуживаться скатами на нескольких сортерах
            self.route, self.sorter_loads = balance_directions_split(
                self.weights, self.n_sorters
            )
        else:  # strict: жёсткая привязка (демонстрирует перегрузку топ-направлениями)
            dir2sorter, self.sorter_loads = balance_directions(self.weights, self.n_sorters)
            self.route = [[(int(s), 1.0)] for s in dir2sorter]

        # --- очереди/буферы (simpy.Store) ---
        e = self.env
        self.receiving_buffer = simpy.Store(e, capacity=cfg["receiving"]["buffer_pallets"])
        self.opening_queue = simpy.Store(e, capacity=cfg["transport_in"]["line_capacity_boxes"])
        self.infeed_queue = simpy.Store(e, capacity=cfg["transport_in"]["line_capacity_boxes"])
        self.sorter_queues = [simpy.Store(e) for _ in range(self.n_sorters)]
        self.sealing_queue = simpy.Store(e)
        self.shipping_buffer = simpy.Store(e)   # заклеенные КТЯ до палетизации
        self.pallets_store = simpy.Store(e)     # готовые палеты у ворот
        self.nonsort_queue = simpy.Store(e)     # пачки nonsort-товаров

        # --- ресурсы (simpy.Resource) ---
        r = simpy.Resource
        self.depal = r(e, capacity=cfg["depalletizing"]["robots"])
        self.openers = r(e, capacity=cfg["box_opening"]["stations"])
        self.infeed = r(e, capacity=cfg["infeed"]["stations"])
        self.sealers = r(e, capacity=cfg["sealing"]["stations"])
        self.palletizers = r(e, capacity=cfg["palletizing"]["robots"])
        self.gates = r(e, capacity=cfg["shipping"]["gates"])
        self.manual_workers = r(e, capacity=cfg["manual_sort"]["workers"])

        for name, res in [
            ("depalletizing", self.depal), ("box_opening", self.openers),
            ("infeed", self.infeed), ("sealing", self.sealers),
            ("palletizing", self.palletizers), ("gates", self.gates),
            ("manual_sort", self.manual_workers),
        ]:
            self.m.set_capacity(name, res.capacity)
        self.m.set_capacity("sorters", self.n_sorters)

        # --- состояние ---
        self.tare_available = 0          # пул пустых КТЯ на повторное использование
        self.open_chutes: dict[int, dict] = {}  # направление -> {qty, opened_at}
        self.rollcage_fill = 0
        self.box_seq = 0
        self.balance = {                 # сквозной баланс для сверки
            "items_in": 0, "items_sorted": 0, "items_nonsort": 0,
            "items_shipped": 0, "boxes_in": 0, "boxes_shipped": 0,
        }

    # ================= процессы =================

    def pallet_arrivals(self):
        cfg = self.cfg
        mean = 3600.0 / cfg["flow"]["pallets_per_hour"]
        pattern = cfg["receiving"].get("arrival_pattern", "uniform")
        while True:
            dt = mean if pattern == "uniform" else self.rng.exponential(mean)
            yield self.env.timeout(dt)
            yield self.receiving_buffer.put({"t_in": self.env.now})
            self.m.count("receiving_pallets", 1, self.env.now)

    def depal_robot(self):
        cfg = self.cfg
        t_service = cfg["depalletizing"]["sec_per_pallet"]
        boxes_per_pallet = cfg["flow"]["boxes_per_pallet"]
        while True:
            pallet = yield self.receiving_buffer.get()
            with self.depal.request() as req:
                yield req
                yield self.env.timeout(t_service)
                self.m.add_busy("depalletizing", t_service)
            self.m.count("depalletizing_pallets", 1, self.env.now)
            for _ in range(boxes_per_pallet):
                self.balance["boxes_in"] += 1
                self.env.process(self.conveyor_in())

    def conveyor_in(self):
        t = self.cfg["transport_in"]["sec"]
        yield self.env.timeout(t)
        yield self.opening_queue.put({"t": self.env.now})
        self.m.count("transport_in_boxes", 1, self.env.now)

    def opener_station(self):
        t = self.cfg["box_opening"]["sec_per_box"]
        while True:
            box = yield self.opening_queue.get()
            with self.openers.request() as req:
                yield req
                yield self.env.timeout(t)
                self.m.add_busy("box_opening", t)
            self.m.count("box_opening", 1, self.env.now)
            yield self.infeed_queue.put(box)

    def infeed_station(self):
        cfg = self.cfg
        mode = cfg["infeed"]["mode"]
        t_item = (cfg["infeed"]["sec_per_item_auto"] if mode == "auto"
                  else cfg["infeed"]["sec_per_item_manual"])
        p_nonsort = cfg["flow"]["p_nonsort"]
        reuse = cfg["tare"]["reuse_share"]
        while True:
            _box = yield self.infeed_queue.get()
            n_items = self.items_per_box
            t_service = n_items * t_item
            with self.infeed.request() as req:
                yield req
                yield self.env.timeout(t_service)
                self.m.add_busy("infeed", t_service)

            self.balance["items_in"] += n_items
            self.m.count("infeed_boxes", 1, self.env.now)
            self.m.count("infeed_items", n_items, self.env.now)

            # nonsort-отщеп (форма/хрупкость/нечитаемый штрихкод)
            k_nonsort = int(self.rng.binomial(n_items, p_nonsort))
            if k_nonsort:
                self.balance["items_nonsort"] += k_nonsort
                self.m.count("nonsort_items", k_nonsort, self.env.now)
                yield self.nonsort_queue.put(k_nonsort)

            # сортопригодные товары: направления -> батчи на сортеры
            sortable = n_items - k_nonsort
            if sortable:
                dir_counts = sample_directions(self.rng, self.weights, sortable)
                per_sorter: dict[int, list[tuple[int, int]]] = {}
                nz = np.nonzero(dir_counts)[0]
                for d in nz:
                    cnt = int(dir_counts[d])
                    targets = self.route[int(d)]
                    if len(targets) == 1:
                        s = targets[0][0]
                        per_sorter.setdefault(s, []).append((int(d), cnt))
                    else:  # тяжёлое направление: дробим поток по его сортерам
                        fracs = np.array([f for _, f in targets])
                        parts = self.rng.multinomial(cnt, fracs / fracs.sum())
                        for (s, _), q in zip(targets, parts):
                            if q:
                                per_sorter.setdefault(s, []).append((int(d), int(q)))
                for s, batch in per_sorter.items():
                    yield self.sorter_queues[s].put(batch)

            # контур тары: пустой входящий КТЯ
            if self.rng.random() < reuse:
                self.tare_available += 1
                self.m.count("tare_reused", 1, self.env.now)
            else:
                self.m.count("tare_scrapped", 1, self.env.now)

    def sorter(self, s: int):
        rate = self.cfg["sorters"]["items_per_hour_each"] / 3600.0  # товаров/сек
        while True:
            batch = yield self.sorter_queues[s].get()
            qty = sum(q for _, q in batch)
            t_service = qty / rate
            yield self.env.timeout(t_service)
            self.m.add_busy("sorters", t_service)
            self.m.count("sorted_items", qty, self.env.now)
            self.m.count(f"sorted_items_s{s:02d}", qty, self.env.now)
            self.balance["items_sorted"] += qty
            for d, q in batch:
                self._chute_add(d, q)

    # --- скаты: наполнение выходных КТЯ по направлениям ---

    def _take_tare(self):
        if self.tare_available > 0:
            self.tare_available -= 1
        else:
            self.m.count("tare_new", 1, self.env.now)  # машина новых коробов

    def _close_chute(self, d: int, by_timeout: bool = False):
        ch = self.open_chutes.pop(d, None)
        if not ch or ch["qty"] <= 0:
            return
        self._take_tare()
        self.box_seq += 1
        self.m.count("outbound_boxes_closed", 1, self.env.now)
        if by_timeout:
            self.m.count("boxes_closed_by_timeout", 1, self.env.now)
        self.m.observe("outbound_box_fill", ch["qty"])
        self.sealing_queue.put({"direction": d, "items": ch["qty"]})

    def _chute_add(self, d: int, q: int):
        target = self.cfg["chutes"]["fill_target_items"]
        ch = self.open_chutes.setdefault(d, {"qty": 0, "opened_at": self.env.now})
        ch["qty"] += q
        while ch["qty"] >= target:
            full, rest = target, ch["qty"] - target
            ch["qty"] = full
            self._close_chute(d)
            if rest > 0:
                ch = self.open_chutes.setdefault(d, {"qty": 0, "opened_at": self.env.now})
                ch["qty"] = rest
            else:
                break

    def chute_timeout_sweep(self):
        sweep = self.cfg["simulation"]["chute_sweep_sec"]
        timeout = self.cfg["chutes"]["fill_timeout_min"] * 60.0
        while True:
            yield self.env.timeout(sweep)
            now = self.env.now
            for d in [d for d, ch in self.open_chutes.items()
                      if ch["qty"] > 0 and now - ch["opened_at"] >= timeout]:
                self._close_chute(d, by_timeout=True)

    # --- заклейка, палетизация, отгрузка ---

    def sealer_station(self):
        t = self.cfg["sealing"]["sec_per_box"]
        while True:
            box = yield self.sealing_queue.get()
            with self.sealers.request() as req:
                yield req
                yield self.env.timeout(t)
                self.m.add_busy("sealing", t)
            self.m.count("sealing_boxes", 1, self.env.now)
            self.env.process(self.conveyor_out(box))

    def conveyor_out(self, box):
        yield self.env.timeout(self.cfg["transport_out"]["sec"])
        self.m.count("transport_out_boxes", 1, self.env.now)
        yield self.shipping_buffer.put(box)

    def palletizer_robot(self):
        cfg = self.cfg
        per_pallet = cfg["flow"]["boxes_per_pallet"]
        t_box = cfg["palletizing"]["sec_per_box"]
        while True:
            boxes = []
            for _ in range(per_pallet):
                boxes.append((yield self.shipping_buffer.get()))
            with self.palletizers.request() as req:
                yield req
                yield self.env.timeout(t_box * per_pallet)
                self.m.add_busy("palletizing", t_box * per_pallet)
            self.m.count("palletizing_boxes", per_pallet, self.env.now)
            self.m.count("pallets_built", 1, self.env.now)
            yield self.pallets_store.put(
                {"boxes": per_pallet, "items": sum(b["items"] for b in boxes)}
            )

    def gate(self, g: int):
        cfg = self.cfg["shipping"]
        per_truck = cfg["pallets_ktya_per_truck"]
        t_load = cfg["loading_hours"] * 3600.0
        while True:
            pallets = []
            for _ in range(per_truck):
                pallets.append((yield self.pallets_store.get()))
            with self.gates.request() as req:
                yield req
                yield self.env.timeout(t_load)
                self.m.add_busy("gates", t_load)
            boxes = sum(p["boxes"] for p in pallets)
            items = sum(p["items"] for p in pallets)
            self.balance["items_shipped"] += items
            self.balance["boxes_shipped"] += boxes
            self.m.count("trucks_loaded", 1, self.env.now)
            self.m.count("shipped_boxes", boxes, self.env.now)
            self.m.count("shipped_items", items, self.env.now)

    # --- nonsort: ручная сортировка и роллкейджи ---

    def manual_sort_worker(self):
        cfg = self.cfg["manual_sort"]
        rate = cfg["items_per_hour_each"] / 3600.0
        cap = cfg["rollcage_capacity_items"]
        while True:
            qty = yield self.nonsort_queue.get()
            t_service = qty / rate
            with self.manual_workers.request() as req:
                yield req
                yield self.env.timeout(t_service)
                self.m.add_busy("manual_sort", t_service)
            self.m.count("manual_sorted_items", qty, self.env.now)
            self.rollcage_fill += qty
            while self.rollcage_fill >= cap:
                self.rollcage_fill -= cap
                self.m.count("rollcages_built", 1, self.env.now)

    # --- мониторинг очередей ---

    def queue_monitor(self, period_sec: float = 30.0):
        while True:
            yield self.env.timeout(period_sec)
            t = self.env.now
            self.m.sample_queue("receiving_buffer_pallets", len(self.receiving_buffer.items), t)
            self.m.sample_queue("opening_queue_boxes", len(self.opening_queue.items), t)
            self.m.sample_queue("infeed_queue_boxes", len(self.infeed_queue.items), t)
            self.m.sample_queue("sealing_queue_boxes", len(self.sealing_queue.items), t)
            self.m.sample_queue("shipping_buffer_boxes", len(self.shipping_buffer.items), t)
            self.m.sample_queue("pallets_awaiting_gates", len(self.pallets_store.items), t)
            self.m.sample_queue("nonsort_batches", len(self.nonsort_queue.items), t)
            self.m.sample_queue("tare_pool_empty_boxes", self.tare_available, t)
            total_sorter_q = sum(len(q.items) for q in self.sorter_queues)
            self.m.sample_queue("sorter_queues_total_batches", total_sorter_q, t)

    # ================= сборка и запуск =================

    def build(self):
        e = self.env
        e.process(self.pallet_arrivals())
        for _ in range(self.cfg["depalletizing"]["robots"]):
            e.process(self.depal_robot())
        for _ in range(self.cfg["box_opening"]["stations"]):
            e.process(self.opener_station())
        for _ in range(self.cfg["infeed"]["stations"]):
            e.process(self.infeed_station())
        for s in range(self.n_sorters):
            e.process(self.sorter(s))
        for _ in range(self.cfg["sealing"]["stations"]):
            e.process(self.sealer_station())
        for _ in range(self.cfg["palletizing"]["robots"]):
            e.process(self.palletizer_robot())
        for g in range(self.cfg["shipping"]["gates"]):
            e.process(self.gate(g))
        for _ in range(self.cfg["manual_sort"]["workers"]):
            e.process(self.manual_sort_worker())
        e.process(self.chute_timeout_sweep())
        e.process(self.queue_monitor())
        return self

    def run(self) -> dict:
        horizon = self.cfg["simulation"]["hours"] * 3600.0
        self.env.run(until=horizon)
        wip_items = (self.balance["items_sorted"] - self.balance["items_shipped"])
        split_dirs = sum(1 for r in self.route if len(r) > 1)
        summary = {
            "config_sorters": self.n_sorters,
            "assignment_mode": self.assignment_mode,
            "directions_split_across_sorters": split_dirs,
            "sorter_load_shares": [round(float(x), 4) for x in self.sorter_loads],
            "balance": self.balance,
            "wip_items_after_sorting": wip_items,
            "operation_labels": OPERATION_LABELS,
        }
        return summary
