#!/usr/bin/env node
// Run uSpec's own _base.json validator against a file.
//
//   node validate.mjs <path to _base.json> --src=<uSpec clone>
//
// uSpec ships the validator as figma-plugin/scripts/validate-base.mjs, but its CLI block is
// guarded by `import.meta.url === \`file://${process.argv[1]}\``, which is never true on
// Windows (import.meta.url is file:///C:/… while argv[1] is C:\…) — running that file
// directly exits 0 without validating anything, including on a deliberately broken file.
// This wrapper imports the exported function instead, so the check is real.

import path from 'node:path';
import { pathToFileURL } from 'node:url';

const target = process.argv[2];
const srcArg = process.argv.find((a) => a.startsWith('--src='));
const SRC = srcArg
  ? path.resolve(srcArg.slice('--src='.length))
  : process.env.USPEC_SRC && path.resolve(process.env.USPEC_SRC);

if (!target || !SRC) {
  console.error('usage: node validate.mjs <_base.json> --src=<uSpec clone>  (or USPEC_SRC=…)');
  process.exit(2);
}

const validatorPath = path.join(SRC, 'figma-plugin/scripts/validate-base.mjs');
const { validateBaseFile } = await import(pathToFileURL(validatorPath).href);

const abs = path.resolve(target);
const result = await validateBaseFile(abs);
if (result.ok) {
  console.log('OK  ' + abs);
  process.exit(0);
}
console.error('FAIL  ' + abs);
for (const e of result.errors) {
  console.error('  ' + (e.instancePath || '(root)') + ' — ' + e.message);
}
process.exit(1);
