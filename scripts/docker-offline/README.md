# 离线 Docker 安装包（aarch64）· docker 29.8.1 + compose v5.5.1

给 维护者 的一句话用法——把整个包传到 ECS（与 oneserve 离线包同通道），在包所在目录执行：

    tar xf docker-offline-aarch64-29.8.1.tar
    cd docker-offline-aarch64-29.8.1
    sudo bash install.sh

**预期输出**（脚本末尾自动自检，三行）：

    Docker version 29.8.1, build xxxxxxx   （build 号随版本，有即可）
    Docker Compose version v5.5.1
    docker.service: active

退出后**重新登录一次**（让 docker 组生效，急用敲 newgrp docker），再验两条：

    docker --version          # 预期：Docker version 29.8.1, build …
    docker compose version    # 预期：Docker Compose version v5.5.1

两条都有输出 → 按《部署引导单-维护者专属_v0》§3 继续传 oneserve 离线包。

## 包内容

| 文件 | 说明 |
|---|---|
| docker-29.8.1.tgz | Docker 官方静态二进制包（aarch64，解出 dockerd/docker/containerd/runc 等 9 个） |
| docker-compose-linux-aarch64 | compose v5.5.1 独立二进制（装为 cli-plugins/docker-compose） |
| docker.service / docker.socket | systemd 单元（官方写法，路径适配 /usr/local/bin） |
| install.sh | 一键安装（幂等可重跑，重跑＝覆盖升级，不动镜像数据） |
| sha256sums.txt | 包内文件校验（install.sh 第 0 步强制执行） |
| README.md | 本文件 |

## 出错怎么办

| 症状 | 处置 |
|---|---|
| sha256 校验失败 | 包传输损坏：重新拷贝整个包目录再跑 |
| 此包不适用（x86_64） | 机器不是 arm64：uname -m 核对；x86_64 找 Rose 另打包 |
| Failed to start docker.service | journalctl -u docker.service --no-pager -n 50 看日志；多数是内核模块缺：sudo modprobe overlay && sudo modprobe br_netfilter，再 sudo systemctl restart docker |
| cgroup / mountpoint for cgroup 报错 | stat -fc %T /sys/fs/cgroup/ 看 cgroup 版本（cgroup2fs=v2 正常）；老内核报错带输出去问 Rose |
| permission denied … /var/run/docker.sock（重登后仍有） | id -nG 核对是否在 docker 组；没有就 sudo usermod -aG docker "$USER" 再重登 |
| 起栈报 address already in use（9080/8000） | 与本包无关，见引导单 §7：sudo lsof -i :9080 找占用 |
| 机器上有运维装的旧 docker | 本脚本已停用旧单元；旧二进制仍在 /usr/bin，但 PATH 中 /usr/local/bin 优先，which docker 应显示 /usr/local/bin/docker |

## 版本与来源（如实记录）

- Docker Engine 29.8.1：官方静态包
  https://download.docker.com/linux/static/stable/aarch64/docker-29.8.1.tgz
  （2026-09-22 该目录最新稳定版；官方文件名即 docker-29.8.1.tgz；aarch64 架构已用 file 命令核验）
- Docker Compose v5.5.1：GitHub 官方 releases
  https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-aarch64
  （直连成功，未用镜像源；ARM aarch64 已用 file 命令核验）
- 版本口径：组件申请清单 §二写的「Docker 24.x」是给运维的**底线**要求；29.8.1 为官方当前
  最新稳定版，向下兼容 24.x 用法，既有镜像与 compose 文件无需改动。
- 注意：本包 2026-09-22 由 Rose 在本机组包，仅做了语法/架构/校验和核验；ECS 首装实测由 维护者 执行（无本机实测条件，本机 Mac 禁装）。
