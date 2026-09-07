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

## 迁移和离线部署

本机 Git 仓库是源代码来源，目标设备只保存运行副本。`.4` 不能联网时，不能在目标机
执行 `uv`、`pip install`、`--bootstrap-python` 或 `--install-system-deps`；这些动作会
尝试访问网络。正确做法是在有网络的本机生成一次离线 bundle，再通过 SSH 上传。

### 1. 生成离线 bundle（本机执行）

仓库已经包含按 `.4`（Ubuntu 20.04 x86_64、OpenCV 4.2）构建的 TacCap-Gripper
0.1.9 wheel，以及 xensesdk wheel。执行：

~~~bash
cd /home/xense/tron2/taccap-websocket
./bundle_offline.sh \
  --xensesdk-wheel vendor/wheels/xensesdk-2.1.3-cp312-cp312-manylinux_2_31_x86_64.whl \
  --taccap-wheel vendor/wheels/taccap_gripper-0.1.9-cp312-cp312-linux_x86_64.whl
~~~

脚本会在本机下载一个可复制的 Python 3.12、所有公开依赖和 `cypack 0.1.2`，生成
被 Git 忽略的 `offline/` 目录（约 270 MB）。目标机不需要 Python、pip、uv 或网络。
`offline/manifest.txt` 记录 bundle 的提交和每个 wheel 的 SHA256。

如果只从 Git 克隆而没有 `offline/`，先在能联网的开发机运行上面的命令；离线 bundle
是平台相关的发布物，不建议把整套 Python 运行时提交进 Git。

### 2. SSH 一键部署到新设备

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --offline-dir /home/xense/tron2/taccap-websocket/offline \
  --enable-systemd
~~~

部署脚本会上传源码和 bundle，在目标机的 `.runtime/python` 中安装自带 Python，使用
`pip --no-index` 从本地 wheel 目录创建 `.venv`，然后启动服务。目标机不需要旧设备的
`/home/guest/py312/bin/python` 或 `activate_taccap312`。如果先验证而不启动：

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --offline-dir /home/xense/tron2/taccap-websocket/offline \
  --no-start
~~~

目标设备需要预先具备系统运行库和设备访问权限：`/usr/bin/ffmpeg`、`curl`、USB/UVC
驱动，以及与 `.4` 相同的 Ubuntu 20.04 OpenCV 4.2 ABI。离线安装器不会调用 apt；若
这些系统组件缺失，应在设备联网时安装，或由设备镜像/离线 apt 包预先提供。当前 `.4`
已经满足这些条件。

部署脚本使用同一个 SSH 复用连接，密码通常只输入一次；长期使用建议配置 SSH 公钥：
`ssh-copy-id user@NEW_DEVICE_IP`。

### 3. 目标机本地安装（可选）

也可以先把仓库和 `offline/` 目录复制到目标机，再执行：

~~~bash
scp -r /home/xense/tron2/taccap-websocket user@NEW_DEVICE_IP:~/
ssh user@NEW_DEVICE_IP
cd ~/taccap-websocket
./install.sh --offline-dir ./offline --with-deps --enable-systemd
~~~

`install.sh --offline-dir ... --with-deps --no-start` 可只安装不启动。安装脚本会保留
已有的 `config/taccap.env`、日志和设备配置，不会把设备特定串口路径写回源码。

### 有网络的目标机

如果目标机能联网，也可以使用 `--bootstrap-python` 和 `--install-system-deps`：

~~~bash
./deploy.sh user@NEW_DEVICE_IP \
  --bootstrap-python --install-system-deps --enable-systemd
~~~

该模式与离线模式互斥；新设备迁移优先使用上面的 bundle 方式。

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
