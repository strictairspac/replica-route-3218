"""Controlled primary-crash drill: WAL, no-steal dirty-page buffer, idempotent redo.

Chosen policy — when the buffer is full of uncommitted pages, a new write waits
for a transaction to end instead of flushing an uncommitted page:

  * A commit is acknowledged only after its WAL commit record is fsynced.
  * Commit does not force data pages; a committed page may stay dirty in cache.
  * A page that still holds an uncommitted overlay is never written to the data
    file. If every slot is pinned by uncommitted pages the new write queues until
    a transaction commits or aborts; only then can a committed page be flushed
    and evicted to make room.
  * Readers never see another session's uncommitted overlay; they get the latest
    committed image (or ``null``). The session that owns the overlay sees its own
    uncommitted value.
  * After a crash the primary rejects every write, commit, flush and read until
    redo finishes. Redo replays only committed WAL records, in LSN order, guarded
    by ``page_lsn``; an interrupted recovery can be restarted and will not apply
    a WAL record twice.

The model is deterministic, uses integer LSNs, and does not touch a real MySQL
server. It is a drill, not a crash-injection tool.
"""
import copy
import hashlib
import json

KINDS = ('write', 'commit', 'abort', 'flush', 'read', 'crash', 'restart')
POLICY = {
    'id': 'wait_no_steal',
    'name': '缓存满时让新写入等待事务结束',
    'buffer_unit': '脏页槽，每个 key 占一页',
    'ack_rule': '提交记录先 fsync 到 WAL 才确认；提交不要求刷数据页',
    'flush_rule': '只刷无未提交覆盖的页；未提交值从不落数据文件',
    'wait_rule': '槽位全被未提交页占满时，新写入排队到事务提交或回滚',
    'visibility_rule': '会话只读取自身未提交值；其他会话只能读到最近已提交值',
    'restart_rule': '断电到恢复完成之间拒绝一切读取',
    'redo_rule': '只重放已提交事务的 WAL 记录；按 page_lsn 判重，中断后可续跑',
}
MAX_CAPACITY = 32
MAX_EVENTS = 200
MAX_MS = 86400000


def _require_int(value, field, low, high=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (
            high is not None and value > high):
        bound = '%d–%d' % (low, high) if high is not None else '≥%d' % low
        raise ValueError('%s 必须是整数（%s）' % (field, bound))
    return value


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s 不能为空' % field)
    return value.strip()


def _validate(capacity, events):
    _require_int(capacity, '缓存容量', 1, MAX_CAPACITY)
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError('需要 1–%d 个时序事件' % MAX_EVENTS)
    last = -1
    for index, event in enumerate(events, 1):
        if not isinstance(event, dict):
            raise ValueError('第 %d 个事件必须是对象' % index)
        kind = event.get('kind')
        if kind not in KINDS:
            raise ValueError('第 %d 个事件类型未知：%s' % (index, kind))
        at = _require_int(event.get('at_ms'), '第 %d 个事件的 at_ms' % index, 0, MAX_MS)
        if at < last:
            raise ValueError('事件必须按 at_ms 非递减排列')
        last = at
        if kind == 'write':
            _require_text(event.get('session'), '第 %d 个写入事件的 session' % index)
            _require_text(event.get('key'), '第 %d 个写入事件的 key' % index)
        if kind in ('commit', 'abort'):
            _require_text(event.get('session'), '第 %d 个 %s 事件的 session' % (index, kind))
        if kind == 'read':
            _require_text(event.get('session'), '第 %d 个读取事件的 session' % index)
            _require_text(event.get('key'), '第 %d 个读取事件的 key' % index)
        if kind == 'restart' and event.get('apply_limit') is not None:
            _require_int(event['apply_limit'], '第 %d 个 restart 的 apply_limit' % index, 1)


def simulate_crash(capacity, events):
    _validate(capacity, events)
    capacity = int(capacity)
    events = copy.deepcopy(events)

    # Buffer: key -> page. A page keeps the last committed image plus an optional
    # overlay written by a transaction that has not committed yet.
    pages = {}
    disk = {}            # key -> {'value','page_lsn'}; only committed images ever land here
    wal = []             # every generated record, with a computed durable flag
    txns = {}            # session -> {'id','state','writes','keys'}
    pending = []         # writes queued in front of pinned buffer slots
    trace, crashes, recovery_attempts = [], [], []
    applied_lsn_history = []
    redo_duplicates = 0
    lsn_seq = txn_seq = write_seq = 0
    durable_lsn = 0
    mode = 'serving'     # serving | crashed | recovering
    wait_total = wait_count = max_wait = 0
    cancelled_pending = 0
    flush_count = evict_flush_count = 0
    read_allowed = read_rejected_down = dirty_read_blocked = 0
    first_read_after_crash = None
    crash_seen = False

    def next_lsn():
        nonlocal lsn_seq
        lsn_seq += 1
        return lsn_seq

    def durable_wal():
        return [r for r in wal if r['lsn'] <= durable_lsn]

    def commit_map(records):
        return {r['txn_id']: r['lsn'] for r in records if r['kind'] == 'commit'}

    def committed_values(records=None):
        records = durable_wal() if records is None else records
        commits = commit_map(records)
        values = {}
        for record in records:
            if record['kind'] == 'write' and record['txn_id'] in commits:
                values[record['key']] = record['value']
        return values

    def disk_lsn(key):
        record = disk.get(key)
        return record['page_lsn'] if record else 0

    def page_base(key):
        page = pages.get(key)
        if page:
            return page['committed_value'], page['committed_lsn']
        record = disk.get(key)
        return (record['value'], record['page_lsn']) if record else (None, 0)

    def is_pinned(page):
        return page['owner'] is not None

    def flush_page(key):
        """Write a page only when it has no uncommitted overlay."""
        page = pages.get(key)
        if page is None or is_pinned(page):
            return False
        if page['committed_value'] is not None and page['committed_lsn'] > disk_lsn(key):
            disk[key] = {'value': page['committed_value'], 'page_lsn': page['committed_lsn']}
            return True
        return False

    def drop_clean_page(key):
        page = pages[key]
        if page['committed_value'] is None or page['committed_lsn'] <= disk_lsn(key):
            del pages[key]
            return True
        return False

    def evict_committed_page():
        nonlocal evict_flush_count
        for key in list(pages):
            if is_pinned(pages[key]):
                continue
            if flush_page(key):
                evict_flush_count += 1
            drop_clean_page(key)
            return key
        return None

    def write_blocker(session, key):
        """Why this write cannot enter the buffer now; '' means it can."""
        page = pages.get(key)
        if page and is_pinned(page) and page['owner'] != session:
            return '同键页面正被会话 %s 的未提交写入占用' % page['owner']
        if page is None and len(pages) >= capacity and all(is_pinned(p) for p in pages.values()):
            owners = {p['owner'] for p in pages.values()}
            if owners == {session}:
                return ('缓存 %d 页全被本会话未提交页占用：本会话已自锁，'
                        '只能 abort 回滚或提高容量' % capacity)
            return '缓存 %d 页全被未提交页占用，等待事务结束' % capacity
        return ''

    def open_txn(session):
        txn = txns.get(session)
        return txn if txn and txn['state'] == 'open' else None

    def apply_write(task, now, released_by=''):
        nonlocal txn_seq
        session, key, value = task['session'], task['key'], task['value']
        evicted = []
        while key not in pages and len(pages) >= capacity:
            victim = evict_committed_page()
            if victim is None:
                return False
            evicted.append(victim)
        page = pages.get(key)
        if page is None:
            base_value, base_lsn = page_base(key)
            page = {'committed_value': base_value, 'committed_lsn': base_lsn,
                    'pending_value': None, 'pending_lsn': None, 'owner': None}
            pages[key] = page
        txn = open_txn(session)
        if txn is None:
            txn_seq += 1
            txn = {'id': txn_seq, 'state': 'open', 'writes': 0, 'keys': set()}
            txns[session] = txn
        lsn = next_lsn()
        page['pending_value'] = value
        page['pending_lsn'] = lsn
        page['owner'] = session
        txn['writes'] += 1
        txn['keys'].add(key)
        wal.append({'lsn': lsn, 'kind': 'write', 'txn_id': txn['id'], 'session': session,
                    'key': key, 'value': value, 'at_ms': now})
        waited = now - task['at_ms']
        reason = '写入缓存页（未提交，不刷盘）'
        if waited > 0:
            reason = '%s → 排队 %dms，%s后写入缓存页' % (
                task.get('last_blocker') or '缓存暂不可用', waited,
                released_by or '阻塞事务结束')
        task['row'].update(decision='applied', applied_at_ms=now, wait_ms=waited, lsn=lsn,
                           waited=waited > 0, released_by=released_by or None, reason=reason)
        if evicted:
            task['row']['evicted_keys'] = evicted
        return True

    def drain_pending(now, released_by=''):
        nonlocal wait_total, wait_count, max_wait
        progressed = True
        while progressed:
            progressed = False
            for task in list(pending):
                blocker = write_blocker(task['session'], task['key'])
                if blocker:
                    task['last_blocker'] = blocker
                    task['row'].update(decision='waiting', reason=blocker, buffer_used=len(pages))
                    continue
                if apply_write(task, now, released_by):
                    pending.remove(task)
                    waited = task['row']['wait_ms']
                    wait_total += waited
                    wait_count += 1
                    max_wait = max(max_wait, waited)
                    progressed = True

    def finish_txn(txn, session, committed):
        for key in list(txn['keys']):
            page = pages.get(key)
            if page is None or page['owner'] != session:
                continue
            if committed:
                page['committed_value'] = page['pending_value']
                page['committed_lsn'] = page['pending_lsn']
            page['pending_value'] = None
            page['pending_lsn'] = None
            page['owner'] = None
            if not committed and page['committed_value'] is None:
                del pages[key]

    def read_value(session, key, row):
        nonlocal dirty_read_blocked
        page = pages.get(key)
        if page:
            if page['owner'] == session and page['pending_value'] is not None:
                row['own_uncommitted'] = True
                row['reason'] = '读取本会话未提交写入（仅本会话可见）'
                return page['pending_value']
            if is_pinned(page):
                dirty_read_blocked += 1
                row['dirty_read_blocked'] = True
                row['reason'] = ('未提交值不可见，该键尚无已提交值' if page['committed_value'] is None
                                 else '未提交值不可见，返回最近已提交值')
                return page['committed_value']
            row['reason'] = '读取缓存中的已提交页'
            return page['committed_value']
        record = disk.get(key)
        row['reason'] = '读取已刷盘页' if record else '该键不存在'
        return record['value'] if record else None

    for index, event in enumerate(events):
        at = event['at_ms']
        kind = event['kind']
        session = event.get('session')
        key = event.get('key')
        value = event.get('value')
        drain_pending(at)
        row = {'index': index + 1, 'at_ms': at, 'kind': kind, 'session': session, 'key': key,
               'value': value, 'decision': 'applied', 'reason': '', 'wait_ms': 0,
               'applied_at_ms': None, 'buffer_used': len(pages), 'buffer_capacity': capacity,
               'pinned_pages': sum(1 for p in pages.values() if is_pinned(p)),
               'gate': 'open' if mode == 'serving' else 'closed'}

        if kind in ('write', 'commit', 'abort', 'flush', 'read') and mode != 'serving':
            row.update(decision='rejected',
                       reason='主库断电，恢复完成前不接受请求' if mode == 'crashed'
                       else '恢复中断，主库仍在重放 WAL，拒绝请求')
            if kind == 'read':
                read_rejected_down += 1
            trace.append(row)
            continue

        if kind == 'write':
            write_seq += 1
            if value is None:
                value = '%s-v%d' % (key, write_seq)
                row['value'] = value
            task = {'row': row, 'at_ms': at, 'session': session, 'key': key, 'value': value,
                    'reason': '', 'last_blocker': ''}
            blocker = write_blocker(session, key)
            if blocker:
                task['reason'] = blocker
                task['last_blocker'] = blocker
                row.update(decision='waiting', reason=blocker)
                pending.append(task)
            else:
                apply_write(task, at)
            row['admitted_at_ms'] = at

        elif kind == 'commit':
            txn = open_txn(session)
            own_pending = [t for t in pending if t['session'] == session]
            if own_pending:
                row.update(decision='blocked',
                           reason='本会话写入仍在缓存前排队，客户端尚未从写入调用返回，提交无法发出')
            elif txn is None:
                row.update(decision='rejected', reason='会话没有进行中的事务，提交无效')
            else:
                lsn = next_lsn()
                wal.append({'lsn': lsn, 'kind': 'commit', 'txn_id': txn['id'],
                            'session': session, 'at_ms': at})
                durable_lsn = lsn
                finish_txn(txn, session, True)
                txn['state'] = 'committed'
                row.update(decision='applied', acknowledged_writes=txn['writes'],
                           durable_lsn=lsn, applied_at_ms=at,
                           reason='WAL 提交记录已 fsync，事务内 %d 次写入获得确认' % txn['writes'])
                drain_pending(at, '会话 %s 提交' % session)

        elif kind == 'abort':
            txn = open_txn(session)
            if txn is None:
                row.update(decision='rejected', reason='会话没有进行中的事务，回滚无效')
            else:
                cancelled = [t for t in pending if t['session'] == session]
                for task in cancelled:
                    task['row'].update(decision='cancelled', reason='事务回滚，排队中的写入取消')
                    pending.remove(task)
                    cancelled_pending += 1
                writes = txn['writes']
                finish_txn(txn, session, False)
                txn['state'] = 'aborted'
                row.update(decision='applied', cancelled_writes=writes + len(cancelled),
                           reason='事务回滚，未提交值丢弃，槽位释放')
                drain_pending(at, '会话 %s 回滚' % session)

        elif kind == 'flush':
            flushed, freed, skipped = [], [], []
            for page_key in list(pages):
                if is_pinned(pages[page_key]):
                    skipped.append(page_key)
                    continue
                if flush_page(page_key):
                    flushed.append(page_key)
                    flush_count += 1
                if drop_clean_page(page_key):
                    freed.append(page_key)
            row.update(decision='flushed' if flushed else 'noop',
                       flushed_keys=flushed, freed_keys=freed, skipped_uncommitted=skipped,
                       reason=('刷盘 %d 个已提交页，跳过 %d 个未提交页' %
                               (len(flushed), len(skipped))) if skipped
                              else '刷盘 %d 个已提交页并释放槽位' % len(flushed))
            drain_pending(at, '刷盘释放槽位')

        elif kind == 'read':
            read_allowed += 1
            shown = read_value(session, key, row)
            row.update(decision='routed', visible_value=shown)
            if crash_seen and first_read_after_crash is None:
                first_read_after_crash = at

        elif kind == 'crash':
            if mode == 'crashed':
                row.update(decision='rejected', reason='主库已经处于断电状态')
            else:
                lost, discarded = [], {}
                for page_key, page in pages.items():
                    if page['committed_value'] is not None and \
                            page['committed_lsn'] > disk_lsn(page_key):
                        lost.append(page_key)
                    if is_pinned(page):
                        discarded[page_key] = {'value': page['pending_value'],
                                               'session': page['owner']}
                for task in pending:
                    task['row'].update(decision='cancelled', reason='断电丢弃排队写入（从未获得确认）')
                    cancelled_pending += 1
                pending.clear()
                for txn in txns.values():
                    if txn['state'] == 'open':
                        txn['state'] = 'aborted'
                wal[:] = [r for r in wal if r['lsn'] <= durable_lsn]
                crashes.append({
                    'at_ms': at, 'durable_lsn': durable_lsn,
                    'lost_unflushed_keys': lost,
                    'discarded_uncommitted': copy.deepcopy(discarded),
                    'disk_values': {k: copy.deepcopy(v) for k, v in disk.items()},
                    'wal_at_crash': copy.deepcopy(wal),
                    'during_recovery': mode == 'recovering',
                })
                pages.clear()
                mode = 'crashed'
                crash_seen = True
                first_read_after_crash = None   # ignore reads admitted before this crash
                row.update(decision='crashed', durable_lsn=durable_lsn,
                           lost_unflushed_keys=lost,
                           discarded_uncommitted_keys=sorted(discarded),
                           reason='断电：缓存清空，只有已 fsync 的 WAL 提交记录可重放')

        elif kind == 'restart':
            if mode == 'serving':
                row.update(decision='rejected', reason='主库未断电，无需恢复')
            else:
                records = durable_wal()
                commits = commit_map(records)
                redo = [r for r in records if r['kind'] == 'write'
                        and r['txn_id'] in commits and r['lsn'] < commits[r['txn_id']]]
                limit = event.get('apply_limit')
                applied, skipped = [], []
                interrupted = False
                for record in redo:
                    if disk_lsn(record['key']) >= record['lsn']:
                        skipped.append(record['lsn'])
                        continue
                    if limit is not None and len(applied) >= limit:
                        interrupted = True
                        break
                    disk[record['key']] = {'value': record['value'], 'page_lsn': record['lsn']}
                    if record['lsn'] in applied_lsn_history:
                        redo_duplicates += 1
                    applied_lsn_history.append(record['lsn'])
                    applied.append(record['lsn'])
                remaining = [r['lsn'] for r in redo if disk_lsn(r['key']) < r['lsn']]
                complete = not remaining
                mode = 'serving' if complete else 'recovering'
                recovery_attempts.append({
                    'attempt': len(recovery_attempts) + 1, 'at_ms': at,
                    'redo_total': len(redo), 'applied_lsns': applied,
                    'skipped_already_applied': skipped, 'remaining_redo': len(remaining),
                    'complete': complete, 'interrupted': interrupted, 'apply_limit': limit,
                    'disk_after': copy.deepcopy(disk) if complete else None,
                })
                row.update(decision='recovered' if complete else 'partial',
                           applied_lsns=applied, skipped_lsns=skipped,
                           remaining_redo=len(remaining),
                           reason=('恢复完成：本次 redo %d 条、跳过已应用 %d 条，主库开始接读' %
                                   (len(applied), len(skipped))) if complete else
                                  ('恢复中断：本次 redo %d 条后停下，主库继续拒绝请求；再次 restart 可续跑' %
                                   len(applied)))

        trace.append(row)

    drain_pending(events[-1]['at_ms'])
    for task in pending:
        task['row'].update(decision='waiting', reason=task['row'].get('reason') or '等待事务结束')

    # Durability is judged at the final crash point: every write acknowledged
    # before it must be on disk once recovery completes. Writes acknowledged after
    # the last crash are durable in the WAL but need not be flushed to a page yet.
    if crashes:
        at_crash = {}
        records = crashes[-1]['wal_at_crash']
        commits = commit_map(records)
        for record in records:
            if record['kind'] == 'write' and record['txn_id'] in commits:
                at_crash[record['key']] = record['value']
    else:
        at_crash = {}
    current_committed = committed_values()
    commits = commit_map(durable_wal())
    acknowledged_writes = sum(r.get('acknowledged_writes', 0) for r in trace
                              if r['kind'] == 'commit' and r['decision'] == 'applied')
    committed_wal_writes = sum(1 for r in durable_wal()
                               if r['kind'] == 'write' and r['txn_id'] in commits)
    recovered = mode == 'serving' and bool(crashes) and bool(recovery_attempts) and \
        recovery_attempts[-1]['complete']

    discarded = {}
    for info in crashes:
        for page_key, entry in info['discarded_uncommitted'].items():
            discarded.setdefault(page_key, entry)

    key_rows = []
    # Inspect the disk image produced by the final (complete) recovery attempt, not
    # the live disk map: later commits after recovery must not mask a leaked value.
    recovered_disk = recovery_attempts[-1]['disk_after'] if recovered else None
    for key in sorted(set(at_crash) | set(current_committed) | set(disk) | set(discarded) |
                      {k for info in crashes for k in info['lost_unflushed_keys']}):
        before = None
        for info in crashes:
            if key in info['disk_values']:
                before = info['disk_values'][key]['value']
        after = disk.get(key, {}).get('value')
        uncommitted = discarded.get(key, {}).get('value')
        if recovered:
            recovered_value = recovered_disk.get(key, {}).get('value') if recovered_disk else None
            survived = (key not in at_crash) or (recovered_value == at_crash[key])
            exposed = bool(uncommitted is not None and recovered_value == uncommitted
                           and at_crash.get(key) != uncommitted)
            shown_after = recovered_value
        else:
            survived, exposed = None, False
            shown_after = after
        key_rows.append({
            'key': key,
            'committed_at_crash': at_crash.get(key),
            'committed_now': current_committed.get(key),
            'disk_at_crash': before,
            'disk_after_recovery': shown_after,
            'disk_now': after,
            'discarded_uncommitted': uncommitted,
            'discarded_uncommitted_by': discarded.get(key, {}).get('session'),
            'committed_value_survived': survived,
            'uncommitted_value_exposed': exposed,
        })

    lost_acknowledged = sum(1 for row in key_rows if row['committed_value_survived'] is False)
    uncommitted_exposed = sum(1 for row in key_rows if row['uncommitted_value_exposed'])
    wal_view = [dict(r, durable=r['lsn'] <= durable_lsn,
                     committed=r['txn_id'] in commit_map(durable_wal())) for r in wal]
    summary = {
        'events': len(events),
        'writes': sum(1 for r in trace if r['kind'] == 'write'),
        'commits': sum(1 for r in trace if r['kind'] == 'commit' and r['decision'] == 'applied'),
        'acknowledged_writes': acknowledged_writes,
        'committed_wal_writes': committed_wal_writes,
        'waited_writes': wait_count,
        'still_waiting': len(pending),
        'max_wait_ms': max_wait,
        'total_wait_ms': wait_total,
        'cancelled_pending_writes': cancelled_pending,
        'flushed_pages': flush_count,
        'evict_flushed_pages': evict_flush_count,
        'crash_count': len(crashes),
        'recovery_attempts': len(recovery_attempts),
        'interrupted_recoveries': sum(1 for a in recovery_attempts if a['interrupted']),
        'redo_applied_total': len(applied_lsn_history),
        'redo_skipped_total': sum(len(a['skipped_already_applied']) for a in recovery_attempts),
        'redo_duplicates': redo_duplicates,
        'recovered': recovered,
        'mode_at_end': mode,
        'reads_allowed': read_allowed,
        'reads_while_down_rejected': read_rejected_down,
        'reads_routed_while_down': sum(1 for r in trace
                                       if r['kind'] == 'read' and r['decision'] == 'routed'
                                       and r['gate'] != 'open'),
        'dirty_reads_blocked': dirty_read_blocked,
        'first_read_allowed_after_crash_ms': first_read_after_crash,
        'acknowledged_lost': lost_acknowledged,
        'uncommitted_exposed': uncommitted_exposed,
        'durable_wal_records': len(durable_wal()),
        'durable_lsn': durable_lsn,
        'values_preserved': (recovered or not crashes) and lost_acknowledged == 0
                            and uncommitted_exposed == 0,
    }
    summary['read_gate_clean'] = summary['reads_routed_while_down'] == 0
    result = {
        'mode': '受控主库断电恢复模型 / 非真实 MySQL 崩溃注入',
        'policy': dict(POLICY, capacity=capacity),
        'trace': trace,
        'wal': wal_view,
        'crashes': crashes,
        'recovery_attempts': recovery_attempts,
        'keys': key_rows,
        'final_values': {row['key']: row['disk_after_recovery'] for row in key_rows
                         if row['disk_after_recovery'] is not None},
        'disk_values_now': {row['key']: row['disk_now'] for row in key_rows
                            if row['disk_now'] is not None},
        'summary': summary,
    }
    result['checksum'] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
