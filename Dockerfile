FROM registry.access.redhat.com/ubi9/python-312:latest
WORKDIR /opt/app-root/src

# Layer 1: deps only (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN pip install --no-cache-dir anthropic[vertex] kubernetes rich fastapi uvicorn[standard] websockets cryptography

# Layer 2: code only (rebuilds fast ~2s)
COPY sre_agent/ sre_agent/
RUN pip install --no-deps --force-reinstall .

USER 1001
EXPOSE 8080
CMD ["pulse-agent-api"]
