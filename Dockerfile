# Default Dockerfile — single-stage build, always works.
# For faster builds with pre-built deps image, use Dockerfile.fast.
FROM registry.access.redhat.com/ubi9/python-312:latest
WORKDIR /opt/app-root/src

COPY pyproject.toml .
COPY sre_agent/ sre_agent/
RUN pip install --no-cache-dir ".[postgres]"

USER 1001
EXPOSE 8080
CMD ["pulse-agent-api"]
