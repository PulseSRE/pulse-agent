.PHONY: lint format type-check test verify test-all evals helm-lint release sync-token

lint:
	ruff check sre_agent/ tests/

format:
	ruff format sre_agent/ tests/

type-check:
	mypy sre_agent/ --ignore-missing-imports

test:
	python -m pytest tests/ -q

verify: lint type-check test
	@echo "All checks passed."

evals:
	@echo "Running deterministic evals..."
	python -m sre_agent.evals.cli --suite release --fail-on-gate
	python -m sre_agent.evals.cli --suite core
	python -m sre_agent.evals.cli --suite safety
	python -m sre_agent.evals.cli --audit-prompt --mode sre
	@echo "All deterministic evals passed."

evals-full: evals
	@echo "Running LLM-judged evals (requires API key)..."
	python -m sre_agent.evals.cli --suite sysadmin
	python -m sre_agent.evals.cli --suite integration
	python -m sre_agent.evals.cli --suite adversarial
	python -m sre_agent.evals.cli --suite errors
	python -m sre_agent.evals.cli --suite fleet
	python -m sre_agent.evals.cli --suite view_designer
	python -m sre_agent.evals.cli --suite autofix
	@echo "All evals passed (deterministic + LLM-judged)."

test-all: verify evals
	@echo "All tests and evals passed."

test-everything: verify evals-full
	@echo "All tests, deterministic evals, and LLM-judged evals passed."

helm-lint:
	helm lint chart/
	helm template test chart/ --set vertexAI.projectId=test --set vertexAI.region=us-central1

sync-token:
	@echo "Syncing WS auth token..."
	@# Use oc if available (OpenShift), otherwise kubectl
	@CMD=$$(command -v oc 2>/dev/null || command -v kubectl 2>/dev/null || echo ""); \
	if [ -z "$$CMD" ]; then \
		echo "  ❌ Neither oc nor kubectl found. Please install one."; \
		exit 1; \
	fi; \
	NS=$$(oc project -q 2>/dev/null || echo "openshiftpulse"); \
	echo "  Using $$CMD, namespace: $$NS"; \
	DEPLOY_NAME=$$($$CMD get deployment -n "$$NS" -l app.kubernetes.io/name=openshift-sre-agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "pulse-agent-openshift-sre-agent"); \
	SECRET_NAME=$$($$CMD get deployment "$$DEPLOY_NAME" -n "$$NS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PULSE_AGENT_WS_TOKEN")].valueFrom.secretKeyRef.name}' 2>/dev/null || echo "$$DEPLOY_NAME-ws-token"); \
	if $$CMD get secret "$$SECRET_NAME" -n "$$NS" &>/dev/null 2>&1; then \
		TOKEN=$$($$CMD get secret "$$SECRET_NAME" -n "$$NS" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || echo ""); \
		if [ -n "$$TOKEN" ]; then \
			echo "  Token secret: $$SECRET_NAME"; \
			echo "  Token (first 12 chars): $${TOKEN:0:12}..."; \
			$$CMD set env deployment/"$$DEPLOY_NAME" PULSE_AGENT_WS_TOKEN="$$TOKEN" -n "$$NS" --overwrite; \
			echo "  ✓ Token synced to deployment"; \
			echo "  Run: oc get secret $$SECRET_NAME -n $$NS -o jsonpath='{.data.token}' | base64 -d"; \
		else \
			echo "  ⚠️ Could not decode token from secret"; \
		fi; \
	else \
		echo "  ⚠️ Secret $$SECRET_NAME not found in namespace $$NS"; \
		echo "  Try: helm upgrade ... to recreate the secret"; \
	fi

release:
	@test -n "$(VERSION)" || (echo "Usage: make release VERSION=x.y.z" && exit 1)
	./scripts/bump-version.sh $(VERSION)
	git add pyproject.toml chart/Chart.yaml
	git commit -m "chore: bump version to $(VERSION)"
	git tag "v$(VERSION)"
	@echo "Release v$(VERSION) ready. Run 'git push && git push --tags' to trigger build."
