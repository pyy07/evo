FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY apps ./apps
COPY services ./services
COPY config ./config

RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV MARKET_DATA_MODE=mock
WORKDIR /app
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "evo_api.main:app", "--host", "0.0.0.0", "--port", "8000"]