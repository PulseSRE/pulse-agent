# Agent documentation audit

Coverage baseline: main `3d4e67b` (2026-10-11 review). All **89 tracked Markdown/HTML documents** were included; the table records every path, disposition and reviewed-content SHA-256 prefix. This audit adds this report separately. Source, tests and workflows take precedence over examples. No product/cluster runtime, provider evaluation, release tag or remote publication was performed by this documentation audit.

Corrections cover stale model-quality/count claims, fixture versus live gates, current container packaging checks, review-only eval drafts, authoritative monitor inbox data, millisecond recovery timing, durable execution evidence, policy refusal scope, API inventory and protocol compatibility. Historical plans/specs already carry warnings and remain excluded from Pages; release/changelog history is preserved rather than rewritten into current claims.

Validation: Markdown links checked outside fenced code; zero missing local file targets. Literal REST paths checked with router prefixes and normalized path parameters; new inventory entries derive authentication dependencies and purposes from source. HTML tags/IDs and repository link targets checked. Documentation commands were cross-checked with Makefile, CLIs and workflows, not executed against live services. Fixed prompt text is not proof of model behavior; paired live and cluster acceptance remain necessary. Routing documentation was checked against the companion source fix `4f7f6b1`; publish it with that fix. Its offline routing tests are not live-provider quality evidence.

| File | Disposition | Reviewed SHA-256 prefix |
|---|---|---|
| `.claude/agents/deploy-validator.md` | Reviewed development procedure; unchanged | `6ab0edcb471c` |
| `.claude/agents/memory-auditor.md` | Reviewed development procedure; unchanged | `bead631ef788` |
| `.claude/agents/performance-analyzer.md` | Reviewed development procedure; unchanged | `590c3f35850d` |
| `.claude/agents/protocol-checker.md` | Reviewed development procedure; unchanged | `0ffaec1eb6b9` |
| `.claude/agents/psychologist.md` | Reviewed development procedure; unchanged | `d7ad223d1964` |
| `.claude/agents/runbook-writer.md` | Reviewed development procedure; unchanged | `3cde5e13b581` |
| `.claude/agents/security-hardener.md` | Reviewed development procedure; unchanged | `09e4533256bd` |
| `.claude/agents/test-writer.md` | Reviewed development procedure; unchanged | `749302ccc337` |
| `.claude/agents/tool-auditor.md` | Reviewed development procedure; unchanged | `76ef88c1f92d` |
| `.claude/agents/tool-writer.md` | Reviewed development procedure; unchanged | `bf1267e1f000` |
| `.claude/agents/ux-workflow-reviewer.md` | Reviewed development procedure; unchanged | `9fbd9b595b18` |
| `.claude/skills/deploy/SKILL.md` | Reviewed development procedure; unchanged | `271a1359f8f7` |
| `.claude/skills/eval-runner/SKILL.md` | Corrected active commands/contracts/evidence or navigation | `3003318309dc` |
| `.claude/skills/release/SKILL.md` | Reviewed development procedure; unchanged | `fad2b5fe6cf5` |
| `API_CONTRACT.md` | Corrected active commands/contracts/evidence or navigation | `0f5426c2a256` |
| `CHANGELOG.md` | Preserved dated history and added Unreleased health, routing and replay corrections; not deployed acceptance evidence | `9e88429ac5a5` |
| `CLAUDE.md` | Corrected active commands/contracts/evidence or navigation | `93aeb3246653` |
| `CONTRIBUTING.md` | Reviewed active guide; no additional correction needed | `c2d66254eefd` |
| `DATABASE.md` | Corrected active commands/contracts/evidence or navigation | `1ee2b66a2243` |
| `DESIGN_PRINCIPLES.md` | Preserved explicitly labelled aspirations | `d2b8fb86d93e` |
| `EVAL_LAB.md` | Reviewed active guide; no additional correction needed | `c67d4cd91c87` |
| `README.md` | Corrected active commands/contracts/evidence or navigation | `a02b48742a1a` |
| `RELEASE.md` | Corrected active commands/contracts/evidence or navigation | `7161e8a016ab` |
| `SECURITY.md` | Corrected active commands/contracts/evidence or navigation | `b90eb513e00e` |
| `TESTING.md` | Corrected active commands/contracts/evidence or navigation | `61e974c6b538` |
| `docs/ARCHITECTURE.md` | Corrected active commands/contracts/evidence or navigation | `55857cfbb86b` |
| `docs/INSTALLATION_READINESS.md` | Reviewed active guide; no additional correction needed | `fbdeb4c2131f` |
| `docs/JOURNEY.md` | Preserved dated history; not current operational evidence | `222f3c5025c6` |
| `docs/README.md` | Corrected active commands/contracts/evidence or navigation | `702faff2f9bf` |
| `docs/SKILL_DEVELOPER_GUIDE.md` | Reviewed active guide; no additional correction needed | `5d36dac4fd29` |
| `docs/TEMPORAL.md` | Corrected active commands/contracts/evidence or navigation | `3908914840ba` |
| `docs/index.html` | Corrected active commands/contracts/evidence or navigation | `2a914f6e3056` |
| `docs/superpowers/plans/2026-04-03-tool-chain-intelligence.md` | Preserved historical proposal; historical warning verified | `534593665885` |
| `docs/superpowers/plans/2026-04-03-tool-tracking-prereqs.md` | Preserved historical proposal; historical warning verified | `f48dbd8609ee` |
| `docs/superpowers/plans/2026-04-03-tool-usage-backend.md` | Preserved historical proposal; historical warning verified | `6010b5c97439` |
| `docs/superpowers/plans/2026-04-03-tool-usage-frontend.md` | Preserved historical proposal; historical warning verified | `33088f57eb6d` |
| `docs/superpowers/plans/2026-04-04-data-first-generative-ui.md` | Preserved historical proposal; historical warning verified | `9e53a4ad4629` |
| `docs/superpowers/plans/2026-04-04-semantic-layout-engine.md` | Preserved historical proposal; historical warning verified | `f55a0efa64f3` |
| `docs/superpowers/plans/2026-04-07-new-dashboard-components.md` | Preserved historical proposal; historical warning verified | `507e9ef9f214` |
| `docs/superpowers/plans/2026-04-12-adaptive-tool-selection.md` | Preserved historical proposal; historical warning verified | `bc0243eb4fdc` |
| `docs/superpowers/plans/2026-04-12-mission-control-backend.md` | Preserved historical proposal; historical warning verified | `33d4e53008d0` |
| `docs/superpowers/plans/2026-04-12-mission-control-frontend.md` | Preserved historical proposal; historical warning verified | `ed4a8e457366` |
| `docs/superpowers/plans/2026-04-13-intelligent-autofix.md` | Preserved historical proposal; historical warning verified | `879b1cd619e7` |
| `docs/superpowers/plans/2026-04-13-resolution-tracking.md` | Preserved historical proposal; historical warning verified | `08f7dcf99b03` |
| `docs/superpowers/plans/2026-04-14-slack-claude-bridge.md` | Preserved historical proposal; historical warning verified | `a942cd99ad09` |
| `docs/superpowers/plans/2026-04-19-parallel-multi-skill-execution.md` | Preserved historical proposal; historical warning verified | `840f5df5b24d` |
| `docs/superpowers/plans/2026-04-19-topology-perspectives.md` | Preserved historical proposal; historical warning verified | `78f72bd9367b` |
| `docs/superpowers/plans/2026-04-20-nextgen-phase1-cleanup.md` | Preserved historical proposal; historical warning verified | `f41b13382bc4` |
| `docs/superpowers/plans/2026-04-20-phase2-view-components.md` | Preserved historical proposal; historical warning verified | `8e91ce314ddf` |
| `docs/superpowers/plans/2026-04-20-phase3a-view-schema.md` | Preserved historical proposal; historical warning verified | `adbfaf40ddff` |
| `docs/superpowers/plans/2026-04-20-phase3b-agent-auto-creation.md` | Preserved historical proposal; historical warning verified | `58ae04f51677` |
| `docs/superpowers/plans/2026-04-21-ops-inbox.md` | Preserved historical proposal; historical warning verified | `0185b56b3ab4` |
| `docs/superpowers/plans/2026-04-22-investigation-view-plan.md` | Preserved historical proposal; historical warning verified | `a7de553a6079` |
| `docs/superpowers/plans/2026-04-22-user-token-forwarding.md` | Preserved historical proposal; historical warning verified | `ced053cc8b31` |
| `docs/superpowers/plans/2026-04-23-async-anthropic-migration.md` | Preserved historical proposal; historical warning verified | `e0ec46027ece` |
| `docs/superpowers/specs/2026-04-03-tool-chain-intelligence-design.md` | Preserved historical proposal; historical warning verified | `65cf876d127e` |
| `docs/superpowers/specs/2026-04-03-tool-usage-tracking-design.md` | Preserved historical proposal; historical warning verified | `0632d21e1f29` |
| `docs/superpowers/specs/2026-04-04-data-first-generative-ui-design.md` | Preserved historical proposal; historical warning verified | `08ac862c60f6` |
| `docs/superpowers/specs/2026-04-04-intelligence-loop-design.md` | Preserved historical proposal; historical warning verified | `4324d7dd0570` |
| `docs/superpowers/specs/2026-04-04-semantic-layout-engine-design.md` | Preserved historical proposal; historical warning verified | `e82cdd4f53bf` |
| `docs/superpowers/specs/2026-04-04-view-designer-tuning-design.md` | Preserved historical proposal; historical warning verified | `bc746162f0e1` |
| `docs/superpowers/specs/2026-04-07-new-dashboard-components-design.md` | Preserved historical proposal; historical warning verified | `4d264f6f80e3` |
| `docs/superpowers/specs/2026-04-09-prompt-optimization-design.md` | Preserved historical proposal; historical warning verified | `c84b473a3ef4` |
| `docs/superpowers/specs/2026-04-10-extensible-agent-architecture-design.md` | Preserved historical proposal; historical warning verified | `c43632a52ff9` |
| `docs/superpowers/specs/2026-04-12-adaptive-tool-selection-design.md` | Preserved historical proposal; historical warning verified | `a9ed311d131a` |
| `docs/superpowers/specs/2026-04-12-mission-control-redesign-design.md` | Preserved historical proposal; historical warning verified | `0031d3d237f0` |
| `docs/superpowers/specs/2026-04-14-slack-claude-bridge-design.md` | Preserved historical proposal; historical warning verified | `4858d7d1849e` |
| `docs/superpowers/specs/2026-04-14-unified-layout-engine-design.md` | Preserved historical proposal; historical warning verified | `f44891f6fd48` |
| `docs/superpowers/specs/2026-04-17-autonomous-remediation-design.md` | Preserved historical proposal; historical warning verified | `f1c0080f71ac` |
| `docs/superpowers/specs/2026-04-19-parallel-multi-skill-execution-design.md` | Preserved historical proposal; historical warning verified | `edc51d16618c` |
| `docs/superpowers/specs/2026-04-19-skill-executor-refactor-design.md` | Preserved historical proposal; historical warning verified | `1f86b7fd2135` |
| `docs/superpowers/specs/2026-04-19-topology-perspectives-design.md` | Preserved historical proposal; historical warning verified | `5d583511bdc9` |
| `docs/superpowers/specs/2026-04-20-nextgen-revised-design.md` | Preserved historical proposal; historical warning verified | `077fbaf77dbb` |
| `docs/superpowers/specs/2026-04-20-ops-inbox-design.md` | Preserved historical proposal; historical warning verified | `5ca2f29508b0` |
| `docs/superpowers/specs/2026-04-21-ops-inbox-design.md` | Preserved historical proposal; historical warning verified | `1463cd6c178f` |
| `docs/superpowers/specs/2026-04-22-investigation-view-plan-design.md` | Preserved historical proposal; historical warning verified | `8657f5b86ee8` |
| `docs/superpowers/specs/2026-04-22-user-token-forwarding-design.md` | Preserved historical proposal; historical warning verified | `d9a0a03fdb9e` |
| `docs/superpowers/specs/2026-04-23-async-anthropic-migration-design.md` | Preserved historical proposal; historical warning verified | `40b71a6758a0` |
| `docs/superpowers/specs/2026-05-27-ocp-console-readiness-platform.md` | Preserved historical proposal; historical warning verified | `7c6852d7e353` |
| `sre_agent/evals/README.md` | Corrected active commands/contracts/evidence or navigation | `d8ae73c5ceb6` |
| `sre_agent/skills/capacity-planner/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `a0809bc9204f` |
| `sre_agent/skills/plan-builder/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `9cee9714ed6b` |
| `sre_agent/skills/postmortem/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `2f1fd7c7ba26` |
| `sre_agent/skills/security/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `24f3c5311bb9` |
| `sre_agent/skills/slo-management/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `4f834078365d` |
| `sre_agent/skills/sre/references/certificate-expiry.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `5e21e9b233d9` |
| `sre_agent/skills/sre/references/node-pressure.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `6e4079e18266` |
| `sre_agent/skills/sre/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `24f819b92739` |
| `sre_agent/skills/view-designer/skill.md` | Reviewed model procedure/reference; unchanged; behavior needs live evaluation | `b1c3f7cd6014` |
