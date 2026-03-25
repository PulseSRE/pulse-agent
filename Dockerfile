# Uses pre-built base image with all dependencies installed.
# Build base image once: oc new-build --name=pulse-agent-base --dockerfile="$(cat Dockerfile.base)" -n openshiftpulse
# Then all subsequent builds only copy source code (~30s instead of ~6min).
FROM image-registry.openshift-image-registry.svc:5000/openshiftpulse/pulse-agent-base:latest

WORKDIR /opt/app-root/src

COPY sre_agent/ sre_agent/
COPY pyproject.toml .
RUN pip install --no-cache-dir --no-deps .

USER 1001

EXPOSE 8080

CMD ["pulse-agent-api"]
