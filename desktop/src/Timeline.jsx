import React, { useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import Icon from './icons.jsx'

// Bottom timeline: ruler + playhead, picture / sticker / drawn / music tracks, beat lines through all of them.
// Wheel zooms around the pointer (Shift + wheel or a touchpad scrolls sideways). Click a clip to select it and
// play just that piece, drag it sideways to put it somewhere else (Esc cancels); click the ruler or empty space
// to go back to the whole video; drag the line between clips to move a cut, which jumps onto the nearest beat while
// 切点对齐节拍 is on (Alt = the other way round); drag a sticker bar's ends or its keyframe diamonds.

const LABEL_W = 92

// 万物生长: when each layer starts and when the picture is complete (same formula as growth.timing)
export function growTiming(length, beat, layers = 3, stagger = 'beat') {
  let step = stagger === 'beat' ? beat : beat / 2
  let rise = Math.min(0.45, Math.max(0.25, 0.9 * step))
  const avail = Math.max(0.3, length - 0.15)
  if ((layers - 1) * step + rise > avail) {
    step = Math.max(0.08, (avail - rise) / Math.max(1, layers - 1))
    if ((layers - 1) * step + rise > avail) rise = Math.max(0.15, avail - (layers - 1) * step)
  }
  const starts = Array.from({ length: layers }, (_, k) => k * step)
  return { starts, rise, land: starts[layers - 1] + rise }
}
const fmtS = (t) => (Number.isInteger(t) ? `${t}s` : `${t.toFixed(1)}s`)

export default function Timeline({ plan, thumbs, strips, onPlayShot, onMoveShot, aspect = 0.5625, wave, time, onSeek, selected, onSelect, stickerName, styleName,
                                   draft, onInOut, onCutMove, busy, pending, live, looks, keySel, onKeySelect, onKeyTime,
                                   metronome, setMetronome, overlays = [], ovSel, onSelectOverlay, onOverlayDraft, onOverlayCommit,
                                   onDropClip, onDropGrid, clipThumbs = {}, clipInfo, onTrim, onShotToPip, onCopyItem, canPaste,
                                   onPasteAt, onDeleteOverlay }) {
  const scroller = useRef(null)
  const canvas = useRef(null)
  const [width, setWidth] = useState(800)
  const [zoom, setZoom] = useState(1)              // 1 = whole edit fits
  const [zoomNote, setZoomNote] = useState(null)
  const [snap, setSnap] = useState(true)
  const [cutDrag, setCutDrag] = useState(null)
  const [shotDrag, setShotDrag] = useState(null)   // a clip being dragged to a new place: {src, dx, dst}
  const [trimDrag, setTrimDrag] = useState(null)   // a main-track clip's edge being dragged: {i, side, d}
  const [pipGhost, setPipGhost] = useState(null)   // a main-track clip dragged over a 画中画 track: {k, t, len}
  const [menu, setMenu] = useState(null)           // right-click menu: {x, y, item: {shot} | {ov}, t, k}
  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    window.addEventListener('pointerdown', close)
    return () => window.removeEventListener('pointerdown', close)
  }, [menu])
  const zoomTimer = useRef(null)
  const zoomRef = useRef(1)                          // latest zoom, for wheel events that arrive faster than renders
  const duration = plan.duration
  const fps = plan.fps || 30

  useEffect(() => {
    const el = scroller.current
    const ro = new ResizeObserver(() => setWidth(el.clientWidth))
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])
  const fit = Math.max(16, (width - LABEL_W - 28) / Math.max(1, duration))
  const scale = fit * zoom
  const x = (t) => LABEL_W + t * scale

  // zoom around a point on screen (the pointer for the wheel, the playhead for the buttons)
  const zoomTo = (z, clientX = null) => {
    const el = scroller.current
    const r = el.getBoundingClientRect()
    const px = clientX == null ? x(time) - el.scrollLeft : clientX - r.left
    const t = (px + el.scrollLeft - LABEL_W) / (fit * zoomRef.current)
    const nz = Math.min(16, Math.max(1, z))
    zoomRef.current = nz
    setZoom(nz)
    requestAnimationFrame(() => { el.scrollLeft = LABEL_W + t * fit * nz - px })
    setZoomNote(nz <= 1.001 ? '适应宽度' : `${nz.toFixed(1)}×`)
    clearTimeout(zoomTimer.current)
    zoomTimer.current = setTimeout(() => setZoomNote(null), 900)
  }
  useEffect(() => {
    const el = scroller.current
    const onWheel = (e) => {
      if (e.shiftKey && !e.ctrlKey) { e.preventDefault(); el.scrollLeft += e.deltaY || e.deltaX; return }
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return            // touchpad sideways: native scroll
      e.preventDefault()
      zoomTo(zoomRef.current * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })
  useEffect(() => {                                // keep the playhead in view
    const el = scroller.current
    const px = x(time)
    if (px < el.scrollLeft + LABEL_W || px > el.scrollLeft + el.clientWidth - 24) el.scrollLeft = px - LABEL_W - 60
  }, [time])

  // music: muted waveform, beat ticks along the top (downbeats orange and taller)
  const g = wave?.grid || {}
  useEffect(() => {
    const c = canvas.current
    if (!c || !wave) return
    const w = Math.ceil(duration * scale), h = 56
    const dpr = window.devicePixelRatio || 1
    c.width = w * dpr; c.height = h * dpr
    c.style.width = `${w}px`; c.style.height = `${h}px`
    const ctx = c.getContext('2d')
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = '#4A505C'
    wave.peaks.forEach((p, i) => {
      const ph = Math.max(1, p * 20)
      ctx.fillRect(i * wave.step * scale, 32 - ph, Math.max(1, wave.step * scale - 0.6), ph * 2)
    })
    ;(g.t || []).forEach((bt, i) => {
      if (!g.on_beat?.[i]) return
      const down = g.downbeat?.[i]
      ctx.fillStyle = down ? '#FF8A5C' : '#8A91A0'
      ctx.fillRect(bt * scale - (down ? 1 : 0.5), 0, down ? 2 : 1, down ? 10 : 5)
    })
  }, [wave, scale, duration])

  const ticks = useMemo(() => {
    const every = scale > 160 ? 0.5 : scale > 70 ? 1 : scale > 30 ? 2 : 5
    const out = []
    for (let t = 0; t <= duration + 1e-6; t += every) out.push(Math.round(t * 100) / 100)
    return out
  }, [scale, duration])

  // beats as a list: {t, no, down, bar, half}
  const beats = useMemo(() => (g.t || []).map((t, i) => ({ t, no: g.beat_no?.[i] || 0, down: !!g.downbeat?.[i], bar: g.bar?.[i] || 0,
                                                           half: !g.on_beat?.[i] })), [wave])
  const nearestBeat = (t) => beats.reduce((b, v) => (!b || Math.abs(v.t - t) < Math.abs(b.t - t) ? v : b), null)
  const beatLabel = (b) => (b.half ? '对齐半拍' : `对齐第 ${b.no} 拍${b.down ? ' · 重拍' : ''}`)

  const tAt = (clientX) => {
    const el = scroller.current
    const r = el.getBoundingClientRect()
    return (clientX - r.left + el.scrollLeft - LABEL_W) / scale
  }
  const scrub = (e, deselect = false) => {
    e.preventDefault()
    if (deselect) { onSelect(null); onSelectOverlay(null) }
    const go = (ev) => onSeek(Math.min(duration - 0.01, Math.max(0, tAt(ev.clientX))))
    go(e)
    const up = () => { window.removeEventListener('pointermove', go); window.removeEventListener('pointerup', up) }
    window.addEventListener('pointermove', go)
    window.addEventListener('pointerup', up)
  }
  const pick = (i, e) => {
    e.stopPropagation()
    if (selected === i) return onSelect(null)
    onSelect(i)
    const s = plan.shots[i]
    if (time < s.t_start || time >= s.t_end) onSeek(s.t_start + 0.01)
  }
  // picture clips: click = select + play that piece; drag sideways = move it (the others make room)
  const clipDown = (e, i) => {
    if (e.button !== 0) return
    e.preventDefault(); e.stopPropagation()
    const x0 = e.clientX, y0 = e.clientY
    const centers = plan.shots.map((s) => (s.t_start + s.t_end) / 2)
    const s0 = plan.shots[i]
    const grab = tAt(e.clientX) - s0.t_start
    let st = null
    const mv = (ev) => {
      const dx = ev.clientX - x0
      if ((!st && Math.abs(dx) < 5 && Math.abs(ev.clientY - y0) < 5) || busy) return
      const lane = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('.track.pip')
      if (lane) {
        const t = Math.max(0, Math.min(duration - 0.3, tAt(ev.clientX) - grab))
        st = { src: i, pip: Number(lane.dataset.k), t }
        setShotDrag(null)
        setPipGhost({ k: st.pip, t, len: s0.t_end - s0.t_start })
        return
      }
      setPipGhost(null)
      const tp = tAt(ev.clientX)
      st = { src: i, dx, dst: centers.filter((c, j) => j !== i && c < tp).length }
      setShotDrag(st)
    }
    const end = (keep) => {
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
      window.removeEventListener('keydown', esc, true)
      setShotDrag(null); setPipGhost(null)
      if (!st) { onSelect(i); onPlayShot(i) }
      else if (keep && st.pip != null) onShotToPip(i, st.pip, st.t)
      else if (keep && st.dst != null && st.dst !== i) onMoveShot(i, st.dst)
    }
    const up = () => end(true)
    const esc = (ev) => { if (ev.key === 'Escape') { ev.stopImmediatePropagation(); st = st || { src: i, dst: i }; end(false) } }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
    window.addEventListener('keydown', esc, true)
  }
  // while dragging: the dragged clip follows the pointer, the ones it passes slide over by its width
  const dragW = shotDrag ? (plan.shots[shotDrag.src].t_end - plan.shots[shotDrag.src].t_start) * scale : 0
  const offset = (j) => {
    if (!shotDrag) return 0
    const { src, dst, dx } = shotDrag
    if (j === src) return dx
    if (src < dst && j > src && j <= dst) return -dragW
    if (dst < src && j >= dst && j < src) return dragW
    return 0
  }
  const shift = (j) => (shotDrag ? { transform: `translateX(${offset(j)}px)` } : undefined)
  const landAt = shotDrag ? (shotDrag.dst > shotDrag.src ? x(plan.shots[shotDrag.dst].t_end) - dragW
                                                           : x(plan.shots[shotDrag.dst].t_start)) : 0
  const filmstrip = (i, w) => {
    const strip = strips?.[i]
    if (!strip) return thumbs?.[i] ? [thumbs[i]] : []
    const n = Math.min(4, Math.max(1, Math.round(w / (54 * aspect))))
    return ({ 1: [thumbs?.[i] || strip[0]], 2: [strip[0], strip[2]], 3: [strip[0], strip[1], strip[3]], 4: strip })[n]
  }
  const startCut = (e, k) => {
    if (busy) return
    e.preventDefault(); e.stopPropagation()
    const lo = plan.shots[k - 1].t_start + 0.4, hi = plan.shots[k].t_end - 0.4
    const inRange = beats.filter((b) => b.t >= lo && b.t <= hi)
    let st = null
    const at = (ev) => {
      const raw = Math.min(hi, Math.max(lo, tAt(ev.clientX)))
      const useSnap = snap !== ev.altKey
      let t = raw, beat = null
      if (useSnap && inRange.length) {
        beat = inRange.reduce((b, v) => (Math.abs(v.t - raw) < Math.abs(b.t - raw) ? v : b), inRange[0])
        t = beat.t
      }
      st = { k, t, raw, beat, useSnap }
      setCutDrag(st)
    }
    at(e)
    const up = () => {
      window.removeEventListener('pointermove', at); window.removeEventListener('pointerup', up)
      setCutDrag(null)
      if (st && Math.abs(st.t - plan.shots[k].t_start) > 0.005) onCutMove(k, st.t, st.useSnap)
    }
    window.addEventListener('pointermove', at); window.addEventListener('pointerup', up)
  }
  const edge = (e, i, which) => {
    e.preventDefault(); e.stopPropagation()
    const s = plan.shots[i]
    const len = s.t_end - s.t_start
    let v
    let moved = false
    const mv = (ev) => {
      const t = Math.min(len, Math.max(0, tAt(ev.clientX) - s.t_start))
      v = which === 'in' ? Math.min(t, (draft.out ?? len) - 0.2) : (t >= len - 0.02 ? null : Math.max(t, (draft.in || 0) + 0.2))
      moved = true
      onInOut(which, v, false)
    }
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) onInOut(which, v, true) }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }
  // 修剪: drag a main-track clip's edge; it gets shorter / longer and the clips after it follow (as in 剪映)
  const trimDown = (e, i, side) => {
    if (e.button !== 0 || busy) return
    e.preventDefault(); e.stopPropagation()
    const s = plan.shots[i]
    const len = s.t_end - s.t_start
    const clipLen = clipInfo?.(s.clip_path)?.duration ?? 1e9
    const minLen = 0.4
    let d = 0, t = null
    const mv = (ev) => {
      if (side === 'out') {
        let end = Math.max(s.t_start + minLen, Math.min(tAt(ev.clientX), s.t_start + clipLen - s.src_start - 0.05))
        end = snapT(end, ev, s.t_start + minLen, s.t_start + clipLen - s.src_start - 0.05)
        d = end - s.t_end
        t = end
      } else {
        const cut = Math.max(-s.src_start, Math.min(tAt(ev.clientX) - s.t_start, len - minLen))
        d = -cut
        t = s.t_start + cut
      }
      setTrimDrag({ i, side, d })
    }
    const up = () => {
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
      setTrimDrag(null)
      if (t != null && Math.abs(d) > 0.5 / fps) onTrim(i, side, t, false)
    }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }
  const trimShift = (j) => (trimDrag && j > trimDrag.i ? trimDrag.d * scale : 0)
  const openMenu = (e, item, k = null) => {
    e.preventDefault(); e.stopPropagation()
    if (item?.shot != null) onSelect(item.shot)
    if (item?.ov != null) onSelectOverlay(item.ov)
    setMenu({ x: e.clientX, y: e.clientY, item, t: Math.max(0, tAt(e.clientX)), k })
  }

  // keyframe diamonds: click = select + jump there; drag = move in time (whole frames, between its neighbours)
  const diamond = (e, i, j, k, keys) => {
    e.stopPropagation(); e.preventDefault()
    const s = plan.shots[i]
    if (selected !== i) onSelect(i)
    onKeySelect(j)
    onSeek(s.t_start + k.t + 0.001)
    if (!draft || selected !== i) return
    const lo = j > 0 ? keys[j - 1].t + 1 / fps : 0
    const hi = j < keys.length - 1 ? keys[j + 1].t - 1 / fps : s.t_end - s.t_start - 1 / fps
    let moved = false, tt = k.t
    const mv = (ev) => {
      tt = Math.round(Math.min(hi, Math.max(lo, tAt(ev.clientX) - s.t_start)) * fps) / fps
      moved = true
      onKeyTime(j, tt, false)
      onSeek(s.t_start + tt + 0.001)
    }
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) onKeyTime(j, tt, true) }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }

  // picture-in-picture lanes: drag a clip in time and between lanes, its ends to trim; beats pull like cuts do
  const LANE_H = 32
  // magnetic: a beat pulls only when the pointer is within a few pixels of it, so the clip follows the mouse
  const snapT = (tt, ev, lo = 0, hi = duration) => {
    if (snap === ev.altKey || !beats.length) return tt
    const b = beats.reduce((m, v) => (Math.abs(v.t - tt) < Math.abs(m.t - tt) ? v : m), beats[0])
    return b.t >= lo && b.t <= hi && Math.abs(b.t - tt) * scale < 8 ? b.t : tt
  }
  // the bar itself moves while dragging (once per frame); the edit is handed over when let go
  const ovDrag = (e, o, what) => {
    if (e.button !== 0) return                  // allowed while an edit runs: the drop waits for it
    e.preventDefault(); e.stopPropagation()
    onSelectOverlay(o.id)
    const bar = what === 'body' ? e.currentTarget : e.currentTarget.parentElement
    const x0 = e.clientX, y0 = e.clientY
    const len = o.t_end - o.t_start
    const clipLen = clipInfo?.(o.clip)?.duration ?? (o.src_start + len)
    let patch = null, last = null, id = 0
    const css = bar.style.cssText
    bar.classList.add('dragging')
    const step = () => {
      id = 0
      const ev = last
      if (what === 'body') {
        let t0 = Math.min(duration - len, Math.max(0, o.t_start + (ev.clientX - x0) / scale))
        t0 = snapT(t0, ev, 0, duration - len)
        const track = Math.min(2, Math.max(0, o.track - Math.round((ev.clientY - y0) / LANE_H)))
        patch = { t_start: t0, t_end: t0 + len, track }
        bar.style.transform = `translate(${(t0 - o.t_start) * scale}px, ${(o.track - track) * LANE_H}px)`
      } else if (what === 'in') {
        const lo = Math.max(0, o.t_start - o.src_start), hi = o.t_end - 0.3
        const t0 = snapT(Math.min(hi, Math.max(lo, tAt(ev.clientX))), ev, lo, hi)
        patch = { t_start: t0, src_start: o.src_start + (t0 - o.t_start) }
        bar.style.left = `${x(t0) + 1}px`; bar.style.width = `${Math.max(8, (o.t_end - t0) * scale - 2)}px`
      } else {
        const lo = o.t_start + 0.3, hi = Math.min(duration, o.t_start + clipLen - o.src_start)
        const t1 = snapT(Math.min(hi, Math.max(lo, tAt(ev.clientX))), ev, lo, hi)
        patch = { t_end: t1 }
        bar.style.width = `${Math.max(8, (t1 - o.t_start) * scale - 2)}px`
      }
    }
    const mv = (ev) => { last = ev; if (!id) id = requestAnimationFrame(step) }
    const up = () => {
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
      if (id) { cancelAnimationFrame(id); step() }
      bar.classList.remove('dragging')
      bar.style.cssText = css                    // React takes over again from here
      if (patch) { flushSync(() => onOverlayDraft(o.id, patch)); onOverlayCommit(o.id) }
    }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }
  // an overlay's keyframe diamond: click = jump there; drag = move it in time (whole frames, between its neighbours)
  const ovKey = (e, o, j) => {
    e.preventDefault(); e.stopPropagation()
    onSelectOverlay(o.id)
    const keys = o.keys
    onSeek(o.t_start + keys[j].t + 0.001)
    const el = e.currentTarget
    const lo = j > 0 ? keys[j - 1].t + 1 / fps : 0
    const hi = j < keys.length - 1 ? keys[j + 1].t - 1 / fps : o.t_end - o.t_start
    const css = el.style.cssText
    let tt = null
    const mv = (ev) => {
      tt = Math.round(Math.min(hi, Math.max(lo, tAt(ev.clientX) - o.t_start)) * fps) / fps
      el.style.left = `${tt * scale - 5}px`
    }
    const up = () => {
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
      el.style.cssText = css
      if (tt != null && tt !== keys[j].t) {
        flushSync(() => onOverlayDraft(o.id, { keys: keys.map((k, i) => (i === j ? { ...k, t: tt } : k)) }))
        onOverlayCommit(o.id); onSeek(o.t_start + tt + 0.001)
      }
    }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }
  const beatLen = plan.beat || 60 / (wave?.tempo || 120)
  const lanes = Math.min(6, Math.max(-1, ...overlays.map((o) => o.track)) + 2)
  const laneDrop = (k) => ({
    onDragOver: (e) => { if (['application/x-oddcut-clip', 'application/x-oddcut-grid'].some((ty) => e.dataTransfer.types.includes(ty))) { e.preventDefault(); e.currentTarget.classList.add('drop') } },
    onDragLeave: (e) => e.currentTarget.classList.remove('drop'),
    onDrop: (e) => {
      e.currentTarget.classList.remove('drop')
      const path = e.dataTransfer.getData('application/x-oddcut-clip')
      const layout = e.dataTransfer.getData('application/x-oddcut-grid')
      if (path) { e.preventDefault(); onDropClip(path, k, Math.max(0, tAt(e.clientX))) }
      else if (layout) { e.preventDefault(); onDropGrid(layout, k, Math.max(0, tAt(e.clientX))) }
    },
  })

  const cutState = (t) => {
    const b = nearestBeat(t)
    if (b && Math.abs(b.t - t) < 0.6 / fps) return { on: true, text: b.half ? '踩在半拍上' : `踩在第 ${b.no} 拍${b.down ? '（重拍）' : ''}` }
    return { on: false, text: '这个切点不在节拍上' }
  }
  const near = cutDrag ? cutDrag.raw : null

  return (
    <section className="timeline">
      <div className="tl-toolbar">
        <button className={`toggle ${snap ? 'on' : ''}`} onClick={() => setSnap((v) => !v)} aria-pressed={snap}
                title="拖动镜头切点时，自动吸到最近的节拍上；按住 Alt 临时自由拖">
          <Icon name="magnet" size={18} />切点对齐节拍
        </button>
        <button className={`toggle ${metronome ? 'on' : ''}`} onClick={() => setMetronome((v) => !v)} aria-pressed={metronome}
                title="播放时在每一拍上响一声（重拍音更高），帮你听出节奏">
          <Icon name="music" size={18} />播放时听节拍
        </button>
        <span className="legend"><i className="lg-on" />踩点的切点<i className="lg-off" />没踩点</span>
        <span className="spacer" />
        {live && live.total > live.done && (
          <span className="live-note"><i className="spin" />正在把贴纸、手绘和转场加进预览（{live.done} / {live.total}）</span>
        )}
        <div className="zoom-btns" title="滚轮缩放（Shift + 滚轮左右移动）">
          <button className="icon" onClick={() => zoomTo(zoom / 1.5)} disabled={zoom <= 1} aria-label="缩小">−</button>
          <button className="icon" onClick={() => zoomTo(zoom * 1.5)} disabled={zoom >= 16} aria-label="放大">+</button>
          <button className="fit" onClick={() => zoomTo(1)} disabled={zoom <= 1}>适应</button>
        </div>
      </div>
      <div className="tl-scroll" ref={scroller}
           onPointerDown={(e) => { if (e.target.classList.contains('track') || e.target.classList.contains('tl-inner')) { onSelect(null); onSelectOverlay(null) } }}>
        <div className="tl-inner" style={{ width: LABEL_W + duration * scale + (zoom > 1 ? 20 : 8) }}>
          <div className="tl-ruler" onPointerDown={(e) => scrub(e, true)} title="点这里回到整段播放">
            <span className="lab-pad" />
            {ticks.map((t) => <span key={t} className="tick" style={{ left: x(t) }}>{fmtS(t)}</span>)}
            {beats.filter((b) => b.down && b.bar > 0).map((b) => (
              <span key={`bar${b.t}`} className="bar-no" style={{ left: x(b.t) }} title={`第 ${b.bar} 小节的第一拍（重拍）`}>{b.bar}</span>
            ))}
          </div>

          <div className="beat-lines" style={{ left: 0 }}>
            {beats.map((b) => {
              const hot = near != null && Math.abs(b.t - near) < 1.2
              const target = cutDrag?.beat && cutDrag.beat.t === b.t
              return <i key={b.t} className={`bl ${b.down ? 'down' : b.half ? 'half' : ''} ${hot && snap ? 'hot' : ''} ${target ? 'target' : ''} ${cutDrag && !cutDrag.useSnap ? 'dim' : ''}`}
                        style={{ left: x(b.t) }} />
            })}
          </div>

          {Array.from({ length: lanes }, (_, n) => lanes - 1 - n).map((k) => {
            const mine = overlays.filter((o) => o.track === k)
            return (
              <div key={`pip${k}`} className="track pip" data-k={k} {...laneDrop(k)} onContextMenu={(e) => openMenu(e, null, k)}>
                <label><Icon name="film" size={14} />画中画 {k + 1}</label>
                {!mine.length && !pipGhost && <span className="lane-hint" style={{ left: LABEL_W + 10 }}>把下面「画面」里的一段拖上来，或者复制后粘贴，就成了画中画</span>}
                {pipGhost?.k === k && <div className="bar ov ghost" style={{ left: x(pipGhost.t) + 1, width: Math.max(8, Math.min(pipGhost.len, duration - pipGhost.t) * scale - 2) }}><span>放到这里</span></div>}
                {mine.map((o) => (
                  <div key={o.id} className={`bar ov ${ovSel === o.id ? 'sel' : ''} ${o.group ? 'grp' : ''}`}
                       style={{ left: x(o.t_start) + 1, width: Math.max(8, (o.t_end - o.t_start) * scale - 2) }}
                       onPointerDown={(e) => ovDrag(e, o, 'body')} onContextMenu={(e) => openMenu(e, { ov: o.id }, k)} title={`${o.name} · ${o.t_start.toFixed(2)} – ${o.t_end.toFixed(2)} 秒（拖动改时间/轨道，拖两端剪短）`}>
                    <i className="edge l" onPointerDown={(e) => ovDrag(e, o, 'in')} />
                    {o.anim_in && <i className="anim-wedge in" style={{ width: o.anim_in.beats * beatLen * scale }} title="入场动画" />}
                    {o.anim_out && <i className="anim-wedge out" style={{ width: o.anim_out.beats * beatLen * scale }} title="出场动画" />}
                    {o.grid ? <i className="bar-grid" /> : clipThumbs[o.clip] && <img src={clipThumbs[o.clip]} alt="" draggable={false} />}
                    <span>{o.fx?.kind === 'drawn' ? `手绘 · ${o.group?.label || ''}` : o.group ? `万物生长 ${o.group.layer}/${o.group.of} · ${o.group.label}` : o.name}</span>
                    {(o.fx?.keys || []).map((k, j) => (
                      <i key={`fx${j}`} className="diamond fx" style={{ left: k.t * scale - 5 }} title={`绘制进度 ${Math.round(k.v * 100)}% · ${k.t.toFixed(2)} 秒`}
                         onPointerDown={(e) => { e.stopPropagation(); e.preventDefault(); onSelectOverlay(o.id); onSeek(o.t_start + k.t + 0.001) }} />
                    ))}
                    {(o.keys || []).map((k, j) => (
                      <i key={j} className="diamond" style={{ left: k.t * scale - 5 }} title={`关键帧 · ${k.t.toFixed(2)} 秒（点一下跳过去，拖动改时间）`}
                         onPointerDown={(e) => ovKey(e, o, j)} />
                    ))}
                    <i className="edge r" onPointerDown={(e) => ovDrag(e, o, 'out')} />
                  </div>
                ))}
              </div>
            )
          })}

          <div className="track video">
            <label><Icon name="film" size={16} />画面</label>
            {plan.shots.map((s, i) => {
              const tw = trimDrag?.i === i ? trimDrag.d * scale : 0
              const w = Math.max(3, (s.t_end - s.t_start) * scale - 2 + tw)
              const tr = trimShift(i)
              return (
                <div key={i} className={`clip ${selected === i ? 'sel' : ''} ${pending.includes(i) ? 'pending' : ''} ${shotDrag?.src === i ? 'lifted' : ''}`}
                     style={{ left: x(s.t_start) + 1, width: w, ...(tr ? { transform: `translateX(${tr}px)` } : shift(i)) }}
                     onPointerDown={(e) => clipDown(e, i)} onContextMenu={(e) => openMenu(e, { shot: i })}
                     title={`镜头 ${i + 1} · ${s.clip} · ${(s.t_end - s.t_start).toFixed(1)} 秒（拖两边改长短，左右拖调换顺序，往上拖成画中画）`}>
                  {selected === i && !shotDrag && <i className="trim l" onPointerDown={(e) => trimDown(e, i, 'in')} title="拖动剪掉开头" />}
                  {selected === i && !shotDrag && <i className="trim r" onPointerDown={(e) => trimDown(e, i, 'out')} title="拖动改结尾（后面的片段跟着移动）" />}
                  <div className="strip">{filmstrip(i, w).map((src, k) => <img key={k} src={src} alt="" draggable={false} />)}</div>
                  <span className="num">{i + 1}</span>
                  {s.grow && !s.grow.off && (() => {
                    const g = growTiming(s.t_end - s.t_start, plan.beat || 60 / (wave?.tempo || 120), s.grow.layers, s.grow.stagger)
                    return (
                      <span className={`grow-mark ${s.grow.source === 'auto' ? 'auto' : ''}`} style={{ width: g.land * scale }}
                            title={`万物生长${s.grow.source === 'auto' ? `（自动：${s.grow.why || '画面合适'}）` : ''}：${s.grow.layers} 层，${s.grow.stagger === 'beat' ? '一拍' : '半拍'}一层，${g.land.toFixed(2)} 秒长完`}>
                        {g.starts.map((st, k) => <i key={k} style={{ left: st * scale }} />)}
                      </span>
                    )
                  })()}
                  {w > 54 && <span className="dur">{(s.t_end - s.t_start).toFixed(1)}s</span>}
                  {pending.includes(i) && <span className="gen">生成中</span>}
                </div>
              )
            })}
            {shotDrag && <i className="insert-mark" style={{ left: landAt }} />}
            {plan.shots.slice(1).map((s, j) => {
              const cs = cutState(s.t_start)
              return (
                <i key={`c${j}`} className={`cut ${busy || shotDrag || trimDrag || selected === j || selected === j + 1 ? 'off' : ''} ${cutDrag?.k === j + 1 ? 'dragging' : ''}`} style={{ left: x(s.t_start) - 5 }}
                   onPointerDown={(e) => startCut(e, j + 1)} title={`${cs.text}。拖动可调整切点`}>
                  <b className={`cut-dot ${cs.on ? 'on' : ''}`} />
                </i>
              )
            })}
          </div>

          <div className="track bars sticker">
            <label><Icon name="sticker" size={16} />贴纸</label>
            {plan.shots.map((s, i) => {
              const d = draft && selected === i ? draft : null
              const at = d || s.sticker_at
              if (!s.sticker && !d) return null
              const len = s.t_end - s.t_start
              const tIn = at?.in || 0
              const tOut = at?.out ?? len
              const id = (d?.id) || s.sticker.split('@')[0]
              const img = looks.stickers.find((x2) => x2.id === id)?.preview
              const keys = at?.keys || []
              return (
                <div key={i} className={`bar ${s.sticker_at || d ? 'manual' : 'auto'} ${selected === i ? 'sel' : ''}`}
                     style={{ left: x(s.t_start + tIn) + 1, width: Math.max(6, (tOut - tIn) * scale - 2), ...shift(i) }}
                     onPointerDown={(e) => pick(i, e)} title={stickerName(s.sticker || id)}>
                  {d && <i className="edge l" onPointerDown={(e) => edge(e, i, 'in')} title="拖动改出现时间" />}
                  {img && <img src={img} alt="" />}
                  <span>{stickerName(s.sticker || id)}{s.sticker_at ? ' ✎' : ''}</span>
                  {(at?.mode === 'still' ? keys.slice(0, 1) : keys).map((k, j) => (
                    <i key={j} className={`diamond ${selected === i && keySel === j ? 'sel' : ''}`} style={{ left: (k.t - tIn) * scale - 5 }}
                       title={`第 ${j + 1} 个关键帧 · ${k.t.toFixed(2)} 秒（拖动改时间）`} onPointerDown={(e) => diamond(e, i, j, k, keys)} />
                  ))}
                  {d && <i className="edge r" onPointerDown={(e) => edge(e, i, 'out')} title="拖动改消失时间" />}
                </div>
              )
            })}
          </div>

          <div className="track bars drawn">
            <label><Icon name="brush" size={16} />手绘</label>
            {plan.shots.map((s, i) => s.drawn && (
              <div key={i} className={`bar ${s.drawn.manual ? 'manual' : 'auto'} ${selected === i ? 'sel' : ''}`}
                   style={{ left: x(s.t_start) + 1, width: Math.max(6, (s.t_end - s.t_start) * scale - 2), ...shift(i) }}
                   onPointerDown={(e) => pick(i, e)} title={`手绘 · ${styleName(s.drawn.style)}`}>
                <span>{styleName(s.drawn.style)}{s.drawn.manual ? ' ✎' : ''}</span>
                <i className="reveal" style={{ left: s.drawn.reveal_at * scale }} title="在这里变回实拍（踩在重拍上）" />
              </div>
            ))}
          </div>

          <div className="track music">
            <label><Icon name="music" size={16} />音乐</label>
            <canvas ref={canvas} style={{ position: 'absolute', left: LABEL_W, top: 4 }} />
          </div>

          {cutDrag && (
            <div className={`cut-ghost ${cutDrag.beat ? 'snapped' : 'free'}`} style={{ left: x(cutDrag.t) }}>
              <span key={cutDrag.beat ? cutDrag.beat.t : 'free'}>
                {cutDrag.beat ? <><Icon name="magnet" size={12} />{beatLabel(cutDrag.beat)}</> : snap ? `自由位置 ${cutDrag.t.toFixed(2)}s` : `自由拖动 ${cutDrag.t.toFixed(2)}s`}
              </span>
            </div>
          )}
          <div className="playhead" style={{ left: x(time) }} onPointerDown={scrub} />
          {trimDrag && (
            <div className="cut-ghost free" style={{ left: x(plan.shots[trimDrag.i].t_end + trimDrag.d) }}>
              <span>{(plan.shots[trimDrag.i].t_end - plan.shots[trimDrag.i].t_start + trimDrag.d).toFixed(1)} 秒</span>
            </div>
          )}
          {zoomNote && <div className="zoom-note" style={{ left: (scroller.current?.scrollLeft || 0) + (width / 2) }}>{zoomNote}</div>}
        </div>
      </div>
      {menu && (
        <div className="ctx-menu" style={{ left: menu.x, top: menu.y }} onPointerDown={(e) => e.stopPropagation()}>
          {menu.item && <button onClick={() => { onCopyItem(menu.item); setMenu(null) }}>复制<kbd>Ctrl+C</kbd></button>}
          <button disabled={!canPaste} onClick={() => { onPasteAt(menu.t, menu.k); setMenu(null) }}>粘贴到这里<kbd>Ctrl+V</kbd></button>
          {menu.item?.ov != null && <button onClick={() => { onDeleteOverlay(menu.item.ov); setMenu(null) }}>删除<kbd>Delete</kbd></button>}
        </div>
      )}
    </section>
  )
}
