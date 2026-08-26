FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_CLI_BANNER=false

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator_core.py server.py ./

EXPOSE 8000

CMD ["python", "server.py"]

