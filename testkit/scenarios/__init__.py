"""scenarios/：S0 冒烟 / S1 爬坡演示 / S2 三通道 / S3 限流 / S4 回归梯子的用户画像。

画像口径＝loadtest/locustfile.py（commit 10e5c81）移植：闭环无思考时间（constant(0)）
＋键池随机取样防单键缓存失真；超时 30s 兜底＝客户端口径（服务端 guard 3s）。
"""
from scenarios import common  # noqa: F401
