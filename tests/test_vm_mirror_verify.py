"""The VM mirror verifier must never pass on evidence that does not prove the layout."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integration'))
import run_vm_mirror as mirror  # noqa: E402

UUID_A = mirror.conversation_id('S1', 3)
BOOT = 'boot-1'
SESSION_STARTED = 1_000_000.0


def rect(x, y, w, h):
    return {'x': x, 'y': y, 'width': w, 'height': h}


LAYOUT = {
    'monitors': [
        {'connector': 'M-A', 'x': 0, 'y': 0, 'width': 1000, 'height': 800, 'primary': True, 'scale': 1},
        {'connector': 'M-B', 'x': 1000, 'y': 0, 'width': 800, 'height': 600, 'primary': False, 'scale': 1}],
    'workspaces': ['One', 'Two'],
    'terminals': [{'session': 'S1', 'placement': {'workspace': 'One', 'monitor': 'M-A', 'state': 'maximized', 'frame': rect(0, 0, 1000, 800)},
                   'windows': [{'index': 1, 'name': 'zsh', 'kind': 'shell'},
                               {'index': 2, 'name': 'DECK', 'kind': 'deck', 'group': 'g'},
                               {'index': 3, 'name': 'chat', 'kind': 'conversation'}]}],
    'chrome': [
        {'workspace': 'One', 'monitor': 'M-A', 'state': 'maximized', 'tabs': 3, 'group_sizes': [2]},
        {'workspace': 'Two', 'monitor': 'M-B', 'state': 'maximized', 'tabs': 2, 'group_sizes': []}],
    'vscode': [{'placement': {'workspace': 'Two', 'monitor': 'M-B', 'state': 'maximized', 'frame': rect(0, 0, 800, 600)}, 'editors': 1}],
    'nemo': [{'placement': {'workspace': 'One', 'monitor': 'M-A', 'state': 'normal', 'frame': rect(50, 50, 400, 300)}}],
    'social': [{'app': 'slack', 'placement': {'workspace': 'One', 'monitor': 'M-A', 'state': 'maximized', 'frame': rect(0, 0, 1000, 800)}},
               {'app': 'imaginary', 'placement': {'workspace': 'Two', 'monitor': 'M-B', 'state': 'normal', 'frame': rect(0, 0, 300, 300)}}],
}
MONITORS = [{'x': 0, 'y': 0, 'width': 1000, 'height': 800, 'primary': True, 'index': 0},
            {'x': 1000, 'y': 0, 'width': 800, 'height': 600, 'primary': False, 'index': 1}]
AREAS = [rect(0, 0, 1000, 800), rect(1000, 0, 800, 600)]


def window(wid, cls, title, workspace, monitor, state, frame=None):
    area = AREAS[monitor]
    geometry = frame or (area if state == 'maximized' else rect(area['x'] + 50, area['y'] + 50, 400, 300))
    relative = {'x': geometry['x'] - area['x'], 'y': geometry['y'] - area['y'],
                'width': geometry['width'], 'height': geometry['height']}
    return {'id': wid, 'wm_class': cls, 'title': title, 'app_ids': [cls], 'workspace': workspace, 'monitor': monitor,
            'state': state, 'geometry': geometry, 'geometry_relative': relative, 'monitor_work_area': area}


def good_observation():
    started = datetime.fromtimestamp(SESSION_STARTED + 30, timezone.utc).isoformat()
    return {
        'monitors': copy.deepcopy(MONITORS),
        'windows': [window(1, 'Alacritty', 'S1', 0, 0, 'maximized'),
                    window(2, 'com.microsoft.VSCode', '1-two - file.txt', 1, 1, 'maximized'),
                    window(3, 'nemo', 'folder', 0, 0, 'normal'),
                    window(4, 'slack', 'Slack', 0, 0, 'maximized'),
                    window(5, 'imaginary', 'Imaginary', 1, 1, 'normal')],
        'tmux': {'S1': [(1, 'zsh'), (2, 'DECK'), (3, 'chat')]},
        'panes': {'S1:1': [{'pid': 10, 'command': 'zsh', 'deck': None, 'uuids': [], 'path': '/usr/bin'}],
                  'S1:2': [{'pid': 11, 'command': 'zsh', 'deck': {'profile': mirror.PROFILE, 'group': 'g'}, 'uuids': [], 'path': '/usr/bin'}],
                  'S1:3': [{'pid': 12, 'command': 'python3', 'deck': None, 'uuids': [UUID_A], 'path': '/usr/bin'}]},
        'chrome': [{'tabs': 3, 'groups': (2,), 'workspace': 'One', 'monitor': 'M-A', 'state': 'maximized'},
                   {'tabs': 2, 'groups': (), 'workspace': 'Two', 'monitor': 'M-B', 'state': 'maximized'}],
        'chrome_error': None,
        'expected_history': {'S1': {'1': ['echo a']}},
        'shared_history_text': 'echo a\n', 'tab_history_text': 'echo a\n',
        'tab_history': {'expected': {'S1:1': {'tab': 'f' * 32, 'commands': ['echo t1']}},
                        'live': {'S1:1': {'tab': 'f' * 32, 'file_text': 'echo t1\n', 'fc_text': 'echo t1\n'}},
                        'shared_text': 'echo t1\n'},
        'conversation_ids': [UUID_A],
        'hud': {'operation_context': {'boot_id': BOOT}, 'started_at': started, 'operation_state': 'completed',
                'overall_state': 'ready', 'overall_message': '', 'stages': [{'id': 'terminals', 'state': 'ready'}]},
        'boot_id': BOOT, 'session_started': SESSION_STARTED, 'before': None}


def with_reference(observation):
    """The same windows as the reference an earlier observation saved."""
    observation['before'] = mirror.window_snapshot(observation, LAYOUT)
    return observation


def verdict(observation, without=()):
    if observation.get('before') is None and not observation.get('_no_reference'):
        with_reference(observation)
    return mirror.evaluate(observation, LAYOUT, without)


class VerifierTests(unittest.TestCase):
    def assertFails(self, report, name):
        self.assertIn(name, report['failures'], report['failures'])

    def test_complete_observation_passes(self):
        report = verdict(good_observation(), without=('app:imaginary',))
        self.assertEqual(report['failures'], [])

    def test_swapped_chrome_windows_of_different_shape_fail(self):
        observation = good_observation()
        first, second = observation['chrome']
        first['workspace'], second['workspace'] = second['workspace'], first['workspace']
        first['monitor'], second['monitor'] = second['monitor'], first['monitor']
        self.assertFails(verdict(observation, ('app:imaginary',)), 'chrome-windows')

    def test_vscode_window_is_found_by_its_project_name(self):
        observation = good_observation()
        observation['windows'][1]['title'] = 'another-project - file.txt'
        report = verdict(observation, ('app:imaginary',))
        self.assertFails(report, 'vscode-window:1-two')

    def test_unsupported_social_app_fails_unless_excluded_explicitly(self):
        report = verdict(good_observation())
        self.assertFails(report, 'social-unsupported:imaginary')
        excluded = verdict(good_observation(), ('app:imaginary',))
        self.assertTrue(excluded['checks']['social-skipped:imaginary']['ok'])

    def test_two_entries_with_one_place_need_two_windows(self):
        layout = copy.deepcopy(LAYOUT)
        layout['social'] = [layout['social'][0], copy.deepcopy(layout['social'][0])]
        report = mirror.evaluate(good_observation(), layout, ())
        self.assertFails(report, 'social:slack')

    def test_extra_file_manager_window_fails(self):
        observation = good_observation()
        observation['windows'].append(window(6, 'nemo', 'folder 2', 0, 0, 'normal'))
        self.assertFails(verdict(observation, ('app:imaginary',)), 'nemo-windows')

    def test_nemo_layout_of_two_equal_places_needs_both_places(self):
        layout = copy.deepcopy(LAYOUT)
        layout['nemo'] = [layout['nemo'][0], copy.deepcopy(layout['nemo'][0])]
        observation = good_observation()
        observation['windows'].append(window(6, 'nemo', 'folder 2', 1, 1, 'normal'))
        report = mirror.evaluate(with_reference(observation), layout, ('app:imaginary',))
        self.assertFails(report, 'nemo-windows')
        observation['windows'][-1] = window(6, 'nemo', 'folder 2', 0, 0, 'normal')
        report = mirror.evaluate(with_reference(observation), layout, ('app:imaginary',))
        self.assertTrue(report['checks']['nemo-windows']['ok'])

    def test_missing_work_area_is_a_failure(self):
        observation = good_observation()
        del observation['windows'][0]['monitor_work_area']
        self.assertFails(verdict(observation, ('app:imaginary',)), 'maximized-frames')

    def test_no_maximized_window_while_layout_has_some_is_a_failure(self):
        observation = good_observation()
        for w in observation['windows']:
            w['state'] = 'normal'
        self.assertFails(verdict(observation, ('app:imaginary',)), 'maximized-frames')

    def test_stalled_maximize_frame_is_a_failure(self):
        observation = good_observation()
        observation['windows'][3]['geometry'] = rect(10, 10, 900, 700)
        self.assertFails(verdict(observation, ('app:imaginary',)), 'maximized-frames')

    def test_missing_expectations_are_failures_not_passes(self):
        for key, name in (('expected_history', 'zsh-history'), ('tab_history', 'tab-history-exact'),
                          ('conversation_ids', 'conversations')):
            observation = good_observation()
            observation[key] = None
            report = verdict(observation, ('app:imaginary',))
            self.assertFails(report, name)
            self.assertIn('not checked', report['checks'][name]['detail'])

    def test_conversation_running_as_a_shell_or_other_uuid_fails(self):
        observation = good_observation()
        observation['panes']['S1:3'][0]['uuids'] = ['22222222-2222-5222-8222-222222222222']
        self.assertFails(verdict(observation, ('app:imaginary',)), 'conversation:S1:3')
        observation = good_observation()
        observation['panes']['S1:3'][0]['uuids'] = []
        self.assertFails(verdict(observation, ('app:imaginary',)), 'conversation:S1:3')

    def test_stale_hud_fails(self):
        observation = good_observation()
        observation['hud']['operation_context']['boot_id'] = 'older-boot'
        self.assertFails(verdict(observation, ('app:imaginary',)), 'hud-exists')
        observation = good_observation()
        observation['hud']['started_at'] = datetime.fromtimestamp(SESSION_STARTED - 600, timezone.utc).isoformat()
        self.assertFails(verdict(observation, ('app:imaginary',)), 'hud-exists')
        observation = good_observation()
        observation['hud'] = {}
        self.assertFails(verdict(observation, ('app:imaginary',)), 'hud-exists')

    def test_hud_without_a_provable_login_time_fails(self):
        observation = good_observation()
        observation['session_started'] = None
        self.assertFails(verdict(observation, ('app:imaginary',)), 'hud-exists')
        observation = good_observation()
        del observation['hud']['started_at']
        self.assertFails(verdict(observation, ('app:imaginary',)), 'hud-exists')

    def test_unreadable_environment_fails_the_path_check(self):
        observation = good_observation()
        observation['panes']['S1:1'][0]['path'] = None
        self.assertFails(verdict(observation, ('app:imaginary',)), 'path:S1:1:0')

    def test_exit_code_follows_failures(self):
        self.assertEqual(mirror.exit_code({'failures': []}), 0)
        self.assertEqual(mirror.exit_code({'failures': ['x']}), 1)
        self.assertEqual(mirror.exit_code(verdict(good_observation(), ('app:imaginary',))), 0)
        self.assertEqual(mirror.exit_code(verdict(good_observation())), 1)

    def test_check_names_are_unique_for_repeated_panes(self):
        observation = good_observation()
        observation['panes']['S1:1'].append({'pid': 13, 'command': 'bash', 'deck': None, 'uuids': [], 'path': '/usr/bin'})
        report = verdict(observation, ('app:imaginary',))
        self.assertIn('shell:S1:1:0', report['checks'])
        self.assertIn('shell:S1:1:1', report['checks'])
        self.assertFails(report, 'shell:S1:1:1')

    def test_duplicate_check_names_never_overwrite(self):
        layout = copy.deepcopy(LAYOUT)
        layout['social'].append(copy.deepcopy(layout['social'][0]))
        report = mirror.evaluate(good_observation(), layout, ('app:imaginary',))
        self.assertEqual(len(report['checks']), len(set(report['checks'])))

    def test_expect_without_skips_shell_deck_and_tab_history(self):
        observation = good_observation()
        for key in ('S1:1', 'S1:2'):
            del observation['panes'][key]
        observation['tmux']['S1'] = [(3, 'chat')]
        del observation['tab_history']['live']['S1:1']
        report = verdict(observation, ('app:imaginary', 'S1:1', 'S1:2'))
        self.assertEqual(report['failures'], [])

    def test_missing_window_without_exclusion_fails(self):
        observation = good_observation()
        del observation['panes']['S1:2']
        report = verdict(observation, ('app:imaginary',))
        self.assertFails(report, 'pane:S1:2')

    def test_normal_window_frame_is_compared_with_the_same_window_before(self):
        observation = good_observation()
        observation['before'] = mirror.window_snapshot(observation, LAYOUT)
        self.assertTrue(verdict(observation, ('app:imaginary',))['checks']['normal-frames']['ok'])
        moved = copy.deepcopy(observation)
        moved['windows'][2]['geometry_relative']['width'] = 900
        self.assertFails(verdict(moved, ('app:imaginary',)), 'normal-frames')

    def test_missing_before_reference_fails_as_not_checked(self):
        observation = good_observation()
        observation['_no_reference'] = True
        report = verdict(observation, ('app:imaginary',))
        self.assertFails(report, 'normal-frames')
        self.assertIn('not checked', report['checks']['normal-frames']['detail'])

    def test_unchanged_window_set_has_no_reference_problems_but_a_new_window_does(self):
        observation = with_reference(good_observation())
        observation['windows'].append(window(6, 'viber', 'extra', 0, 0, 'normal'))
        self.assertFails(verdict(observation, ('app:imaginary',)), 'normal-frames')

    def test_terminal_title_may_carry_a_static_header(self):
        observation = good_observation()
        observation['windows'][0]['title'] = 'wsctl:S1:0a1b2c3d'
        report = verdict(observation, ('app:imaginary',))
        self.assertFails(report, 'alacritty-name:S1')
        self.assertTrue(report['checks']['alacritty-window:S1']['ok'])


class HelperTests(unittest.TestCase):
    def test_saved_placement_binds_each_provider_to_its_own_window(self):
        snapshot = {'sessions': [{'name': 'S1', 'placement': {'workspace_name': 's'}}],
                    'vscode': {'windows': [{'placement': {'workspace_name': 'v0'}}, {'placement': {'workspace_name': 'v1'}}]},
                    'file_manager': {'windows': [{'placement': {'workspace_name': 'n0'}}, {'placement': {'workspace_name': 'n1'}}]},
                    'social_apps': {'slack': {'windows': [{'workspace_name': 'a0'}, {'workspace_name': 'a1'}]}}}
        place = mirror.saved_placement
        self.assertEqual(place(snapshot, 'alacritty', 'S1/1')['workspace_name'], 's')
        self.assertEqual(place(snapshot, 'vscode', '2')['workspace_name'], 'v1')
        self.assertEqual(place(snapshot, 'nemo', '1')['workspace_name'], 'n0')
        self.assertEqual(place(snapshot, 'nemo', 'nemo:2')['workspace_name'], 'n1')
        self.assertEqual(place(snapshot, 'social-apps', 'slack:2')['workspace_name'], 'a1')
        self.assertIsNone(place(snapshot, 'vscode', '9'))

    def test_history_commands_quote_layout_values(self):
        self.assertEqual(mirror.echo_command('mirror-S1-1-1'), 'echo mirror-S1-1-1')
        self.assertEqual(mirror.echo_command('x; rm -rf y'), "echo 'x; rm -rf y'")

    def test_scale_scenario_reads_only_its_own_chrome_expectation(self):
        import json
        import os
        import tempfile
        import run_vm_scale
        with tempfile.TemporaryDirectory() as directory:
            root, other = Path(directory) / 'scale', Path(directory) / 'mirror.json'
            root.mkdir()
            other.write_text(json.dumps({'windows': 11, 'tabs': 61, 'groups': 15}))
            saved_root, saved_env = run_vm_scale.ROOT, os.environ.pop('WSCTL_CHROME_EXPECTATION', None)
            try:
                run_vm_scale.ROOT = root
                scale_default = (run_vm_scale.EXPECTED['chrome_windows'], run_vm_scale.EXPECTED['chrome_tabs'],
                                 run_vm_scale.EXPECTED['chrome_groups'])
                self.assertEqual(run_vm_scale.chrome_expectation(), scale_default)
                (root / 'chrome-expected.json').write_text(json.dumps({'windows': 2, 'tabs': 5, 'groups': 1}))
                self.assertEqual(run_vm_scale.chrome_expectation(), (2, 5, 1))
                os.environ['WSCTL_CHROME_EXPECTATION'] = str(other)
                self.assertEqual(run_vm_scale.chrome_expectation(), (11, 61, 15))
            finally:
                run_vm_scale.ROOT = saved_root
                os.environ.pop('WSCTL_CHROME_EXPECTATION', None)
                if saved_env is not None:
                    os.environ['WSCTL_CHROME_EXPECTATION'] = saved_env


if __name__ == '__main__':
    unittest.main()
