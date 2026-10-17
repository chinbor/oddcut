import React, { useCallback, useEffect, useRef, useState } from 'react'
import Inspector from './Inspector.jsx'
import Timeline from './Timeline.jsx'

const vc = window.oddcut

const STAGES = {
  analyze: [['clips', 0.45, '分析素材画面'], ['music', 0.1, '分析音乐节拍'],
            ['stickers', 0.3, '识别可加贴纸的物体'], ['drawn', 0.15, '寻找手绘开场镜头']],
  plan: [['cuts', 1, '生成剪辑方案']],
  music: [['music', 1, '分析新音乐的节拍']],
  edit: [['edit', 1, '替换镜头']],
  shot: [['edit', 1, '应用修改']],
  check: [['check', 1, '检查能不能画']],
  render: [['effects', 0.45, '渲染贴纸和手绘'], ['shots', 0.5, '渲染镜头'], ['mux', 0.05, '合成音乐']],
  preview: [['shots', 0.9, '生成预览'], ['mux', 0.1, '合成音乐']],
}
const ROLES = {
  hook: ['开场', '#7aa2ff'], drawn: ['手绘开场', '#ff9f6b'], body: ['快切', '#5c6378'],
  breath: ['呼吸镜头', '#5fc9a8'], sticker: ['贴纸', '#ffd166'], outro: ['结尾', '#b18cff'],
}
const STICKERS = { eyes: '眼睛', anger: '💢', question: '问号' }
// "plane@animal" -> 小飞机（跟随）; names come from /api/looks
const stickerLabel = (sticker, looks) => {
  const [id, mode] = sticker.split('@')
  const name = looks.stickers.find((s) => s.id === id)?.name || STICKERS[id] || id
  return mode ? `${name}（${mode === 'free' ? '装饰' : '跟随'}）` : name
}
const ASPECT_NAMES = { '9:16': '竖屏 9:16', '3:4': '3:4', '4:5': '4:5', '1:1': '方形 1:1', '16:9': '横屏 16:9' }

// overall 0..1 for a job: stages run in order, so a later stage means the earlier ones are finished
const stagesOf = (job) => STAGES[job?.preview ? 'preview' : job?.kind] || []

function jobFraction(job) {
  if (!job) return 0
  if (job.status === 'done') return 1
  const stages = stagesOf(job)
  const lastSeen = Math.max(-1, ...stages.map(([s], i) => (job.stages[s] ? i : -1)))
  let sum = 0
  stages.forEach(([s, w], i) => {
    if (i < lastSeen) sum += w
    else if (job.stages[s]) sum += w * job.stages[s].fraction
  })
  return Math.min(1, sum / stages.reduce((a, [, w]) => a + w, 0))
}

function stageLabel(job) {
  const stages = stagesOf(job)
  for (let i = stages.length - 1; i >= 0; i--) {
    const [s, , label] = stages[i]
    const st = job.stages[s]
    if (st) return st.total > 1 ? `${label} ${st.done}/${st.total}` : label
  }
  return '准备中'
}

const fileName = (p) => (p ? p.split(/[\\/]/).pop() : '')
const mediaUrl = (p) => `media://v/?p=${encodeURIComponent(p)}`

export default function App() {
  const [backend, setBackend] = useState({ ready: false, error: null })
  const [templates, setTemplates] = useState([])
  const [aspects, setAspects] = useState([])
  const [form, setForm] = useState({ clips: '', music: '', template: '', aspect: '9:16', duration: 18,
                                     stickerSel: ['eyes', 'anger', 'question'], drawn: true, drawnStyle: 'lineart' })
  const [looks, setLooks] = useState({ stickers: [], sticker_groups: [], drawn_styles: [] })
  const [planLooks, setPlanLooks] = useState(null)  // the selection the current plan was made with
  const [job, setJob] = useState(null)          // the running / last job, as the backend reports it
  const [analysis, setAnalysis] = useState(null)
  const [plan, setPlan] = useState(null)
  const [video, setVideo] = useState(null)        // the full render of the current plan
  const [preview, setPreview] = useState(null)    // the quick low-res render of the current plan
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const cancelRef = useRef(null)
  const videoRef = useRef(null)
  const [selected, setSelected] = useState(null)   // shot open in the inspector
  const [time, setTime] = useState(0)              // playhead, seconds on the timeline
  const [playing, setPlaying] = useState(false)
  const [thumbs, setThumbs] = useState(null)
  const [wave, setWave] = useState(null)
  const [tab, setTab] = useState('shot')
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))

  useEffect(() => {
    const onReady = async (s) => {
      setBackend(s)
      if (!s.ready) return
      const [t, a] = await Promise.all([vc.api('GET', '/api/templates'), vc.api('GET', '/api/aspects')])
      setTemplates(t.templates)
      setAspects(a.aspects.map((x) => x.id))
      if (t.templates[0]) setForm((f) => ({ ...f, template: f.template || t.templates[0].id,
                                            duration: t.templates[0].duration || f.duration }))
      vc.api('GET', '/api/looks').then(setLooks).catch(() => {})   // pictures are optional
    }
    vc.onBackendReady(onReady)
    vc.onBackendExit((msg) => setBackend({ ready: false, error: msg }))
    vc.backendStatus().then((s) => (s.ready || s.error) && onReady(s))
  }, [])

  const runJob = useCallback(async (body) => {
    const { id } = await vc.api('POST', '/api/jobs', body)
    cancelRef.current = () => vc.api('POST', `/api/jobs/${id}/cancel`)
    for (;;) {
      const j = { ...(await vc.api('GET', `/api/jobs/${id}`)), preview: !!body.preview }
      setJob(j)
      if (j.status === 'done') return j.result
      if (j.status === 'cancelled') throw new Error('已取消')
      if (j.status === 'failed') throw new Error(j.error)
      await new Promise((r) => setTimeout(r, 400))
    }
  }, [])

  const guard = (fn) => async () => {
    setError(null)
    setNotice(null)
    try {
      await fn()
    } catch (e) {
      setError(String(e.message || e).replace(/^Error invoking remote method 'api': Error: /, ''))
    } finally {
      cancelRef.current = null
      setJob((j) => (j && j.status === 'running' ? { ...j, status: 'failed' } : j))
    }
  }

  const busy = job && (job.status === 'running' || job.status === 'queued')

  const lookParams = () => ({ stickers: form.stickerSel, drawn_style: form.drawnStyle })
  const looksKey = JSON.stringify(lookParams())
  const toggleSticker = (id) => setForm((f) => ({
    ...f, stickerSel: f.stickerSel.includes(id) ? f.stickerSel.filter((x) => x !== id) : [...f.stickerSel, id],
  }))

  // every new plan gets a quick preview right away; the full render is a separate click.
  // keep=true (an edit of the same plan): the old preview stays up until the new one is ready
  const showPlan = async (p, keep = false) => {
    setPlan(p)
    setVideo(null)
    if (!keep) setPreview(null)
    setPreview(await runJob({ kind: 'render', plan: p.plan, preview: true }))
  }

  const q = (s) => encodeURIComponent(s)
  useEffect(() => {
    if (!plan) return
    setThumbs(null)
    vc.api('GET', `/api/thumbs?plan=${q(plan.plan)}`).then((r) => setThumbs(r.thumbs)).catch(() => {})
    vc.api('GET', `/api/waveform?plan=${q(plan.plan)}`).then(setWave).catch(() => {})
  }, [plan?.plan])

  // playhead <-> video: scrubbing seeks the video; while playing the playhead follows it every frame
  const seek = (t) => {
    setTime(t)
    const v = videoRef.current
    if (v && Math.abs(v.currentTime - t) > 0.02) v.currentTime = t
  }
  useEffect(() => {
    if (!playing) return
    let id
    const tick = () => { if (videoRef.current) setTime(videoRef.current.currentTime); id = requestAnimationFrame(tick) }
    id = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(id)
  }, [playing])
  useEffect(() => {
    const onKey = (e) => {
      if (e.code !== 'Space' || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(e.target.tagName)) return
      const v = videoRef.current
      if (!v) return
      e.preventDefault()
      v.paused ? v.play() : v.pause()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const selectShot = (i) => {
    setSelected(i)
    setTab('shot')
    const s = plan.shots[i]
    if (time < s.t_start || time >= s.t_end) seek(s.t_start + 0.01)
  }

  const applyShot = (index, change) => guard(async () => {
    const r = await runJob({ kind: 'shot', analysis: analysis.analysis, plan: plan.plan, index, ...change })
    if (r.notes?.length) setNotice(r.notes.join('；'))
    if (index >= r.shots.length) setSelected(null)
    await showPlan(r, true)
  })()
  const checkShot = (index, box) => runJob({ kind: 'check', plan: plan.plan, index, box })
  const getStill = (index, t) => vc.api('GET', `/api/still?plan=${q(plan.plan)}&index=${index}&t=${t}`)

  const makePlan = guard(async () => {
    setSelected(null)
    setPlan(null)
    setVideo(null)
    setPreview(null)
    const a = await runJob({ kind: 'analyze', clips: form.clips, music: form.music, template: form.template,
                             aspect: form.aspect, duration: Number(form.duration),
                             stickers: true, drawn: form.drawn })
    setAnalysis(a)
    const rejected = a.clips.filter((c) => c.rejected)
    if (rejected.length) setNotice(`${rejected.length} 段素材没被选用：${rejected.map((c) => `${c.name}（${c.reason}）`).join('，')}`)
    setPlanLooks(looksKey)
    await showPlan(await runJob({ kind: 'plan', analysis: a.analysis, ...lookParams() }))
  })

  const rerollPlan = guard(async () => {
    if (plan?.manual && !window.confirm('换一版会重新排所有镜头，手动加的贴纸和手绘都会丢掉。确定吗？')) return
    setSelected(null)
    setPlanLooks(looksKey)
    await showPlan(await runJob({ kind: 'plan', analysis: analysis.analysis, ...lookParams() }))
  })

  const changeMusic = guard(async () => {
    const music = await vc.pickMusic()
    if (!music) return
    const a = await runJob({ kind: 'music', analysis: analysis.analysis, music, duration: Number(form.duration) })
    setAnalysis(a)
    set('music')(music)
    setPlanLooks(looksKey)
    const p = await runJob({ kind: 'plan', analysis: a.analysis, ...lookParams(), carry_from: plan.plan })
    setNotice([`已换成 ${a.music.name}（${a.music.tempo} BPM），镜头按新节拍重新排了`, ...(p.notes || [])].join('；'))
    setSelected(null)
    await showPlan(p)
  })

  const swapShot = (index, clip) => guard(async () => {
    await showPlan(await runJob({ kind: 'edit', analysis: analysis.analysis, plan: plan.plan, index, clip }), true)
  })()

  const render = guard(async () => {
    setVideo(await runJob({ kind: 'render', plan: plan.plan }))
  })

  const useExamples = async () => {
    const ex = await vc.examples()
    setForm((f) => ({ ...f, clips: ex.clips, music: ex.music }))
  }

  const usableClips = analysis ? analysis.clips.filter((c) => !c.rejected) : []
  const shown = video || preview

  const exportVideo = async () => {
    const dest = await vc.exportVideo(video.video)
    if (dest) setNotice(`已导出到 ${dest}`)
  }

  const canStart = backend.ready && form.clips && form.music && form.template && !busy
  const [aw, ah] = (plan?.aspect || form.aspect).split(':').map(Number)

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">
          Oddcut <span className={`dot ${backend.ready ? 'ok' : backend.error ? 'bad' : ''}`} />
        </div>

        <section>
          <h3>1 · 素材</h3>
          <button className="pick" onClick={async () => { const p = await vc.pickFolder('选择素材文件夹'); if (p) set('clips')(p) }}>
            <span>视频文件夹</span><b>{fileName(form.clips) || '选择…'}</b>
          </button>
          <button className="pick" onClick={async () => { const p = await vc.pickMusic(); if (p) set('music')(p) }}>
            <span>音乐</span><b>{fileName(form.music) || '选择…'}</b>
          </button>
          <button className="link" onClick={useExamples}>用示例素材试一下</button>
        </section>

        <section>
          <h3>2 · 风格</h3>
          {templates.map((t) => (
            <label key={t.id} className={`card ${form.template === t.id ? 'on' : ''}`}>
              <input type="radio" name="tpl" checked={form.template === t.id}
                     onChange={() => setForm((f) => ({ ...f, template: t.id, duration: t.duration || f.duration }))} />
              <b>{t.name}</b>
              <small>{t.description}</small>
            </label>
          ))}
        </section>

        <section>
          <h3>3 · 画幅和时长</h3>
          <div className="seg">
            {aspects.map((a) => (
              <button key={a} className={form.aspect === a ? 'on' : ''} onClick={() => set('aspect')(a)}>
                {ASPECT_NAMES[a] || a}
              </button>
            ))}
          </div>
          <label className="row">
            <span>目标时长</span>
            <input type="range" min="10" max="40" value={form.duration} onChange={(e) => set('duration')(e.target.value)} />
            <b>{form.duration}s</b>
          </label>
        </section>

        <section>
          <h3>4 · 贴纸选择 <small>{form.stickerSel.length ? `已选 ${form.stickerSel.length} 种` : '不加贴纸'}</small></h3>
          {looks.sticker_groups.filter((g) => looks.stickers.some((s) => s.group === g.id)).map((g) => (
            <div key={g.id} className="look-group">
              <span className="look-label">{g.name}</span>
              <div className="tiles">
                {looks.stickers.filter((s) => s.group === g.id).map((s) => (
                  <button key={s.id} className={`tile ${form.stickerSel.includes(s.id) ? 'on' : ''}`}
                          onClick={() => toggleSticker(s.id)} aria-pressed={form.stickerSel.includes(s.id)}
                          title={s.families.includes('free') ? '有猫狗、杯子等主体时跟着它动，没有时在画面空白处做装饰动画'
                            : s.families.includes('animal') ? '贴在猫狗等动物头上' : '贴在杯子、碗等物体上'}>
                    <img src={s.preview} alt="" />
                    <span>{s.name}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </section>

        <section>
          <h3>5 · 手绘风格选择</h3>
          <label className="row"><input type="checkbox" checked={form.drawn} onChange={(e) => set('drawn')(e.target.checked)} />用手绘开场（素材里有合适镜头时）</label>
          <div className={`styles ${form.drawn ? '' : 'off'}`}>
            {looks.drawn_styles.map((d) => (
              <button key={d.id} className={`style-card ${form.drawnStyle === d.id ? 'on' : ''}`} disabled={!form.drawn}
                      onClick={() => set('drawnStyle')(d.id)} aria-pressed={form.drawnStyle === d.id}>
                <img src={d.preview} alt="" />
                <b>{d.name}</b>
                <small>{d.description}</small>
              </button>
            ))}
          </div>
        </section>

        <button className="primary" disabled={!canStart} onClick={makePlan}>
          {analysis ? '重新分析并生成方案' : '分析素材并生成方案'}
        </button>
      </aside>

      <main className="main">
        {!backend.ready && (
          <div className={`banner ${backend.error ? 'bad' : ''}`}>
            {backend.error ? <pre>{backend.error}</pre> : '正在启动剪辑引擎…'}
          </div>
        )}
        {error && <div className="banner bad">{error}</div>}
        {plan && analysis && planLooks && planLooks !== looksKey && !busy && (
          <div className="banner">贴纸或手绘风格改了，点「换一版」按新的选择重新排镜头。</div>
        )}
        {notice && <div className="banner">{notice}</div>}

        {busy && (
          <div className="progress">
            <div className="bar"><i style={{ width: `${Math.round(jobFraction(job) * 100)}%` }} /></div>
            <div className="meta">
              <span>{stageLabel(job)} · {Math.round(jobFraction(job) * 100)}% · {job.elapsed}s</span>
              <button className="link" onClick={() => cancelRef.current?.()}>取消</button>
            </div>
          </div>
        )}

        {!plan && !busy && (
          <div className="empty">
            <h2>选好素材和音乐，点「分析素材并生成方案」</h2>
            <p>全程在你的电脑上运行，素材不会上传。</p>
          </div>
        )}

        {plan && (
          <div className="editor">
            <div className="toolbar">
              <h2>剪辑方案 <small>{plan.shots.length} 个镜头 · {plan.duration}s · {plan.aspect} · 方案 #{plan.seed}</small></h2>
              <div className="actions">
                <button disabled={busy} onClick={rerollPlan} title="同样的素材和音乐，换一种剪法">换一版</button>
                <button disabled={busy} onClick={changeMusic} title="换一首歌，按新节拍重新排镜头（手动加的贴纸和手绘会尽量保留）">换歌…</button>
                <button className="primary" disabled={busy || !!video} onClick={render}>{video ? '已渲染' : '渲染成片'}</button>
                {video && <button className="primary" onClick={exportVideo}>导出…</button>}
                {video && <button onClick={() => vc.showInFolder(video.video)}>打开所在文件夹</button>}
              </div>
            </div>

            <div className="stage">
              <div className="viewer">
                <div className="preview" style={{ aspectRatio: `${aw} / ${ah}`, '--ar': aw / ah }}>
                  {shown ? (
                    <video key={shown.video} ref={videoRef} src={mediaUrl(shown.video)} controls
                           onSeeked={(e) => !playing && setTime(e.currentTarget.currentTime)}
                           onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                           onLoadedMetadata={(e) => { e.currentTarget.currentTime = Math.min(time, plan.duration - 0.05) }} />
                  ) : (
                    <div className="ph">{busy ? (job?.preview ? '正在生成预览…' : job?.kind === 'render' ? '渲染中…' : '处理中…') : '预览会显示在这里'}</div>
                  )}
                  {shown && !video && <span className="badge">预览 · 低清{plan.manual ? '，手动加的效果已包含' : '，自动贴纸 / 手绘在成片里才有'}</span>}
                </div>
                <p className="viewer-note">{video ? `${fileName(video.video)} · ${video.duration}s` : '满意的话点「渲染成片」出高清版，带全部贴纸和手绘。'}</p>
              </div>

              <div className="side-pane">
                <div className="tabs">
                  <button className={tab === 'shot' ? 'on' : ''} onClick={() => setTab('shot')}>镜头</button>
                  <button className={tab === 'list' ? 'on' : ''} onClick={() => setTab('list')}>列表</button>
                </div>
                {tab === 'shot' && (selected != null && plan.shots[selected] ? (
                  <Inspector plan={plan} index={selected} looks={looks} usableClips={usableClips} time={time} busy={busy}
                             onSwap={swapShot} onApply={applyShot} onCheck={checkShot} getStill={getStill}
                             stickerName={(x) => stickerLabel(x, looks)} onClose={() => setSelected(null)} />
                ) : (
                  <div className="empty-pane">
                    <p>在下面的时间线上点一个镜头，就能在这里：</p>
                    <ul><li>换素材</li><li>在画面上点一下放贴纸（跟着走 / 固定）</li><li>把这个镜头改成手绘</li></ul>
                  </div>
                ))}
                {tab === 'list' && (
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>#</th><th>时间</th><th>类型</th><th>素材</th><th>特效</th></tr></thead>
                      <tbody>
                        {plan.shots.map((s, i) => (
                          <tr key={i} className={selected === i ? 'sel' : ''} onClick={() => selectShot(i)}>
                            <td>{i + 1}</td>
                            <td>{s.t_start.toFixed(2)}–{s.t_end.toFixed(2)}</td>
                            <td><i className="chip" style={{ background: ROLES[s.role]?.[1] }} />{ROLES[s.role]?.[0] || s.role}</td>
                            <td>{s.clip}</td>
                            <td>{s.sticker ? stickerLabel(s.sticker, looks) : s.drawn ? `手绘 ${s.drawn.label}` : ''}{(s.sticker_at || s.drawn?.manual) ? ' ✎' : ''}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            <Timeline plan={plan} thumbs={thumbs} wave={wave} time={time} onSeek={seek} selected={selected}
                      onSelect={selectShot} stickerName={(x) => stickerLabel(x, looks)}
                      styleName={(id) => looks.drawn_styles.find((d) => d.id === (id || 'lineart'))?.name || '线稿上色'} />
          </div>
        )}
      </main>
    </div>
  )
}
