"""
Дашборд метрик сортировочного центра (Робозон, Задача 1).

Запуск:  streamlit run dashboard/app.py   (или: make dashboard)

Читает результаты прогона из папки data/out (меняется в боковой панели):
  operations_minutely.csv, operations_agg.csv, queues.csv,
  utilization.csv, summary.json
Позволяет менять параметры и запускать новый прогон прямо из интерфейса.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Корень репозитория = на уровень выше папки dashboard/
ROOT = Path(__file__).resolve().parent.parent

st.set_page_config(page_title="Сортировочный центр — метрики", layout="wide")

# --------- боковая панель: выбор папки с результатами ---------
st.sidebar.title("Ящик Шрёдингера")
st.sidebar.caption("Робозон · Задача 1 · имитационная модель СЦ")
out_dir = st.sidebar.text_input("Папка с результатами прогона", value="data/out")
DATA = ROOT / out_dir


def _load_csv(name: str) -> pd.DataFrame | None:
    p = DATA / name
    if not p.exists():
        return None
    return pd.read_csv(p)


def _load_summary() -> dict | None:
    p = DATA / "summary.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


summary = _load_summary()
agg = _load_csv("operations_agg.csv")
queues = _load_csv("queues.csv")
util = _load_csv("utilization.csv")

if summary is None:
    st.title("Сортировочный центр — метрики")
    st.warning(
        f"В папке `{out_dir}` нет результатов. Сначала выполните прогон:\n\n"
        "```bash\nmake run\n```\n\nили укажите другую папку слева."
    )
    st.stop()

labels: dict = summary.get("operation_labels", {})


def human(op: str) -> str:
    """Код операции -> русская подпись (или сам код, если подписи нет)."""
    return labels.get(op, op)


# =================== ШАПКА: карточки балансов ===================
st.title("Сортировочный центр — метрики")
b = summary.get("balance", {})
c1, c2, c3, c4 = st.columns(4)
c1.metric("Товаров вошло", f"{b.get('items_in', 0):,}".replace(",", " "))
c2.metric("Отсортировано", f"{b.get('items_sorted', 0):,}".replace(",", " "))
c3.metric("Отгружено", f"{b.get('items_shipped', 0):,}".replace(",", " "))
c4.metric("Nonsort (не для авто)", f"{b.get('items_nonsort', 0):,}".replace(",", " "))

mode = summary.get("assignment_mode", "?")
n_split = summary.get("directions_split_across_sorters", 0)
st.caption(
    f"Режим назначения: **{mode}** · раздроблено направлений: **{n_split}** · "
    f"сортеров: **{summary.get('config_sorters', '?')}** · "
    f"средняя заполненность КТЯ: "
    f"**{summary.get('observations_avg', {}).get('outbound_box_fill', '?')}** из 27"
)

st.divider()

# =================== ГРАФИК: темп по операциям ===================
st.subheader("Темп операций во времени")
if agg is not None:
    window = st.radio(
        "Окно агрегации (за какой период считаем)",
        ["1min", "1h", "12h", "24h"],
        index=1, horizontal=True,
    )
    ops_all = sorted(agg["operation"].unique())
    default_ops = [o for o in [
        "infeed_items", "sorted_items", "sealing_boxes",
        "palletizing_boxes", "shipped_items", "nonsort_items",
    ] if o in ops_all]
    chosen = st.multiselect(
        "Операции",
        options=ops_all,
        default=default_ops,
        format_func=human,
    )
    sub = agg[(agg["window"] == window) & (agg["operation"].isin(chosen))].copy()
    if not sub.empty:
        sub["Операция"] = sub["operation"].map(human)
        fig = px.line(
            sub, x="window_index", y="rate_per_hour",
            color="Операция", markers=(window in ("12h", "24h")),
            labels={"window_index": f"Интервал ({window})",
                    "rate_per_hour": "Темп, единиц/час"},
        )
        fig.update_layout(height=420, legend_title_text="")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Выберите хотя бы одну операцию.")
else:
    st.info("Нет файла operations_agg.csv")

st.divider()

# =================== ГРАФИК: очереди ===================
st.subheader("Очереди и буферы")
if queues is not None:
    qnames = sorted(queues["queue"].unique())
    qchosen = st.multiselect(
        "Очереди", options=qnames,
        default=[q for q in ["sorter_queues_total_batches",
                             "shipping_buffer_boxes",
                             "pallets_awaiting_gates"] if q in qnames],
    )
    qsub = queues[queues["queue"].isin(qchosen)]
    if not qsub.empty:
        figq = px.line(
            qsub, x="minute", y="avg_length", color="queue",
            labels={"minute": "Минута", "avg_length": "Средняя длина"},
        )
        figq.update_layout(height=360, legend_title_text="")
        st.plotly_chart(figq, use_container_width=True)
    else:
        st.info("Выберите очередь для отображения.")
else:
    st.info("Нет файла queues.csv")

st.divider()

# =================== ГРАФИК: загрузка ресурсов ===================
st.subheader("Загрузка ресурсов (узкие места)")
if util is not None:
    u = util.sort_values("utilization", ascending=True).copy()
    u["Загрузка, %"] = (u["utilization"] * 100).round(1)
    # красным — всё, что выше 90% (узкое место)
    u["Зона"] = u["utilization"].apply(lambda x: "Узкое место (>90%)" if x > 0.9 else "Норма")
    figu = px.bar(
        u, x="Загрузка, %", y="resource", orientation="h", color="Зона",
        color_discrete_map={"Узкое место (>90%)": "#e34948", "Норма": "#199e70"},
        labels={"resource": "Ресурс"},
    )
    figu.update_layout(height=420, legend_title_text="")
    st.plotly_chart(figu, use_container_width=True)
else:
    st.info("Нет файла utilization.csv")

st.divider()

# =================== ПАНЕЛЬ: запустить новый прогон ===================
st.subheader("Запустить новый прогон")
st.caption("Меняете параметры → жмёте кнопку → модель считает → графики обновятся.")

p1, p2, p3 = st.columns(3)
with p1:
    n_sorters = st.number_input("Число сортеров", 8, 14, 11, 1)
    hours = st.number_input("Горизонт, часов", 2, 48, 24, 1)
with p2:
    p_nonsort = st.slider("Доля nonsort", 0.0, 0.15, 0.05, 0.01)
    mode_in = st.selectbox("Режим инфида", ["auto", "manual"], index=0,
                           help="auto — автозагрузка сортеров; manual — вручную")
with p3:
    assign = st.selectbox("Назначение направлений", ["split", "strict"], index=0,
                          help="split — дробление тяжёлых направлений; "
                               "strict — жёсткая привязка (демонстрирует затор)")
    new_out = st.text_input("Папка результата", value="data/out_ui")

if st.button("▶ Запустить прогон", type="primary"):
    cmd = [
        sys.executable, "-m", "src.run",
        "--out", new_out,
        "--hours", str(hours),
        "--set", f"sorters.count={int(n_sorters)}",
        "--set", f"flow.p_nonsort={p_nonsort}",
        "--set", f"infeed.mode={mode_in}",
        "--set", f"sorters.assignment_mode={assign}",
    ]
    if mode_in == "manual":
        cmd += ["--set", "infeed.stations=100"]
    with st.spinner("Модель считает..."):
        res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if res.returncode == 0:
        st.success("Готово. Укажите слева папку результата, чтобы посмотреть.")
        st.code(res.stdout)
        st.info(f"Результаты сохранены в: {new_out}")
    else:
        st.error("Прогон упал. Смотрите ошибку ниже.")
        st.code(res.stderr)
