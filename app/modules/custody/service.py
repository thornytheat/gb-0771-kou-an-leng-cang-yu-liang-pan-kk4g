from collections import defaultdict
from itertools import groupby
from .transitions import change


def relocation_conflicts(ordered):
    """同一货批在同一生效时刻出现两个不同调位去向时，整组记为调位冲突。"""
    groups = []
    by_lot = defaultdict(list)
    for row in ordered:
        if row['kind'] == 'MOVE':
            by_lot[row['lot']].append(row)
    for lot, rows in sorted(by_lot.items()):
        for moment, batch in groupby(sorted(rows, key=lambda r: (r['time'], r['line'])), key=lambda r: r['time']):
            batch = list(batch)
            slots = {row['slot'] for row in batch}
            if len(slots) > 1:
                groups.append(dict(lot=lot, time=moment, slots=sorted(slots),
                                   lines=sorted(row['line'] for row in batch),
                                   rows=batch))
    return groups


def replay(events, registry, start, end):
    states, began, high = {}, {}, {}
    periods, activity, isolated = [], [], []
    late = set()
    for row in sorted(events, key=lambda r: (r['arrival'], r['line'])):
        max_time = high.get(row['lot'], row['time'])
        if row['time'] < max_time:
            late.add(row['event'])
        high[row['lot']] = max(max_time, row['time'])
    ordered = sorted((row for row in events if row['time'] < end), key=lambda r: (r['time'], r['line']))
    conflicts = relocation_conflicts(ordered)
    conflict_keys = {(row['event'], row['line']) for group in conflicts for row in group['rows']}
    for moment, grouped in groupby(ordered, key=lambda row: row['time']):
        by_lot = defaultdict(list)
        for row in grouped:
            by_lot[row['lot']].append(row)
        for lot, batch in sorted(by_lot.items()):
            active = [row for row in batch if row['kind'] != 'SEAL']
            disputed = [row for row in active if (row['event'], row['line']) in conflict_keys]
            if len(active) > 1 and not disputed:
                isolated.extend(dict(row, code='SIMULTANEOUS') for row in batch)
                continue
            for row in sorted(batch, key=lambda row: (row['kind'] == 'SEAL', row['line'])):
                if (row['event'], row['line']) in conflict_keys:
                    previous = states.get(lot)
                    isolated.append(dict(row, code='MOVE_DISPUTED'))
                    activity.append(dict(row, late=row['event'] in late, before=previous, after=previous,
                                         code='MOVE_DISPUTED'))
                    continue
                next_state, issue = change(row, states, registry)
                if issue:
                    isolated.append(dict(row, code=issue))
                    if issue not in ('SLOT_UNKNOWN', 'SLOT_AMBIGUOUS'):
                        continue
                previous = states.get(lot)
                if previous is not None and previous != next_state:
                    close(periods, lot, previous, began[lot], moment, start)
                if previous != next_state:
                    began[lot] = (moment, row['line'])
                if next_state is None:
                    states.pop(lot, None)
                else:
                    states[lot] = next_state
                activity.append(dict(row, late=row['event'] in late, before=previous, after=next_state, code=issue))
    for lot, state in states.items():
        close(periods, lot, state, began[lot], end, start)
    return dict(rows=activity, periods=periods, states=states, isolated=isolated,
                relocation_conflicts=conflicts,
                counts=dict(applied=len(activity), late=sum(row['late'] for row in activity),
                            isolated=len(isolated), move_disputes=len(conflicts), in_stock=len(states)))


def close(periods, lot, state, opening, end, start):
    left = max(opening[0], start)
    if left < end:
        periods.append(dict(lot=lot, start=left, end=end, source_line=opening[1], **state))
