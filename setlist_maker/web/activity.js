// The toolbar's activity area (#62): work that is still running. A toast says
// that something finished; this says what is happening now, for as long as it
// takes. Shazam lookups queue on the server (it paces them like an identify
// run), so several of a kind read as one line, "… · 2 queued".
//
//   const job = begin("shazam", "Asking Shazam about 1:23:45");
//   try { ... } finally { job.end(); }

import { el, icon } from "./dom.js";

const box = document.getElementById("activity");
const tasks = [];

export function begin(kind, label) {
  const task = { kind, label };
  tasks.push(task);
  paint();
  return {
    set(l) { task.label = l; paint(); },
    end() {
      const at = tasks.indexOf(task);
      if (at !== -1) { tasks.splice(at, 1); paint(); }
    },
  };
}

// Runs fn under a task that ends however fn does.
export async function during(kind, label, fn) {
  const job = begin(kind, label);
  try { return await fn(); } finally { job.end(); }
}

function paint() {
  const kinds = [];
  for (const t of tasks) if (!kinds.includes(t.kind)) kinds.push(t.kind);
  box.replaceChildren(...kinds.map(k => {
    const of = tasks.filter(t => t.kind === k);
    const row = el("div", "act");
    row.append(icon("loader", "spin"), el("span", "act-label", of[0].label));
    if (of.length > 1) row.append(el("span", "act-more", "· " + (of.length - 1) + " queued"));
    return row;
  }));
  box.title = tasks.map(t => t.label).join("\n");
  box.hidden = !tasks.length;
}
