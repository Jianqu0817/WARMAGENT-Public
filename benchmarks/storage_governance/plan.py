"""Pure planning examples for storage-governance/v1. No filesystem operations.

Inputs are verified inventories supplied by the future runtime adapter, not raw
directory listings. Passing these examples does not validate runtime enforcement.
"""


def generation_plan(inventory):
    rows = {row['id']: row for row in inventory['generations']}
    current = rows.get(inventory['current'])
    if not inventory['pointer_valid'] or not current or not current['verified']:
        return {'status': 'blocked', 'reclaim': []}
    protected = {current['id'], *inventory['pins'], *inventory['in_flight']}
    previous = sorted(
        (row for row in rows.values()
         if row['id'] != current['id'] and row['verified'] and row['published']),
        key=lambda row: row['publication_order'], reverse=True)
    protected.update(row['id'] for row in previous[:1])
    reclaim = sorted(row['id'] for row in rows.values()
                     if row['id'] not in protected and row['verified'] and row['mature'])
    return {'status': 'planned', 'reclaim': reclaim}


def backup_plan(inventory):
    rows = inventory['backups']
    reusable = [row for row in rows
                if row['project'] == inventory['project'] and row['complete']
                and row['verified'] and row['key'] == inventory['candidate_key']]
    if reusable:
        chosen = max(reusable, key=lambda row: row['order'])
        return {'action': 'reuse', 'reuse': chosen['id'], 'reclaim': []}
    # No rotation until the candidate has passed restore checks and is complete.
    if not inventory['candidate_complete']:
        return {'action': 'stage', 'reuse': None, 'reclaim': []}
    complete = sorted(
        (row for row in rows if row['project'] == inventory['project']
         and row['complete'] and row['verified']),
        key=lambda row: row['order'], reverse=True)
    # The newly completed candidate occupies one of the two ordinary slots.
    protected = {row['id'] for row in complete[:1]} | set(inventory['pins'])
    reclaim = sorted(row['id'] for row in complete if row['id'] not in protected)
    return {'action': 'publish', 'reuse': None, 'reclaim': reclaim}


def reservation_plan(state):
    total = state['used_bytes'] + state['unwritten_reserved_bytes'] + state['peak_bytes']
    if total > state['hard_limit_bytes']:
        return {'admitted': False, 'reason': 'quota'}
    available = state['disk_free_bytes'] - state['unwritten_reserved_bytes']
    if available < state['peak_bytes'] + state['disk_floor_bytes']:
        return {'admitted': False, 'reason': 'disk_free'}
    return {'admitted': True, 'reason': 'reserved'}
