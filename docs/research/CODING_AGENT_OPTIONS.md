# Coding-Agent Options — Snapshot 17 October 2026

Re-open official sources before each major phase; provider details and entitlements change.

| Tool | Available path | Best role | Limits and precautions |
|---|---|---|---|
| Codex IDE extension with ChatGPT Go | Included on ChatGPT Go; limits vary | Default implementer | No fixed lines/tasks estimate. Codex Cloud is not included with Go. https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan |
| Google Antigravity VS Code extension | Sign in with the Google account and check the plan/model/usage panel | Independent read-only reviewer; bounded fallback implementer | Entitlements differ between Google AI Plus, Pro and other tiers. Never assume a fixed quota or model access. https://www.antigravity.google/docs/ide/extensions/ and https://www.antigravity.google/docs/plans |
| GitHub Copilot Free | Optional free completion/chat access where offered | Optional autocomplete / minor assistance | Limits can change; not required for the main build. https://github.com/features/copilot/plans |
| Claude Code | Not included in our mandatory zero-additional-cost workflow | Optional manual review only if already available to the user | Do not assume the Claude web free tier includes Claude Code. https://docs.anthropic.com/en/docs/claude-code/getting-started |
| OpenHands SDK | Open-source coding-agent SDK/workspace architecture | Reference or later bounded experiment | Local service/container can add memory and CPU overhead. https://github.com/OpenHands/software-agent-sdk |

## Quota-efficient operating model
- Codex implements one bounded task; Antigravity reviews it read-only at milestones.
- If Codex reaches its limit, checkpoint and check Git state first; then Antigravity may take one bounded implementation task if its included usage is available. Do not have both edit the same change.
- Check each product's usage panel before large tasks. Do not translate request limits to number of completed coding tasks.
- One prompt = one acceptance-tested change.
- Use local tests and Git diff checks to avoid repeated agent calls.
- No account rotation, purchased credits, billing-enabled keys, or automatic paid fallback.
