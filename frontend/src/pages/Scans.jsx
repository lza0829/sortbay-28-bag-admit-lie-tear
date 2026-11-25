import React, { useCallback, useEffect, useState } from 'react'
import { api, notifyDataChanged, onDataChanged } from '../api'

export default function Scans() {
  const [rows, setRows] = useState([])
  const [bags, setBags] = useState([])
  const [barcode, setBarcode] = useState('')
  const [dest, setDest] = useState('110')
  const [bagId, setBagId] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    const [scanRows, bagRows] = await Promise.all([api('/扫描'), api('/封袋')])
    setRows(scanRows)
    setBags(bagRows)
    setBagId((prev) => {
      if (prev) return prev
      const open = bagRows.find((b) => b.status === '集包中')
      return open ? String(open.id) : ''
    })
  }, [])

  useEffect(() => {
    load().catch((e) => setErr(e.message))
    return onDataChanged(() => { load().catch((e) => setErr(e.message)) })
  }, [load])

  const create = async () => {
    setErr(''); setMsg('')
    try {
      await api('/扫描', { method: 'POST', body: JSON.stringify({ 条码: barcode, 目的地码: dest }) })
      setMsg('过机装载成功：当前为待路由，仅完成裁决后才可入袋'); setBarcode(''); await load()
      notifyDataChanged({ type: 'scan-created' })
    } catch (e) { setErr(e.message) }
  }

  const routeOne = async (id) => {
    setErr(''); setMsg('')
    try {
      const result = await api(`/扫描/${id}/路由`, { method: 'POST' })
      setMsg(`裁决成功：${result.status} / ${result.口门码 || ''}`); await load()
      notifyDataChanged({ type: 'routed', scanId: id })
    } catch (e) { setErr(e.message) }
  }

  const bagOne = async (id) => {
    setErr(''); setMsg('')
    if (!bagId) {
      setErr('没有可用袋编号：请先到封袋页开袋，或在上方选择集包袋')
      return
    }
    try {
      const result = await api(`/封袋/${bagId}/入袋`, { method: 'POST', body: JSON.stringify({ 扫描编号: id }) })
      setMsg(`入袋成功，袋件数 ${result.袋件数}`); await load()
      notifyDataChanged({ type: 'admitted', scanId: id })
    } catch (e) {
      setErr(e.message)
      await load().catch(() => {})
      notifyDataChanged({ type: 'admission-rejected', scanId: id })
    }
  }

  const divertOne = async (id) => {
    setErr(''); setMsg('')
    try {
      await api('/回流批次', { method: 'POST', body: JSON.stringify({ 扫描编号: id }) })
      setMsg('已建错分回流批次，若曾入袋则同事务移出袋册'); await load()
      notifyDataChanged({ type: 'diverted', scanId: id })
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className="panel">
      <h2>过机扫描册 / 后补裁决入口</h2>
      <div className="policy-banner compact">
        <strong>准入谓词：</strong><span>只有 <code>已路由</code> 能转为 <code>已入袋</code>；在 <code>待路由</code> 行点入袋会整笔拒绝并留审计。</span>
      </div>
      <div className="row">
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} /></label>
        <label>目的地码<input value={dest} onChange={(e) => setDest(e.target.value)} /></label>
        <button type="button" onClick={create}>装载扫描</button>
        <label>集包袋
          <select value={bagId} onChange={(e) => setBagId(e.target.value)}>
            <option value="">选择集包中袋</option>
            {bags.filter((b) => b.status === '集包中').map((b) => (
              <option key={b.id} value={b.id}>{b.id} · {b.code} · {b.口门码}</option>
            ))}
          </select>
        </label>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <table>
        <thead><tr><th>编号</th><th>条码</th><th>目的地</th><th>状态</th><th>口门</th><th>袋</th><th>联动操作</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className={r.bag_id && !['已入袋', '已封袋'].includes(r.status) ? 'drift-row' : ''}>
              <td>{r.id}</td>
              <td>{r.barcode}</td>
              <td>{r.dest_code}</td>
              <td><span className={`badge ${r.status === '待路由' ? 'warn' : r.status === '已入袋' || r.status === '已封袋' ? 'ok' : ''}`}>{r.status}</span></td>
              <td>{r.口门码 || r.chute_id || '-'}</td>
              <td>{r.袋码 || r.bag_id || '-'}</td>
              <td className="actions">
                {(r.status === '待路由' || r.status === '错分回流') && (
                  <button type="button" onClick={() => routeOne(r.id)}>路由</button>
                )}
                <button
                  type="button"
                  className={r.status === '待路由' ? 'danger-btn' : 'secondary'}
                  onClick={() => bagOne(r.id)}
                  title="仅已路由可入袋；待路由点击将验证整笔拒绝"
                >
                  入袋
                </button>
                {(r.status === '已路由' || r.status === '已入袋') && (
                  <button type="button" className="secondary" onClick={() => divertOne(r.id)}>回流</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
