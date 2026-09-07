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

.PHONY: install install-session uninstall check test

install:
	@test -f "$(GNOME_WINCTL_DIR)/Makefile" || { echo "Missing sibling gnome-winctl project at $(GNOME_WINCTL_DIR)" >&2; exit 1; }
	@test -f "$(LOGIN_HUD_DIR)/Makefile" || { echo "Missing sibling login-hud project at $(LOGIN_HUD_DIR)" >&2; exit 1; }
	$(MAKE) -C "$(GNOME_WINCTL_DIR)" install PREFIX="$(PREFIX)"
	$(MAKE) -C "$(LOGIN_HUD_DIR)" install
	install -d "$(PREFIX)/bin" "$(PREFIX)/share/zsh/site-functions" "$(PREFIX)/share/gnome-shell/extensions" "$(PREFIX)/share/workspace-state" "$(PREFIX)/share/applications"
	ln -sfn "$(CURDIR)/bin/wsctl" "$(PREFIX)/bin/wsctl"
	ln -sfn "$(CURDIR)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-native-host"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-startup-launch"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-worker" "$(PREFIX)/bin/wsctl-startup-worker"
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

install-session:
	install -d "$(PREFIX)/bin" "$(SYSTEMD_USER_DIR)"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-worker" "$(PREFIX)/bin/wsctl-startup-worker"
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
	-systemctl --user disable --now wsctl-login-finalize.service
	-systemctl --user disable --now wsctl-gnome-session.service
	rm -f "$(SYSTEMD_USER_DIR)/wsctl-gnome-session.service" "$(SYSTEMD_USER_DIR)/wsctl-login-finalize.service" "$(SYSTEMD_USER_DIR)/wsctl-shutdown-finalize@.service" "$(SYSTEMD_USER_DIR)/wsctl-workspace-restored.target" "$(SYSTEMD_USER_DIR)/cloud-drives-warmup.service" "$(SYSTEMD_USER_DIR)/cloud-drives-warmup.timer"
	@for unit in $(POST_WORKSPACE_UNITS); do \
		rm -f "$(SYSTEMD_USER_DIR)/$$unit.d/50-workspace-restore.conf" "$(SYSTEMD_USER_DIR)/$$unit.d/60-shutdown-unmount.conf"; \
		rmdir --ignore-fail-on-non-empty "$(SYSTEMD_USER_DIR)/$$unit.d" 2>/dev/null || true; \
	done
	systemctl --user daemon-reload
	-systemctl --user enable $(POST_WORKSPACE_UNITS)
	rm -f "$(PREFIX)/bin/wsctl" "$(PREFIX)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-startup-worker" "$(PREFIX)/bin/wsctl-gnome-session" "$(PREFIX)/bin/wsctl-login-finalize" "$(PREFIX)/bin/wsctl-shutdown-finalize" "$(PREFIX)/bin/cloud-drives-warmup" "$(PREFIX)/bin/wsctl-continuum-save" "$(PREFIX)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/wsctl-codex-resume" "$(PREFIX)/bin/alacritty" "$(PREFIX)/bin/alacritty-tmux-session" "$(PREFIX)/share/zsh/site-functions/_wsctl" "$(PREFIX)/share/applications/Alacritty.desktop" "$(PREFIX)/share/gnome-shell/extensions/$(LEGACY_EXTENSION_UUID)" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do rm -f "$$directory/$(NATIVE_HOST_MANIFEST)"; done

check:
	PYTHONPATH=src python3 -m unittest discover -s tests -v
	node --check chrome-extension/service-worker.js
	node --check chrome-extension/identify.js
	node tests/test_chrome_extension.js

test: check
	$(MAKE) -C "$(GNOME_WINCTL_DIR)" test
	$(MAKE) -C "$(LOGIN_HUD_DIR)" check
