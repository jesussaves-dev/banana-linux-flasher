"""
Linux system-disk detection, drive re-validation and fail-closed unmounting.

A fake /sys/class/block tree stands in for sysfs, so these run anywhere.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import flash_banana as fb


class FakeSysfs:
    """Builds /sys/class/block-style entries under a temp dir."""

    def __init__(self, root: str):
        self.root = root
        self.devices = os.path.join(root, "devices")
        self.cls = os.path.join(root, "class_block")
        os.makedirs(self.devices)
        os.makedirs(self.cls)

    def disk(self, name: str, *parts: str) -> None:
        path = os.path.join(self.devices, name)
        os.makedirs(path)
        os.symlink(path, os.path.join(self.cls, name))
        for part in parts:
            ppath = os.path.join(path, part)
            os.makedirs(ppath)
            open(os.path.join(ppath, "partition"), "w").close()
            os.symlink(ppath, os.path.join(self.cls, part))

    def mapper(self, name: str, *slaves: str) -> None:
        path = os.path.join(self.devices, "virtual", name)
        os.makedirs(os.path.join(path, "slaves"))
        for slave in slaves:
            open(os.path.join(path, "slaves", slave), "w").close()
        os.symlink(path, os.path.join(self.cls, name))


class LinuxDiskResolutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="banana-sysfs_")
        self.addCleanup(tmp.cleanup)
        self.sys = FakeSysfs(tmp.name)
        self.sys.disk("nvme0n1", "nvme0n1p1", "nvme0n1p2")
        self.sys.disk("sda", "sda1")
        self.sys.disk("mmcblk0", "mmcblk0p1")
        self.sys.mapper("dm-0", "nvme0n1p2")       # LUKS on nvme0n1p2
        self.sys.mapper("dm-1", "dm-0")            # LVM LV inside the LUKS

        real_realpath = os.path.realpath
        aliases = {"/dev/mapper/cryptroot": "/dev/dm-0",
                   "/dev/mapper/vg-root": "/dev/dm-1"}

        def realpath(path, *a, **kw):
            if path in aliases:
                return aliases[path]
            if path.startswith("/dev/"):
                return path
            return real_realpath(path, *a, **kw)

        for patcher in (
            mock.patch.object(fb, "SYS_CLASS_BLOCK", self.sys.cls),
            mock.patch.object(fb.os.path, "realpath", side_effect=realpath),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_partition_resolves_to_disk(self):
        self.assertEqual(fb._linux_disks_of("/dev/nvme0n1p1"), {"nvme0n1"})
        self.assertEqual(fb._linux_disks_of("/dev/mmcblk0p1"), {"mmcblk0"})

    def test_btrfs_subvolume_suffix_is_stripped(self):
        # findmnt prints btrfs roots as "/dev/sda1[/@]".
        self.assertEqual(fb._linux_disks_of("/dev/sda1[/@]"), {"sda"})

    def test_luks_and_lvm_resolve_to_the_physical_disk(self):
        self.assertEqual(fb._linux_disks_of("/dev/mapper/cryptroot"), {"nvme0n1"})
        self.assertEqual(fb._linux_disks_of("/dev/mapper/vg-root"), {"nvme0n1"})

    def test_whole_disk_and_unknown_nodes(self):
        self.assertEqual(fb._linux_disks_of("/dev/sda"), {"sda"})
        self.assertEqual(fb._linux_disks_of("/dev/nope"), set())
        self.assertEqual(fb._linux_disks_of("tmpfs"), set())

    def test_protected_disks(self):
        mounts = [
            ("/dev/mapper/vg-root", "/"),
            ("/dev/sda1", "/mnt/data"),                    # second disk: protect
            ("/dev/mmcblk0p1", "/run/media/me/BOOT"),      # automounted card
        ]
        with mock.patch.object(fb, "_linux_mounts", return_value=mounts), \
                mock.patch.object(fb.subprocess, "check_output",
                                  side_effect=OSError):
            self.assertEqual(fb._linux_protected_disks(), {"nvme0n1", "sda"})

    def test_swap_is_protected_even_on_removable_media(self):
        mounts = [("/dev/mmcblk0p1", "[SWAP]")]
        with mock.patch.object(fb, "_linux_mounts", return_value=mounts), \
                mock.patch.object(fb.subprocess, "check_output",
                                  side_effect=OSError):
            self.assertEqual(fb._linux_protected_disks(), {"mmcblk0"})

    def test_mount_field_unescape(self):
        self.assertEqual(fb._unescape_mount_field(r"/run/media/me/MY\040CARD"),
                         "/run/media/me/MY CARD")


class LinuxUnmountTests(unittest.TestCase):
    DEVICE = {"path": "/dev/sdb", "raw": "/dev/sdb", "label": "", "size": 8,
              "removable": True, "protected": False, "detail": "", "index": None}

    def _wipe(self, run):
        mounts = [("/dev/sdb1", "/run/media/me/BOOT"),
                  ("/dev/sdb2", "/run/media/me/BOOT/inner"),
                  ("/dev/sda1", "/")]
        disks = {"/dev/sdb1": {"sdb"}, "/dev/sdb2": {"sdb"}, "/dev/sda1": {"sda"}}
        with mock.patch.object(fb, "_linux_mounts", return_value=mounts), \
                mock.patch.object(fb, "_linux_disks_of",
                                  side_effect=lambda n: disks.get(n, set())), \
                mock.patch.object(fb.os.path, "realpath", side_effect=lambda p: p), \
                mock.patch.object(fb, "_run", side_effect=run):
            fb._wipe_linux(self.DEVICE, lambda _m: None)

    def test_unmounts_only_this_disk_deepest_first(self):
        calls = []
        self._wipe(lambda cmd, **kw: calls.append(cmd))
        umounts = [c for c in calls if c[0] == "umount"]
        self.assertEqual(umounts, [["umount", "/run/media/me/BOOT/inner"],
                                   ["umount", "/run/media/me/BOOT"]])
        self.assertTrue(any(c[0] == "dd" for c in calls))

    def test_failed_unmount_aborts_before_wiping(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "umount":
                raise fb.FlasherError("target is busy")

        with self.assertRaises(fb.FlasherError) as ctx:
            self._wipe(run)
        self.assertIn("NOT modified", str(ctx.exception))
        self.assertFalse(any(c[0] in ("wipefs", "dd") for c in calls))


class RevalidateTests(unittest.TestCase):
    def _dev(self, **over):
        dev = fb._device("/dev/sdb", "/dev/sdb", "/dev/sdb card", 8 * 2**30,
                         True, False, detail="SD Card USB")
        dev.update(over)
        return dev

    def test_same_card_passes(self):
        with mock.patch.object(fb, "list_devices", return_value=[self._dev()]):
            self.assertEqual(fb.revalidate_device(self._dev())["path"], "/dev/sdb")

    def test_swapped_drive_is_refused(self):
        other = self._dev(size=2 * 2**40, detail="Backup HDD USB")
        with mock.patch.object(fb, "list_devices", return_value=[other]):
            with self.assertRaises(fb.FlasherError):
                fb.revalidate_device(self._dev())

    def test_removed_drive_is_refused(self):
        with mock.patch.object(fb, "list_devices", return_value=[]):
            with self.assertRaises(fb.FlasherError):
                fb.revalidate_device(self._dev())

    def test_now_protected_drive_is_refused(self):
        with mock.patch.object(fb, "list_devices",
                               return_value=[self._dev(protected=True)]):
            with self.assertRaises(fb.FlasherError):
                fb.revalidate_device(self._dev())

    def test_image_must_fit(self):
        dev = self._dev(size=4 * 2**30)
        fb.check_image_fits(3 * 2**30, dev)
        fb.check_image_fits(0, dev)                  # unknown size: allowed
        with self.assertRaises(fb.FlasherError):
            fb.check_image_fits(5 * 2**30, dev)


if __name__ == "__main__":
    unittest.main()
