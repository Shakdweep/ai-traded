FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend ./frontend
ENV TRADER_HOST=0.0.0.0 TRADER_PORT=8000 TRADER_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "backend"]
