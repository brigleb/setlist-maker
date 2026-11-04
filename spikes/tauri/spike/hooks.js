// Spike 3, loaded on every page: report page errors and every drop, from both sides. Tauri's
// native drop arrives as a `tauri://drag-drop` event through window.__TAURI__; an HTML5 `drop`
// is what the page's own cover/artwork targets listen for today.
(() => {
  const report = (kind, data) =>
    fetch("/spike/report", { method: "POST", body: JSON.stringify({ kind, ...data }) }).catch(() => {});
  addEventListener("error", (e) => report("page-error", { msg: String(e.message), src: e.filename }));
  addEventListener("unhandledrejection", (e) => report("page-rejection", { msg: String(e.reason) }));
  for (const t of ["dragenter", "drop"]) {
    addEventListener(t, (e) =>
      report("html5-" + t, {
        files: [...(e.dataTransfer?.files || [])].map((f) => f.name),
        types: [...(e.dataTransfer?.types || [])],
        target: e.target?.id || e.target?.className || e.target?.tagName,
      }), true);
  }
  const ev = window.__TAURI__?.event;
  report("tauri-global", { present: !!window.__TAURI__, event: !!ev });
  if (ev) {
    ev.listen("tauri://drag-drop", (e) => report("tauri-js-drop", { payload: e.payload }))
      .then(() => report("tauri-js-listen", { ok: true }))
      .catch((err) => report("tauri-js-listen", { ok: false, err: String(err) }));
  }
})();
