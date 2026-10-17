# Initial Threat Model

## Assets
Private files, source code, account credentials, model credentials (if later added), conversations, memory, task artifacts, contact information, phone notifications and permission settings.

## Threats
- Prompt injection in websites, documents, email and tool output.
- Accidental deletion/modification by generated commands.
- Credential leakage into Git, logs, screenshots or agent prompts.
- Unauthorized message/email sending.
- Overbroad Windows/mobile permissions.
- Unexpected model API/cloud charges.
- False claims that a task completed.
- Duplicate external actions after retry/restart.
- Background work/recording without consent.

## Initial controls
- Private GitHub repository; `.env` and local data ignored.
- No secrets in code or coding-agent prompts.
- Bind development server to `127.0.0.1`, not `0.0.0.0`.
- Typed tool allowlists; no arbitrary shell by default.
- Policy is separate from model reasoning.
- Approval for high-impact/irreversible operations.
- Timeouts, bounded retries, idempotency where possible, and audit events.
- External content is untrusted.
- No paid fallbacks; no billing-enabled API keys for initial build.
- Stop/cancel controls and explicit device capability status.
- Verify actual Android/telephony support before claiming call handling.

Update this document whenever an integration or permission is introduced.
