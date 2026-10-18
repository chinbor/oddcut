// The backend from the page's side: starting up (templates and looks once it is ready), running a job and
// polling it until it is done, and the background preview that is rendered while the user edits (先快后全).
import { useCallback, useEffect, useRef, useState } from 'react'
import { errText, vc } from './api.js'

export function useBackend(setForm) {
  const [backend, setBackend] = useState({ ready: false, error: null })
  const [templates, setTemplates] = useState([])
  const [looks, setLooks] = useState({ stickers: [], sticker_groups: [], drawn_styles: [] })
  const [width, setWidth] = useState(window.innerWidth)
  useEffect(() => {
    const onReady = async (s) => {
      setBackend(s)
      if (!s.ready) return
      const t = await vc.api('GET', '/api/templates')
      setTemplates(t.templates)
      if (t.templates[0]) setForm((f) => ({ ...f, template: f.template || t.templates[0].id, duration: t.templates[0].duration || f.duration }))
      vc.api('GET', '/api/looks').then(setLooks).catch(() => {})
    }
    vc.onBackendReady(onReady)
    vc.onBackendExit((msg) => setBackend({ ready: false, error: msg }))
    vc.backendStatus().then((s) => (s.ready || s.error) && onReady(s))
    const onResize = () => setWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  return { backend, templates, looks, width }
}

export function useJob() {
  const [job, setJob] = useState(null)
  const cancelRef = useRef(null)
  const busy = !!job && (job.status === 'running' || job.status === 'queued')
  const runJob = useCallback(async (body) => {
    const { id } = await vc.api('POST', '/api/jobs', body)
    cancelRef.current = () => vc.api('POST', `/api/jobs/${id}/cancel`)
    try {
      for (;;) {
        const j = await vc.api('GET', `/api/jobs/${id}`)
        setJob(j)
        if (j.status === 'done') return j.result
        if (j.status === 'cancelled') throw new Error('已取消')
        if (j.status === 'failed') throw new Error(j.error)
        await new Promise((r) => setTimeout(r, 250))
      }
    } finally {
      cancelRef.current = null
      setJob((j) => (j && j.status === 'running' ? { ...j, status: 'failed' } : j))
    }
  }, [])
  return { job, busy, runJob, cancelRef }
}

export function useLivePreview(toast) {
  const [live, setLive] = useState(null)               // background preview: {video, done, total, pending}
  const [previewSrc, setPreviewSrc] = useState(null)
  const liveToken = useRef(0)
  const liveJob = useRef(null)
  const startLive = useCallback(async (p) => {
    const token = ++liveToken.current
    if (liveJob.current) vc.api('POST', `/api/jobs/${liveJob.current}/cancel`).catch(() => {})
    setLive({ done: 0, total: 0, pending: [], starting: true })
    let id
    try { id = (await vc.api('POST', '/api/jobs', { kind: 'live', plan: p.plan })).id } catch (e) { toast(errText(e), 'error'); return }
    liveJob.current = id
    let shown = null
    for (;;) {
      if (token !== liveToken.current) return
      let j
      try { j = await vc.api('GET', `/api/jobs/${id}`) } catch { return }
      if (token !== liveToken.current) return
      const pa = j.partial
      if (pa) {
        setLive(pa)
        if (pa.video !== shown) { shown = pa.video; setPreviewSrc(pa.video) }
      }
      if (j.status !== 'running' && j.status !== 'queued') {
        if (j.status === 'failed') toast(`预览没生成好：${j.error}`, 'error')
        if (j.status === 'done' && j.result?.failed?.length) toast(`${j.result.failed.length} 个镜头的贴纸 / 手绘没放上去（画面里没找到合适的位置）`)
        liveJob.current = null
        return
      }
      await new Promise((r) => setTimeout(r, 400))
    }
  }, [toast])
  return { live, previewSrc, setPreviewSrc, startLive }
}
