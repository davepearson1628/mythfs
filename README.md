# MythFS for MythTV v36

MythFS is a read-only FUSE filesystem for MythTV recordings. It presents the
recordings stored by MythTV with human-readable names and a folder layout that
works well with Jellyfin, Emby and Plex, without renaming or modifying the
actual MythTV recording files.

This implementation targets MythTV v36, Python 3 and FUSE3/pyfuse3.

## Features

- Read-only: MythTV remains the owner of the real recording files.
- MythTV Recording Groups become top-level folders.
- `Deleted` and `LiveTV` are excluded by default.
- TV recordings use media-server-friendly naming:
  `Series/Season 01/Series - S01E01 - Episode.ts`.
- The `Movies` Recording Group uses:
  `Movie Title (YEAR)/Movie Title (YEAR).ts`.
- Movie year comes first from `recordedprogram.airdate`.
- If that year is absent and the recording has a `tmdb3.py_<id>` inetref,
  MythFS uses MythTV's own `tmdb3.py` metadata grabber in the background.
- TMDB-derived years are cached persistently.
- Automatic refresh adds/removes recordings without remounting.
- Multiple MythTV `Default` Storage Group directories are discovered
  automatically.
- Optional local recording paths can be supplied when another backend's
  storage is mounted at a different path.
- FUSE `allow_other` support allows the virtual filesystem to be exported
  through Samba and NFS.

## Example layout

```text
/media/mythfs/
├── Default/
├── Documentaries/
├── Glastonbury/
├── Movies/
│   ├── A Quiet Place (2018)/
│   │   └── A Quiet Place (2018).ts
│   └── Blade II (2002)/
│       └── Blade II (2002).ts
├── Music/
└── The Fast Show/
    └── The Fast Show/
        └── Season 01/
            └── The Fast Show - S01E01 - Episode Title.ts
```

## Requirements

On Ubuntu:

```bash
sudo apt install fuse3 python3-pyfuse3
```

MythTV's Python bindings must also be installed and usable:

```bash
python3 - <<'PY'
import MythTV
print(MythTV.__file__)
print(getattr(MythTV, "__version__", "unknown"))
PY
```

MythFS has been developed and tested with MythTV v36.

## Installation

Install the program:

```bash
sudo install -m 755 mythfs.py /usr/local/bin/mythfs.py
```

Create the production mount and cache directories:

```bash
sudo mkdir -p /media/mythfs /var/cache/mythfs
```

Choose the account that will run MythFS and make those directories writable by
that account. For example, if MythFS runs as the `mythtv` user:

```bash
sudo chown mythtv:mythtv /media/mythfs /var/cache/mythfs
```

Install the example configuration:

```bash
sudo install -m 644 mythfs.conf.example /etc/mythfs.conf
```

Edit `/etc/mythfs.conf` as required.

If `allow_other = yes`, ensure `/etc/fuse.conf` contains:

```text
user_allow_other
```

For example:

```bash
grep -q '^user_allow_other' /etc/fuse.conf \
  || echo user_allow_other | sudo tee -a /etc/fuse.conf
```

## Configuration

The default configuration is:

```ini
[mythfs]
mountpoint = /media/mythfs
discover_storage_groups = yes

refresh = 300

exclude_groups = Deleted, LiveTV
movie_groups = Movies
include_all_groups = no

tmdb_grabber = /usr/share/mythtv/metadata/Movie/tmdb3.py
cache_file = /var/cache/mythfs/movie-years.json

allow_other = yes
debug = no
debug_fuse = no
```

### Multiple recording storage locations

MythFS queries MythTV's `storagegroup` table for every directory in the
`Default` Storage Group.

For each recording it searches in this order:

1. Explicit `storage_dirs` configured in `/etc/mythfs.conf`.
2. `Default` Storage Group directories for the backend named by
   `recorded.hostname`.
3. All other discovered `Default` Storage Group directories.

A candidate path is only accepted when the actual recording file exists on the
machine running MythFS. MythFS therefore does not assume that identical paths
on two MythTV backends refer to the same storage.

If another backend's recordings are mounted locally under a different path,
add the local paths:

```ini
storage_dirs = /mnt/backend1-recordings, /mnt/backend2-recordings
```

The older singular form remains accepted:

```ini
storage_dir = /media/myth/tv
```

Storage Group discovery can be disabled if required:

```ini
discover_storage_groups = no
storage_dirs = /mnt/recordings-a, /mnt/recordings-b
```

Remote-only recordings that are not locally mounted are not streamed through
the MythTV backend protocol; they are simply omitted until their backing file
becomes locally accessible.

## Running manually

```bash
/usr/local/bin/mythfs.py --config /etc/mythfs.conf
```

Useful overrides include:

```bash
/usr/local/bin/mythfs.py --help
/usr/local/bin/mythfs.py --refresh 300
/usr/local/bin/mythfs.py --storage-dir /mnt/extra-recordings
/usr/local/bin/mythfs.py --debug
```

## systemd

Edit `mythfs.service` and set `User=` and `Group=` to the account that should
run MythFS.

Install and enable it:

```bash
sudo install -m 644 mythfs.service /etc/systemd/system/mythfs.service
sudo systemctl daemon-reload
sudo systemctl enable --now mythfs.service
```

Check:

```bash
systemctl status mythfs.service --no-pager
mount | grep ' /media/mythfs '
```

## Samba

With `allow_other = yes`, Samba can export the FUSE filesystem directly.

Example:

```ini
[MythFS]
    comment = MythTV Recordings
    path = /media/mythfs
    browseable = yes
    read only = yes
    guest ok = no
    follow symlinks = yes
    wide links = no
```

After adding it to `/etc/samba/smb.conf`:

```bash
sudo testparm
sudo systemctl reload smbd
```

## NFS

Example `/etc/exports` entry:

```text
/media/mythfs 192.168.1.0/24(ro,sync,no_subtree_check,fsid=30)
```

Apply it with:

```bash
sudo exportfs -ra
```

### Boot ordering with NFS

Do not make `nfs-server.service` depend directly on `mythfs.service` when the
underlying MythTV recording storage is itself involved in NFS/systemd mount
ordering; that can create an ordering cycle.

Instead, use the supplied `mythfs-nfs-export.service.example` as a starting
point. It explicitly removes any export associated with an old FUSE instance,
exports the newly mounted MythFS filesystem, and unexports MythFS before the
FUSE filesystem is stopped.

This is important because restarting MythFS creates a new FUSE filesystem
instance. A simple `exportfs -r` may leave NFS exporting the empty directory
underneath `/media/mythfs` rather than the new FUSE mount.

Existing NFS clients can still receive stale file handles after a MythFS
restart because their file handles belong to the previous FUSE instance. In
that case, unmount and remount the MythFS NFS share on the client. Normal
300-second MythFS refreshes do not recreate the FUSE filesystem and therefore
do not cause this issue.

The example contains a LAN-specific client address. Edit it before installing.

## Movie metadata cache

MythFS stores TMDB-derived years in:

```text
/var/cache/mythfs/movie-years.json
```

The cache is independent of MythTV's own `~/.mythtv/cache/pytmdb3.cache`.

If MythTV's TMDB cache becomes corrupt, stop MythFS before removing that
MythTV-owned cache so no metadata grabber process is using it.

## Refresh behaviour

The default refresh interval is 300 seconds (5 minutes). New recordings appear
and deleted recordings disappear automatically.

Normal MythFS refreshes update the existing FUSE filesystem in place and do
not require SMB or NFS clients to reconnect.

Set:

```ini
refresh = 0
```

to disable automatic refresh.

Movie-year lookups are performed in the background so a slow TMDB request does
not block mounting or periodic refreshes.

## Read-only behaviour

MythFS rejects write, create, delete, rename and truncate operations with
`EROFS`. Samba and NFS exports should also be configured read-only.

## Troubleshooting

### `allow_other` fails

Ensure `/etc/fuse.conf` contains:

```text
user_allow_other
```

### Recording is missing from MythFS

Check:

- it is present in MythTV's `recorded` table;
- its Recording Group is not excluded;
- at least one candidate Storage Group/configured directory contains the
  recording basename locally;
- the MythFS service account can read the real file.

Enable `debug = yes` to log inaccessible recording basenames and backend
hostnames.

### FUSE mount is busy during shutdown

Stop/unexport services using `/media/mythfs` first. The supplied NFS helper
service removes the NFS export before MythFS stops and recreates it against the
new FUSE instance after MythFS starts. The systemd unit also uses a lazy FUSE
unmount as a fallback.

If an NFS client reports `Stale file handle` after MythFS has been restarted,
unmount and remount the share on that client.

## Uninstall

```bash
sudo systemctl disable --now mythfs.service
sudo fusermount3 -uz /media/mythfs
sudo rm -f /etc/systemd/system/mythfs.service
sudo rm -f /usr/local/bin/mythfs.py
sudo systemctl daemon-reload
```

Remove `/etc/mythfs.conf`, the cache directory and SMB/NFS configuration only
if they are no longer required.

## License

MIT. See `LICENSE`.
