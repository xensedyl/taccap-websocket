# taccap-websocket

TacCap 双夹爪、四路触觉和两路腕部相机的设备端服务。服务直接运行在连接
USB 设备的机器人主机上，通过 HTTP 向局域网中的 LeRobot 或其他控制端提供：

- 左右夹爪状态、使能、位置控制和 5 秒安全租约；
- `left_wrist`、`right_wrist` 两路腕部视频；
- `left_tactile_left/right`、`right_tactile_left/right` 四路触觉视频；
- `/api/health`、`/api/grippers`、`/api/cameras` 诊断接口。

触觉由 `xensesdk.Sensor.OutputType.Rectify` 采集，SDK 参数为
`rectify_size=(400, 700)`（宽、高），输出保持标定矫正图像格式。

## 项目结构

```text
taccap-websocket/
├── src/taccap_websocket/       # 核心 Python 服务和触觉采集 worker
├── config/                     # 配置模板（设备配置不提交）
├── scripts/taccap.sh           # 目标机运行、启停、诊断入口
├── bundle.sh                   # 本机生成完整离线发布包
├── deploy.sh                   # SSH 上传并一键安装/启动
├── packaging/                  # systemd 模板
├── vendor/                     # SDK 来源说明（不存二进制 wheel）
└── .log/                       # 运行日志（不提交）
```

## 部署方式

只保留两步。目标机不需要 Python、pip、uv、Git、编译器或网络连接；目标机只需要
已经具备 Linux USB/UVC 驱动、`ffmpeg`、`curl` 和与 SDK 匹配的系统运行库。

### 1. 生成离线 bundle（本机执行）

在可以联网的开发机执行。构建机需要 `uv`、`git`、Python 3.12+、C++ 编译器，
以及 TacCap-Gripper 所需的 C++ OpenCV 和 spdlog 开发包；脚本会在临时构建环境中
安装 CMake、Ninja、scikit-build-core 和 pybind11：
（若 `uv` 无法下载 portable Python，可设置 `TACCAP_BUNDLE_RUNTIME` 指向一个
已准备好的 Python 3.12 runtime 目录。）

如果 C++ 依赖安装在 conda/mamba 环境而不是系统路径，先指定该环境的前缀：

```bash
export TACCAP_CPP_PREFIX=/path/to/cpp-deps-env
```

这个前缀只用于构建机编译；目标机不需要 conda 或编译器，但仍需要与构建产物
ABI 兼容的系统运行库（尤其是 glibc、libstdc++ 和 OpenCV 运行库）。

```bash
cd /home/xense/tron2/taccap-websocket
./bundle.sh \
  --xensesdk 'xensesdk==2.1.3' \
  --taccap-source https://github.com/XenseRobotics-AI/TacCap-Gripper.git
```

脚本会生成 `offline/`，其中包含：

- 可复制的 Python 3.12 运行时；
- 已安装的 `site-packages.tar.gz`（包括 `xensesdk`、`taccap-gripper` 和传递依赖）；
- `manifest.txt` 及归档 SHA256。

`xensesdk` 使用 pip 的包名安装，因此可通过 pip 默认索引、公司内部索引或本机
配置的镜像解析。项目的普通安装命令也会从 Git URL 构建 `taccap-gripper`；离线
bundle 则允许用 `--taccap-source` 指向本机 checkout，避免重复 clone。例如：

```bash
./bundle.sh --taccap-source /home/xense/tron2/TacCap-Gripper
```

若 `xensesdk` 不是默认版本，可传入 requirement：

```bash
./bundle.sh --xensesdk 'xensesdk==2.1.3'
```

下载、编译和安装只发生在联网构建机；目标机只解包已安装的 Python 文件，完全不会
访问 Python 包索引，也不需要 pip、uv、Git 或编译器。发布包不包含任何 `.whl`；
构建过程中 pip 产生的临时构建产物会在脚本退出时删除。

### 2. SSH 一键部署到新设备

生成 bundle 后，在本机执行：

```bash
./deploy.sh guest@10.192.1.4
```

它会自动完成：

1. 通过 SSH 上传源码和 `offline/`；
2. 校验 bundle 的 SHA256；
3. 安装自带 Python 3.12，并解包构建机已经安装好的 SDK site-packages；
4. 保留目标机已有的 `config/taccap.env`、设备配置和日志；
5. 写入用户级 systemd 服务并启动；
6. 使用目标机的 `ffmpeg` 启动六路视频和夹爪接口。

指定安装目录：

```bash
./deploy.sh guest@10.192.1.4 \
  --bundle ./offline \
  --install-dir /home/guest/taccap-websocket
```

只安装、不启动：

```bash
./deploy.sh guest@10.192.1.4 --no-start
```

首次连接建议配置 SSH 公钥，避免每次输入密码：

```bash
ssh-copy-id guest@10.192.1.4
```

部署脚本不使用旧设备上的 `/home/guest/py312/bin/python` 或
`/home/guest/activate_taccap312`。目标机的 Python 和 SDK 全部来自 bundle，且不依赖
目标机联网。

## 目标机运行管理

部署完成后，登录目标机执行：

```bash
cd ~/taccap-websocket
./scripts/taccap.sh status
./scripts/taccap.sh health
./scripts/taccap.sh doctor
./scripts/taccap.sh restart
./scripts/taccap.sh logs
```

服务默认监听 `0.0.0.0:8765`，本机可直接访问目标设备地址：

```bash
curl http://10.192.1.4:8765/api/health
curl http://10.192.1.4:8765/api/grippers
curl http://10.192.1.4:8765/api/cameras
```

日志统一保存到目标机项目的 `.log/`，文件名包含日期、时间和进程号，例如：

```text
.log/taccap_20260907-153012_12345.log
```

## 配置

首次部署从 `config/taccap.env.example` 创建 `config/taccap.env`。常用配置：

```bash
TACCAP_BIND_HOST=0.0.0.0
TACCAP_PORT=8765
TACCAP_FFMPEG=/usr/bin/ffmpeg
TACCAP_LOG_DIR=.log
```

设备默认按 TacCap 序列号、V4L2 `by-id` 和 USB 拓扑自动发现。特殊硬件可复制
`config/devices.json.example` 为 `config/devices.json`，再设置：

```bash
TACCAP_DEVICE_CONFIG=config/devices.json
```

设备配置和日志属于目标机状态，不会被后续部署覆盖。

## 版本和回滚

发布前在本机提交 Git：

```bash
git add .
git commit -m "describe the release"
./bundle.sh
./deploy.sh guest@TARGET
```

`deploy.sh` 会把当前提交写入目标机的 `.release`。要回滚，切换到已知稳定提交后重新
执行 `bundle.sh` 和 `deploy.sh`。

## License

本项目采用 [MIT License](LICENSE)。
