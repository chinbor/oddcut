// Picture-in-picture helpers shared by the preview box, the properties panel and the timeline.
// The pose formulas are the same as code/oddcut/pip.py (key_pose, the mask shapes), so what is drawn on the
// preview matches the render.

export const LAYOUTS = { '2v': [1, 2], '2h': [2, 1], '3v': [1, 3], 4: [2, 2], 9: [3, 3] }
export const GRID_NAMES = { '2v': '上下分屏', '2h': '左右分屏', '3v': '三宫格', 4: '四宫格', 9: '九宫格' }
export const MASKS = [['none', '无'], ['circle', '圆形'], ['heart', '心形'], ['star', '星形'], ['rect', '圆角矩形'], ['linear', '线性'], ['mirror', '镜面']]
export const BLENDS = [['normal', '正常'], ['screen', '滤色'], ['multiply', '正片叠底'], ['overlay', '叠加'], ['lighten', '变亮'], ['darken', '变暗'], ['add', '相加']]
export const ANIMS = [['none', '无'], ['pop', '弹入'], ['slide_l', '从左滑入'], ['slide_r', '从右滑入'], ['slide_u', '从上滑入'], ['slide_d', '从下滑入'], ['zoom', '缩放'], ['fade', '渐显'], ['spin', '旋入']]

const smooth = (u) => { const v = Math.min(1, Math.max(0, u)); return v * v * (3 - 2 * v) }

// centre (0..1 of the frame), scale (1 = the box) and rotation (degrees) at `local` s into the overlay
export function keyPose(o, local) {
  const keys = o.keys || []
  if (!keys.length) return { x: o.x + o.w / 2, y: o.y + o.h / 2, s: 1, r: o.rot || 0 }
  if (local <= keys[0].t) return { ...keys[0] }
  for (let i = 0; i + 1 < keys.length; i++) {
    const a = keys[i], b = keys[i + 1]
    if (local <= b.t) {
      const u = smooth((local - a.t) / Math.max(1e-6, b.t - a.t))
      return { x: a.x + (b.x - a.x) * u, y: a.y + (b.y - a.y) * u, s: a.s + (b.s - a.s) * u, r: a.r + (b.r - a.r) * u }
    }
  }
  return { ...keys[keys.length - 1] }
}

// a keyframe at `local` with `patch` applied (made from the current pose if there is none there yet)
export function withKey(o, local, patch, fps = 30) {
  const keys = [...(o.keys || [])]
  let j = keys.findIndex((k) => Math.abs(k.t - local) < 0.5 / fps)
  if (j < 0) {
    const p = keyPose(o, local)
    keys.push({ t: Math.round(local * fps) / fps, x: p.x, y: p.y, s: p.s, r: p.r })
    keys.sort((a, b) => a.t - b.t)
    j = keys.findIndex((k) => Math.abs(k.t - local) < 0.5 / fps)
  }
  const { t, ...pose } = patch                     // a pose read from another keyframe carries its time: not wanted
  keys[j] = { ...keys[j], ...pose }
  return keys
}

// outline of the mask in box pixels (bw x bh), as an SVG path
export function maskPath(o, bw, bh) {
  const m = o.mask || {}
  const shape = m.shape || 'none'
  if (shape === 'none') return ''
  const cx = (m.cx ?? 0.5) * bw, cy = (m.cy ?? 0.5) * bh
  const short = Math.min(bw, bh)
  const ang = ((m.rot || 0) * Math.PI) / 180
  const rot = ([x, y]) => [cx + x * Math.cos(ang) - y * Math.sin(ang), cy + x * Math.sin(ang) + y * Math.cos(ang)]
  const poly = (pts) => `M${pts.map((p) => rot(p).map((v) => v.toFixed(1)).join(',')).join('L')}Z`
  const size = (m.size ?? 0.8)
  if (shape === 'rect') {
    const hw = (size * bw) / 2, hh = (size * bh) / 2
    return poly([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]])
  }
  const r = (size * short) / 2
  if (shape === 'circle') return poly(Array.from({ length: 48 }, (_, k) => [r * Math.cos((k * Math.PI) / 24), r * Math.sin((k * Math.PI) / 24)]))
  if (shape === 'heart') {
    return poly(Array.from({ length: 64 }, (_, k) => {
      const t = (k / 64) * 2 * Math.PI
      const x = 16 * Math.sin(t) ** 3
      const y = -(13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t))
      return [(x / 17) * r, ((y + 2.5) / 17) * r]
    }))
  }
  if (shape === 'star') {
    return poly(Array.from({ length: 10 }, (_, k) => {
      const rr = (k % 2 === 0 ? 1 : 0.45) * r, a = -Math.PI / 2 + (k * Math.PI) / 5
      return [rr * Math.cos(a), rr * Math.sin(a)]
    }))
  }
  const L = Math.hypot(bw, bh)                 // linear: the dividing line; mirror: the band's two edges
  const line = (off) => { const a = rot([-L, off]), b = rot([L, off]); return `M${a.join(',')}L${b.join(',')}` }
  return shape === 'linear' ? line(0) : `${line(-r)} ${line(r)}`
}

// pixel rectangles of a grid's cells inside a box of bw x bh
export function gridCells(o, bw, bh) {
  const [cols, rows] = LAYOUTS[o.grid.layout]
  const gap = (o.grid.gap ?? 0.01) * Math.min(bw, bh)
  const cw = (bw - gap * (cols - 1)) / cols, ch = (bh - gap * (rows - 1)) / rows
  const out = []
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) out.push({ x: c * (cw + gap), y: r * (ch + gap), w: cw, h: ch })
  return out
}
