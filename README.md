# taccap-websocket

TacCap 双夹爪、四路触觉和两路腕部相机的设备端服务。服务直接运行在连接
USB 设备的机器人主机上，通过 HTTP 向局域网中的 LeRobot 或其他控制端提供：

- 左右夹爪状态、使能、位置控制、MIT 阻抗控制和 5 秒安全租约；
- `left_wrist`、`right_wrist` 两路腕部视频；
- `left_tactile_left/right`、`right_tactile_left/right` 四路触觉视频；
- `/api/health`、`/api/grippers`、`/api/cameras` 诊断接口。
- `/api/grippers/{side}/stream` 持续推送 `.4` 已缓存的最新夹爪状态（NDJSON），供
  LeRobot 无轮询读取；每条记录包含 `server_status_sequence`、
  `server_status_updated_at_s`、`server_sent_at_s` 和 `server_cache_age_ms`，用于定位
  `.4` 采样停顿或到客户端的传输延迟。

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

如果 C++ 依赖安装在 conda/mamba 环境而不是系统路径，脚本会自动使用当前已激活
环境的 `CONDA_PREFIX`/`MAMBA_PREFIX`。也可以显式指定该环境的前缀：

```bash
export TACCAP_CPP_PREFIX=/path/to/cpp-deps-env
```

这个前缀只用于构建机编译。`xense.taccap` 含有 C++ 原生扩展，必须在不高于目标
系统 ABI 的环境中构建：例如要部署到 Ubuntu 20.04，就应在 Ubuntu 20.04/glibc
2.31 的构建机（或兼容构建机）生成 bundle。Ubuntu 22.04 上生成的扩展可能要求
`GLIBC_2.32` 或更高版本，不能部署到 Ubuntu 20.04。

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

如果本机不是 Ubuntu 20.04，不要在本机编译 TacCap 原生扩展。先在 Ubuntu 20.04
构建机上生成 wheel，再复制回本机组装 bundle。当前 `.4` 使用系统 OpenCV 4.2，
因此不能使用在 Ubuntu 22.04 上编译、依赖 `libopencv_*.so.4.5d` 的 wheel：

```bash
./bundle.sh \
  --taccap-wheel /path/to/taccap_gripper-*.whl \
  --xensesdk 'xensesdk==2.1.3'
```

`--taccap-wheel` 只替换 TacCap 原生扩展来源；Python runtime、xensesdk 和离线
归档仍由本机生成。wheel 必须来自不高于目标系统 ABI 的构建机。脚本不会在构建
机上强行加载目标机没有的 OpenCV SONAME，而是把最终导入检查留给目标机；这样
Ubuntu 20.04 的系统 `libopencv_*.so.4.2` 可以正常解析。

本项目开发机已经保存了一份可供 `.4` 使用的 wheel，可直接执行：

```bash
cd /home/xense/tron2/taccap-websocket
./bundle.sh \
  --taccap-wheel /home/xense/taccap-sdk-wheels/taccap_gripper-0.1.9-cp312-cp312-linux_x86_64.whl \
  --xensesdk 'xensesdk==2.1.3'
```

不要把之前用普通 `--taccap-source` 在 Ubuntu 22.04 上生成的 `offline/` 继续部署
到 `.4`；其中的原生模块会绑定 `libopencv_core.so.4.5d`，并可能要求高于
Ubuntu 20.04 的 glibc。重新生成 bundle 后再执行 `./deploy.sh guest@10.192.1.4`。

若 `xensesdk` 不是默认版本，可传入 requirement：

```bash
./bundle.sh --xensesdk 'xensesdk==2.1.3'
```

下载、编译和安装只发生在联网构建机；目标机只解包已安装的 Python 文件，完全不会
访问 Python 包索引，也不需要 pip、uv、Git 或编译器。发布包不包含任何 `.whl`；
构建过程中 pip 产生的临时构建产物会在脚本退出时删除。

安装器会在停止现有服务前，在临时目录中导入 `xensesdk` 和 `xense.taccap` 做 ABI
预检。预检失败会直接报错并退出，不会使用目标机遗留的 `/home/guest/py312`、
系统 Python 或旧 TacCap SDK 作为替代。此时必须在兼容的 Ubuntu 20.04 构建机重新
生成 bundle。

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
./scripts/taccap.sh start
./scripts/taccap.sh stop
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

# 查询当前命令模式
curl http://10.192.1.4:8765/api/grippers/left

# 持续查看左夹爪缓存状态（每行一个 JSON 快照）
curl -N http://10.192.1.4:8765/api/grippers/left/stream

# 切换左夹爪到 MIT 阻抗模式
curl -X POST http://10.192.1.4:8765/api/grippers/left/control_mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"mit"}'
```

夹爪控制模式
--------------

服务支持两种夹爪命令模式，默认是 `position`，以保持旧客户端兼容：

- `position`：使用 SDK `ControlLoop` 的保守阻抗增益；
- `mit`：使用 SDK `ControlLoop` 的 MIT 阻抗增益和前馈力矩。当前 SDK 已经从
  Python 中移除裸 `Motor.submit_impedance()` / `Motor.set_position()`，因此两种
  模式都通过 `ControlLoop` 的安全、锁相实时路径发送。

Web 页面中可以分别为左右夹爪选择模式。命令行客户端也可以选择：

```bash
taccap-client --base http://10.192.1.4:8765 mode left mit
taccap-client --base http://10.192.1.4:8765 mode right position
taccap-client --base http://10.192.1.4:8765 mode left
```

模式默认值可以在目标机 `config/taccap.env` 中设置：

```bash
TACCAP_GRIPPER_CONTROL_MODE=mit
# 或只覆盖一侧
TACCAP_LEFT_GRIPPER_CONTROL_MODE=mit
TACCAP_RIGHT_GRIPPER_CONTROL_MODE=position
```

位置模式参数默认是 `kp=8.0`、`kd=1.0`；MIT 参数默认是 `kp=8.0`、`kd=1.0`、
前馈力矩 `0.0 Nm`，可以通过
`TACCAP_POSITION_KP`、`TACCAP_POSITION_KD`、
`TACCAP_MIT_KP`、`TACCAP_MIT_KD` 和 `TACCAP_MIT_FEEDFORWARD_TORQUE` 调整。
服务状态中的 `command_mode` 表示当前选中的增益组；当前 SDK 的安全控制器实际
发送的是 MIT impedance 帧，`control_mode_name` 会显示 `impedance (MIT)`。

调试时可以通过 Web 页面每个夹爪卡片中的“应用调参”修改当前或指定模式的
`kp`、`kd`、有符号的基础前馈力矩、速度前馈上限、位置误差力矩上限和目标速度。
也可以直接调用 REST API：

```bash
# 查询当前参数和安全范围
curl http://10.192.1.4:8765/api/grippers/left/control_parameters

# 修改左夹爪 MIT 参数（立即作用于运行中的 ControlLoop）
curl -X POST http://10.192.1.4:8765/api/grippers/left/control_parameters \
  -H 'Content-Type: application/json' \
  -d '{"mode":"mit","kp_nm_per_rad":12,"kd_nm_s_per_rad":1.5,
       "feedforward_torque_nm":0,"speed_feedforward_limit_nm":1.0,
       "max_position_torque_nm":0.25,
       "target_max_velocity_rad_s":0.6}'
```

参数有服务端安全上限，当前分别是 `kp≤100`、`kd≤50`、前馈力矩绝对值
`≤2 Nm`、速度前馈上限 `0–2 Nm`、位置误差力矩上限 `≤2 Nm`、目标速度 `≤4 rad/s`。
目标速度现在同时用于
反馈目标和 MIT 速度前馈：桥接层根据实际位置、开合方向和 `kd` 计算有界的
速度前馈力矩，并在每个电机状态周期更新；用户前馈与速度前馈的合计值受
`±2 Nm` 上限约束，ControlLoop 和固件仍会执行各自的力矩与堵转保护。速度前馈先独立限制在
`±speed_feedforward_limit_nm`，再与有符号的基础前馈相加，因此将速度前馈上限设为 `1 Nm`
时，张开和闭合方向的速度分量都不会超过 `±1 Nm`。页面中的
“速度”是电机实际反馈，“目标”
是请求值，“速度前馈力矩”是限幅后实际参与合成的速度分量，“合计前馈”是最终
传给 `ControlLoop` 的基础与速度前馈之和。将目标速度设为 `0` 会关闭速度
前馈和目标斜坡，恢复为普通位置阻抗跟踪。由于电机负载、力矩上限和堵转保护的影响，
每个新位置目标只启动一次速度辅助接近；进入容差或越过目标后会锁存为 `holding`，
速度前馈清零并固定下发最终目标，之后的回弹或反馈噪声不会重新启动或反转速度前馈。
目标速度是受安全约束的运动目标，不保证任何负载下都能达到该数值；当速度目标较高
而 `kd × 目标速度` 超过前馈上限时，速度会因饱和而低于设定值。因此默认
`kd=1.0` 时约从 `2 rad/s` 起不再增加速度前馈；调试 `3–4 rad/s` 时需要逐步将
`kd` 降至约 `0.7–0.5`，并同时观察实际速度、力矩和温度。新版 SDK 的
`STREAM_LOCKED` 模式下，控制循环频率和电机状态流固定为 100 Hz，不能通过旧的
`max_velocity` 参数直接改变电机固件速度。

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
