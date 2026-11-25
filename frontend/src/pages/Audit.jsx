import React, { useCallback, useEffect, useState } from 'react'
import { api, onDataChanged } from '../api'

export default function Audit() {
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    api('/审计?limit=500').then(setRows).catch((e) => setErr(e.message))
  }, [])
  useEffect(() => {
    load()
    return onDataChanged(load)
  }, [load])

  return (
    <div className="panel">
      <h2>审计与准入重放</h2>
      <p className="muted">所有入袋允许、拒绝、幂等重放和并发冲突均在此留痕；准入编号可回读完整结局。</p>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button type="button" className="secondary" onClick={load}>刷新审计</button>
      </div>
      <table>
        <thead>
          <tr><th>时间</th><th>类别</th><th>动作</th><th>结果</th><th>关联</th><th>准入号</th><th>明细</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.created_at}</td>
              <td>{r.kind}</td>
              <td>{r.action}</td>
              <td><span className={`badge ${r.result === '允许' ? 'ok' : r.result === '拒绝' ? 'danger' : r.result === '重放' ? 'warn' : ''}`}>{r.result || '-'}</span></td>
              <td>{r.ref_id ?? '-'}</td>
              <td>{r.admission_id ?? '-'}</td>
              <td>{r.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
