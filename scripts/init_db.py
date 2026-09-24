"""初始化入口只委托唯一迁移 CLI，不创建另一套 schema。"""

from scripts.migrate import main

if __name__ == "__main__":
    raise SystemExit(main())
