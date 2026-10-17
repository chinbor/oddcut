import React from 'react'
import Icon from './icons.jsx'

const ASPECTS = [
  ['9:16', '9:16', '抖音 / 小红书'], ['3:4', '3:4', '小红书图文'], ['1:1', '1:1', '方形'],
  ['4:5', '4:5', 'Instagram'], ['16:9', '16:9', '横屏 / B 站'],
]

// 新建: footage + music, aspect + duration, style; one button. Everything else is edited shot by shot later.
export default function NewProject({ form, setForm, templates, folderInfo, musicInfo, onPickFolder, onPickMusic,
                                     onExamples, onGenerate, canGenerate, busy, progress, hasProject, onBack }) {
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))
  return (
    <div className="new-page">
      <div className="new-col">
        {hasProject && (
          <button className="ghost back" onClick={onBack}><Icon name="back" size={18} />回到编辑器</button>
        )}
        <h1>做一条新短片</h1>
        <p className="lead">选好素材和音乐，点生成。贴纸、手绘、切点都可以在编辑器里逐个镜头改。</p>

        <section className="step">
          <h2><span className="step-n">1</span>素材和音乐</h2>
          <div className="big-picks">
            <button className={`big-pick ${form.clips ? 'set' : ''}`} onClick={onPickFolder} disabled={busy}>
              <Icon name="folder" size={28} />
              <span><b>视频素材文件夹</b><small>{form.clips ? `${folderInfo?.name || ''} · ${folderInfo?.count ?? '…'} 段视频` : '选一个放视频的文件夹'}</small></span>
            </button>
            <button className={`big-pick ${form.music ? 'set' : ''}`} onClick={onPickMusic} disabled={busy}>
              <Icon name="music" size={28} />
              <span><b>背景音乐</b><small>{form.music ? `${musicInfo?.name || ''}${musicInfo?.tempo ? ` · ${Math.round(musicInfo.tempo)} BPM` : ''}` : '选一首歌（mp3 / wav / m4a）'}</small></span>
            </button>
          </div>
          <button className="link" onClick={onExamples} disabled={busy}>没有素材？用示例素材试一下</button>
        </section>

        <section className="step">
          <h2><span className="step-n">2</span>画幅和时长</h2>
          <div className="aspect-cards">
            {ASPECTS.map(([id, label, hint]) => {
              const [w, h] = id.split(':').map(Number)
              const k = 44 / Math.max(w, h)
              return (
                <button key={id} className={`aspect-card ${form.aspect === id ? 'on' : ''}`} onClick={() => set('aspect', id)} disabled={busy}>
                  <i style={{ width: w * k, height: h * k }} />
                  <b>{label}</b>
                  <small>{hint}</small>
                </button>
              )
            })}
          </div>
          <label className="duration">
            <span>时长</span>
            <input type="range" min="10" max="40" value={form.duration} onChange={(e) => set('duration', Number(e.target.value))} disabled={busy} />
            <b>{form.duration} 秒</b>
          </label>
        </section>

        <section className="step">
          <h2><span className="step-n">3</span>风格</h2>
          {templates.map((t) => (
            <button key={t.id} className={`tpl-card ${form.template === t.id ? 'on' : ''}`} onClick={() => set('template', t.id)} disabled={busy}>
              <b>{t.name}</b>
              <small>{t.description}</small>
            </button>
          ))}
          <div className="switches">
            <label className="switch-row">
              <span><b>自动加贴纸</b><small>给画面里的东西加表情和卡通小物件</small></span>
              <input type="checkbox" className="switch" checked={form.autoStickers} onChange={(e) => set('autoStickers', e.target.checked)} disabled={busy} />
            </label>
            <label className="switch-row">
              <span><b>手绘开场</b><small>素材里有合适的镜头时，开头先画再变回实拍</small></span>
              <input type="checkbox" className="switch" checked={form.drawn} onChange={(e) => set('drawn', e.target.checked)} disabled={busy} />
            </label>
            <label className="switch-row">
              <span><b>全片万物生长</b><small>每个镜头按画面里的东西分层，踩着节拍从下往上长出来，盖住上一个镜头</small></span>
              <input type="checkbox" className="switch" checked={!!form.grow} onChange={(e) => set('grow', e.target.checked)} disabled={busy} />
            </label>
          </div>
          <p className="note">用哪些贴纸、哪种手绘风格，生成后在编辑器左边的素材库里换。</p>
        </section>

        <button className="primary huge" disabled={!canGenerate || busy} onClick={onGenerate}>
          {busy ? (progress?.label || '正在生成…') : '生成剪辑方案'}
        </button>
        {busy && progress && (
          <div className="gen-progress"><i style={{ width: `${Math.round(progress.fraction * 100)}%` }} /></div>
        )}
        <p className="caption center">第一次分析这些素材约 1 分钟，之后几秒就好</p>
      </div>
    </div>
  )
}
