FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

COPY app.py /app/app.py

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 18789

CMD ["python", "/app/app.py"]
