# Review document annotations menu item

The Tools menu item of the annotation-review plugin, formerly "Review Annotations", reviewed only the node selected by node navigation when the `xpath` state was set, which duplicated the "LLM Review" button of focus mode.

Change (Christian, 2026-11-10):

- The menu item is renamed to "Review document annotations" and always reviews the whole document with the model selected in Tools > Inference (`runReview(undefined, { wholeDocument: true })`).
- The focus-mode "LLM Review" button keeps reviewing only the focused node (`runReview()` still follows the `xpath` state).
- The user manual pages in pdf-tei-editor that name the menu item were updated in a separate PR.
