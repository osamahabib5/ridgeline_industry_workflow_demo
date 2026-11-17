# Conversation history

At the start of each Copilot session in this workspace, read `.chat-history/log.md` for context. If the file or its parent directory does not exist, create them.

Before completing every response to a user prompt in this workspace, append exactly one entry to `.chat-history/log.md` in this format:

---
- timestamp: "<ISO 8601 timestamp, or an estimate based on conversation order if unavailable>"
- user_prompt: "<the user's original prompt>"
- assistant_response_summary: "<concise, specific summary of the response>"
- files_affected: "<comma-separated list of files explicitly created or modified for this response, or none>"

Preserve all existing entries; never overwrite or delete history. Do not ask for confirmation. Include only files actually created or modified for that response in `files_affected`. Keep the summary concise and specific, mentioning relevant function names, endpoints, or key decisions when applicable. Before writing, redact API keys, tokens, passwords, and `.env` values by replacing each sensitive value with `***` in every field, including `user_prompt`. Never copy secret values into the history.
