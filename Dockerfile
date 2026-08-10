# Bot de aprovação de pedidos: Flask (webhook da Evolution) + Playwright/Chromium.
# Imagem única de propósito: o bot roda o liberar_pedidos.py como subprocesso,
# então o Chromium precisa morar aqui dentro.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=America/Belem

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements primeiro: mexer no código não reconstrói o Chromium (~500MB)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

# usuário não-root: o Chromium se recusa a rodar como root sem --no-sandbox,
# e não queremos alterar o liberar_pedidos.py só por causa do container.
RUN useradd -m -u 1000 app \
 && mkdir -p /app/logs /app/capturas

COPY bot_whatsapp.py liberar_pedidos.py ./
RUN chown -R app:app /app
USER app

EXPOSE 8090

# o próprio bot responde "bot-aprovacao ok" na raiz
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/',timeout=5)"

CMD ["python", "bot_whatsapp.py"]
