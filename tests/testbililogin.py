"""刷刷B站的登录边界、账号关注来源和缓存失效检查。"""

import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # noqa: F401

# isort: split
from core.browsing.feed import BiliLoginRequired, BiliReader, BiliReadError
from core.browsing.material import BrowseMaterial
from core.interface.panel import DailyLifeDashboardComponent
from core.sight.bridge import SightComponent


class LoginReaderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cookies = {"SESSDATA": "test-session"}
        self.reader = BiliReader(lambda: dict(self.cookies))
        self.nav = {
            "isLogin": True,
            "mid": 1234,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
            },
        }
        self.reader._request = AsyncMock(return_value=self.nav)

    async def test_missing_cookie_prevents_all_sources(self):
        self.cookies.clear()
        for operation in (
            self.reader.candidates(["科学"]),
            self.reader.metadata("BV1xx411c7mD"),
            self.reader.subtitles({}),
            self.reader.audience({}),
        ):
            with self.assertRaises(BiliLoginRequired):
                await operation
        self.reader._request.assert_not_awaited()

    async def test_auth_cache_revalidates_and_detects_local_logout(self):
        with patch("core.browsing.feed.time.time", return_value=1000):
            self.assertEqual(await self.reader.require_login(), "1234")
            self.assertEqual(await self.reader.require_login(), "1234")
        self.reader._request.assert_awaited_once()
        with patch("core.browsing.feed.time.time", return_value=1301):
            await self.reader.require_login()
        self.assertEqual(self.reader._request.await_count, 2)
        self.cookies.clear()
        with self.assertRaises(BiliLoginRequired):
            await self.reader.require_login()
        self.assertEqual(self.reader._key, "")
        self.assertEqual(self.reader._mid, "")

    async def test_expired_cookie_is_cached_but_relogin_recovers(self):
        self.reader._request.return_value = {"isLogin": False}
        for _ in range(2):
            with self.assertRaisesRegex(BiliLoginRequired, "已失效"):
                await self.reader.require_login()
        self.reader._request.assert_awaited_once()
        self.cookies["SESSDATA"] = "new-test-session"
        self.reader._request.return_value = self.nav
        self.assertEqual(await self.reader.require_login(), "1234")

    async def test_cookie_switch_during_validation_rejects_old_account(self):
        async def switch(*args):
            self.cookies["SESSDATA"] = "another-test-session"
            return self.nav

        self.reader._request.side_effect = switch
        with self.assertRaisesRegex(BiliLoginRequired, "已变更"):
            await self.reader.require_login()
        self.assertEqual(self.reader._mid, "")

    async def test_following_pages_are_lazy_cached_and_rotated(self):
        requested = []

        async def request(url, params=None):
            if url.endswith("/nav"):
                return self.nav
            self.assertTrue(url.endswith("/x/relation/followings"))
            self.assertEqual(params["vmid"], "1234")
            page = params["pn"]
            requested.append(page)
            start = (page - 1) * 50 + 1
            return {
                "total": 121,
                "list": [{"mid": i} for i in range(start, min(start + 50, 122))],
            }

        self.reader._request.side_effect = request
        self.assertEqual(await self.reader.followed_up(0), "1")
        self.assertEqual(await self.reader.followed_up(1), "51")
        self.assertEqual(await self.reader.followed_up(2), "101")
        self.assertEqual(await self.reader.followed_up(3), "2")
        self.assertEqual(await self.reader.followed_up(147), "50")
        self.assertEqual(requested, [1, 2, 3])
        self.reader._follow_until = 0
        await self.reader.followed_up(0)
        self.assertEqual(requested, [1, 2, 3, 1])

    async def test_account_switch_clears_following_cache(self):
        self.reader._request.side_effect = [
            self.nav,
            {"total": 1, "list": [{"mid": 88}]},
            {**self.nav, "mid": 5678},
            {"total": 0, "list": []},
        ]
        self.assertEqual(await self.reader.followed_up(), "88")
        self.cookies["SESSDATA"] = "new-test-session"
        self.assertEqual(await self.reader.followed_up(), "")
        self.assertEqual(self.reader._mid, "5678")
        self.assertEqual(self.reader._request.await_args.args[1]["vmid"], "5678")

    async def test_following_permission_error_does_not_fake_interests(self):
        self.reader._request.side_effect = [self.nav, BiliReadError("列表暂不可读")]
        self.assertEqual(await self.reader.followed_up(), "")
        self.assertEqual(await self.reader.followed_up(), "")
        self.assertEqual(self.reader._request.await_count, 2)
        self.assertEqual(self.reader._mid, "1234")

    async def test_login_loss_never_falls_back_to_discovery(self):
        self.reader.followed_up = AsyncMock(side_effect=BiliLoginRequired("登录已失效"))
        with self.assertRaises(BiliLoginRequired):
            await self.reader.candidates([])
        urls = [call.args[0] for call in self.reader._request.await_args_list]
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("/nav"))
        self.assertTrue(urls[1].endswith("/x/polymer/web-dynamic/v1/feed/all"))
        self.assertFalse(any(url.endswith("/popular") for url in urls))

    async def test_api_queue_logout_stops_request_before_network(self):
        reader = BiliReader(lambda: {})
        with patch("core.browsing.feed.aiohttp.ClientSession") as session:
            with self.assertRaises(BiliLoginRequired):
                await reader._request(
                    "https://api.bilibili.com/x/web-interface/popular"
                )
            session.assert_not_called()


class LoginMaterialAndInterfaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_cached_material_still_requires_login(self):
        reader = types.SimpleNamespace(
            require_login=AsyncMock(side_effect=BiliLoginRequired("请先登录"))
        )
        material = BrowseMaterial(None, reader)
        material.cache["BV1xx411c7mD:1"] = {"segments": [{"text": "旧字幕"}]}
        with self.assertRaises(BiliLoginRequired):
            await material.load({"bvid": "BV1xx411c7mD", "page": 1})

    async def test_expired_subtitles_do_not_fall_back_to_anonymous_asr(self):
        reader = types.SimpleNamespace(
            require_login=AsyncMock(return_value="1234"),
            subtitles=AsyncMock(side_effect=BiliLoginRequired("登录已失效")),
        )
        runtime = types.SimpleNamespace(_prepare_sight_clip_material=AsyncMock())
        material = BrowseMaterial(runtime, reader)
        with self.assertRaises(BiliLoginRequired):
            await material.load(
                {
                    "bvid": "BV1xx411c7mD",
                    "page": 1,
                    "url": "https://www.bilibili.com/video/BV1xx411c7mD",
                    "title": "实验",
                }
            )
        runtime._prepare_sight_clip_material.assert_not_awaited()

    def context(self):
        jar = types.SimpleNamespace(is_logged_in=Mock(return_value=True), clear=Mock())
        context = types.SimpleNamespace(
            _event_is_group_message=lambda event: False,
            _bili_cookie_jar_for_runtime=lambda: jar,
            _sight_plain_result=lambda event, text: text,
            bili_browsing=types.SimpleNamespace(
                login_changed=AsyncMock(),
                reader=types.SimpleNamespace(
                    require_login=AsyncMock(return_value="1234")
                ),
            ),
        )
        return context, jar

    async def test_logout_invalidates_browsing_immediately(self):
        context, jar = self.context()
        result = await SightComponent(context).bili_logout(object())
        self.assertIn("已退出", result)
        jar.clear.assert_called_once()
        context.bili_browsing.login_changed.assert_awaited_once()

    async def test_login_status_checks_validity_not_file_presence(self):
        context, _ = self.context()
        context.bili_browsing.reader.require_login.side_effect = BiliLoginRequired(
            "已失效"
        )
        result = await SightComponent(context).bili_status(object())
        self.assertIn("已失效", result)

    async def test_history_remains_readable_without_login(self):
        async def page_json(callback):
            return await callback()

        service = types.SimpleNamespace(
            archive=types.SimpleNamespace(
                list=AsyncMock(return_value=[{"id": "saved"}]),
                state=AsyncMock(return_value={}),
            ),
            reader=types.SimpleNamespace(
                require_login=AsyncMock(side_effect=BiliLoginRequired("请先登录"))
            ),
            card=lambda record: record,
            social=types.SimpleNamespace(context=AsyncMock(return_value=[])),
        )
        context = types.SimpleNamespace(
            _page_query_args=lambda: {},
            _page_json=page_json,
            runtime=types.SimpleNamespace(
                bili_browsing=service,
                config=types.SimpleNamespace(
                    browsing=types.SimpleNamespace(enabled=True)
                ),
            ),
        )
        result = await DailyLifeDashboardComponent(context).page_browsing_list()
        self.assertEqual(result["items"], [{"id": "saved"}])
        self.assertIn("请先登录", result["error"])
