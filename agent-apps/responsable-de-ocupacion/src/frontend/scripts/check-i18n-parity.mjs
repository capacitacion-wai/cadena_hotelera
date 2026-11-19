#!/usr/bin/env node
/**
 * A9: build-time guard against i18n drift.
 *
 * Loads ``src/i18n/es.ts`` and ``src/i18n/en.ts``, walks the exported
 * objects, and reports keys that exist in one bundle but not in the
 * other. Exits non-zero on mismatch so CI / verify_generated_app.sh
 * can refuse delivery.
 *
 * v12.5.0 — compares the UNION of the two layers: the SKILL's (``skill.es.ts`` /
 * ``skill.en.ts``, INVARIANT) and the EMPLOYEE's (``es.ts`` / ``en.ts``, scaffold), which is
 * how i18next sees them at runtime (see ``i18n/i18n.ts``). Looking only at the employee's
 * layer, a skill string that existed only in Spanish would pass the gate and the English
 * UI would stay in Spanish without anything failing.
 *
 * Usage (from the frontend dir):
 *   node scripts/check-i18n-parity.mjs
 *
 * Output:
 *   ✓ es and en are in sync (N keys)
 *   ✗ N missing in en, M missing in es; full list below…
 */

import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(__dirname, '..');
const ES_PATH = resolve(FRONTEND_ROOT, 'src/i18n/es.ts');
const EN_PATH = resolve(FRONTEND_ROOT, 'src/i18n/en.ts');
// The SKILL's layer. It may not exist in an employee not yet upgraded to 12.5.0:
// then there is nothing to merge and the check runs as it always did.
const SKILL_ES_PATH = resolve(FRONTEND_ROOT, 'src/i18n/skill.es.ts');
const SKILL_EN_PATH = resolve(FRONTEND_ROOT, 'src/i18n/skill.en.ts');

if (!existsSync(ES_PATH) || !existsSync(EN_PATH)) {
  console.error('check-i18n-parity: cannot find', ES_PATH, 'or', EN_PATH);
  process.exit(1);
}
if (existsSync(SKILL_ES_PATH) !== existsSync(SKILL_EN_PATH)) {
  console.error('✗ only ONE of the two skill layers exists (skill.es.ts / skill.en.ts).');
  console.error('  They go together: with only one, the other language is left without the skill strings.');
  process.exit(1);
}

/**
 * Strip TypeScript syntax to a JS-evaluable IIFE that returns the
 * exported translation tree. We do this via a simple regex because
 * the i18n bundles are by contract plain object literals and never
 * import other modules — see patterns/layout.md.
 */
function loadBundle(path) {
  let src = readFileSync(path, 'utf8');
  // Remove single-line `as const` / type annotations on object exports.
  src = src.replace(/\bas\s+const\b/g, '');
  // Drop the TS `export const xx = ` prefix; we'll wrap below.
  src = src.replace(/export\s+default\s+/, '');
  src = src.replace(/export\s+const\s+\w+\s*=\s*/, '');
  // Strip trailing semicolons that follow the closing brace.
  src = src.replace(/;\s*$/g, '');
  // eslint-disable-next-line no-new-func
  return new Function(`return (${src});`)();
}

let skillEs = {};
let skillEn = {};

function flatten(obj, prefix = '') {
  const out = new Set();
  for (const [k, v] of Object.entries(obj || {})) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      for (const inner of flatten(v, path)) out.add(inner);
    } else {
      out.add(path);
    }
  }
  return out;
}

let es;
let en;
try {
  es = loadBundle(ES_PATH);
  en = loadBundle(EN_PATH);
  if (existsSync(SKILL_ES_PATH)) {
    skillEs = loadBundle(SKILL_ES_PATH);
    skillEn = loadBundle(SKILL_EN_PATH);
  }
} catch (exc) {
  console.error('check-i18n-parity: failed to parse bundles —', exc.message);
  console.error('Bundles must be plain TS object literals (no imports).');
  process.exit(1);
}

// The union of the two layers, which is what i18next resolves at runtime.
const esKeys = new Set([...flatten(es), ...flatten(skillEs)]);
const enKeys = new Set([...flatten(en), ...flatten(skillEn)]);

const missingInEn = [...esKeys].filter((k) => !enKeys.has(k)).sort();
const missingInEs = [...enKeys].filter((k) => !esKeys.has(k)).sort();

if (missingInEn.length === 0 && missingInEs.length === 0) {
  console.log(`✓ es and en are in sync (${esKeys.size} keys)`);
  process.exit(0);
}

console.error(`✗ i18n drift detected — bundles must contain identical keys.`);
console.error(`  ${missingInEn.length} key(s) in es but missing in en`);
console.error(`  ${missingInEs.length} key(s) in en but missing in es`);
if (missingInEn.length) {
  console.error('\nMissing in en.ts:');
  for (const k of missingInEn) console.error(`  - ${k}`);
}
if (missingInEs.length) {
  console.error('\nMissing in es.ts:');
  for (const k of missingInEs) console.error(`  - ${k}`);
}
process.exit(1);
