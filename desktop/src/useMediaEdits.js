// 素材 after the edit was made (as in 剪映): import, drop on the 主轨, delete a piece or the music, a song dropped
// on the music track, take a 素材 out of the project. Each one is one undo step.
import { useEffect, useState } from 'react'
import { vc } from './api.js'

export default function useMediaEdits({ guard, runJob, toast, commit, snapshot, setPast, setFuture, loadPlan, analysis, setAnalysis,
                                        plan, overlays, form, setForm, setSongs, setLibTab, selected, setSelected, ovSel, setOvSel }) {
  const [musicSel, setMusicSel] = useState(false)       // the music clip is selected (Delete removes it)
  useEffect(() => { if (selected != null || ovSel) setMusicSel(false) }, [selected, ovSel])
  const importMedia = guard(async (files) => {
    if (!files) files = await vc.pickMedia()
    if (!files?.length) return
    const before = snapshot()
    const a = await runJob({ kind: 'import', analysis: analysis.analysis, files })
    setPast((h) => [...h.slice(-49), before]); setFuture([])
    setAnalysis(a)
    if (a.songs?.length) setSongs((s) => [...new Set([...s, ...a.songs])])
    const added = a.clips.length - analysis.clips.length
    toast([added ? `导入了 ${added} 个素材：拖到主轨插入，拖到主轨上方成为画中画` : '', a.songs?.length ? `${a.songs.length} 首音乐在「音乐」里` : '', ...(a.notes || [])].filter(Boolean).join('；'))
    if (added) setLibTab({ tab: 'clips', n: Date.now() })
  })
  const insertClip = guard(async (path, t) => {
    commit(await runJob({ kind: 'insert', analysis: analysis.analysis, plan: plan.plan, t, clip: path }))
  })
  const deleteShot = guard(async (i) => {
    if (plan.shots.length < 2) { toast('主轨至少要留一段'); return }
    setSelected(null)
    commit(await runJob({ kind: 'delete', analysis: analysis.analysis, plan: plan.plan, index: i }))
  })
  const deleteMusic = guard(async () => {
    setMusicSel(false)
    commit(await runJob({ kind: 'delete', analysis: analysis.analysis, plan: plan.plan, music: true }))
    toast('音乐已删除。想换一首：把「音乐」里的歌拖到音乐轨道上')
  })
  // a song dropped on the music track: the edit stays exactly as it is, only the music changes (as in 剪映)
  const swapMusic = guard(async (music) => {
    const before = snapshot()
    const a = await runJob({ kind: 'music', analysis: analysis.analysis, music, duration: Number(form.duration) })
    const p = await runJob({ kind: 'swap_music', analysis: a.analysis, plan: plan.plan })
    setPast((h) => [...h.slice(-49), before]); setFuture([])
    setAnalysis(a)
    setForm((f) => ({ ...f, music }))
    loadPlan(p)
    toast(`音乐换成了 ${a.music.name}（${Math.round(a.music.tempo)} BPM），剪辑没有动`)
  })
  const removeClip = guard(async (path) => {
    const used = plan.shots.some((s) => s.clip_path === path) || overlays.some((o) => o.clip === path)
    if (used && !window.confirm('素材已被使用，同时删除时间线上的片段？')) return
    const before = snapshot()
    const r = await runJob({ kind: 'remove_clip', analysis: analysis.analysis, plan: plan.plan, clip: path, timeline: used })
    setPast((h) => [...h.slice(-49), before]); setFuture([])
    setAnalysis(r.analysis)
    setSelected(null); setOvSel(null)
    loadPlan(r.plan)
  })
  return { musicSel, setMusicSel, importMedia, insertClip, deleteShot, deleteMusic, swapMusic, removeClip }
}
