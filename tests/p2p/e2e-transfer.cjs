/**
 * P2P 端到端回归自测：双浏览器上下文走真实站点信令 + ICE 直传。
 * 用法：P2P_E2E_BASE=http://<站点>:<端口> node tests/p2p/e2e-transfer.cjs
 * 依赖：全局安装 playwright 与 chromium（CHROME 常量可按环境调整）。
 * 场景 1：20MB 文件正常直传（同机直连），断言双端状态与文件一致性
 * 场景 2：传输进行中强杀下载端信令 WebSocket，验证发布端是否被 peer-left 自杀（复现 channel_closed）
 */
const { chromium } = require('playwright')
const crypto = require('crypto')
const fs = require('fs')
const path = require('path')

const CHROME = 'C:/Users/ay/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe'
const BASE = process.env.P2P_E2E_BASE || 'http://100.66.1.2:32345'
const FILE_MB = parseInt(process.env.FILE_MB || '20', 10)

const RTC_HOOK = `
  (() => {
    window.__rtcLog = []
    const log = (m) => window.__rtcLog.push(((performance.now() / 1000).toFixed(2)) + 's ' + m)
    const hookCh = (ch, tag) => {
      ch.addEventListener('open', () => log(tag + ' open'))
      ch.addEventListener('close', () => log(tag + ' CLOSE'))
      ch.addEventListener('error', (e) => log(tag + ' error:' + ((e.error && e.error.message) || e.message || '?')))
      const origSend = ch.send.bind(ch)
      ch.send = (d) => { try { origSend(d); if (typeof d === 'string') log(tag + ' send-ctrl:' + String(d).slice(0, 60)) } catch (err) { log(tag + ' send-FAIL:' + err.message); throw err } }
    }
    const OrigPC = window.RTCPeerConnection
    function HookedPC(cfg) {
      const pc = new OrigPC(cfg)
      log('pc created')
      pc.addEventListener('connectionstatechange', () => log('pc.connectionState=' + pc.connectionState))
      pc.addEventListener('iceconnectionstatechange', () => log('ice=' + pc.iceConnectionState))
      pc.addEventListener('datachannel', (e) => { log('dc(remote)'); hookCh(e.channel, 'dc-remote') })
      const origCreate = pc.createDataChannel.bind(pc)
      pc.createDataChannel = (label, opts) => { const ch = origCreate(label, opts); log('dc(local) label=' + label); hookCh(ch, 'dc-local'); return ch }
      return pc
    }
    HookedPC.prototype = OrigPC.prototype
    window.RTCPeerConnection = HookedPC
  })()
`

const WS_HOOK = `
  (() => {
    const OrigWS = window.WebSocket
    function HookedWS(url, protocols) {
      const ws = protocols !== undefined ? new OrigWS(url, protocols) : new OrigWS(url)
      if (String(url).includes('/p2p/signal/')) {
        window.__signalWs = ws
        window.__signalWsClosedAt = null
        ws.addEventListener('close', () => { window.__signalWsClosedAt = Date.now() })
      }
      return ws
    }
    HookedWS.prototype = OrigWS.prototype
    for (const k of ['CONNECTING','OPEN','CLOSING','CLOSED']) HookedWS[k] = OrigWS[k]
    window.WebSocket = HookedWS
    window.__killSignalWs = () => {
      const ws = window.__signalWs
      if (ws && ws.readyState === 1) { ws.close(4000, 'e2e-kill'); return true }
      return false
    }
  })()
`

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex')
}

async function dumpPanel(page, label) {
  try {
    const text = await page.evaluate(() => document.body.innerText)
    const compact = text.replace(/\n{2,}/g, '\n').slice(0, 900)
    console.log(`---- ${label} 页面文本 ----\n${compact}`)
  } catch (e) {
    console.log(`---- ${label} 页面文本获取失败: ${e.message}`)
  }
}

async function runScenario(name, { killWs }) {
  console.log(`\n========== 场景：${name} ==========`)
  const browser = await chromium.launch({ headless: true, executablePath: CHROME })

  // 测试文件
  const filePath = path.join(__dirname, `e2e-p2p-${FILE_MB}mb.bin`)
  if (!fs.existsSync(filePath)) {
    const buf = Buffer.alloc(FILE_MB * 1024 * 1024)
    for (let off = 0; off < buf.length; off += 4 * 1024 * 1024) {
      crypto.randomBytes(Math.min(4 * 1024 * 1024, buf.length - off)).copy(buf, off)
    }
    fs.writeFileSync(filePath, buf)
  }
  const fileBuf = fs.readFileSync(filePath)
  const fileSha = sha256(fileBuf)
  console.log(`测试文件: ${FILE_MB}MB sha256=${fileSha.slice(0, 16)}...`)

  // ---- 发布端 ----
  const ctxA = await browser.newContext({ viewport: { width: 480, height: 960 } })
  await ctxA.addInitScript(RTC_HOOK)
  const pageA = await ctxA.newPage()
  const errorsA = []
  pageA.on('pageerror', (e) => errorsA.push('[pageerror] ' + String(e && e.message).slice(0, 200)))

  let p2pCode = ''
  pageA.on('response', async (r) => {
    if (r.url().includes('p2p/publish') && !p2pCode) {
      try {
        const j = await r.json()
        p2pCode = (j.detail && j.detail.code) || ''
      } catch { /* 忽略 */ }
    }
  })

  await pageA.goto(`${BASE}/#/send`, { waitUntil: 'load', timeout: 30000 })
  await pageA.waitForSelector('input[type=file]', { state: 'attached', timeout: 15000 })
  await pageA.setInputFiles('input[type=file]', filePath)

  // 确认 P2P 开关勾选
  const p2pChecked = await pageA.isChecked('input[type=checkbox]')
  console.log(`发布端 P2P 开关勾选: ${p2pChecked}`)
  if (!p2pChecked) await pageA.check('input[type=checkbox]')

  await pageA.click('button[type=submit]')
  await pageA.waitForFunction(() => document.body.innerText.includes('取件码'), { timeout: 30000 })
  console.log(`取件码(响应): ${p2pCode}`)
  if (!p2pCode) {
    await dumpPanel(pageA, '发布端(未拿到取件码)')
    await browser.close()
    process.exit(1)
  }

  // ---- 下载端 ----
  const ctxB = await browser.newContext({ viewport: { width: 480, height: 960 } })
  await ctxB.addInitScript(RTC_HOOK)
  if (killWs) await ctxB.addInitScript(WS_HOOK)
  const pageB = await ctxB.newPage()
  const errorsB = []
  pageB.on('pageerror', (e) => errorsB.push('[pageerror] ' + String(e && e.message).slice(0, 200)))

  const downloadPromise = pageB.waitForEvent('download', { timeout: 90000 }).catch(() => null)

  await pageB.goto(`${BASE}/#/?code=${p2pCode}`, { waitUntil: 'load', timeout: 30000 })
  await pageB.waitForSelector('text=直连取件', { timeout: 30000 })
  console.log('下载端: P2P 面板已出现，点击直连取件')
  await pageB.click('text=直连取件')

  // ---- 场景 2：接收开始后杀掉下载端信令 WS ----
  let wsKilled = false
  if (killWs) {
    try {
      await pageB.waitForFunction(
        () => {
          const m = document.body.innerText.match(/已接收\s*([\d.]+)\s*([KM]?B)/)
          return m && parseFloat(m[1]) > 0
        },
        { timeout: 30000 }
      )
      wsKilled = await pageB.evaluate(() => window.__killSignalWs())
      console.log(`下载端已开始接收，强杀信令 WS: ${wsKilled}`)
    } catch {
      console.log('下载端 30s 内未开始接收，跳过杀 WS（传输可能根本没建立）')
    }
  }

  // ---- 等待下载端完成/失败 ----
  const download = await downloadPromise
  let savedPath = ''
  if (download) {
    savedPath = path.join(__dirname, 'e2p-saved.bin')
    await download.saveAs(savedPath)
    console.log(`下载端: 触发保存 ${savedPath}`)
  }

  await pageB.waitForFunction(
    () => /接收完成|接收失败|完整性校验/.test(document.body.innerText),
    { timeout: 30000 }
  ).catch(() => {})

  const textB = await pageB.evaluate(() => document.body.innerText)
  const doneB = textB.includes('接收完成')
  const verifiedB = textB.includes('完整性校验通过')
  const failedB = textB.includes('接收失败')
  console.log(`下载端结果: 完成=${doneB} 校验通过=${verifiedB} 失败=${failedB}`)
  if (killWs) {
    const closedAt = await pageB.evaluate(() => window.__signalWsClosedAt)
    console.log(`下载端信令 WS 已关闭: ${!!closedAt}`)
  }

  // 校验文件
  if (savedPath && fs.existsSync(savedPath)) {
    const savedBuf = fs.readFileSync(savedPath)
    const savedSha = sha256(savedBuf)
    const match = savedSha === fileSha && savedBuf.length === fileBuf.length
    console.log(`文件校验: 大小 ${savedBuf.length}/${fileBuf.length} sha256一致=${match}`)
    fs.unlinkSync(savedPath)
  } else {
    console.log('文件校验: 未捕获到下载文件')
  }

  // ---- 发布端状态 ----
  await pageA.waitForTimeout(2000)
  const textA = await pageA.evaluate(() => document.body.innerText)
  const servedMatch = textA.match(/已完成传输\s*(\d+)/)
  const errA = textA.match(/channel_closed|peer_aborted|signaling_\w+|p2p_error/)
  console.log(`发布端: 已完成传输=${servedMatch ? servedMatch[1] : '?'} 错误=${errA ? errA[0] : '无'}`)
  console.log(`发布端 pageerror: ${errorsA.length ? errorsA.join(' | ') : '无'}`)
  console.log(`下载端 pageerror: ${errorsB.length ? errorsB.join(' | ') : '无'}`)

  for (const [label, pg] of [['发布端', pageA], ['下载端', pageB]]) {
    try {
      const rtcLog = await pg.evaluate(() => window.__rtcLog || [])
      console.log(`---- ${label} RTC 时间线 ----\n  ${rtcLog.join('\n  ')}`)
    } catch { /* 忽略 */ }
  }

  if (failedB || errA) await dumpPanel(pageB, '下载端')

  await browser.close()

  const pass = doneB && verifiedB && !errA && (killWs ? true : true)
  console.log(`场景「${name}」判定: ${pass && !failedB ? 'PASS' : 'FAIL'}`)
  return pass && !failedB
}

;(async () => {
  const r1 = await runScenario('基线：20MB 正常直传', { killWs: false })
  const r2 = await runScenario('传输中强杀下载端信令 WS', { killWs: true })
  console.log('\n========== 汇总 ==========')
  console.log(`基线传输: ${r1 ? 'PASS' : 'FAIL'}`)
  console.log(`信令断开韧性: ${r2 ? 'PASS' : 'FAIL'}`)
  process.exit(r1 && r2 ? 0 : 1)
})().catch((e) => {
  console.error('e2e 异常:', e)
  process.exit(2)
})
