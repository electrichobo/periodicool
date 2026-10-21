#!/bin/sh
# Prepare /config, then run PeriodiCool as PUID:PGID. Never runs the app as root.
# Only /config changes owner: /library and /downloads hold other content and
# are left exactly as they are.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"

case "$PUID$PGID" in
    *[!0-9]*) echo "PUID and PGID must be numbers (got $PUID:$PGID)" >&2; exit 1 ;;
esac
umask "$UMASK"

if [ "$(id -u)" = "0" ]; then
    if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
        echo "Refusing to run as root: set PUID and PGID to a normal user (Unraid: 99 and 100)." >&2
        exit 1
    fi
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    echo "Starting PeriodiCool as $PUID:$PGID, umask $UMASK"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

# Already started as a normal user (for example `user:` in compose).
echo "Starting PeriodiCool as $(id -u):$(id -g), umask $UMASK"
exec "$@"
