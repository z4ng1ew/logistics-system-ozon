# Воспроизводимая среда для проверки экспертами (критерий 6.2, до 10 баллов)
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Прогон суточного сценария с экспортом метрик в /app/data/out
CMD ["python", "-m", "src.run", "--config", "config.yaml", "--out", "data/out"]
