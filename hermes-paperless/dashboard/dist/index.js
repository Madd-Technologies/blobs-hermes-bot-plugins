// hermes-paperless dashboard tab: search Paperless-ngx and view a document, streamed through this Hermes server.
//
// Plain JS (no build step) against the dashboard's plugin SDK (window.__HERMES_PLUGIN_SDK__: its React, components and
// authenticated fetch), registered as the plugin's tab. It reads the plugin's own routes under /api/plugins/hermes-paperless/;
// the Paperless token stays on the server.
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;

  if (!SDK || !window.__HERMES_PLUGINS__) {
    return;
  }

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useRef, useCallback } = SDK.hooks;
  const C = SDK.components || {};
  const BASE = "/api/plugins/hermes-paperless";
  const LIMIT = 30;

  // fetchJSON throws Error("<status>: <body>"); this reads the status and the server's `detail`.
  function failure(error) {
    const text = String((error && error.message) || error || "");
    const status = Number((/^(\d{3})/.exec(text) || [])[1] || 0);
    let detail = text.replace(/^\d{3}:\s*/, "");

    try {
      const parsed = JSON.parse(detail);
      detail = parsed.detail || detail;
    } catch (_) {
      // plain text
    }

    return { status, detail };
  }

  function day(created) {
    return created ? String(created).slice(0, 10) : "";
  }

  function kindOf(mime) {
    if (mime === "application/pdf") return "pdf";
    if (/^image\//.test(mime)) return "image";
    if (/^text\//.test(mime)) return "text";
    return "other";
  }

  // The document as an object URL the browser can show (its own PDF viewer for PDFs), revoked when it changes.
  function usePreview(id) {
    const [state, setState] = useState({ loading: false });

    useEffect(() => {
      if (id == null) {
        setState({ loading: false });
        return undefined;
      }

      let cancelled = false;
      let url = null;
      setState({ loading: true });

      SDK.authedFetch(`${BASE}/documents/${id}/preview`)
        .then(async (response) => {
          if (!response.ok) {
            let detail = `Paperless answered ${response.status}.`;
            try {
              detail = (await response.json()).detail || detail;
            } catch (_) {
              // not JSON
            }
            throw new Error(detail);
          }

          const blob = await response.blob();
          const mime = (response.headers.get("content-type") || blob.type || "").split(";")[0].trim();
          const kind = kindOf(mime);
          const text = kind === "text" ? await blob.text() : null;
          url = kind === "text" ? null : URL.createObjectURL(blob);

          if (!cancelled) setState({ loading: false, url, kind, text });
        })
        .catch((error) => {
          if (!cancelled) setState({ loading: false, error: String(error.message || error) });
        });

      return () => {
        cancelled = true;
        if (url) URL.revokeObjectURL(url);
      };
    }, [id]);

    return state;
  }

  async function downloadOriginal(doc, showToast) {
    try {
      const [details, response] = await Promise.all([
        SDK.fetchJSON(`${BASE}/documents/${doc.id}`).catch(() => ({})),
        SDK.authedFetch(`${BASE}/documents/${doc.id}/original`),
      ]);

      if (!response.ok) throw new Error(`Paperless answered ${response.status}.`);

      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = details.original_file_name || doc.title || `document-${doc.id}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (error) {
      showToast(String(error.message || error), "error");
    }
  }

  function Row({ doc, active, onOpen }) {
    const meta = [day(doc.created), doc.correspondent].filter(Boolean).join(" · ");

    return h(
      "button",
      { type: "button", className: "hp-row" + (active ? " hp-row-active" : ""), onClick: () => onOpen(doc), "aria-pressed": active },
      h("span", { className: "hp-row-title" }, doc.title || `Document ${doc.id}`),
      meta ? h("span", { className: "hp-row-meta" }, meta) : null,
      doc.snippet ? h("span", { className: "hp-row-snippet" }, doc.snippet) : null,
    );
  }

  function Viewer({ doc, onDownload }) {
    const preview = usePreview(doc ? doc.id : null);

    if (!doc) {
      return h("div", { className: "hp-viewer hp-viewer-empty" }, "Pick a document to see it here.");
    }

    let body;

    if (preview.loading) body = h("div", { className: "hp-viewer-note" }, "Loading…");
    else if (preview.error) body = h("div", { className: "hp-viewer-note" }, preview.error);
    else if (preview.kind === "pdf") body = h("iframe", { className: "hp-frame", src: preview.url, title: doc.title || "Document" });
    else if (preview.kind === "image") body = h("img", { className: "hp-image", src: preview.url, alt: doc.title || "Document" });
    else if (preview.kind === "text") body = h("pre", { className: "hp-text" }, preview.text);
    else body = h("div", { className: "hp-viewer-note" }, "No preview for this kind of file. Download the original to open it.");

    const Button = C.Button || "button";

    return h(
      "div",
      { className: "hp-viewer" },
      h(
        "div",
        { className: "hp-viewer-bar" },
        h("span", { className: "hp-viewer-title" }, doc.title || `Document ${doc.id}`),
        h(Button, { type: "button", variant: "outline", size: "sm", onClick: () => onDownload(doc) }, "Download original"),
      ),
      body,
    );
  }

  function PaperlessPage() {
    const [status, setStatus] = useState(null);
    const [query, setQuery] = useState("");
    const [list, setList] = useState({ loading: true, results: [], count: 0 });
    const [doc, setDoc] = useState(null);
    const asked = useRef(0);
    const toast = SDK.hooks.useToast ? SDK.hooks.useToast() : { showToast: () => {}, toast: null };

    useEffect(() => {
      SDK.fetchJSON(`${BASE}/status`).then(setStatus, () => setStatus({ configured: false, host: null }));
    }, []);

    const load = useCallback((words) => {
      const mine = ++asked.current;
      const path = words ? `${BASE}/search?q=${encodeURIComponent(words)}&limit=${LIMIT}` : `${BASE}/recent?limit=${LIMIT}`;
      setList((current) => ({ ...current, loading: true, error: null }));

      SDK.fetchJSON(path).then(
        (body) => mine === asked.current && setList({ loading: false, words, results: body.results || [], count: body.count || 0 }),
        (error) => mine === asked.current && setList({ loading: false, words, results: [], count: 0, error: failure(error) }),
      );
    }, []);

    // Newest first until something is typed; then Paperless's full-text search, after a pause in typing.
    useEffect(() => {
      const words = query.trim();
      const timer = setTimeout(() => load(words), words ? 300 : 0);
      return () => clearTimeout(timer);
    }, [query, load]);

    const Input = C.Input || "input";
    // Named for the words the list was loaded for, not what is being typed.
    const heading = list.words ? `${list.count} for “${list.words}”` : "Newest";
    const notSetUp = (status && !status.configured) || (list.error && list.error.status === 503);

    return h(
      "div",
      { className: "hermes-paperless" },
      h(
        "div",
        { className: "hp-head" },
        h("div", { className: "hp-title" }, "Paperless"),
        h("div", { className: "hp-sub" }, status && status.configured ? `Documents from ${status.host}, shown through this server.` : "Documents from Paperless-ngx, shown through this server."),
      ),
      notSetUp
        ? h(
            "div",
            { className: "hp-setup" },
            "Paperless isn’t set up on this server. Add its address and an API token to ~/.hermes/.env (PAPERLESS_URL and PAPERLESS_TOKEN, or PAPERLESS_TOKEN_FILE), then restart Hermes.",
          )
        : h(
            React.Fragment,
            null,
            h(Input, {
              className: "hp-search",
              type: "search",
              placeholder: "Search documents",
              value: query,
              onChange: (event) => setQuery(event.target.value),
              "aria-label": "Search documents",
            }),
            h(
              "div",
              { className: "hp-body" },
              h(
                "div",
                { className: "hp-list" },
                h("div", { className: "hp-list-head" }, list.loading ? "Looking…" : heading),
                list.error
                  ? h("div", { className: "hp-viewer-note" }, list.error.detail || "Couldn’t reach Paperless.")
                  : !list.loading && list.results.length === 0
                    ? h("div", { className: "hp-viewer-note" }, list.words ? "Nothing matches. Try other words." : "No documents yet.")
                    : list.results.map((item) => h(Row, { key: item.id, doc: item, active: !!doc && doc.id === item.id, onOpen: setDoc })),
              ),
              h(Viewer, { doc, onDownload: (item) => downloadOriginal(item, toast.showToast) }),
            ),
          ),
      C.Toast && toast.toast ? h(C.Toast, { toast: toast.toast }) : null,
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-paperless", PaperlessPage);
})();
