"""端口探测小工具：服务在监听则退出码 0，否则 1。

单独成文件是为了避开 bat 里内联 Python 代码的转义问题
（括号、分号在 cmd 里很容易被解析拆坏）。
"""
import socket
import sys

PORT = 8787


def main() -> int:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        code = s.connect_ex(("127.0.0.1", PORT))
    finally:
        s.close()
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
