import { test } from 'node:test';
import assert from 'node:assert/strict';
import { filterByScope, analyzeCommits, generateNotes } from '../scoped-release-plugin.js';

const logger = { log: () => {}, error: () => {} };
const baseContext = {
  cwd: process.cwd(),
  options: { repositoryUrl: 'https://github.com/mpilhlt/pte-plugins' },
  lastRelease: { version: '1.0.0', gitTag: 'grobid-v1.0.0', gitHead: 'aaaaaaa' },
  nextRelease: { version: '1.1.0', gitTag: 'grobid-v1.1.0', gitHead: 'bbbbbbb' },
  logger,
};

async function withPluginId(pluginId, fn) {
  const previous = process.env.PLUGIN_ID;
  if (pluginId === undefined) delete process.env.PLUGIN_ID;
  else process.env.PLUGIN_ID = pluginId;
  try {
    return await fn();
  } finally {
    if (previous === undefined) delete process.env.PLUGIN_ID;
    else process.env.PLUGIN_ID = previous;
  }
}

test('keeps only commits whose scope matches the plugin id', () => {
  const commits = [
    { message: 'fix(archival-edition): handle broken pb sync' },
    { message: 'feat(grobid): add retry' },
  ];
  assert.deepEqual(filterByScope(commits, 'archival-edition'), [commits[0]]);
});

test('drops unscoped commits', () => {
  const commits = [{ message: 'chore: update deps' }];
  assert.deepEqual(filterByScope(commits, 'archival-edition'), []);
});

test('does not match on scope substrings', () => {
  const commits = [
    { message: 'fix(grobid-extra): tweak' },
    { message: 'fix(grobid): tweak' },
  ];
  assert.deepEqual(filterByScope(commits, 'grobid'), [commits[1]]);
});

test('returns an empty array when nothing matches', () => {
  assert.deepEqual(filterByScope([{ message: 'fix(other): x' }], 'archival-edition'), []);
});

test('parses scope with the same header pattern @semantic-release/commit-analyzer uses for the angular preset (comma-containing scope)', () => {
  // conventional-commits-parser's own default headerPattern excludes "," from the scope
  // character class, so this header fails to match at all under the bare default parser
  // (scope comes back undefined, not "a,b"), while the angular preset's parserOpts --
  // which @semantic-release/commit-analyzer and @semantic-release/release-notes-generator
  // actually use when configured with `preset: 'angular'` -- parses it fine. filterByScope
  // must use the same parserOpts the real analyzer uses, or a commit the real analyzer can
  // read could be invisible to our filter for reasons that have nothing to do with scope.
  const commits = [{ message: 'fix(a,b): multi scope' }];
  assert.deepEqual(filterByScope(commits, 'a'), commits);
});

test('a comma-separated scope counts for each plugin it names', () => {
  const commits = [{ message: 'feat(grobid, annotation-review): by page' }, { message: 'fix(grobid-extra,other): x' }];
  assert.deepEqual(filterByScope(commits, 'grobid'), [commits[0]]);
  assert.deepEqual(filterByScope(commits, 'annotation-review'), [commits[0]]);
  assert.deepEqual(filterByScope(commits, 'grobid-extra'), [commits[1]]);
  assert.deepEqual(filterByScope(commits, 'a,b'), []);
});

test('analyzeCommits delegates to @semantic-release/commit-analyzer, counting only scoped commits', async () => {
  await withPluginId('grobid', async () => {
    const patch = await analyzeCommits(
      { preset: 'angular' },
      { ...baseContext, commits: [{ message: 'fix(grobid): a bug' }, { message: 'feat(archival-edition): unrelated' }] }
    );
    assert.equal(patch, 'patch');

    const minor = await analyzeCommits(
      { preset: 'angular' },
      { ...baseContext, commits: [{ message: 'feat(grobid): a feature' }, { message: 'fix(grobid): a bug' }] }
    );
    assert.equal(minor, 'minor');

    const none = await analyzeCommits(
      { preset: 'angular' },
      { ...baseContext, commits: [{ message: 'fix(archival-edition): not us' }] }
    );
    assert.equal(none, null);
  });
});

test('analyzeCommits throws without PLUGIN_ID set', async () => {
  await withPluginId(undefined, async () => {
    await assert.rejects(
      () => analyzeCommits({ preset: 'angular' }, { ...baseContext, commits: [{ message: 'fix(grobid): a bug' }] }),
      /PLUGIN_ID/
    );
  });
});

test('generateNotes delegates to @semantic-release/release-notes-generator, including only scoped commits', async () => {
  await withPluginId('grobid', async () => {
    const notes = await generateNotes(
      { preset: 'angular' },
      {
        ...baseContext,
        commits: [{ message: 'fix(grobid): fix the thing', hash: 'c0ffee1' }, { message: 'fix(archival-edition): other', hash: 'c0ffee2' }],
      }
    );
    assert.match(notes, /fix the thing/);
    assert.doesNotMatch(notes, /other/);
  });
});
