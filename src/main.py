"""런타임 엔트리포인트: 설정을 로드하고 게이트웨이를 구동한다."""
import asyncio

from .config import load_config
from .gateway import Gateway


async def _main():
    config = load_config()
    await Gateway(config).run()


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
