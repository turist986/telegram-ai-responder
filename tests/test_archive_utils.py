import io
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from app.services import archive_utils as au
from app.services.archive_utils import ArchiveError, detect_kind, extract_tdata_archive

try:
    import rarfile

    HAVE_RARFILE = True
except ImportError:
    HAVE_RARFILE = False


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def make_rar4_stored(files: dict[str, bytes]) -> bytes:
    """Настоящий RAR 4.x без сжатия (method 0x30), собранный вручную: rar-архиватор
    проприетарный, а для «хранимых» файлов rarfile не требует внешней программы."""
    crc16 = lambda b: zlib.crc32(b) & 0xFFFF  # noqa: E731
    out = b"Rar!\x1a\x07\x00"
    main = struct.pack("<BHH", 0x73, 0, 13) + struct.pack("<HI", 0, 0)
    out += struct.pack("<H", crc16(main)) + main
    for name, data in files.items():
        nb = name.encode("utf-8")
        body = struct.pack("<BHH", 0x74, 0x8000, 32 + len(nb))
        body += struct.pack("<IIBIIBBHI", len(data), len(data), 3, zlib.crc32(data) & 0xFFFFFFFF,
                            0x5A1F0000, 20, 0x30, len(nb), 0x20)
        body += nb
        out += struct.pack("<H", crc16(body)) + body + data
    return out + b"\xc4\x3d\x7b\x00\x40\x07\x00"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_zip_extracts_needed_files_and_skips_profile_cache(self):
        src = self._write("a.zip", make_zip({
            "1/tdata/key_datas": b"k",
            "1/tdata/D877F783D5D3EF8C/maps": b"m",
            "1/tdata/user_data/cache/big": b"x" * 100,        # кеш профиля — не нужен
            "1/tdata/emoji/cache_18_0": b"e",
            "1/Telegram.exe": b"z",
        }))
        n = extract_tdata_archive(src, self.tmp / "out")
        self.assertTrue((self.tmp / "out/1/tdata/key_datas").exists())
        self.assertTrue((self.tmp / "out/1/tdata/D877F783D5D3EF8C/maps").exists())
        self.assertFalse((self.tmp / "out/1/tdata/user_data").exists())
        self.assertFalse((self.tmp / "out/1/tdata/emoji").exists())
        self.assertEqual(n, 3)

    def test_format_detected_by_content_not_extension(self):
        src = self._write("really_zip.rar", make_zip({"tdata/key_datas": b"k"}))
        self.assertEqual(detect_kind(src), "zip")
        extract_tdata_archive(src, self.tmp / "out")
        self.assertTrue((self.tmp / "out/tdata/key_datas").exists())

    def test_not_an_archive(self):
        with self.assertRaises(ArchiveError):
            extract_tdata_archive(self._write("x.zip", b"plain text, not an archive"), self.tmp / "out")

    def test_unsafe_paths_reject_the_whole_archive(self):
        for evil in ("../evil.txt", "a/../../evil.txt", "/abs/evil.txt", "C:/evil.txt", "..\\evil.txt"):
            with self.subTest(evil=evil):
                src = self._write("e.zip", make_zip({"ok/key_datas": b"k", evil: b"x"}))
                with self.assertRaises(ArchiveError):
                    extract_tdata_archive(src, self.tmp / "out")
                self.assertFalse((self.tmp / "evil.txt").exists())
                self.assertFalse((self.tmp.parent / "evil.txt").exists())

    def test_windows_backslash_names_are_treated_as_folders(self):
        src = self._write("w.zip", make_zip({"1\\tdata\\key_datas": b"k"}))
        extract_tdata_archive(src, self.tmp / "out")
        self.assertTrue((self.tmp / "out/1/tdata/key_datas").exists())

    def test_size_and_count_limits(self):
        src = self._write("l.zip", make_zip({f"t/f{i}": b"x" for i in range(5)}))
        with patch.object(au, "MAX_FILES", 3), self.assertRaises(ArchiveError):
            extract_tdata_archive(src, self.tmp / "o1")
        src2 = self._write("l2.zip", make_zip({"t/a": b"x" * 50, "t/b": b"x" * 50}))
        with patch.object(au, "MAX_TOTAL_BYTES", 60), self.assertRaises(ArchiveError):
            extract_tdata_archive(src2, self.tmp / "o2")
        with patch.object(au, "MAX_FILE_BYTES", 10):             # слишком крупный файл — пропускаем, не падаем
            extract_tdata_archive(src2, self.tmp / "o3")
        self.assertFalse((self.tmp / "o3/t/a").exists())

    def test_declared_size_lie_is_caught_while_copying(self):
        with self.assertRaises(ArchiveError):
            au._copy_limited(io.BytesIO(b"x" * 100), io.BytesIO(), 10, "f")

    @unittest.skipUnless(HAVE_RARFILE, "rarfile не установлен")
    def test_rar_stored_extracts_and_skips_cache(self):
        src = self._write("a.rar", make_rar4_stored({
            "2/tdata/key_datas": b"k2",
            "2/tdata/user_data/x": b"cache",
        }))
        self.assertEqual(detect_kind(src), "rar")
        extract_tdata_archive(src, self.tmp / "out")
        self.assertEqual((self.tmp / "out/2/tdata/key_datas").read_bytes(), b"k2")
        self.assertFalse((self.tmp / "out/2/tdata/user_data").exists())

    @unittest.skipUnless(HAVE_RARFILE, "rarfile не установлен")
    def test_rar_without_extraction_tool_gives_actionable_message(self):
        src = self._write("a.rar", make_rar4_stored({"1/tdata/key_datas": b"k"}))

        class Boom:
            def __init__(self, *a, **k):
                raise rarfile.RarCannotExec("no tool")

        with patch.object(rarfile, "RarFile", Boom), self.assertRaises(ArchiveError) as cm:
            extract_tdata_archive(src, self.tmp / "out")
        self.assertIn("7-Zip", str(cm.exception))                # что именно поставить, а не «ошибка»


if __name__ == "__main__":
    unittest.main()
