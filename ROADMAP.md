# Roadmap — odoo-schema-mcp

This document tracks known gaps, planned improvements, and future capabilities for the `odoo-schema-mcp` server.

---

## Current State (v1.3)

The MCP server exposes 9 tools backed by a Neo4j graph populated from a live Odoo 18 instance:

| Tool | What it does |
|------|-------------|
| `search_schema` | Semantic (vector) or keyword search over field nodes |
| `get_model_blueprint` | Full field listing for a model — types, constraints, compute, relations |
| `resolve_model_dependencies` | Maps model names → Odoo module names for `depends` in manifests |
| `find_views_for_model` | Lists all views registered for a model with XML IDs and inherit targets |
| `find_views_containing_field` | Finds every view that references a field — native **and** inherited via XPath |
| `find_similar_fields` | Finds fields semantically similar to a description (vector search) |
| `get_view_inheritance_chain` | Traces a view's full inheritance hierarchy (parent → children) |
| `trace_field_impact` | Shows all computed fields invalidated when a field changes (DEPENDS_ON graph) |
| `get_model_access` | Returns full ACL picture — group rules, record rules, field-level restrictions |

---

## Completed

### v1.1 — View Inheritance Field Tracking ✅
- Added `_propagate_inherited_fields()` to `neo4j_config.py`
- Runs automatically after view sync cron completes
- "Propagate Inherited Fields" button for on-demand runs
- `find_views_containing_field` now returns native + `[inherited]` results
- Gap: `fields_used` only reflected base arch — **closed**

### v1.2 — Computed Field Dependency Graph ✅
- Added `_push_field_depends_edges()` — reads `ir.model.fields.depends`, creates `DEPENDS_ON` edges in Neo4j
- "Sync Field Dependencies" button on config form
- New MCP tool: `trace_field_impact(field_name, model_name)` — traverses `DEPENDS_ON` edges forward and reverse, up to 6 hops
- Gap: agent could not answer "what breaks if I change this field" — **closed**

### v1.3 — ACL / Security Group Visibility ✅
- Added `_push_security_data()` — syncs `res.groups`, `ir.model.access`, `ir.rule` to Neo4j
- Creates `SecurityGroup` nodes, `CAN_ACCESS` edges, `GLOBAL_ACCESS` edges, `RecordRule` nodes
- Adds `field_groups` property to `DevelopmentField` nodes
- "Sync Security Data" button on config form
- New MCP tool: `get_model_access(model_name)` — returns all access rules, record rules, field restrictions
- Gap: agent had to query Odoo separately for ACL data — **closed**

---

## Remaining

### v2.0 — Module Source Code Indexing (FUTURE)

**Problem:** The MCP server knows the schema but not the business logic. An agent asked "how does invoice validation work?" cannot answer from Neo4j alone.

**Fix needed:** Index Python method bodies from custom modules into a vector store (pgvector or Neo4j vector index). Add a `search_code` tool for semantic code search.

**Effort:** Large — requires source extraction pipeline

---

## Sync Checklist

After installing/upgrading modules in Odoo, run these buttons on the Neo4j config form in order:

1. **Sync Schema Only** — re-syncs models, fields, views
2. **Sync Field Metadata** — enriches field nodes with full ir.model.fields properties
3. **Propagate Inherited Fields** — updates `fields_used_inherited` on parent view nodes
4. **Sync Field Dependencies** — creates `DEPENDS_ON` edges for computed fields
5. **Sync Security Data** — syncs groups, ACL rules, record rules
6. **Generate Embeddings** — re-embeds any new/changed fields for vector search

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
       └─ Buttons: Sync Field Metadata, Propagate Inherited Fields,
                   Sync Field Dependencies, Sync Security Data

Neo4j (VPS)
  └─ odoo-schema-mcp (Docker)
       └─ 9 MCP tools → Claude Code / any MCP client
```
