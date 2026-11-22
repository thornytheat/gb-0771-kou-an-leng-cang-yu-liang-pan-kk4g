from app.faults import DockFault
from app.parcels import load_example
from app.pipeline import review
from app.modules.custody.service import relocation_conflicts, replay

WINDOW = '{"from":"2026-11-21T08:00:00+08:00","until":"2026-11-21T09:00:00+08:00"}'

LOTS = """lot,pieces,description
F01,4,鳕鱼箱
F02,3,冻豌豆
F10,6,交班待核货批
"""

SLOTS = """slot,revision,door,capacity,from,until
A1,01,EAST,20,2026-11-21T00:00:00Z,2026-11-21T01:00:00Z
B1,01,WEST,9,2026-11-21T00:00:00Z,2026-11-21T01:00:00Z
C1,01,WEST,7,2026-11-21T00:00:00Z,2026-11-21T01:00:00Z
"""

DOORS = """door,signal,effective
EAST,CLOSED,2026-11-21T08:00:00+08:00
WEST,CLOSED,2026-11-21T08:00:00+08:00
"""

TEMPS = """door,celsius,effective
EAST,2.0,2026-11-21T08:00:00+08:00
WEST,2.5,2026-11-21T08:00:00+08:00
"""

# 交班时同一货批 F10 的两张调位原件申报了两个去向 B1 / C1
MOVES_DISPUTED = """event,lot,kind,slot,ticket,effective,received
M01,F01,IN,A1,,2026-11-21T08:00:00+08:00,2026-11-21T08:00:00+08:00
M02,F02,IN,B1,,2026-11-21T08:00:00+08:00,2026-11-21T08:00:00+08:00
M03,F10,IN,A1,,2026-11-21T08:05:00+08:00,2026-11-21T08:05:00+08:00
M04,F10,HOLD,,H77,2026-11-21T08:10:00+08:00,2026-11-21T08:10:00+08:00
M05,F10,MOVE,B1,,2026-11-21T08:30:00+08:00,2026-11-21T08:31:00+08:00
M06,F10,MOVE,C1,,2026-11-21T08:30:00+08:00,2026-11-21T08:32:00+08:00
"""

# 晚到的核实记录确认去向为 C1（修正后的原件只保留一个权威去向）
MOVES_VERIFIED = MOVES_DISPUTED.replace(
    'M05,F10,MOVE,B1,,2026-11-21T08:30:00+08:00,2026-11-21T08:31:00+08:00',
    'M05,F10,MOVE,C1,,2026-11-21T08:30:00+08:00,2026-11-21T08:45:00+08:00').replace(
    'M06,F10,MOVE,C1,,2026-11-21T08:30:00+08:00,2026-11-21T08:32:00+08:00',
    'M06,F10,MOVE,C1,,2026-11-21T08:31:00+08:00,2026-11-21T08:46:00+08:00')


def pack(moves):
    return dict(moves=moves, slots=SLOTS, doors=DOORS, temperatures=TEMPS, lots=LOTS, window=WINDOW)


def test_disputed_moves_are_grouped_and_kept_at_prior_slot():
    stages = review(pack(MOVES_DISPUTED))
    custody = stages['custody']
    assert [c['slots'] for c in custody['relocation_conflicts']] == [['B1', 'C1']]
    dispute, = custody['relocation_conflicts']
    assert dispute['lot'] == 'F10' and dispute['lines'] == [6, 7]
    codes = {(row['event'], row['code']) for row in custody['isolated']}
    assert ('M05', 'MOVE_DISPUTED') in codes and ('M06', 'MOVE_DISPUTED') in codes
    # 照旧认原库位：F10 仍在 A1，冻结随调位保留
    assert custody['states']['F10'] == dict(slot='A1', ticket='H77')


def test_capacity_page_quarantines_only_disputed_destinations():
    stages = review(pack(MOVES_DISPUTED))
    stock = stages['capacity']
    by_slot = {row['slot']: row for row in stock['rows']}
    # 两个去向槽位整位停收，避免把可能已占的库位放给下一批
    assert set(stock['quarantined_slots']) == {'B1', 'C1'}
    assert by_slot['B1']['held'] and by_slot['C1']['held']
    assert by_slot['B1']['receivable'] == 0 and by_slot['C1']['receivable'] == 0
    # 不整库停收：无冲突的 A1 仍可收货
    assert not by_slot['A1']['held'] and by_slot['A1']['receivable'] == by_slot['A1']['free']
    assert stock['receivable_slots'] == ['A1']
    # F10 六件仍按原库位 A1 计冻结
    assert by_slot['A1']['frozen'] == 6
    totals = stock['counts']
    assert totals['quarantined'] == 16 and totals['receivable'] == by_slot['A1']['free']
    # 件数守恒仍然成立
    assert stock['explained']


def test_verified_record_reopens_quarantine_and_rebalances_capacity():
    before = review(pack(MOVES_DISPUTED))
    after = review(pack(MOVES_VERIFIED))
    assert before['capacity']['quarantined_slots'] == ['B1', 'C1']
    # 核实后冲突解除：F10 带冻结落到 C1
    assert after['custody']['relocation_conflicts'] == []
    assert after['custody']['states']['F10'] == dict(slot='C1', ticket='H77')
    assert after['capacity']['quarantined_slots'] == []
    by_slot = {row['slot']: row for row in after['capacity']['rows']}
    assert by_slot['C1']['frozen'] == 6 and by_slot['A1']['frozen'] == 0
    # 这一班可收货余量按确认后的占位重新对账
    assert after['capacity']['counts']['receivable'] == sum(row['free'] for row in after['capacity']['rows'])
    assert after['capacity']['balanced']


def test_conflict_isolation_code_is_known_to_labels():
    from app.labels import CODES
    assert CODES['MOVE_DISPUTED'] == '调位去向冲突'


def test_unknown_destination_in_dispute_does_not_create_phantom_slot():
    moves = MOVES_DISPUTED.replace('M06,F10,MOVE,C1,', 'M06,F10,MOVE,Z9,')
    stages = review(pack(moves))
    stock = stages['capacity']
    # 只有存在主数据的 B1 能整位停收；Z9 不产生幽灵槽位
    assert stock['quarantined_slots'] == ['B1']
    dispute, = stock['relocation_conflicts']
    assert dispute['slots'] == ['B1', 'Z9']


def test_two_packets_same_destination_are_not_a_dispute():
    # 同刻同去向只是重复申报，不算两个去向
    moves = MOVES_DISPUTED.replace(
        'M06,F10,MOVE,C1,,2026-11-21T08:30:00+08:00,2026-11-21T08:32:00+08:00',
        'M06,F10,MOVE,B1,,2026-11-21T08:30:00+08:00,2026-11-21T08:32:00+08:00')
    stages = review(pack(moves))
    assert stages['custody']['relocation_conflicts'] == []


def test_certificate_reflects_conflict_and_verification(client):
    disputed = client.post('/api/receipts', json=pack(MOVES_DISPUTED)).json
    verified = client.post('/api/receipts', json=pack(MOVES_VERIFIED)).json
    # 冲突档案与核实档案是两份不同原件、各自可查证
    assert disputed['id'] != verified['id']
    doc = client.application.extensions['ice_receipts'].read(disputed['id'])
    export = client.get(f"/api/certificate/{disputed['id']}/export").json
    assert export['verified']
    assert export['stages']['capacity']['quarantined_slots'] == ['B1', 'C1']
    assert doc['manifest']['result_sha256'] == disputed['manifest']['result_sha256']
    # 核实后的档案停收槽位清零、余量更新
    assert verified['capacity']['receivable'] > disputed['capacity']['receivable']


def test_rerunning_same_verification_does_not_reissue(client):
    first = client.post('/api/receipts', json=pack(MOVES_VERIFIED)).json
    again = client.post('/api/receipts', json=pack(MOVES_VERIFIED)).json
    assert again['reused'] and again['id'] == first['id']
    assert again['manifest'] == first['manifest']
    # 对同一档案复算同样不重复签发
    refreshed = client.post(f"/api/receipts/{first['id']}/refresh").json
    assert refreshed['reused'] and refreshed['manifest'] == first['manifest']
    assert client.get('/api/receipts').json['count'] == 1


def test_old_conflicted_receipt_still_verifiable_after_verification(client):
    disputed = client.post('/api/receipts', json=pack(MOVES_DISPUTED)).json
    client.post('/api/receipts', json=pack(MOVES_VERIFIED))
    # 核实件归档后，老的冲突签发件仍可独立查证，结论不变
    response = client.get(f"/api/certificate/{disputed['id']}")
    assert response.status_code == 200 and response.json['verified']
    assert response.json['manifest']['id'] == disputed['id']


def test_capacity_guard_still_blocks_tampered_conflict_document(client):
    disputed = client.post('/api/receipts', json=pack(MOVES_DISPUTED)).json
    archive = client.application.extensions['ice_receipts']
    document = archive.read(disputed['id'])
    document['stages']['capacity']['counts']['receivable'] = 999
    with __import__('pytest').raises(DockFault, match='散列'):
        from app.modules.certificate.service import issue
        issue(document)


def test_replay_exposes_dispute_count(dirty):
    assert 'move_disputes' in dirty['custody']['counts']
