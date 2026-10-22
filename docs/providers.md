# Providers and models

| Kind | Endpoint | Notes |
|---|---|---|
| `openrouter` | `https://openrouter.ai/api/v1` | Model list includes pricing, which drives free/paid detection. |
| `openai` / OpenAI-compatible | your `base_url` | Works with OpenAI, local servers (Ollama, LM Studio, vLLM) and most gateways that implement `/chat/completions` and `/models`. |
| `anthropic` | `https://api.anthropic.com` | Native Messages API with tool use. |
| `custom` | your `base_url` | Same protocol as OpenAI-compatible; a key is optional. |

## Models are discovered, not hardcoded

`@models` calls the provider's model-list endpoint. If a provider has none, `@changemodel provider:model-id` accepts an id you type and validates it with a real request. Nothing in the code base maps product names to model ids.

## Switching models

`@changemodel` runs this procedure: save task state, stop the active request safely, reconcile interrupted tool operations, write a handoff, validate the new model (including a tool-calling probe), update configuration, inject a restore notice into the conversation, and resume if a task was running. If any step fails the old model stays configured and tasks are paused, not lost.

## Fallbacks and limits

`@fallback` manages an ordered chain. Errors are classified (rate limit on a model, account-wide limit, context overflow, auth, invalid request, transient). Transient errors back off exponentially with jitter and honour `Retry-After` (capped at 300 s). Account-wide limits skip the provider. Context overflow compacts once and retries.

When every model is exhausted the task is paused with its state saved; `@retry` or `@resume` continues later. WorkFlaskCode never rotates accounts, never evades limits, and never moves to a known-paid fallback unless you enabled `allow_paid` for that provider. `@usage` and `@limits` show real recorded usage and any rate-limit headers the provider returned; unknown costs are shown as unknown.
