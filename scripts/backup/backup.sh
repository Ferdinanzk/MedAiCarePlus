#!/bin/sh
set -eu
umask 077
export TZ=Asia/Taipei

mkdir -p /backups/nightly /backups/weekly /ledger
touch /ledger/deletion_ledger.jsonl

backup_once() (
    set -eu
    stamp=$(date +%F)
    target="/backups/nightly/$stamp"
    stage=$(mktemp -d /backups/nightly/.pending.XXXXXX)
    weekly_tmp="/backups/weekly/.$stamp.tar.gpg.tmp"
    trap 'rm -rf "$stage"; rm -f "$weekly_tmp"' EXIT
    if [ ! -d "$target" ]; then
        pg_dump -Fc --file="$stage/database.dump"
        tar -C /gallery -cf "$stage/gallery.tar" .
        mv "$stage" "$target"
    fi
    if [ "$(date +%u)" = 7 ] && [ ! -f "/backups/weekly/$stamp.tar.gpg" ]; then
        if [ -z "${BACKUP_PASSPHRASE:-}" ]; then
            echo "Weekly backup failed: BACKUP_PASSPHRASE is empty" >&2
            exit 1
        fi
        # Materialise the bundle so tar failures cannot be hidden by a pipeline.
        mkdir -p "$stage"
        tar -C "$target" -cf "$stage/weekly.tar" database.dump gallery.tar
        gpg --batch --yes --pinentry-mode loopback --passphrase-fd 3 \
            --symmetric --cipher-algo AES256 --output "$weekly_tmp" "$stage/weekly.tar" 3<<EOF
$BACKUP_PASSPHRASE
EOF
        mv "$weekly_tmp" "/backups/weekly/$stamp.tar.gpg"
    fi
    echo "Backup completed: $stamp"
)

prune_backups() {
    # ISO dates sort chronologically; keep at most 14 nightly / 4 weekly sets.
    find /backups/nightly -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' |
        sort -r | tail -n +15 | while IFS= read -r path; do rm -rf "$path"; done
    find /backups/weekly -maxdepth 1 -type f -name '20??-??-??.tar.gpg' |
        sort -r | tail -n +5 | while IFS= read -r path; do rm -f "$path"; done
    # Expire old copies even if subsequent backups have failed.
    find /backups/nightly -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +13 -exec rm -rf {} \;
    find /backups/weekly -maxdepth 1 -type f -name '20??-??-??.tar.gpg' -mtime +27 -delete
    find /backups/nightly -mindepth 1 -maxdepth 1 -type d -name '.pending.*' -mtime +0 -exec rm -rf {} \;
    find /backups/weekly -maxdepth 1 -type f -name '.*.tar.gpg.tmp' -mtime +0 -delete
}

last_attempt=""
while :; do
    prune_backups
    today=$(date +%F)
    if [ "$(date +%H:%M)" = "02:30" ] && [ "$last_attempt" != "$today" ]; then
        last_attempt=$today
        # Run outside a conditional so set -e remains effective in the subshell.
        set +e
        backup_once
        status=$?
        set -e
        if [ "$status" -ne 0 ]; then
            echo "Backup failed (exit $status); previous completed copies retained" >&2
        fi
        prune_backups
    fi
    sleep 60
done
