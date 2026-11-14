FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m aegis
COPY aegis ./aegis
COPY web ./web
COPY configs ./configs
COPY data ./data
RUN mkdir -p /app/runtime && chown -R aegis:aegis /app
USER aegis
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"
CMD ["uvicorn", "aegis.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
