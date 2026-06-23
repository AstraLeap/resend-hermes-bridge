FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Provide a container-side `hermes` wrapper that forwards `hermes send` to the
# host proxy. The real Hermes binary lives on the host.
RUN chmod +x /app/scripts/hermes \
    && ln -s /app/scripts/hermes /usr/local/bin/hermes

# Runtime state lives here by default
RUN mkdir -p /app/data/attachments /app/data/bot_reply_contexts

EXPOSE 8765

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
