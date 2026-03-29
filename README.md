# Odoo Schema MCP Server

A Docker-ready [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI assistants (Claude Code, Open WebUI) the ability to query your Odoo schema directly from a Neo4j graph database.

**What it can do:**
- Search for Odoo fields by natural language description
- Find which views already display a given field
- Show the full inheritance chain of any view
- Find existing fields similar to one you are about to add

> This server is designed to work alongside the [Odoo Neo4j Connector](https://github.com/habbaba/odoo-schema-mcp) module installed in your Odoo instance.

---

## Prerequisites

- Docker and Docker Compose installed on your server
- A running Neo4j instance with Odoo schema data synced (via the Odoo Neo4j Connector module)
- Open WebUI already running (optional — the server also works standalone with Claude Code)

---

## Installation

### 1. Clone this repo into the same folder as your `docker-compose.yml`

```bash
# On your VPS, go to the folder where your docker-compose.yml lives
cd /path/to/your/stack

# Clone the MCP server repo as a subfolder
git clone https://github.com/habbaba/odoo-schema-mcp.git
```

Your folder should now look like:
```
your-stack/
├── docker-compose.yml         ← your existing compose file
└── odoo-schema-mcp/           ← just cloned
    ├── Dockerfile
    ├── server.py
    └── requirements.txt
```

### 2. Add the `odoo-mcp` service to your `docker-compose.yml`

Copy the `odoo-mcp` service block from the `docker-compose.yml` in this repo and paste it into your existing `docker-compose.yml` under `services:`.

Then replace the three placeholder values:

| Placeholder | What to put |
|-------------|-------------|
| `YOUR_NEO4J_HOST` | IP or hostname of your Neo4j server |
| `YOUR_NEO4J_PASSWORD` | Your Neo4j password |
| `YOUR_SECRET_TOKEN` | Run `openssl rand -hex 32` to generate one |

> If Ollama is already in your stack, `OLLAMA_URL: http://ollama:11434` works as-is — no changes needed.

### 3. Start the new service

```bash
docker compose up -d odoo-mcp
```

### 4. Verify it is running

```bash
curl http://localhost:8000/health
# Expected: {"status": "ok", "tenant": "Development"}
```

---

## Connect to Open WebUI

1. Open your Open WebUI instance
2. Go to **Admin Panel → Settings → Tools**
3. Click **Add Tool Server** and enter:
   - **Type:** MCP
   - **URL:** `http://odoo-mcp:8000/mcp`
   - **Auth:** Bearer — paste the value of `MCP_API_TOKEN`
4. Click Save

The four Odoo Schema tools will now appear in your Open WebUI tool list.

---

## Connect to Claude Code

Add this to your project's `.mcp.json` file (on your development machine):

```json
{
  "mcpServers": {
    "odoo-schema": {
      "type": "http",
      "url": "http://YOUR_SERVER_IP:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_TOKEN"
      }
    }
  }
}
```

Replace `YOUR_SERVER_IP` with the public IP or hostname of your server and `YOUR_SECRET_TOKEN` with the same token set in `docker-compose.yml`.

---

## Available Tools

| Tool | Description |
|------|-------------|
| `search_schema` | Search for Odoo fields by natural language — e.g. *"invoice payment date"* |
| `find_views_containing_field` | Find all views that display a specific field |
| `get_view_inheritance_chain` | Show all parent and child views for a given view XML ID |
| `find_similar_fields` | Find existing fields similar to one you are about to add |

When Ollama is running and a vector index exists in Neo4j, `search_schema` and `find_similar_fields` use semantic (AI-powered) search. Otherwise they fall back to keyword search automatically.

---

## Updating

Pull the latest version and rebuild:

```bash
cd odoo-schema-mcp
git pull
docker compose up -d --build
```

---

## Security Notes

- Always set `MCP_API_TOKEN` on a publicly accessible server.
- The `/health` endpoint is always public (no token required) — it returns only status info.
- If both Claude Code and Open WebUI run inside Docker on the same server, you can remove the `ports` section from `docker-compose.yml` entirely and access the server only via the internal Docker network (`http://odoo-mcp:8000/mcp`).
