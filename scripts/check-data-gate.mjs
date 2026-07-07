// Data-quality gate. Runs automatically before `npm run build` (as the npm
// `prebuild` hook), so a site build/deploy cannot ship a dataset that hasn't
// passed both validators.
//
// It reads the COMMITTED reports (no scraping, so it runs fine in CI):
//   data/nba/<season>.validation.json   (from data/scripts/validate.py)
//   data/nba/<season>.crosscheck.json   (from data/scripts/crosscheck.py)
//
// Blocks the build if, for any season dataset, there are internal validation
// errors, unreconciled cross-source team mismatches, or unmapped team names —
// or if a dataset is missing either report entirely.
//
// Escape hatch: set ALLOW_UNVERIFIED=1 to build anyway (e.g. to publish a
// known-imperfect demo). CI does not set it, so deploys stay gated.

import { readdirSync, readFileSync, existsSync } from 'node:fs';
import path from 'node:path';

const DIR = 'data/nba';
const SEASON_RE = /^(\d{4}-\d{2})\.json$/;

const seasons = readdirSync(DIR)
  .map((f) => f.match(SEASON_RE))
  .filter(Boolean)
  .map((m) => m[1]);

let problems = 0;
const lines = [];

for (const s of seasons) {
  const vPath = path.join(DIR, `${s}.validation.json`);
  const cPath = path.join(DIR, `${s}.crosscheck.json`);

  if (!existsSync(vPath)) {
    problems++;
    lines.push(`✗ ${s}: missing validation report — run data/scripts/validate.py`);
    continue;
  }
  if (!existsSync(cPath)) {
    problems++;
    lines.push(`✗ ${s}: missing cross-check report — run data/scripts/crosscheck.py`);
    continue;
  }

  const v = JSON.parse(readFileSync(vPath, 'utf-8'));
  const c = JSON.parse(readFileSync(cPath, 'utf-8'));
  const vErr = v.errors ?? 0;
  const unrec = c.unreconciled?.length ?? 0;
  const unmapped = c.unmapped_espn_teams?.length ?? 0;
  const bad = vErr + unrec + unmapped;

  if (bad > 0) {
    problems += bad;
    lines.push(
      `✗ ${s}: ${vErr} validation error(s), ${unrec} unreconciled team ` +
        `mismatch(es), ${unmapped} unmapped team name(s)`,
    );
  } else {
    lines.push(`✓ ${s}: clean (internal + cross-source)`);
  }
}

console.log('data gate:');
if (seasons.length === 0) lines.push('(no season datasets found)');
for (const l of lines) console.log('  ' + l);

if (problems > 0) {
  if (process.env.ALLOW_UNVERIFIED) {
    console.warn(
      `\n⚠  ${problems} unresolved data flag(s) — proceeding because ` +
        `ALLOW_UNVERIFIED is set.`,
    );
    process.exit(0);
  }
  console.error(
    `\n✗ Build blocked: ${problems} unresolved data flag(s).\n` +
      `  Resolve them (see data/nba/<season>.validation.json / .crosscheck.json),\n` +
      `  or set ALLOW_UNVERIFIED=1 to publish a known-imperfect build anyway.`,
  );
  process.exit(1);
}

console.log('\n✓ data gate passed.');
