// The page's one door to Preact: components are written with htm's tagged
// templates (no JSX, so no build step). Nothing else imports the vendor files.

import { h, render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import htm from "htm";

export const html = htm.bind(h);
export { render, useEffect, useRef, useState };

// An icon from the page's sprite (#62), as dom.icon() is outside Preact.
export const Icon = ({ name, cls }) =>
  html`<svg class=${"ic" + (cls ? " " + cls : "")} aria-hidden="true"><use href=${"#i-" + name} /></svg>`;

// A stable key per track *object*. Rows, blocks and seams are keyed by the track
// itself, so an insert or a re-sort moves DOM nodes instead of rebuilding them
// (and an input inside one keeps its focus and its half-typed text). load()
// replaces every object, so a reload remounts everything, as it should.
const keys = new WeakMap();
let nextKey = 0;
export function keyOf(t) {
  let k = keys.get(t);
  if (k === undefined) keys.set(t, (k = ++nextKey));
  return k;
}
