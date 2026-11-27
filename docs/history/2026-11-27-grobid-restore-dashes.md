# GROBID: restore the PDF's dashes in segmentation documents

GROBID's TEI has `-` where the PDF has `—`, `–` or `−` (`UnicodeUtil.normaliseText()` folds Unicode class Pd and U+2212), and no space after a token ending with a dash (`PDFALTOSaxHandler` keeps line-end hyphens attached; newer GROBID restores the space only for SPs wider than 0.05 of the font size). On OCR documents, "— II." became "-II." and "rechts- und" became "rechts-und", and the editor's "Write text corrections to PDF" listed these as unwritable differences (mpilhlt/pdf-tei-editor#951 makes it ignore them).

## Change

- `dashes.py`: `restore_dashes(xml, pdf_text)` looks up every `-` in `<text>` (outside markup, entities decoded) in the whitespace-free PDF text by 12 characters of context on each side (24 if ambiguous); if the context occurs once, the PDF's dash is taken, and a space is added if the PDF has a space (not a line break) after it and the TEI has none and no markup follows. `pdf_text()` reads the text of all pages with pypdfium2 (core dependency).
- The extractor calls it for `grobid.training.segmentation` after inserting the page breaks; a failure leaves the document unchanged.
- Comparisons with the feature file fold dashes: `layout.first_token()` (page layout, page breaks, corrected-PDF handler) and the tokens of the frontend sync check (`foldDashes()` in `grobid-sync.js`).

GROBID's `SegmentationTrainer` normalizes the TEI tokens with `normaliseTextAndRemoveSpaces()` before matching them with the feature file, so training is unaffected. Other training variants are not changed, since it is not verified that their trainers normalize in the same way. Existing documents are not changed.
