import {table} from '../components/common/table.js';import {badge} from '../components/common/badge.js';import {esc,date} from '../utils/html.js';import {session} from '../hooks/session.js';
const sample=[
  {at_ms:0,kind:'write',session:'A',key:'acct',value:100},
  {at_ms:10,kind:'commit',session:'A'},
  {at_ms:20,kind:'write',session:'A',key:'acct',value:60},
  {at_ms:30,kind:'commit',session:'A'},
  {at_ms:40,kind:'write',session:'B',key:'acct',value:999},
  {at_ms:50,kind:'write',session:'C',key:'ship',value:'c1'},
  {at_ms:60,kind:'write',session:'D',key:'hold',value:'d1'},
  {at_ms:70,kind:'read',session:'A',key:'acct'},
  {at_ms:150,kind:'commit',session:'C'},
  {at_ms:160,kind:'crash'},
  {at_ms:180,kind:'read',session:'A',key:'acct'},
  {at_ms:200,kind:'restart',apply_limit:1},
  {at_ms:210,kind:'read',session:'A',key:'acct'},
  {at_ms:230,kind:'restart'},
  {at_ms:240,kind:'read',session:'A',key:'acct'}
];
export function crashPage(scripts,drills){
  return `<div class="split"><section><div class="section-top"><h2>断电时序脚本</h2><span>${scripts.length} SCRIPTS / WAL + BUFFER</span></div>
  ${table(['脚本 / 场景','缓存','事件','操作'],scripts.map(s=>[`<strong>${esc(s.code)}</strong><small>${esc(s.title)}</small><small>${esc(s.note)}</small>`,`${s.capacity} 页`,`${s.events.length} 个`,`<button class="small edit-crash" data-id="${s.id}" ${session.user.role==='viewer'?'disabled':''}>编辑</button>`]))}
  <div class="protocol"><h3>本次演练采用的策略</h3><ol>
  <li><b>缓存满时让新写入等待事务结束</b>，绝不刷未提交页腾空间。等待时长 = 事务提交或回滚时刻 − 写入到达时刻。</li>
  <li>提交只保证 WAL 提交记录已 fsync 即返回确认；数据页可以滞后刷盘，所以断电后必须做 redo。</li>
  <li>未提交值不落数据文件、也不被其他会话读到；本会话可读自己的未提交值。</li>
  <li>断电到恢复完成之间一律拒绝读取；恢复中断可再次 restart 续跑，按 page_lsn 判重，不重复应用。</li>
  </ol>
  <p>事件字段：write / commit / abort 需要 session，write 另需 key（value 可省略自动生成）；read 需要 session 与 key；restart 可选 apply_limit 模拟恢复中断；crash 清空缓存。</p></div>
  <div class="section-top"><h2>演练记录</h2><span>ACKED / REDO / READ GATE</span></div>
  ${table(['演练 / 时间','脚本','状态','证据'],drills.map(d=>[`<strong>${esc(d.name)}</strong><small>${date(d.created_at)}</small>`,esc(scripts.find(s=>s.id===d.script_id)?.code||'—'),badge(d.status),`<a href="#crash_evidence/${d.id}">查看 →</a>`]))}
  </section>
  <section class="form-panel"><span class="eyebrow">CRASH DRILL</span><h2>启动一次断电演练</h2>
  <form id="drill-form"><label>演练名称<input name="name" value="crash-20261117" required></label>
  <label>时序脚本<select name="script_id">${scripts.map(s=>`<option value="${s.id}">${esc(s.code)} · ${esc(s.title)}</option>`).join('')}</select></label>
  <label>缓存容量覆盖（留空沿用脚本）<input name="capacity" type="number" min="1" max="32" placeholder="例如 2"></label>
  <button class="primary" ${session.user.role==='viewer'?'disabled':''}>重放断电与恢复 ↗</button></form>
  <p class="help">冻结脚本与容量后由后台逐事件计算。演练只读模型，不会对真实数据库做任何操作。</p>
  <span class="eyebrow">TIMELINE SCRIPT</span><h2 id="crash-heading">编排断电时序</h2>
  <form id="crash-form"><input type="hidden" name="id">
  <label>脚本编号<input name="code" placeholder="QY-CRASH-D" required></label>
  <label>场景标题<input name="title" value="缓存排队后断电恢复" required></label>
  <label>缓存容量（脏页槽）<input name="capacity" type="number" min="1" max="32" value="2" required></label>
  <label>时序事件JSON<textarea name="events" rows="14" required>${esc(JSON.stringify(sample,null,2))}</textarea></label>
  <label>备注<input name="note" value="断电恢复演练"></label>
  <button class="primary" ${session.user.role==='viewer'?'disabled':''}>保存时序脚本 +</button></form>
  </section></div>`;
}
