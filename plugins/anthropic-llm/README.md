# Anthropic Claude LLM Plugin

Registers Anthropic's Claude API as a provider in the core LLM provider registry (`fastapi_app/lib/llm/`), so Claude models can be selected wherever the registry is used (for example Tools > Default Model, the [Annotation Review](../annotation_review/README.md) plugin and the [LLamore extractor](../llamore_extractor/README.md)).

The plugin has no menu entries or routes of its own.

## User manual

The page [Anthropic Claude](src/pte_plugin_anthropic_llm/docs/anthropic-claude.md) in the section "Choosing a language model" of the editor's user manual (`manual` in `pte-plugin.json`) tells users where to get a key and how to enter it.

## Configuration

| Config key | Environment variable | Default |
| --- | --- | --- |
| `plugin.anthropic-llm.api.key` | `ANTHROPIC_API_KEY` | none |
| `plugin.anthropic-llm.api.url` | `ANTHROPIC_API_URL` | `https://api.anthropic.com/v1` |

API keys: Each user enters their own key in the web interface (user menu > API Keys); it is stored encrypted on the server (see the PDF-TEI Editor documentation on user API keys). Optionally, an administrator can set a shared key in `plugin.anthropic-llm.api.key`, which is then used for all users instead. The provider is not registered when `FASTAPI_APPLICATION_MODE=testing`.

## Provider

- Provider ID: `anthropic`
- Models are listed live from the API's `/models` endpoint
- Supports PDF input: `chat_completion_with_pdf()` sends the PDF as a base64 `document` content block
- Retries a request without `temperature` when a model rejects that parameter

## Implementation

- **Plugin file**: [plugin.py](src/pte_plugin_anthropic_llm/plugin.py)
- **Provider**: [llm_provider.py](src/pte_plugin_anthropic_llm/llm_provider.py)
- **Tests**: [tests/](src/pte_plugin_anthropic_llm/tests/)

## Technical Details

- Category: `inference`
- Requires: User role or higher
