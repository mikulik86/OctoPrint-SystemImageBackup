# OctoPrint System Image Backup

> This plugin was created by [Claude](https://claude.ai), an AI assistant made by Anthropic.

Makes a **full, bootable image of your OctoPi system** (SD card or USB/NVMe boot drive) from
inside OctoPrint, or from a Custom Action in OctoDash. The image can be written to a new card
with Raspberry Pi Imager or balenaEtcher and boots straight into your current setup:
OctoPrint, plugins, OctoDash, network settings, everything.

It uses **image-utils** by RonR (Raspberry Pi forums). `image-backup` copies the running
system with `rsync` into an image file sized to the used space instead of the whole card
(see [how big the image gets](#good-to-know)), and can update that image incrementally later.

OctoPrint's built-in *Backup & Restore* only saves OctoPrint's own settings, plugins and
uploads. This plugin saves the whole operating system.

## How it works

A plugin can't stop OctoPrint and keep running, because it lives inside OctoPrint. So the
work is done by a small system service:

1. You click **Start backup** (or use the OctoDash action).
2. The plugin checks the settings and writes them to a job file. It then asks systemd to
   start `octopi-image-backup.service`.
3. The service checks everything again as root. For a network share it connects the share
   first, so an unreachable NAS or a wrong password is reported *before* anything is stopped.
   Then it stops OctoPrint and any services you listed. If enabled, it switches the Pi's
   screen to a text console with live progress.
4. It runs `image-backup`, writes the result to a status file, disconnects the share and
   starts everything again. Restarting is guaranteed even if the backup fails or crashes
   (`ExecStopPost`).
5. When OctoPrint is back, the tab shows the result and the log.

While the backup runs, OctoPrint's web interface is offline. Printing is blocked: the
backup refuses to start while a print is running or paused.

## Requirements

- OctoPi or Raspberry Pi OS (Bullseye, Bookworm or newer) with OctoPrint 1.8.3 or newer,
  running as a systemd service (the default on OctoPi).
- A **destination**, one of:
  - a **network share**, SMB (Windows, most NAS) or NFS. You enter it in the plugin; it is
    connected only for the backup, so there's nothing to set up on the Pi.
  - a **USB drive** (or a share you mount yourself) under `/mnt` or `/media`. It can't be a
    folder on the SD card itself. image-backup only accepts these paths and leaves them out
    of the backup.
- Space: the first backup needs about 1.2 × the used space of your system, plus 0.8 GB, plus
  the *Room for updates* you set.
- Image files are usually bigger than 4 GB, so **don't use a FAT32 USB drive**. Use ext4 or exFAT.

## Installation

1. In OctoPrint, open **Settings → Plugin Manager → Get More** and paste this into
   **… from URL**, then install and restart OctoPrint:

   ```
   https://github.com/mikulik86/OctoPrint-SystemImageBackup/archive/refs/heads/main.zip
   ```

   New releases show up in OctoPrint's **Software Update** like any other plugin.
2. Open the new **Image Backup** tab. It shows a *One-time setup needed* box.
3. Click **Install helper**. On a standard OctoPi this works directly from the browser, because
   the `pi` user can use sudo without a password there. If it can't, the tab shows the exact
   command to run over SSH instead, for example:

   ```
   sudo bash /home/pi/oprint/lib/python3.x/site-packages/octoprint_systemimagebackup/helper/install.sh \
        --user pi --job-file /home/pi/.octoprint/data/systemimagebackup/job.conf --with-image-utils
   ```

The installer puts these in place:

| What | Where |
|---|---|
| Helper script (the backup job) | `/usr/local/sbin/octopi-image-backup` |
| systemd service | `/etc/systemd/system/octopi-image-backup.service` |
| Location of the plugin's job file | `/etc/default/octopi-image-backup` |
| sudoers rules: OctoPrint may *only* start that service, test the destination and save the SMB password | `/etc/sudoers.d/octopi-image-backup` |
| Saved SMB password (readable by root only) | `/etc/octopi-image-backup/` |
| Status and log of the last backup | `/var/lib/octopi-image-backup/` |
| Progress view and a copy of image-backup with the loop wait, while a backup runs (in RAM, gone after a reboot) | `/run/octopi-image-backup/` |
| `rsync`, `gdisk`, `cifs-utils`, `nfs-common` (from apt, if missing) | system packages |
| image-utils (with `--with-image-utils`) | `/usr/local/sbin/image-*` |

image-utils is downloaded from the [seamusdemora/RonR-RPi-image-utils](https://github.com/seamusdemora/RonR-RPi-image-utils)
GitHub mirror, pinned to a fixed commit and checked against SHA-256 hashes. If you already
have image-utils installed, it is used as is.

After a plugin update that changes the helper, the tab asks you to install it again
(**Update helper**).

## Using it

**Settings** (in the tab):

- **Destination**:
  - *SMB share (Windows, NAS)*: the share as `//server/share` (`\\server\share` is accepted too),
    an optional **folder on the share** (created if missing), and a **username and password**.
    Leave the username empty for guest access. The password is stored on the Pi in a file only
    root can read, never in OctoPrint's settings.
  - *NFS share*: the export as `server:/path`. NFS has no password; the server must
    let this Pi's root user write, e.g. with `no_root_squash` or by mapping root to a user with
    write access.
  - **Mount options** (optional) are only needed if connecting fails with the defaults, for
    example `vers=3.0` or `vers=2.0` for older SMB servers, `domain=WORKGROUP`, or
    `nfsvers=4.1`. NFS shares are mounted `soft`, so a vanished server makes the backup fail
    instead of keeping OctoPrint offline; add `hard` to change that.
  - **Test connection** connects the share as root, checks that it's writable and shows the
    free space and whether an image is already there. Errors like a wrong password, an
    unknown share or an unreachable server are shown in plain words.
  - *USB drive or mounted folder*: a path under `/mnt` or `/media`, e.g. `/mnt/backup`. The tab
    shows whether it's mounted and how much space is free. The drive has to be mounted
    already; see [Setting up a USB drive](#setting-up-a-usb-drive).
- **Backup type**:
  - *Keep one image and update it*: the first run creates `octopi-backup.img`. Later runs only
    copy what changed, which is fast. **Room for updates** is free space reserved inside the
    image. If your system grows by more than that, tick *Recreate the image from scratch
    this time*. The old image stays in place until the new one is complete.
  - *Create a new image every time*: `octopi-backup-YYYYMMDD-HHMMSS.img`. Older images beyond
    **Images to keep** are deleted after a successful backup.
- **Also stop**: other services to stop during the backup, e.g. `webcamd` or a camera-streamer
  service. Running camera services are suggested. Network, SSH and system services can't be
  stopped.
- **Screen**: shows the progress on the screen attached to the Pi (see below).

**Start backup now** asks for confirmation, saves unsaved settings and starts the backup.
OctoPrint goes offline a few seconds later. The page reconnects by itself and shows the result.

### Setting up a USB drive

The plugin connects SMB and NFS shares itself, but a USB drive has to be mounted already.
OctoPi doesn't mount USB drives automatically, so add a line for it to `/etc/fstab`, the
list of drives the Pi mounts at every boot:

1. Plug in the drive and find its UUID and filesystem type with `lsblk -f`.
2. Create the folder it will appear in: `sudo mkdir -p /mnt/backup`
3. Open the list with `sudo nano /etc/fstab` and add a line at the end:

   ```
   UUID=1234-ABCD  /mnt/backup  ext4  defaults,nofail  0 2
   ```

   - `UUID=1234-ABCD`: the drive, by the UUID from step 1. It stays the same even if the
     device name (`/dev/sda1`) changes.
   - `/mnt/backup`: the folder from step 2.
   - `ext4`: the filesystem. Use `exfat` for an exFAT drive.
   - `defaults,nofail`: standard options; `nofail` lets the Pi boot normally without the drive.
   - `0 2`: the first number is unused (always 0); the 2 checks the drive for errors at boot.
     Use `0 0` for exFAT.
4. Mount it with `sudo mount -a` (this also reports mistakes in the line) and check it with
   `df -h /mnt/backup`.

Then enter `/mnt/backup` as the folder in the plugin.

### Progress on the Pi's screen (DSI/HDMI) or over SSH

With *Show progress on the screen* on, the service switches the display to an unused text
console (default 8) and shows each step with a progress bar while copying, the elapsed time
and the latest log lines, as many as fit on the screen. It updates every second. When
OctoPrint answers again, it switches back to the previous screen, e.g. OctoDash, which keeps
running in the background the whole time. After a failure, the error stays on screen for 30 s
first.

Without a screen, leave the option off.

To watch a backup over SSH, with or without a screen, run this on the Pi. The tab shows it
under the Start button, with a Copy button:

```
/usr/local/sbin/octopi-image-backup watch
```

It shows the same view as the Pi's screen; in a smaller window it leaves out older log lines
instead of scrolling. If no backup is running yet, it waits for one, so
you can start it before clicking **Start backup now**. It ends by itself with the result.
Ctrl+C stops watching, not the backup. It doesn't need sudo. The full log of a running backup
is also in the system journal: `journalctl -u octopi-image-backup -f`.

### OctoDash Custom Action

The tab shows a ready-made Custom Action command. In OctoDash, go to Settings → Custom
Actions, add an action and paste it as the command:

```
[!WEB]http://localhost:5000/plugin/systemimagebackup/octodash?token=…
```

Tapping it opens a page inside OctoDash with the last backup, the destination status and a
**Start backup** button with confirmation. It works even when the printer is off. The token
in the link only allows viewing the status and starting a backup. **New link** in the tab
invalidates the old one.

Alternative: a Custom Action with the command `@IMAGEBACKUP` (turn on *Confirm*) starts the
backup with one tap. This only works while the printer is connected, because OctoDash sends
it as a printer command, and errors only show up in the plugin tab. You can switch this
command off in the settings.

## Restoring

Next to the images, the plugin keeps two plain-text files that open fine in Windows Notepad:

- `README.txt`: these restore instructions, filled in with the image name, the stopped
  services and the date and size of the last backup. Rewritten after each successful backup.
  If the folder already has a `README.txt` of your own, the plugin leaves it alone and
  writes `octopi-backup-README.txt` (named after the image) instead.
- `octopi-backup-log.txt` (named after the image): the log of the last backup run, with the
  result at the top. Written after failed backups too, as long as the backup folder was
  reachable.

The plugin recognises its own files by a marker line and never overwrites a file it didn't
write.

Write the `.img` file to an SD card with **Raspberry Pi Imager** (*Choose OS → Use custom*)
or **balenaEtcher**. Don't apply Imager's OS customisation. On first boot, the root partition
expands to fill the card and the Pi reboots once. The new card must be at least as big as the
image file, not as big as the original card.

## Uninstalling

```
sudo bash /path/to/octoprint_systemimagebackup/helper/install.sh --uninstall
```

The tab shows the path in its install command. This removes the helper, the service, the
sudoers rules, the saved SMB password and the status folder; image-utils, rsync, gdisk,
cifs-utils and nfs-common stay installed. Then remove the plugin in the Plugin Manager.
Your image files are never touched.

## Good to know

- `image-backup` copies the *running* system. With OctoPrint stopped this is reliable, but it
  is not the same as imaging a powered-off card. Write a backup to a spare card once and boot
  it to be sure.
- In update mode there is only one image. If a backup is interrupted halfway, that image may
  be incomplete. The tab says so; recreate it from scratch next time. Use *new image every
  time* with *keep 2* or more if you want a safety net.
- A new image is bigger than the used space on the card. image-backup makes the system
  partition 1.2 × the used space and then shrinks it, but the shrinking step often finds
  nothing to remove. On top of that come the *Room for updates* and the 0.5 GB boot partition.
  For example, 8.1 GB of used space gives an image of about 11.4 GB, with 2.2 GB free inside.
  The last lines of the log show this breakdown for each new image. The free space isn't lost:
  in update mode, it's what the system can grow into, and on restore the system expands to fill
  the card anyway.
- Sizes are given in GB of 10⁹ bytes, the way SD cards are labelled. Windows Explorer counts
  in units of 2³⁰ bytes, so it shows a slightly smaller number, e.g. 10.5 GB for an
  11.4 GB image.
- image-backup uses the image's partitions right after attaching it as a loop device, but
  the partition devices appear a moment later. Now and then this made a backup fail at once
  with "special device /dev/loop1p2 does not exist". The plugin therefore runs image-backup
  from a copy in RAM that waits for the partitions; the installed file stays unchanged. The
  copy is only made if image-backup's `mkloop` function is exactly the known code. If not
  (e.g. a different image-utils version), image-backup runs unchanged and the tab and the Start
  confirmation show a warning. If the error appears, start the backup again.
- The backup runs at most 6 hours before systemd stops it. OctoPrint is restarted in any case.

## Changelog

- **0.4.7** – Fixes an occasional failure at the start of a new image ("special device
  /dev/loop1p2 does not exist"): image-backup now waits until the image's partitions are ready.
  The tab and the Start confirmation warn if the installed image-backup differs from the known
  version, in which case it runs unchanged. Click **Update helper** after updating.
- **0.4.6** – The progress view (Pi's screen and SSH) updates every second, shows a progress bar
  while copying and the latest log lines, as many as fit on the screen. In a smaller SSH
  window, older lines are left out instead of scrolling. The view's files are kept in RAM
  (`/run`) instead of on the SD card. Click **Update helper** after updating.
- **0.4.5** – The start confirmation also shows the command for watching the backup over SSH.
- **0.4.4** – Watch a running backup over SSH with `/usr/local/sbin/octopi-image-backup watch`:
  the same view as on the Pi's screen, with or without the screen option. The tab shows this
  command instead of the journalctl one. Click **Update helper** after updating.
- **0.4.3** – The tab shows the SSH command for following a running backup, with a Copy button.
- **0.4.2** – Clearer log: each step after copying (four filesystem checks, shrinking, adding
  the room for updates) is one plain line, in the right order, and the log ends with what the
  image is made of. The Pi's screen shows these steps too. Sizes are now in GB of 10⁹ bytes
  everywhere, as in the tab; the log and `README.txt` used 2³⁰ before. The free-space check
  before a new image now includes the room for updates. Click **Update helper** after updating.
- **0.4.1** – The `README.txt` next to the images has no line breaks inside paragraphs, so
  Notepad's word wrap lays it out to the window width.
  The log file is easier to read: image-backup's repeated check output is condensed, and
  the last step is shown as "Finishing" instead of a second "Copying files".
- **0.4.0** – First release on GitHub. Full system images to a USB drive or an SMB or NFS
  share, progress on the Pi's screen, start from OctoDash, `README.txt` and log file next
  to the images, update notifications through OctoPrint's Software Update.

## Credits and license

image-utils (`image-backup` and friends) by **RonR** on the Raspberry Pi forums. It is not
part of this repository; the installer downloads it from the
[seamusdemora/RonR-RPi-image-utils](https://github.com/seamusdemora/RonR-RPi-image-utils)
mirror.

This plugin is released under the [MIT License](LICENSE).
