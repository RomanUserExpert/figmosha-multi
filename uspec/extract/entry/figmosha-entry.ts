// Bundle entry for the Figmosha orchestrator.
//
// It exposes uSpec's phase functions as a `USPEC.*` global so they can be driven from
// exec scripts sent through the bridge, instead of from the plugin's own UI. Nothing is
// added to or removed from the phases themselves — this file is only a re-export surface.
//
// Built by ../build-bundle.mjs against the pinned uSpec commit. See ../README.md.

export { runPhaseA } from '../phaseA';
export { runPhaseB } from '../phaseB';
export { runPhaseC } from '../phaseC';
export { runPhaseD } from '../phaseD';
export { runPhaseE } from '../phaseE';
export { runPhaseF } from '../phaseF';
export { runPhaseG } from '../phaseG';
export { runPhaseH } from '../phaseH';
export { runPhaseI } from '../phaseI';
export { buildFirstGuess } from '../childComposition';
export { safeStringify, sanitizeText } from '../sanitize';
export { buildFigmaUrl } from '../figmaUrl';
export { slugify } from '../safe';
