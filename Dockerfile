FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1

# WeasyPrint требует Pango/HarfBuzz; шрифты Noto дают кириллицу, латиницу и иероглифы
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        fonts-noto-cjk \
        fonts-noto-core \
        fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY templates ./templates
COPY branding ./branding

RUN mkdir -p /app/data

CMD ["python", "-m", "bot.main"]
