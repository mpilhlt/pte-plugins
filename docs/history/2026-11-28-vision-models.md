# Vision models in the LLM providers

mpilhlt/pdf-tei-editor#960: the review of OCR errors sends the page images to models that accept images. The core derives `supports_image_input()` from the `vision` capability that `list_models()` reports; the providers now report it.

| Plugin | Vision models | Images sent by |
| --- | --- | --- |
| kisski | `input` of the KISSKI `/models` entry includes `image` | core `OpenAICompatibleProvider.chat_completion_with_images()` (`image_url` parts) |
| anthropic-llm | all models | existing `chat_completion_with_images()` |
| google-llm | all listed Gemini chat models | new `chat_completion_with_images()` (inline `Part.from_bytes`) |
| mpcdf-llm | presets named in `plugin.mpcdf-llm.vision-presets` (presets and the MPCDF service carry no modalities) | new `chat_completion_with_images()` (`image_url` parts) |
| inference-endpoints | endpoints with the vision flag (unchanged) | existing |

With a core older than #960, KISSKI's `supports_image_input()` stays False, since the base class default then does not read the capabilities.
