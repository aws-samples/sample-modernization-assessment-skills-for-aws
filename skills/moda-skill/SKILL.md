---
name: moda-skill
description: "Entry point for the AWS Modernization Assessment. Subcommands: prepare, start, status, resume, report, doctor, help, version. This is the unrendered source; run `moda-skill config --harness <claude|codex|kiro>` (or render SKILL.template.md) so the slash-command prefix and skill paths match your client."
---

# moda-skill (unrendered source)

This copy was installed without rendering. The client-specific version comes from `SKILL.template.md` in this folder:

```bash
python3 scripts/moda_skill_cli.py render --harness kiro > SKILL.md     # or claude / codex
```

Until then, treat `/moda-skill <subcommand>` as described in `SKILL.template.md` with `{{INVOKE}}` = `/` (Claude Code, Kiro) or `$` (Codex) and `{{SKILLS_DIR}}` = the directory this skill was installed into.
