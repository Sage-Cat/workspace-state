PREFIX ?= $(HOME)/.local
EXTENSION_UUID := workspace-state@sagecat.local
CHROME_EXTENSION_ID := gnccboicpdhhhpdcogeleiegokieocmn
CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)
NATIVE_HOST_MANIFEST := org.sagecat.workspace_state.json
NATIVE_HOST_DIRS := $(CONFIG_HOME)/google-chrome/NativeMessagingHosts
NATIVE_HOST_PATH := $(abspath $(PREFIX)/bin/wsctl-native-host)

.PHONY: install uninstall test

install:
	install -d "$(PREFIX)/bin" "$(PREFIX)/share/zsh/site-functions" "$(PREFIX)/share/gnome-shell/extensions" "$(PREFIX)/share/workspace-state" "$(PREFIX)/share/applications"
	ln -sfn "$(CURDIR)/bin/wsctl" "$(PREFIX)/bin/wsctl"
	ln -sfn "$(CURDIR)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-native-host"
	ln -sfn "$(CURDIR)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-startup-launch"
	ln -sfn "$(CURDIR)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/wsctl-continuum-restore"
	ln -sfn "$(CURDIR)/bin/alacritty-wsctl" "$(PREFIX)/bin/alacritty"
	ln -sfn "$(CURDIR)/bin/alacritty-tmux-session" "$(PREFIX)/bin/alacritty-tmux-session"
	ln -sfn "$(CURDIR)/completions/_wsctl" "$(PREFIX)/share/zsh/site-functions/_wsctl"
	ln -sfn "$(CURDIR)/applications/Alacritty.desktop" "$(PREFIX)/share/applications/Alacritty.desktop"
	ln -sfn "$(CURDIR)/gnome-extension/$(EXTENSION_UUID)" "$(PREFIX)/share/gnome-shell/extensions/$(EXTENSION_UUID)"
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
	@echo "Installed wsctl, the GNOME companion, and the Chrome native host."
	@echo "Load unpacked once: $(PREFIX)/share/workspace-state/chrome-extension (ID $(CHROME_EXTENSION_ID))."
	@echo "Log out and back in once after enabling a newly installed GNOME extension."

uninstall:
	rm -f "$(PREFIX)/bin/wsctl" "$(PREFIX)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-startup-launch" "$(PREFIX)/bin/wsctl-continuum-restore" "$(PREFIX)/bin/alacritty" "$(PREFIX)/bin/alacritty-tmux-session" "$(PREFIX)/share/zsh/site-functions/_wsctl" "$(PREFIX)/share/applications/Alacritty.desktop" "$(PREFIX)/share/gnome-shell/extensions/$(EXTENSION_UUID)" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do rm -f "$$directory/$(NATIVE_HOST_MANIFEST)"; done

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v
