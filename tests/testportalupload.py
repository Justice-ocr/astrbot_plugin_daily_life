import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import support  # noqa: F401 -- 先安装共用的 AstrBot 测试桩

# isort: split

from core.interface.portal.entry import PortalBaseComponent
from quart import Quart
from testdashboard import PagePlugin
from werkzeug.exceptions import RequestEntityTooLarge


class PortalUploadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="portal_upload_"))
        self.addCleanup(shutil.rmtree, self.root)
        self.app = Quart(__name__)
        self.base = PortalBaseComponent(None)
        self.base.context = self.base

    def request_context(self, data):
        body = (
            b'--upload-test\r\nContent-Disposition: form-data; name="file"; '
            b'filename="backup.zip"\r\nContent-Type: application/zip\r\n\r\n'
            + data
            + b"\r\n--upload-test--\r\n"
        )
        return self.app.test_request_context(
            "/restore",
            method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=upload-test"},
            data=body,
        )

    async def test_large_emoji_and_closet_backups_restore_through_multipart(self):
        for kind in ("emoji", "closet"):
            with self.subTest(kind=kind):
                plugin = PagePlugin()
                self.addCleanup(shutil.rmtree, plugin.runtime.data_path.parent)
                base = PortalBaseComponent(plugin)
                plugin._page_receive_upload = base._page_receive_upload
                manifest = {
                    "format": f"daily_life_{kind}_backup",
                    "version": 1,
                    "items": [],
                }
                expected = {}
                buffer = BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as package:
                    for index in range(5):
                        image = b"\x89PNG\r\n\x1a\n" + bytes([index]) * (
                            4 * 1024 * 1024
                        )
                        digest = hashlib.sha256(image).hexdigest()
                        member = f"assets/{digest}.png"
                        package.writestr(member, image)
                        manifest["items"].append(
                            {
                                "backup_asset": member,
                                "backup_sha256": digest,
                                "file_hash": digest,
                                "label": f"表情 {index}",
                                "kind": "hair",
                                "title": f"发型 {index}",
                                "description": f"还原素材 {index}",
                                "source_image_hash": digest,
                                "status": "ready" if kind == "emoji" else "active",
                                "source_kind": "manual",
                                "sendable": True,
                            }
                        )
                        expected[digest] = image
                    package.writestr("manifest.json", json.dumps(manifest))
                data = buffer.getvalue()
                self.assertGreater(len(data), 16 * 1024 * 1024)

                async with self.request_context(data):
                    response = await getattr(plugin, f"page_{kind}_restore")()
                    result = await response.get_json()

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["data"]["restored"], 5)
                self.assertEqual(result["data"]["files"], 5)
                self.assertEqual(result["data"]["skipped_records"], 0)
                for item in result["data"]["items"]:
                    path = item["file_path" if kind == "emoji" else "image_path"]
                    image = Path(path).read_bytes()
                    digest = hashlib.sha256(image).hexdigest()
                    self.assertEqual(image, expected[digest])
                transfer = plugin.runtime.data_path.parent / "transfer"
                self.assertFalse(list(transfer.glob("*.zip")))
                self.assertEqual(
                    self.app.config["MAX_CONTENT_LENGTH"], 16 * 1024 * 1024
                )

    async def test_file_at_limit_allows_multipart_headers(self):
        data = b"x" * (1024 * 1024)
        target = self.root / "backup.zip"
        async with self.request_context(data):
            result = await self.base._page_receive_upload(target, max_bytes=len(data))
        self.assertEqual(result["size"], len(data))
        self.assertEqual(target.read_bytes(), data)

    async def test_oversized_file_is_rejected_and_partial_file_removed(self):
        target = self.root / "backup.zip"
        async with self.request_context(b"x" * (1024 * 1024 + 1)):
            with self.assertRaisesRegex(ValueError, "上传文件超过大小限制"):
                await self.base._page_receive_upload(target, max_bytes=1024 * 1024)
        self.assertFalse(target.exists())

    def test_server_size_error_is_public(self):
        message = self.base._page_public_error(RequestEntityTooLarge())
        self.assertIn("超过服务器大小限制", message)
