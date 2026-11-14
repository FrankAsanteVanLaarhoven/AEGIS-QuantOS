FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m aegis
COPY aegis ./aegis
COPY web ./web
RUN mkdir -p /app/runtime && chown -R aegis:aegis /app
USER aegis
EXPOSE 8765
CMD ["uvicorn", "aegis.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
