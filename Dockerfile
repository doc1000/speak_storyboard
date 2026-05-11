FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached; only re-runs when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything else, including img/ seed images and all JSON/prompt files
COPY . .

# Generate PWA icons at build time (Pillow already installed above)
RUN python3 /app/create_icons.py

# Entrypoint seeds the Fly volume on first boot, then starts gunicorn
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
