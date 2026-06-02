FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
# Certs are mounted at runtime via docker-compose volume (backend/certs/ -> /app/certs/).
# If present, uvicorn serves HTTPS. If absent, plain HTTP (for reverse-proxy setups).
CMD ["sh", "-c", \
     "if [ -f /app/certs/cert.pem ] && [ -f /app/certs/key.pem ]; then \
        uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-certfile /app/certs/cert.pem --ssl-keyfile /app/certs/key.pem; \
      else \
        uvicorn main:app --host 0.0.0.0 --port 8000; \
      fi"]
