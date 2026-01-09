# Builder
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Build deps for wheels
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
     build-essential \
     gcc \
     libpq-dev \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip wheel \
  && pip wheel --no-cache-dir -r /app/requirements.txt -w /wheels


# Runtime
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime deps only
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
     libpq5 \
  && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -u 10001 -m appuser

# Install wheels
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir /wheels/* \
  && rm -rf /wheels

# Copy app code + migrations
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY src /app/src

USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.core.main:app", "--host", "0.0.0.0", "--port", "8000"]
