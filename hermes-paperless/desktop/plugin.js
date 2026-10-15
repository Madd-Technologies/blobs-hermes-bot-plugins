/**
 * hermes-paperless for Hermes Desktop: a Paperless page (a sidebar row, then /paperless) to search Paperless-ngx and look at a
 * document, through this plugin's own routes. Desktop loads it at runtime from <hermes home>/plugins/hermes-paperless/desktop/
 * plugin.js: plain ESM with jsx() calls, importing only the plugin SDK and React (its admission lint allows nothing else).
 *
 * A Desktop plugin reads its routes as JSON (ctx.rest), so a document arrives from /documents/{id}/preview-data as a data URL
 * and is shown from a blob URL, the way Desktop's own file preview shows a PDF. The Paperless token stays on the server.
 */

import { EmptyState, SearchField } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const LIMIT = 30

/** Set by register(): this plugin's own REST door (`/api/plugins/hermes-paperless/...`). */
let rest = null

const S = {
  page: { display: 'flex', flexDirection: 'column', gap: 12, height: '100%', padding: '16px 20px', boxSizing: 'border-box', color: 'var(--ui-text-primary)' },
  title: { fontSize: 18, fontWeight: 600 },
  sub: { fontSize: 13, color: 'var(--ui-text-tertiary)' },
  body: { display: 'grid', gridTemplateColumns: 'minmax(240px, 320px) 1fr', gap: 12, flex: 1, minHeight: 0 },
  list: { display: 'flex', flexDirection: 'column', gap: 2, overflowY: 'auto', minHeight: 0 },
  head: { fontSize: 12, color: 'var(--ui-text-tertiary)', padding: '0 10px 4px' },
  row: { display: 'flex', flexDirection: 'column', gap: 2, width: '100%', padding: '8px 10px', border: 0, borderRadius: 8, color: 'inherit', textAlign: 'left', cursor: 'pointer', font: 'inherit' },
  rowTitle: { fontWeight: 500, fontSize: 13 },
  meta: { fontSize: 12, color: 'var(--ui-text-tertiary)' },
  snippet: { fontSize: 12, color: 'var(--ui-text-secondary)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' },
  viewer: { display: 'flex', flexDirection: 'column', minHeight: 0, border: '1px solid var(--ui-stroke-tertiary)', borderRadius: 8, overflow: 'hidden', background: 'var(--ui-bg-card)' },
  bar: { padding: '8px 12px', borderBottom: '1px solid var(--ui-stroke-tertiary)', fontSize: 13, fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  frame: { flex: 1, width: '100%', border: 0, background: '#fff' },
  image: { flex: 1, minHeight: 0, objectFit: 'contain', padding: 12 },
  text: { flex: 1, margin: 0, padding: 16, overflow: 'auto', whiteSpace: 'pre-wrap', fontSize: 12 },
  note: { padding: 16, fontSize: 13, color: 'var(--ui-text-tertiary)' }
}

function message(error) {
  return String((error && error.message) || error || 'Paperless could not be reached.')
}

/** A data URL as a Blob (base64 decoded by hand: fetch() on a data: URL can be refused by the renderer's policy). */
function dataUrlToBlob(dataUrl) {
  const comma = dataUrl.indexOf(',')
  const mime = dataUrl.slice(5, comma).split(';')[0] || 'application/octet-stream'
  const binary = atob(dataUrl.slice(comma + 1))
  const bytes = new Uint8Array(binary.length)

  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }

  return new Blob([bytes], { type: mime })
}

function kindOf(mime) {
  if (mime === 'application/pdf') return 'pdf'
  if (/^image\//.test(mime)) return 'image'
  if (/^text\//.test(mime)) return 'text'
  return 'other'
}

function usePreview(id) {
  const [state, setState] = useState({ loading: false })

  useEffect(() => {
    if (id == null) {
      setState({ loading: false })
      return undefined
    }

    let cancelled = false
    let url = null
    setState({ loading: true })

    rest(`/documents/${id}/preview-data`, { timeoutMs: 60000 })
      .then(async body => {
        const blob = dataUrlToBlob(body.data_url)
        const kind = kindOf(body.mime_type)
        const text = kind === 'text' ? await blob.text() : null
        url = kind === 'text' ? null : URL.createObjectURL(blob)

        if (!cancelled) setState({ loading: false, url, kind, text })
      })
      .catch(error => {
        if (!cancelled) setState({ loading: false, error: message(error) })
      })

    return () => {
      cancelled = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [id])

  return state
}

function Row({ doc, active, onOpen }) {
  const meta = [doc.created ? String(doc.created).slice(0, 10) : '', doc.correspondent].filter(Boolean).join(' · ')

  return jsxs('button', {
    type: 'button',
    'aria-pressed': active,
    onClick: () => onOpen(doc),
    style: { ...S.row, background: active ? 'var(--ui-row-active-background)' : 'transparent' },
    children: [
      jsx('span', { style: S.rowTitle, children: doc.title || `Document ${doc.id}` }),
      meta ? jsx('span', { style: S.meta, children: meta }) : null,
      doc.snippet ? jsx('span', { style: S.snippet, children: doc.snippet }) : null
    ]
  })
}

function Viewer({ doc }) {
  const preview = usePreview(doc ? doc.id : null)

  if (!doc) {
    return jsx('div', { style: { ...S.viewer, alignItems: 'center', justifyContent: 'center' }, children: jsx('span', { style: S.note, children: 'Pick a document to see it here.' }) })
  }

  let body

  if (preview.loading) body = jsx('div', { style: S.note, children: 'Loading…' })
  else if (preview.error) body = jsx('div', { style: S.note, children: preview.error })
  else if (preview.kind === 'pdf') body = jsx('iframe', { style: S.frame, src: preview.url, title: doc.title || 'Document' })
  else if (preview.kind === 'image') body = jsx('img', { style: S.image, src: preview.url, alt: doc.title || 'Document' })
  else if (preview.kind === 'text') body = jsx('pre', { style: S.text, children: preview.text })
  else body = jsx('div', { style: S.note, children: 'No preview for this kind of file. Open it from Paperless or the Hermes dashboard.' })

  return jsxs('div', { style: S.viewer, children: [jsx('div', { style: S.bar, children: doc.title || `Document ${doc.id}` }), body] })
}

function PaperlessPage() {
  const [status, setStatus] = useState(null)
  const [query, setQuery] = useState('')
  const [list, setList] = useState({ loading: true, results: [], count: 0, words: '' })
  const [doc, setDoc] = useState(null)
  const asked = useRef(0)

  useEffect(() => {
    rest('/status').then(setStatus, () => setStatus({ configured: false, host: null }))
  }, [])

  const load = useCallback(words => {
    const mine = ++asked.current
    const path = words ? `/search?q=${encodeURIComponent(words)}&limit=${LIMIT}` : `/recent?limit=${LIMIT}`
    setList(current => ({ ...current, loading: true, error: null }))

    rest(path).then(
      body => mine === asked.current && setList({ loading: false, words, results: body.results || [], count: body.count || 0 }),
      error => mine === asked.current && setList({ loading: false, words, results: [], count: 0, error: message(error) })
    )
  }, [])

  // Newest first until something is typed; then Paperless's full-text search, after a pause in typing.
  useEffect(() => {
    const words = query.trim()
    const timer = setTimeout(() => load(words), words ? 300 : 0)
    return () => clearTimeout(timer)
  }, [query, load])

  const header = jsxs('div', {
    children: [
      jsx('div', { style: S.title, children: 'Paperless' }),
      jsx('div', { style: S.sub, children: status && status.configured ? `Documents from ${status.host}, shown through your Hermes server.` : 'Documents from Paperless-ngx, shown through your Hermes server.' })
    ]
  })

  if (status && !status.configured) {
    return jsxs('div', {
      style: S.page,
      children: [
        header,
        jsx(EmptyState, {
          title: 'Paperless isn’t set up on this server',
          description: 'Add its address and an API token to ~/.hermes/.env (PAPERLESS_URL and PAPERLESS_TOKEN, or PAPERLESS_TOKEN_FILE), then restart Hermes.'
        })
      ]
    })
  }

  const heading = list.loading ? 'Looking…' : list.words ? `${list.count} for “${list.words}”` : 'Newest'
  const rows = list.error
    ? [jsx('div', { style: S.note, children: list.error }, 'error')]
    : !list.loading && list.results.length === 0
      ? [jsx('div', { style: S.note, children: list.words ? 'Nothing matches. Try other words.' : 'No documents yet.' }, 'none')]
      : list.results.map(item => jsx(Row, { doc: item, active: !!doc && doc.id === item.id, onOpen: setDoc }, item.id))

  return jsxs('div', {
    style: S.page,
    children: [
      header,
      jsx(SearchField, { placeholder: 'Search documents', value: query, onChange: setQuery, loading: list.loading && !!query.trim(), 'aria-label': 'Search documents' }),
      jsxs('div', {
        style: S.body,
        children: [
          jsxs('div', { style: S.list, children: [jsx('div', { style: S.head, children: heading }), ...rows] }),
          jsx(Viewer, { doc })
        ]
      })
    ]
  })
}

export default {
  id: 'hermes-paperless',
  name: 'Paperless',
  description: 'Search Paperless-ngx and view documents through your Hermes server.',
  register(ctx) {
    rest = ctx.rest
    ctx.registerMany([
      { id: 'page', area: 'routes', title: 'Paperless', data: { path: '/paperless' }, render: () => jsx(PaperlessPage, {}) },
      { id: 'nav', area: 'sidebar.nav', data: { codicon: 'file-pdf', label: 'Paperless', path: '/paperless' } }
    ])
  }
}
