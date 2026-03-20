import * as esbuild from 'esbuild';
import { mkdirSync } from 'fs';

// Output compiled JS alongside HTML/CSS in popup/ and options/
for (const dir of ['popup', 'options']) {
  mkdirSync(dir, { recursive: true });
}

/** @type {import('esbuild').BuildOptions} */
const shared = {
  bundle: true,
  format: 'iife',
  target: 'firefox109',
  minify: false,
};

await Promise.all([
  esbuild.build({
    ...shared,
    entryPoints: ['src/background.ts'],
    outfile: 'background.js',
  }),
  esbuild.build({
    ...shared,
    entryPoints: ['src/popup/popup.ts'],
    outfile: 'popup/popup.js',
  }),
  esbuild.build({
    ...shared,
    entryPoints: ['src/options/options.ts'],
    outfile: 'options/options.js',
  }),
]);
