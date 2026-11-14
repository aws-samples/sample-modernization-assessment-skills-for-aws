# Client and tool compatibility

Reference date: 2026-10-16. Skills use common name/description metadata and portable scripts, without client-specific execution APIs. agents/openai.yaml supplies optional Codex UI metadata.

| Client | Project skills | User skills | Project MCP | User MCP |
|---|---|---|---|---|
| Claude Code | .claude/skills | ~/.claude/skills | .mcp.json | ~/.claude.json mcpServers |
| Codex | .agents/skills | ~/.agents/skills | .codex/config.toml | ~/.codex/config.toml |
| Kiro | .kiro/skills | ~/.kiro/skills | .kiro/settings/mcp.json | ~/.kiro/settings/mcp.json |

The `aws-skills-install` skill carries a file manifest and these snippets so a client installs the skills by following the guide; `setup_toolkit.py` handles companions and MCP merging, `doctor.py` verifies. Use supported client versions and verify current official paths. Do not reuse old .codex/skills examples blindly. For older clients without skill discovery, update or explicitly load SKILL.md according to their documentation. Installation alone does not prove client/session compatibility.

## AWS Knowledge MCP

The default server is the public AWS documentation endpoint. It does not require account credentials. Preserve existing servers; account-resource and diagram MCP servers are not required by this workflow.

Claude:
```json
{"mcpServers":{"aws-knowledge":{"type":"http","url":"https://knowledge-mcp.global.api.aws"}}}
```
Codex:
```toml
[mcp_servers.aws-knowledge]
url = "https://knowledge-mcp.global.api.aws"
```
Kiro:
```json
{"mcpServers":{"aws-knowledge":{"url":"https://knowledge-mcp.global.api.aws","disabled":false,"autoApprove":[]}}}
```

Do not add wildcard auto-approval or administrative AWS credentials. Use a documented proxy only for clients that need stdio; native HTTP needs no extra proxy. Client first-use approval remains the client's policy.

For certificate verification errors, fix the Python trust store or configure an approved CA bundle with SSL_CERT_FILE. A valid macOS system bundle may be available at /etc/ssl/cert.pem. Never disable TLS verification.

## D2 and browser rendering

Use an existing Homebrew installation on macOS where appropriate, or verified official archives for Linux/Windows matching the CPU. Do not install a package manager implicitly. Keep CLI, Python and file paths within the same WSL environment when using WSL. Verify `d2 --version` and a real local SVG render; source generation or SVG file presence alone is insufficient. Do not use public playgrounds for account data.

Install Playwright/Chromium locally only when needed, or reuse an approved existing browser. Pin the version the
renderer was tested with (1.64.1; `assets/dependencies.lock.json` records it) so the package and the browser build it
downloads are the same on every machine:

```bash
npm install playwright@1.64.1
npx playwright@1.64.1 install chromium
node "$SKILL_DIR/scripts/render_artifact.mjs" report.html qa
node "$SKILL_DIR/scripts/render_artifact.mjs" presentation.bento.html qa --bento
```

Node helpers resolve Playwright from the current working directory. `--channel chrome` can reuse installed Chrome. The portable helper reads the local file and serves it from memory at a fixed address on a reserved origin (`moda-render.invalid`), aborting every other request, so the browser never opens a file:// path or network URL and spaces, Unicode and Windows paths need no URL handling. The inspected upstream Bento renderer constructs file URLs directly; use this helper if necessary. Use bundled Ember fonts or disclose the fallback. Preserve visual QA.

## Official references

- https://developers.openai.com/codex/skills/
- https://developers.openai.com/codex/mcp/
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/mcp
- https://kiro.dev/docs/skills/
- https://kiro.dev/docs/mcp/configuration/
- https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server
- https://d2lang.com/tour/install/
- https://d2lang.com/tour/man/
- https://github.com/masangbeom/aws-diagram-design
- https://github.com/aws-samples/sample-apj-sup-sa/tree/main/ai-coding-assistants/skills/aws-bento-deck
