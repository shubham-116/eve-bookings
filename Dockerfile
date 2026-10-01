FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
ENV DATABASE=/data/eve.db PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
CMD ["gunicorn", "--preload", "-w", "2", "-b", "0.0.0.0:8000", "app:create_app()"]
