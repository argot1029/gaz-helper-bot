
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data

CMD ["python", "monitor.py"]
