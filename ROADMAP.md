# Roadmap — odoo-schema-mcp

This document tracks known gaps, planned improvements, and future capabilities for the `odoo-schema-mcp` server.

---

## Current State (v1.0)

The MCP server exposes 7 tools backed by a Neo4j graph populated from a live Odoo 18 instance:

| Tool | What it does |
|------|-------------|
| `search_schema` | Semantic (vector) or keyword search over field nodes |
| `get_model_blueprint` | Full field listing for a model — types, constraints, compute, relations |
| `resolve_model_dependencies` | Maps model names → Odoo module names for `depends` in manifests |
| `find_views_for_model` | Lists all views registered for a model with XML IDs and inherit targets |
| `find_views_containing_field` | Finds every view that references a given field |
| `find_similar_fields` | Finds fields semantically similar to a description (vector search) |
| `get_view_inheritance_chain` | Traces a view's full inheritance hierarchy (parent → children) |

---

## Known Gaps

### Gap 1 — View Inheritance Field Tracking (HIGH PRIORITY)

**Problem:** `fields_used` on each `OdooView` node is extracted from the base view `arch` XML only. When a custom module adds a field to a standard view via inheritance (XPath injection), that field is NOT added to the parent view's `fields_used` property.

**Impact:**
- `find_views_containing_field` may miss a field that exists only via an inherited view extension
- An agent using `find_views_containing_field` before adding a field to a view could get a false negative — thinking the field is not in the view when it actually is (via an extension)
- Duplicate field protection at the view level is incomplete

**Fix needed in:** `neo4j_config.py` → `_run_view_batch()`

The fix should:
1. For each view that has `inherit_id` set (i.e. it is an extension view), parse its `arch` and extract `<field name="...">` tags from XPath bodies
2. Collect those field names and `MERGE` them into the **parent** view's `fields_used` node property
3. Tag the source as `inherited` vs `native` so agents can distinguish

**Effort:** Medium — one additional pass over `ir.ui.view` records where `inherit_id IS NOT NULL`

---

### Gap 2 — Computed Field Dependency Graph (MEDIUM PRIORITY)

**Problem:** `get_model_blueprint` shows that a field is computed and which method computes it, but the `@api.depends()` chain is not in the graph. An agent cannot answer "if I change field X, which computed fields get invalidated?"

**Fix needed:** During field metadata sync, parse `_depends` from `ir.model.fields` (the `depends` attribute stored by the ORM) and create `DEPENDS_ON` edges between field nodes in Neo4j.

**Effort:** Medium — requires parsing the `depends` string and creating edges

---

### Gap 3 — ACL / Security Group Visibility (LOW PRIORITY)

**Problem:** `find_views_for_model` and `get_model_blueprint` do not show which security groups have access to a model or which fields are restricted by `groups=`. An agent generating access rules must query Odoo separately.

**Fix needed:** Sync `ir.model.access` and `ir.rule` records into Neo4j. Link `SecurityGroup` nodes to `OdooModel` nodes via `HAS_ACCESS` edges. Add `groups` property to `OdooField` nodes.

**Effort:** Medium-Large

---

### Gap 4 — Module Source Code Indexing (FUTURE)

**Problem:** The MCP server knows the schema but not the business logic. An agent asked "how does invoice validation work?" cannot answer from Neo4j alone.

**Fix needed:** Index Python method bodies from custom modules into a vector store (pgvector or Neo4j vector index). Add a `search_code` tool for semantic code search.

**Effort:** Large — requires source extraction pipeline

---

## Planned Releases

### v1.1 — View Inheritance Fix (Gap 1)
- Update `_run_view_batch()` to extract fields from inherited view arch
- Merge inherited fields into parent view's `fields_used` with `source: "inherited"` tag
- Add `Sync Field Metadata` + re-run view sync to propagate changes
- Update `find_views_containing_field` query to also search `fields_used_inherited`

### v1.2 — Dependency Graph (Gap 2)
- Add `DEPENDS_ON` edges between `OdooField` nodes
- New MCP tool: `trace_field_impact(field_name, model_name)` — returns all downstream computed fields

### v1.3 — Security Layer (Gap 3)
- Sync `ir.model.access` + `ir.rule` into Neo4j
- New MCP tool: `get_model_access(model_name)` — returns groups and rules

### v2.0 — Code Search
- Index custom module Python source into vector store
- New MCP tool: `search_code(query)` — semantic search over method bodies

---

## Architecture Notes

### Why Neo4j?

The schema is a graph by nature: models reference other models, views inherit views, fields belong to models that inherit other models. Neo4j lets the MCP server answer multi-hop questions (e.g. "which views show fields from models that depend on `account.move`?") in a single Cypher query rather than multiple ORM calls.

### Deployment

The server is stateless — it reads from Neo4j on every request. The Neo4j graph is populated by the `odoo_puppygraph_connector` Odoo module, which syncs schema data via a scheduled cron and on-demand buttons.

```
Odoo instance
  └─ odoo_puppygraph_connector
       ├─ Cron: sync models/fields/views → Neo4j
       └─ Button: Sync Field Metadata (enriches field nodes)

Neo4j (VPS)
  └─ odoo-schema-mcp (Docker)
       └─ MCP tools → Claude Code / any MCP client
```
