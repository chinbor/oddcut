import React, { useRef } from 'react'

// Over the preview while a sticker is being placed: its tracked path (line), keyframes (dots, drag to
// correct; the track restarts from a corrected keyframe), and the sticker itself where the playhead is.
// Clicking on the picture at the playhead adds a keyframe there (or moves the one at this moment).

export default function StickerOverlay({ shot, draft, track, time, aspect, image, keySel, onPlace, onMoveKey, onSelectKey, ghost = true }) {
  const ref = useRef(null)
  const [aw, ah] = aspect
  const local = time - shot.t_start
  const inside = local >= 0 && local < shot.t_end - shot.t_start
  const fps = track?.fps || 30
  const f = track ? Math.min(track.frames - 1, Math.max(0, Math.round(local * fps))) : 0
  const tIn = draft.in || 0
  const tOut = draft.out ?? shot.t_end - shot.t_start
  const visible = inside && local >= tIn && local < tOut && track
  const minSide = Math.min(aw, ah)

  const point = (e) => {
    const r = ref.current.getBoundingClientRect()
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]
  }
  const drag = (e, onMove) => {
    e.preventDefault()
    e.stopPropagation()
    const move = (ev) => onMove(...point(ev))
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  const onDown = (e) => {
    if (!inside) return
    const j = onPlace(...point(e))
    drag(e, (x, y) => onMoveKey(j, x, y))
  }

  // path: only the part where the sticker shows
  const i0 = track ? Math.max(0, Math.round(tIn * fps)) : 0
  const i1 = track ? Math.min(track.frames - 1, Math.round(tOut * fps)) : 0
  const pts = track ? track.x.slice(i0, i1 + 1).map((x, k) => `${(x * 100).toFixed(2)},${(track.y[i0 + k] * 100).toFixed(2)}`).join(' ') : ''

  return (
    <div className={`sticker-overlay ${inside ? '' : 'away'}`} ref={ref} onPointerDown={onDown}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none">
        {pts && <polyline points={pts} />}
      </svg>
      {visible && image && ghost && (
        <img className="sticker-ghost" src={image} alt="" draggable={false}
             style={{ left: `${track.x[f] * 100}%`, top: `${track.y[f] * 100}%`, width: `${(track.size[f] * minSide / aw) * 100}%` }} />
      )}
      {draft.keys.map((k, j) => (
        <i key={j} className={`kf ${keySel === j ? 'sel' : ''} ${Math.abs(k.t - local) < 0.5 / fps ? 'now' : ''}`}
           style={{ left: `${k.x * 100}%`, top: `${k.y * 100}%` }} title={`关键帧 ${j + 1} · ${k.t.toFixed(2)}s`}
           onPointerDown={(e) => { onSelectKey(j); drag(e, (x, y) => onMoveKey(j, x, y)) }} />
      ))}
      {!inside && <span className="overlay-note">播放头不在这个镜头里，点时间线上的镜头回来</span>}
      {inside && !draft.keys.length && <span className="overlay-note">在画面上点一下放贴纸</span>}
    </div>
  )
}
