"""Offline policy examples, not tests of live pruning or quota enforcement."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from benchmarks.storage_governance.plan import backup_plan, generation_plan, reservation_plan


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'benchmarks/storage_governance/cases.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('case', CASES['generation_cases'], ids=lambda row: row['id'])
def test_generation_policy(case):
    inventory = deepcopy(CASES['generation_base'])
    inventory.update(case['patch'])
    for row in inventory['generations']:
        row.update(case.get('rows', {}).get(row['id'], {}))
    assert generation_plan(inventory) == case['expected']


@pytest.mark.parametrize('case', CASES['backup_cases'], ids=lambda row: row['id'])
def test_backup_policy(case):
    inventory = deepcopy(CASES['backup_base'])
    inventory.update(case['patch'])
    assert backup_plan(inventory) == case['expected']


@pytest.mark.parametrize('case', CASES['reservation_cases'], ids=lambda row: row['id'])
def test_reservation_policy(case):
    state = {**CASES['reservation_base'], **case['patch']}
    assert reservation_plan(state) == case['expected']
