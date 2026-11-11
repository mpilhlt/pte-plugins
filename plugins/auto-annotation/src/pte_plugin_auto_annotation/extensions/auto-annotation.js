/**
 * Auto-annotation frontend extension.
 *
 * Adds "Correct annotations in new version" to the Tools menu (annotation category): the model
 * selected in Tools > Inference corrects the review units of the open document against the
 * document's machine rules, and the result is saved as a new version of it. The work is done one
 * batch of units per request behind a cancellable progress widget. Meanwhile the document is
 * locked for the job on the server, so it opens read-only everywhere. When the new version is
 * saved, it is loaded if the source document is still open; otherwise the user is asked whether to
 * load it. Routes: correction_routes.py.
 *
 * @import { PluginContext } from '../../../../app/src/modules/plugin-context.js'
 */

/** Seconds between refreshes of the job's lock while a batch is under way (locks expire after 90 s). */
const LOCK_REFRESH_SECONDS = 30;

/** Roles that may create versions. */
const ALLOWED_ROLES = ['annotator', 'reviewer', 'admin'];

/** Statuses of units whose answer failed the checks (see checks.py). */
const INVALID_STATUSES = new Set(['missing', 'malformed', 'wrong_root', 'text_changed']);

/**
 * @typedef {{index: number, status: string, xml: string|null}} UnitResult
 * @typedef {{units: UnitResult[], error: string|null, calls: number, retries: number, seconds: number}} BatchResult
 * @typedef {{job_id: string, doc_id: string, variant: string|null, unit_count: number, batches: number[][]}} StartResult
 */

/**
 * The outcome of a correction run.
 * @typedef {object} CorrectionOutcome
 * @property {Map<number, string>} corrections - Corrected unit XML by unit index
 * @property {number} processed - Units sent to the model
 * @property {number} invalid - Units whose answer failed the checks
 * @property {number} errors - Units of failed requests
 * @property {number} seconds - Duration of the run
 * @property {'done'|'cancelled'|'failed'} state
 * @property {string|null} error - Why the run failed
 */

/**
 * Send the batches to the server one after the other and collect the corrections. Stops before the
 * next batch if `isCancelled()` returns true, and at the first batch whose request failed.
 * @param {(units: number[]) => Promise<BatchResult>} correctBatch - Corrects the units of one batch
 * @param {number[][]} batches - Unit indices of each batch
 * @param {{isCancelled?: () => boolean, onProgress?: (processed: number, total: number, changed: number) => void, now?: () => number}} [hooks]
 * @returns {Promise<CorrectionOutcome>}
 */
export const runCorrection = async (correctBatch, batches, hooks = {}) => {
  const now = hooks.now ?? (() => Date.now());
  const started = now();
  const total = batches.reduce((sum, batch) => sum + batch.length, 0);
  /** @type {CorrectionOutcome} */
  const outcome = { corrections: new Map(), processed: 0, invalid: 0, errors: 0, seconds: 0, state: 'done', error: null };
  for (const batch of batches) {
    if (hooks.isCancelled?.()) {
      outcome.state = 'cancelled';
      break;
    }
    let result;
    try {
      result = await correctBatch(batch);
    } catch (err) {
      outcome.state = 'failed';
      outcome.error = err instanceof Error ? err.message : String(err);
      break;
    }
    for (const unit of result.units) {
      if (unit.xml !== null) outcome.corrections.set(unit.index, unit.xml);
      if (INVALID_STATUSES.has(unit.status)) outcome.invalid++;
      if (unit.status === 'error') outcome.errors++;
    }
    outcome.processed += batch.length;
    hooks.onProgress?.(outcome.processed, total, outcome.corrections.size);
    if (result.error) {
      outcome.state = 'failed';
      outcome.error = result.error;
      break;
    }
  }
  outcome.seconds = Math.round((now() - started) / 100) / 10;
  return outcome;
};

/**
 * Escape text for use in a dialog's HTML message.
 * @param {string} text
 * @returns {string}
 */
function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export default class AutoAnnotationExtension extends FrontendExtensionPlugin {
  /**
   * @param {PluginContext} context
   */
  constructor(context) {
    super(context, { name: 'auto-annotation', deps: ['xmleditor', 'tools'] });
    /** @type {HTMLElement|undefined} The Tools-menu item, created in start(). */
    this._menuItem = undefined;
    /** @type {Set<string>} Stable ids of the documents being corrected in this tab. */
    this._running = new Set();
    /** @type {number} */
    this._lockRefreshMs = LOCK_REFRESH_SECONDS * 1000;
  }

  /**
   * Add the Tools-menu item.
   * @returns {Promise<void>}
   */
  async start() {
    const item = document.createElement('sl-menu-item');
    item.textContent = 'Correct annotations in new version';
    item.disabled = true;
    item.addEventListener('click', () => this.runCorrection());
    this._menuItem = item;
    this.getDependency('tools').addMenuItems([item], 'annotation');
    this.getDependency('xmleditor').on('editorReady', () => this._updateMenuState());
    this._updateMenuState();
  }

  /**
   * @param {string} [_doc]
   * @returns {Promise<void>}
   */
  async onXmlChange(_doc) {
    this._updateMenuState();
  }

  /**
   * @returns {Promise<void>}
   */
  async onUserChange() {
    this._updateMenuState();
  }

  /**
   * Why the open document cannot be corrected now, or '' if it can.
   * @returns {string}
   */
  _disabledReason() {
    const user = this.state?.user;
    if (!user || !this.getDependency('acl-utils').userHasRole(user, ALLOWED_ROLES)) {
      return 'Requires the annotator or reviewer role.';
    }
    const xml = this.state?.xml;
    const tree = this.getDependency('xmleditor').getXmlTree();
    if (!xml || !tree) return 'Requires an open document.';
    if (this._running.has(xml)) return 'The annotations of this document are being corrected.';
    const guides = this.getDependency('tei-utils').getEditorialDeclGuides(tree);
    if (!guides.some(g => g.refs.some(r => r.subtype === 'machine'))) {
      return 'Requires a document whose editorialDecl has machine-readable annotation rules (a ref with subtype="machine").';
    }
    return '';
  }

  /**
   * Enable the menu item only if the open document can be corrected; the tooltip says why not.
   * @returns {void}
   */
  _updateMenuState() {
    if (!this._menuItem) return;
    const reason = this._disabledReason();
    this._menuItem.disabled = reason !== '';
    this._menuItem.title = reason;
  }

  /**
   * The title of the document a version belongs to, or '' if unknown.
   * @param {string} stableId
   * @returns {string}
   */
  _documentTitle(stableId) {
    const doc = (this.state?.fileData ?? []).find(d => d.artifacts?.some(a => a.id === stableId));
    return doc?.doc_metadata?.title || '';
  }

  /**
   * Correct the annotations of the open document with the selected model and save them as a new
   * version, see the file comment.
   * @returns {Promise<void>}
   */
  async runCorrection() {
    const notify = this.getDependency('sl-utils').notify;
    const dialog = this.getDependency('dialog');
    const sourceId = this.state?.xml;
    if (!sourceId || this._running.has(sourceId)) return;
    const model = this.getDependency('inference-settings').getDefaultModel();
    if (!model) {
      notify('No model selected. Choose one via Tools → Inference.', 'warning', 'exclamation-triangle');
      return;
    }
    const label = this.getDependency('inference-settings').getModelLabel(model.providerId, model.modelId) || `${model.providerId}/${model.modelId}`;
    const confirmed = await dialog.confirm(
      `Correct the annotations of the current document using ${escapeHtml(label)}? The result is saved as a new version. The current version cannot be edited until the correction is done.`,
      'Correct annotations in new version'
    );
    if (!confirmed || this.state?.xml !== sourceId) return;

    const xmleditor = this.getDependency('xmleditor');
    try {
      await xmleditor.saveIfDirty();
    } catch (err) {
      notify(`The document could not be saved, so its annotations cannot be corrected: ${err.message}`, 'danger', 'exclamation-octagon');
      return;
    }
    const title = this._documentTitle(sourceId);
    this._running.add(sourceId);
    this._updateMenuState();
    // before the server takes over the lock, so that the editor's heartbeat does not report it as lost
    const wasReadOnly = Boolean(this.state?.editorReadOnly);
    if (!wasReadOnly) await this.dispatchStateChange({ editorReadOnly: true });

    /** @type {StartResult|null} */
    let job = null;
    try {
      job = /** @type {StartResult} */ (await this.callPluginApi('/api/plugins/auto-annotation/correct/start', 'POST', {
        stable_id: sourceId, provider_id: model.providerId, model_id: model.modelId,
      }));
    } catch (err) {
      notify(`The annotations cannot be corrected: ${err.message}`, 'danger', 'exclamation-octagon');
      this._running.delete(sourceId);
      if (!wasReadOnly && this.state?.xml === sourceId) await this.dispatchStateChange({ editorReadOnly: false });
      this._updateMenuState();
      return;
    }

    const jobBody = { job_id: job.job_id, stable_id: sourceId };
    const progress = this.getDependency('progress');
    const progressId = `auto-annotation-${job.job_id}`;
    const heading = `Correcting annotations${title ? ` of "${title}"` : ''} using ${label}…`;
    let cancelled = false;
    let lockLost = false;
    progress.show(progressId, { label: `${heading} (0 of ${job.unit_count} units)`, value: 0, cancellable: true, onCancel: () => { cancelled = true; } });
    const refresher = setInterval(() => {
      this.callPluginApi('/api/plugins/auto-annotation/correct/heartbeat', 'POST', jobBody).catch(() => { lockLost = true; });
    }, this._lockRefreshMs);

    /** @type {string|null} */
    let savedId = null;
    try {
      const outcome = await runCorrection(
        units => /** @type {Promise<BatchResult>} */ (this.callPluginApi('/api/plugins/auto-annotation/correct/batch', 'POST', {
          ...jobBody, provider_id: model.providerId, model_id: model.modelId, units,
        })),
        job.batches,
        {
          isCancelled: () => cancelled || lockLost,
          onProgress: (processed, total, changed) => {
            progress.setValue(progressId, Math.round((processed / total) * 100));
            progress.setLabel(progressId, `${heading} (${processed} of ${total} units, ${changed} changed)`);
          },
        }
      );
      if (lockLost && outcome.state === 'cancelled') {
        outcome.state = 'failed';
        outcome.error = 'The document is no longer locked for the correction.';
      }
      clearInterval(refresher);
      if (outcome.state === 'failed' && lockLost) {
        notify(`The correction stopped: ${outcome.error}`, 'danger', 'exclamation-octagon');
        return;
      }
      if (!outcome.corrections.size) {
        if (outcome.state === 'done') notify(`${label} found nothing to correct; no new version was created.`, 'success', 'check-circle');
        else if (outcome.state === 'cancelled') notify('The correction was cancelled.', 'warning', 'exclamation-triangle');
        else notify(`The correction failed: ${outcome.error}`, 'danger', 'exclamation-octagon');
        return;
      }
      if (outcome.state !== 'done') {
        const reason = outcome.state === 'cancelled' ? 'was cancelled' : `failed (${escapeHtml(outcome.error ?? '')})`;
        const keep = await dialog.confirm(
          `The correction ${reason} after ${outcome.processed} of ${job.unit_count} units. Save the ${outcome.corrections.size} corrected unit(s) so far as a new version?`,
          'Correct annotations in new version'
        );
        if (!keep) return;
      }
      progress.setValue(progressId, null);
      progress.setLabel(progressId, 'Saving the corrected version…');
      const { xml, changed } = /** @type {{xml: string, changed: number}} */ (await this.callPluginApi('/api/plugins/auto-annotation/correct/finish', 'POST', {
        ...jobBody,
        provider_id: model.providerId, model_id: model.modelId, model_label: label,
        corrections: [...outcome.corrections].map(([index, unitXml]) => ({ index, xml: unitXml })),
        processed: outcome.processed, invalid: outcome.invalid, errors: outcome.errors, seconds: outcome.seconds,
      }));
      const client = this.getDependency('client');
      const saved = /** @type {{file_id: string}} */ (await client.saveXml(xml, sourceId, true));
      savedId = saved.file_id;
      await this._release(jobBody);
      await this.getDependency('file-selection').reload({ refresh: true });
      const summary = `${changed} of ${job.unit_count} unit(s) changed`;
      if (this.state?.xml === sourceId) {
        await this.getDependency('services').load({ xml: savedId });
        notify(`The corrected version was saved and loaded (${summary}).`, 'success', 'check-circle');
        return;
      }
      // the save locked the new version for this session; it is locked again when loaded
      await client.releaseLock(savedId).catch(() => {});
      const load = await dialog.confirm(
        `The annotations${title ? ` of "${escapeHtml(title)}"` : ''} were corrected and saved as a new version (${summary}). Load it now?`,
        'Annotations corrected'
      );
      if (load) await this.getDependency('services').load({ xml: savedId });
    } catch (err) {
      notify(`The correction failed: ${err.message}`, 'danger', 'exclamation-octagon');
    } finally {
      clearInterval(refresher);
      progress.hide(progressId);
      this._running.delete(sourceId);
      if (savedId === null) {
        await this._release(jobBody);
        // the source can be edited again; loading it acquires the editor's lock
        if (this.state?.xml === sourceId) await this.getDependency('services').load({ xml: sourceId });
      }
      this._updateMenuState();
    }
  }

  /**
   * Release the job's lock; failures are logged only, the lock expires anyway.
   * @param {{job_id: string, stable_id: string}} jobBody
   * @returns {Promise<void>}
   */
  async _release(jobBody) {
    try {
      await this.callPluginApi('/api/plugins/auto-annotation/correct/release', 'POST', jobBody);
    } catch (err) {
      console.warn(`auto-annotation: could not release the lock of ${jobBody.stable_id}: ${err.message}`);
    }
  }
}
