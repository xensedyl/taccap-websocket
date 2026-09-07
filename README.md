# taccap-websocket

可迁移的 TacCap 夹爪、触觉和腕部相机服务。项目不依赖 ROS，目标设备只需要
连接两只 TacCap 夹爪，并提供四路触觉相机和两路腕部 UVC 相机。服务在设备本地
采集数据，通过目标设备的 HTTP 端口直接向 LeRobot 提供：

- 两侧夹爪状态、使能、位置控制和安全心跳租约；
- left_wrist、right_wrist 两路腕部图像；
- left_tactile_left/right、right_tactile_left/right 四路触觉图像；
- /api/health、/api/grippers、/api/cameras 等诊断接口。

触觉图像在目标设备上使用 xensesdk.Sensor.OutputType.Rectify 采集，SDK 参数为
rectify_size=(400, 700)（宽、高）。SDK 返回通常为 (700, 400, 3)，LeRobot
客户端再转换为旧数据集使用的 (400, 700, 3)。设备编号优先使用 TacCap 固件
序列号、V4L2 by-id 和 USB 拓扑发现，不依赖会随重启变化的 /dev/videoN。

## 迁移方式

本机 Git 仓库是唯一源代码来源，目标设备只保存部署后的运行副本。换设备时，不需要
复制旧设备的 Python 虚拟环境或手工修改串口路径；把仓库部署到新设备即可。

### 方式一：本机通过 SSH 一键部署

在本仓库目录执行：

~~~bash
cd /home/xense/tron2/taccap-websocket
./deploy.sh guest@10.192.1.4 \
  --python /home/guest/py312/bin/python \
  --env-script /home/guest/activate_taccap312 \
  --no-deps
~~~

换成新设备时只替换 SSH 地址：

~~~bash
./deploy.sh user@NEW_DEVICE_IP --install-system-deps --enable-systemd
~~~

新设备没有 Python 3.12、`TACCAP_PYTHON` 或旧设备激活脚本时，使用自动引导模式：

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --bootstrap-python \
  --install-system-deps \
  --enable-systemd
~~~

该模式需要目标设备能访问网络，并预先安装 `uv`。它会将 Python 3.12 放在项目的
`.runtime/python` 下。若目标设备没有 `uv`，可先执行官方安装命令：

~~~bash
curl -LsSf https://astral.sh/uv/install.sh | sh
~~~

若环境不能联网，则应准备 Python 3.12 或使用 `--wheel-dir` 上传专有 SDK wheel。

deploy.sh 会完成以下工作：

1. 以当前 Git 提交为版本打包（不会上传 .git、日志、PID、虚拟环境或本地配置）；
2. 通过 SSH 上传到目标设备的临时目录；
3. 在目标设备运行 install.sh；
4. 默认创建项目专用 .venv 并安装 requirements.txt；
5. 可选安装 Ubuntu 的 Python、FFmpeg、V4L2 工具；
6. 保留目标设备已有的 config/taccap.env；
7. 可选安装并启动用户级 systemd 服务。

部署脚本使用同一个 SSH 复用连接，因此密码登录通常只输入一次。长期使用建议先运行
`ssh-copy-id user@NEW_DEVICE_IP` 配置 SSH 密钥。

默认安装目录是目标用户的 ~/taccap-websocket。如果需要固定到其他目录：

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --install-dir /home/user/taccap-websocket \
  --install-system-deps \
  --enable-systemd
~~~

如果目标设备已经准备好所有 Python 依赖，可以跳过虚拟环境安装：

~~~bash
./deploy.sh user@NEW_DEVICE_IP --no-deps --enable-systemd
~~~

如果专有 SDK wheel 不在 Python 包索引中，可以从本机一起上传 wheel 目录：

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --wheel-dir /home/xense/sdk-wheels \
  --install-system-deps \
  --enable-systemd
~~~

wheel 目录至少应包含 xensesdk 和 taccap-gripper 的兼容 wheel；其余公开依赖仍从
`requirements.txt` 安装。

### 方式二：把仓库复制到目标设备后本地安装

~~~bash
scp -r /home/xense/tron2/taccap-websocket user@NEW_DEVICE_IP:~/
ssh user@NEW_DEVICE_IP
cd ~/taccap-websocket
./install.sh --with-deps --install-system-deps --enable-systemd
~~~

install.sh 也支持只安装、不启动：

~~~bash
./install.sh --with-deps --no-start
~~~

目标设备需要 Python 3.10 或更新版本。Ubuntu 20.04 的系统 Python 通常是 3.8；
也可以显式指定已经安装好的 Python 3.12：

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --python /home/user/py312/bin/python \
  --install-system-deps \
  --enable-systemd
~~~

### 专有 SDK 依赖

requirements.txt 包含 xensesdk 和 taccap-gripper。如果目标设备无法从当前
Python 包索引下载这两个专有包，需要先把对应 wheel 拷贝到目标设备并安装，或者
让 config/taccap.env 的 TACCAP_PYTHON 指向已经装好 SDK 的 Python：

~~~bash
python3 -m pip install /path/to/xensesdk-*.whl
python3 -m pip install /path/to/taccap_gripper-*.whl
~~~

然后执行：

~~~bash
./install.sh --no-start
~~~

安装脚本不会覆盖已有的 config/taccap.env。设备差异应只写在这个文件中，不要写回
server.py。

服务默认自动识别左右夹爪和六路相机。如果某批硬件的序列号命名规则不同，可复制
config/devices.json.example 为 config/devices.json，填写目标设备的稳定 by-id 路径，
并在 config/taccap.env 中启用：

~~~bash
TACCAP_DEVICE_CONFIG=config/devices.json
~~~

devices.json 同样不会被部署覆盖或提交到 Git。

## 配置

首次安装会从 config/taccap.env.example 创建 config/taccap.env。常用配置：

~~~bash
TACCAP_BIND_HOST=0.0.0.0
TACCAP_PORT=8765
TACCAP_FFMPEG=/usr/bin/ffmpeg
TACCAP_PYTHON=/home/user/taccap-websocket/.venv/bin/python
TACCAP_ENV_SCRIPT=
TACCAP_LOG_DIR=.log
~~~

服务默认监听目标设备的所有网卡，不再要求 SSH 端口转发。本机可直接使用目标设备
IP，例如：

~~~bash
curl http://10.192.1.4:8765/api/health
curl http://10.192.1.4:8765/api/grippers
curl http://10.192.1.4:8765/api/cameras
curl -o left_wrist.jpg http://10.192.1.4:8765/camera/left_wrist.jpg
~~~

命令行客户端同样直接填写设备地址：

~~~bash
python3 client.py --base http://10.192.1.4:8765 health
~~~

LeRobot 的远程夹爪配置使用相同地址：

~~~yaml
gripper:
  type: taccap_follower
  remote_base_url: http://10.192.1.4:8765
  auto_discover_cameras: true
  remote_auto_enable: false
~~~

该端口同时包含夹爪电机控制接口，目前没有身份认证。只应运行在可信机器人局域网，
不要映射到公网；生产环境建议通过主机防火墙只允许控制电脑访问 8765 端口。

## 启停、检查和日志

不使用 systemd 时：

~~~bash
cd ~/taccap-websocket
./taccap.sh start
./taccap.sh status
./taccap.sh health
./taccap.sh stop
./taccap.sh logs
~~~

每次启动都会在 `.log/` 生成一个带日期、时间和进程号的独立日志，例如：

~~~text
.log/taccap_20260907-153012_12345.log
~~~

`.log/latest.log` 始终指向最近一次启动的日志。部署时不会覆盖或上传历史日志。

使用用户级 systemd 时：

~~~bash
systemctl --user status taccap-websocket.service
systemctl --user restart taccap-websocket.service
journalctl --user -u taccap-websocket.service -f
~~~

安装后建议先运行：

~~~bash
./taccap.sh doctor
~~~

它会检查 Python、FFmpeg、两个 SDK、串口 by-id 和 V4L2 设备。doctor 命令报告
没有相机或串口时，应先检查 USB 供电、USB2 拓扑和用户的设备访问权限。

## 发布和回滚建议

每次改动先在本机提交并验证：

~~~bash
git add .
git commit -m "feat: ..."
git push
~~~

再从该提交部署到目标设备。deploy.sh 会在目标副本写入 .release，便于确认运行
版本。出现问题时，回到本机上一个提交重新部署即可：

~~~bash
git checkout <known-good-commit>
./deploy.sh user@NEW_DEVICE_IP --enable-systemd
~~~

开发完成后再切回主分支或新建发布分支。目标设备上的配置、日志和虚拟环境不应提交
到 Git。

## License

本项目采用 [MIT License](LICENSE)。
