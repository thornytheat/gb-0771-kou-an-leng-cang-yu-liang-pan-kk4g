from collections import defaultdict
from app.modules.registry.versions import at


def capacity(registry, states, moment, disputes=()):
    """收盘占位对账；调位冲突涉及的去向槽位整位冻结，暂停收货。"""
    buckets = defaultdict(list)
    for lot, state in sorted(states.items()):
        buckets[state['slot']].append(dict(lot=lot, pieces=registry['lots'][lot]['pieces'], frozen=bool(state['ticket'])))
    quarantined = {slot for group in disputes for slot in group['slots']
                   if slot in registry['versions']}
    dispute_lines = {slot: sorted({line for group in disputes if slot in group['slots']
                                   for line in group['lines']})
                     for slot in quarantined}
    dispute_lots = defaultdict(list)
    for group in disputes:
        for slot in group['slots']:
            if slot in registry['versions']:
                dispute_lots[slot].append(group['lot'])
    rows, pending = [], []
    for slot in sorted(set(registry['versions']) | set(buckets)):
        version, code = at(registry['versions'], slot, moment)
        items = buckets[slot]
        if version is None:
            pending.extend(dict(row, slot=slot, code=code) for row in items)
            continue
        frozen = sum(row['pieces'] for row in items if row['frozen'])
        occupied = sum(row['pieces'] for row in items if not row['frozen'])
        held = slot in quarantined
        free = max(0, version['capacity']-frozen-occupied)
        receivable = 0 if held else free
        rows.append(dict(slot=slot, door=version['door'], version=version['revision'], source_line=version['line'],
                         lots=items, available=version['capacity'], frozen=frozen, occupied=occupied,
                         free=free, excess=max(0, occupied+frozen-version['capacity']),
                         held=held, receivable=receivable,
                         dispute_lots=sorted(set(dispute_lots[slot])), dispute_lines=dispute_lines.get(slot, [])))
    totals = {field: sum(row[field] for row in rows)
              for field in ('available','frozen','occupied','free','excess','receivable')}
    totals['quarantined'] = sum(row['available'] for row in rows if row['held'])
    totals['unplaced'] = sum(row['pieces'] for row in pending)
    totals['in_stock'] = sum(registry['lots'][lot]['pieces'] for lot in states)
    return rows, pending, totals
