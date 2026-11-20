# Running a model on the MPCDF LLM service

The [MPCDF LLM service](https://llm.mpcdf.mpg.de) runs language models on the GPU clusters of the Max Planck Computing and Data Facility. With this plugin, you start such a model with your own MPCDF account and use it in the editor, for example for the LLM Reviewer, the LLamore extractor or the automatic annotation. The model runs as a job at MPCDF for a limited time and is renewed as long as you need it.

The MPCDF terms of use do not allow sharing an account, so every user logs in with their own MPCDF account, and every user's models are visible to that user only.

## Opening the page

Open the plugins menu (plug icon) in the toolbar and choose **My MPCDF endpoints** in the **Inference** section. The page has three parts for every user, numbered in the order you use them.

![The page "My MPCDF endpoints": under "1. MPCDF session" the line "Logged in as alice" with the button Log out; under "2. Start" the preset llama-3.3-70b, "Keep alive until" 06:00 PM, the hint about the missing Hugging Face read token and the progress "submitting", "queued"; under "3. My endpoints" the endpoint of the preset with the status Queued and the button Cancel](images/user-page.png)

*A model was started and waits in the queue at MPCDF.*

## 1. Logging in

Enter your MPCDF user name, password and one-time code and click **Log in**. The editor keeps your login encrypted on the server, so it can renew your models while you are offline, as long as the login is valid. **Log out** ends it. **Remove all keys** in the API Keys dialog (user menu > **API Keys**) removes it as well. When the login has expired, the page asks you to log in again.

## 2. Starting a model

Choose a preset and click **Start**. A preset describes a model and the hardware it runs on; your administrator sets up the presets. Optionally enter a time of day under **Keep alive until**: until then, the model is renewed before its run time ends, without an interruption.

The page shows the progress of the start: queued at MPCDF, building, pulling the model, ready. The first start of a large model can take half an hour or more, because the model has to be downloaded. You can close the page meanwhile; the start goes on.

Some models (gated models on Hugging Face) need a Hugging Face read token. If you have not stored one, the page says so. Add it as **Hugging Face read token** in the API Keys dialog.

## 3. Using and ending a model

When the endpoint is ready, its model appears in the **Tools** menu under **Inference** as **MPCDF: &lt;preset&gt;**, for you only. Choose it there to use it for your session (see [Choosing a language model](https://github.com/mpilhlt/pdf-tei-editor/blob/main/docs/user-manual/language-models.md)). Jobs that run in the background, such as the automatic annotation of a document, use the endpoint of the user who started them.

**My endpoints** lists your endpoints with their preset, status, the time left and the "keep alive" time. **Cancel** ends an endpoint. An endpoint also ends:

- when its run time is over and no "keep alive" time is set or that time has passed,
- when nobody has used it for the idle time of the preset.

When the endpoint ends, the model disappears from the menu within a few seconds. Stopping or restarting the editor does not end the endpoints.
