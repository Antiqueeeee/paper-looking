FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils ripgrep unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md config.example.toml ./
COPY paperbase ./paperbase
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /app/data /app/papers
VOLUME ["/app/data", "/app/papers"]

EXPOSE 8000
CMD ["paper", "web", "--host", "0.0.0.0", "--port", "8000"]
