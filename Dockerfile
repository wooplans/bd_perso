FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-comic-neue \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p bibliotheque output fonts \
    && wget -q "https://github.com/wooplans/bd_perso/raw/main/comic.ttf" -O /app/comic.ttf || true

EXPOSE 7860

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:7860", "--workers", "2", "--timeout", "120"]
