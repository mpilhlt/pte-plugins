/**
 * Auto-annotation frontend extension: "Test model with current document".
 *
 * Adds two items to the Tools menu (inference category) for reviewers and admins:
 *
 * - "Test model with current document" runs the model probe on the open version: the version is the
 *   reference, and a model corrects an earlier extraction of the same document, once per batch
 *   size, output mode and repetition chosen in a form. A warning asks for confirmation if the open
 *   version is not a gold version. The test runs as a job on the server; a minimized progress
 *   widget follows it (also after a reload of the page), and can cancel it. Errors open an error
 *   alert. When the test is done, its results open in the plugin result dialog.
 * - "Model test results" opens the list of all stored tests.
 *
 * Routes: probe_routes.py; result pages: probe_views.py.
 *
 * @import { PluginContext } from '../../../../app/src/modules/plugin-context.js'
 */

/** Roles that may run model tests. */
const PROBE_ROLES = ['reviewer', 'admin'];

/** Batch sizes offered in the form (all checked by default). */
const BATCH_SIZES = ['units:1', 'units:4', 'units:8', 'units:16', 'units:32'];

/** Output modes offered in the form. */
const OUTPUT_MODES = [
  { value: 'all', label: 'all units', checked: true },
  { value: 'changed', label: 'changed units only', checked: false },
];

/** Default retries of a request after a provider overload (as RetryPolicy in correction.py). */
const DEFAULT_RETRIES = 4;

/** Milliseconds between status requests while a test runs. */
const POLL_MS = 3000;

/** Status requests that may fail in a row before the widget gives up. */
const MAX_POLL_FAILURES = 5;

const FINAL_STATES = new Set(['done', 'failed', 'cancelled', 'interrupted']);

const BATCH_SPEC = /^(units|chars):[1-9]\d*$/;

/**
 * @typedef {{providerId: string, modelId: string, label: string}} ModelChoice
 * @typedef {{id: string, label: string}} VersionChoice
 * @typedef {object} ProbeFormValues
 * @property {string} providerId
 * @property {string} modelId
 * @property {string} modelLabel
 * @property {string[]} batches - Batch specs (units:N or chars:N)
 * @property {string[]} modes - Output modes
 * @property {number} repeats
 * @property {number} concurrency
 * @property {number} retries
 * @property {number} temperature
 * @property {number|null} maxUnits - Only the first N units, or null for all
 * @property {string|null} inputStableId - Version to correct, or null for automatic
 * @property {boolean} forceBusy
 * @typedef {object} ProbeStatus
 * @property {string} run_id
 * @property {string} state - running, done, failed, cancelled or interrupted
 * @property {number} planned_runs
 * @property {number} finished_runs
 * @property {number} current_run
 * @property {string|null} current
 * @property {string|null} error
 * @property {string} model_label
 * @property {string|null} title
 * @typedef {{run_id: string, planned_runs: number, units: number, reference_is_gold: boolean, input_stable_id: string}} ProbeStartResult
 * @typedef {{id: string, label?: string, variant?: string, is_gold_standard?: boolean}} ArtifactItem
 * @typedef {{doc_metadata?: {title?: string}, artifacts?: ArtifactItem[]}} DocumentEntry
 */

/**
 * Escape text for HTML.
 * @param {string} text
 * @returns {string}
 */
function escapeHtml(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * The document and the version entry of a stable id in the file data.
 * @param {DocumentEntry[]} fileData
 * @param {string} stableId
 * @returns {{doc: DocumentEntry, artifact: ArtifactItem}|null}
 */
export function findVersion(fileData, stableId) {
  for (const doc of fileData ?? []) {
    const artifact = doc.artifacts?.find(a => a.id === stableId);
    if (artifact) return { doc, artifact };
  }
  return null;
}

/**
 * The versions that can be corrected instead of the automatically chosen extraction: the other
 * versions of the same variant of the document.
 * @param {DocumentEntry[]} fileData
 * @param {string} stableId - The reference version
 * @returns {VersionChoice[]}
 */
export function inputChoices(fileData, stableId) {
  const found = findVersion(fileData, stableId);
  if (!found) return [];
  return (found.doc.artifacts ?? [])
    .filter(a => a.id !== stableId && a.variant === found.artifact.variant)
    .map(a => ({ id: a.id, label: `${a.label || a.id}${a.is_gold_standard ? ' (gold)' : ''}` }));
}

/**
 * The batch specs of the form: the checked sizes and the comma-separated extra specs.
 * @param {string[]} checked
 * @param {string} extra
 * @returns {string[]}
 * @throws {Error} An extra spec is not of the form units:N or chars:N
 */
export function parseBatchSpecs(checked, extra) {
  const specs = [...checked];
  for (const item of (extra ?? '').split(',').map(s => s.trim()).filter(Boolean)) {
    if (!BATCH_SPEC.test(item)) throw new Error(`"${item}" is not a batch size; use units:N or chars:N`);
    specs.push(item);
  }
  return [...new Set(specs)];
}

/**
 * The body of POST /probe/start.
 * @param {ProbeFormValues} values
 * @param {string} referenceStableId
 * @param {string} title
 * @returns {Record<string, unknown>}
 */
export function startRequest(values, referenceStableId, title) {
  return {
    reference_stable_id: referenceStableId,
    input_stable_id: values.inputStableId,
    provider_id: values.providerId,
    model_id: values.modelId,
    model_label: values.modelLabel,
    title: title || null,
    batches: values.batches,
    modes: values.modes,
    repeats: values.repeats,
    concurrency: values.concurrency,
    retries: values.retries,
    temperature: values.temperature,
    max_units: values.maxUnits,
    force_busy: values.forceBusy,
  };
}

/**
 * The label of the progress widget.
 * @param {ProbeStatus} status
 * @returns {string}
 */
export function progressLabel(status) {
  const subject = `Testing ${status.model_label}${status.title ? ` on "${status.title}"` : ''}`;
  if (!status.current) return `${subject}…`;
  return `${subject}: run ${status.current_run} of ${status.planned_runs} (${status.current})`;
}

/**
 * Progress in percent: the finished runs.
 * @param {ProbeStatus} status
 * @returns {number}
 */
export function progressValue(status) {
  return status.planned_runs ? Math.round((status.finished_runs / status.planned_runs) * 100) : 0;
}

export default class AutoAnnotationModelTestExtension extends FrontendExtensionPlugin {
  /**
   * @param {PluginContext} context
   */
  constructor(context) {
    super(context, { name: 'auto-annotation-model-test', deps: ['tools', 'inference-settings', 'xmleditor'] });
    /** @type {HTMLElement|undefined} */
    this._testItem = undefined;
    /** @type {HTMLElement|undefined} */
    this._resultsItem = undefined;
    /** @type {Map<string, Promise<void>>} Runs followed by a progress widget in this tab */
    this._tracked = new Map();
    /** @type {number} */
    this._pollMs = POLL_MS;
    /** @type {(ms: number) => Promise<void>} */
    this._sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  }

  /**
   * Add the menu items.
   * @returns {Promise<void>}
   */
  async start() {
    const test = document.createElement('sl-menu-item');
    test.textContent = 'Test model with current document';
    test.addEventListener('click', () => { this.testModel(); });
    const results = document.createElement('sl-menu-item');
    results.textContent = 'Model test results';
    results.addEventListener('click', () => { this.showResults(null); });
    this._testItem = test;
    this._resultsItem = results;
    this.getDependency('tools').addMenuItems([test, results], 'inference');
    this.getDependency('inference-settings').onProvidersChange?.(() => this._updateMenuState());
    this.getDependency('xmleditor').on?.('editorReady', () => this._updateMenuState());
    this._updateMenuState();
    await this._resumeJobs();
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
    await this._resumeJobs();
  }

  /**
   * Whether the current user may run model tests.
   * @returns {boolean}
   */
  _mayTest() {
    const user = this.state?.user;
    return Boolean(user) && this.getDependency('acl-utils').userHasRole(user, PROBE_ROLES);
  }

  /**
   * Why the open document cannot be tested now, or '' if it can.
   * @returns {string}
   */
  _disabledReason() {
    const tree = this.getDependency('xmleditor').getXmlTree();
    if (!this.state?.xml || !tree) return 'Requires an open document.';
    const guides = this.getDependency('tei-utils').getEditorialDeclGuides(tree);
    if (!guides.some(g => g.refs.some(r => r.subtype === 'machine'))) {
      return 'Requires a document whose editorialDecl has machine-readable annotation rules (a ref with subtype="machine").';
    }
    if (!this.getDependency('inference-settings').getModels().length) return 'No model is available. Configure a provider in Tools → Inference.';
    return '';
  }

  /**
   * Show the items to reviewers and admins only; disable the test with a tooltip when the open
   * document cannot be tested.
   * @returns {void}
   */
  _updateMenuState() {
    if (!this._testItem || !this._resultsItem) return;
    const visible = this._mayTest();
    this._testItem.style.display = visible ? '' : 'none';
    this._resultsItem.style.display = visible ? '' : 'none';
    if (!visible) return;
    const reason = this._disabledReason();
    this._testItem.disabled = reason !== '';
    this._testItem.title = reason;
  }

  /**
   * Follow the user's running tests again, e.g. after a reload of the page.
   * @returns {Promise<void>}
   */
  async _resumeJobs() {
    if (!this._mayTest()) return;
    try {
      const jobs = /** @type {ProbeStatus[]} */ (await this.callPluginApi('/api/plugins/auto-annotation/probe/jobs', 'GET'));
      for (const job of jobs) this.track(job);
    } catch (err) {
      console.warn(`auto-annotation: cannot list the running model tests: ${err.message}`);
    }
  }

  /**
   * Run the model test on the open version, see the file comment.
   * @returns {Promise<void>}
   */
  async testModel() {
    const dialog = this.getDependency('dialog');
    const referenceId = this.state?.xml;
    if (!referenceId || !this._mayTest()) return;
    const found = findVersion(this.state?.fileData ?? [], referenceId);
    const title = found?.doc.doc_metadata?.title || '';
    if (!found?.artifact.is_gold_standard) {
      const go = await dialog.confirm(
        'The current version is not a gold version. The model\'s corrections will be scored against annotations that may contain errors, so the scores may be misleading. Test the model anyway?',
        'Test model with current document',
        'warning'
      );
      if (!go) return;
    }
    const values = await this.openForm(inputChoices(this.state?.fileData ?? [], referenceId));
    if (!values) return;
    /** @type {ProbeStartResult} */
    let started;
    try {
      started = /** @type {ProbeStartResult} */ (await this.callPluginApi('/api/plugins/auto-annotation/probe/start', 'POST', startRequest(values, referenceId, title)));
    } catch (err) {
      dialog.error(`The model test could not be started: ${escapeHtml(err.message)}`);
      return;
    }
    this.getDependency('sl-utils').notify(
      `The model test of ${values.modelLabel} has started (${started.planned_runs} runs on ${started.units} units). Its progress is shown at the bottom left.`,
      'primary', 'info-circle'
    );
    this.track({
      run_id: started.run_id, state: 'running', planned_runs: started.planned_runs, finished_runs: 0, current_run: 0,
      current: null, error: null, model_label: values.modelLabel, title: title || null,
    });
  }

  /**
   * Follow a running test with a minimized progress widget until it ends, then show its results
   * or its error.
   * @param {ProbeStatus} job
   * @returns {Promise<void>}
   */
  track(job) {
    const existing = this._tracked.get(job.run_id);
    if (existing) return existing;
    const promise = this._follow(job).finally(() => this._tracked.delete(job.run_id));
    this._tracked.set(job.run_id, promise);
    return promise;
  }

  /**
   * @param {ProbeStatus} job
   * @returns {Promise<void>}
   */
  async _follow(job) {
    const progress = this.getDependency('progress');
    const dialog = this.getDependency('dialog');
    const progressId = `auto-annotation-probe-${job.run_id}`;
    progress.show(progressId, {
      label: progressLabel(job), value: progressValue(job), cancellable: true, minimized: true,
      onCancel: () => {
        this.callPluginApi('/api/plugins/auto-annotation/probe/cancel', 'POST', { run_id: job.run_id })
          .catch(err => dialog.error(`The model test could not be cancelled: ${escapeHtml(err.message)}`));
      },
    });
    let status = job;
    let failures = 0;
    try {
      while (!FINAL_STATES.has(status.state)) {
        await this._sleep(this._pollMs);
        try {
          status = /** @type {ProbeStatus} */ (await this.callPluginApi('/api/plugins/auto-annotation/probe/status', 'GET', { run: job.run_id }));
          failures = 0;
        } catch (err) {
          if (++failures >= MAX_POLL_FAILURES) {
            dialog.error(`The progress of the model test cannot be followed: ${escapeHtml(err.message)}. Its results will be listed in Tools → Model test results.`);
            return;
          }
          continue;
        }
        progress.setValue(progressId, progressValue(status));
        progress.setLabel(progressId, progressLabel(status));
      }
    } finally {
      progress.hide(progressId);
    }
    const kept = status.finished_runs > 0 ? ` The results of the ${status.finished_runs} finished run(s) are kept; see Tools → Model test results.` : '';
    if (status.state === 'failed' || status.state === 'interrupted') {
      dialog.error(`The model test of ${escapeHtml(status.model_label)} ${status.state === 'failed' ? 'failed' : 'was interrupted'}: ${escapeHtml(status.error ?? 'unknown error')}.${kept}`);
      return;
    }
    if (status.state === 'cancelled' && status.finished_runs === 0) {
      this.getDependency('sl-utils').notify('The model test was cancelled before a run finished.', 'warning', 'exclamation-triangle');
      return;
    }
    await this.showResults(job.run_id);
  }

  /**
   * Open the page of a test, or of all tests, in the plugin result dialog.
   * @param {string|null} runId
   * @returns {Promise<void>}
   */
  async showResults(runId) {
    await this.getDependency('backend-plugins').execute('auto-annotation', 'probe_results', runId ? { run: runId } : {});
  }

  /**
   * Ask for the parameters of the test.
   * @param {VersionChoice[]} inputs - Versions that can be corrected instead of the automatic choice
   * @returns {Promise<ProbeFormValues|null>} null if cancelled
   */
  openForm(inputs) {
    const settings = this.getDependency('inference-settings');
    /** @type {ModelChoice[]} */
    const models = settings.getModels();
    const preferred = settings.getDefaultModel();
    const selected = Math.max(0, models.findIndex(m => preferred && m.providerId === preferred.providerId && m.modelId === preferred.modelId));

    const form = document.createElement('sl-dialog');
    form.label = 'Test model with current document';
    form.dataset.testId = 'auto-annotation-model-test-dialog';
    form.style.setProperty('--width', '36rem');
    form.innerHTML = `
      <style>
        .mt-form { display: flex; flex-direction: column; gap: 0.9rem; }
        .mt-row { display: flex; gap: 0.75rem; flex-wrap: wrap; }
        .mt-row > sl-input { flex: 1 1 7rem; }
        .mt-group-label { font-size: var(--sl-input-label-font-size-medium); margin-bottom: 0.3rem; }
        .mt-checks { display: flex; gap: 1rem; flex-wrap: wrap; }
        .mt-estimate, .mt-error { font-size: var(--sl-font-size-small); }
        .mt-error { color: var(--sl-color-danger-600); }
      </style>
      <div class="mt-form">
        <sl-select name="model" label="Model" value="${selected}" hoist>
          ${models.map((m, i) => `<sl-option value="${i}">${escapeHtml(m.label)}</sl-option>`).join('')}
        </sl-select>
        <div>
          <div class="mt-group-label">Batch sizes (review units per request)</div>
          <div class="mt-checks">
            ${BATCH_SIZES.map(b => `<sl-checkbox name="batch" value="${b}" checked>${b.split(':')[1]}</sl-checkbox>`).join('')}
          </div>
          <sl-input name="extraBatches" size="small" placeholder="More sizes, e.g. units:64, chars:4000" style="margin-top: 0.4rem"></sl-input>
        </div>
        <div>
          <div class="mt-group-label">Output modes</div>
          <div class="mt-checks">
            ${OUTPUT_MODES.map(m => `<sl-checkbox name="mode" value="${m.value}" ${m.checked ? 'checked' : ''}>${m.label}</sl-checkbox>`).join('')}
          </div>
        </div>
        <div class="mt-row">
          <sl-input name="repeats" type="number" label="Repetitions" value="1" min="1" max="10"></sl-input>
          <sl-input name="concurrency" type="number" label="In parallel" value="1" min="1" max="8"></sl-input>
          <sl-input name="retries" type="number" label="Retries" value="${DEFAULT_RETRIES}" min="0" max="10"></sl-input>
          <sl-input name="temperature" type="number" label="Temperature" value="0" min="0" max="2" step="0.1"></sl-input>
        </div>
        <div class="mt-row">
          <sl-input name="maxUnits" type="number" label="Only the first units" placeholder="all" min="1" help-text="For a quick test"></sl-input>
          <sl-select name="input" label="Version to correct" value="auto" hoist style="flex: 2 1 12rem"
            help-text="Automatic: the extraction the current version derives from, else the earliest extraction">
            <sl-option value="auto">Automatic</sl-option>
            ${inputs.map((v, i) => `<sl-option value="v${i}">${escapeHtml(v.label)}</sl-option>`).join('')}
          </sl-select>
        </div>
        <sl-checkbox name="forceBusy">Start even if the model is reported as busy</sl-checkbox>
        <div class="mt-estimate" data-name="estimate"></div>
        <div class="mt-error" data-name="error"></div>
      </div>
      <sl-button slot="footer" data-name="cancel">Cancel</sl-button>
      <sl-button slot="footer" variant="primary" data-name="start">Start test</sl-button>
    `;
    document.body.appendChild(form);

    /** @param {string} name @returns {any} */
    const field = name => form.querySelector(`[name="${name}"]`);
    /** @param {string} name @returns {string[]} */
    const checkedValues = name => [...form.querySelectorAll(`sl-checkbox[name="${name}"]`)].filter(c => /** @type {any} */ (c).checked).map(c => /** @type {any} */ (c).value);
    /** @param {string} name @param {number} fallback @returns {number} */
    const numberValue = (name, fallback) => {
      const value = Number(field(name).value);
      return Number.isFinite(value) && field(name).value !== '' ? value : fallback;
    };
    const estimate = /** @type {HTMLElement} */ (form.querySelector('[data-name="estimate"]'));
    const error = /** @type {HTMLElement} */ (form.querySelector('[data-name="error"]'));
    const updateEstimate = () => {
      let batches = 0;
      try { batches = parseBatchSpecs(checkedValues('batch'), field('extraBatches').value).length; } catch { batches = checkedValues('batch').length; }
      const runs = batches * checkedValues('mode').length * Math.max(1, numberValue('repeats', 1));
      estimate.textContent = `${runs} run(s); each run sends all units of the document to the model once.`;
    };
    form.addEventListener('sl-change', updateEstimate);
    form.addEventListener('sl-input', updateEstimate);

    return new Promise(resolve => {
      /** @type {ProbeFormValues|null} */
      let result = null;
      const close = () => form.hide();
      form.querySelector('[data-name="cancel"]')?.addEventListener('click', close);
      form.querySelector('[data-name="start"]')?.addEventListener('click', () => {
        try {
          const batches = parseBatchSpecs(checkedValues('batch'), field('extraBatches').value);
          const modes = checkedValues('mode');
          if (!batches.length) throw new Error('Choose at least one batch size.');
          if (!modes.length) throw new Error('Choose at least one output mode.');
          const model = models[Number(field('model').value)];
          if (!model) throw new Error('Choose a model.');
          const inputValue = String(field('input').value);
          result = {
            providerId: model.providerId, modelId: model.modelId, modelLabel: model.label, batches, modes,
            repeats: numberValue('repeats', 1), concurrency: numberValue('concurrency', 1), retries: numberValue('retries', DEFAULT_RETRIES),
            temperature: numberValue('temperature', 0), maxUnits: field('maxUnits').value ? numberValue('maxUnits', 0) || null : null,
            inputStableId: inputValue === 'auto' ? null : inputs[Number(inputValue.slice(1))]?.id ?? null,
            forceBusy: Boolean(field('forceBusy').checked),
          };
          close();
        } catch (err) {
          error.textContent = err.message;
        }
      });
      form.addEventListener('sl-after-hide', event => {
        if (event.target !== form) return;
        form.remove();
        resolve(result);
      });
      // the estimate reads the checkboxes' state, which they only have once defined
      Promise.all(['sl-dialog', 'sl-checkbox', 'sl-input'].map(tag => customElements.whenDefined(tag))).then(() => {
        updateEstimate();
        form.show();
      });
    });
  }
}
