import React, { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import Icon from './icons.jsx'
import { gridCells, keyPose, maskPath, withKey } from './pip.js'

const fmt = (t) => {
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`
}

// Ramer-Douglas-Peucker on [t, x, y] samples (x, y in 0..1): keeps the corners of a recorded drag
function rdp(pts, eps) {
  if (pts.length < 3) return pts
  const [a, b] = [pts[0], pts[pts.length - 1]]
  let best = -1, idx = 0
  for (let i = 1; i < pts.length - 1; i++) {
    const p = pts[i]
    const u = (p[0] - a[0]) / Math.max(1e-6, b[0] - a[0])
    const d = Math.hypot(p[1] - (a[1] + (b[1] - a[1]) * u), p[2] - (a[2] + (b[2] - a[2]) * u))
    if (d > best) { best = d; idx = i }
  }
  if (best <= eps) return [a, b]
  return [...rdp(pts.slice(0, idx + 1), eps).slice(0, -1), ...rdp(pts.slice(idx), eps)]
}

// Centre of the editor: the preview (no native controls), our transport bar, the beat indicator, and direct
// manipulation of the selected shot's sticker: drag it to move, a corner to resize (a keyframe is added at the
// playhead if there isn't one), keyframe dots on its path (click = select + jump there, drag = move,
// right-click = menu), and 边播边拖 recording.
export default function Viewer({ src, videoRef, time, onTime, playing, setPlaying, duration, fps, aspect, shot,
                                 draft, track, stickerImage, showImage, autoSticker, keySel, onSelectKey, onDraft,
                                 onMoveKey, onCommit, onDeleteKey, onStillKey, onPlaceAuto, onDropSticker, recordArmed,
                                 onRecorded, boxMode, onBox, status, wave, segment, loop, setLoop, overlays = [], ovSel,
                                 onSelectOverlay, onOverlayDraft, onOverlayCommit, clipThumbs = {}, ovShowImage, cutTool, setCutTool,
                                 planPath, drawPick, onDrawPick }) {
  const boxRef = useRef(null)
  const [aw, ah] = aspect
  const keep = useRef({ t: 0, playing: false })
  const [hint, setHint] = useState(() => { try { return !localStorage.getItem('oddcut.hint.sticker2') } catch { return true } })
  const [boxDraft, setBoxDraft] = useState(null)
  const [menu, setMenu] = useState(null)          // right-click menu on a keyframe: {j, x, y}
  const [help, setHelp] = useState(false)
  const [rec, setRec] = useState(null)            // live recorded points while 边播边拖
  const [stroke, setStroke] = useState(null)        // a 抠像 brush stroke being painted: {op, pts}
  const [cutImg, setCutImg] = useState(null)       // the selected layer's 抠像 mask at the playhead (preview)
  const [fsz, setFsz] = useState([0, 0])            // preview frame size in px (for rounded overlay corners)
  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const ro = new ResizeObserver(() => { const r = el.getBoundingClientRect(); setFsz([r.width, r.height]) })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // ---- two stacked <video> layers: a new file loads behind, seeks to the same moment, then swaps in
  const [layers, setLayers] = useState([null, null])
  const [front, setFront] = useState(0)
  const vids = [useRef(null), useRef(null)]
  useEffect(() => {
    if (!src || layers[front] === src) return
    keep.current = { t: time, playing }
    const target = layers[front] ? 1 - front : front
    setLayers((l) => { const n = [...l]; n[target] = src; return n })
  }, [src])
  useEffect(() => { videoRef.current = vids[front].current })
  const onLoaded = (i) => (e) => {
    const v = e.currentTarget
    v.currentTime = Math.min(keep.current.t, Math.max(0, duration - 0.05))
    if (i === front && keep.current.playing) v.play().catch(() => {})
  }
  const onSeekedLayer = (i) => (e) => {
    const v = e.currentTarget
    if (i !== front && layers[i] === src) {
      const old = vids[front].current
      if (keep.current.playing || (old && !old.paused)) v.play().catch(() => {})
      if (old) old.pause()
      setFront(i)
      return
    }
    if (i === front && !playing) onTime(v.currentTime)
  }
  useEffect(() => {
    const v = videoRef.current
    if (v && !playing && Math.abs(v.currentTime - time) > 0.02) v.currentTime = time
  }, [time, playing])
  // a selected shot plays on its own: stop on its last frame, or go round again with 循环
  const seg = useRef(null)
  seg.current = segment ? { ...segment, loop } : null
  useEffect(() => {
    if (!playing) return
    let id
    const tick = () => {
      const v = videoRef.current
      const s = seg.current
      if (v && s && v.currentTime >= s.end - 0.5 / fps) {
        if (s.loop) v.currentTime = s.start + 0.001
        else { v.pause(); v.currentTime = s.end - 1 / fps }
      }
      if (v) onTime(v.currentTime)
      id = requestAnimationFrame(tick)
    }
    id = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(id)
  }, [playing])

  const toggle = () => {
    const v = videoRef.current
    if (!v) return
    if (!v.paused) return v.pause()
    if (segment && (v.currentTime < segment.start - 1e-3 || v.currentTime >= segment.end - 1.5 / fps)) v.currentTime = segment.start + 0.001
    v.play()
  }
  const step = (n) => {
    const v = videoRef.current
    if (v && !v.paused) v.pause()
    onTime(Math.min(duration - 0.01, Math.max(0, Math.round((time + n / fps) * fps) / fps)))
  }
  useEffect(() => {
    const onKey = (e) => {
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return
      if (e.key === 'Escape') setMenu(null)
      if (e.code === 'Space') { e.preventDefault(); toggle() }
      if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1) }
      if (e.key === 'ArrowRight') { e.preventDefault(); step(1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ---- beat indicator: pulses on every beat, harder on downbeats
  const beats = wave?.grid?.t || []
  let lastBeat = -1
  for (let i = 0; i < beats.length && beats[i] <= time + 1e-3; i++) lastBeat = i
  const age = lastBeat >= 0 ? time - beats[lastBeat] : 9
  const strong = lastBeat >= 0 && wave.grid.downbeat?.[lastBeat]
  const pulse = Math.max(0, 1 - age / 0.22)

  // ---- sticker of the selected shot
  const local = shot ? time - shot.t_start : -1
  const inShot = shot && local >= 0 && local < shot.t_end - shot.t_start
  const f = track ? Math.min(track.frames - 1, Math.max(0, Math.round(local * track.fps))) : 0
  const tIn = draft?.in || 0
  const tOut = draft?.out ?? (shot ? shot.t_end - shot.t_start : 0)
  // at a keyframe the box sits exactly on it (no waiting for the tracked path to come back from the backend)
  const kHere = draft && inShot ? draft.keys.find((k) => Math.abs(k.t - local) < 0.5 / fps) : null
  const bx = kHere ? kHere.x : track?.x[f], by = kHere ? kHere.y : track?.y[f]
  const bsize = kHere ? (kHere.size ?? 0.22) : track?.size[f]
  const visible = draft && (track || kHere) && inShot && local >= tIn && local < tOut
  const rect = () => boxRef.current.getBoundingClientRect()
  const norm = (e) => { const r = rect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))] }
  const minSidePx = () => { const r = rect(); return Math.min(r.width, r.height) }
  const dismissHint = () => { setHint(false); try { localStorage.setItem('oddcut.hint.sticker2', '1') } catch {} }

  // Drags move the element itself, at most once per frame, and tell the app only when let go: re-rendering
  // the whole editor on every pointer move is what made dragging stutter.
  const drag = (e, move, done = () => {}, el = null) => {
    e.preventDefault()
    e.stopPropagation()
    const css = el ? el.style.cssText : null
    let last = null, id = 0
    const step = () => { id = 0; if (last) move(last) }
    const mv = (ev) => { last = ev; if (!id) id = requestAnimationFrame(step) }
    const up = (ev) => {
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
      if (id) { cancelAnimationFrame(id); step() }
      if (el) el.style.cssText = css                  // React takes over again, with the new values
      flushSync(() => done(ev))
    }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', up)
  }
  const pct = (v) => `${v * 100}%`
  const onStickerDown = (e) => {
    if (e.button !== 0) return
    if (recordArmed) return startRecording(e)
    const el = e.currentTarget
    const [x0, y0] = norm(e)
    const cx = bx, cy = by
    let at = null
    drag(e, (ev) => {
      const [x, y] = norm(ev)
      at = { x: Math.min(1, Math.max(0, cx + x - x0)), y: Math.min(1, Math.max(0, cy + y - y0)) }
      el.style.left = pct(at.x); el.style.top = pct(at.y)
    }, () => { if (at) { onDraft(at); onCommit() } }, el)
  }
  const resize = (e) => {
    const el = e.currentTarget.parentElement
    const r = rect()
    const cx = r.left + bx * r.width, cy = r.top + by * r.height
    let size = null
    drag(e, (ev) => {
      const side = 2 * Math.max(Math.abs(ev.clientX - cx), Math.abs(ev.clientY - cy))
      size = Math.min(0.6, Math.max(0.06, side / minSidePx()))
      el.style.width = pct((size * Math.min(aw, ah)) / aw); el.style.height = pct((size * Math.min(aw, ah)) / ah)
    }, () => { if (size != null) { onDraft({ size }); onCommit() } }, el)
  }
  const onKeyDown = (e, j, k) => {
    setMenu(null)
    if (e.button === 2) return
    onSelectKey(j)
    onTime(shot.t_start + k.t + 0.001)
    const el = e.currentTarget
    let at = null
    drag(e, (ev) => { at = norm(ev); el.style.left = pct(at[0]); el.style.top = pct(at[1]) },
         () => { if (at) { onMoveKey(j, at[0], at[1]); onCommit() } }, el)
  }

  // picture-in-picture: click to select, drag to move, a corner to scale, the knob above it to turn. With
  // keyframes the change goes into the keyframe at the playhead (made if there is none), else into the box.
  const ovLocal = (o) => Math.round((time - o.t_start) * fps) / fps
  const poseStyle = (o, p) => ({ left: pct(p.x - o.w / 2), top: pct(p.y - o.h / 2), width: pct(o.w), height: pct(o.h),
                                 transform: `rotate(${p.r}deg) scale(${p.s})` })
  const applyStyle = (el, st) => { Object.assign(el.style, st) }
  const ovSave = (o, pose) => {
    if (o.keys?.length) onOverlayDraft(o.id, { keys: withKey(o, ovLocal(o), pose, fps) })
    else onOverlayDraft(o.id, { x: pose.x - (pose.w ?? o.w) / 2, y: pose.y - (pose.h ?? o.h) / 2, w: pose.w ?? o.w, h: pose.h ?? o.h, rot: pose.r })
    onOverlayCommit()
  }
  // a point in the layer's own (unrotated, 0..1) box space
  const boxPoint = (ev, o, boxEl) => {
    const br = boxEl.getBoundingClientRect()
    const p0 = keyPose(o, ovLocal(o))
    const fr = rect()
    const bw = o.w * fr.width * p0.s, bh = o.h * fr.height * p0.s
    const ang = (-p0.r * Math.PI) / 180
    const dx = ev.clientX - (br.left + br.width / 2), dy = ev.clientY - (br.top + br.height / 2)
    return [Math.min(1, Math.max(0, 0.5 + (dx * Math.cos(ang) - dy * Math.sin(ang)) / bw)),
            Math.min(1, Math.max(0, 0.5 + (dx * Math.sin(ang) + dy * Math.cos(ang)) / bh))]
  }
  // 抠像 tools on the selected layer: click an object to cut it out; paint to add to / erase from the cut
  const cutDown = (e, o) => {
    const el = e.currentTarget
    const m = o.mask || {}
    if (cutTool === 'pick') {
      e.preventDefault(); e.stopPropagation()
      const [x, y] = boxPoint(e, o, el)
      onOverlayDraft(o.id, { mask: { ...m, shape: 'cutout', target: { kind: 'object', x, y } } })
      onOverlayCommit()
      setCutTool(null)
      return
    }
    const pts = [boxPoint(e, o, el)]
    setStroke({ op: cutTool, pts })
    drag(e, (ev) => { pts.push(boxPoint(ev, o, el)); setStroke({ op: cutTool, pts: [...pts] }) }, () => {
      setStroke(null)
      onOverlayDraft(o.id, { mask: { ...m, brush: [...(m.brush || []), { op: cutTool, r: 0.03, pts: pts.filter((_, i) => i % 2 === 0 || i === pts.length - 1) }] } })
      onOverlayCommit()
    })
  }
  const ovDown = (e, o) => {
    if (e.button !== 0) return
    if (o.id === ovSel && cutTool) return cutDown(e, o)
    if (o.id !== ovSel) onSelectOverlay(o.id)
    const el = e.currentTarget
    const p0 = keyPose(o, ovLocal(o))
    const [x0, y0] = norm(e)
    let p = null
    el.classList.add('dragging')
    drag(e, (ev) => {
      const [x, y] = norm(ev)
      p = { ...p0, x: p0.x + x - x0, y: p0.y + y - y0 }
      applyStyle(el, poseStyle(o, p))
    }, () => { el.classList.remove('dragging'); if (p) ovSave(o, p) }, el)
  }
  const ovResize = (e, o) => {
    const el = e.currentTarget.parentElement
    const r = rect()
    const p0 = keyPose(o, ovLocal(o))
    const cx = r.left + p0.x * r.width, cy = r.top + p0.y * r.height
    const d0 = Math.max(4, Math.hypot(e.clientX - cx, e.clientY - cy))
    const keyed = !!o.keys?.length
    let p = null
    el.classList.add('dragging')
    drag(e, (ev) => {
      const f = Math.max(0.05, Math.hypot(ev.clientX - cx, ev.clientY - cy) / d0)
      p = keyed ? { ...p0, s: Math.min(5, p0.s * f) } : { ...p0, w: Math.min(1.5, o.w * f), h: Math.min(1.5, o.h * f) }
      applyStyle(el, keyed ? poseStyle(o, p) : { ...poseStyle({ ...o, w: p.w, h: p.h }, p) })
    }, () => { el.classList.remove('dragging'); if (p) ovSave(o, p) }, el)
  }
  const ovRotate = (e, o) => {
    const el = e.currentTarget.parentElement
    const r = rect()
    const p0 = keyPose(o, ovLocal(o))
    const cx = r.left + p0.x * r.width, cy = r.top + p0.y * r.height
    let p = null
    drag(e, (ev) => {
      let deg = (Math.atan2(ev.clientY - cy, ev.clientX - cx) * 180) / Math.PI + 90
      if (ev.shiftKey) deg = Math.round(deg / 15) * 15
      if (Math.abs(deg) < 3) deg = 0
      p = { ...p0, r: Math.round(deg * 10) / 10 }
      applyStyle(el, poseStyle(o, p))
    }, () => { if (p) ovSave(o, p) }, el)
  }
  // the mask: drag its middle to move it, the side knob to size it, the top knob to turn it (in box space)
  const maskDrag = (e, o, what) => {
    const box = e.currentTarget.closest('.ov-box')
    const br = box.getBoundingClientRect()
    const p0 = keyPose(o, ovLocal(o))
    const m0 = { cx: 0.5, cy: 0.5, size: 0.8, rot: 0, ...o.mask }
    const fr = rect()
    const bw = o.w * fr.width * p0.s, bh = o.h * fr.height * p0.s
    const ang = (-p0.r * Math.PI) / 180
    const toBox = (ev) => {                       // pointer in the box's own (unrotated, 0..1) space
      const dx = ev.clientX - (br.left + br.width / 2), dy = ev.clientY - (br.top + br.height / 2)
      return [0.5 + (dx * Math.cos(ang) - dy * Math.sin(ang)) / bw, 0.5 + (dx * Math.sin(ang) + dy * Math.cos(ang)) / bh]
    }
    const [sx, sy] = toBox(e)
    let m = null
    drag(e, (ev) => {
      const [x, y] = toBox(ev)
      if (what === 'move') m = { ...m0, cx: m0.cx + x - sx, cy: m0.cy + y - sy }
      else if (what === 'size') {
        const short = Math.min(bw, bh)
        m = { ...m0, size: Math.max(0.05, Math.min(2, (2 * Math.hypot((x - m0.cx) * bw, (y - m0.cy) * bh)) / short)) }
      } else m = { ...m0, rot: Math.round((Math.atan2((y - m0.cy) * bh, (x - m0.cx) * bw) * 180) / Math.PI + 90) }
      onOverlayDraft(o.id, { mask: m })
    }, () => { if (m) onOverlayCommit() })
  }
  const [frameW, frameH] = fsz
  // the 抠像 mask of the selected layer at the playhead, from the backend (it follows the picture)
  const selOv = overlays.find((o) => o.id === ovSel)
  const cutKey = selOv && selOv.mask?.shape === 'cutout' && time >= selOv.t_start && time < selOv.t_end
    ? `${planPath}|${selOv.id}|${JSON.stringify(selOv.mask)}|${Math.round((time - selOv.t_start) * 10)}` : null
  useEffect(() => {
    if (!cutKey) { setCutImg(null); return }
    let alive = true
    const id = setTimeout(() => {
      window.oddcut.api('GET', `/api/cutout?plan=${encodeURIComponent(planPath)}&id=${encodeURIComponent(selOv.id)}&t=${(time - selOv.t_start).toFixed(2)}`)
        .then((r) => { if (alive) setCutImg(r.image) }).catch(() => {})
    }, 120)
    return () => { alive = false; clearTimeout(id) }
  }, [cutKey])
  const liveOv = overlays.filter((o) => time >= o.t_start && time < o.t_end).sort((a, b) => a.track - b.track)

  // 边播边拖: hold the sticker, playback runs at half speed, the drag is recorded and thinned into keyframes
  const startRecording = (e) => {
    const v = videoRef.current
    if (!v) return
    e.preventDefault(); e.stopPropagation()
    const pts = []
    let last = norm(e)
    const sample = () => {
      const lt = v.currentTime - shot.t_start
      if (lt >= 0 && lt <= tOut) pts.push([lt, last[0], last[1]])
      setRec([...pts])
    }
    sample()
    v.playbackRate = 0.5
    v.play().catch(() => {})
    let id = requestAnimationFrame(function loop() {
      sample()
      if (v.currentTime - shot.t_start >= tOut - 0.01) return stop()
      id = requestAnimationFrame(loop)
    })
    const mv = (ev) => { last = norm(ev) }
    const stop = () => {
      cancelAnimationFrame(id)
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', stop)
      v.pause(); v.playbackRate = 1
      setRec(null)
      if (pts.length < 2) return
      // one point every ~0.25 s, then drop the ones a straight line already explains
      const t0 = pts[0][0], t1 = pts[pts.length - 1][0]
      const grid = []
      for (let t = t0; t < t1; t += 0.25) grid.push(pts.reduce((b, p) => (Math.abs(p[0] - t) < Math.abs(b[0] - t) ? p : b)))
      grid.push(pts[pts.length - 1])
      const keys = rdp(grid, 0.012).map(([t, x, y]) => ({ t: Math.round(t * fps) / fps, x, y }))
      onRecorded(keys, t0, t1)
    }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', stop)
  }

  const onSurface = (e) => {
    setMenu(null)
    if (e.button !== 0) return
    if (drawPick) { e.preventDefault(); onDrawPick(...norm(e)); return }
    if (boxMode) {
      const [x0, y0] = norm(e)
      let b = [x0, y0, x0, y0]
      setBoxDraft(b)
      const mv = (ev) => { const [x1, y1] = norm(ev); b = [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]; setBoxDraft(b) }
      const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); setBoxDraft(null); if (b[2] - b[0] > 0.03 && b[3] - b[1] > 0.03) onBox(b) }
      window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
      return
    }
    if (autoSticker && !draft && inShot) onPlaceAuto(...norm(e))   // an auto sticker becomes a hand-placed one there
  }

  // path: whole shot, played part solid, the rest dashed; tracked paths (跟着物体) drawn thinner
  const pts = (a, b) => track.x.slice(a, b + 1).map((x, k) => `${(x * 100).toFixed(2)},${(track.y[a + k] * 100).toFixed(2)}`).join(' ')
  const showPath = draft && track && inShot && draft.mode !== 'still'
  const i0 = track ? Math.round(tIn * track.fps) : 0
  const i1 = track ? Math.min(track.frames - 1, Math.round(tOut * track.fps)) : 0
  const side = visible ? bsize * Math.min(aw, ah) : 0

  return (
    <div className="viewer">
      <div className="stage-box">
        <div className={`beat-badge ${playing ? 'live' : ''}`}>
          <i className={`beat-dot ${strong ? 'strong' : ''}`} style={{ transform: `scale(${1 + pulse * (strong ? 0.9 : 0.45)})`, opacity: 0.45 + 0.55 * pulse }} />
          <span>{wave?.tempo ? `${Math.round(wave.tempo)} BPM` : '节拍'}</span>
          <button className="help" onClick={() => setHelp((h) => !h)} title="什么是卡点？">?</button>
        </div>
        {help && (
          <div className="beat-help" onPointerDown={(e) => e.stopPropagation()}>
            <div className="bh-head"><b>什么是卡点</b><button className="icon" onClick={() => setHelp(false)}><Icon name="close" size={14} /></button></div>
            <svg viewBox="0 0 240 70" className="bh-diagram">
              {Array.from({ length: 48 }, (_, i) => <rect key={i} x={i * 5} y={38 - (6 + ((i * 37) % 17))} width="3" height={2 * (6 + ((i * 37) % 17))} className="wv" />)}
              {Array.from({ length: 8 }, (_, i) => <rect key={`b${i}`} x={10 + i * 30} y="0" width={i % 4 === 0 ? 2.5 : 1.5} height={i % 4 === 0 ? 12 : 7} className={i % 4 === 0 ? 'down' : 'beat'} />)}
              <line className="cutline" x1="0" y1="2" x2="0" y2="68" />
            </svg>
            <p>音乐里有规律的鼓点叫<b>节拍</b>，每 4 拍里最重的那一下是<b>重拍</b>。</p>
            <p>Oddcut 会把镜头切换放在节拍上，重要的镜头（手绘变回实拍）放在重拍上。</p>
            <p>你拖切点时会自动吸到最近的节拍；时间轴上绿点 = 这个切点踩在拍上。</p>
          </div>
        )}

        <div className="frame" ref={boxRef} style={{ aspectRatio: `${aw} / ${ah}`, '--ar': aw / ah }}
             onContextMenu={(e) => e.preventDefault()}
             onDragOver={(e) => { if (e.dataTransfer.types.includes('application/x-oddcut-sticker')) e.preventDefault() }}
             onDrop={(e) => { const id = e.dataTransfer.getData('application/x-oddcut-sticker'); if (id) { e.preventDefault(); onDropSticker(id, ...norm(e)) } }}>
          {[0, 1].map((i) => layers[i] && (
            <video key={i} ref={vids[i]} src={layers[i]} className={i === front ? 'front' : 'back'} playsInline preload="auto"
                   onLoadedMetadata={onLoaded(i)} onSeeked={onSeekedLayer(i)}
                   onPlay={() => i === front && setPlaying(true)} onPause={() => i === front && setPlaying(false)}
                   onEnded={() => i === front && setPlaying(false)} />
          ))}
          {!layers[front] && <div className="frame-empty">{status || '正在准备预览…'}</div>}

          <div className={`surface ${boxMode || drawPick ? 'box-mode' : autoSticker && !draft && inShot ? 'place-mode' : ''}`} onPointerDown={onSurface}>
            {showPath && (
              <svg viewBox="0 0 100 100" preserveAspectRatio="none" className={draft.mode === 'follow' ? 'tracked' : 'drawn-path'}>
                <polyline className="past" points={pts(i0, Math.max(i0, Math.min(f, i1)))} />
                <polyline className="future" points={pts(Math.max(i0, Math.min(f, i1)), i1)} />
              </svg>
            )}
            {rec && <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="recording"><polyline points={rec.map((p) => `${p[1] * 100},${p[2] * 100}`).join(' ')} /></svg>}
            {draft && inShot && draft.keys.map((k, j) => (
              <i key={j} className={`kdot ${keySel === j ? 'sel' : ''} ${Math.abs(k.t - local) < 0.5 / fps ? 'now' : ''}`}
                 style={{ left: `${k.x * 100}%`, top: `${k.y * 100}%` }} title={`第 ${j + 1} 个关键帧 · ${k.t.toFixed(2)} 秒（拖动调位置，右键更多）`}
                 onPointerDown={(e) => onKeyDown(e, j, k)}
                 onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); const r = rect(); onSelectKey(j); setMenu({ j, x: e.clientX - r.left, y: e.clientY - r.top }) }} />
            ))}
            {visible && !rec && (
              <div className={`sel-box ${recordArmed ? 'armed' : ''}`} onPointerDown={onStickerDown}
                   style={{ left: `${bx * 100}%`, top: `${by * 100}%`, width: `${(side / aw) * 100}%`, height: `${(side / ah) * 100}%` }}>
                {showImage && stickerImage && <img src={stickerImage} alt="" draggable={false} />}
                {!recordArmed && ['nw', 'ne', 'sw', 'se'].map((c) => <i key={c} className={`corner ${c}`} onPointerDown={resize} />)}
              </div>
            )}
            {liveOv.map((o) => {
              const sel = o.id === ovSel
              if (!sel && shot) return null                  // while a shot is being edited, overlays stay out of the way
              const p = keyPose(o, ovLocal(o))
              const bw = o.w * frameW, bh = o.h * frameH
              const m = o.mask && !['none', 'cutout'].includes(o.mask.shape) ? { cx: 0.5, cy: 0.5, size: 0.8, rot: 0, ...o.mask } : null
              const mr = m ? (m.size * Math.min(bw, bh)) / 2 : 0
              const ma = m ? (m.rot * Math.PI) / 180 : 0
              return (
                <div key={o.id} className={`ov-box ${sel ? 'sel' : ''} ${sel && ovShowImage ? 'show' : ''} ${sel && cutTool ? `tool-${cutTool}` : ''}`} onPointerDown={(e) => ovDown(e, o)}
                     title={sel ? '拖动移动，拖四角缩放，拖上面的圆点旋转' : `画中画 ${o.track + 1}：点一下选中`}
                     style={{ ...poseStyle(o, p), borderRadius: o.radius * Math.min(bw, bh) }}>
                  {sel && clipThumbs[o.clip] && <img src={clipThumbs[o.clip]} alt="" draggable={false} style={{ opacity: o.opacity }} />}
                  {sel && cutImg && o.mask?.shape === 'cutout' && (!o.fx || cutTool) && (
                    <i className="cut-preview" style={{ WebkitMaskImage: `url(${cutImg})`, maskImage: `url(${cutImg})` }} />
                  )}
                  {sel && stroke && (
                    <svg className={`cut-stroke ${stroke.op}`} viewBox="0 0 100 100" preserveAspectRatio="none">
                      <polyline points={stroke.pts.map(([x, y]) => `${x * 100},${y * 100}`).join(' ')} />
                    </svg>
                  )}
                  {sel && o.grid && (
                    <svg className="ov-grid" viewBox={`0 0 ${bw} ${bh}`} preserveAspectRatio="none">
                      {gridCells(o, bw, bh).map((c, k) => <rect key={k} x={c.x} y={c.y} width={c.w} height={c.h} />)}
                    </svg>
                  )}
                  {sel && m && (
                    <>
                      <svg className="ov-mask" viewBox={`0 0 ${bw} ${bh}`} preserveAspectRatio="none"><path d={maskPath(o, bw, bh)} /></svg>
                      <i className="mask-knob move" style={{ left: pct(m.cx), top: pct(m.cy) }} title="拖动移动蒙版" onPointerDown={(e) => maskDrag(e, o, 'move')} />
                      <i className="mask-knob size" style={{ left: m.cx * bw + mr * Math.cos(ma), top: m.cy * bh + mr * Math.sin(ma) }}
                         title="拖动调蒙版大小" onPointerDown={(e) => maskDrag(e, o, 'size')} />
                      <i className="mask-knob turn" style={{ left: m.cx * bw + (mr + 18) * Math.sin(ma), top: m.cy * bh - (mr + 18) * Math.cos(ma) }}
                         title="拖动旋转蒙版" onPointerDown={(e) => maskDrag(e, o, 'turn')} />
                    </>
                  )}
                  {sel && ['nw', 'ne', 'sw', 'se'].map((c) => <i key={c} className={`corner ${c}`} onPointerDown={(e) => ovResize(e, o)} />)}
                  {sel && <i className="turn-knob" title="拖动旋转（按住 Shift 每 15°）" onPointerDown={(e) => ovRotate(e, o)} />}
                </div>
              )
            })}
            {boxDraft && (
              <i className="subject-box" style={{ left: `${boxDraft[0] * 100}%`, top: `${boxDraft[1] * 100}%`,
                                                 width: `${(boxDraft[2] - boxDraft[0]) * 100}%`, height: `${(boxDraft[3] - boxDraft[1]) * 100}%` }} />
            )}
            {menu && (
              <div className="kmenu" style={{ left: menu.x, top: menu.y }} onPointerDown={(e) => e.stopPropagation()}>
                <button onClick={() => { setMenu(null); onDeleteKey(menu.j) }}><Icon name="trash" size={16} />删除这个关键帧</button>
                <button onClick={() => { setMenu(null); onStillKey(menu.j) }}><Icon name="check" size={16} />设为停在这里</button>
              </div>
            )}
          </div>

          {boxMode && <span className="frame-tip">在画面上框出要画的东西</span>}
          {drawPick && <span className="frame-tip">点一下画面里要画成手绘的物体（Esc 取消）</span>}
          {!boxMode && recordArmed && visible && <span className="frame-tip">按住贴纸拖动，视频会慢速播放并记录路线；松开结束</span>}
          {!boxMode && !recordArmed && autoSticker && !draft && inShot && <span className="frame-tip">自动加的贴纸：点画面上的位置，改成手动放在那里</span>}
          {!boxMode && !recordArmed && draft && inShot && hint && (
            <span className="frame-tip dismiss">拖贴纸移动、拖四角调大小，会在当前时刻自动加关键帧。白色菱形是关键帧，点它跳过去，拖它改位置，右键删除
              <button onClick={dismissHint} aria-label="知道了"><Icon name="close" size={14} /></button></span>
          )}
        </div>
      </div>
      <div className="transport">
        <button className="icon" onClick={() => step(-1)} title="上一帧（←）"><Icon name="prev" /></button>
        <button className="play" onClick={toggle} title={playing ? '暂停（空格）' : segment ? `播放片段 ${segment.n}（空格）` : '播放（空格）'} disabled={!src}>
          <Icon name={playing ? 'pause' : 'play'} size={24} fill />
        </button>
        <button className="icon" onClick={() => step(1)} title="下一帧（→）"><Icon name="next" /></button>
        {segment && (
          <button className={`toggle loop ${loop ? 'on' : ''}`} onClick={() => setLoop((l) => !l)} aria-pressed={loop}
                  title="循环播放选中的这一段"><Icon name="refresh" size={16} />循环</button>
        )}
        <span className={`tc ${segment ? 'seg' : ''}`}>
          {segment
            ? <>片段 {segment.n} · {Math.min(Math.max(0, time - segment.start), segment.end - segment.start).toFixed(1)} / {(segment.end - segment.start).toFixed(1)}s</>
            : <>{fmt(time)} / {fmt(duration)}</>}
        </span>
      </div>
    </div>
  )
}
