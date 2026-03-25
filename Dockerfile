FROM registry.access.redhat.com/ubi9/python-312:1-51

WORKDIR /opt/app-root/src

COPY pyproject.toml .
COPY sre_agent/ sre_agent/

RUN pip install --no-cache-dir .

USER 1001

EXPOSE 8080

# Default: run the API server. Override CMD to run the CLI instead.
CMD ["pulse-agent-api"]
