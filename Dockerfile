FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV FLASK_ENV=production DATABASE_PATH=/data/ciclo.db PORT=8000
VOLUME /data
EXPOSE 8000
CMD gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
