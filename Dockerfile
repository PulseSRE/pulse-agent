FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /app
COPY . .

USER 1001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8080/ || exit 1
