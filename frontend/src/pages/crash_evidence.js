import {table} from '../components/common/table.js';import {badge} from '../components/common/badge.js';import {esc,date} from '../utils/html.js';import {CRASH_EVENTS,CRASH_DECISIONS} from '../constants/enums.js';
const decisionClass=d=>['rejected','blocked','cancelled'].includes(d)?'rejected':(d==='waiting'?'waiting':'');
function stat(label,value,note=''){return `<div class="stat"><span>${label}</span><b>${value}</b>${note?`<small>${note}</small>`:''}</div>`;}
export function crashEvidencePage(drill){
  if(!drill)return '<div class="empty">还没有断电演练证据。先登记时序脚本，再从「断电恢复」启动一次演练。</div>';
  const r=drill.result;
  const summary=r?.summary;
  return `<div class="evidence-top"><div><span class="eyebrow">CRASH EVIDENCE / #${drill.id}</span><h2>${esc(drill.name)} ${badge(drill.status)}</h2><p>${date(drill.created_at)} · 脚本与缓存容量已冻结 · 缓存 ${drill.capacity} 页</p></div><button id="crash-export" data-id="${drill.id}">导出 JSON ↓</button></div>
  ${drill.error?`<div class="error">${esc(drill.error)}。请检查脚本并重新执行。</div>`:''}
  ${r?`<div class="verdict"><div class="verdict-icon">${summary.values_preserved?'✓':'!'}</div><div><span class="eyebrow">DURABILITY CHECK</span><h2>${summary.crash_count===0?'脚本没有触发断电，未检验恢复路径':(summary.values_preserved?'断电前已确认写入全部保留，未提交值从未被读到':(summary.recovered?'恢复完成但存在不满足项':'尚未恢复完成，主库仍拒绝读取'))}</h2><p>确认写入 ${summary.acknowledged_writes} 次 · 丢失已确认写入 ${summary.acknowledged_lost} 个 · 未提交值被读到 ${summary.uncommitted_exposed} 个 · 重放重复应用 ${summary.redo_duplicates} 条</p></div></div>
  <div class="policy-strip">策略：${esc(r.policy.name)} <span>／</span> 缓存 ${r.policy.capacity} 页 <span>／</span> 等待写入 ${summary.waited_writes} 次，最长 ${summary.max_wait_ms}ms <span>／</span> 恢复 ${summary.recovered?'已完成':'未完成'}</div>
  <div class="stat-grid">
  ${stat('ACKED WRITES',summary.acknowledged_writes,'提交记录 fsync 后确认')}
  ${stat('MAX WAIT',summary.max_wait_ms+'ms','缓存满时最长排队')}
  ${stat('WAL 记录',summary.durable_wal_records,'断电点仍可重放')}
  ${stat('REDO 跳过',summary.redo_skipped_total,'按 page_lsn 判重')}
  ${stat('断电后拒读',summary.reads_while_down_rejected,'恢复完成前')}
  ${stat('首读放行',summary.first_read_allowed_after_crash_ms==null?'—':summary.first_read_allowed_after_crash_ms+'ms','恢复完成后')}
  </div>
  <div class="section-top"><h2>逐事件时序</h2><span>受控模型 / 非真实崩溃注入</span></div>
  ${table(['时间 / 事件','会话 / 键','决策','等待','说明'],r.trace.map(e=>[`${e.at_ms}ms<small>${esc(CRASH_EVENTS[e.kind]||e.kind)}</small>`,`${esc(e.session||'—')}<small>${esc(e.key||'—')}${e.value!=null?' = '+esc(e.value):''}</small>`,`<span class="decision ${decisionClass(e.decision)}">${esc(CRASH_DECISIONS[e.decision]||e.decision)}</span>`,e.wait_ms?`${e.wait_ms}ms`:'—',esc(e.reason)]))}
  <div class="evidence-notes"><section><h3>缓存与刷盘</h3><p>缓存 ${r.policy.buffer_unit}。写入只改缓存页中的未提交覆盖层；提交只落 WAL 提交记录。刷盘事件只刷无未提交覆盖的页，未提交页原样留在缓存，槽位全被占满时新写入排队到事务结束。</p><p>本次显式刷盘 ${summary.flushed_pages} 次、为腾槽位刷盘 ${summary.evict_flushed_pages} 次、因回滚或断电取消的排队写入 ${summary.cancelled_pending_writes} 次。</p></section>
  <section><h3>恢复与重试</h3><p>断电清空缓存，只有 durable_lsn 之前的 WAL 记录存在。恢复只重放已提交事务的写记录，逐条比较 page_lsn，已应用则跳过，因此恢复中断后再次 restart 不会重复应用。</p><p>恢复尝试 ${summary.recovery_attempts} 次，其中中断 ${summary.interrupted_recoveries} 次，累计应用 ${summary.redo_applied_total} 条，跳过 ${summary.redo_skipped_total} 条。${summary.recovered&&summary.first_read_allowed_after_crash_ms!=null?`第一次放行读取在 ${summary.first_read_allowed_after_crash_ms}ms。`:'恢复尚未完成，读请求仍然拒绝。'}</p></section></div>
  <div class="section-top"><h2>键的最终去留</h2><span>COMMITTED / DISK / UNCOMMITTED</span></div>
  ${table(['键','断电时已提交值','当前已提交值','断电时磁盘','恢复后磁盘','未提交值（应丢弃）'],r.keys.map(k=>[esc(k.key),k.committed_at_crash==null?'—':esc(k.committed_at_crash),k.committed_now==null?'—':esc(k.committed_now),k.disk_at_crash==null?'—':esc(k.disk_at_crash),k.disk_after_recovery==null?'—':esc(k.disk_after_recovery),k.discarded_uncommitted==null?'—':`${esc(k.discarded_uncommitted)} <small>${esc(k.discarded_uncommitted_by||'')}</small>`]))}
  <div class="evidence-notes"><section><h3>键的留存判定</h3><p>判定基准是最后一次断电时点：断电前已确认的提交，其值必须在恢复完成后出现在数据文件里。断电后新发生的提交只保证在 WAL 中，可以等下一次刷盘。</p><p>${r.keys.filter(k=>k.committed_value_survived===false).length?'存在丢失的已确认写入。':'断电前已确认写入全部保留。'}${r.keys.filter(k=>k.uncommitted_value_exposed).length?'存在被读到的未提交值。':'未提交值从未出现在数据文件。'}</p></section>
  <section><h3>确定性摘要</h3><p>相同脚本、相同容量产生相同摘要，不依赖随机数或外部网络。WAL 位点为单调整数 LSN。断电后实际被路由的读取 ${summary.reads_routed_while_down} 次（应为 0）。</p><code>${esc(r.checksum)}</code><p>${esc(r.mode)}</p></section></div>`
  :'<div class="notice">后台重放中，每1.5秒刷新。可保留此页，或稍后从演练记录返回。</div>'}`;
}
