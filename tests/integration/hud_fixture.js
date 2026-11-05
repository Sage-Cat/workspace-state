// Loaded only by a temporary gnome-winctl copy in the private headless session.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const HUD_UUID = 'login-hud-headless-test@sagecat.local';
const GENERATION = 'a11ce00000000001';

function hud() {
    const instance = Main.extensionManager.lookup(HUD_UUID)?.stateObj;
    if (!instance)
        throw new Error('Temporary HUD has not loaded');
    return instance;
}

function guardSession(instance) {
    instance._currentSessionId = GENERATION; // No session manager is started.
}

function progressGeometry(track) {
    if (!track?.get_stage() || !track.has_allocation())
        return null;
    const fill = track.get_first_child();
    if (!fill?.has_allocation())
        return null;
    return {trackWidth: track.width, fillWidth: fill.get_transformed_size()[0],
            leftOffset: fill.get_transformed_position()[0] - track.get_transformed_position()[0]};
}

function snapshot(instance, fixture) {
    const panel = instance._hud?._panel;
    const allocated = Boolean(panel?.get_stage() && panel.has_allocation());
    return {
        ready: !Main.layoutManager._startingUp,
        overviewVisible: Main.overview.visible,
        modalCount: Main.modalCount,
        baseline: fixture.baseline,
        visible: Boolean(instance._hud?.visible),
        mapped: Boolean(instance._hud?.mapped),
        reactive: Boolean(instance._hud?.reactive),
        focusLabel: global.stage.get_key_focus()?.get_label?.() ?? '',
        focusGroupRegistered: Boolean(panel && St.FocusManager.get_for_stage(global.stage).get_group(panel) === panel),
        startupAbortPending: Boolean(instance._startupAbortPending),
        startupAbortReceipt: instance._readProtocolFileSync(instance._startupAbortFile),
        dismissal: instance._readProtocolFileSync(instance._dismissedFile),
        statusIndicatorVisible: Boolean(instance._startupIndicator?.visible),
        actionLabels: instance._hud?._actions.get_children().map(actor => actor.get_label?.() ?? '') ?? [],
        operation: instance._lastGoodStatus?.operationId ?? null,
        context: instance._lastGoodStatus?.operationContext ?? null,
        panelMapped: Boolean(instance._hud?._panel?.mapped),
        panelWidth: allocated ? panel.width : 0,
        panelHeight: allocated ? panel.height : 0,
        overallProgress: progressGeometry(instance._hud?._overallProgressTrack),
        progressRows: instance._hud?._rows.get_children().map(row =>
            progressGeometry(row.get_children().find(child =>
                child.has_style_class_name?.('login-hud-progress-track')))).filter(Boolean) ?? [],
        stages: instance._lastGoodStatus?.stages.map(stage => ({id: stage.id, state: stage.state})) ?? [],
        titleText: instance._hud?._title?.text ?? null,
        overallText: instance._hud?._overall?.text ?? null,
        noticeText: instance._hud?._notice?.text ?? null,
        locked: Boolean(Main.sessionMode.isLocked),
        sessionMode: Main.sessionMode.currentMode,
        currentEpochOwnsOld: fixture.oldEpoch ? instance._ownsEpoch(fixture.oldEpoch) : null,
        actorPresent: Boolean(instance._hud),
        uuid: instance.uuid,
        version: instance.metadata.version,
    };
}

export function hudControl(requestJson) {
    const request = JSON.parse(requestJson);
    const instance = hud();
    const fixture = this._hudFixture ??= {baseline: null};
    const directory = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'workspace-state']);
    if (!GLib.get_user_runtime_dir().startsWith('/tmp/wsctl-hud-headless-'))
        throw new Error('HUD fixture refuses a non-private runtime');
    if (request.action === 'setup') {
        Main.overview.hide();
        fixture.baseline = Main.modalCount;
        GLib.mkdir_with_parents(directory, 0o700);
        GLib.file_set_contents(GLib.build_filenamev([directory, 'login-generation']), GENERATION);
        guardSession(instance);
    } else if (request.action === 'publish') {
        guardSession(instance);
        const now = new Date().toISOString();
        // Startup is the only presented mode; any other mode is written
        // verbatim so a test can prove the HUD ignores it.
        const mode = request.mode ?? 'startup';
        const context = request.context ?? {
            boot_id: instance._bootId, login_generation: GENERATION,
            operation_id: request.operation, mode, attempt: 1,
            deadline: GLib.get_monotonic_time() / 1000000 + 90,
        };
        const status = {
            schema_version: 1, mode, session_id: GENERATION,
            show_startup_hud: mode === 'startup',
            operation_id: request.operation, operation_context: context,
            operation_state: request.operationState ?? (request.cancelled ? 'cancelled'
                : request.state === 'ready' ? 'completed' : request.state === 'failed' ? 'failed' : 'running'),
            cancelled: request.cancelled === true,
            started_at: request.startedAt ?? now, updated_at: now,
            overall_state: request.state, overall_message: 'Disposable HUD integration fixture',
            error_log_path: GLib.build_filenamev([directory, 'fixture.log']),
            stages: request.stages ?? Array.from({length: request.stageCount ?? 1}, (_unused, index) => ({
                id: index === 0 ? 'workspace-save' : `fixture-stage-${index}`,
                label: `Disposable checkpoint ${index + 1}`, state: request.state,
                message: 'No real checkpoint is performed',
            })),
        };
        instance._writeProtocolFile(instance._statusFile, 'fixture-status', status);
        instance._loadStatus();
        fixture.lastStatus = status;
        return JSON.stringify({published: true, context, startedAt: status.started_at});
    } else if (request.action === 'startup-abort') {
        const original = instance._writeProtocolFile;
        if (request.failWrite)
            instance._writeProtocolFile = function (...args) {
                if (args[1] === 'startup-abort')
                    throw new Error('Injected private startup cancellation write failure');
                return original.apply(this, args);
            };
        try {
            instance._hud._actions.get_children().find(actor =>
                actor.get_label?.().startsWith('Abort')).emit('clicked', 1);
        } finally {
            instance._writeProtocolFile = original;
        }
    } else if (request.action === 'native-key') {
        fixture.keyboard ??= Clutter.get_default_backend().get_default_seat().create_virtual_device(
            Clutter.InputDeviceType.KEYBOARD_DEVICE);
        const symbol = request.key === 'Tab' ? Clutter.KEY_Tab : Clutter.KEY_Return;
        fixture.keyboard.notify_keyval(GLib.get_monotonic_time(), symbol, Clutter.KeyState.PRESSED);
        fixture.keyboard.notify_keyval(GLib.get_monotonic_time(), symbol, Clutter.KeyState.RELEASED);
    } else if (request.action === 'background') {
        instance._hud._actions.get_children().find(actor =>
            actor.get_label?.() === 'Run in background').emit('clicked', 1);
    } else if (request.action === 'reopen-startup') {
        instance._startupIndicator.emit('button-press-event', null);
    } else if (request.action === 'layout') {
        instance._hud.setWorkArea(request.workArea, request.scaleFactor ?? 1);
    } else if (request.action === 'dismiss') {
        const began = GLib.get_monotonic_time();
        instance._dismissHud();
        return JSON.stringify({...snapshot(instance, fixture),
            elapsedMs: (GLib.get_monotonic_time() - began) / 1000});
    } else if (request.action === 'enter-lock-mode') {
        if (Main.sessionMode.currentMode !== 'unlock-dialog') {
            Main.sessionMode.pushMode('unlock-dialog');
            fixture.pushedUnlockMode = true;
        }
        if (fixture.lockModeTimer)
            GLib.Source.remove(fixture.lockModeTimer);
        // Restore the private Shell to user mode even if the test driver exits
        // or a later assertion fails before it can issue leave-lock-mode.
        fixture.lockModeTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
            fixture.lockModeTimer = 0;
            if (fixture.pushedUnlockMode && Main.sessionMode.currentMode === 'unlock-dialog')
                Main.sessionMode.popMode('unlock-dialog');
            fixture.pushedUnlockMode = false;
            return GLib.SOURCE_REMOVE;
        });
    } else if (request.action === 'leave-lock-mode') {
        if (fixture.lockModeTimer) {
            GLib.Source.remove(fixture.lockModeTimer);
            fixture.lockModeTimer = 0;
        }
        if (fixture.pushedUnlockMode && Main.sessionMode.currentMode === 'unlock-dialog')
            Main.sessionMode.popMode('unlock-dialog');
        fixture.pushedUnlockMode = false;
    } else if (request.action === 'disable-pending') {
        fixture.oldEpoch = instance._enableEpoch;
        instance._loadStatus(); // Queue genuine Gio I/O, then invalidate its epoch.
        instance.disable();
    } else if (request.action === 'enable') {
        instance.enable();
        guardSession(instance);
        instance._loadStatus();
    } else if (request.action !== 'state') {
        throw new Error('Unknown HUD test action');
    }
    return JSON.stringify(snapshot(instance, fixture));
}
