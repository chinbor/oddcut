import React, { useCallback, useEffect, useRef, useState } from 'react'

const vc = window.oddcut

const STAGES = {
  analyze: [['clips', 0.45, '分析素材画面'], ['music', 0.1, '分析音乐节拍'],
            ['stickers', 0.3, '识别可加贴纸的物体'], ['drawn', 0.15, '寻找手绘开场镜头']],
  plan: [['cuts', 1, '生成剪辑方案']],
  render: [['effects', 0.45, '渲染贴纸和手绘'], ['shots', 0.5, '渲染镜头'], ['mux', 0.05, '合成音乐']],
}
const ROLES = {
  hook: ['开场', '#7aa2ff'], drawn: ['手绘开场', '#ff9f6b'], body: ['快切', '#5c6378'],
  breath: ['呼吸镜头', '#5fc9a8'], sticker: ['贴纸', '#ffd166'], outro: ['结尾', '#b18cff'],
}
const STICKERS = { eyes: '眼睛', anger: '💢', question: '问号' }
const ASPECT_NAMES = { '9:16': '竖屏 9:16', '3:4': '3:4', '4:5': '4:5', '1:1': '方形 1:1', '16:9': '横屏 16:9' }

// overall 0..1 for a job: stages run in order, so a later stage means the earlier ones are finished
function jobFraction(job) {
  if (!job) return 0
  if (job.status === 'done') return 1
  const stages = STAGES[job.kind] || []
  const lastSeen = Math.max(-1, ...stages.map(([s], i) => (job.stages[s] ? i : -1)))
  let sum = 0
  stages.forEach(([s, w], i) => {
    if (i < lastSeen) sum += w
    else if (job.stages[s]) sum += w * job.stages[s].fraction
  })
  return Math.min(1, sum / stages.reduce((a, [, w]) => a + w, 0))
}

function stageLabel(job) {
  const stages = STAGES[job?.kind] || []
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
                                     stickers: true, drawn: true })
  const [job, setJob] = useState(null)          // the running / last job, as the backend reports it
  const [analysis, setAnalysis] = useState(null)
  const [plan, setPlan] = useState(null)
  const [video, setVideo] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const cancelRef = useRef(null)
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
    }
    vc.onBackendReady(onReady)
    vc.onBackendExit((msg) => setBackend({ ready: false, error: msg }))
    vc.backendStatus().then((s) => (s.ready || s.error) && onReady(s))
  }, [])

  const runJob = useCallback(async (body) => {
    const { id } = await vc.api('POST', '/api/jobs', body)
    cancelRef.current = () => vc.api('POST', `/api/jobs/${id}/cancel`)
    for (;;) {
      const j = await vc.api('GET', `/api/jobs/${id}`)
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

  const makePlan = guard(async () => {
    setPlan(null)
    setVideo(null)
    const a = await runJob({ kind: 'analyze', clips: form.clips, music: form.music, template: form.template,
                             aspect: form.aspect, duration: Number(form.duration),
                             stickers: form.stickers, drawn: form.drawn })
    setAnalysis(a)
    const rejected = a.clips.filter((c) => c.rejected)
    if (rejected.length) setNotice(`${rejected.length} 段素材没被选用：${rejected.map((c) => `${c.name}（${c.reason}）`).join('，')}`)
    setPlan(await runJob({ kind: 'plan', analysis: a.analysis }))
  })

  const rerollPlan = guard(async () => {
    setVideo(null)
    setPlan(await runJob({ kind: 'plan', analysis: analysis.analysis }))
  })

  const render = guard(async () => {
    setVideo(await runJob({ kind: 'render', plan: plan.plan }))
  })

  const useExamples = async () => {
    const ex = await vc.examples()
    setForm((f) => ({ ...f, clips: ex.clips, music: ex.music }))
  }

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
          <label className="row"><input type="checkbox" checked={form.stickers} onChange={(e) => set('stickers')(e.target.checked)} />贴纸（眼睛、💢、问号）</label>
          <label className="row"><input type="checkbox" checked={form.drawn} onChange={(e) => set('drawn')(e.target.checked)} />手绘开场</label>
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
          <div className="result">
            <div className="plan">
              <div className="plan-head">
                <h2>剪辑方案 <small>{plan.shots.length} 个镜头 · {plan.duration}s · {plan.aspect} · 方案 #{plan.seed}</small></h2>
                <div className="actions">
                  <button disabled={busy} onClick={rerollPlan}>换一版</button>
                  <button className="primary" disabled={busy} onClick={render}>{video ? '重新渲染' : '渲染成片'}</button>
                </div>
              </div>
              <div className="timeline">
                {plan.shots.map((s, i) => (
                  <i key={i} title={`${ROLES[s.role]?.[0] || s.role} · ${s.clip}`}
                     style={{ flexGrow: s.t_end - s.t_start, background: ROLES[s.role]?.[1] }} />
                ))}
              </div>
              <div className="legend">
                {Object.entries(ROLES).filter(([r]) => plan.shots.some((s) => s.role === r)).map(([r, [n, c]]) => (
                  <span key={r}><i style={{ background: c }} />{n}</span>
                ))}
              </div>
              <table>
                <thead><tr><th>时间</th><th>类型</th><th>素材</th><th>从第几秒</th><th>特效</th></tr></thead>
                <tbody>
                  {plan.shots.map((s, i) => (
                    <tr key={i}>
                      <td>{s.t_start.toFixed(2)}–{s.t_end.toFixed(2)}</td>
                      <td><i className="chip" style={{ background: ROLES[s.role]?.[1] }} />{ROLES[s.role]?.[0] || s.role}</td>
                      <td>{s.clip}</td>
                      <td>{s.src_start.toFixed(2)}s</td>
                      <td>{s.sticker ? STICKERS[s.sticker] || s.sticker : s.drawn ? `手绘 ${s.drawn.label}` : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="right">
            <div className="preview" style={{ aspectRatio: `${aw} / ${ah}` }}>
              {video ? (
                <video key={video.video} src={mediaUrl(video.video)} controls autoPlay />
              ) : (
                <div className="ph">{busy && job?.kind === 'render' ? '渲染中…' : '渲染后在这里预览'}</div>
              )}
            </div>
            {video && (
              <div className="video-actions">
                <span>{fileName(video.video)} · {video.duration}s</span>
                <button className="primary" onClick={exportVideo}>导出…</button>
                <button onClick={() => vc.showInFolder(video.video)}>打开所在文件夹</button>
              </div>
            )}
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
