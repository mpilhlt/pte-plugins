# Google Gemini LLM Plugin

Registers Google's Gemini API as a provider in the core LLM provider registry (`fastapi_app/lib/llm/`), so Gemini models can be selected wherever the registry is used (for example Tools > Default Model, the [Annotation Review](../annotation_review/README.md) plugin and the [LLamore extractor](../llamore_extractor/README.md)).

The plugin has no menu entries or routes of its own.

## Configuration

| Config key | Environment variable | Default |
| --- | --- | --- |
| `plugin.google-llm.api.key` | `GEMINI_API_KEY` | none |

API keys: Each user enters their own key in the web interface (user menu > API Keys); it is stored encrypted on the server (see the PDF-TEI Editor documentation on user API keys). Optionally, an administrator can set a shared key in `plugin.google-llm.api.key`, which is then used for all users instead. Keys are obtained from Google AI Studio. The provider is not registered when `FASTAPI_APPLICATION_MODE=testing`.

## Provider

- Provider ID: `google`
- Uses the `google-genai` SDK
- Lists `gemini-*` models that support `generateContent`, excluding image, TTS, transcription and computer-use models
- Supports PDF input: `chat_completion_with_pdf()` uploads the PDF via the Files API, prompts with it and deletes it afterwards
- Sends `temperature` only to models before Gemini 3; Flash-Lite models of Gemini 3+ use a medium thinking level
- Rate limits and exhausted quotas: a 429 raises `LLMRateLimitError`, and the key is remembered as limited for that model in the editor's rate-limit tracker (in memory, per process), so that further calls fail at once without calling the API. The key is limited for the `retryDelay` of the error, until midnight Pacific time when a daily quota is exhausted (a quota id with `PerDay`), and otherwise for the editor's default period (60 s). The SDK does not retry 429 itself, since no `HttpRetryOptions` are set

## Implementation

- **Plugin file**: [plugin.py](src/pte_plugin_google_llm/plugin.py)
- **Provider**: [llm_provider.py](src/pte_plugin_google_llm/llm_provider.py)
- **Tests**: [tests/](src/pte_plugin_google_llm/tests/)

## Technical Details

- Category: `inference`
- Requires: User role or higher
