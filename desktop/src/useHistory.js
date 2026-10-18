// Undo / redo: every edit keeps the plan and analysis from before it (the last 50).
import { useState } from 'react'

export default function useHistory({ plan, analysis, selected, busy, setSelected, setAnalysis, loadPlan, toast }) {
  const [past, setPast] = useState([])
  const [future, setFuture] = useState([])
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
  return { past, future, setPast, setFuture, snapshot, commit, undo, redo }
}
