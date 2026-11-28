# KISSKI Extractor Plugin

Extract structured JSON data from PDFs and text using the KISSKI Academic Cloud LLM API.

## Features

- **PDF extraction**: Convert PDF pages to images and extract structured data using multimodal LLMs
- **Text extraction**: Extract structured data from plain text input
- **JSON schema validation**: Optionally enforce output schema with automatic retry on validation failure
- **Model capabilities**: Query available models and their input/output modalities

## Requirements

### User manual

The page [KISSKI Academic Cloud](src/pte_plugin_kisski/docs/kisski.md) in the section "Choosing a language model" of the editor's user manual (`manual` in `pte-plugin.json`) tells users where to get a key and how to enter it.

## Configuration

| Config key | Environment variable | Default |
| --- | --- | --- |
| `plugin.kisski.api.key` | `KISSKI_API_KEY` | none |
| `plugin.kisski.api.url` | `KISSKI_API_URL` | `https://chat-ai.academiccloud.de/v1` |

The environment variables set the initial value when the key is not yet in `config.json`.

### API Key

Models whose `input` in KISSKI's model list includes `image` are marked as vision models in the inference model picker (eye icon) and get the page images in the editor's review of OCR errors.

For KISSKI models in the inference model picker, each user enters their own key in the web interface (user menu > API Keys); it is stored encrypted on the server (see the PDF-TEI Editor documentation on user API keys). Optionally, an administrator can set a shared key in `plugin.kisski.api.key`, which is then used for all users instead.

The structured-data extractor (`/api/plugins/kisski/extract`) uses only the shared key and is unavailable without it.

Obtain a key from the [KISSKI LLM Service](https://saia.gwdg.de/dashboard) dashboard.

### PDF Support (Optional)

For PDF extraction, install poppler:

```bash
# macOS
brew install poppler pdf2image

# Linux (Debian/Ubuntu)
apt-get install poppler-utils pdf2image
```

The plugin works without poppler (e.g. on Windows) but PDF extraction will be disabled.

## API Endpoints

### POST `/api/plugins/kisski/extract`

Extract structured JSON data from a PDF or text input.

**Request body:**

```json
{
  "model": "gemma-3-27b-it",
  "prompt": "Extract the article title and authors as JSON",
  "stable_id": "abc123",
  "json_schema": {
    "type": "object",
    "properties": {
      "title": { "type": "string" },
      "authors": { "type": "array", "items": { "type": "string" } }
    },
    "required": ["title", "authors"]
  },
  "temperature": 0.1,
  "max_retries": 2
}
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | string | Yes | Model ID (use `/models` endpoint to list available models) |
| `prompt` | string | Yes | Extraction instructions |
| `stable_id` | string | No* | PDF file stable_id (from upload) |
| `text_input` | string | No* | Plain text to extract from |
| `json_schema` | object | No | JSON schema for output validation |
| `temperature` | float | No | LLM temperature (default: 0.1) |
| `max_retries` | int | No | Max retries for JSON/schema correction (default: 2) |

*Either `stable_id` or `text_input` must be provided.

**Response:**

```json
{
  "success": true,
  "data": {
    "title": "Example Article",
    "authors": ["John Doe", "Jane Smith"]
  },
  "model": "gemma-3-27b-it",
  "extractor": "kisski-neural-chat",
  "retries": 0
}
```

### GET `/api/plugins/kisski/models`

List available KISSKI models with their capabilities.

**Response:**

```json
{
  "models": [
    {
      "id": "gemma-3-27b-it",
      "name": "Gemma 3 27B IT",
      "input": ["text", "image"],
      "output": ["text"]
    }
  ],
  "pdf_support": true
}
```

## Multimodal Models

For PDF extraction, use a model that supports image input. Current multimodal models include:

- `gemma-3-27b-it`
- `internvl3.5-30b-a3b`
- `qwen3-vl-30b-a3b-instruct`
- `qwen2.5-vl-72b-instruct`
- `medgemma-27b-it`

Check the `/models` endpoint for the current list.

## Usage Examples

### Extract article metadata from PDF

```javascript
const response = await fetch('/api/plugins/kisski/extract', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-Session-Id': sessionId
  },
  body: JSON.stringify({
    model: 'gemma-3-27b-it',
    prompt: `Extract article metadata: title, authors (with affiliations),
             journal, volume, issue, pages, year, and abstract.`,
    stable_id: uploadedPdfStableId,
    json_schema: {
      type: 'object',
      properties: {
        title: { type: 'string' },
        authors: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              affiliation: { type: 'string' }
            }
          }
        },
        journal: { type: 'string' },
        year: { type: 'string' }
      },
      required: ['title', 'authors']
    }
  })
});

const result = await response.json();
if (result.success) {
  console.log('Extracted:', result.data);
}
```

### Extract from text input

```javascript
const response = await fetch('/api/plugins/kisski/extract', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-Session-Id': sessionId
  },
  body: JSON.stringify({
    model: 'llama-3.3-70b-instruct',
    prompt: 'Extract all person names and their roles',
    text_input: 'Dr. Jane Smith, lead researcher, worked with John Doe, research assistant.',
    json_schema: {
      type: 'object',
      properties: {
        people: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              role: { type: 'string' }
            }
          }
        }
      }
    }
  })
});
```

## Running Tests

```bash
node tests/backend-test-runner.js \
  --test-dir ../pte-plugins/plugins/kisski/src/pte_plugin_kisski/tests/integration \
  --env-file ../pte-plugins/plugins/kisski/src/pte_plugin_kisski/tests/integration/.env.test
```

The test uploads a sample PDF and verifies metadata extraction.

## Architecture

```
plugins/kisski/src/pte_plugin_kisski/
├── __init__.py          # Plugin exports
├── plugin.py            # Plugin registration
├── routes.py            # API endpoints
├── extractor.py         # LLM extraction logic
├── cache.py             # PDF image extraction utilities
├── models-and-api.md    # API documentation
└── tests/
    ├── .env.test        # Test environment
    └── test_extract.test.js  # Integration tests
```

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| `PDF support not available` | poppler not installed | Install poppler |
| `Model does not support image input` | Using text-only model for PDF | Use a multimodal model |
| `API key not available` | No shared key configured | Set `plugin.kisski.api.key` (or `KISSKI_API_KEY`) |
| `Schema validation failed` | Output doesn't match schema | Automatic retry, then returns error with raw response |
