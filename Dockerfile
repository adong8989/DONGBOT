
FROM python:3.10.13-slim

RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

ENV PORT=10000
CMD ["python", "app.py"]
