# spider-hoarder

`mcp-research`: a generic, Ariadne-agnostic fetch-and-parse MCP server. Given
a URL or an uploaded file, returns Markdown text. No auth, no layers, no
knowledge of any downstream graph.

See [docs/plans/agentic-research-mcp.md](docs/plans/agentic-research-mcp.md)
for the design. Only the `mcp-research` half of that plan lives here — the
`ingest_text` tool on `article-graph`'s `graph-explorer-api` (blocked on
[Layer Access Control](https://github.com/redhog/article-graph)) and the
nginx/docker-compose wiring into the `ariadne` stack live in the
`article-graph` repo.

## Run locally

```bash
cd mcp-research
docker build -t mcp-research:local .
docker run -p 8080:8080 \
  -e BLOB_TOKEN_SECRET=$(openssl rand -hex 32) \
  -e PUBLIC_BASE_URL=http://localhost:8080 \
  -e RESEARCH_BLOB_URL=file:///data \
  -v $(pwd)/data:/data \
  mcp-research:local
```

MCP endpoint: `POST /mcp`. Tools: `ingest_page`, `get_upload_link`,
`list_files`, `get_file_text`, `get_file_url`.
