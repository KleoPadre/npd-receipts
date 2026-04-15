FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости первыми — используем кэш слоёв Docker
COPY requirements.txt .
COPY wheels/ ./wheels/

# Устанавливаем из локальных wheels (не нужен интернет во время сборки)
RUN pip install --no-cache-dir --no-index --find-links=./wheels -r requirements.txt

COPY app/ ./app/

# Папки монтируются с NAS — создаём только как fallback
RUN mkdir -p logs env

CMD ["python", "-m", "app.main"]
