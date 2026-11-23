# WOWS 港口开发部

Windows 上的《World of Warships》录像复盘与战绩查询工具。导入 `.wowsreplay` 后可以回看地图与阵容、查看结算、运行本地开火和走位模型，并生成带小地图的 AI 报告。

## 安装与更新

在 [GitHub Releases](https://github.com/NHblock714/wows-training/releases/latest) 下载 Windows x64 安装包。安装时可选择目录和桌面快捷方式。首次启动会创建本地资料库，手动公开战绩查询默认启用，AI 服务由使用者在「AI 连接」配置。

客户端默认在启动后查询 GitHub 的正式发行版，发现新版会下载并校验 SHA-256。在「客户端设置 → 版本更新」可调整开关、手动检查及打开下载目录。下载完成后由使用者启动安装包。

## 分析报告

单局报告结合连续小地图、敌我阵容、实装配件、起火进水、承伤和结算数据。位置优先使用录像时点、A–J／1–10 地图格和占点相对方位，例如「D6，B 点西侧」。已有录像可以重新预览并生成报告。

## 从源代码构建

在 Windows 上使用 Python 3.13，安装 `requirements-desktop.txt` 中的依赖。克隆仓库后，从 Releases 下载同版本的 `win64-distribution.zip`。用发布页给出的归档 SHA-256 还原运行资源，再启动源码或使用 PyInstaller 打包。

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.venv\Scripts\python.exe tools/restore_runtime_assets.py <便携包路径> --sha256 <发布页中的归档SHA256>
.venv\Scripts\python.exe desktop_launcher.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm WOWSTraining.spec
```

## 鸣谢

- [langyo / WoWSP 源码](https://github.com/langyo/wowsp/tree/23e41b1a77035e194e4ec09476766fbd54eb5d11) 提供本地模型所用的录像解码、特征提取和开火、走位评分实现。配套权重来自 [WoWSP-D1 模型仓库](https://huggingface.co/langyo/wowsp-d1/tree/0834f0638deba50d46ff610734cedcfa0c8b89b7)。
- [Monstrofil / replay_unpack](https://github.com/Monstrofil/replay_unpack) 提供回放解析基础。[landaire / wows-toolkit](https://github.com/landaire/wows-toolkit/tree/9ab2388002f3075d0bc85c2d7c879679f7f55383) 提供小地图和录像语义参考。
- [Wargaming Public API](https://developers.wargaming.net/) 提供公开战绩查询接口。[Ship Tool](https://shiptool.st/stats) 提供区服同舰参考数据。

本项目为独立的社区工具，由 [NHblock](https://github.com/NHblock714) 开发。相关游戏素材归各自权利人所有，来源和许可见 [第三方声明](THIRD_PARTY_NOTICES.md) 及 [模型声明](decision_models/NOTICE.md)。
