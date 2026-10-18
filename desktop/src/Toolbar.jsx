import React, { useEffect, useState } from 'react'
import Icon from './icons.jsx'

// The tools above the preview while a clip (a shot of the main track or a 画中画) is selected - one job each,
// as in 剪映: 分割 · 抠像 · 画中画 · 特效. (The ◆ keyframe button sits next to the play button.)
export default function Toolbar({ kind, busy, cutTool, setCutTool, hasCutout, onSplit, onDup, onFromLibrary,
                                  canGrow, onGrow, drawnStyles, drawPick, onDrawPick }) {
  const [menu, setMenu] = useState(null)              // 'pip' | 'fx'
  const [layers, setLayers] = useState(3)
  useEffect(() => { setMenu(null) }, [kind])
  if (!kind) return <div className="toolbar empty">选中时间轴上的片段，就能分割、抠像、做画中画和加特效</div>
  const cutting = !!cutTool
  const tool = (name, icon, label, onClick, on = false, title = '') => (
    <button className={`tool ${on ? 'on' : ''}`} disabled={busy} onClick={onClick} title={title}><Icon name={icon} size={18} />{label}</button>
  )
  return (
    <div className="toolbar">
      {tool('split', 'scissors', '分割', onSplit, false, '在播放头这里把片段切成两段（Ctrl+B）')}
      {tool('cut', 'cutout', '抠像', () => setCutTool(cutTool === 'pick' ? null : 'pick'), cutting, '点一下画面里的主体，把它抠出来')}
      <span className="tool-wrap">
        {tool('pip', 'layers', '画中画', () => setMenu(menu === 'pip' ? null : 'pip'), menu === 'pip')}
        {menu === 'pip' && (
          <div className="tool-menu" onMouseLeave={() => setMenu(null)}>
            <button onClick={() => { setMenu(null); onDup() }}>复制当前片段为画中画</button>
            <button onClick={() => { setMenu(null); onFromLibrary() }}>从素材添加画中画</button>
          </div>
        )}
      </span>
      <span className="tool-wrap">
        {tool('fx', 'sparkle', '特效', () => setMenu(menu === 'fx' ? null : 'fx'), menu === 'fx' || !!drawPick)}
        {menu === 'fx' && (
          <div className="tool-menu wide" onMouseLeave={() => setMenu(null)}>
            <div className="tm-row">
              <button className="tm-main" disabled={!canGrow} onClick={() => { setMenu(null); onGrow(layers) }}
                      title={canGrow ? '在这个片段开头做万物生长' : '选中主轨上的一个片段（不是开场）才能用'}>
                <b>万物生长</b><small>下一个镜头一层层从下往上长出来</small></button>
              <span className="segmented mini">
                {[3, 4].map((n) => <button key={n} className={layers === n ? 'on' : ''} onClick={() => setLayers(n)}>{n} 层</button>)}
              </span>
            </div>
            <div className="tm-row col">
              <span className="tm-label"><b>手绘</b><small>选风格，再点画面里要画的物体</small></span>
              <span className="style-chips">
                {drawnStyles.map((d) => <button key={d.id} onClick={() => { setMenu(null); onDrawPick(d.id) }}>{d.name}</button>)}
              </span>
            </div>
          </div>
        )}
      </span>
      <span className="tool-hint">
        {drawPick ? '点一下画面里要画成手绘的物体（Esc 取消）'
          : cutTool === 'pick' ? (kind === 'shot' ? '点一下画面里的主体：抠出来放到画中画上，可以单独动' : '点一下画面里要留下的主体')
          : cutTool === 'review' ? '抠好了。边缘不对就点「修整」涂一涂；想换个主体，再点一下「抠像」'
          : cutTool === 'add' ? '在画面上涂：把漏掉的部分加回来' : cutTool === 'sub' ? '在画面上涂：擦掉多抠的部分' : ''}
      </span>
      {cutting && hasCutout && (
        <span className="tool-sub">
          <button className={cutTool === 'add' || cutTool === 'sub' ? 'on' : ''} onClick={() => setCutTool(cutTool === 'add' || cutTool === 'sub' ? 'review' : 'add')}>
            <Icon name="brush2" size={16} />修整</button>
          {(cutTool === 'add' || cutTool === 'sub') && (
            <>
              <button className={cutTool === 'add' ? 'on' : ''} onClick={() => setCutTool('add')}>增加</button>
              <button className={cutTool === 'sub' ? 'on' : ''} onClick={() => setCutTool('sub')}>擦除</button>
            </>
          )}
          <button onClick={() => setCutTool(null)}>完成</button>
        </span>
      )}
    </div>
  )
}
