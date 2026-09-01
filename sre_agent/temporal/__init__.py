"""Durable plan execution on Temporal.

The in-process engine (plan_runtime) runs a plan as an asyncio task inside the
agent pod, which has two structural consequences this package exists to remove:

- **Executions die with the pod.** ``_record_execution`` writes only at the
  end, so a restart mid-plan loses the run entirely — no record, no resume.
  The pod was rolled five times in one working day of this repo's history;
  every in-flight plan died silently each time.
- **Approval can't actually wait.** ``approval_required`` phases are marked
  ``needs_escalation`` and skipped, because an in-process engine cannot afford
  to block for hours. Human-in-the-loop plans are structurally impossible.

On Temporal, the workflow is an *interpreter*: one ``PlanWorkflow`` executes
any plan definition, including plans created in the UI at runtime, without
deploying new workflow code per plan. Phases run as activities; approval is a
signal the workflow can durably wait days for; Temporal's history is the audit
trail and the resume mechanism.

Everything here is inert until ``PULSE_AGENT_TEMPORAL_HOST`` is set. The
monitor's automatic path keeps using the in-process engine; only the explicit
run endpoints route here.
"""
