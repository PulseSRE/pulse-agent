# Component Spec Contract

## Overview

Tools return structured component specs alongside text responses. The UI renders
these as interactive widgets inline in the chat. The contract is defined in:

- **Agent:** Tool functions return `(text, spec_dict)` tuples
- **UI:** `src/kubeview/engine/agentComponents.ts` defines TypeScript types
- **Protocol:** `component` WebSocket event carries the spec

## Supported Component Types

| Kind | Description | Key Fields |
|------|-------------|------------|
| `data_table` | Interactive table with sortable columns | `columns`, `rows`, `title` |
| `info_card_grid` | Grid of label/value/sub cards | `cards[]` |
| `badge_list` | Colored badges (success/warning/error/info) | `badges[]` |
| `status_list` | Resources with health status indicators | `items[]` with `status` |
| `key_value` | Key-value pair display | `pairs[]` |
| `chart` | Time-series chart with multiple series | `series[]` with `[timestamp, value][]` |
| `tabs` | Tabbed container of other components | `tabs[]` with `components[]` |
| `grid` | Multi-column layout of other components | `items[]`, `columns` |
| `section` | Collapsible section with title | `components[]`, `collapsible` |

## Schema (TypeScript canonical source)

```typescript
// Source: src/kubeview/engine/agentComponents.ts

export type ComponentSpec =
  | DataTableSpec
  | InfoCardGridSpec
  | BadgeListSpec
  | StatusListSpec
  | KeyValueSpec
  | ChartSpec
  | TabsSpec
  | GridSpec
  | SectionSpec;

export interface DataTableSpec {
  kind: 'data_table';
  title?: string;
  columns: Array<{ id: string; header: string; width?: string }>;
  rows: Array<Record<string, string | number | boolean>>;
}

export interface ChartSpec {
  kind: 'chart';
  title?: string;
  series: Array<{
    label: string;
    data: Array<[number, number]>;  // [timestamp_ms, value]
    color?: string;
  }>;
  yAxisLabel?: string;
  height?: number;
}

// See agentComponents.ts for full type definitions
```

## ViewSpec (Custom Dashboards)

```typescript
export interface ViewSpec {
  id: string;        // cv-{uuid12}
  title: string;
  icon?: string;
  description?: string;
  layout: ComponentSpec[];  // Ordered list of widgets
  generatedAt: number;     // Unix ms
}
```

Agent emits `view_spec` WebSocket event when `create_dashboard` tool is called.
UI persists to localStorage via `customViewStore`. Accessible at `/custom/:viewId`.

## Agent-Side Usage

```python
@beta_tool
def list_pods(namespace: str = "") -> str:
    # ... fetch pods ...
    text = f"Found {len(pods)} pods"
    spec = {
        "kind": "data_table",
        "title": "Pods",
        "columns": [
            {"id": "name", "header": "Name"},
            {"id": "status", "header": "Status"},
        ],
        "rows": [{"name": p.name, "status": p.status.phase} for p in pods],
    }
    return (text, spec)  # Tuple: text for Claude, spec for UI
```

## Persistence Limits

- Tables truncated to 50 rows for localStorage persistence (`MAX_PERSISTED_ROWS`)
- Max 20 saved custom views
- Component specs are JSON-serializable (no functions, no circular refs)
