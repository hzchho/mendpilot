# Optional repair workspace image. Install application dependencies here if needed.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash git \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir pytest
WORKDIR /testbed
ENV PYTHONDONTWRITEBYTECODE=1
