import React from 'react'
import Icon from './icons.jsx'
import { ANIMS, BLENDS, EASES, GRID_NAMES, GROW_STEPS, keyPose, LAYOUTS, MASKS, PARTS, withKey } from './pip.js'

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

// Right panel while a picture-in-picture clip (or 宫格) is selected. Every change is one undo step.
export default function OverlayProps({ ov, thumb, pip, busy, onClose, onChange, onCommit, onDelete, local, fps, onSeek,
                                       clips, clipThumbs, cutTool, setCutTool, onRemoveGroup, groupWhy }) {
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

  return (
    <aside className="props">
      <header className="props-head">
        {thumb ? <img src={thumb} alt="" /> : <i className="thumb-ph" />}
        <span><b>{ov.group ? `万物生长 · 第 ${ov.group.layer}/${ov.group.of} 层` : ov.grid ? GRID_NAMES[ov.grid.layout] : '画中画'} · 轨道 {ov.track + 1}</b>
          <small>{ov.grid ? `${ov.grid.cells.length} 段素材` : ov.name} · {ov.t_start.toFixed(1)} – {ov.t_end.toFixed(1)} 秒 · 共 {len.toFixed(1)} 秒</small></span>
        <button className="icon" onClick={onClose} title="关闭（Esc）"><Icon name="close" /></button>
      </header>

      {ov.group && (
        <section>
          <h3>万物生长 · {ov.group.label}</h3>
          {groupWhy && <p className="caption grow-why">自动选的：{groupWhy}</p>}
          <ol className="grow-steps">{GROW_STEPS.map((s, i) => <li key={i}>{s}</li>)}</ol>
          <p className="caption">这一层就是普通的画中画：改抠像、拖关键帧、换时间都可以。</p>
          <button className="danger wide" disabled={busy} onClick={() => onRemoveGroup(ov.group.id)}><Icon name="trash" size={16} />删除整个万物生长</button>
        </section>
      )}

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

      <section>
        <h3>位置</h3>
        <div className="preset-grid">
          {PRESETS.map(([id, label, f]) => (
            <button key={id} disabled={busy} onClick={() => set({ ...f(ov, pip), keys: undefined, rot: 0 })} className={`preset ${id}`}>
              <i className="preset-ico"><b /></i>{label}
            </button>
          ))}
        </div>
        <p className="caption">在预览画面上拖动移动，拖四角缩放，拖上面的圆点旋转（按住 Shift 每 15°）。</p>
      </section>

      {!ov.grid && (
        <section>
          <h3>蒙版</h3>
          <Chips items={MASKS} value={mask.shape} disabled={busy}
                 onPick={(shape) => setMask(shape === 'cutout' ? { shape, target: ov.mask?.target || { kind: 'region', parts: ['things'] }, feather: 0.004 } : { shape })} />
          {mask.shape === 'cutout' && (
            <>
              <div className="field">
                <span className="field-label">抠什么</span>
                <div className="style-chips">
                  <button className={cutTool === 'pick' ? 'on' : ''} disabled={busy} onClick={() => setCutTool(cutTool === 'pick' ? null : 'pick')}>点选物体</button>
                  {PARTS.map(([id, l]) => {
                    const on = mask.target?.kind === 'region' && (mask.target.parts || []).includes(id)
                    return (
                      <button key={id} className={on ? 'on' : ''} disabled={busy}
                              onClick={() => {
                                const cur = mask.target?.kind === 'region' ? mask.target.parts || [] : []
                                const parts = on ? cur.filter((p) => p !== id) : [...cur, id]
                                if (parts.length) setMask({ target: { kind: 'region', parts } })
                              }}>{l}</button>
                    )
                  })}
                </div>
                <p className="caption">{cutTool === 'pick' ? '在预览上点一下要抠的物体' : mask.target?.kind === 'object' ? '抠的是点选的那个物体，会跟着画面动' : '可以多选，合在一起抠出来'}</p>
              </div>
              <div className="field">
                <span className="field-label">画笔修边</span>
                <div className="style-chips">
                  <button className={cutTool === 'add' ? 'on' : ''} disabled={busy} onClick={() => setCutTool(cutTool === 'add' ? null : 'add')}>增加</button>
                  <button className={cutTool === 'sub' ? 'on' : ''} disabled={busy} onClick={() => setCutTool(cutTool === 'sub' ? null : 'sub')}>擦除</button>
                  {(mask.brush || []).length > 0 && <button disabled={busy} onClick={() => setMask({ brush: [] })}>清除画笔（{mask.brush.length}）</button>}
                </div>
              </div>
              <Slider label="羽化" min={0} max={0.1} step={0.002} value={mask.feather ?? 0.004} fmt={(v) => `${Math.round(v * 1000) / 10}%`} disabled={busy}
                      onChange={(v) => setMask({ feather: v }, false)} onCommit={onCommit} />
              <label className="switch-row">
                <span><b>反转</b><small>留下抠出部分以外的画面</small></span>
                <input type="checkbox" className="switch" checked={!!mask.invert} disabled={busy} onChange={(e) => setMask({ invert: e.target.checked })} />
              </label>
            </>
          )}
          {mask.shape !== 'none' && mask.shape !== 'cutout' && (
            <>
              <Slider label="大小" min={0.05} max={2} step={0.01} value={mask.size} fmt={(v) => `${Math.round(v * 100)}%`} disabled={busy}
                      onChange={(v) => setMask({ size: v }, false)} onCommit={onCommit} />
              <Slider label="羽化" min={0} max={0.5} step={0.01} value={mask.feather} fmt={(v) => `${Math.round(v * 200)}%`} disabled={busy}
                      onChange={(v) => setMask({ feather: v }, false)} onCommit={onCommit} />
              <Slider label="旋转" min={-180} max={180} step={1} value={mask.rot} fmt={(v) => `${v}°`} disabled={busy}
                      onChange={(v) => setMask({ rot: v }, false)} onCommit={onCommit} />
              <label className="switch-row">
                <span><b>反转</b><small>显示蒙版外面，挖掉里面</small></span>
                <input type="checkbox" className="switch" checked={!!mask.invert} disabled={busy} onChange={(e) => setMask({ invert: e.target.checked })} />
              </label>
              <p className="caption">在预览上拖蒙版中间的点移动，拖侧边的点调大小，拖上方的点旋转。</p>
            </>
          )}
        </section>
      )}

      <section>
        <h3>混合</h3>
        <Chips items={BLENDS} value={ov.blend || 'normal'} disabled={busy} onPick={(blend) => set({ blend })} />
        <Slider label="不透明度" min={0.1} max={1} step={0.05} value={ov.opacity} fmt={(v) => `${Math.round(v * 100)}%`} disabled={busy}
                onChange={(v) => onChange({ opacity: v })} onCommit={onCommit} />
      </section>

      <section>
        <h3>关键帧动画</h3>
        <div className="keybar">
          <button className="icon" disabled={busy || !prev} onClick={() => onSeek(ov.t_start + prev.t + 0.001)} title="上一个关键帧"><Icon name="prev" size={18} /></button>
          <button className={`kbtn ${here >= 0 ? 'on' : ''}`} disabled={busy} onClick={toggleKey}
                  title={here >= 0 ? '删除播放头这里的关键帧' : '在播放头这里加一个关键帧'}><i /></button>
          <button className="icon" disabled={busy || !next} onClick={() => onSeek(ov.t_start + next.t + 0.001)} title="下一个关键帧"><Icon name="next" size={18} /></button>
          <span className="keybar-text">{keys.length ? (here >= 0 ? `第 ${here + 1} 个关键帧，共 ${keys.length} 个` : `播放头不在关键帧上（共 ${keys.length} 个）`) : '还没有关键帧'}</span>
        </div>
        {keys.length > 0 && (
          <Slider label="缩放" min={0.2} max={3} step={0.01} value={Math.round(pose.s * 100) / 100} fmt={(v) => `${Math.round(v * 100)}%`} disabled={busy}
                  onChange={(v) => setPose({ s: v })} onCommit={onCommit} />
        )}
        <Slider label="旋转" min={-180} max={180} step={1} value={Math.round(pose.r)} fmt={(v) => `${v}°`} disabled={busy}
                onChange={(v) => setPose({ r: v })} onCommit={onCommit} />
        {here >= 0 && here < keys.length - 1 && (
          <div className="field">
            <span className="field-label">到下一个关键帧怎么动</span>
            <Chips items={EASES} value={keys[here].e || 'smooth'} disabled={busy}
                   onPick={(e) => set({ keys: keys.map((k, i) => (i === here ? { ...k, e } : k)) })} />
          </div>
        )}
        <Slider label="动感模糊" min={0} max={1.5} step={0.1} value={ov.mblur || 0} fmt={(v) => `${Math.round(v * 100)}%`} disabled={busy}
                onChange={(v) => onChange({ mblur: v })} onCommit={onCommit} />
        <p className="caption">{keys.length ? '有关键帧时，在画面上拖动、缩放、旋转都会记到播放头这一刻。' : '点 ◆ 在这一刻记下位置；换个时刻再拖动画面，就会动起来。'}</p>
      </section>

      <section>
        <h3>入场 / 出场</h3>
        {[['anim_in', '入场'], ['anim_out', '出场']].map(([which, label]) => (
          <div key={which} className="field">
            <span className="field-label">{label}</span>
            <Chips items={which === 'anim_in' ? ANIMS : ANIMS.map(([id, l]) => [id, l.replace('入', '出').replace('渐显', '渐隐')])}
                   value={anim(which).type} disabled={busy} onPick={(type) => set({ [which]: { ...anim(which), type } })} />
            {anim(which).type !== 'none' && (
              <div className="segmented">
                {[[0.5, '半拍'], [1, '一拍']].map(([b, l]) => (
                  <button key={b} className={anim(which).beats === b ? 'on' : ''} disabled={busy} onClick={() => set({ [which]: { ...anim(which), beats: b } })}>{l}</button>
                ))}
              </div>
            )}
          </div>
        ))}
        <p className="caption">动画时长按节拍算，正好踩在拍子上。</p>
      </section>

      <section>
        <h3>样式</h3>
        <Slider label="圆角" min={0} max={0.5} step={0.02} value={ov.radius} fmt={(v) => `${Math.round(v * 200)}%`} disabled={busy}
                onChange={(v) => onChange({ radius: v })} onCommit={onCommit} />
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
        <button className="danger wide" disabled={busy} onClick={onDelete}><Icon name="trash" size={16} />删除</button>
      </section>
    </aside>
  )
}
