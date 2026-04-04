# View Designer Tuning — Design Spec

**Goal:** Make the view designer produce consistently high-quality dashboards by adding code-level validation, improving the critique rubric, tightening prompts, and building a comprehensive fixture-based test suite.

**Problem:** The view designer is nearly unusable — duplicate widgets, generic titles, broken layouts. All quality enforcement is in the system prompt (hoping Claude follows it). No validation before saving. Critique scoring is too shallow.

**Approach:** Validation layer (block bad views before saving) + prompt improvements (reduce how often validation fires).

---

## 1. Component Validator (`sre_agent/view_validator.py`)

New module called from `api.py` before saving any view. Returns a list of validation errors. If errors exist, the view is NOT saved — errors are returned to Claude to fix.

### Deduplication (auto-applied, silent)
- Remove components with identical `query` fields
- Remove components with identical `kind` + `title` combo
- Keep the first occurrence, discard subsequent duplicates
- Log dedup actions at debug level

### Schema Validation
Every component must pass:
- Required: `kind` field (must be one of the 15 valid kinds)
- Required: `title` field (non-empty, non-generic)
- `chart`: must have `series` (list) or `query` (string)
- `metric_card`: must have `value` (string) or `query` (string)
- `data_table`: must have `columns` (list) and `rows` (list)
- `info_card_grid`: must have `cards` or `items` (list)
- `status_list`: must have `items` (list)
- `grid`: must have `items` (list)

### Generic Title Detection
Reject these titles (case-insensitive):
- "Chart", "Chart 1", "Chart 2", etc.
- "Table", "Table 1", etc.
- "Metric Card", "Metric", "Card"
- "Widget", "Component"
- Any title that is just the `kind` value

### Widget Count Enforcement
- Minimum: 3 components (metric source + chart + table)
- Maximum: 8 components (configurable)
- Over max: return error listing which widgets to merge/remove

### Required Structure
- At least 1 metric source: `metric_card`, `info_card_grid`, or `grid` containing metric cards
- At least 1 `chart`
- At least 1 `data_table`

### PromQL Basic Validation
- Balanced braces: every `{` has a matching `}`
- No double label blocks: `}{` pattern
- No empty label matchers: `{}`  with nothing inside
- Valid function wrapping: common functions like `rate()`, `sum()`, `avg()`, `topk()` are recognized

### Return Format
```python
@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]      # Blocking issues
    warnings: list[str]    # Non-blocking suggestions
    deduped_count: int     # How many duplicates were removed
    components: list[dict] # Cleaned component list (after dedup)
```

---

## 2. Enhanced Critique (`sre_agent/view_critic.py`)

Additions to the existing 10-point rubric:

### New Checks
- **Duplicate detection (all)**: Flag ALL duplicates by query AND by title+kind. Deduct 1 point per duplicate found.
- **Title quality**: Deduct 1 point per generic title. List the offending widget indices.
- **PromQL syntax**: Basic regex validation. Flag invalid queries as errors, not suggestions.
- **Empty data detection**: Charts with `series: []` and no `query` = guaranteed empty. Flag as error.
- **Component balance**: If >80% of widgets are the same kind (e.g., all tables), deduct 1 point.
- **Unique titles**: All widget titles must be unique within the view. Deduct 1 point for duplicates.

### Score Recalibration
- Keep max at 10
- New deductions can push score negative — clamp at 0
- Pass threshold stays at 7 but is now harder to reach with garbage content
- The added checks mean a "presence-only" dashboard that has metric cards, charts, and tables but with duplicates and generic titles will score 4-5 instead of 7+

---

## 3. Prompt Improvements (`sre_agent/view_designer.py`)

### Anti-Patterns Section
Add after the Rules section:

```
## Anti-Patterns (NEVER do these)
- NEVER call cluster_metrics() AND manually create individual metric cards for the same KPIs — this creates duplicates
- NEVER reuse the same PromQL query across multiple charts — each chart must show a DIFFERENT metric
- NEVER use generic titles: "Chart", "Table", "Metric Card", "Widget" — every title must describe the data
- NEVER call get_prometheus_query() more than 3 times — pick the 2-3 most important metrics
- NEVER create metric cards for values you already got from cluster_metrics() or namespace_summary()
```

### Component Accumulation Warning
Add to the BUILD step:

```
IMPORTANT: Every tool that returns a component (cluster_metrics, namespace_summary, 
get_prometheus_query, list_pods, etc.) automatically adds that component to the view.
Do NOT create components manually if you already called the tool. For example:
- cluster_metrics() returns 4 metric cards — do NOT also create metric_card components
- get_prometheus_query() returns a chart — do NOT also create a chart component with the same data
```

### Golden Example
Add one complete worked example with exact tool calls and parameters for a namespace overview dashboard.

---

## 4. Integration Point (`sre_agent/api.py`)

In the `view_spec` signal handler (around line 516):

```python
# After _sanitize_components(session_components)
from .view_validator import validate_components
result = validate_components(session_components)
session_components = result.components  # Use deduped list

if not result.valid:
    # Don't save — return errors to Claude
    error_text = "View validation failed:\n" + "\n".join(f"- {e}" for e in result.errors)
    # Emit error event to WebSocket
    await websocket.send_json({"type": "view_error", "errors": result.errors})
    # The agent loop will see this in the next turn
    continue
```

---

## 5. Test Framework

### Layer 1: Validator Unit Tests (`tests/test_view_validator.py`)
~35 tests covering:
- Dedup by query (2 tests: exact match, near-match)
- Dedup by title+kind (2 tests)
- Schema: missing kind, missing title, invalid kind (3 tests)
- Schema: chart without series/query, metric_card without value/query, table without columns (3 tests)
- Generic titles: "Chart", "Chart 1", kind-as-title (3 tests)
- Widget count: under min, at min, over max, at max (4 tests)
- Required structure: no metrics, no chart, no table, all present (4 tests)
- PromQL: balanced braces, double-brace, empty matcher, valid (4 tests)
- Full validation: golden dashboard passes, garbage dashboard fails (2 tests)
- Edge cases: empty list, single component, grid with nested metric cards (3 tests)

### Layer 2: Critique Regression Tests (`tests/test_view_critic.py`)
~25 tests covering:
- Golden dashboards: 5 pre-built fixtures (one per template) that MUST score 8+
- Bad dashboards: 5 fixtures with known issues that MUST score <5
- Individual checks: duplicate detection, title quality, empty data, balance (10 tests)
- Score boundary: verify that garbage can't score 7+ (regression guard)

### Layer 3: Integration Tests (in `tests/test_view_validator.py`)
~10 tests covering:
- validate → save pipeline: valid components saved, invalid blocked
- dedup runs before template matching
- ValidationResult dataclass serialization
- Nested components (grid items, tab content) validated recursively

---

## 6. Files Modified/Created

| File | Action | Purpose |
|------|--------|---------|
| `sre_agent/view_validator.py` | CREATE | Component validation + dedup |
| `sre_agent/view_critic.py` | MODIFY | Enhanced rubric + new checks |
| `sre_agent/view_designer.py` | MODIFY | Anti-patterns + accumulation warning + golden example |
| `sre_agent/api.py` | MODIFY | Wire validator before save |
| `tests/test_view_validator.py` | CREATE | Validator + integration tests (~45 tests) |
| `tests/test_view_critic.py` | CREATE | Critique regression tests (~25 tests) |

---

## Out of Scope (for now)
- Tool analytics integration in critique (not enough data yet)
- Layout template matching improvements (separate concern)
- Live LLM eval suite (fixture-based only)
- Frontend changes
