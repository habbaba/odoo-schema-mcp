#!/usr/bin/env python3
"""
Odoo Schema MCP Server — Neo4j graph tools for Claude Code and Open WebUI.

Supports two transports:
  stdio           — local use (Claude Code .mcp.json with command/args)
  streamable-http — Docker/network use (Open WebUI, remote Claude Code)

Tools expose capabilities the live Odoo MCP CANNOT provide:
  search_schema               — semantic / keyword search over field descriptions
  find_views_containing_field — which views already display a given field
  get_view_inheritance_chain  — full EXTENDS_VIEW parent chain for a view
  find_similar_fields         — fields semantically similar to a description

Configuration (environment variables):
  NEO4J_URI       bolt://host:7687           (required)
  NEO4J_USER      neo4j                      (default: neo4j)
  NEO4J_PASSWORD  your-password              (required)
  NEO4J_DATABASE  neo4j                      (default: neo4j)
  TENANT_LABEL    Development                (must match Odoo config, default: Development)
  OLLAMA_URL      http://ollama:11434        (optional — enables semantic search)
  EMBED_MODEL     nomic-embed-text           (optional — embedding model name)
  MCP_TRANSPORT   stdio | streamable-http    (default: stdio)
  MCP_HOST        0.0.0.0                    (default: 0.0.0.0, HTTP mode only)
  MCP_PORT        8000                       (default: 8000, HTTP mode only)
  MCP_API_TOKEN   secret-token               (optional — enables bearer auth, HTTP mode only)
"""
import logging
import os
from contextlib import contextmanager
from typing import Optional

import requests as _requests
from mcp.server.fastmcp import FastMCP
from neo4j import GraphDatabase

_logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_NEO4J_URI      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
_NEO4J_USER     = os.environ.get("NEO4J_USER", "neo4j")
_NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
_NEO4J_DB       = os.environ.get("NEO4J_DATABASE", "neo4j")
_TENANT         = os.environ.get("TENANT_LABEL", "Development")
_FIELD_LABEL    = f"{_TENANT}Field"
_OLLAMA_URL     = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_EMBED_MODEL    = os.environ.get("EMBED_MODEL", "nomic-embed-text")
_TRANSPORT      = os.environ.get("MCP_TRANSPORT", "stdio")
_HOST           = os.environ.get("MCP_HOST", "0.0.0.0")
_PORT           = int(os.environ.get("MCP_PORT", "8000"))
_API_TOKEN      = os.environ.get("MCP_API_TOKEN", "")

mcp = FastMCP(f"Odoo Graph [{_TENANT}]", host=_HOST, port=_PORT)

# ── Neo4j helpers ─────────────────────────────────────────────────────────────

@contextmanager
def _session():
    driver = GraphDatabase.driver(_NEO4J_URI, auth=(_NEO4J_USER, _NEO4J_PASSWORD))
    try:
        with driver.session(database=_NEO4J_DB) as s:
            yield s
    finally:
        driver.close()


def _q(session, cypher: str, **params) -> list[dict]:
    return [dict(r) for r in session.run(cypher, **params)]


# ── Embedding helper ──────────────────────────────────────────────────────────

def _embed(text: str) -> Optional[list]:
    """
    Call Ollama to embed text for vector search.
    Returns None if Ollama is unreachable or returns an error — callers fall back to keyword search.
    Logs a warning so failures are visible in Docker logs.
    """
    if not _OLLAMA_URL:
        return None
    try:
        resp = _requests.post(
            f"{_OLLAMA_URL}/api/embed",
            json={"model": _EMBED_MODEL, "input": text},
            timeout=15,
        )
        if resp.status_code == 405:
            # Older Ollama API — fall back to /api/embeddings endpoint
            resp = _requests.post(
                f"{_OLLAMA_URL}/api/embeddings",
                json={"model": _EMBED_MODEL, "prompt": text},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("embedding")
        if not resp.ok:
            _logger.warning(
                "Ollama embedding request failed: HTTP %s — %s. "
                "Vector search disabled for this request.",
                resp.status_code, resp.text[:200],
            )
            return None
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings", [])
        return embeddings[0] if embeddings else None
    except _requests.exceptions.ConnectionError:
        _logger.warning(
            "Ollama unreachable at %s — vector search disabled for this request.",
            _OLLAMA_URL,
        )
        return None
    except _requests.exceptions.Timeout:
        _logger.warning("Ollama request timed out — vector search disabled for this request.")
        return None
    except Exception as e:
        _logger.warning("Unexpected error calling Ollama embedding API: %s", e)
        return None


def _keyword_search(session, query: str, top_k: int) -> list[dict]:
    """
    Keyword search across field_name, field_label, and chunk_text.
    Splits query into terms, matches any term (OR logic).
    """
    terms = [t.strip().lower() for t in query.split() if len(t.strip()) > 2]
    if not terms:
        return []
    conditions = " OR ".join(
        f"toLower(f.field_name) CONTAINS $t{i} "
        f"OR toLower(f.field_label) CONTAINS $t{i} "
        f"OR toLower(f.chunk_text) CONTAINS $t{i}"
        for i in range(len(terms))
    )
    params = {f"t{i}": t for i, t in enumerate(terms)}
    return _q(
        session,
        f"MATCH (f:`{_FIELD_LABEL}`) "
        f"WHERE {conditions} "
        "RETURN f.model AS model, f.field_name AS name, "
        "       f.field_label AS label, f.field_type AS ftype, "
        "       f.chunk_text AS chunk "
        f"LIMIT {top_k}",
        **params,
    )


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_schema(query: str, top_k: int = 15) -> str:
    """
    Search the Odoo schema for fields matching a natural language description.

    Uses vector similarity search when Ollama is running and a vector index
    exists in Neo4j. Falls back to keyword search automatically.

    Use this BEFORE adding a new field to check whether a field tracking the
    same concept already exists — either on the target model or a related one.
    Also useful when you know what a field does but not its technical name.

    Examples:
      search_schema("delivery deadline date")
      search_schema("invoice payment state")
      search_schema("customer credit limit warning")
      search_schema("stock reservation quantity")

    Args:
        query:  Natural language description of the field you are looking for
        top_k:  Maximum results to return (default 15)
    """
    vector = _embed(query)

    with _session() as s:
        search_type = "keyword"
        results = None

        if vector:
            index_name = f"{_TENANT}_field_embeddings"
            try:
                results = _q(s,
                    "CALL db.index.vector.queryNodes($idx, $k, $vec) "
                    "YIELD node, score "
                    "RETURN node.model AS model, node.field_name AS name, "
                    "       node.field_label AS label, node.field_type AS ftype, "
                    "       node.chunk_text AS chunk, score "
                    "ORDER BY score DESC",
                    idx=index_name, k=top_k, vec=vector)
                search_type = "vector (semantic)"
            except Exception as e:
                _logger.warning(
                    "Vector index query failed for index '%s': %s — falling back to keyword search.",
                    index_name, e,
                )
                results = None

        if not results:
            results = _keyword_search(s, query, top_k)
            if vector:
                search_type = "keyword (vector index unavailable — check Neo4j index and embeddings)"
            else:
                search_type = "keyword (Ollama unavailable — set OLLAMA_URL to enable semantic search)"

    if not results:
        return f"No fields found matching '{query}'. Try a shorter or broader query."

    out = [
        f"SCHEMA SEARCH: '{query}'",
        f"Mode: {search_type} | Results: {len(results)}",
        "",
    ]
    for r in results:
        score_str = f"  score={r['score']:.3f}" if "score" in r else ""
        out.append(f"  {r.get('model', '')}.{r.get('name', '')}")
        out.append(f"    Label : {r.get('label') or ''}")
        out.append(f"    Type  : {r.get('ftype') or ''}{score_str}")
        chunk_parts = (r.get("chunk") or "").split(" | ")
        if len(chunk_parts) > 1:
            out.append(f"    Desc  : {' | '.join(chunk_parts[-2:])[:120]}")
        out.append("")

    return "\n".join(out)


@mcp.tool()
def find_views_containing_field(field_name: str, model_name: str = "") -> str:
    """
    Find all views that already display a specific field.

    Use this before adding a field to an existing view — to check whether
    it is already shown in another view of the same model, or to find which
    view is the right place to add it.

    Also useful when inheriting a view: shows you exactly which existing
    views reference a field so you can target the right inherit_id.

    Args:
        field_name:  Technical field name, e.g. 'partner_id', 'state', 'amount_total'
        model_name:  Optional — restrict results to one model, e.g. 'account.move'
    """
    model_filter = "AND v.model = $model " if model_name else ""
    model_param  = {"field": field_name, "model": model_name} if model_name else {"field": field_name}

    with _session() as s:
        # Native: field appears in the view's own arch
        native = _q(s,
            f"MATCH (v:OdooView:`{_TENANT}`) "
            f"WHERE $field IN v.fields_used {model_filter}"
            "RETURN v.external_id AS ext_id, v.key AS key, "
            "       v.view_type AS vtype, v.model AS model, "
            "       v.module AS module, v.priority AS priority, "
            "       'native' AS source "
            "ORDER BY v.model, v.view_type, v.priority",
            **model_param)

        # Inherited: field is injected into this view by an extension view
        inherited = _q(s,
            f"MATCH (v:OdooView:`{_TENANT}`) "
            f"WHERE $field IN coalesce(v.fields_used_inherited, []) {model_filter}"
            "  AND NOT ($field IN coalesce(v.fields_used, [])) "
            "RETURN v.external_id AS ext_id, v.key AS key, "
            "       v.view_type AS vtype, v.model AS model, "
            "       v.module AS module, v.priority AS priority, "
            "       'inherited' AS source "
            "ORDER BY v.model, v.view_type, v.priority",
            **model_param)

    views = native + inherited

    if not views:
        scope = f" on model '{model_name}'" if model_name else ""
        return (
            f"Field '{field_name}' is not referenced in any indexed view{scope}.\n"
            "It may not be displayed anywhere yet, or the view cron may still be running."
        )

    out = [
        f"VIEWS CONTAINING FIELD: '{field_name}'"
        + (f" (model: {model_name})" if model_name else ""),
        f"Found in {len(views)} view(s)  ({len(native)} native, {len(inherited)} via inheritance)",
        "",
    ]

    current_model = None
    for v in views:
        m = v.get("model") or "(no model — QWeb template)"
        if m != current_model:
            out.append(f"  [{m}]")
            current_model = m
        ext    = v.get("ext_id") or v.get("key") or "(no external_id)"
        vtype  = v.get("vtype") or ""
        module = v.get("module") or ""
        tag    = " [inherited]" if v.get("source") == "inherited" else ""
        out.append(f"    {ext:<55} {vtype:<10} {module}{tag}")

    return "\n".join(out)


@mcp.tool()
def get_view_inheritance_chain(external_id: str) -> str:
    """
    Traverse the full EXTENDS_VIEW chain for a view — both upward (parents)
    and downward (all modules that extend this view).

    Use this before writing a view inheritance to understand:
      - What this view itself inherits from (its parent chain)
      - Which other modules already extend it (so you know what's already there
        and avoid conflicts)

    Args:
        external_id: Fully-qualified view XML ID, e.g. 'account.view_move_form'
    """
    with _session() as s:
        parents = _q(s,
            f"MATCH path = (v:OdooView:`{_TENANT}` {{id: $eid}})"
            f"-[:EXTENDS_VIEW*1..10]->(ancestor:OdooView:`{_TENANT}`) "
            "RETURN ancestor.external_id AS ext_id, ancestor.view_type AS vtype, "
            "       ancestor.module AS module, length(path) AS depth "
            "ORDER BY depth",
            eid=external_id)

        children = _q(s,
            f"MATCH (child:OdooView:`{_TENANT}`)-[:EXTENDS_VIEW]->"
            f"(v:OdooView:`{_TENANT}` {{id: $eid}}) "
            "RETURN child.external_id AS ext_id, child.view_type AS vtype, "
            "       child.module AS module "
            "ORDER BY child.module",
            eid=external_id)

        meta = _q(s,
            f"MATCH (v:OdooView:`{_TENANT}` {{id: $eid}}) "
            "RETURN v.model AS model, v.view_type AS vtype, "
            "       v.module AS module, v.priority AS priority",
            eid=external_id)

    if not meta:
        return (
            f"View '{external_id}' not found in the graph.\n"
            "Check: correct external_id? View cron may still be running."
        )

    info = meta[0]
    out = [
        f"VIEW INHERITANCE CHAIN: {external_id}",
        f"Model: {info.get('model') or '(QWeb template)'}  "
        f"Type: {info.get('vtype') or ''}  "
        f"Module: {info.get('module') or ''}  "
        f"Priority: {info.get('priority') or 16}",
        "",
    ]

    if parents:
        out.append(f"PARENT CHAIN (this view inherits from):")
        for p in parents:
            indent = "  " + ("  " * (p.get("depth", 1) - 1))
            out.append(f"{indent}↑ {p.get('ext_id') or '(unknown)'}  [{p.get('module') or ''}]")
    else:
        out.append("PARENT CHAIN: (root — does not extend any view)")

    out += ["", f"EXTENDED BY ({len(children)} module(s) extend this view):"]
    if children:
        for c in children:
            out.append(f"  ↓ {(c.get('ext_id') or '(unknown)'):<55} [{c.get('module') or ''}]")
    else:
        out.append("  (no modules extend this view yet)")

    return "\n".join(out)


@mcp.tool()
def find_similar_fields(description: str, model_name: str = "", top_k: int = 10) -> str:
    """
    Find existing Odoo fields semantically similar to what you are about to add.

    Call this BEFORE defining a new custom field. If a similar field already
    exists — on the target model or a related one — you should reuse it rather
    than adding a duplicate.

    Requires Ollama to be running for vector similarity. Falls back to keyword
    search if Ollama is unavailable.

    Args:
        description:  Plain English description of the field you plan to add,
                      e.g. 'date when payment is expected to arrive'
                      or 'flag indicating the invoice needs manual review'
        model_name:   Optional — if set, shows results on this model first
        top_k:        Maximum results (default 10)
    """
    vector = _embed(description)

    with _session() as s:
        search_type = "keyword"
        results = None

        if vector:
            index_name = f"{_TENANT}_field_embeddings"
            try:
                results = _q(s,
                    "CALL db.index.vector.queryNodes($idx, $k, $vec) "
                    "YIELD node, score "
                    "RETURN node.model AS model, node.field_name AS name, "
                    "       node.field_label AS label, node.field_type AS ftype, "
                    "       node.chunk_text AS chunk, score "
                    "ORDER BY score DESC",
                    idx=index_name, k=top_k, vec=vector)
                search_type = "vector (semantic)"
            except Exception as e:
                _logger.warning(
                    "Vector index query failed for index '%s': %s — falling back to keyword search.",
                    index_name, e,
                )
                results = None

        if not results:
            results = _keyword_search(s, description, top_k)
            if vector:
                search_type = "keyword (vector index unavailable — check Neo4j index and embeddings)"
            else:
                search_type = "keyword (Ollama unavailable — set OLLAMA_URL to enable semantic search)"

    if not results:
        return f"No similar fields found for: '{description}'"

    if model_name:
        target = [r for r in results if r.get("model") == model_name]
        others = [r for r in results if r.get("model") != model_name]
        results = target + others

    out = [
        f"SIMILAR FIELDS TO: '{description}'",
        f"Mode: {search_type} | Results: {len(results)}",
        "",
        "Review these before adding a new field — reuse if one fits:",
        "",
    ]

    for r in results:
        score_str = f"  score={r['score']:.3f}" if "score" in r else ""
        model = r.get("model") or ""
        marker = " ◄ target model" if model_name and model == model_name else ""
        out.append(f"  {model}.{r.get('name', '')}{marker}")
        out.append(f"    Label : {r.get('label') or ''}")
        out.append(f"    Type  : {r.get('ftype') or ''}{score_str}")
        chunk_parts = (r.get("chunk") or "").split(" | ")
        if len(chunk_parts) > 1:
            out.append(f"    Desc  : {' | '.join(chunk_parts[-2:])[:120]}")
        out.append("")

    return "\n".join(out)


# ── Blueprint / dependency / view tools ───────────────────────────────────────

def _base_module(modules_str: str) -> str:
    """
    Pick the primary defining module from a comma-separated module list.
    Heuristic: namespace match first, then known namespace map, then shortest.
    """
    if not modules_str:
        return ""
    modules = [m.strip() for m in modules_str.split(",") if m.strip()]
    if not modules:
        return ""
    if len(modules) == 1:
        return modules[0]
    ns_map = {
        "res": "base", "ir": "base", "mail": "mail",
        "account": "account", "sale": "sale", "purchase": "purchase",
        "stock": "stock", "hr": "hr", "project": "project",
        "crm": "crm", "mrp": "mrp", "product": "product",
    }
    first_ns = modules[0].split("_")[0]
    if first_ns in ns_map and ns_map[first_ns] in modules:
        return ns_map[first_ns]
    for m in modules:
        ns = m.split("_")[0]
        if ns == m and ns in ns_map:
            return m
    return min(modules, key=len)


@mcp.tool()
def get_model_blueprint(model_name: str) -> str:
    """
    Return the full field blueprint for an Odoo model — every field with its
    type, label, required/store/readonly flags, comodel, compute expression,
    related path, selection values, help text, and defining module.

    Use this BEFORE inheriting a model or adding fields — to see exactly what
    already exists so you never duplicate a field.

    Also use it to find the correct field names for XML views, Python compute
    methods, and domain expressions.

    Args:
        model_name: Technical model name, e.g. 'account.move', 'res.partner'
    """
    with _session() as s:
        meta = _q(s,
            f"MATCH (m:OdooModel:`{_TENANT}` {{id: $model}}) "
            "RETURN m._model_name AS label, m._module AS modules",
            model=model_name)

        fields = _q(s,
            f"MATCH (f:`{_FIELD_LABEL}` {{model: $model}}) "
            "RETURN f.field_name AS name, f.field_type AS ftype, "
            "       f.field_label AS label, f.required AS required, "
            "       f.store AS store, f.readonly AS readonly, "
            "       f.compute AS compute, f.related AS related, "
            "       f.relation AS relation, f.modules AS modules, "
            "       f.defining_module AS def_mod, "
            "       f.selection_values AS sel, f.help AS help "
            "ORDER BY f.field_name",
            model=model_name)

    if not fields:
        return (
            f"Model '{model_name}' not found in the graph.\n"
            "Check the model name or run 'Sync Schema Only' in Odoo."
        )

    model_label = meta[0].get("label") or model_name if meta else model_name

    out = [
        f"MODEL BLUEPRINT: {model_name}",
        f"Label   : {model_label}",
        f"Fields  : {len(fields)}",
        "",
    ]

    # Group by type for readability
    TYPE_ORDER = ["many2one", "one2many", "many2many", "selection",
                  "char", "text", "integer", "float", "monetary",
                  "boolean", "date", "datetime", "json"]
    type_groups = {}
    for f in fields:
        t = f.get("ftype") or "other"
        type_groups.setdefault(t, []).append(f)
    for t in TYPE_ORDER:
        if t not in type_groups:
            continue
        out.append(f"── {t.upper()} ──")
        for f in type_groups.pop(t):
            _append_field_line(out, f)
    for t, fs in type_groups.items():
        out.append(f"── {t.upper()} ──")
        for f in fs:
            _append_field_line(out, f)

    return "\n".join(out)


def _append_field_line(out: list, f: dict):
    flags = []
    if f.get("required"):  flags.append("required")
    if f.get("readonly"):  flags.append("readonly")
    if not f.get("store"): flags.append("NOT stored")
    if f.get("compute"):   flags.append(f"compute={f['compute']}")
    if f.get("related"):   flags.append(f"related={f['related']}")

    flag_str = f"  [{', '.join(flags)}]" if flags else ""
    comodel  = f"  → {f['relation']}" if f.get("relation") else ""
    sel      = f"  ({f['sel']})" if f.get("sel") else ""
    mod      = f"  [{f.get('def_mod') or ''}]" if f.get("def_mod") else ""

    out.append(f"  {f.get('name', ''):<40} {f.get('label') or ''}{comodel}{sel}{flag_str}{mod}")
    if f.get("help"):
        out.append(f"    help: {f['help'][:120]}")


@mcp.tool()
def resolve_model_dependencies(model_names: str) -> str:
    """
    Given a comma-separated list of model names, return the `depends` list
    for your module manifest — the primary module that defines each model.

    Use this EVERY TIME you write a __manifest__.py. Pass all models you
    are inheriting or referencing and copy the result directly into `depends`.

    Args:
        model_names: Comma-separated model names,
                     e.g. 'account.move,res.partner,sale.order'
    """
    models = [m.strip() for m in model_names.split(",") if m.strip()]
    if not models:
        return "No model names provided."

    with _session() as s:
        results = _q(s,
            f"MATCH (m:OdooModel:`{_TENANT}`) "
            "WHERE m.id IN $models "
            "RETURN m.id AS model, m._module AS modules",
            models=models)

    found = {r["model"]: r["modules"] for r in results}
    not_found = [m for m in models if m not in found]

    depends = []
    rows = []
    for model in models:
        if model not in found:
            rows.append((model, "", "NOT FOUND"))
            continue
        base = _base_module(found[model])
        rows.append((model, found[model] or "", base))
        if base and base not in depends:
            depends.append(base)

    out = [
        f"RESOLVE MODEL DEPENDENCIES",
        f"Models requested: {len(models)}",
        "",
        f"  {'Model':<35} {'Defining module':<20} {'All modules'}",
        "  " + "-" * 80,
    ]
    for model, all_mods, base in rows:
        out.append(f"  {model:<35} {base:<20} {all_mods[:60]}")

    out += [
        "",
        "COPY INTO __manifest__.py depends:",
        f"  'depends': {depends},",
    ]

    if not_found:
        out += ["", f"WARNING — not found in graph: {not_found}"]

    return "\n".join(out)


@mcp.tool()
def find_views_for_model(model_name: str) -> str:
    """
    Return all views defined for a model, grouped by view type.

    Use this before writing a view inheritance to see every existing view
    for the model — form, list, kanban, search, activity — and their
    external IDs so you can pick the right inherit_id.

    Args:
        model_name: Technical model name, e.g. 'account.move'
    """
    with _session() as s:
        views = _q(s,
            f"MATCH (v:OdooView:`{_TENANT}` {{model: $model}}) "
            "RETURN v.external_id AS ext_id, v.view_type AS vtype, "
            "       v.module AS module, v.priority AS priority, "
            "       v.key AS key "
            "ORDER BY v.view_type, v.priority",
            model=model_name)

    if not views:
        return (
            f"No views found for model '{model_name}'.\n"
            "Check the model name or run 'Sync Schema Only' in Odoo."
        )

    out = [
        f"VIEWS FOR MODEL: {model_name}",
        f"Total: {len(views)}",
        "",
    ]

    current_type = None
    for v in views:
        vtype = v.get("vtype") or "other"
        if vtype != current_type:
            out.append(f"── {vtype.upper()} ──")
            current_type = vtype
        ext_id = v.get("ext_id") or v.get("key") or "(no id)"
        module = v.get("module") or ""
        priority = v.get("priority") or 16
        out.append(f"  {ext_id:<55} [{module}]  priority={priority}")

    return "\n".join(out)


@mcp.tool()
def trace_field_impact(field_name: str, model_name: str) -> str:
    """
    Trace which computed fields will be invalidated when a given field changes.

    Traverses DEPENDS_ON edges in reverse — starting from the target field and
    following all computed fields that depend on it, recursively up to 6 hops.

    Use this BEFORE modifying a field's compute logic or storage — to understand
    the full downstream impact across the model's computed field graph.

    Also use it to understand why a field is expensive: if many computed fields
    depend on it, changing it triggers a large recompute cascade.

    Args:
        field_name:  Technical field name, e.g. 'amount_untaxed'
        model_name:  Model the field belongs to, e.g. 'account.move'
    """
    with _session() as s:
        # Direct dependants: computed fields whose @api.depends() includes this field
        direct = _q(s,
            f"MATCH (computed:`{_FIELD_LABEL}`)-[r:DEPENDS_ON]->(src:`{_FIELD_LABEL}` "
            f"  {{model: $model, field_name: $field}}) "
            "RETURN computed.field_name AS name, computed.field_type AS ftype, "
            "       computed.compute AS compute_fn, r.path AS path, "
            "       1 AS depth "
            "ORDER BY computed.field_name",
            model=model_name, field=field_name)

        # Full cascade: all computed fields reachable in the dependency graph
        cascade = _q(s,
            f"MATCH path = (computed:`{_FIELD_LABEL}`)-[:DEPENDS_ON*1..6]->"
            f"(src:`{_FIELD_LABEL}` {{model: $model, field_name: $field}}) "
            "WHERE length(path) > 1 "
            "RETURN DISTINCT computed.field_name AS name, "
            "       computed.field_type AS ftype, "
            "       computed.compute AS compute_fn, "
            "       length(path) AS depth "
            "ORDER BY depth, computed.field_name",
            model=model_name, field=field_name)

        # What does the field itself depend on?
        depends_on = _q(s,
            f"MATCH (src:`{_FIELD_LABEL}` {{model: $model, field_name: $field}})"
            f"-[r:DEPENDS_ON]->(dep:`{_FIELD_LABEL}`) "
            "RETURN dep.field_name AS name, dep.field_type AS ftype, "
            "       dep.model AS dep_model, r.path AS path "
            "ORDER BY r.path",
            model=model_name, field=field_name)

    if not direct and not depends_on:
        return (
            f"No dependency edges found for {model_name}.{field_name}.\n"
            "Either the field has no @api.depends(), nothing depends on it,\n"
            "or 'Sync Field Dependencies' has not been run yet."
        )

    out = [
        f"FIELD IMPACT TRACE: {model_name}.{field_name}",
        "",
    ]

    if depends_on:
        out.append(f"THIS FIELD DEPENDS ON ({len(depends_on)} source(s)):")
        for d in depends_on:
            model_tag = f"  [{d.get('dep_model')}]" if d.get("dep_model") != model_name else ""
            out.append(f"  ← {d.get('path') or d.get('name')}{model_tag}")
        out.append("")

    if direct:
        out.append(f"DIRECT DEPENDANTS — invalidated when {field_name} changes ({len(direct)}):")
        for d in direct:
            fn = d.get("compute_fn") or ""
            fn_str = f"  [{fn}]" if fn else ""
            out.append(f"  → {d.get('name'):<40} {d.get('ftype') or ''}{fn_str}")
            out.append(f"      via depends path: {d.get('path') or ''}")
    else:
        out.append(f"DIRECT DEPENDANTS: none — no computed field declares @api.depends({field_name!r})")

    if cascade:
        out += ["", f"FULL CASCADE (indirect, up to 6 hops) — {len(cascade)} field(s):"]
        for c in cascade:
            indent = "  " + ("  " * (int(c.get("depth", 1)) - 1))
            fn = c.get("compute_fn") or ""
            fn_str = f"  [{fn}]" if fn else ""
            out.append(f"{indent}→ {c.get('name'):<40} {c.get('ftype') or ''}{fn_str}")

    return "\n".join(out)


@mcp.tool()
def get_model_access(model_name: str) -> str:
    """
    Return the full security picture for an Odoo model:
      - Which security groups can read / write / create / delete records
      - Global (public) access rules with no group restriction
      - Record rules (ir.rule) with their filter domains and scope
      - Fields restricted to specific groups (groups= attribute)

    Use this before writing ir.model.access.csv or ir.rule records — to see
    what already exists and which groups you should reference.

    Also use it when debugging access errors: find exactly which group is
    missing the permission causing the issue.

    Args:
        model_name: Technical model name, e.g. 'account.move', 'res.partner'
    """
    with _session() as s:
        # Group-based ACL
        group_acl = _q(s,
            f"MATCH (g:SecurityGroup:`{_TENANT}`)-[r:CAN_ACCESS]->"
            f"(m:OdooModel:`{_TENANT}` {{id: $model}}) "
            "RETURN g.full_name AS group, g.id AS group_id, "
            "       r.read AS read, r.write AS write, "
            "       r.create AS create, r.unlink AS unlink, r.name AS rule_name "
            "ORDER BY g.full_name",
            model=model_name)

        # Global access (no group)
        global_acl = _q(s,
            f"MATCH (m:OdooModel:`{_TENANT}` {{id: $model}})-[r:GLOBAL_ACCESS]->(m) "
            "RETURN r.read AS read, r.write AS write, "
            "       r.create AS create, r.unlink AS unlink, r.name AS rule_name",
            model=model_name)

        # Record rules
        record_rules = _q(s,
            f"MATCH (r:RecordRule:`{_TENANT}`)-[:APPLIES_TO]->"
            f"(m:OdooModel:`{_TENANT}` {{id: $model}}) "
            "OPTIONAL MATCH (r)-[:SCOPED_TO]->(g:SecurityGroup:`{_TENANT}`) "
            "RETURN r.name AS name, r.domain AS domain, "
            "       r.read AS read, r.write AS write, "
            "       r.create AS create, r.unlink AS unlink, "
            "       r.is_global AS is_global, "
            "       collect(g.full_name) AS groups "
            "ORDER BY r.is_global DESC, r.name",
            model=model_name)

        # Field-level group restrictions
        restricted_fields = _q(s,
            f"MATCH (f:`{_FIELD_LABEL}` {{model: $model}}) "
            "WHERE f.field_groups IS NOT NULL AND size(f.field_groups) > 0 "
            "RETURN f.field_name AS name, f.field_label AS label, "
            "       f.field_groups AS groups "
            "ORDER BY f.field_name",
            model=model_name)

    if not group_acl and not global_acl and not record_rules:
        return (
            f"No security data found for model '{model_name}'.\n"
            "Run 'Sync Security Data' in the Odoo Neo4j config, or check the model name."
        )

    def perm_str(r):
        p = []
        if r.get("read"):   p.append("R")
        if r.get("write"):  p.append("W")
        if r.get("create"): p.append("C")
        if r.get("unlink"): p.append("D")
        return "/".join(p) if p else "none"

    out = [
        f"MODEL ACCESS: {model_name}",
        "",
    ]

    if global_acl:
        out.append(f"GLOBAL ACCESS (no group restriction) — {len(global_acl)} rule(s):")
        for r in global_acl:
            out.append(f"  {r.get('rule_name') or '(unnamed)'}  perms={perm_str(r)}")
        out.append("")

    if group_acl:
        out.append(f"GROUP ACCESS RULES — {len(group_acl)} group(s):")
        out.append(f"  {'Group':<50} {'Perms':<12} Rule name")
        out.append("  " + "-" * 80)
        for r in group_acl:
            out.append(
                f"  {(r.get('group') or r.get('group_id') or ''):<50} "
                f"{perm_str(r):<12} {r.get('rule_name') or ''}"
            )
        out.append("")

    if record_rules:
        out.append(f"RECORD RULES (ir.rule) — {len(record_rules)} rule(s):")
        for r in record_rules:
            scope = "GLOBAL" if r.get("is_global") else ", ".join(r.get("groups") or []) or "(no group)"
            out.append(f"  {r.get('name') or '(unnamed)'}  [{scope}]  perms={perm_str(r)}")
            domain = (r.get("domain") or "[]").strip()
            if domain and domain != "[]":
                out.append(f"    domain: {domain[:120]}")
        out.append("")

    if restricted_fields:
        out.append(f"FIELD-LEVEL RESTRICTIONS — {len(restricted_fields)} field(s) have groups=:")
        for f in restricted_fields:
            groups = ", ".join(f.get("groups") or [])
            out.append(f"  {f.get('name'):<40} {f.get('label') or ''}  →  {groups}")

    return "\n".join(out)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport=_TRANSPORT)
