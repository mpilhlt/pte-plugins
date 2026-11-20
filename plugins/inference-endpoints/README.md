# Inference Endpoints Plugin

Lets administrators add and remove any number of OpenAI-compatible inference endpoints, such as [Ollama](https://ollama.com), vLLM, LM Studio, the llama.cpp server, LiteLLM or OpenRouter. Each endpoint is registered as a provider in the core LLM provider registry (`fastapi_app/lib/llm/`), so its models can be selected wherever the registry is used (for example Tools > Default Model, the [Annotation Review](../annotation-review/README.md) plugin and the [LLamore extractor](../llamore-extractor/README.md)).

## User manual

How administrators add, test and delete endpoints, and which base URL to use for Ollama on their own computer, is described in the manual page [Adding inference endpoints](src/pte_plugin_inference_endpoints/docs/inference-endpoints.md). The plugin adds it to the editor's user manual for administrators, in the section on language models (`manual` in `pte-plugin.json`, slot `inference`).

The management page is opened from the plugins menu (Inference > Inference endpoints). The fields of an endpoint are name, base URL, key mode (`none`, `shared`, `user`), model ids, vision, free and timeout; an endpoint cannot be edited, only deleted and added again.

## Security

Endpoint URLs make the server send requests, so the management page and its API are restricted to administrators. URLs must be `http` or `https` without credentials, query or fragment. Local and private addresses are allowed (that is what Ollama on `localhost` or in a VPN needs), but link-local addresses (`169.254.0.0/16`, `fe80::/10`, including cloud metadata services), multicast and unspecified addresses are always rejected. The URL is checked when an endpoint is saved and before each request, and redirects are not followed.

API keys are stored encrypted in the editor's secret store (the shared key under a reserved pseudo user, per-user keys under each user) and are never returned by the API.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `INFERENCE_ENDPOINTS_ALLOWED_HOSTS` | none (all hosts allowed, except the addresses above) | Comma-separated host names (`ollama.example.org`, `*.ts.net`) and networks in CIDR notation (`10.0.0.0/8`, `127.0.0.1/32`). If set, endpoint URLs must match one of them; a host name matches by name, an IP network matches if all addresses of the host are in it. Read from the environment only, so that administrators cannot widen it in the application's configuration |

The endpoints are stored in `data/db/inference-endpoints.json`. Every server worker watches this file and updates its providers within a few seconds of a change.

## Provider

- Provider ID: `endpoint-<id>`, where `<id>` is derived from the name
- Requests: `POST {base_url}/chat/completions` (non-streaming), `GET {base_url}/models`, with `Authorization: Bearer <key>` if there is a key
- Image input if "Vision" is set; PDF input is not supported (consumers send the PDF's text layer instead)

## Implementation

- **Plugin file**: [plugin.py](src/pte_plugin_inference_endpoints/plugin.py) (provider registration, file watcher)
- **Provider**: [llm_provider.py](src/pte_plugin_inference_endpoints/llm_provider.py)
- **Storage**: [store.py](src/pte_plugin_inference_endpoints/store.py), [keys.py](src/pte_plugin_inference_endpoints/keys.py)
- **URL checks**: [url_policy.py](src/pte_plugin_inference_endpoints/url_policy.py)
- **Routes and page**: [routes.py](src/pte_plugin_inference_endpoints/routes.py), [static/manage.html](src/pte_plugin_inference_endpoints/static/manage.html)
- **Tests**: [tests/](src/pte_plugin_inference_endpoints/tests/)

## Technical Details

- Category: `inference`
- Menu entry and API: admin role. The registered models follow the editor's model access rules
