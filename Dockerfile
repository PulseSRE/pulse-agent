# Fast code-only build — deps are in the pre-built pulse-agent-deps:latest image.
# If deps image is missing, use Dockerfile.full as fallback.
ARG BASE_IMAGE=image-registry.openshift-image-registry.svc:5000/openshiftpulse/pulse-agent-deps:latest
FROM $BASE_IMAGE

WORKDIR /opt/app-root/src
COPY sre_agent/ sre_agent/
COPY pyproject.toml .
RUN pip install --no-deps --force-reinstall .

USER 1001
EXPOSE 8080
CMD ["pulse-agent-api"]
