# SDK 来源与离线发布

发布包在有网络且具备 C++ 编译器、OpenCV/spdlog 开发包的开发机上生成，目标设备不需要网络、
pip、uv、Git 或编译器。由于 `xense.taccap` 包含原生扩展，构建机的 glibc 版本不能
高于目标设备；Ubuntu 20.04 目标应使用 Ubuntu 20.04/glibc 2.31 构建机。
构建 C++ 依赖在非系统前缀时，脚本会自动使用已激活 conda/mamba 环境的前缀；也
可设置 `TACCAP_CPP_PREFIX` 指向该前缀，脚本会把它加入 CMake 的搜索路径。
如果构建机无法由 `uv` 下载 portable Python，可用 `TACCAP_BUNDLE_RUNTIME` 指定已
准备好的 Python 3.12 runtime 目录。

- `xensesdk` 通过包名安装（默认 `xensesdk`，可用 `--xensesdk` 指定版本或内部
  index）；
- `taccap-gripper` 直接从源码目录或官方仓库构建安装：
  `https://github.com/XenseRobotics-AI/TacCap-Gripper.git`；
- 构建完成后只把已安装的 `site-packages.tar.gz` 放进 bundle；源码构建产生的
  临时 wheel 不会进入 Git 或发布包。

示例：

```bash
./bundle.sh \
  --xensesdk 'xensesdk==2.1.3' \
  --taccap-source /home/xense/tron2/TacCap-Gripper
```

或者让脚本从 Git URL 临时 clone：

```bash
./bundle.sh \
  --taccap-source https://github.com/XenseRobotics-AI/TacCap-Gripper.git
```

目标设备收到的 bundle 只包含 portable Python、安装后的 site-packages 归档和
校验清单，不包含任何 `.whl`。安装前会严格验证两个原生 SDK 的导入；ABI 不兼容
会终止部署，不会回退到目标机已有的 Python 或 SDK。
