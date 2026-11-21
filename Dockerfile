FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HELIOGRAPH_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY heliograph ./heliograph

# Named volumes are seeded with this directory's ownership, so /data stays writable for the non-root user.
RUN useradd --create-home --uid 1000 heliograph && mkdir /data && chown heliograph /data
USER heliograph
VOLUME /data
EXPOSE 8080

# Healthy = the poll loop completed a cycle recently (Hardcover reachable, loop alive).
HEALTHCHECK --interval=5m --timeout=15s --start-period=15m --retries=2 \
  CMD ["python", "-m", "heliograph", "--healthcheck"]

CMD ["python", "-m", "heliograph"]
