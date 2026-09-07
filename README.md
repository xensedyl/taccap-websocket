# `.4` TacCap remote service

该目录是运行在 `guest@10.192.1.4` 上的独立 TacCap 项目。服务不依赖 ROS，
在 `.4` 本地打开两只 TacCap 夹爪、四个触觉传感器和两个腕部 UVC 相机，
再通过一个 loopback HTTP 接口向本机 LeRobot 提供：

- 两侧夹爪状态、使能、位置控制和安全心跳租约；
- `left_wrist`、`right_wrist`；
- `left_tactile_left/right`、`right_tactile_left/right` 六路 30 Hz MJPEG。

触觉源使用 `xensesdk.Sensor.OutputType.Rectify`，SDK 参数为
`rectify_size=(400, 700)`（宽、高），在 `.4` 上每路只编码一次 JPEG，然后由
HTTP 客户端共享最新帧。SDK 返回数组通常是 `(700, 400, 3)`；本机
`lerobot-tron2` 再按旧数据集约定转成 `(400, 700, 3)`。设备编号在启动时通过 TacCap 固件 SN、V4L2
`by-id` 和 sysfs USB hub 自动发现，不依赖重启后可能变化的 `/dev/videoN`。

## 在 `.4` 启停

```bash
cd ~/taccap
./start.sh
./status.sh
./stop.sh
```

日志在 `~/taccap/taccap.log`。启动脚本会 source `~/activate_taccap312`，
确保 Python 3.12、TacCap SDK 和匹配的动态库一起使用。

服务只监听 `.4` 的 `127.0.0.1:8765`，不会把电机控制端口暴露到局域网。
本机访问前建立 SSH 隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 guest@10.192.1.4
```

然后本机可访问 `http://127.0.0.1:8765/` 或：

```bash
curl http://127.0.0.1:8765/api/grippers
curl http://127.0.0.1:8765/api/cameras
curl -o left_wrist.jpg http://127.0.0.1:8765/camera/left_wrist.jpg
```

触觉流暴露 `.4` 上 `xensesdk.Sensor.OutputType.Rectify` 的标定矫正 GSPS 帧。
`rectify_size=(400, 700)`，SDK 数组通常为 `(700, 400, 3)`，每路以稳定 30 Hz
采样并只编码一次 JPEG。`/api/health` 和 `/api/cameras` 会报告 Rectify 模式、
矫正尺寸及实际生产者速率；如果 USB2 等时带宽不足，状态中会明确报告 `ENOSPC`。

## 控制安全

服务启动时电机保持断使能；位置命令前必须显式 `POST /api/grippers/<side>/enable`。
心跳停止约 5 秒会自动断使能；完全闭合（位置 `<=0.05`）还要求
`confirm_close: true`。位置 `0` 是闭合，`1` 是张开。默认速度上限为
`0.60 rad/s`，力矩上限为 `0.25 Nm`。

## 本机 LeRobot 配置

本机 `tron2_rt` 使用嵌套 TacCap 配置切换到远端：

```yaml
gripper:
  type: taccap_follower
  remote_base_url: http://127.0.0.1:8765
  auto_discover_cameras: true
  remote_auto_enable: false
```

本机侧不会调用 `find_left()`/`find_right()`，也不会打开本机的串口、UVC 或
`xensesdk`；六路图像和夹爪数据全部来自 `.4`。若希望 LeRobot 连接时自动
使能电机，明确设置 `remote_auto_enable: true`，并确认工作区安全。

## License

本项目采用 [MIT License](LICENSE)。
