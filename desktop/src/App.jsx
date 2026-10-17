import React, { useCallback, useEffect, useRef, useState } from 'react'
import Icon from './icons.jsx'
import Library from './Library.jsx'
import NewProject from './NewProject.jsx'
import Properties from './Properties.jsx'
import Timeline from './Timeline.jsx'
import Viewer from './Viewer.jsx'

const vc = window.oddcut
const q = (s) => encodeURIComponent(s)
const mediaUrl = (p) => `media://v/?p=${encodeURIComponent(p)}`
const fileName = (p) => (p ? p.split(/[\\/]/).pop() : '')
const errText = (e) => String(e?.message || e).replace(/^Error invoking remote method 'api': Error: /, '')

// what a blocking job is doing, for the progress pill / the 新建 button
const STAGES = {
  analyze: [['clips', 0.45, '分析素材画面'], ['music', 0.1, '分析音乐节拍'], ['stickers', 0.3, '找能加贴纸的东西'], ['drawn', 0.15, '找适合手绘的镜头']],
  plan: [['cuts', 1, '排镜头']], music: [['music', 1, '分析新音乐']], edit: [['edit', 1, '换素材']], shot: [['edit', 1, '应用修改']],
  cut: [['edit', 1, '调整切点']], retime: [['edit', 1, '对齐新节拍']], check: [['check', 1, '看这个镜头能不能画']],
  render: [['effects', 0.5, '渲染贴纸和手绘'], ['shots', 0.45, '渲染镜头'], ['mux', 0.05, '合成音乐']],
}
function progressOf(job) {
  if (!job) return null
  const st = STAGES[job.kind] || []
  let label = st[0]?.[2] || '处理中'
  let sum = 0, all = 0
  st.forEach(([s, w, l]) => { all += w; const e = job.stages[s]; if (e) { sum += w * e.fraction; label = l + (e.total > 1 ? ` ${e.done}/${e.total}` : '') } })
  return { label, fraction: job.status === 'done' ? 1 : all ? sum / all : 0 }
}

export default function App() {
  const [backend, setBackend] = useState({ ready: false, error: null })
  const [templates, setTemplates] = useState([])
  const [looks, setLooks] = useState({ stickers: [], sticker_groups: [], drawn_styles: [] })
  const [view, setView] = useState('new')
  const [form, setForm] = useState({ clips: '', music: '', template: '', aspect: '9:16', duration: 18, autoStickers: true, drawn: true })
  const [folderInfo, setFolderInfo] = useState(null)
  const [analysis, setAnalysis] = useState(null)
  const [plan, setPlan] = useState(null)
  const [job, setJob] = useState(null)
  const [live, setLive] = useState(null)               // background preview: {video, done, total, pending}
  const [previewSrc, setPreviewSrc] = useState(null)
  const [time, setTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [selected, setSelected] = useState(null)
  const [thumbs, setThumbs] = useState(null)
  const [wave, setWave] = useState(null)
  const [draft, setDraft] = useState(null)              // working copy of the selected shot's hand-placed sticker
  const [track, setTrack] = useState(null)
  const [check, setCheck] = useState(null)              // drawable check of the selected shot
  const [boxMode, setBoxMode] = useState(false)
  const [keySel, setKeySel] = useState(null)            // selected keyframe of the selected shot's sticker
  const [recordArmed, setRecordArmed] = useState(false) // 边播边拖 waiting for the user to grab the sticker
  const [metronome, setMetronome] = useState(false)
  const [past, setPast] = useState([])
  const [future, setFuture] = useState([])
  const [toasts, setToasts] = useState([])
  const [songs, setSongs] = useState([])
  const [exported, setExported] = useState(null)        // full render of the current plan
  const [menu, setMenu] = useState(false)
  const [width, setWidth] = useState(window.innerWidth)
  const videoRef = useRef(null)
  const liveToken = useRef(0)
  const liveJob = useRef(null)
  const cancelRef = useRef(null)
  const busy = !!job && (job.status === 'running' || job.status === 'queued')

  // ---------------------------------------------------------------- basics
  const toast = useCallback((text, kind = 'info', action = null) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((t) => [...t.slice(-3), { id, text, kind, action }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'error' || action ? 9000 : 5000)
  }, [])
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
  useEffect(() => {
    if (!form.clips || !backend.ready) { setFolderInfo(null); return }
    vc.api('GET', `/api/folder?path=${q(form.clips)}`).then(setFolderInfo).catch(() => setFolderInfo(null))
  }, [form.clips, backend.ready])
  useEffect(() => {
    if (!form.music || !backend.ready) return
    vc.api('GET', `/api/songs?near=${q(form.music)}`).then((r) => setSongs(r.songs)).catch(() => setSongs([]))
  }, [form.music, backend.ready])

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
  const guard = (fn) => async (...args) => {
    try { await fn(...args) } catch (e) { if (errText(e) !== '已取消') toast(errText(e), 'error') }
  }

  // ---------------------------------------------------------------- live preview (先快后全)
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

  const loadPlan = useCallback((p) => {
    setPlan(p)
    setExported(null)
    startLive(p)
  }, [startLive])
  useEffect(() => {
    if (!plan) return
    setThumbs(null)
    vc.api('GET', `/api/thumbs?plan=${q(plan.plan)}`).then((r) => setThumbs(r.thumbs)).catch(() => {})
    vc.api('GET', `/api/waveform?plan=${q(plan.plan)}`).then(setWave).catch(() => {})
  }, [plan?.plan])

  // ---------------------------------------------------------------- history
  const snapshot = () => ({ plan, analysis })
  const commit = (r, notes = r.notes) => {
    setPast((h) => [...h.slice(-49), snapshot()])
    setFuture([])
    if (selected != null && selected >= r.shots.length) setSelected(null)
    loadPlan(r)
    if (notes?.length) toast(notes.join('；'))
  }
  const restore = (s) => { setAnalysis(s.analysis); loadPlan(s.plan) }
  const undo = () => { if (!past.length || busy) return; setFuture((f) => [snapshot(), ...f]); restore(past[past.length - 1]); setPast((h) => h.slice(0, -1)) }
  const redo = () => { if (!future.length || busy) return; setPast((h) => [...h, snapshot()]); restore(future[0]); setFuture((f) => f.slice(1)) }

  // ---------------------------------------------------------------- new project
  const pickFolder = async () => { const p = await vc.pickFolder('选择视频素材文件夹'); if (p) setForm((f) => ({ ...f, clips: p })) }
  const pickMusic = async () => { const p = await vc.pickMusic(); if (p) setForm((f) => ({ ...f, music: p })) }
  const useExamples = async () => { const ex = await vc.examples(); setForm((f) => ({ ...f, clips: ex.clips, music: ex.music })) }
  const allStickers = () => looks.stickers.map((s) => s.id)
  const generate = guard(async () => {
    const a = await runJob({ kind: 'analyze', clips: form.clips, music: form.music, template: form.template, aspect: form.aspect,
                             duration: Number(form.duration), stickers: form.autoStickers, drawn: form.drawn })
    setAnalysis(a)
    const p = await runJob({ kind: 'plan', analysis: a.analysis, stickers: form.autoStickers ? allStickers() : [] })
    setPast([]); setFuture([]); setSelected(null); setTime(0); setPreviewSrc(null)
    setView('editor')
    loadPlan(p)
    const rejected = a.clips.filter((c) => c.rejected)
    if (rejected.length) toast(`${rejected.length} 段素材没用上：${rejected.map((c) => `${c.name}（${c.reason}）`).join('，')}`)
  })

  // ---------------------------------------------------------------- edits (each one = one undo step)
  const shotAt = (t) => plan.shots.findIndex((s) => t >= s.t_start && t < s.t_end)
  const target = () => (selected != null ? selected : Math.max(0, shotAt(time)))
  const localT = (i) => { const s = plan.shots[i]; const fps = plan.fps || 30; return Math.round(Math.min(Math.max(0, time - s.t_start), s.t_end - s.t_start - 0.01) * fps) / fps }
  const applyShot = guard(async (index, change) => commit(await runJob({ kind: 'shot', analysis: analysis.analysis, plan: plan.plan, index, ...change })))
  const swapShot = guard(async (index, clip) => commit(await runJob({ kind: 'edit', analysis: analysis.analysis, plan: plan.plan, index, clip })))
  const moveCut = guard(async (k, t, snap) => commit(await runJob({ kind: 'cut', analysis: analysis.analysis, plan: plan.plan, k, t, snap })))

  const placeSticker = (i, id, x, y) => {
    setSelected(i)
    applyShot(i, { sticker: { id, mode: 'follow', in: 0, keys: [{ t: localT(i), x, y, size: 0.22 }] } })
  }
  const addSticker = (id) => placeSticker(target(), id, 0.5, 0.42)
  const dropSticker = (id, x, y) => placeSticker(Math.max(0, shotAt(time)), id, x, y)

  const runCheck = async (i, box) => {
    setCheck({ index: i, pending: true })
    try {
      const r = await runJob({ kind: 'check', plan: plan.plan, index: i, box })
      setCheck({ index: i, ...r, box })
      return r
    } catch (e) { setCheck(null); toast(errText(e), 'error'); return null }
  }
  const drawShot = async (i, style, box = null) => {
    const s = plan.shots[i]
    if (s.drawn && !box) return applyShot(i, { drawn: { style, box: s.drawn.box, force: s.drawn.force } })
    const r = await runCheck(i, box)
    if (r?.ok) { setCheck(null); return applyShot(i, { drawn: { style, box, label: r.label } }) }
  }
  const applyStyle = (style) => { const i = target(); setSelected(i); drawShot(i, style) }

  // ---------------------------------------------------------------- the selected shot's sticker draft
  useEffect(() => {
    const s = plan && selected != null ? plan.shots[selected] : null
    setDraft(s?.sticker_at ? { index: selected, id: s.sticker.split('@')[0], ...s.sticker_at } : null)
    setCheck((c) => (c && c.index === selected ? c : null))
    setBoxMode(false)
    setRecordArmed(false)
    setKeySel(null)
  }, [plan?.plan, selected])
  const myDraft = draft && draft.index === selected ? draft : null
  const draftKey = myDraft ? JSON.stringify([myDraft.mode, myDraft.keys]) : ''
  useEffect(() => {
    setTrack(null)
    if (!myDraft || !myDraft.keys.length) return
    const id = setTimeout(() => {
      vc.api('POST', '/api/track', { plan: plan.plan, index: selected, place: myDraft }).then(setTrack).catch(() => {})
    }, 90)
    return () => clearTimeout(id)
  }, [draftKey, plan?.plan, selected])

  // move / resize at the playhead: the keyframe at this moment changes, or a new one is made
  const editDraft = (patch) => {
    const i = selected
    const s = plan.shots[i]
    const t = localT(i)
    const fps = plan.fps || 30
    setDraft((d0) => {
      const d = d0 && d0.index === i ? d0 : { index: i, id: s.sticker.split('@')[0], mode: 'follow', in: 0, out: null, keys: [] }
      const keys = [...d.keys]
      let j = keys.findIndex((k) => Math.abs(k.t - t) < 0.5 / fps)
      if (j < 0) {
        const f = track ? Math.min(track.frames - 1, Math.round(t * track.fps)) : 0
        const cur = track ? { x: track.x[f], y: track.y[f], size: track.size[f] } : keys[0] || { x: 0.5, y: 0.42, size: 0.22 }
        keys.push({ t, x: cur.x, y: cur.y, size: cur.size })
        keys.sort((a, b) => a.t - b.t)
        j = keys.findIndex((k) => k.t === t)
      }
      keys[j] = { ...keys[j], ...patch, x: Math.min(1, Math.max(0, patch.x ?? keys[j].x)), y: Math.min(1, Math.max(0, patch.y ?? keys[j].y)) }
      return { ...d, keys }
    })
  }
  const draftRef = useRef(null)
  draftRef.current = myDraft
  // after the drag's last state update has rendered (draftRef is set during render)
  const commitDraft = () => setTimeout(() => {
    const d = draftRef.current
    if (!d || !d.keys.length) return
    const { index, ...place } = d
    applyShot(index, { sticker: place })
  }, 30)
  const onInOut = (which, v, done) => {
    setDraft((d) => (d ? { ...d, [which]: v } : d))
    if (done) commitDraft()
  }
  // ---- keyframes
  const fpsP = plan?.fps || 30
  const keyAt = (() => {
    if (!myDraft || selected == null) return { here: null, prev: null, next: null }
    const t = localT(selected)
    let here = null, prev = null, next = null
    myDraft.keys.forEach((k, j) => {
      if (Math.abs(k.t - t) < 0.5 / fpsP) here = j
      else if (k.t < t) prev = j
      else if (next == null) next = j
    })
    return { here, prev, next }
  })()
  const commitPlace = (d) => { const { index, ...place } = d; applyShot(index, { sticker: place }) }
  const seekKey = (j) => { if (j == null) return; setKeySel(j); setTime(plan.shots[selected].t_start + myDraft.keys[j].t + 0.001) }
  const deleteKey = (j) => {
    if (myDraft.keys.length <= 1) { toast('至少要留一个关键帧；不想要这个贴纸可以点删除'); return }
    setKeySel(null)
    commitPlace({ ...myDraft, keys: myDraft.keys.filter((_, i) => i !== j) })
  }
  const toggleKey = () => {
    if (keyAt.here != null) return deleteKey(keyAt.here)
    editDraft({})                                   // a keyframe at the playhead, where the sticker is now
    commitDraft()
  }
  const stillKey = (j) => commitPlace({ ...myDraft, mode: 'still', keys: [{ ...myDraft.keys[j], t: 0 }] })
  const moveKey = (j, x, y) => setDraft((d) => ({ ...d, keys: d.keys.map((k, i) => (i === j ? { ...k, x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) } : k)) }))
  const keyTime = (j, t, done) => {
    setDraft((d) => ({ ...d, keys: d.keys.map((k, i) => (i === j ? { ...k, t } : k)) }))
    if (done) commitDraft()
  }
  const onRecorded = (keys, t0, t1) => {
    setRecordArmed(false)
    const sizeAt = (t) => (track ? track.size[Math.min(track.frames - 1, Math.max(0, Math.round(t * track.fps)))] : 0.22)
    const kept = myDraft.keys.filter((k) => k.t < t0 - 1 / fpsP || k.t > t1 + 1 / fpsP)
    const rec = keys.map((k) => ({ ...k, size: sizeAt(k.t) }))
    commitPlace({ ...myDraft, mode: 'path', keys: [...kept, ...rec].sort((a, b) => a.t - b.t) })
    toast(`录好了：${rec.length} 个关键帧，贴纸会按你拖的路线走`)
  }
  const placeAuto = (x, y) => {
    const s = plan.shots[selected]
    applyShot(selected, { sticker: { id: s.sticker.split('@')[0], mode: 'follow', in: 0, keys: [{ t: localT(selected), x, y, size: 0.22 }] } })
  }
  const draftDirty = (() => {
    const s = myDraft && plan?.shots[selected]
    if (!s) return false
    const n = (d) => d && JSON.stringify([d.mode, d.keys, d.in || 0, d.out ?? null])
    return n(myDraft) !== n(s.sticker_at)
  })()

  // ---- metronome: a click on every beat while playing (downbeats higher)
  const audio = useRef(null)
  useEffect(() => {
    if (!playing || !metronome || !wave?.grid?.t?.length) return
    const ctx = audio.current || (audio.current = new AudioContext())
    let prev = videoRef.current?.currentTime ?? 0
    let id
    const loop = () => {
      const now = videoRef.current?.currentTime ?? prev
      if (now > prev) {
        wave.grid.t.forEach((b, i) => {
          if (b > prev && b <= now && wave.grid.on_beat?.[i]) {
            const o = ctx.createOscillator(), g = ctx.createGain()
            o.frequency.value = wave.grid.downbeat?.[i] ? 1760 : 1100
            g.gain.setValueAtTime(0.35, ctx.currentTime)
            g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.05)
            o.connect(g).connect(ctx.destination)
            o.start(); o.stop(ctx.currentTime + 0.06)
          }
        })
      }
      prev = now
      id = requestAnimationFrame(loop)
    }
    id = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(id)
  }, [playing, metronome, wave])

  const curSize = (() => {
    if (!myDraft || selected == null) return null
    const f = track ? Math.min(track.frames - 1, Math.max(0, Math.round(localT(selected) * track.fps))) : 0
    return track ? track.size[f] : myDraft.keys[0]?.size
  })()

  // ---------------------------------------------------------------- regenerate / music / export
  const reroll = guard(async () => {
    setMenu(false)
    if (plan.manual && !window.confirm('换一版会重新排所有镜头，手动加的贴纸、手绘和调过的切点都会丢掉（可以撤销回来）。确定吗？')) return
    commit(await runJob({ kind: 'plan', analysis: analysis.analysis, stickers: form.autoStickers ? allStickers() : [] }), [])
    setSelected(null)
  })
  const changeMusic = guard(async (music) => {
    setMenu(false)
    if (!music) music = await vc.pickMusic()
    if (!music) return
    const keep = plan.manual && window.confirm('保留现在的剪辑，只把切点对齐到新歌的节拍？\n\n确定：保留剪辑（贴纸、手绘、切点都保留）\n取消：按新歌重新排镜头（手动效果尽量保留）')
    const before = snapshot()
    const a = await runJob({ kind: 'music', analysis: analysis.analysis, music, duration: Number(form.duration) })
    const p = keep ? await runJob({ kind: 'retime', analysis: a.analysis, plan: plan.plan })
      : await runJob({ kind: 'plan', analysis: a.analysis, stickers: form.autoStickers ? allStickers() : [], carry_from: plan.plan })
    setAnalysis(a)
    setForm((f) => ({ ...f, music }))
    setPast((h) => [...h.slice(-49), before]); setFuture([])
    if (!keep) setSelected(null)
    loadPlan(p)
    toast([`已换成 ${a.music.name}（${Math.round(a.music.tempo)} BPM）`, keep ? '剪辑保留，切点对齐了新节拍' : '镜头按新节拍重新排了', ...(p.notes || [])].join('；'))
  })
  const exportVideo = guard(async () => {
    let v = exported?.plan === plan.plan ? exported : null
    if (!v) {
      const r = await runJob({ kind: 'render', plan: plan.plan })
      v = { plan: plan.plan, video: r.video }
      setExported(v)
    }
    const dest = await vc.exportVideo(v.video)
    if (dest) toast(`已导出：${fileName(dest)}`, 'ok', { label: '打开所在文件夹', run: () => vc.showInFolder(dest) })
  })

  // ---------------------------------------------------------------- keys
  useEffect(() => {
    const onKey = (e) => {
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return
      if (e.key === 'Escape') { if (recordArmed) setRecordArmed(false); else if (boxMode) setBoxMode(false); else if (menu) setMenu(false); else setSelected(null) }
      if ((e.key === 'Delete' || e.key === 'Backspace') && keySel != null && myDraft && !busy) { e.preventDefault(); deleteKey(keySel) }
      if (!(e.ctrlKey || e.metaKey)) return
      const k = e.key.toLowerCase()
      if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo() }
      if ((k === 'z' && e.shiftKey) || k === 'y') { e.preventDefault(); redo() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ---------------------------------------------------------------- render
  const stickerName = (sticker) => {
    if (!sticker) return ''
    const id = sticker.split('@')[0]
    return looks.stickers.find((s) => s.id === id)?.name || id
  }
  const styleName = (id) => looks.drawn_styles.find((d) => d.id === (id || 'lineart'))?.name || '线稿上色'
  const progress = progressOf(busy ? job : null)
  const toastsEl = (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <Icon name={t.kind === 'error' ? 'alert' : 'check'} size={18} /><span>{t.text}</span>
          {t.action && <button onClick={() => { t.action.run(); setToasts((x) => x.filter((y) => y.id !== t.id)) }}>{t.action.label}</button>}
          <button className="icon" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))} aria-label="关闭"><Icon name="close" size={14} /></button>
        </div>
      ))}
    </div>
  )

  if (!backend.ready || view === 'new' || !plan) {
    return (
      <>
        {!backend.ready && (
          <div className={`boot ${backend.error ? 'bad' : ''}`}>{backend.error ? <pre>{backend.error}</pre> : '正在启动剪辑引擎…'}</div>
        )}
        <NewProject form={form} setForm={setForm} templates={templates} folderInfo={folderInfo}
                    musicInfo={{ name: fileName(form.music), tempo: analysis && fileName(analysis.music?.path) === fileName(form.music) ? analysis.music.tempo : null }}
                    onPickFolder={pickFolder} onPickMusic={pickMusic} onExamples={useExamples} onGenerate={generate}
                    canGenerate={backend.ready && form.clips && form.music && form.template} busy={busy} progress={progress}
                    hasProject={!!plan} onBack={() => setView('editor')} />
        {toastsEl}
      </>
    )
  }

  const shot = selected != null ? plan.shots[selected] : null
  const [aw, ah] = plan.aspect.split(':').map(Number)
  const rail = width < 1100
  const autoSticker = !!(shot && shot.sticker && !shot.sticker_at)

  return (
    <div className={`editor ${shot ? 'with-props' : ''} ${rail ? 'narrow' : ''}`}>
      <header className="topbar">
        <button className="ghost" onClick={() => setView('new')} title="新建 / 改素材"><Icon name="back" size={18} />新建</button>
        <div className="title">
          <b>{folderInfo?.name || fileName(form.clips) || '短片'} 的剪辑</b>
          <small>{plan.aspect} · {plan.duration.toFixed(1)} 秒 · {plan.shots.length} 个镜头 · {fileName(analysis?.music?.path)}</small>
        </div>
        <span className="spacer" />
        {busy && progress && (
          <span className="busy-pill"><i className="spin" />{progress.label}{job?.kind === 'render' ? ` · ${Math.round(progress.fraction * 100)}%` : ''}
            {job?.kind === 'render' && <button className="link" onClick={() => cancelRef.current?.()}>取消</button>}</span>
        )}
        <button className="icon big" disabled={!past.length || busy} onClick={undo} title="撤销（Ctrl+Z）"><Icon name="undo" /></button>
        <button className="icon big" disabled={!future.length || busy} onClick={redo} title="重做（Ctrl+Shift+Z）"><Icon name="redo" /></button>
        <div className="menu-wrap">
          <button className="secondary" disabled={busy} onClick={() => setMenu((m) => !m)}>重新生成<Icon name="chevronDown" size={16} /></button>
          {menu && (
            <div className="menu" onMouseLeave={() => setMenu(false)}>
              <button onClick={reroll}><b>换一版</b><small>同样的素材和音乐，重新排镜头</small></button>
              <button onClick={() => changeMusic()}><b>换音乐…</b><small>可保留现在的剪辑，只对齐新节拍</small></button>
              <button onClick={() => { setMenu(false); setView('new') }}><b>改素材、画幅或时长…</b><small>回到新建页修改</small></button>
            </div>
          )}
        </div>
        <button className="primary" disabled={busy} onClick={exportVideo}><Icon name="export" size={18} />导出视频</button>
      </header>

      <Library looks={looks} activeSticker={shot?.sticker?.split('@')[0]} activeStyle={shot?.drawn ? shot.drawn.style || 'lineart' : null}
               onAddSticker={addSticker} onApplyStyle={applyStyle} music={{ path: analysis?.music?.path, name: analysis?.music?.name, tempo: analysis?.music?.tempo }}
               songs={songs} onPickSong={(s) => changeMusic(s)} onBrowseMusic={() => changeMusic()} busy={busy} rail={rail} />

      <Viewer src={previewSrc && mediaUrl(previewSrc)} videoRef={videoRef} time={time} onTime={setTime} playing={playing} setPlaying={setPlaying}
              duration={plan.duration} fps={plan.fps || 30} aspect={[aw, ah]} shot={shot} draft={myDraft} track={track}
              stickerImage={myDraft && looks.stickers.find((s) => s.id === myDraft.id)?.preview} autoSticker={autoSticker}
              onDraft={editDraft} onCommit={commitDraft} onDropSticker={dropSticker} boxMode={boxMode}
              showImage={draftDirty || !previewSrc || (live?.pending || []).includes(selected)} keySel={keySel} onSelectKey={setKeySel}
              onMoveKey={moveKey} onDeleteKey={deleteKey} onStillKey={stillKey} onPlaceAuto={placeAuto}
              recordArmed={recordArmed} onRecorded={onRecorded} wave={wave}
              onBox={(b) => { setBoxMode(false); drawShot(selected, shot.drawn?.style || 'lineart', b) }}
              status={live?.starting ? '正在生成预览…' : null} />

      {shot && (
        <Properties shot={shot} index={selected} thumb={thumbs?.[selected]} looks={looks}
                    usableClips={analysis ? analysis.clips.filter((c) => !c.rejected) : []}
                    draft={myDraft && { ...myDraft, curSize }} check={check && check.index === selected ? check : null} busy={busy}
                    onClose={() => setSelected(null)} onSwap={(clip) => swapShot(selected, clip)}
                    onRemoveSticker={() => applyShot(selected, { sticker: null })}
                    onMode={(mode) => { const { index, ...place } = myDraft; applyShot(selected, { sticker: { ...place, mode } }) }}
                    onSize={(v) => editDraft({ size: v })} onSizeCommit={commitDraft}
                    onDrawn={(on) => { if (on) drawShot(selected, 'lineart'); else { setCheck(null); applyShot(selected, { drawn: null }) } }}
                    onStyle={(st) => drawShot(selected, st)}
                    onForceDrawn={() => { const c = check; setCheck(null); applyShot(selected, { drawn: { style: 'lineart', box: c?.box, force: true, label: c?.label } }) }}
                    onBoxMode={() => setBoxMode(true)} stickerName={stickerName}
                    keyAt={keyAt} onPrevKey={() => seekKey(keyAt.prev)} onNextKey={() => seekKey(keyAt.next)} onToggleKey={toggleKey}
                    recordArmed={recordArmed} onRecordArm={() => setRecordArmed((v) => !v)} />
      )}

      <Timeline plan={plan} thumbs={thumbs} wave={wave} time={time} onSeek={setTime} selected={selected}
                onSelect={setSelected} stickerName={stickerName} styleName={styleName} draft={myDraft} onInOut={onInOut}
                onCutMove={moveCut} busy={busy} pending={live?.pending || []} live={live} looks={looks}
                keySel={keySel} onKeySelect={setKeySel} onKeyTime={keyTime} metronome={metronome} setMetronome={setMetronome} />
      {toastsEl}
    </div>
  )
}
