FROM python:3.10-slim

# ─── System dependencies ─────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python dependencies ─────────────────────────────────────────────────────
# We install dependencies as root to avoid permission overhead and ensure 
# systems paths are correctly populated. We switch to appuser later.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --default-timeout=1000 -r requirements.txt

# ─── Non-root user setup ──────────────────────────────────────────────────────
RUN useradd -m -u 1000 appuser && \
    mkdir -p uploads logs /home/appuser/.deepface/weights && \
    chown -R appuser:appuser /app /home/appuser

# ─── Application code ────────────────────────────────────────────────────────
COPY --chown=appuser . .

USER appuser
ENV PATH="/home/appuser/.local/bin:$PATH"
# ─── Expose & Config ─────────────────────────────────────────────────────────
EXPOSE 8001

# Health check for container orchestrators
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

# ─── Start command ───────────────────────────────────────────────────────────
# For production: 2 uvicorn workers via gunicorn
CMD ["gunicorn", "main:app", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--workers", "2", \
    "--bind", "0.0.0.0:8001", \
    "--timeout", "120", \
    "--access-logfile", "-", \
    "--error-logfile", "-"]
