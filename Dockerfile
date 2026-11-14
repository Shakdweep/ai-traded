FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-hosted.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-hosted.txt
COPY backend ./backend
COPY frontend ./frontend
ENV TRADER_HOST=0.0.0.0 TRADER_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "backend"]
