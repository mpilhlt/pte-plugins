# Setting up MPCDF presets

Users start models on the MPCDF LLM service from presets that administrators set up (see [Running a model on the MPCDF LLM service](mpcdf-endpoints.md)). The page **My MPCDF endpoints** (plugins menu, **Inference** section) shows administrators two more parts: **4. Presets** and **5. All users' endpoints**.

![The parts "4. Presets", a table of three presets with their model and hardware and the buttons Edit, Duplicate and Delete, followed by the buttons New preset, Export and Import; and "5. All users' endpoints", one user with a stored session, an endpoint that is starting and the button Remove session](images/admin-page.png)

*The presets and the endpoints of all users.*

## Presets

A preset describes one endpoint: the framework (vLLM or Ollama), the model, the machine, the GPU type and number, the run time and queue time, extra arguments and environment variables for the framework, and the rules below. All users start from the same presets, and only administrators change them.

The MPCDF service does not say whether a model accepts images. For a vision model, check **Accepts images** in the preset form: the model then shows an eye icon in the Inference menu and gets the page images in the review of OCR errors.

**New preset** opens the form for a new preset, **Edit** next to a preset opens it with that preset, and **Duplicate** with a copy under a free name such as `<name>-copy`. **Delete** removes a preset. **Save preset** checks the preset against the options the service offers at the moment, so it needs your own MPCDF login in part 1.

| Rule | Meaning |
| --- | --- |
| Time | The run time of a job at MPCDF. The endpoint stops when it is over, unless it is renewed |
| Min remaining (min) | While the user's "keep alive" time is not reached, a replacement is started when less than this time is left, and the model switches over without interruption |
| Idle timeout (min) | An endpoint nobody uses for this time is cancelled. Using the model counts as activity |
| Build timeout (min) | An endpoint that does not serve its model within this time after its job starts running is cancelled, and the start fails. Empty: 30 minutes. Large models need more on their first start, when their files are not yet cached at MPCDF (a model of 155 GiB took about 45 minutes) |

## Exporting and importing presets

**Export** downloads all presets as a JSON file. **Import** reads such a file. It does not need an MPCDF login, so it checks only the fields of the presets, not whether the service offers their machine or GPU type; a preset the service does not support fails when it is started. The page lists the presets it could not import, with the reason.

If presets exist already, the import asks how to combine them, by name:

- **Merge** adds only the presets with a new name and leaves the existing ones unchanged.
- **Replace** saves all imported presets, overwriting those with the same name, and deletes the existing presets that are not in the file. If a preset cannot be imported, nothing is deleted.

## The endpoints of all users

**All users' endpoints** lists every user with a stored MPCDF login and the endpoints the editor manages for them: preset, status, "keep alive" time and last activity. Administrators cannot cancel another user's endpoints, since that would act through that user's MPCDF account.

**Remove session** deletes a user's stored login. The editor then no longer renews or uses that user's endpoints, which end at MPCDF when their time is over or they are idle. The user has to log in again to start a model.
