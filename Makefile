PREFIX ?= $(HOME)/.local
EXTENSION_UUID := workspace-state@sagecat.local
CHROME_EXTENSION_ID := gnccboicpdhhhpdcogeleiegokieocmn
CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)
NATIVE_HOST_MANIFEST := org.sagecat.workspace_state.json
NATIVE_HOST_DIRS := $(CONFIG_HOME)/google-chrome/NativeMessagingHosts $(CONFIG_HOME)/google-chrome-beta/NativeMessagingHosts $(CONFIG_HOME)/chromium/NativeMessagingHosts
NATIVE_HOST_PATH := $(abspath $(PREFIX)/bin/wsctl-native-host)

.PHONY: install uninstall test

install:
	install -d "$(PREFIX)/bin" "$(PREFIX)/share/zsh/site-functions" "$(PREFIX)/share/gnome-shell/extensions" "$(PREFIX)/share/workspace-state"
	ln -sfn "$(CURDIR)/bin/wsctl" "$(PREFIX)/bin/wsctl"
	ln -sfn "$(CURDIR)/bin/wsctl-native-host" "$(PREFIX)/bin/wsctl-native-host"
	ln -sfn "$(CURDIR)/completions/_wsctl" "$(PREFIX)/share/zsh/site-functions/_wsctl"
	ln -sfn "$(CURDIR)/gnome-extension/$(EXTENSION_UUID)" "$(PREFIX)/share/gnome-shell/extensions/$(EXTENSION_UUID)"
	ln -sfn "$(CURDIR)/chrome-extension" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do \
		install -d "$$directory"; \
		sed 's|@HOST_PATH@|$(NATIVE_HOST_PATH)|g' native-messaging/$(NATIVE_HOST_MANIFEST).in > "$$directory/$(NATIVE_HOST_MANIFEST)"; \
		chmod 644 "$$directory/$(NATIVE_HOST_MANIFEST)"; \
	done
	@echo "Installed wsctl, the GNOME companion, and the Chrome native host."
	@echo "Load unpacked: $(PREFIX)/share/workspace-state/chrome-extension (ID $(CHROME_EXTENSION_ID))"
	@echo "Log out and back in once after enabling a newly installed GNOME extension."

uninstall:
	rm -f "$(PREFIX)/bin/wsctl" "$(PREFIX)/bin/wsctl-native-host" "$(PREFIX)/share/zsh/site-functions/_wsctl" "$(PREFIX)/share/gnome-shell/extensions/$(EXTENSION_UUID)" "$(PREFIX)/share/workspace-state/chrome-extension"
	@for directory in $(NATIVE_HOST_DIRS); do rm -f "$$directory/$(NATIVE_HOST_MANIFEST)"; done

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v
