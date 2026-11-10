/**
 * Unit tests for the pure helpers of the MPCDF LLM admin dialog.
 *
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/static/manage-helpers.js
 */

import { describe, it } from 'node:test';
import assert from 'node:assert';
import {
  parseSseChunk,
  untilToIso,
  describeFrame,
  fieldErrors,
  parseDuration,
  formatDuration,
  formatTimeLeft,
  exportPresets,
  parsePresetsImport,
  planImport,
  copyName,
} from '../static/manage-helpers.js';

describe('parseSseChunk', () => {
  it('parses complete frames', () => {
    const { frames, rest } = parseSseChunk('', 'event: queued\ndata: {"phase":"queued"}\n\n');
    assert.deepStrictEqual(frames, [{ event: 'queued', data: { phase: 'queued' } }]);
    assert.strictEqual(rest, '');
  });

  it('keeps a partial frame across chunk boundaries', () => {
    let r = parseSseChunk('', 'event: pulling\ndata: {"phase":"pul');
    assert.deepStrictEqual(r.frames, []);
    r = parseSseChunk(r.rest, 'ling","progress":0.5}\n\nevent: rea');
    assert.strictEqual(r.frames.length, 1);
    assert.strictEqual(r.frames[0].data.progress, 0.5);
    r = parseSseChunk(r.rest, 'dy\ndata: {"phase":"ready"}\r\n\r\n');
    assert.strictEqual(r.frames[0].event, 'ready');
  });

  it('handles CRLF split between chunks, ignores pings and comments', () => {
    let r = parseSseChunk('', ': ping\r\n\r\nevent: queued\r\ndata: {"phase":"queued"}\r\n\r');
    assert.strictEqual(r.frames.length, 0);
    r = parseSseChunk(r.rest, '\n');
    assert.strictEqual(r.frames.length, 1);
    assert.strictEqual(r.frames[0].event, 'queued');
  });

  it('survives non-JSON data', () => {
    const { frames } = parseSseChunk('', 'event: x\ndata: not json\n\n');
    assert.strictEqual(frames[0].data, null);
  });
});

describe('untilToIso', () => {
  it('returns null for empty input', () => {
    assert.strictEqual(untilToIso('', new Date(2026, 0, 1, 10, 0)), null);
  });
  it('uses today when the time has not passed', () => {
    const now = new Date(2026, 5, 10, 10, 0);
    assert.strictEqual(untilToIso('18:30', now), new Date(2026, 5, 10, 18, 30).toISOString());
  });
  it('uses tomorrow when the time has passed', () => {
    const now = new Date(2026, 5, 10, 20, 0);
    assert.strictEqual(untilToIso('08:15', now), new Date(2026, 5, 11, 8, 15).toISOString());
  });
  it('rejects malformed times', () => {
    assert.throws(() => untilToIso('25:00', new Date()));
    assert.throws(() => untilToIso('abc', new Date()));
  });
});

describe('describeFrame', () => {
  it('renders the ready frame without result or key', () => {
    const text = describeFrame({
      event: 'ready',
      data: { phase: 'ready', preset: 'qwen', result: { api_key: 'SECRET-KEY', base_url: 'https://x', model: 'm' } },
    });
    assert.ok(text.includes('qwen'));
    assert.ok(!text.includes('SECRET-KEY'));
    assert.ok(!text.includes('https://x'));
  });
  it('shows pull progress as percent', () => {
    assert.match(describeFrame({ event: 'pulling', data: { phase: 'pulling', progress: 0.42 } }), /42%/);
  });
  it('shows the message of a failure', () => {
    assert.match(describeFrame({ event: 'failed', data: { phase: 'failed', message: 'boom' } }), /boom/);
  });
});

describe('fieldErrors', () => {
  it('maps the dict form', () => {
    assert.deepStrictEqual(fieldErrors({ errors: { time: 'bad' } }), { time: 'bad' });
  });
  it('maps the loc/msg list form, dropping the body prefix', () => {
    const out = fieldErrors({ errors: [{ loc: ['body', 'name'], msg: 'bad name' }, { loc: ['body', 'envs', 'X'], msg: 'bad env' }] });
    assert.strictEqual(out.name, 'bad name');
    assert.strictEqual(out.envs, 'bad env');
  });
  it('returns an empty object for other shapes', () => {
    assert.deepStrictEqual(fieldErrors({ message: 'x' }), {});
    assert.deepStrictEqual(fieldErrors(null), {});
  });
});

describe('durations', () => {
  it('parses ISO 8601 durations to minutes', () => {
    assert.strictEqual(parseDuration('PT1H'), 60);
    assert.strictEqual(parseDuration('PT1H30M'), 90);
    assert.strictEqual(parseDuration('P1DT2H'), 26 * 60);
    assert.strictEqual(parseDuration('PT20M'), 20);
  });
  it('formats minutes as HH:MM', () => {
    assert.strictEqual(formatDuration(90), '01:30');
  });
  it('formats time left', () => {
    const now = new Date('2026-01-01T10:00:00Z');
    assert.strictEqual(formatTimeLeft('2026-01-01T11:05:00Z', now), '1 h 05 min');
    assert.strictEqual(formatTimeLeft('2026-01-01T10:12:00Z', now), '12 min');
    assert.strictEqual(formatTimeLeft('2026-01-01T09:00:00Z', now), 'expired');
    assert.strictEqual(formatTimeLeft(null, now), '-');
  });
});

describe('preset export and import', () => {
  const a = { name: 'a', model: 'm1' };
  const b = { name: 'b', model: 'm2' };
  const b2 = { name: 'b', model: 'other' };
  const c = { name: 'c', model: 'm3' };

  it('round-trips an export', () => {
    const text = exportPresets([a, b], new Date('2026-01-01T00:00:00Z'));
    assert.strictEqual(JSON.parse(text).format, 'mpcdf-llm-presets');
    assert.deepStrictEqual(parsePresetsImport(text), [a, b]);
  });

  it('accepts a bare array', () => {
    assert.deepStrictEqual(parsePresetsImport(JSON.stringify([c])), [c]);
  });

  it('rejects invalid files', () => {
    assert.throws(() => parsePresetsImport('{'), /not valid JSON/);
    assert.throws(() => parsePresetsImport('{"presets": []}'), /not an MPCDF LLM preset export/);
    assert.throws(() => parsePresetsImport('[{"model": "x"}]'), /Preset 1 has no name/);
    assert.throws(() => parsePresetsImport(JSON.stringify([a, a])), /"a" occurs more than once/);
  });

  it('merge adds only presets with new names', () => {
    assert.deepStrictEqual(planImport([a, b], [b2, c], 'merge'), { put: [c], remove: [], skipped: ['b'] });
  });

  it('replace saves all imported presets and removes the others', () => {
    assert.deepStrictEqual(planImport([a, b], [b2, c], 'replace'), { put: [b2, c], remove: ['a'], skipped: [] });
  });
});

describe('copyName', () => {
  it('appends -copy and a free number', () => {
    assert.strictEqual(copyName('a', ['a']), 'a-copy');
    assert.strictEqual(copyName('a', ['a', 'a-copy', 'a-copy-2']), 'a-copy-3');
  });
});
