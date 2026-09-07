#!/usr/bin/env node
// Build the uSpec extraction bundle the Figmosha orchestrator sends through POST /exec.
//
//   node build-bundle.mjs --src=<path to a uSpec clone>
//
// What it does, in order:
//   1. asserts the clone sits on the pinned commit (phase output must match the plan's numbers);
//   2. restores phaseF.ts / phaseG.ts, then re-applies the read-only patch below;
//   3. copies entry/figmosha-entry.ts into <src>/figma-plugin/src/entry/;
//   4. bundles it with esbuild using the plugin's own target settings.
//
// The patch is the only divergence from uSpec's source, and it is deliberately anchored on
// exact text: when a uSpec upgrade moves those lines the build FAILS rather than silently
// producing a bundle that writes to Figma.

import { readFile, writeFile, mkdir, copyFile, access } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PINNED = '1e25e9b8bc2e694689ba0489594f38807a341f14';
const OUT = path.join(HERE, 'uspec-extract.bundle.js');

const srcArg = process.argv.find((a) => a.startsWith('--src='));
const SRC = srcArg
  ? path.resolve(srcArg.slice('--src='.length))
  : process.env.USPEC_SRC && path.resolve(process.env.USPEC_SRC);
if (!SRC) {
  console.error('Give the uSpec clone: --src=<path> or USPEC_SRC=<path>.');
  console.error('  git clone --filter=blob:none https://github.com/redongreen/uSpec.git');
  console.error('  cd uSpec && git checkout ' + PINNED);
  process.exit(1);
}
const PLUGIN = path.join(SRC, 'figma-plugin');

// ---------------------------------------------------------------- 1. pinned commit

const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: SRC, encoding: 'utf8' }).trim();
if (head !== PINNED) {
  console.error('Clone is at ' + head + ', expected ' + PINNED + '.');
  console.error('  cd ' + SRC + ' && git checkout ' + PINNED);
  process.exit(1);
}

// ---------------------------------------------------------------- 2. the read-only patch
//
// Phases F and G are uSpec's only Figma writers: each measures a temporary instance and
// removes it again. Under Figmosha that write would be the agent's, and this workspace
// forbids one without a per-run go-ahead — so both phases learn a read-only mode that
// measures the variant node itself. For a component with no BOOLEAN properties and no
// SLOTs the two are the same measurement: setProperties({}) is a no-op and createInstance()
// reproduces the variant's geometry. With booleans present, read-only mode measures the
// un-revealed state, and the orchestrator records that in _extractionNotes.warnings.
//
// globalThis.USPEC_READONLY is set by the exec script, per call.

const RO_DECL = '  const RO = (globalThis as any).USPEC_READONLY === true;\n';

const PATCHES = [
  {
    file: 'src/phaseF.ts',
    edits: [
      {
        find: "  const mutationsPerformed: PhaseFResult['mutationsPerformed'] = [];\n",
        replace: "  const mutationsPerformed: PhaseFResult['mutationsPerformed'] = [];\n" + RO_DECL,
      },
      {
        find:
          '      const inst = variant.createInstance();\n' +
          "      mutationsPerformed.push({ action: 'createInstance', target: `axisDiff-${axis}-${val}` });\n",
        replace:
          '      const inst = RO ? variant : variant.createInstance();\n' +
          '      if (!RO)\n' +
          "        mutationsPerformed.push({ action: 'createInstance', target: `axisDiff-${axis}-${val}` });\n",
      },
      {
        find:
          '      try {\n' +
          '        inst.setProperties(enable);\n' +
          '        mutationsPerformed.push({\n' +
          "          action: 'setProperties-all-booleans',\n" +
          '          target: `axisDiff-${axis}-${val}`,\n' +
          '        });\n' +
          '      } catch {}\n',
        replace:
          '      try {\n' +
          '        if (!RO) {\n' +
          '          inst.setProperties(enable);\n' +
          '          mutationsPerformed.push({\n' +
          "            action: 'setProperties-all-booleans',\n" +
          '            target: `axisDiff-${axis}-${val}`,\n' +
          '          });\n' +
          '        }\n' +
          '      } catch {}\n',
      },
      {
        find:
          '        try {\n' +
          '          inst.remove();\n' +
          "          mutationsPerformed.push({ action: 'remove', target: `axisDiff-${axis}-${val}` });\n" +
          '        } catch {}\n',
        replace:
          '        try {\n' +
          '          if (!RO) {\n' +
          '            inst.remove();\n' +
          "            mutationsPerformed.push({ action: 'remove', target: `axisDiff-${axis}-${val}` });\n" +
          '          }\n' +
          '        } catch {}\n',
      },
    ],
  },
  {
    file: 'src/phaseG.ts',
    edits: [
      {
        find: "  const mutationsPerformed: PhaseGResult['mutationsPerformed'] = [];\n",
        replace: "  const mutationsPerformed: PhaseGResult['mutationsPerformed'] = [];\n" + RO_DECL,
      },
      {
        find:
          '      testInst = (variant as ComponentNode).createInstance();\n' +
          "      mutationsPerformed.push({ action: 'createInstance', target: tag });\n" +
          '      try {\n' +
          '        testInst.setProperties(enable);\n' +
          "        mutationsPerformed.push({ action: 'setProperties-all-booleans', target: tag });\n" +
          '      } catch {}\n',
        replace:
          '      testInst = RO ? (variant as any) : (variant as ComponentNode).createInstance();\n' +
          "      if (!RO) mutationsPerformed.push({ action: 'createInstance', target: tag });\n" +
          '      try {\n' +
          '        if (!RO) {\n' +
          '          testInst.setProperties(enable);\n' +
          "          mutationsPerformed.push({ action: 'setProperties-all-booleans', target: tag });\n" +
          '        }\n' +
          '      } catch {}\n',
      },
      {
        // Slot swaps replace the slot's children on the temp instance. Nothing about that
        // is measurable read-only, so the loop is skipped and slotHostGeometry comes back
        // with empty swap results.
        find: '      for (const pref of slotPrefList) {\n',
        replace: '      for (const pref of RO ? [] : slotPrefList) {\n',
      },
      {
        find:
          '      if (testInst) {\n' +
          '        try {\n' +
          '          testInst.remove();\n' +
          "          mutationsPerformed.push({ action: 'remove', target: tag });\n" +
          '        } catch {}\n' +
          '      }\n',
        replace:
          '      if (testInst && !RO) {\n' +
          '        try {\n' +
          '          testInst.remove();\n' +
          "          mutationsPerformed.push({ action: 'remove', target: tag });\n" +
          '        } catch {}\n' +
          '      }\n',
      },
    ],
  },
];

execFileSync('git', ['checkout', '--', 'figma-plugin/src/phaseF.ts', 'figma-plugin/src/phaseG.ts'], {
  cwd: SRC,
});

for (const { file, edits } of PATCHES) {
  const p = path.join(PLUGIN, file);
  // git checks the sources out with CRLF on Windows; the anchors below are written with LF.
  let text = (await readFile(p, 'utf8')).replace(/\r\n/g, '\n');
  edits.forEach((edit, i) => {
    const hits = text.split(edit.find).length - 1;
    if (hits !== 1) {
      console.error('PATCH ANCHOR LOST — ' + file + ' edit ' + (i + 1) + ' matched ' + hits + ' times, expected 1.');
      console.error('uSpec moved the code this build patches. Re-derive the patch before trusting');
      console.error('the bundle: an unpatched phase F or G WRITES TEMPORARY INSTANCES TO FIGMA.');
      process.exit(1);
    }
    text = text.replace(edit.find, edit.replace);
  });
  await writeFile(p, text, 'utf8');
  console.log('patched ' + file + ' (' + edits.length + ' edits)');
}

// ---------------------------------------------------------------- 3. the entry

const entryDir = path.join(PLUGIN, 'src/entry');
await mkdir(entryDir, { recursive: true });
await copyFile(path.join(HERE, 'entry/figmosha-entry.ts'), path.join(entryDir, 'figmosha-entry.ts'));

// ---------------------------------------------------------------- 4. bundle

const esbuildPath = path.join(PLUGIN, 'node_modules/esbuild/lib/main.js');
try {
  await access(esbuildPath);
} catch {
  console.error('esbuild not installed. Run: cd ' + PLUGIN + ' && npm install');
  process.exit(1);
}
const esbuild = await import(pathToFileURL(esbuildPath).href);

await esbuild.build({
  entryPoints: [path.join(entryDir, 'figmosha-entry.ts')],
  outfile: OUT,
  bundle: true,
  platform: 'browser',
  format: 'iife',
  globalName: 'USPEC',
  target: 'es2017',
  supported: { 'optional-catch-binding': false },
  // The plugin's own build sets this from editions.json. The orchestrator never reads
  // figma.fileKey (undefined under a development plugin) and passes --file-key instead.
  define: { __REQUIRE_FILE_LINK__: 'true' },
  logLevel: 'info',
});

const built = await readFile(OUT, 'utf8');
console.log('bundle → ' + path.relative(HERE, OUT) + '  ' + built.length + ' bytes, uSpec ' + PINNED.slice(0, 7));
