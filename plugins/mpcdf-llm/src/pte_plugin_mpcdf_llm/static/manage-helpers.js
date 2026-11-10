/**
 * @file Pure helpers of the MPCDF LLM admin dialog (manage.html). No DOM access.
 */

/**
 * @typedef {object} SseFrame
 * @property {string} event
 * @property {any} data - Parsed JSON, or null when the data was not JSON
 */

/**
 * Split SSE text into complete frames. Call it with each decoded chunk and the `rest` it returned
 * last time, so frames that span chunk boundaries are reassembled.
 * @param {string} rest
 * @param {string} chunk
 * @returns {{frames: SseFrame[], rest: string}}
 */
export function parseSseChunk(rest, chunk) {
  const buffer = (rest + chunk).replace(/\r\n/g, '\n').replace(/\r(?!$)/g, '\n');
  const parts = buffer.split('\n\n');
  const remainder = parts.pop() ?? '';
  /** @type {SseFrame[]} */
  const frames = [];
  for (const part of parts) {
    let event = 'message';
    const data = [];
    for (const line of part.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const idx = line.indexOf(':');
      const field = idx < 0 ? line : line.slice(0, idx);
      const value = idx < 0 ? '' : line.slice(idx + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    }
    if (!data.length) continue;
    let parsed = null;
    try {
      parsed = JSON.parse(data.join('\n'));
    } catch {
      parsed = null;
    }
    frames.push({ event, data: parsed });
  }
  return { frames, rest: remainder };
}

/**
 * Convert "HH:MM" to an ISO datetime (UTC): today, or tomorrow when the time has passed.
 * @param {string} value
 * @param {Date} [now]
 * @returns {string|null} null for empty input
 */
export function untilToIso(value, now = new Date()) {
  const text = (value || '').trim();
  if (!text) return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(text);
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) throw new Error('Time must be HH:MM');
  const target = new Date(now.getFullYear(), now.getMonth(), now.getDate(), Number(m[1]), Number(m[2]));
  if (target.getTime() <= now.getTime()) target.setDate(target.getDate() + 1);
  return target.toISOString();
}

/**
 * One-line text for a start/status frame. Never includes the `result` (it holds the API key).
 * @param {SseFrame} frame
 * @returns {string}
 */
export function describeFrame(frame) {
  const d = frame.data || {};
  const phase = d.phase || frame.event;
  const parts = [d.preset ? `${d.preset}: ${phase}` : String(phase)];
  if (typeof d.progress === 'number') parts.push(`${Math.round(d.progress * 100)}%`);
  if (d.message) parts.push(String(d.message));
  return parts.join(' - ');
}

/**
 * Field errors of a 422 body: `{errors: {field: msg}}` or `{errors: [{loc, msg}]}`.
 * @param {any} body
 * @returns {Object<string, string>}
 */
export function fieldErrors(body) {
  const errors = body && body.errors;
  /** @type {Object<string, string>} */
  const out = {};
  if (Array.isArray(errors)) {
    for (const e of errors) {
      const loc = (Array.isArray(e.loc) ? e.loc : []).filter((x) => x !== 'body');
      const field = loc.length ? String(loc[0]) : '';
      if (field && !(field in out)) out[field] = String(e.msg);
    }
  } else if (errors && typeof errors === 'object') {
    for (const [k, v] of Object.entries(errors)) out[k] = String(v);
  }
  return out;
}

/**
 * ISO 8601 duration (as pydantic serializes timedeltas) to whole minutes.
 * @param {string} iso
 * @returns {number}
 */
export function parseDuration(iso) {
  const m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/.exec(String(iso));
  if (!m) return NaN;
  return Number(m[1] || 0) * 1440 + Number(m[2] || 0) * 60 + Number(m[3] || 0) + Math.floor(Number(m[4] || 0) / 60);
}

/** @param {number} minutes @returns {string} ISO 8601 duration */
export function toIsoDuration(minutes) {
  return `PT${Math.round(minutes)}M`;
}

/** @param {number} minutes @returns {string} "HH:MM" */
export function formatDuration(minutes) {
  const h = Math.floor(minutes / 60);
  const mm = minutes % 60;
  return `${String(h).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
}

/**
 * @param {string|null|undefined} expiresAt
 * @param {Date} [now]
 * @returns {string}
 */
export function formatTimeLeft(expiresAt, now = new Date()) {
  if (!expiresAt) return '-';
  const minutes = Math.floor((new Date(expiresAt).getTime() - now.getTime()) / 60000);
  if (!(minutes > 0)) return 'expired';
  const h = Math.floor(minutes / 60);
  const mm = minutes % 60;
  return h ? `${h} h ${String(mm).padStart(2, '0')} min` : `${mm} min`;
}
