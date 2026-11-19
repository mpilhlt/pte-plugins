/**
 * @file Enhancement: Insert page breaks into a GROBID segmentation document
 *
 * Sends the document to the GROBID plugin, which inserts `<pb n="…"/>` before the first word of
 * every PDF page, from the cached feature file of the document's extraction. Page starts that
 * already have a `<pb/>` nearby are left alone, so the enhancement can run again.
 */

/**
 * Human-readable name for the enhancement
 */
export const name = "Insert page breaks";

/**
 * Description shown in the UI
 */
export const description = "Marks the start of every PDF page with <pb n=\"…\"/>, from the GROBID feature file of the document's extraction. Pages that already have a <pb/> nearby are left alone.";

/**
 * Only segmentation documents have a page layout
 */
export const variants = ["grobid.training.segmentation"];

/**
 * Inserts the page breaks on the server and returns the resulting document.
 *
 * @param {Document} xmlDoc - The XML DOM Document object
 * @param {{xml: string|null, sessionId: string|null}} currentState - The current application state
 * @returns {Promise<Document>} - The document with page breaks
 */
export async function execute(xmlDoc, currentState) {
  if (!(xmlDoc instanceof Document)) {
    throw new Error(`Invalid parameter: Expected document, got ${xmlDoc}`);
  }
  if (!currentState.xml) {
    throw new Error("No XML document is currently open");
  }
  const xml = new XMLSerializer().serializeToString(xmlDoc);
  const response = await fetch(`/api/plugins/grobid/insert-page-breaks`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Session-ID": currentState.sessionId ?? "" },
    body: JSON.stringify({ stable_id: currentState.xml, xml })
  });
  if (!response.ok) {
    throw new Error(`Inserting page breaks failed (${response.status}): ${await response.text()}`);
  }
  const result = await response.json();
  if (result.status !== "ok") {
    throw new Error(result.message);
  }
  if (result.inserted === 0) {
    throw new Error("Every page already has a page break.");
  }
  const doc = new DOMParser().parseFromString(result.xml, "application/xml");
  if (doc.getElementsByTagName("parsererror").length > 0) {
    throw new Error("The document with page breaks could not be parsed");
  }
  return doc;
}
