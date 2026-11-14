# Sources and scope

Initial review date: 2026-10-16. Recheck changing installation methods, options, product constraints, regional availability and pricing when executing a skill.

| Source | Reviewed version | Use |
|---|---|---|
| masangbeom/aws-diagram-design | 9e71e40ed1480dd0086a937ceebb32e9723ea58d | AWS icons/branding, standalone diagrams, geometry/self-check/export |
| aws-samples/sample-apj-sup-sa, ai-coding-assistants/skills/aws-bento-deck | 53c3791967bb1f6da222a0e1652e14b1e3f4f0c7 | Optional Bento plan/build/QA |
| aws-samples/sample-ai-driven-modernization-with-kiro | 1c3dd302aaf753ad2f39e239398245ee0d0a26eb | CLI evidence and infrastructure/application/database separation |
| aws-samples/service-screener-v2 | 055c882bbda3dcea831ed81ee41f371030e25b11 | Collector/rule/framework/reporting approach; independent starter implementation |
| d2lang/d2 | v0.9.0 in validation | Initial structural render and exit-code verification |

Companions are fetched at pinned commits with their licenses/notices rather than vendored into distribution source. Distinguish local assessments from upstream Service Screener results.

## Official installation and compatibility

- https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html
- https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html
- https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-pagination.html
- https://developers.openai.com/codex/skills/
- https://developers.openai.com/codex/mcp/
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/mcp
- https://kiro.dev/docs/skills/
- https://kiro.dev/docs/mcp/configuration/
- https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server
- https://d2lang.com/tour/man/

## Assessment and later extensions

The public Well-Architected Framework and per-check source_urls in review.py support assessment. [Pathway guidance](../skills/aws-modernization-pathway/references/pathways.md) supplies modernization sources.

On 2026-10-20, AWS Knowledge MCP/public documentation informed [Modern DevOps](../skills/aws-modernization-pathway/references/modern-devops.md), the [12-option AI catalog](../skills/aws-modernization-pathway/assets/ai-options.json) and [AI fit](../skills/aws-modernization-pathway/references/genai-ai-fit.md).

[Platform options](../skills/aws-modernization-pathway/assets/platform-options.json), [observability](../skills/aws-modernization-pathway/references/observability.md) and [messaging](../skills/aws-modernization-pathway/references/message-event-driven.md) use public AWS/OTel documentation and relevant CLI contracts, reviewed 2026-10-20.

[Host inspection](../skills/aws-as-is-analysis/references/host-inspection.md) references Run Command, GetCommandInvocation, SSM execution privileges and OpenSSH configuration. Existing host access and trusted keys must be verified in the actual environment.

This bundle does not implement every service/control or replace a formal WA review, security certification or compliance determination.
