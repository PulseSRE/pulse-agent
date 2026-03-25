FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src

# Upgrade pip first for faster dependency resolution
RUN pip install --no-cache-dir --upgrade pip

# Layer 1: Install dependencies only (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    'anthropic[vertex]>=0.52.0' \
    'kubernetes>=31.0.0' \
    'rich>=13.0.0' \
    'fastapi>=0.115.0' \
    'uvicorn[standard]>=0.34.0' \
    'websockets>=14.0'

# Layer 2: Copy source code (changes every build, but deps are cached)
COPY sre_agent/ sre_agent/
RUN pip install --no-cache-dir --no-deps .

USER 1001

EXPOSE 8080

CMD ["pulse-agent-api"]
