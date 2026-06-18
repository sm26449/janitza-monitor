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

# Create config directory
RUN mkdir -p config

# Expose ports
EXPOSE 8080

# Health check — use python stdlib (no curl in the slim image, was the
# root cause of 40k+ failing health checks since image build).
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/api/status', timeout=5).status == 200 else 1)" || exit 1

# Run application
CMD ["python", "main.py", "-c", "config/config.yaml"]
