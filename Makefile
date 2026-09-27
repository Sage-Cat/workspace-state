PREFIX ?= $(HOME)/.local
LEGACY_EXTENSION_UUID := workspace-state@sagecat.local
GNOME_WINCTL_UUID := gnome-winctl-v3@sagecat.local
LOGIN_HUD_UUID := login-hud-v2@sagecat.local
LEGACY_LOGIN_HUD_UUID := login-hud@sagecat.local
LEGACY_GNOME_WINCTL_UUID := gnome-winctl@sagecat.local
LEGACY_MONITOR_GUARD_UUID := alacritty-monitor-guard@sagecat.local
GNOME_WINCTL_DIR ?= $(abspath $(CURDIR)/../gnome-winctl)
LOGIN_HUD_DIR ?= $(abspath $(CURDIR)/../login-hud)
CHROME_EXTENSION_ID := gnccboicpdhhhpdcogeleiegokieocmn
CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)
SYSTEMD_USER_DIR := $(CONFIG_HOME)/systemd/user
NATIVE_HOST_MANIFEST := org.sagecat.workspace_state.json
NATIVE_HOST_DIRS := $(CONFIG_HOME)/google-chrome/NativeMessagingHosts
NATIVE_HOST_PATH := $(abspath $(PREFIX)/bin/wsctl-native-host)
POST_WORKSPACE_UNITS := rclone-gdrive.service rclone-sagecat-serv-drive.service protondrive-mount-pdrive.service cloud-drives-warmup.timer

.PHONY: install install-dev install-vscode install-file-manager install-alerts install-tmux-lifecycle install-session install-shutdown-compat-user install-shutdown-compat-system check-shutdown-compat uninstall check test check-vscode

.PHONY: check-install-prerequisites
check-install-prerequisites:
	@/usr/bin/python3 -c 'import sys; sys.exit("workspace-state installation requires Python 3.11 or later") if sys.version_info < (3, 11) else None'

install install-dev install-vscode install-file-manager install-alerts install-tmux-lifecycle install-session install-shutdown-compat-user install-shutdown-compat-system: check-install-prerequisites

# Opt-in host integration: never privilege-escalate as part of normal HUD install.
check-shutdown-compat:
	/usr/bin/python3 -I scripts/check-shutdown-compat.py user
	/usr/bin/python3 -I scripts/check-shutdown-compat.py system

# Opt-in only: packages and installs the local VS Code companion, without
# restarting VS Code or changing global settings.
install-vscode:
	/usr/bin/python3 -I scripts/install-vscode-extension.py --all-profiles --force

check-vscode:
	node --check vscode-extension/extension.js
	@if [ -f tests/test_vscode_extension.mjs ]; then node tests/test_vscode_extension.mjs; fi
	@if [ -f tests/test_vscode_extension_protocol.mjs ]; then node tests/test_vscode_extension_protocol.mjs; fi

install-shutdown-compat-user:
	/usr/bin/python3 -I scripts/install-shutdown-compat.py --user

install-shutdown-compat-system:
	pkexec /usr/bin/python3 -I "$(CURDIR)/scripts/install-shutdown-compat.py" --system

install-file-manager:
	@/usr/bin/python3 -c 'import gi; gi.require_version("Nemo", "3.0"); from gi.repository import Nemo' || { echo "Install nemo-python and gir1.2-nemo-3.0 before installing the Nemo integration." >&2; exit 1; }
	@/usr/bin/python3 -c 'import glob, sys; sys.exit(not (glob.glob("/usr/lib/*/nemo/extensions-3.0/libnemo-python.so") or glob.glob("/usr/lib/nemo/extensions-3.0/libnemo-python.so")))' || { echo "Missing nemo-python extension loader." >&2; exit 1; }
	install -d "$(PREFIX)/share/nemo-python/extensions"
	install -m 0644 nemo-extension/wsctl_nemo_bridge.py "$(PREFIX)/share/nemo-python/extensions/wsctl_nemo_bridge.py"
	@echo "Installed Nemo state integration. Existing Nemo processes must be reopened to load it; no processes were stopped."

# Production installation is staged and applied before the next graphical login.
install:
	@test "$(PREFIX)" = "$(HOME)/.local" || { echo "Production make install requires PREFIX=$(HOME)/.local; use deployment.Locations for isolated roots or install-dev for legacy PREFIX behavior." >&2; exit 2; }
	XDG_CONFIG_HOME="$(CONFIG_HOME)" /usr/bin/python3 -I scripts/desktop-release deploy

# Explicit legacy development/host setup: mutable sources and service activation.
install-dev: install-file-manager
	@test -f "$(GNOME_WINCTL_DIR)/Makefile" || { echo "Missing sibling gnome-winctl project at $(GNOME_WINCTL_DIR)" >&2; exit 1; }
	@test -f "$(LOGIN_HUD_DIR)/Makefile" || { echo "Missing sibling login-hud project at $(LOGIN_HUD_DIR)" >&2; exit 1; }
	$(MAKE) -C "$(GNOME_WINCTL_DIR)" install PREFIX="$(PREFIX)"
	$(MAKE) -C "$(LOGIN_HUD_DIR)" install
	install -d "$(PREFIX)/bin" "$(PREFIX)/share/zsh/site-functions" "$(PREFIX)/share/gnome-shell/extensions" "$(PREFIX)/share/workspace-state" "$(PREFIX)/share/applications"
	ln -sfn "$(CURDIR)/bin/wsctl" "$(PREFIX)/bin/wsctl"
	ln -sfn "$(CURDIR)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-native-host"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-startup-launch"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-worker" "$(PREFIX)/bin/wsctl-startup-worker"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-barrier" "$(PREFIX)/bin/wsctl-startup-barrier"
	ln -sfn "$(CURDIR)/bin/wsctl-gnome-session" "$(PREFIX)/bin/wsctl-gnome-session"
	ln -sfn "$(CURDIR)/bin/wsctl-login-finalize" "$(PREFIX)/bin/wsctl-login-finalize"
	ln -sfn "$(CURDIR)/bin/wsctl-shutdown-finalize" "$(PREFIX)/bin/wsctl-shutdown-finalize"
	ln -sfn "$(CURDIR)/bin/cloud-drives-warmup" "$(PREFIX)/bin/cloud-drives-warmup"
	ln -sfn "$(CURDIR)/bin/wsctl-continuum-save" "$(PREFIX)/bin/wsctl-continuum-save"
	ln -sfn "$(CURDIR)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/wsctl-continuum-restore"
	ln -sfn "$(CURDIR)/bin/wsctl-codex-resume" "$(PREFIX)/bin/wsctl-codex-resume"
	ln -sfn "$(CURDIR)/bin/alacritty-wsctl" "$(PREFIX)/bin/alacritty"
	ln -sfn "$(CURDIR)/bin/alacritty-tmux-session" "$(PREFIX)/bin/alacritty-tmux-session"
	ln -sfn "$(CURDIR)/completions/_wsctl" "$(PREFIX)/share/zsh/site-functions/_wsctl"
	ln -sfn "$(CURDIR)/applications/Alacritty.desktop" "$(PREFIX)/share/applications/Alacritty.desktop"
	ln -sfn "$(CURDIR)/chrome-extension" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do \
		install -d "$$directory"; \
		sed 's|@HOST_PATH@|$(NATIVE_HOST_PATH)|g' native-messaging/$(NATIVE_HOST_MANIFEST).in > "$$directory/$(NATIVE_HOST_MANIFEST)"; \
		chmod 644 "$$directory/$(NATIVE_HOST_MANIFEST)"; \
	done
	@if command -v dconf >/dev/null 2>&1; then \
		dconf write /org/gnome/desktop/applications/terminal/exec "'$(PREFIX)/bin/alacritty-tmux-session'"; \
		dconf write /org/gnome/desktop/applications/terminal/exec-arg "'-e'"; \
	fi
	@if command -v gsettings >/dev/null 2>&1; then \
		remove_legacy=0; \
		if gnome-extensions info "$(GNOME_WINCTL_UUID)" >/dev/null 2>&1; then remove_legacy=1; fi; \
			enabled="$$(gsettings get org.gnome.shell enabled-extensions | \
				python3 -c 'import ast, sys; raw = sys.stdin.read().strip(); raw = raw[4:] if raw.startswith("@as ") else raw; values = ast.literal_eval(raw); values = [v for v in values if v not in {"$(LEGACY_GNOME_WINCTL_UUID)", "$(LEGACY_MONITOR_GUARD_UUID)", "$(LEGACY_LOGIN_HUD_UUID)"} and not (sys.argv[1] == "1" and v == "$(LEGACY_EXTENSION_UUID)")]; values.append("$(GNOME_WINCTL_UUID)") if "$(GNOME_WINCTL_UUID)" not in values else None; values.append("$(LOGIN_HUD_UUID)") if "$(LOGIN_HUD_UUID)" not in values else None; print(repr(values))' "$$remove_legacy")"; \
		gsettings set org.gnome.shell enabled-extensions "$$enabled"; \
	fi
	@echo "Installed wsctl and the Chrome native host; window placement uses gnome-winctl."
	@echo "The former standalone monitor guard is disabled but retained on disk for rollback."
	@echo "Load unpacked once: $(PREFIX)/share/workspace-state/chrome-extension (ID $(CHROME_EXTENSION_ID))."
	"$(PREFIX)/bin/wsctl" tmux configure
	@echo "Log out and back in once so GNOME Shell loads $(GNOME_WINCTL_UUID) and $(LOGIN_HUD_UUID)."
	$(MAKE) install-session PREFIX="$(PREFIX)" CONFIG_HOME="$(CONFIG_HOME)"

install-alerts:
	install -d "$(PREFIX)/bin" "$(SYSTEMD_USER_DIR)" "$(CONFIG_HOME)/workspace-state/alerts.d"
	ln -sfn "$(CURDIR)/bin/wsctl" "$(PREFIX)/bin/wsctl"
	install -m 0644 systemd/wsctl-alerts.service systemd/wsctl-alerts.timer "$(SYSTEMD_USER_DIR)/"
	@if [ ! -e "$(CONFIG_HOME)/workspace-state/alerts.d/owned-systems.toml" ]; then \
		install -m 0600 config/owned-systems.toml "$(CONFIG_HOME)/workspace-state/alerts.d/owned-systems.toml"; \
	fi
	/usr/bin/python3 -I scripts/desktop-release migrate-inventory --path "$(CONFIG_HOME)/workspace-state/alerts.d/owned-systems.toml"
	systemctl --user daemon-reload
	systemctl --user enable wsctl-alerts.service
	systemctl --user enable --now wsctl-alerts.timer
	@echo "Installed read-only owned-system scans; no monitored services were changed."

install-tmux-lifecycle:
	install -d "$(PREFIX)/bin" "$(SYSTEMD_USER_DIR)"
	ln -sfn "$(CURDIR)/bin/wsctl-tmux-shutdown" "$(PREFIX)/bin/wsctl-tmux-shutdown"
	install -m 0644 systemd/wsctl-tmux-shutdown.service "$(SYSTEMD_USER_DIR)/"
	systemctl --user daemon-reload
	systemctl --user enable --now wsctl-tmux-shutdown.service
	@echo "Armed Ubuntu-only tmux cleanup; existing sessions were not changed."

install-session: install-alerts install-tmux-lifecycle
	install -d "$(PREFIX)/bin" "$(SYSTEMD_USER_DIR)"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-worker" "$(PREFIX)/bin/wsctl-startup-worker"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-barrier" "$(PREFIX)/bin/wsctl-startup-barrier"
	ln -sfn "$(CURDIR)/bin/wsctl-gnome-session" "$(PREFIX)/bin/wsctl-gnome-session"
	ln -sfn "$(CURDIR)/bin/wsctl-login-finalize" "$(PREFIX)/bin/wsctl-login-finalize"
	ln -sfn "$(CURDIR)/bin/wsctl-shutdown-finalize" "$(PREFIX)/bin/wsctl-shutdown-finalize"
	ln -sfn "$(CURDIR)/bin/cloud-drives-warmup" "$(PREFIX)/bin/cloud-drives-warmup"
	install -m 0644 systemd/wsctl-gnome-session.service "$(SYSTEMD_USER_DIR)/wsctl-gnome-session.service"
	install -m 0644 systemd/wsctl-workspace-restored.target "$(SYSTEMD_USER_DIR)/wsctl-workspace-restored.target"
	install -m 0644 systemd/wsctl-login-finalize.service "$(SYSTEMD_USER_DIR)/wsctl-login-finalize.service"
	install -m 0644 systemd/wsctl-shutdown-finalize@.service "$(SYSTEMD_USER_DIR)/wsctl-shutdown-finalize@.service"
	install -m 0644 systemd/cloud-drives-warmup.service "$(SYSTEMD_USER_DIR)/cloud-drives-warmup.service"
	install -m 0644 systemd/cloud-drives-warmup.timer "$(SYSTEMD_USER_DIR)/cloud-drives-warmup.timer"
	@for unit in $(POST_WORKSPACE_UNITS); do \
		install -d "$(SYSTEMD_USER_DIR)/$$unit.d"; \
		install -m 0644 systemd/post-workspace-drive.conf "$(SYSTEMD_USER_DIR)/$$unit.d/50-workspace-restore.conf"; \
	done
	install -m 0644 systemd/rclone-gdrive-shutdown.conf "$(SYSTEMD_USER_DIR)/rclone-gdrive.service.d/60-shutdown-unmount.conf"
	install -m 0644 systemd/rclone-sagecat-serv-drive-shutdown.conf "$(SYSTEMD_USER_DIR)/rclone-sagecat-serv-drive.service.d/60-shutdown-unmount.conf"
	install -m 0644 systemd/protondrive-mount-pdrive-shutdown.conf "$(SYSTEMD_USER_DIR)/protondrive-mount-pdrive.service.d/60-shutdown-unmount.conf"
	systemctl --user daemon-reload
	-systemctl --user disable $(POST_WORKSPACE_UNITS)
	systemctl --user enable wsctl-login-finalize.service
	systemctl --user enable wsctl-gnome-session.service
	@if systemctl --user is-active --quiet graphical-session.target; then \
		systemctl --user restart wsctl-gnome-session.service; \
	else \
		echo "GNOME session is not active; wsctl-gnome-session will start at the next graphical login."; \
	fi

uninstall:
	rm -f "$(PREFIX)/share/nemo-python/extensions/wsctl_nemo_bridge.py"
	-systemctl --user disable --now wsctl-tmux-shutdown.service
	rm -f "$(SYSTEMD_USER_DIR)/wsctl-tmux-shutdown.service" "$(PREFIX)/bin/wsctl-tmux-shutdown"
	-systemctl --user disable --now wsctl-alerts.timer wsctl-alerts.service
	rm -f "$(SYSTEMD_USER_DIR)/wsctl-alerts.service" "$(SYSTEMD_USER_DIR)/wsctl-alerts.timer"
	@echo "Preserving owned-system inventory and incident history."
	-systemctl --user disable --now wsctl-login-finalize.service
	-systemctl --user disable --now wsctl-gnome-session.service
	rm -f "$(SYSTEMD_USER_DIR)/wsctl-gnome-session.service" "$(SYSTEMD_USER_DIR)/wsctl-login-finalize.service" "$(SYSTEMD_USER_DIR)/wsctl-shutdown-finalize@.service" "$(SYSTEMD_USER_DIR)/wsctl-workspace-restored.target" "$(SYSTEMD_USER_DIR)/cloud-drives-warmup.service" "$(SYSTEMD_USER_DIR)/cloud-drives-warmup.timer"
	@for unit in $(POST_WORKSPACE_UNITS); do \
		rm -f "$(SYSTEMD_USER_DIR)/$$unit.d/50-workspace-restore.conf" "$(SYSTEMD_USER_DIR)/$$unit.d/60-shutdown-unmount.conf"; \
		rmdir --ignore-fail-on-non-empty "$(SYSTEMD_USER_DIR)/$$unit.d" 2>/dev/null || true; \
	done
	systemctl --user daemon-reload
	-systemctl --user enable $(POST_WORKSPACE_UNITS)
	-"$(PREFIX)/bin/wsctl" tmux unconfigure
	rm -f "$(PREFIX)/bin/wsctl" "$(PREFIX)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-startup-worker" "$(PREFIX)/bin/wsctl-gnome-session" "$(PREFIX)/bin/wsctl-login-finalize" "$(PREFIX)/bin/wsctl-shutdown-finalize" "$(PREFIX)/bin/cloud-drives-warmup" "$(PREFIX)/bin/wsctl-continuum-save" "$(PREFIX)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/wsctl-codex-resume" "$(PREFIX)/bin/alacritty" "$(PREFIX)/bin/alacritty-tmux-session" "$(PREFIX)/share/zsh/site-functions/_wsctl" "$(PREFIX)/share/applications/Alacritty.desktop" "$(PREFIX)/share/gnome-shell/extensions/$(LEGACY_EXTENSION_UUID)" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do rm -f "$$directory/$(NATIVE_HOST_MANIFEST)"; done

check:
	python3 scripts/run-tests.py
	node --check chrome-extension/service-worker.js
	node --check chrome-extension/identify.js
	node tests/test_chrome_extension.js
	$(MAKE) check-vscode

test: check
	$(MAKE) -C "$(GNOME_WINCTL_DIR)" test
	$(MAKE) -C "$(LOGIN_HUD_DIR)" check
