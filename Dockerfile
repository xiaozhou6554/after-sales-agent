FROM python:3.10.21-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app

WORKDIR /app

RUN python -m pip install --no-cache-dir --upgrade pip==25.3 setuptools==80.9.0 wheel==0.45.1

COPY requirements-container.lock ./

# This Linux/Python 3.10 lock includes the official CPU-only PyTorch wheel.
RUN python -m pip install --no-cache-dir -r requirements-container.lock \
    --extra-index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml ./
COPY bootstrap ./bootstrap
COPY migrations ./migrations
COPY src ./src
COPY data ./data
COPY frontend ./frontend

RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/generated \
    && chown -R app:app /app

USER 10001:10001

CMD ["python", "-m", "bootstrap.api_main"]
