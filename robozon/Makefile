.PHONY: install smoke run day dashboard test docker

install:
	pip install -r requirements.txt

# Быстрая проверка работоспособности: 2 часа модельного времени
smoke:
	python -m src.run --hours 2 --out data/out_smoke

# Полный суточный прогон (критерий: метрики 1мин/1ч/12ч/24ч)
run day:
	python -m src.run --config config.yaml --out data/out

# Сценарий «10 сортеров, без резерва» — демонстрация узкого места
scenario-tight:
	python -m src.run --set sorters.count=10 --out data/out_tight

# Сценарий «ручной инфид» — база для сравнения уровня автоматизации
scenario-manual:
	python -m src.run --set infeed.mode=manual --set infeed.stations=100 --out data/out_manual

dashboard:
	streamlit run dashboard/app.py

test:
	python -m pytest tests/ -q

docker:
	docker build -t robozon-sc . && docker run --rm -v $(PWD)/data/out:/app/data/out robozon-sc
