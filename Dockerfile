FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_CLI_BANNER=false \
    LTX23_WORKFLOW_API_FILE=/app/ltx23_workflow_api.json

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy every runtime file explicitly so a missing workflow API export fails
# during the image build instead of failing later when ChatGPT calls inspect.
COPY server.py ./server.py
COPY orchestrator_core.py ./orchestrator_core.py
COPY ltx23_workflow_api.json ./ltx23_workflow_api.json

# Fail the Railway build if the API export is missing, empty, invalid JSON, or
# is not the expected ComfyUI API-format object keyed by node IDs.
RUN python -c "import json; p='/app/ltx23_workflow_api.json'; data=json.load(open(p, encoding='utf-8')); assert isinstance(data, dict) and data, 'ltx23_workflow_api.json must be a non-empty JSON object'; assert '60' in data and '61' in data and '281' in data, 'required nodes 60, 61, and 281 are missing'"

EXPOSE 8000

CMD ["python", "server.py"]
