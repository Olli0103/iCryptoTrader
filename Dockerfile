FROM python:3.11-slim AS base

WORKDIR /app

# Install only production dependencies first (for caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source code
COPY src/ src/
COPY config/ config/

# Create data directory for ledger persistence
RUN mkdir -p data

# Non-root user for security
RUN useradd -m -r trader && chown -R trader:trader /app
USER trader

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 9090

ENTRYPOINT ["python", "-m", "icryptotrader"]
