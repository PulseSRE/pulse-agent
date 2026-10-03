# Design Principles

These principles state product goals, not implemented guarantees or measured service levels. Current behavior and limitations are documented in [SECURITY](SECURITY.md) and [architecture](docs/ARCHITECTURE.md).

## 1. Conversational-first, visual-second, code-third

The default way to interact is natural language or voice. Visuals (maps, diagrams, heatmaps) appear instantly to confirm understanding. Code or YAML only surfaces when you explicitly ask for it or need to tweak something deep.

## 2. Intent -> Visibility -> Trust -> Action

Every user flow follows this exact sequence so there is zero ambiguity about what the platform is doing or why.

## 3. Zero training curve; interface teaches itself

New users (even junior cluster admins) should be productive in <30 minutes. Tooltips, inline explanations, and guided "first intent" flows disappear as you gain mastery.

## 4. Delight: proactive, plain-English, confidence scores everywhere

The UI feels alive — it greets you, surfaces wins, explains trade-offs in human language, and always shows confidence % so you know exactly how much to trust each suggestion.

## 5. Human-in-the-loop by default for anything that matters

Prefer explicit approval for high-risk, high-cost, or high-impact changes. Autonomous completion rates must be measured on representative workloads; no universal percentage is established here. Validate execution-path controls before enabling unattended writes.

## 6. Radical transparency & explainability

Every agent action, simulation result, or optimization includes a plain-English "Why did I do this?" narrative + full audit trail. You can click any node and ask "Show me the reasoning chain" or "What would happen if we changed X?" No black boxes.

## 7. Proactive intelligence without alert fatigue

The system surfaces insights intelligently (prioritized, time-sensitive, personalized). Noise is filtered by AI; only things that actually require your attention reach you. "Good morning" summary replaces the 47 Slack alerts you used to get.

## 8. Minimal cognitive load & single pane of glass

No context switching between 8 different tools. One dashboard, one chat, one review queue. Everything about your entire fleet (multi-cloud, edge, on-prem) is visible and actionable in one place.

## 9. Forgiving & resilient by design

Simulations are mandatory before any live change. Every action has one-click rollback. The UI anticipates mistakes ("You're about to increase costs by 40% — want the cheaper alternative?") and makes undoing trivial.

## 10. Personalized & adaptive over time

Pulse learns your risk tolerance, communication style, preferred trade-offs, and even the quirks of your specific fleet. After a few weeks it feels like it was built by someone who sat next to you for years.
