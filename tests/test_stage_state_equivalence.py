"""The state a restore returns and the state the status document derives must agree."""
import itertools
import unittest
from unittest.mock import patch

from workspace_state import cli, stage_state
from workspace_state.provider_results import (ADVISORY_PREFIX, RECIPE_AGE_ITEM, EvidenceState as S,
                                              PhaseEvidence as E, ProviderCount, ProviderItemResult,
                                              ProviderRestoreError)


def item(name, identity=S.VERIFIED, content=S.VERIFIED, placement=S.VERIFIED, attention=(), request='r'):
    token = request if S.WAITING in (identity, content, placement) else None
    return ProviderItemResult('chrome', name, E(identity, '', False, token), E(content, '', False, token),
                              E(placement, '', False, token), attention)


KINDS = {
    'ok': lambda n: item(n),
    'skipped': lambda n: item(n, S.SKIPPED, S.SKIPPED, S.SKIPPED),
    'waiting': lambda n: item(n, placement=S.WAITING),
    'failed': lambda n: item(n, placement=S.FAILED),
    'unknown': lambda n: item(n, content=S.UNKNOWN),
    'blocking': lambda n: item(n, attention=('needs a look',)),
    'advisory': lambda n: item(n, attention=(ADVISORY_PREFIX + 'old',)),
    'failed+waiting': lambda n: item(n, content=S.FAILED, placement=S.WAITING),
    'unknown+waiting': lambda n: item(n, content=S.UNKNOWN, placement=S.WAITING),
    'blocking+waiting': lambda n: item(n, placement=S.WAITING, attention=('needs a look',)),
    'advisory+waiting': lambda n: item(n, placement=S.WAITING, attention=(ADVISORY_PREFIX + 'old',)),
    'recipe': lambda n: item(RECIPE_AGE_ITEM, S.SKIPPED, S.SKIPPED, S.SKIPPED, (ADVISORY_PREFIX + 'old',)),
}


def returned_state(results, error):
    """What `_publish_category_outcome` hands its caller."""
    with patch.object(cli, 'update_stage'), patch.object(cli, 'publish_provider_results'), \
            patch.object(cli, '_record_seen'), patch.object(cli, '_seen_directory', return_value=None), \
            patch.object(cli, '_checkpoint_login', return_value=None):
        if error:
            return cli._publish_category_outcome('vscode', {}, error=ProviderRestoreError('boom', results))
        return cli._publish_category_outcome('vscode', {}, count=ProviderCount(0, results))


def document_state(results, failed_by_error):
    values = [result.to_dict() for result in results]
    return stage_state.stage_state(values, 'boom' if failed_by_error else None).real


class StageStateEquivalenceTests(unittest.TestCase):
    def combinations(self):
        for size in (1, 2):
            for names in itertools.product(KINDS, repeat=size):
                yield names, tuple(KINDS[name](str(index)) for index, name in enumerate(names))

    def test_restore_and_document_states_agree(self):
        different = {}
        for names, results in self.combinations():
            for error in (False, True):
                returned = returned_state(results, error)
                derived = document_state(results, returned == 'failed' and error)
                if returned != derived:
                    different[(names, error)] = (returned, derived)
        self.assertEqual(different, {})

    def test_an_item_with_its_own_fault_does_not_hold_the_category_waiting(self):
        # Rule: an item that itself has a failed or unknown phase or a blocking attention
        # does not hold its category in `waiting`, even while another phase still waits.
        # The restore used to report `waiting` for these while the document said `failed`.
        # An advisory attention is no fault, so that item keeps waiting.
        expected = {
            'failed+waiting': ('failed', 'degraded'),
            'unknown+waiting': ('failed', 'degraded'),
            'blocking+waiting': ('failed', 'degraded'),
            'advisory+waiting': ('waiting', 'waiting'),
        }
        for kind, (alone, beside_verified) in expected.items():
            for error in (False, True):
                with self.subTest(kind=kind, error=error):
                    results = (KINDS[kind]('0'),)
                    self.assertEqual(returned_state(results, error), alone)
                    self.assertEqual(returned_state(results + (KINDS['ok']('1'),), error), beside_verified)
