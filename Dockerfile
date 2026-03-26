FROM registry.access.redhat.com/ubi9/python-312:latest
WORKDIR /opt/app-root/src

# Install dependencies first (cached if pyproject.toml unchanged)
COPY pyproject.toml .
RUN pip install --no-cache-dir anthropic kubernetes fastapi uvicorn

# Copy source and install the package (always fresh)
COPY sre_agent/ sre_agent/
RUN pip install --no-cache-dir --no-deps --force-reinstall .

USER 1001
EXPOSE 8080
CMD ["pulse-agent-api"]
