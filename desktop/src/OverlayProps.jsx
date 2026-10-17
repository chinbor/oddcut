import React from 'react'
import Icon from './icons.jsx'

// Box presets: where the picture-in-picture sits (the clip fills its box, cropped to fit)
const PRESETS = [
  ['pip', '小窗', (o, pip) => pip],
  ['full', '全屏', () => ({ x: 0, y: 0, w: 1, h: 1 })],
  ['left', '分屏左', () => ({ x: 0, y: 0, w: 0.5, h: 1 })],
  ['right', '分屏右', () => ({ x: 0.5, y: 0, w: 0.5, h: 1 })],
  ['top', '分屏上', () => ({ x: 0, y: 0, w: 1, h: 0.5 })],
  ['bottom', '分屏下', () => ({ x: 0, y: 0.5, w: 1, h: 0.5 })],
]

// Right panel while a picture-in-picture clip is selected. Sliders change the box live; letting go saves.
export default function OverlayProps({ ov, thumb, pip, busy, onClose, onChange, onCommit, onDelete }) {
  const len = ov.t_end - ov.t_start
  const set = (patch, commit = true) => { onChange(patch); if (commit) onCommit() }
  return (
    <aside className="props">
      <header className="props-head">
        {thumb ? <img src={thumb} alt="" /> : <i className="thumb-ph" />}
        <span><b>画中画 {ov.track + 1}</b><small>{ov.name} · {ov.t_start.toFixed(1)} – {ov.t_end.toFixed(1)} 秒 · 共 {len.toFixed(1)} 秒</small></span>
        <button className="icon" onClick={onClose} title="关闭（Esc）"><Icon name="close" /></button>
      </header>

      <section>
        <h3>位置</h3>
        <div className="preset-grid">
          {PRESETS.map(([id, label, f]) => (
            <button key={id} disabled={busy} onClick={() => set(f(ov, pip))} className={`preset ${id}`}>
              <i className="preset-ico"><b /></i>{label}
            </button>
          ))}
        </div>
        <p className="caption">在预览画面上拖动画中画来移动，拖四角缩放（保持比例）。</p>
      </section>

      <section>
        <h3>样式</h3>
        <label className="slider-row">
          <span>不透明度</span>
          <input type="range" min="0.1" max="1" step="0.05" value={ov.opacity} disabled={busy}
                 onChange={(e) => onChange({ opacity: Number(e.target.value) })} onPointerUp={onCommit} onKeyUp={onCommit} />
          <b>{Math.round(ov.opacity * 100)}%</b>
        </label>
        <label className="slider-row">
          <span>圆角</span>
          <input type="range" min="0" max="0.5" step="0.02" value={ov.radius} disabled={busy}
                 onChange={(e) => onChange({ radius: Number(e.target.value) })} onPointerUp={onCommit} onKeyUp={onCommit} />
          <b>{Math.round(ov.radius * 200)}%</b>
        </label>
        <label className="switch-row">
          <span><b>白色描边</b></span>
          <input type="checkbox" className="switch" checked={!!ov.border} disabled={busy} onChange={(e) => set({ border: e.target.checked })} />
        </label>
      </section>

      <section>
        <h3>轨道</h3>
        <div className="segmented three">
          {[0, 1, 2].map((k) => (
            <button key={k} className={ov.track === k ? 'on' : ''} disabled={busy} onClick={() => set({ track: k })}>画中画 {k + 1}</button>
          ))}
        </div>
        <p className="caption">数字大的轨道叠在上面。在时间轴上拖动可改时间和轨道，拖两端可剪短。</p>
        <button className="danger wide" disabled={busy} onClick={onDelete}><Icon name="trash" size={16} />删除这个画中画</button>
      </section>
    </aside>
  )
}
