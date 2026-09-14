// Ambient declarations for the globals the app's classic scripts share.
//
// This file is NOT TypeScript creeping in: nothing compiles it, nothing ships
// it, and deleting it changes what the browser does by exactly nothing. It is
// what makes `// @ts-check` usable in an editor -- without it every reference
// to `maplibregl`, `ee` or a name defined in a sibling file is reported as
// undefined, which is noise rather than a finding.
//
// Keep it in step with the seams listed in eslint.config.mjs. The two files say
// the same thing to two different tools.

/** MapLibre GL, loaded from vendor/ with an unpkg fallback (js/boot.js). */
declare const maplibregl: any;

/** The Earth Engine client, loaded on demand for the auxiliary overlays. */
declare const ee: any;

/** Set by config.js, which js/boot.js loads before the app. */
declare const LABEL_APP_CONFIG: any;

// --- defined in js/cell.js, used in js/app.js -------------------------------
declare const LABEL_CELL_M: number;
declare function s2Cell(lon: number, lat: number, epsg?: number | string): {
  epsg: number; x0: number; y0: number; ring: number[][];
};

// --- defined in js/chips.js, used in js/app.js ------------------------------
declare const CHIP_RGB: Record<string, any>;
declare const CHIP_INDEX: Record<string, any>;
declare const INDEX_RAMP: string[];
declare function chipIsIndex(combo: string): boolean;
declare function chipSpec(combo: string): any;
declare function comboBounds(p: any, combo: string): any;
declare function indexComboForSeries(series: string): string | null;
declare function rampColor(stops: string[], t: number): string;

// Nothing is declared here for the other direction -- `chipStretch` and
// `evSeries`, which js/chips.js calls and js/app.js defines. It does not need
// to be: app.js has no `module.exports`, so TypeScript reads it as a global
// script and its top-level names are already in scope everywhere. Declaring
// `evSeries` a second time here is in fact an error (TS2451), which is how
// this comment came to exist.
