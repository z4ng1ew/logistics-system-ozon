# Подготовка

1) sudo dnf install git make ffmpeg   # ffmpeg — для видео позже

2) conda create -n robozon python=3.12 -y && conda activate robozon

3) cd robozon

4) pip install -r requirements.txt    # simpy, numpy, pyyaml, streamlit, plotly, pandas

5) make smoke                         # 2 часа модельного времени — первый запуск


6) make run

7) make dashboard


8) как мёрджить свою ветку в мастер ?

git checkout master && \
git merge feature_spectrew && \
git push origin master


- одной командой


9) make report  # docs/report.md → docs/report.pdf с оглавлением