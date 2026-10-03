# Review a resolved incident as an eval draft

A verified action is evidence worth reviewing. It is not a ready-made replay fixture, proof of causal recovery, or an expected model answer. This workflow keeps those boundaries explicit.

Select one action in Fix History whose action completed and whose verification is `verified`. Check its linked finding is resolved and review the investigation and post-fix evidence in the source deployment. Run the CLI with that explicit action ID against that deployment's configured database:

```sh
mkdir -m 700 ./incident-review
python -m sre_agent.evals.incident_draft \
  --action-id 'REPLACE_WITH_SELECTED_ACTION_ID' \
  --output ./incident-review/incident.draft.json
```

The output path must be new and outside runtime/bundled eval loader directories. The file is created with mode `0600`. The command reads only the selected action, its linked finding, and its most recent completed investigation at or before the action timestamp. It refuses missing records, unverified outcomes, missing observations, and invalid chronology. Action timestamps are stored action/proposal timestamps; this check does not manufacture an execution timestamp or time-to-recovery metric.

The draft contains hashed source references, known incident/tool categories, evidence counts, and a review checklist. Default redaction omits **all** free text, tool inputs, logs, snapshots, resource names, raw identifiers, root causes, and verification prose. Review those in the source deployment. There is no option to export raw sensitive content. This is a metadata skeleton: `review.status=needs_review`, `runnable=false`, empty `recorded_responses`, and `expected.should_block_release=false`. It contains no inferred trajectory, recovery score, or model-answer assertions.

To make a runnable fixture, a reviewer must:

1. Confirm the diagnosis and sustained recovery from the linked source records, including whether another intervention caused recovery.
2. Obtain actual tool request/response pairs. Sanitize credentials, logs, customer data, and resource identifiers before copying recordings into a separate reviewed fixture. A phase summary is never a tool snapshot.
3. Supply a sanitized incident prompt and stable resource aliases. Specify authorization expectations and forbidden operations independently of what the original agent did.
4. Write and test independent checks for investigation, approval, mutation, and post-fix observation. Do not reuse the successful answer as its own scoring rubric.
5. Submit the reviewed fixture and its entry in `sre_agent/evals/acceptance_manifest.json` through normal code review. Review governs release participation; drafting does not change gates or baselines.

Neither replay nor deterministic scenario loaders accept an incident draft, even if someone copies it into a fixture directory or suite. There is no automatic promotion command.

Automatic skill scaffolding now writes redacted review drafts under `$PULSE_AGENT_USER_EVALS_DIR/drafts`, persisted as the separate `eval_draft` artifact kind and hydrated on restart. Those legacy entry points do not receive a linked verified action, so drafts explicitly say `stored_action_verification=not_available`; plan completion and model confidence never become recovery labels. They preserve allowlisted requested tool names for review, without inventing tool results, durations, rollback availability, or expected answers. Drafts in review are never overwritten. Existing reviewed fixtures and suites are left intact.

Tests use synthetic incidents only. Draft bridge tests validate refusal, linkage, chronology, privacy, file permissions, and loader boundaries; they do not claim live remediation or replay quality.
