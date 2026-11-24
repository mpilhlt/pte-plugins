/**
 * Upload form of the GROBID training data import (#68).
 *
 * Requests to /api/plugins/ are authenticated by the plugin session cookie, which the server
 * sets when the application opens this page.
 */

/**
 * @typedef {object} ImportReport
 * @property {number} documents - Documents with at least one imported annotation
 * @property {number} files_imported - PDFs and annotations stored
 * @property {{name: string, reason: string}[]} skipped - Skipped documents with reasons
 * @property {string[]} warnings - Warnings
 */

const form = /** @type {HTMLFormElement} */ (document.getElementById('importForm'))
const statusEl = /** @type {HTMLDivElement} */ (document.getElementById('status'))
const submitBtn = /** @type {HTMLButtonElement} */ (document.getElementById('submitBtn'))

/**
 * Show a status message.
 * @param {string} type - "info", "success" or "error"
 * @param {string} message - Text of the message
 * @param {string[]} [items] - Lines listed below the message
 */
function setStatus (type, message, items = []) {
  statusEl.className = 'status ' + type
  statusEl.textContent = message
  if (items.length) {
    const list = document.createElement('ul')
    for (const item of items) {
      const li = document.createElement('li')
      li.textContent = item
      list.append(li)
    }
    statusEl.append(list)
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault()
  const formData = new FormData(form)
  submitBtn.disabled = true
  setStatus('info', 'Importing…')
  try {
    const response = await fetch('/api/plugins/grobid/import-training', { method: 'POST', body: formData })
    const result = await response.json()
    if (!response.ok) {
      setStatus('error', result.detail || `Import failed (HTTP ${response.status})`)
      return
    }
    const report = /** @type {ImportReport} */ (result)
    const lines = [
      ...report.skipped.map(s => `Skipped ${s.name}: ${s.reason}`),
      ...report.warnings
    ]
    setStatus(report.documents ? 'success' : 'error',
      `Imported ${report.documents} documents (${report.files_imported} files), skipped ${report.skipped.length}.`, lines)
  } catch (error) {
    setStatus('error', 'Import failed: ' + String(error))
  } finally {
    submitBtn.disabled = false
  }
})
