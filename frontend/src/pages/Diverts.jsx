import React, { useCallback, useEffect, useState } from 'react'
import { api, notifyDataChanged, onDataChanged } from '../api'

export default function Diverts() {
  const [batches, setBatches] = useState([])
  const [scans, setScans] = useState([])
  const [scanId, setScanId] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    const [batchRows, allScans] = await Promise.all([api('/回流批次'), api('/扫描')])
    setBatches(batchRows)
    // 严格 A 口径下待路由件不可能已入袋；回流入口只对已路由/已入袋可见。
    setScans(allScans.filter((x) => x.status === '已路由' || x.status === '已入袋'))
  }, [])

  useEffect(() => {
    load().catch((e) => setErr(e.message))
    return onDataChanged(() => { load().catch((e) => setErr(e.message)) })
  }, [load])

  const create = async () => {
    setErr(''); setMsg('')
    try {
      await api('/回流批次', { method: 'POST', body: JSON.stringify({ 扫描编号: Number(scanId) }) })
      setMsg('回流批次已建；如扫描在袋，袋册事件和袋件数已在同事务更新')
      setScanId('')
      await load()
      notifyDataChanged({ type: 'diverted' })
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className="panel">
      <h2>错分回流入口可见性</h2>
      <p className="muted">仅 <code>已路由</code>、<code>已入袋</code> 显示为可回流；待路由必须先裁决，不能伪装成袋内回流。</p>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <div className="row">
        <label>扫描
          <select value={scanId} onChange={(e) => setScanId(e.target.value)}>
            <option value="">选择</option>
            {scans.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id} · {s.barcode} · {s.status} · {s.口门码 || '-'} · {s.袋码 || '未入袋'}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={create}>建回流批次</button>
      </div>
      <table>
        <thead><tr><th>编号</th><th>批次码</th><th>状态</th><th>时间</th></tr></thead>
        <tbody>
          {batches.map((b) => (
            <tr key={b.id}><td>{b.id}</td><td>{b.code}</td><td>{b.status}</td><td>{b.created_at}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
