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
import os
from contextlib import contextmanager
from typing import Optional

import requests as _requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from neo4j import GraphDatabase
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.applications import Starlette

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

mcp = FastMCP(f"Odoo Graph [{_TENANT}]")

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
    Returns None silently if Ollama is unreachable — callers fall back to keyword search.
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
            resp = _requests.post(
                f"{_OLLAMA_URL}/api/embeddings",
                json={"model": _EMBED_MODEL, "prompt": text},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("embedding")
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings", [])
        return embeddings[0] if embeddings else None
    except Exception:
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
            index_name = f"{_TENANT}FieldVector"
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
            except Exception:
                results = None

        if not results:
            results = _keyword_search(s, query, top_k)
            search_type = "keyword" if not vector else "keyword (vector index not yet created)"

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
    with _session() as s:
        if model_name:
            views = _q(s,
                f"MATCH (v:OdooView:`{_TENANT}`) "
                "WHERE $field IN v.fields_used AND v.model = $model "
                "RETURN v.external_id AS ext_id, v.key AS key, "
                "       v.view_type AS vtype, v.model AS model, "
                "       v.module AS module, v.priority AS priority "
                "ORDER BY v.model, v.view_type, v.priority",
                field=field_name, model=model_name)
        else:
            views = _q(s,
                f"MATCH (v:OdooView:`{_TENANT}`) "
                "WHERE $field IN v.fields_used "
                "RETURN v.external_id AS ext_id, v.key AS key, "
                "       v.view_type AS vtype, v.model AS model, "
                "       v.module AS module, v.priority AS priority "
                "ORDER BY v.model, v.view_type, v.priority",
                field=field_name)

    if not views:
        scope = f" on model '{model_name}'" if model_name else ""
        return (
            f"Field '{field_name}' is not referenced in any indexed view{scope}.\n"
            "It may not be displayed anywhere yet, or the view cron may still be running."
        )

    out = [
        f"VIEWS CONTAINING FIELD: '{field_name}'"
        + (f" (model: {model_name})" if model_name else ""),
        f"Found in {len(views)} view(s)",
        "",
    ]

    current_model = None
    for v in views:
        m = v.get("model") or "(no model — QWeb template)"
        if m != current_model:
            out.append(f"  [{m}]")
            current_model = m
        ext = v.get("ext_id") or v.get("key") or "(no external_id)"
        vtype = v.get("vtype") or ""
        module = v.get("module") or ""
        out.append(f"    {ext:<55} {vtype:<10} {module}")

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
            index_name = f"{_TENANT}FieldVector"
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
            except Exception:
                results = None

        if not results:
            results = _keyword_search(s, description, top_k)
            search_type = "keyword" if not vector else "keyword (vector index not yet created)"

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


# ── HTTP entry point (SSE transport) ──────────────────────────────────────────

async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "tenant": _TENANT})


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Simple pre-shared bearer token guard.
    Skipped entirely when MCP_API_TOKEN is not set (open access).
    Health endpoint is always public.
    """
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if _API_TOKEN:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {_API_TOKEN}":
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


def _build_http_app():
    """
    Wrap the FastMCP SSE ASGI app with auth middleware and a public /health route.
    Exposes /sse (GET) and /messages (POST) — compatible with Claude Code type=sse.
    """
    mcp_asgi = mcp.sse_app()

    # Inject health route
    mcp_asgi.routes.insert(0, Route("/health", _health, methods=["GET"]))

    # Add auth middleware
    from starlette.middleware import Middleware
    app = Starlette(
        routes=[Route("/health", _health, methods=["GET"])],
        middleware=[Middleware(_BearerAuthMiddleware)],
    )
    app.mount("/", mcp_asgi)
    return app


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if _TRANSPORT == "sse":
        app = _build_http_app()
        uvicorn.run(app, host=_HOST, port=_PORT)
    else:
        mcp.run()
