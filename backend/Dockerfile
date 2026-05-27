FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY certs/cert.pem cert.pem
COPY certs/key.pem  key.pem
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--ssl-certfile", "/app/cert.pem", "--ssl-keyfile", "/app/key.pem"]
