// Small helpers for talking to the backend (window.oddcut, set up by preload.cjs) and the progress of its jobs.

export const vc = window.oddcut
export const q = (s) => encodeURIComponent(s)
export const mediaUrl = (p) => `media://v/?p=${encodeURIComponent(p)}`
export const fileName = (p) => (p ? p.split(/[\\/]/).pop() : '')
export const errText = (e) => String(e?.message || e).replace(/^Error invoking remote method 'api': Error: /, '')

// what a blocking job is doing, for the progress pill / the 新建 button
const STAGES = {
  analyze: [['clips', 0.45, '分析素材画面'], ['music', 0.1, '分析音乐节拍'], ['stickers', 0.3, '找能加贴纸的东西'], ['drawn', 0.15, '找适合手绘的镜头']],
  plan: [['cuts', 1, '排镜头']], music: [['music', 1, '分析新音乐']], edit: [['edit', 1, '换素材']], shot: [['edit', 1, '应用修改']],
  cut: [['edit', 1, '调整切点']], retime: [['edit', 1, '对齐新节拍']], check: [['check', 1, '看这个镜头能不能画']],
  render: [['effects', 0.5, '渲染贴纸和手绘'], ['shots', 0.45, '渲染镜头'], ['mux', 0.05, '合成音乐']],
  grow_auto: [['score', 1, '挑适合万物生长的镜头']],
}
export function progressOf(job) {
  if (!job) return null
  const st = STAGES[job.kind] || []
  let label = st[0]?.[2] || '处理中'
  let sum = 0, all = 0
  st.forEach(([s, w, l]) => { all += w; const e = job.stages[s]; if (e) { sum += w * e.fraction; label = l + (e.total > 1 ? ` ${e.done}/${e.total}` : '') } })
  return { label, fraction: job.status === 'done' ? 1 : all ? sum / all : 0 }
}
