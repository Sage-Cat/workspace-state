// Loaded only by a temporary gnome-winctl copy in the private headless session.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const HUD_UUID = 'login-hud-headless-test@sagecat.local';
const GENERATION = 'a11ce00000000001';

function hud() {
    const instance = Main.extensionManager.lookup(HUD_UUID)?.stateObj;
    if (!instance)
        throw new Error('Temporary HUD has not loaded');
    return instance;
}

function guardNative(instance, fixture) {
    instance._currentSessionId = GENERATION; // No session manager is started.
    instance._originalEndSessionConfirm = () => {
        fixture.confirmAttempts++;
        throw new Error('A real shutdown handoff is forbidden in this fixture');
    };
    if (Main.endSessionDialog)
        Main.endSessionDialog.cancel = () => fixture.nativeCancelCalls++;
}

function snapshot(instance, fixture) {
    return {
        ready: !Main.layoutManager._startingUp,
        overviewVisible: Main.overview.visible,
        modalCount: Main.modalCount,
        baseline: fixture.baseline,
        modal: Boolean(instance._modalGrab),
        seatAll: instance._modalGrab?.get_seat_state() === Clutter.GrabState.ALL,
        visible: Boolean(instance._hud?.visible),
        mapped: Boolean(instance._hud?.mapped),
        reactive: Boolean(instance._hud?.reactive),
        operation: instance._lastGoodStatus?.operationId ?? null,
        context: instance._lastGoodStatus?.operationContext ?? null,
        localCancelled: instance._isLocallyCancelled(instance._lastGoodStatus),
        cancellationWritten: Boolean(instance._cancelRequestPending),
        countdown: Boolean(instance._shutdownCountdownId),
        commit: instance._commitWrittenOperationId,
        rendered: instance._renderAckWrittenOperationId,
        renderScheduled: instance._renderAckScheduledOperationId,
        panelMapped: Boolean(instance._hud?._panel?.mapped),
        panelWidth: instance._hud?._panel?.width ?? 0,
        panelHeight: instance._hud?._panel?.height ?? 0,
        stages: instance._lastGoodStatus?.stages.map(stage => ({id: stage.id, state: stage.state})) ?? [],
        titleText: instance._hud?._title?.text ?? null,
        overallText: instance._hud?._overall?.text ?? null,
        noticeText: instance._hud?._notice?.text ?? null,
        preparedPolling: Boolean(instance._preparedPollId),
        nativeHandoff: instance._nativeHandoffOperationId,
        confirmAttempts: fixture.confirmAttempts,
        nativeCancelCalls: fixture.nativeCancelCalls,
        locked: Boolean(Main.sessionMode.isLocked),
        sessionMode: Main.sessionMode.currentMode,
        confirmHookInstalled: Boolean(Main.endSessionDialog &&
            Main.endSessionDialog._confirm === instance._wrappedEndSessionConfirm),
        confirmSettled: fixture.confirmSettled ?? null,
        confirmError: fixture.confirmError ?? null,
        preflightWrites: fixture.preflightWrites ?? 0,
        currentEpochOwnsOld: fixture.oldEpoch ? instance._ownsEpoch(fixture.oldEpoch) : null,
        actorPresent: Boolean(instance._hud),
        uuid: instance.uuid,
        version: instance.metadata.version,
    };
}

export function hudControl(requestJson) {
    const request = JSON.parse(requestJson);
    const instance = hud();
    const fixture = this._hudFixture ??= {baseline: null, confirmAttempts: 0,
                                         nativeCancelCalls: 0};
    const directory = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'workspace-state']);
    if (!GLib.get_user_runtime_dir().startsWith('/tmp/wsctl-hud-headless-'))
        throw new Error('HUD fixture refuses a non-private runtime');
    if (request.action === 'setup') {
        Main.overview.hide();
        fixture.baseline = Main.modalCount;
        GLib.mkdir_with_parents(directory, 0o700);
        GLib.file_set_contents(GLib.build_filenamev([directory, 'login-generation']), GENERATION);
        guardNative(instance, fixture);
    } else if (request.action === 'publish') {
        guardNative(instance, fixture);
        const now = new Date().toISOString();
        if (!request.context) {
            // The private fixture stands in for a newly confirmed preflight;
            // only a late publication retains the previous operation identity.
            instance._preflightOperationId = request.operation;
            instance._preflightAction = 'poweroff';
            instance._preflightSignal = 'ConfirmedShutdown';
        }
        const context = request.context ?? {
            boot_id: instance._bootId, login_generation: GENERATION,
            operation_id: request.operation, mode: 'shutdown', attempt: 1,
            deadline: GLib.get_monotonic_time() / 1000000 + 90,
        };
        const status = {
            schema_version: 1, mode: 'shutdown', session_id: GENERATION,
            operation_id: request.operation, operation_context: context,
            operation_state: request.operationState ?? (request.cancelled ? 'cancelled'
                : request.state === 'ready' ? 'prepared' : request.state === 'failed' ? 'failed' : 'preparing'),
            recovery_pending: request.recoveryRunning === true,
            shutdown_action: 'poweroff', shutdown_origin: 'preflight', cancelled: request.cancelled === true,
            started_at: request.startedAt ?? now, updated_at: now,
            overall_state: request.state, overall_message: 'Disposable HUD integration fixture',
            error_log_path: GLib.build_filenamev([directory, 'fixture.log']),
            stages: request.stages ?? Array.from({length: request.stageCount ?? 1}, (_unused, index) => ({
                id: index === 0 ? 'workspace-save' : `fixture-stage-${index}`,
                label: `Disposable checkpoint ${index + 1}`, state: request.state,
                message: 'No real checkpoint or shutdown action is performed',
            })),
        };
        if (request.recoveryRunning) {
            status.operation_state = 'recovering';
            status.stages.push({id: 'profile-recovery', label: 'Disposable recovery',
                state: 'running', message: 'Backend recovery continues independently'});
        }
        instance._writeProtocolFile(instance._requestFile, 'fixture-request', {
            schema_version: 1, operation_id: request.operation, session_id: GENERATION,
            action: 'poweroff', requested_at: now,
        });
        instance._writeProtocolFile(instance._statusFile, 'fixture-status', status);
        instance._loadStatus();
        fixture.lastStatus = status;
        return JSON.stringify({published: true, context, startedAt: status.started_at});
    } else if (request.action === 'cancel') {
        const original = instance._writeProtocolFile;
        if (request.failWrite) {
            instance._writeProtocolFile = function (...args) {
                if (args[1] === 'shutdown-cancel')
                    throw new Error('Injected private cancellation write failure');
                return original.apply(this, args);
            };
        }
        const began = GLib.get_monotonic_time();
        let result;
        try {
            result = instance._requestCancel(instance._lastGoodStatus);
        } finally {
            instance._writeProtocolFile = original;
        }
        return JSON.stringify({...snapshot(instance, fixture), cancelReturned: result,
                               elapsedMs: (GLib.get_monotonic_time() - began) / 1000});
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
    } else if (request.action === 'locked-confirm') {
        const coordinatorCheck = instance._shutdownCoordinatorIsActive;
        const writeProtocolFile = instance._writeProtocolFile;
        fixture.preflightWrites = 0;
        fixture.confirmSettled = false;
        fixture.confirmError = null;
        guardNative(instance, fixture);
        instance._shutdownCoordinatorIsActive = () => true;
        instance._writeProtocolFile = function (file, kind, ...args) {
            if (kind === 'shutdown-request')
                fixture.preflightWrites++;
            return writeProtocolFile.call(this, file, kind, ...args);
        };
        const dialog = Main.endSessionDialog;
        fixture.confirmHookInstalled = Boolean(dialog && dialog._confirm === instance._wrappedEndSessionConfirm);
        if (fixture.confirmHookInstalled) {
            try {
                // The original native confirmation is fixture-guarded. Keep
                // those guards in place until the wrapped async callback settles.
                Promise.resolve(dialog._confirm.call(dialog, 'ConfirmedShutdown')).then(() => {
                    fixture.confirmSettled = true;
                    instance._shutdownCoordinatorIsActive = coordinatorCheck;
                    instance._writeProtocolFile = writeProtocolFile;
                }, error => {
                    fixture.confirmError = String(error);
                    fixture.confirmSettled = true;
                    instance._shutdownCoordinatorIsActive = coordinatorCheck;
                    instance._writeProtocolFile = writeProtocolFile;
                });
            } catch (error) {
                fixture.confirmError = String(error);
                fixture.confirmSettled = true;
                instance._shutdownCoordinatorIsActive = coordinatorCheck;
                instance._writeProtocolFile = writeProtocolFile;
            }
        } else {
            fixture.confirmSettled = true;
            instance._shutdownCoordinatorIsActive = coordinatorCheck;
            instance._writeProtocolFile = writeProtocolFile;
        }
        return JSON.stringify({...snapshot(instance, fixture), confirmHookInstalled: fixture.confirmHookInstalled});
    } else if (request.action === 'disable-pending') {
        fixture.oldEpoch = instance._enableEpoch;
        instance._loadStatus(); // Queue genuine Gio I/O, then invalidate its epoch.
        instance.disable();
    } else if (request.action === 'enable') {
        instance.enable();
        guardNative(instance, fixture);
        instance._loadStatus();
    } else if (request.action !== 'state') {
        throw new Error('Unknown HUD test action');
    }
    return JSON.stringify(snapshot(instance, fixture));
}
