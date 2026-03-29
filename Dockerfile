FROM python:3.12-slim

# Non-root user
RUN groupadd -r mcp && useradd -r -g mcp mcp

WORKDIR /app

# Install deps first — better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('localhost', 8000), timeout=3)"

ENV MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

CMD ["python", "server.py"]
