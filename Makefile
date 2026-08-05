PREFIX ?= $(HOME)/.local
LEGACY_EXTENSION_UUID := workspace-state@sagecat.local
GNOME_WINCTL_UUID := gnome-winctl@sagecat.local
GNOME_WINCTL_DIR ?= $(abspath $(CURDIR)/../gnome-winctl)
CHROME_EXTENSION_ID := gnccboicpdhhhpdcogeleiegokieocmn
CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)
NATIVE_HOST_MANIFEST := org.sagecat.workspace_state.json
NATIVE_HOST_DIRS := $(CONFIG_HOME)/google-chrome/NativeMessagingHosts
NATIVE_HOST_PATH := $(abspath $(PREFIX)/bin/wsctl-native-host)

.PHONY: install uninstall test

install:
	@test -f "$(GNOME_WINCTL_DIR)/Makefile" || { echo "Missing sibling gnome-winctl project at $(GNOME_WINCTL_DIR)" >&2; exit 1; }
	$(MAKE) -C "$(GNOME_WINCTL_DIR)" install PREFIX="$(PREFIX)"
	install -d "$(PREFIX)/bin" "$(PREFIX)/share/zsh/site-functions" "$(PREFIX)/share/gnome-shell/extensions" "$(PREFIX)/share/workspace-state" "$(PREFIX)/share/applications"
	ln -sfn "$(CURDIR)/bin/wsctl" "$(PREFIX)/bin/wsctl"
	ln -sfn "$(CURDIR)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-native-host"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-startup-launch"
	ln -sfn "$(CURDIR)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/wsctl-continuum-restore"
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
			python3 -c 'import ast, sys; raw = sys.stdin.read().strip(); raw = raw[4:] if raw.startswith("@as ") else raw; values = ast.literal_eval(raw); values = [v for v in values if not (sys.argv[1] == "1" and v == "$(LEGACY_EXTENSION_UUID)")]; values.append("$(GNOME_WINCTL_UUID)") if "$(GNOME_WINCTL_UUID)" not in values else None; print(repr(values))' "$$remove_legacy")"; \
		gsettings set org.gnome.shell enabled-extensions "$$enabled"; \
	fi
	@echo "Installed wsctl and the Chrome native host; window placement uses gnome-winctl."
	@echo "Load unpacked once: $(PREFIX)/share/workspace-state/chrome-extension (ID $(CHROME_EXTENSION_ID))."
	@echo "Log out and back in once so GNOME Shell loads $(GNOME_WINCTL_UUID)."

uninstall:
	rm -f "$(PREFIX)/bin/wsctl" "$(PREFIX)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/alacritty" "$(PREFIX)/bin/alacritty-tmux-session" "$(PREFIX)/share/zsh/site-functions/_wsctl" "$(PREFIX)/share/applications/Alacritty.desktop" "$(PREFIX)/share/gnome-shell/extensions/$(LEGACY_EXTENSION_UUID)" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do rm -f "$$directory/$(NATIVE_HOST_MANIFEST)"; done

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v
	node --check chrome-extension/service-worker.js
	$(MAKE) -C "$(GNOME_WINCTL_DIR)" test
