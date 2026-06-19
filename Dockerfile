# Janitza UMG 512-PRO Monitor
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY janitza/ ./janitza/
COPY ui/ ./ui/
COPY docs/modbus_data.json ./docs/
COPY main.py .
# Ship the default config + meter templates so the prebuilt image is
# self-contained (a host ./config volume still overlays user edits).
COPY config/ ./config/

# Expose ports: Web UI, the published virtual-meter range, and standard Modbus.
EXPOSE 8080 1502-1512 502

# Health check — use python stdlib (no curl in the slim image, was the
# root cause of 40k+ failing health checks since image build). Probes /health:
# 200 for ok/degraded (a stale source is a correct fail-safe, not a fault),
# 503 (→ HTTPError → unhealthy) only when an enabled virtual meter is down.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=5).status == 200 else 1)" || exit 1

# Run application
CMD ["python", "main.py", "-c", "config/config.yaml"]
