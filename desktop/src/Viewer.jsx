import React, { useEffect, useRef, useState } from 'react'
import Icon from './icons.jsx'

const fmt = (t) => {
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`
}

// Centre of the editor: the preview (no native controls), our own transport bar, and direct manipulation of
// the selected shot's sticker (drag to move, corners to resize; doing that at a new moment adds a keyframe).
export default function Viewer({ src, videoRef, time, onTime, playing, setPlaying, duration, fps, aspect, shot,
                                 draft, track, stickerImage, autoSticker, onDraft, onCommit, onDropSticker,
                                 boxMode, onBox, status }) {
  const boxRef = useRef(null)
  const [aw, ah] = aspect
  const keep = useRef({ t: 0, playing: false })
  const [hint, setHint] = useState(() => { try { return !localStorage.getItem('oddcut.hint.sticker') } catch { return true } })
  const [boxDraft, setBoxDraft] = useState(null)

  // Two stacked <video> layers: a new preview file (an edit, or an effect filled in) loads in the back one,
  // seeks to the same moment, and only then comes to the front, so there is no black flash on a swap.
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
    if (i !== front && layers[i] === src) {           // the new file is ready at the right moment: swap
      const old = vids[front].current
      if (keep.current.playing || (old && !old.paused)) v.play().catch(() => {})
      if (old) old.pause()
      setFront(i)
      return
    }
    if (i === front && !playing) onTime(v.currentTime)
  }
  // scrubbing from the timeline moves the video
  useEffect(() => {
    const v = videoRef.current
    if (v && !playing && Math.abs(v.currentTime - time) > 0.02) v.currentTime = time
  }, [time, playing])
  useEffect(() => {
    if (!playing) return
    let id
    const tick = () => { if (videoRef.current) onTime(videoRef.current.currentTime); id = requestAnimationFrame(tick) }
    id = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(id)
  }, [playing])

  const toggle = () => {
    const v = videoRef.current
    if (!v) return
    v.paused ? v.play() : v.pause()
  }
  const step = (n) => {
    const v = videoRef.current
    if (v && !v.paused) v.pause()
    onTime(Math.min(duration - 0.01, Math.max(0, Math.round((time + n / fps) * fps) / fps)))
  }
  useEffect(() => {
    const onKey = (e) => {
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return
      if (e.code === 'Space') { e.preventDefault(); toggle() }
      if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1) }
      if (e.key === 'ArrowRight') { e.preventDefault(); step(1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ---- sticker on the selected shot
  const local = shot ? time - shot.t_start : -1
  const inShot = shot && local >= 0 && local < shot.t_end - shot.t_start
  const f = track ? Math.min(track.frames - 1, Math.max(0, Math.round(local * track.fps))) : 0
  const tIn = draft?.in || 0
  const tOut = draft?.out ?? (shot ? shot.t_end - shot.t_start : 0)
  const showSticker = draft && track && inShot && local >= tIn && local < tOut
  const rect = () => boxRef.current.getBoundingClientRect()
  const norm = (e) => { const r = rect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))] }
  const minSidePx = () => { const r = rect(); return Math.min(r.width, r.height) }
  const dismissHint = () => { setHint(false); try { localStorage.setItem('oddcut.hint.sticker', '1') } catch {} }

  const drag = (e, move) => {
    e.preventDefault()
    e.stopPropagation()
    const mv = (ev) => move(ev)
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); onCommit() }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', up)
  }
  const moveSticker = (e) => {
    const [x0, y0] = norm(e)
    const cx = track.x[f], cy = track.y[f]
    drag(e, (ev) => { const [x, y] = norm(ev); onDraft({ x: cx + (x - x0), y: cy + (y - y0) }) })
  }
  const resize = (e) => {
    const r = rect()
    const cx = r.left + track.x[f] * r.width, cy = r.top + track.y[f] * r.height
    drag(e, (ev) => {
      const side = 2 * Math.max(Math.abs(ev.clientX - cx), Math.abs(ev.clientY - cy))
      onDraft({ size: Math.min(0.6, Math.max(0.06, side / minSidePx())) })
    })
  }
  // clicking empty picture: a sticker being edited jumps there (keyframe at this moment); an auto sticker
  // becomes a hand-placed one at that spot
  const onSurface = (e) => {
    if (boxMode) {
      const [x0, y0] = norm(e)
      let b = [x0, y0, x0, y0]
      setBoxDraft(b)
      const mv = (ev) => { const [x1, y1] = norm(ev); b = [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]; setBoxDraft(b) }
      const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); setBoxDraft(null); if (b[2] - b[0] > 0.03 && b[3] - b[1] > 0.03) onBox(b) }
      window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
      return
    }
    if (!inShot || !(draft || autoSticker)) return
    const [x, y] = norm(e)
    onDraft({ x, y })
    drag(e, (ev) => { const [x2, y2] = norm(ev); onDraft({ x: x2, y: y2 }) })
  }

  const pts = track && draft ? track.x.slice(Math.round(tIn * track.fps), Math.round(tOut * track.fps) + 1)
    .map((x, k) => `${(x * 100).toFixed(2)},${(track.y[Math.round(tIn * track.fps) + k] * 100).toFixed(2)}`).join(' ') : ''
  const side = showSticker ? track.size[f] * Math.min(aw, ah) : 0

  return (
    <div className="viewer">
      <div className="stage-box">
        <div className="frame" ref={boxRef} style={{ aspectRatio: `${aw} / ${ah}`, '--ar': aw / ah }}
             onDragOver={(e) => { if (e.dataTransfer.types.includes('application/x-oddcut-sticker')) e.preventDefault() }}
             onDrop={(e) => { const id = e.dataTransfer.getData('application/x-oddcut-sticker'); if (id) { e.preventDefault(); onDropSticker(id, ...norm(e)) } }}>
          {[0, 1].map((i) => layers[i] && (
            <video key={i} ref={vids[i]} src={layers[i]} className={i === front ? 'front' : 'back'} playsInline preload="auto"
                   onLoadedMetadata={onLoaded(i)} onSeeked={onSeekedLayer(i)}
                   onPlay={() => i === front && setPlaying(true)} onPause={() => i === front && setPlaying(false)}
                   onEnded={() => i === front && setPlaying(false)} />
          ))}
          {!layers[front] && <div className="frame-empty">{status || '正在准备预览…'}</div>}
          <div className={`surface ${boxMode ? 'box-mode' : (draft || autoSticker) && inShot ? 'place-mode' : ''}`} onPointerDown={onSurface}>
            {draft && pts && inShot && (
              <svg viewBox="0 0 100 100" preserveAspectRatio="none"><polyline points={pts} /></svg>
            )}
            {draft && inShot && draft.keys.map((k, j) => (
              <i key={j} className={`kdot ${Math.abs(k.t - local) < 0.5 / (track?.fps || 30) ? 'now' : ''}`}
                 style={{ left: `${k.x * 100}%`, top: `${k.y * 100}%` }} />
            ))}
            {showSticker && (
              <div className="sel-box" onPointerDown={moveSticker}
                   style={{ left: `${track.x[f] * 100}%`, top: `${track.y[f] * 100}%`, width: `${(side / aw) * 100}%`, height: `${(side / ah) * 100}%` }}>
                {stickerImage && <img src={stickerImage} alt="" draggable={false} />}
                {['nw', 'ne', 'sw', 'se'].map((c) => <i key={c} className={`corner ${c}`} onPointerDown={resize} />)}
              </div>
            )}
            {boxDraft && (
              <i className="subject-box" style={{ left: `${boxDraft[0] * 100}%`, top: `${boxDraft[1] * 100}%`,
                                                 width: `${(boxDraft[2] - boxDraft[0]) * 100}%`, height: `${(boxDraft[3] - boxDraft[1]) * 100}%` }} />
            )}
          </div>
          {boxMode && <span className="frame-tip">在画面上框出要画的东西</span>}
          {!boxMode && autoSticker && !draft && inShot && <span className="frame-tip">自动加的贴纸：点画面上的位置，改成手动放在那里</span>}
          {!boxMode && draft && inShot && hint && (
            <span className="frame-tip dismiss">拖动贴纸移动，拖四角调大小。换个时刻再拖，会自动加关键帧
              <button onClick={dismissHint} aria-label="知道了"><Icon name="close" size={14} /></button></span>
          )}
        </div>
      </div>
      <div className="transport">
        <button className="icon" onClick={() => step(-1)} title="上一帧（←）"><Icon name="prev" /></button>
        <button className="play" onClick={toggle} title={playing ? '暂停（空格）' : '播放（空格）'} disabled={!src}>
          <Icon name={playing ? 'pause' : 'play'} size={24} fill />
        </button>
        <button className="icon" onClick={() => step(1)} title="下一帧（→）"><Icon name="next" /></button>
        <span className="tc">{fmt(time)} / {fmt(duration)}</span>
      </div>
    </div>
  )
}
