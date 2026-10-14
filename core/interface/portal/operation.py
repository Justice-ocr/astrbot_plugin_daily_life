from __future__ import annotations

from ...clock import now as life_now
from ...runtime.locks import operation_lock

from typing import Any, Protocol


class PortalActionComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _build_page_status: Any
    _page_bool: Any
    _page_json: Any
    _page_json_body: Any
    _page_week_plan: Any
    runtime: Any


class PortalActionComponent:
    def __init__(self, context: PortalActionComponentContext) -> None:
        self.context = context

    async def page_refresh_state(self):
        async def handler():
            now = life_now()
            target_date, _ = await self.context.runtime.resolve_injection_target(now)
            weather_refreshed = await self.context.runtime.try_update_weather(
                target_date,
                force=True,
            )
            await self.context.runtime.refresh_state_for_day(
                target_date,
                now=now,
                source="dashboard",
                detail="面板手动刷新天气和实时状态",
                force=True,
            )
            return {
                "weather_refreshed": weather_refreshed,
                "status": await self.context._build_page_status(),
            }

        return await self.context._page_json(handler)

    async def page_reset_day(self):
        async def handler():
            body = await self.context._page_json_body()
            now = life_now()
            target_date, _ = await self.context.runtime.resolve_injection_target(now)
            target_dt = self.context.runtime._target_datetime_for_command(target_date, now)
            extra = str(body.get("extra") or "").strip()
            web_inspiration = ""
            use_web = (
                self.context._page_bool(body["use_web"])
                if "use_web" in body
                else bool(self.context.runtime.config.search.inspiration_enabled)
            )
            result = await self.context.runtime.run_daily_generation(
                date=target_dt,
                source="dashboard_reset",
                force=True,
                extra=extra,
                use_web=use_web,
                search_keyword=extra or "今日生活",
                search_prompt=self.context.runtime.config.search.today_prompt,
                search_category="今日生活背景",
                reject_if_busy=True,
            )
            day = result.day
            web_inspiration = result.web_inspiration
            return {
                "day": day.as_dict() if day else None,
                "web_inspiration": web_inspiration,
                "operation_id": result.operation_id,
                "status": await self.context._build_page_status(),
            }

        return await self.context._page_json(handler)

    async def page_generate_week(self):
        async def handler():
            body = await self.context._page_json_body()
            goals = str(body.get("goals") or "").strip()
            web_inspiration = ""
            use_web = (
                self.context._page_bool(body["use_web"])
                if "use_web" in body
                else bool(self.context.runtime.config.search.inspiration_enabled)
            )
            week = life_now().strftime("%G-W%V")
            async with operation_lock(self.context.runtime, f"week:{week}"):
                if use_web:
                    web_inspiration = await self.context.runtime.composer.search.inspiration(
                        goals or "本周计划",
                        self.context.runtime.config.search.today_prompt,
                        category="周计划",
                        persona=await self.context.runtime.get_persona_text(),
                        today=life_now().strftime("%Y-%m-%d"),
                    )
                plan = await self.context.runtime.composer.generate_week_plan(
                    goals,
                    web_inspiration=web_inspiration,
                )
            return {
                "week_plan": self.context._page_week_plan(plan) if plan else None,
                "web_inspiration": web_inspiration,
                "status": await self.context._build_page_status(),
            }

        return await self.context._page_json(handler)
