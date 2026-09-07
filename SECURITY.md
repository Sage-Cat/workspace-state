# Security policy

## Reporting

Use GitHub private vulnerability reporting for flaws that could run an
unintended profile command, overwrite another shutdown transaction, bypass
rollback, authorize stale state, or block normal GNOME shutdown. Include the
GNOME version and redacted user-journal excerpts.

## Shutdown profile trust

Profiles execute as the logged-in user and are therefore trusted local code.
Configuration directories and files must be owned by that user or root and not
be writable by group or others. The command adapter passes an exact argv array
directly to the operating system and never invokes an implicit shell.

Runtime status, authorization markers, and snapshotted rollback instructions
remain in the current user's private runtime directory. The project makes no
network requests and never reads cloud-drive credentials.

The graphical-session coordinator holds a logind block inhibitor until a
shutdown checkpoint has durable, operation-bound authorization. Normal local
power commands therefore cannot silently bypass the HUD. Root or a privileged
forced shutdown can override this operating-system boundary; that override is
outside the transaction's safety guarantee.
