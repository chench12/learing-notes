"""最小可运行示例：验证本仓库的代码目录能直接执行。

运行：python 01-python-basics/examples/hello.py
"""

from datetime import date


def greet(name: str) -> str:
    return f"Hello, {name}! Today is {date.today():%Y-%m-%d}."


def main() -> None:
    print(greet("learning notes"))


if __name__ == "__main__":
    main()
