import React, { useEffect, useState } from 'react'
import Icon from './icons.jsx'

const COLORS = ['#ffffff', '#000000', '#ffd400', '#ff4d6d']
const WIDTHS = [[0.01, '细'], [0.02, '中'], [0.035, '粗']]

// The tools above the preview while a clip (a shot of the main track or a 画中画) is selected, as in 剪映:
// 分割 · 复制 · 粘贴 · 抠像 · 特效 · 删除. 抠像 is one click on the thing; then only 描边 / 擦除 / 添加.
export default function Toolbar({ kind, busy, cutTool, setCutTool, hasCutout, stroke, onStroke, onSplit, onCopy, onPaste, canPaste,
                                  onDelete, canGrow, onGrow, drawnStyles, drawPick, onDrawPick }) {
  const [menu, setMenu] = useState(false)
  useEffect(() => { setMenu(false) }, [kind])
  if (!kind) {
    return (
      <div className="toolbar empty">
        <span>点时间轴上的一段，就能分割、复制、抠像</span>
        {canPaste && <button className="tool" disabled={busy} onClick={onPaste} title="粘贴到播放头这里的画中画轨道（Ctrl+V）"><Icon name="paste" size={18} />粘贴</button>}
      </div>
    )
  }
  const tool = (icon, label, onClick, on = false, title = '', disabled = false) => (
    <button className={`tool ${on ? 'on' : ''}`} disabled={busy || disabled} onClick={onClick} title={title}><Icon name={icon} size={18} />{label}</button>
  )
  const cutting = !!cutTool
  if (cutting && hasCutout && cutTool !== 'pick') {
    return (
      <div className="toolbar cutbar">
        <b className="cutbar-title"><Icon name="cutout" size={18} />抠像</b>
        <button className={`tool ${stroke ? 'on' : ''}`} disabled={busy} onClick={() => onStroke(stroke ? null : { color: '#ffffff', width: 0.02 })}>描边</button>
        {stroke && (
          <>
            {COLORS.map((c) => <button key={c} className={`swatch ${stroke.color === c ? 'on' : ''}`} style={{ background: c }} disabled={busy}
                                       onClick={() => onStroke({ ...stroke, color: c })} aria-label={`描边颜色 ${c}`} />)}
            <span className="segmented mini">
              {WIDTHS.map(([w, l]) => <button key={w} className={Math.abs(stroke.width - w) < 1e-3 ? 'on' : ''} disabled={busy} onClick={() => onStroke({ ...stroke, width: w })}>{l}</button>)}
            </span>
          </>
        )}
        <i className="tb-sep" />
        <button className={`tool ${cutTool === 'sub' ? 'on' : ''}`} disabled={busy} onClick={() => setCutTool(cutTool === 'sub' ? 'review' : 'sub')}
                title="在画面上涂，擦掉多抠的地方"><Icon name="eraser" size={18} />擦除</button>
        <button className={`tool ${cutTool === 'add' ? 'on' : ''}`} disabled={busy} onClick={() => setCutTool(cutTool === 'add' ? 'review' : 'add')}
                title="在画面上涂，把漏掉的地方加回来"><Icon name="brush2" size={18} />添加</button>
        <button className="tool" disabled={busy} onClick={() => setCutTool('pick')} title="重新点一个物体">重选</button>
        <button className="primary done" onClick={() => setCutTool(null)}>完成</button>
      </div>
    )
  }
  return (
    <div className="toolbar">
      {tool('scissors', '分割', onSplit, false, '在播放头这里切成两段（Ctrl+B）')}
      {tool('copy', '复制', onCopy, false, '复制这一段（Ctrl+C），再粘贴就成了画中画')}
      {tool('paste', '粘贴', onPaste, false, '粘贴到播放头这里的画中画轨道（Ctrl+V）', !canPaste)}
      {tool('cutout', '抠像', () => setCutTool(cutting ? null : 'pick'), cutting, '点一下画面里的物体，把它抠出来')}
      {kind === 'shot' && (
        <span className="tool-wrap">
          {tool('sparkle', '特效', () => setMenu((m) => !m), menu || !!drawPick)}
          {menu && (
            <div className="tool-menu" onMouseLeave={() => setMenu(false)}>
              <button disabled={!canGrow} onClick={() => { setMenu(false); onGrow(3) }} title={canGrow ? '' : '开场的第一段不能用'}>万物生长</button>
              {drawnStyles.map((d) => <button key={d.id} onClick={() => { setMenu(false); onDrawPick(d.id) }}>手绘 · {d.name}</button>)}
            </div>
          )}
        </span>
      )}
      {kind === 'pip' && tool('trash', '删除', onDelete, false, '删除这个画中画（Delete）')}
      <span className="tool-hint">
        {drawPick ? '点一下画面里要画成手绘的物体（Esc 取消）' : cutTool === 'pick' ? '点一下画面里要抠的物体' : ''}
      </span>
    </div>
  )
}
