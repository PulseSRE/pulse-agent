# Agent documentation

Start with the guide for your task. Current implementation lives in `sre_agent/`; Markdown examples explain it and must be updated with behavior changes.

| Task | Guide |
|---|---|
| Install/configure and run the CLI | [README](../README.md) |
| Deploy the OpenShift product | [pulse-operator](https://github.com/PulseSRE/pulse-operator) |
| Understand execution and trust boundaries | [Architecture](ARCHITECTURE.md), [Security](../SECURITY.md) |
| Integrate REST/WebSocket clients | [API contract](../API_CONTRACT.md) |
| Run checks and interpret their evidence | [Testing](../TESTING.md), [Eval framework](../sre_agent/evals/README.md) |
| Operate schema/persistence | [Database](../DATABASE.md) |
| Configure durable workflows | [Temporal](TEMPORAL.md) |
| Create a skill | [Skill developer guide](SKILL_DEVELOPER_GUIDE.md) |
| Contribute/release | [Contributing](../CONTRIBUTING.md), [Release](../RELEASE.md) |
| Read release/project history | [Changelog](../CHANGELOG.md), [Journey](JOURNEY.md) |

Design principles are aspirations, not measured guarantees. Dated specifications and plans under `superpowers/` retain historical proposals/tasks and may name removed files or deployment tooling. They are not current operational instructions. Built-in skill Markdown is model-facing procedure content; changes to it need replay/behavior validation, not just spelling review.

Local tests, provider-backed gates, and cluster acceptance are different evidence layers. Documentation review does not verify live cluster readiness. In particular, test databases are destructive fixtures; browser trust/category controls are not a server authorization boundary.
