import React, { useCallback, useEffect, useState } from 'react'
import { api, notifyDataChanged, onDataChanged } from '../api'

const surface_panel = true
export default function Bags() {
  const [bags, setBags] = useState([])
  const [chutes, setChutes] = useState([])
  const [scans, setScans] = useState([])
  const [report, setReport] = useState(null)
  const [book, setBook] = useState(null)
  const [chuteId, setChuteId] = useState('')
  const [bagId, setBagId] = useState('')
  const [scanId, setScanId] = useState('')
  const [idemKey, setIdemKey] = useState('')
  const [receipt, setReceipt] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    const [bagRows, chuteRows, scanRows, consistency] = await Promise.all([
      api('/封袋'),
      api('/口门'),
      api('/扫描'),
      api('/封袋/一致性'),
    ])
    setBags(bagRows)
    setChutes(chuteRows)
    setScans(scanRows)
    setReport(consistency)
    setChuteId((prev) => prev || (chuteRows[0] ? String(chuteRows[0].id) : ''))
    setBagId((prev) => {
      if (prev) return prev
      const firstOpen = bagRows.find((b) => b.status === '集包中')
      return firstOpen ? String(firstOpen.id) : ''
    })
  }, [])

  useEffect(() => {
    load().catch((e) => setErr(e.message))
    return onDataChanged(() => { load().catch((e) => setErr(e.message)) })
  }, [load])

  const openBag = async () => {
    setErr(''); setMsg(''); setReceipt(null)
    try {
      const b = await api('/封袋/开袋', { method: 'POST', body: JSON.stringify({ 口门编号: Number(chuteId) }) })
      setMsg(`已开袋 ${b.code}`); setBagId(String(b.id)); await load(); notifyDataChanged({ type: 'bag-opened' })
    } catch (e) { setErr(e.message) }
  }

  const add = async (event) => {
    if (event) event.preventDefault()
    setErr(''); setMsg(''); setReceipt(null)
    if (!bagId) { setErr('请先填写或开一个集包袋编号'); return }
    if (!scanId) { setErr('请选择扫描件'); return }
    try {
      const payload = { 扫描编号: Number(scanId) }
      if (idemKey.trim()) payload.准入键 = idemKey.trim()
      const result = await api(`/封袋/${bagId}/入袋`, { method: 'POST', body: JSON.stringify(payload) })
      setReceipt(result)
      setMsg(result.重放 ? `准入结局重放：袋件数 ${result.袋件数}` : `入袋成功：准入#${result.准入编号}，袋件数 ${result.袋件数}`)
      setIdemKey('')
      await load()
      notifyDataChanged({ type: 'admitted', admissionId: result.准入编号 })
    } catch (e) {
      setErr(e.message)
      setReceipt(e.data || null)
      await load().catch(() => {})
      notifyDataChanged({ type: 'admission-rejected' })
    }
  }

  const seal = async () => {
    setErr(''); setMsg('')
    try {
      const b = await api(`/封袋/${bagId}/封口`, { method: 'POST' })
      setMsg(`封袋成功：${b.code}`); await load(); notifyDataChanged({ type: 'sealed' })
    } catch (e) { setErr(e.message); if (e.data) setReceipt(e.data) }
  }

  const showBook = async (id) => {
    setErr('')
    try { setBook(await api(`/封袋/${id}/册`)) } catch (e) { setErr(e.message) }
  }

  const rollbackDrift = async (id) => {
    setErr(''); setMsg('')
    try {
      const result = await api(`/封袋/${id}/回滚异常`, { method: 'POST' })
      setMsg(`已回滚 ${result.已回滚.length} 条异常挂袋；阻断 ${result.阻断.length} 条`)
      await load()
      notifyDataChanged({ type: 'admission-rollback' })
    } catch (e) { setErr(e.message) }
  }

  const replayReceipt = async () => {
    if (!receipt?.准入编号) return
    try {
      const result = await api(`/封袋/准入/${receipt.准入编号}/重放`, { method: 'POST' })
      setMsg(`已重放准入#${receipt.准入编号}`)
      setReceipt(result.响应 || result)
      notifyDataChanged({ type: 'admission-replayed' })
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className={surface_panel ? "panel" : "panel"}>
      <h2>封袋 / 集包袋册</h2>
      <div className="policy-banner">
        <strong>当前唯一准入口径：仅已路由可入袋。</strong>
        <span>待路由、错分回流或口门不一致时整笔失败；不增袋件数、不改扫描态，失败记录可在审计中重放。</span>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{typeof err === 'string' ? err : JSON.stringify(err)}</div>}

      <div className="row">
        <label>口门
          <select value={chuteId} onChange={(e) => setChuteId(e.target.value)}>
            {chutes.map((c) => <option key={c.id} value={c.id}>{c.code} {c.title}</option>)}
          </select>
        </label>
        <button type="button" onClick={openBag}>开袋</button>
      </div>

      <form className="row" onSubmit={add}>
        <label>袋编号<input value={bagId} onChange={(e) => setBagId(e.target.value)} /></label>
        <label className="grow">已路由扫描 / 待路由拒入验证
          <select aria-label="已路由扫描" value={scanId} onChange={(e) => setScanId(e.target.value)}>
            <option value="">选择扫描（含状态/口门/袋码）</option>
            {scans.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id} · {s.barcode} · {s.status} · {s.口门码 || '无口门'}{s.袋码 ? ` · ${s.袋码}` : ''}
              </option>
            ))}
          </select>
        </label>
        <label>准入键（可重放）<input value={idemKey} onChange={(e) => setIdemKey(e.target.value)} placeholder="留空也可自然重放" /></label>
        <button type="submit">入袋</button>
        <button type="button" className="secondary" onClick={seal}>封口</button>
      </form>

      {receipt && (
        <div className={`receipt ${receipt.允许 === false ? 'bad' : 'good'}`}>
          <div><strong>准入回执</strong> 编号 {receipt.准入编号 ?? '-'} · {receipt.重放 ? '重放' : '首次结局'}</div>
          <div>扫描状态：{receipt.扫描状态 || receipt.status || '-'}；袋件数：{receipt.袋件数 ?? receipt.袋?.件数 ?? '-'}；原因：{receipt.原因 || receipt.detail || '允许入袋'}</div>
          {receipt.准入编号 && <button type="button" className="secondary small" onClick={replayReceipt}>重放此准入结局</button>}
        </div>
      )}

      {report && (
        <div className={`consistency ${report.一致 ? 'ok-box' : 'bad-box'}`}>
          <strong>口门/袋册一致性：{report.一致 ? '一致' : `发现 ${report.问题总数} 个问题`}</strong>
          <span>袋件总数 {report.袋件总数}，对齐 {report.对齐袋件数}，异常 {report.异常袋件数}，悬挂引用 {report.悬挂引用数}</span>
          {!report.一致 && (
            <table className="compact">
              <thead><tr><th>类型</th><th>袋</th><th>扫描</th><th>明细</th></tr></thead>
              <tbody>
                {report.问题.slice(0, 8).map((p, i) => (
                  <tr key={`${p.类型}-${p.scan_id || p.bag_id}-${i}`}>
                    <td>{p.类型}</td><td>{p.bag_id ?? '-'}</td><td>{p.scan_id ?? '-'}</td>
                    <td>{p.条码 || p.扫描状态 || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <table>
        <thead><tr><th>编号</th><th>袋码</th><th>口门</th><th>状态</th><th>件数</th><th>一致性</th><th>袋册</th></tr></thead>
        <tbody>
          {bags.map((b) => (
            <tr key={b.id} className={b.一致 === false ? 'drift-row' : ''}>
              <td>{b.id}</td>
              <td>{b.code}</td>
              <td>{b.口门码}</td>
              <td><span className={`badge ${b.status === '集包中' ? 'ok' : 'warn'}`}>{b.status}</span></td>
              <td data-testid="bag-count">{b.件数}</td>
              <td>{b.一致 ? '一致' : `异常${b.状态不一致数 + b.口门不一致数}`}</td>
              <td className="actions">
                <button type="button" className="secondary small" onClick={() => showBook(b.id)}>看袋册</button>
                {b.一致 === false && <button type="button" className="danger-btn small" onClick={() => rollbackDrift(b.id)}>回滚异常挂袋</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {book && (
        <div className="book-panel">
          <h3>袋册：{book.code} · 册页件数 {book.册页件数}</h3>
          <table>
            <thead><tr><th>扫描号</th><th>条码</th><th>状态</th><th>口门</th><th>目的地</th></tr></thead>
            <tbody>
              {book.册页.map((s) => (
                <tr key={s.id} className={s.status !== (book.status === '已封袋' ? '已封袋' : '已入袋') ? 'drift-row' : ''}>
                  <td>{s.id}</td><td>{s.barcode}</td><td>{s.status}</td><td>{s.口门码 || '-'}</td><td>{s.dest_code}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
