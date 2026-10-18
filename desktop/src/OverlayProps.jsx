import React, { useState } from 'react'
import Icon from './icons.jsx'
import { ANIMS, BLENDS, EASES, GRID_NAMES, keyAlpha, keyPose, LAYOUTS, MASKS, PARTS, withKey } from './pip.js'

// Box presets: where the picture-in-picture sits (the clip fills its box, cropped to fit)
const PRESETS = [
  ['pip', '小窗', (o, pip) => pip],
  ['full', '全屏', () => ({ x: 0, y: 0, w: 1, h: 1 })],
  ['left', '分屏左', () => ({ x: 0, y: 0, w: 0.5, h: 1 })],
  ['right', '分屏右', () => ({ x: 0.5, y: 0, w: 0.5, h: 1 })],
  ['top', '分屏上', () => ({ x: 0, y: 0, w: 1, h: 0.5 })],
  ['bottom', '分屏下', () => ({ x: 0, y: 0.5, w: 1, h: 0.5 })],
]

const Chips = ({ items, value, onPick, disabled }) => (
  <div className="style-chips">
    {items.map(([id, label]) => (
      <button key={id} className={value === id ? 'on' : ''} disabled={disabled} onClick={() => onPick(id)}>{label}</button>
    ))}
  </div>
)

// A slider that shows its value live and saves when let go
const Slider = ({ label, min, max, step, value, fmt, onChange, onCommit, disabled }) => (
  <label className="slider-row">
    <span>{label}</span>
    <input type="range" min={min} max={max} step={step} value={value} disabled={disabled}
           onChange={(e) => onChange(Number(e.target.value))} onPointerUp={onCommit} onKeyUp={onCommit} />
    <b>{fmt(value)}</b>
  </label>
)

// 手绘 on this layer: style and the 绘制进度 keyframes (0 blank -> 100% drawn -> 200% the real picture)
function DrawnFx({ ov, busy, local, fps, onSeek, set, onChange, onCommit, styles }) {
  const keys = ov.fx.keys || []
  const here = keys.findIndex((k) => Math.abs(k.t - local) < 0.5 / fps)
  const prev = [...keys].reverse().find((k) => k.t < local - 0.5 / fps)
  const next = keys.find((k) => k.t > local + 0.5 / fps)
  let v = keys.length ? keys[keys.length - 1].v : 2
  if (keys.length && local <= keys[0].t) v = keys[0].v
  for (let i = 0; i + 1 < keys.length; i++) {
    if (local >= keys[i].t && local <= keys[i + 1].t) v = keys[i].v + (keys[i + 1].v - keys[i].v) * ((local - keys[i].t) / Math.max(1e-6, keys[i + 1].t - keys[i].t))
  }
  const withV = (val) => {
    const ks = [...keys]
    const j = ks.findIndex((k) => Math.abs(k.t - local) < 0.5 / fps)
    if (j >= 0) ks[j] = { ...ks[j], v: val }
    else ks.push({ t: Math.round(local * fps) / fps, v: val })
    return ks.sort((a, b) => a.t - b.t)
  }
  const stage = v <= 0.01 ? '空白' : v < 0.55 ? '画线稿' : v < 1 ? '上色' : v <= 1.001 ? '画完' : v < 2 ? '变回实拍' : '实拍'
  return (
    <section>
      <h3>手绘</h3>
      <div className="style-chips">
        {styles.map((d) => <button key={d.id} className={ov.fx.style === d.id ? 'on' : ''} disabled={busy} onClick={() => set({ fx: { ...ov.fx, style: d.id } })}>{d.name}</button>)}
      </div>
      <div className="field">
        <span className="field-label">绘制进度（关键帧）</span>
        <div className="keybar">
          <button className="icon" disabled={busy || !prev} onClick={() => onSeek(ov.t_start + prev.t + 0.001)} title="上一个关键帧"><Icon name="prev" size={18} /></button>
          <button className={`kbtn ${here >= 0 ? 'on' : ''}`} disabled={busy}
                  onClick={() => set({ fx: { ...ov.fx, keys: here >= 0 ? keys.filter((_, i) => i !== here) : withV(Math.round(v * 100) / 100) } })}
                  title={here >= 0 ? '删除这个关键帧' : '在这里加一个关键帧'}><i /></button>
          <button className="icon" disabled={busy || !next} onClick={() => onSeek(ov.t_start + next.t + 0.001)} title="下一个关键帧"><Icon name="next" size={18} /></button>
          <span className="keybar-text">{here >= 0 ? `第 ${here + 1} 个，共 ${keys.length} 个` : `共 ${keys.length} 个关键帧`} · 现在：{stage}</span>
        </div>
        <label className="slider-row">
          <span>进度</span>
          <input type="range" min="0" max="2" step="0.01" value={Math.round(v * 100) / 100} disabled={busy}
                 onChange={(e) => onChange({ fx: { ...ov.fx, keys: withV(Number(e.target.value)) } })} onPointerUp={onCommit} onKeyUp={onCommit} />
          <b>{Math.round(v * 100)}%</b>
        </label>
      </div>
      <p className="caption">0 = 空白，100% = 画完，200% = 变回实拍。拖动会在播放头这里记关键帧；时间轴上橙色菱形就是这些关键帧。</p>
    </section>
  )
}

// 手绘's main control, in the basics: the 绘制进度 at the playhead (keyed)
function DrawnProgress({ ov, busy, local, fps, onChange, onCommit }) {
  const keys = ov.fx.keys || []
  let v = keys.length ? keys[keys.length - 1].v : 2
  if (keys.length && local <= keys[0].t) v = keys[0].v
  for (let i = 0; i + 1 < keys.length; i++) {
    if (local >= keys[i].t && local <= keys[i + 1].t) v = keys[i].v + (keys[i + 1].v - keys[i].v) * ((local - keys[i].t) / Math.max(1e-6, keys[i + 1].t - keys[i].t))
  }
  const withV = (val) => {
    const ks = [...keys]
    const j = ks.findIndex((k) => Math.abs(k.t - local) < 0.5 / fps)
    if (j >= 0) ks[j] = { ...ks[j], v: val }
    else ks.push({ t: Math.round(local * fps) / fps, v: val })
    return ks.sort((a, b) => a.t - b.t)
  }
  return (
    <Slider label="绘制进度" min={0} max={2} step={0.01} value={Math.round(v * 100) / 100} fmt={(x) => (x >= 2 ? '实拍' : x > 1 ? '变回实拍' : `${Math.round(x * 100)}%`)}
            disabled={busy} onChange={(x) => onChange({ fx: { ...ov.fx, keys: withV(x) } })} onCommit={onCommit} />
  )
}

// Right panel while a picture-in-picture clip (or 宫格) is selected. Every change is one undo step.
export default function OverlayProps({ ov, thumb, pip, busy, onClose, onChange, onCommit, onDelete, local, fps, onSeek,
                                       clips, clipThumbs, cutTool, setCutTool, onRemoveGroup, groupWhy, drawnStyles = [] }) {
  const len = ov.t_end - ov.t_start
  const set = (patch) => { onChange(patch); onCommit() }
  const keys = ov.keys || []
  const here = keys.findIndex((k) => Math.abs(k.t - local) < 0.5 / fps)
  const prev = [...keys].reverse().find((k) => k.t < local - 0.5 / fps)
  const next = keys.find((k) => k.t > local + 0.5 / fps)
  const pose = keyPose(ov, local)
  const mask = { shape: 'none', cx: 0.5, cy: 0.5, size: 0.8, rot: 0, feather: 0, invert: false, ...ov.mask }
  const setMask = (patch, commit = true) => { onChange({ mask: { ...mask, ...patch } }); if (commit) onCommit() }
  // scale / rotation: into the keyframe at the playhead when the clip has keyframes, else the box itself
  const setPose = (patch) => onChange(keys.length ? { keys: withKey(ov, local, patch, fps) } : { rot: patch.r })
  const toggleKey = () => {
    if (here >= 0) set({ keys: keys.filter((_, i) => i !== here) })
    else set({ keys: withKey(ov, local, {}, fps) })
  }
  const anim = (which) => ({ type: 'none', beats: 1, ...ov[which] })

  const [more, setMore] = useState(() => { try { return localStorage.getItem('oddcut.more.pip') === '1' } catch { return false } })
  const toggleMore = () => setMore((m) => { try { localStorage.setItem('oddcut.more.pip', m ? '0' : '1') } catch {} ; return !m })
  const alpha = keyAlpha(ov, local)
  // the four basics: with keyframes they go into the keyframe at the playhead, else into the layer itself
  const setBasic = (patch) => {
    if (keys.length) return onChange({ keys: withKey(ov, local, patch, fps) })
    const out = {}
    if (patch.x != null) out.x = patch.x - ov.w / 2
    if (patch.y != null) out.y = patch.y - ov.h / 2
    if (patch.s != null) out.sc = patch.s
    if (patch.r != null) out.rot = patch.r
    if (patch.a != null) out.opacity = patch.a
    onChange(out)
  }
  const title = ov.fx?.kind === 'drawn' ? `手绘 · ${ov.group?.label || ''}` : ov.group?.kind === 'grow' ? `万物生长 · 第 ${ov.group.layer} 层` : ov.grid ? GRID_NAMES[ov.grid.layout] : '画中画'

  return (
    <aside className="props">
      <header className="props-head">
        {thumb ? <img src={thumb} alt="" /> : <i className="thumb-ph" />}
        <span><b>{title}</b><small>{ov.t_start.toFixed(1)} – {ov.t_end.toFixed(1)} 秒</small></span>
        <button className="icon" disabled={busy} onClick={onDelete} title="删除"><Icon name="trash" size={18} /></button>
        <button className="icon" onClick={onClose} title="关闭（Esc）"><Icon name="close" /></button>
      </header>

      <section>
        {ov.fx?.kind === 'drawn' && <DrawnProgress ov={ov} busy={busy} local={local} fps={fps} onChange={onChange} onCommit={onCommit} />}
        <Slider label="位置 X" min={-0.5} max={1.5} step={0.005} value={Math.round(pose.x * 1000) / 1000} fmt={(v) => `${Math.round(v * 100)}`} disabled={busy}
                onChange={(v) => setBasic({ x: v })} onCommit={onCommit} />
        <Slider label="位置 Y" min={-0.5} max={1.5} step={0.005} value={Math.round(pose.y * 1000) / 1000} fmt={(v) => `${Math.round(v * 100)}`} disabled={busy}
                onChange={(v) => setBasic({ y: v })} onCommit={onCommit} />
        <Slider label="缩放" min={0.2} max={3} step={0.01} value={Math.round(pose.s * 100) / 100} fmt={(v) => `${Math.round(v * 100)}%`} disabled={busy}
                onChange={(v) => setBasic({ s: v })} onCommit={onCommit} />
        <Slider label="旋转" min={-180} max={180} step={1} value={Math.round(pose.r)} fmt={(v) => `${v}°`} disabled={busy}
                onChange={(v) => setBasic({ r: v })} onCommit={onCommit} />
        <Slider label="透明度" min={0} max={1} step={0.01} value={Math.round((keys.length ? alpha : ov.opacity) * 100) / 100} fmt={(v) => `${Math.round(v * 100)}%`}
                disabled={busy} onChange={(v) => setBasic({ a: v })} onCommit={onCommit} />
        <p className="caption">{keys.length
          ? `有 ${keys.length} 个关键帧${here >= 0 ? '（播放头在第 ' + (here + 1) + ' 个上）' : ''}：改这里会记到播放头这一刻。`
          : '想让它动起来：点下面的「添加关键帧」，换个时刻再在画面上拖或改数值。'}</p>
      </section>

      <button className={`more-toggle ${more ? 'open' : ''}`} onClick={toggleMore}>更多<Icon name="chevronDown" size={16} /></button>
      {more && (
        <div className="more-body">
      {ov.fx?.kind === 'drawn' && <DrawnFx ov={ov} busy={busy} local={local} fps={fps} onSeek={onSeek} set={set} onChange={onChange}
                                           onCommit={onCommit} styles={drawnStyles} />}


      {ov.grid && (
        <section>
          <h3>宫格</h3>
          <Chips items={Object.keys(LAYOUTS).map((id) => [id, GRID_NAMES[id]])} value={ov.grid.layout} disabled={busy}
                 onPick={(layout) => {
                   const k = LAYOUTS[layout][0] * LAYOUTS[layout][1]
                   const cells = Array.from({ length: k }, (_, i) => ov.grid.cells[i] || { clip: clips[(clips.findIndex((c) => c.path === ov.grid.cells[ov.grid.cells.length - 1].clip) + 1 + i) % clips.length].path })
                   set({ grid: { ...ov.grid, layout, cells } })
                 }} />
          <div className="cell-list">
            {ov.grid.cells.map((cell, i) => (
              <label key={i} className="cell-row">
                <span className="cell-n">{i + 1}</span>
                {clipThumbs[cell.clip] && <img src={clipThumbs[cell.clip]} alt="" />}
                <select value={cell.clip} disabled={busy}
                        onChange={(e) => set({ grid: { ...ov.grid, cells: ov.grid.cells.map((c, j) => (j === i ? { clip: e.target.value } : c)) } })}>
                  {clips.map((c) => <option key={c.path} value={c.path}>{c.name}</option>)}
                </select>
              </label>
            ))}
          </div>
          <Slider label="间隔" min={0} max={0.06} step={0.002} value={ov.grid.gap ?? 0.01} fmt={(v) => `${Math.round(v * 1000) / 10}%`} disabled={busy}
                  onChange={(v) => onChange({ grid: { ...ov.grid, gap: v } })} onCommit={onCommit} />
        </section>
      )}

      {!ov.grid && (
        <section>
          <h3>蒙版</h3>
          <Chips items={MASKS.filter(([id]) => id !== 'cutout')} value={mask.shape} disabled={busy} onPick={(shape) => setMask({ shape })} />
          {mask.shape !== 'none' && mask.shape !== 'cutout' && (
            <>
              <Slider label="大小" min={0.05} max={2} step={0.01} value={mask.size} fmt={(v) => `${Math.round(v * 100)}%`} disabled={busy}
                      onChange={(v) => setMask({ size: v }, false)} onCommit={onCommit} />
              <Slider label="羽化" min={0} max={0.5} step={0.01} value={mask.feather} fmt={(v) => `${Math.round(v * 200)}%`} disabled={busy}
                      onChange={(v) => setMask({ feather: v }, false)} onCommit={onCommit} />
            </>
          )}
        </section>
      )}

      <section>
        <h3>混合</h3>
        <Chips items={BLENDS} value={ov.blend || 'normal'} disabled={busy} onPick={(blend) => set({ blend })} />
      </section>

      <section>
        <h3>入场 / 出场</h3>
        {[['anim_in', '入场'], ['anim_out', '出场']].map(([which, label]) => (
          <div key={which} className="field">
            <span className="field-label">{label}</span>
            <Chips items={which === 'anim_in' ? ANIMS : ANIMS.map(([id, l]) => [id, l.replace('入', '出').replace('渐显', '渐隐')])}
                   value={anim(which).type} disabled={busy} onPick={(type) => set({ [which]: { ...anim(which), type } })} />
          </div>
        ))}
      </section>
          {ov.group?.kind === 'grow' && (
            <section>
              <h3>万物生长</h3>
              <button className="danger wide" disabled={busy} onClick={() => onRemoveGroup(ov.group.id)}><Icon name="trash" size={16} />删除整个万物生长</button>
            </section>
          )}
        </div>
      )}
    </aside>
  )
}
