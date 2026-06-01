"""런타임 엔트리포인트: 설정을 로드하고 Step 1 런타임(App)을 구동한다."""
import asyncio

from .app import App
from .config import load_config


async def _main():
    config = load_config()
    await App(config).run()


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
