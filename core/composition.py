"""为独立组件声明宿主入口，保留原函数的签名与装饰器语义。"""

from __future__ import annotations

from functools import wraps
import inspect
from typing import Any


class ComponentCall:
    """将一个明确声明的入口委托给宿主拥有的组件实例。

    不扫描或复制组件的方法，不参与继承；每个入口由宿主逐一声明。
    组件由宿主的 cached_property 创建，状态和任务生命周期仍归宿主管理。
    """

    def __init__(self, field: str, component: type, method: str) -> None:
        self.field = field
        self.method = method
        original = getattr(component, method)

        if inspect.isasyncgenfunction(original):
            @wraps(original)
            async def invoke(host: Any, *args: Any, **kwargs: Any):
                iterator = getattr(getattr(host, field), method)(*args, **kwargs)
                try:
                    async for item in iterator:
                        yield item
                finally:
                    await iterator.aclose()
        elif inspect.iscoroutinefunction(original):
            @wraps(original)
            async def invoke(host: Any, *args: Any, **kwargs: Any) -> Any:
                return await getattr(getattr(host, field), method)(*args, **kwargs)
        else:
            @wraps(original)
            def invoke(host: Any, *args: Any, **kwargs: Any) -> Any:
                return getattr(getattr(host, field), method)(*args, **kwargs)

        self.invoke = invoke

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self.invoke
        return getattr(getattr(instance, self.field), self.method)
