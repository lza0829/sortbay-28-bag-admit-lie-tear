import React, { useCallback, useEffect, useState } from 'react'
import { api, onDataChanged } from '../api'

export default function Overview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => api('/总览').then(setData).catch((e) => setErr(e.message)), [])
  useEffect(() => {
    load()
    return onDataChanged(load)
  }, [load])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>
  const d = data.分布 || {}
  const rec = data.对账 || {}
  return (
    <div className="panel">
      <h2>总览对账</h2>
      <div className="policy-banner compact">
        <strong>{data.入袋政策 || '仅已路由可入袋'}</strong>
        <span>待补裁决 {data.待补裁决件数 ?? 0} · 隔离 {data.隔离件数 ?? 0} · 口径全局唯一，失败入袋不进入集包读路径。</span>
      </div>
      <p className="muted">当前班次：{data.班次?.business_day} {data.班次?.name} · {data.班次?.sealed ? '已封班' : '未封班'}</p>
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
        <div className="stat"><div className="n">{data.袋件总数 ?? 0}</div><div className="l">袋件总数</div></div>
        <div className="stat"><div className="n">{rec.问题总数 ?? 0}</div><div className="l">一致性问题</div></div>
      </div>
      <div className={`consistency ${rec.一致 ? 'ok-box' : 'bad-box'}`}>
        <strong>袋册/口门对账：{rec.一致 ? '一致' : `${rec.问题总数} 个问题`}</strong>
        <span>袋总数 {data.袋总数} · 集包中 {data.集包中袋数} · 对齐 {rec.口门一致性?.对齐袋件数 ?? 0} · 异常 {rec.口门一致性?.异常袋件数 ?? 0} · 悬挂 {rec.悬挂引用数 ?? 0}</span>
        <span>准入允许 {rec.准入统计?.允许数 ?? 0} · 拒绝 {rec.准入统计?.拒绝数 ?? 0}</span>
      </div>
      <table>
        <thead><tr><th>口门</th><th>标题</th><th>状态</th><th>在口件数</th><th>集包在袋数</th></tr></thead>
        <tbody>
          {(data.口门 || []).map((c) => (
            <tr key={c.id}>
              <td>{c.code}</td><td>{c.title}</td><td>{c.status}</td><td>{c.在口件数}</td><td>{c.集包在袋数}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新总览</button></div>
    </div>
  )
}
