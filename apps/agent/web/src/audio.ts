// 语音前台：播放队列 + 麦克风采集 + 能量 VAD + 打断检测 + WAV 编码

/* ---------------- 播放：按 turn 顺序播放 TTS 片段，可立即停止 ---------------- */
export class Player {
  ctx: AudioContext | null = null
  private queue: Promise<void> = Promise.resolve()
  private current: AudioBufferSourceNode | null = null
  private gen = 0
  speaking = false
  onSpeaking: (on: boolean) => void = () => {}
  muted = false
  private pending = 0

  ensure() {
    if (!this.ctx) this.ctx = new AudioContext()
    if (this.ctx.state === 'suspended') void this.ctx.resume()
    return this.ctx
  }

  enqueue(b64: string) {
    if (this.muted) return
    const ctx = this.ensure()
    const g = this.gen
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
    const decoded = ctx.decodeAudioData(bytes.buffer.slice(0))
    this.pending++
    this.setSpeaking(true)
    this.queue = this.queue.then(async () => {
      try {
        const buf = await decoded
        if (g !== this.gen) return
        if (ctx.state === 'suspended') await ctx.resume()
        await new Promise<void>(res => {
          const src = ctx.createBufferSource()
          src.buffer = buf
          src.connect(ctx.destination)
          // 看门狗：onended 偶发不触发（音频设备切换、上下文挂起）时也能继续，避免一直"播报中"
          const t = setTimeout(res, buf.duration * 1000 + 1500)
          src.onended = () => { clearTimeout(t); res() }
          this.current = src
          src.start()
        })
      } catch { /* 解码失败跳过 */ } finally {
        if (g === this.gen) { // 已被 stop() 作废的片段不再动计数
          this.current = null
          this.pending--
          if (this.pending <= 0) { this.pending = 0; this.setSpeaking(false) }
        }
      }
    })
  }

  stop() {
    this.gen++
    try { this.current?.stop() } catch { /* noop */ }
    this.current = null
    this.queue = Promise.resolve()
    this.pending = 0
    this.setSpeaking(false)
  }

  private setSpeaking(on: boolean) {
    if (this.speaking !== on) { this.speaking = on; this.onSpeaking(on) }
  }
}

/* ---------------- 采集 + VAD ---------------- */
export interface MicEvents {
  onSpeechStart: () => void // 开始说话（用于打断与"识别中"占位）
  onUtterance: (wav: Blob) => void // 一句话结束
  onDiscard: () => void // 太短、视为噪声
  onLevel?: (rms: number) => void
}

export class Mic {
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private node: ScriptProcessorNode | null = null
  private src: MediaStreamAudioSourceNode | null = null // 持有引用，防止被回收后采集静默停止
  private sr = 48000
  private floor = 0.004
  private inSpeech = false
  private voiced = 0
  private silence = 0
  private buf: Float32Array[] = []
  private pre: Float32Array[] = []
  private manual = false // 按住说话
  continuous = false // 免提监听（VAD 自动切句）
  ttsPlaying = () => false
  active = false

  private ev: MicEvents
  constructor(ev: MicEvents) { this.ev = ev }

  async start() {
    if (this.active) return
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    })
    this.ctx = new AudioContext()
    this.sr = this.ctx.sampleRate
    this.src = this.ctx.createMediaStreamSource(this.stream)
    this.node = this.ctx.createScriptProcessor(2048, 1, 1)
    this.node.onaudioprocess = e => this.frame(new Float32Array(e.inputBuffer.getChannelData(0)))
    this.src.connect(this.node)
    this.node.connect(this.ctx.destination)
    this.active = true
  }

  stop() {
    this.node?.disconnect()
    this.stream?.getTracks().forEach(t => t.stop())
    void this.ctx?.close()
    this.ctx = this.stream = this.node = this.src = null
    this.active = false
    this.inSpeech = false
  }

  pttDown() { this.manual = true; this.beginSpeech() }
  pttUp() { if (this.manual) { this.manual = false; this.endSpeech(true) } }

  private beginSpeech() {
    if (this.inSpeech) return
    this.inSpeech = true
    this.buf = [...this.pre]
    this.silence = 0
    this.ev.onSpeechStart()
  }

  private endSpeech(force = false) {
    if (!this.inSpeech) return
    this.inSpeech = false
    const total = this.buf.reduce((n, b) => n + b.length, 0)
    const chunks = this.buf
    this.buf = []
    if (!force && total < this.sr * 0.35) { this.ev.onDiscard(); return }
    if (total < this.sr * 0.2) { this.ev.onDiscard(); return }
    this.ev.onUtterance(encodeWav(chunks, this.sr, 16000))
  }

  private frame(x: Float32Array) {
    let e = 0
    for (let i = 0; i < x.length; i++) e += x[i] * x[i]
    const rms = Math.sqrt(e / x.length)
    this.ev.onLevel?.(rms)
    this.pre.push(x)
    if (this.pre.length > 12) this.pre.shift() // ~500ms 预录（防止切掉句首）
    if (this.manual) { this.buf.push(x); return }
    if (!this.continuous) return
    // 播报中提高门限，降低回声误打断
    const k = this.ttsPlaying() ? 6 : 3
    const th = Math.max(this.floor * k, this.ttsPlaying() ? 0.03 : 0.012)
    if (!this.inSpeech) {
      // 噪声底：安静时快速跟随；持续偏吵时缓慢上调，避免底噪一直被当成人声
      this.floor = rms < th ? this.floor * 0.95 + rms * 0.05 : this.floor * 0.998 + rms * 0.002
      this.voiced = rms > th ? this.voiced + 1 : 0
      if (this.voiced >= 3) this.beginSpeech() // 约 130ms 持续人声
    } else {
      this.buf.push(x)
      this.silence = rms < th * 0.7 ? this.silence + 1 : 0
      const dur = this.buf.length * x.length / this.sr
      if (this.silence * x.length / this.sr > 0.75 || dur > 15) this.endSpeech()
    }
  }
}

function encodeWav(chunks: Float32Array[], inRate: number, outRate: number): Blob {
  const total = chunks.reduce((n, b) => n + b.length, 0)
  const pcm = new Float32Array(total)
  let o = 0
  for (const c of chunks) { pcm.set(c, o); o += c.length }
  const ratio = inRate / outRate
  const n = Math.floor(total / ratio)
  const view = new DataView(new ArrayBuffer(44 + n * 2))
  const w = (p: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(p + i, s.charCodeAt(i)) }
  w(0, 'RIFF'); view.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, outRate, true); view.setUint32(28, outRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  w(36, 'data'); view.setUint32(40, n * 2, true)
  for (let i = 0; i < n; i++) {
    // 简单平均降采样
    const a = Math.floor(i * ratio), b = Math.min(total, Math.floor((i + 1) * ratio))
    let s = 0
    for (let j = a; j < b; j++) s += pcm[j]
    s = Math.max(-1, Math.min(1, s / Math.max(1, b - a)))
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([view], { type: 'audio/wav' })
}

/* ---------------- 唤醒词（识别文本匹配，PRD FR-01 的软件近似） ---------------- */
const WAKE = /^(你好|嗨|hi|hello|hey)[，,\s]*(小禾|小和|小河|xiao\s*he)[，,。!！\s]*/i
export function stripWake(text: string): { woke: boolean; rest: string } {
  const m = text.match(WAKE)
  if (!m) return { woke: false, rest: text }
  return { woke: true, rest: text.slice(m[0].length).trim() }
}
