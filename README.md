# Agnes 生图工具

一个基于 Agnes 云服务的桌面生图工具，支持**文字生图**与**图片生图（图生图）**，带命令行与图形界面两套入口，并提供任务记录、断点重试与本地校验能力。

本项目在原有工具基础上完成了两件事：

1. 排查「图生图提示服务异常」的问题，定位并修复**错误信息被吞掉**这一根因；
2. 新增**个人 API Key 配置能力**，让工具不再只能借用系统里的对话模型 Key。

> 详细排查过程见 [docs/01-问题排查报告.md](docs/01-问题排查报告.md)，接口实测数据见 [docs/03-接口实测记录.md](docs/03-接口实测记录.md)。

---

## 目录结构

```
agnes-image-tool/
├── README.md                     # 本文件：项目概览与快速开始
├── CHANGELOG.md                  # 版本变更记录
├── .gitignore                    # 已排除 api_config.json 与生成产物
├── docs/
│   ├── 01-问题排查报告.md         # 「服务异常」的排查过程与结论
│   ├── 02-API-Key-配置说明.md     # Key 的获取、优先级、保存与校验
│   ├── 03-接口实测记录.md         # 真实调用记录与关键发现
│   └── 04-改造说明.md             # 本次代码改动的完整清单
└── src/
    ├── agnes_image.py            # 核心模块 + 命令行入口
    ├── agnes_gui.pyw             # 图形界面（tkinter）
    ├── styles.json               # 风格库与默认参数
    ├── 启动生图工具.bat           # Windows 启动脚本（原文件名：生图工具.bat）
    └── api_config.json           # 运行后生成，存放个人 Key，不入库
```

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 文字生图 / 图片生图 | 图生图以 base64 内嵌参考图提交，不落地临时文件 |
| 风格库 | 通过 `styles.json` 配置，界面可热刷新 |
| 参数校验 | 尺寸、比例、数量、超时、图片体积与像素数本地预校验 |
| 结果校验 | 魔数 + 尺寸解析校验，拒绝损坏或伪图片写入 |
| 任务记录 | 每个任务落盘为 JSON，支持刷新、复用参数、重试未完成项 |
| 并发保护 | 进程内锁 + 文件锁，避免多窗口同时提交 |
| **个人 API Key** | 界面填写、保存、测试连接、清除；保存后持续生效 |
| **可读的错误** | HTTP 错误附带上游返回的真实原因 |

## 环境要求

- Windows（界面依赖 `tkinter`，"打开目录/文件" 依赖 `os.startfile`）
- Python 3.9 及以上
- 第三方库：`Pillow`（图片预览与校验增强；缺失时界面会提示）
- 一个可用的 Agnes API Key

> 实测提示：仅有 Python 解释器不够，必须是**带 tkinter 且装有 Pillow** 的那个解释器。

若机器上存在多个 Python，`where pythonw` 可能命中不含 Pillow 的那个，此时界面会提示「缺少 Pillow 图片库」。解决办法是先设置环境变量再启动：

```bat
set "PYTHONW_EXE=C:\Path\To\pythonw.exe"
启动生图工具.bat
```

可用 `python -c "import tkinter, PIL; print('ok')"` 确认某个解释器是否满足要求。

## 快速开始

### 图形界面

双击 `src/启动生图工具.bat`，或手动执行：

```bash
python src/agnes_gui.pyw
```

### 命令行

```bash
# 文字生图
python src/agnes_image.py "一只在窗台晒太阳的橘猫" --size 1K --ratio 16:9

# 图片生图
python src/agnes_image.py "把画面转成水彩风格" --image ./reference.png --size 1K --ratio 3:2

# 查看风格库
python src/agnes_image.py --list-styles

# 查看历史任务
python src/agnes_image.py --history

# 重试未完成项（只处理失败项，已拿到结果地址的仅重下）
python src/agnes_image.py --retry <JOB_ID>
```

## 配置 API Key（重要）

工具**必须**调用 Agnes 云接口，因此**必须**配置 API Key。Key 的取值优先级为：

1. **工具保存的 Key** —— 界面填写并保存，或 `python src/agnes_image.py --set-key <KEY>`，存于 `src/api_config.json`
2. **环境变量** `AGNES_API_KEY`
3. **回退**：系统模型配置中 Agnes 对话模型的 Key

未配置个人 Key 时，工具会回退到第 3 项；一旦保存了个人 Key，就**一直使用它，直到再次修改**。

```bash
# 保存 / 覆盖
python src/agnes_image.py --set-key sk-<YOUR_KEY>

# 查看当前生效的来源（Key 以掩码显示）
python src/agnes_image.py --show-key

# 删除已保存的 Key，回退到环境变量或系统配置
python src/agnes_image.py --clear-key
```

完整说明见 [docs/02-API-Key-配置说明.md](docs/02-API-Key-配置说明.md)。

## 安全说明

- `src/api_config.json` 已被 `.gitignore` 排除，**请勿将保存了 Key 的文件提交到仓库**。
- 任务记录（默认在输出目录的 `records/` 下）只保存提示词与参考图路径，**不保存 Key**。
- 界面与 `--show-key` 均以掩码形式展示 Key。
- 参考图与提示词会发送到你所配置 Key 对应的 Agnes 云服务，请确认拥有使用权限。

## 默认限制

| 项 | 取值 |
| --- | --- |
| 尺寸档位 | 1K / 2K / 3K / 4K |
| 比例 | 1:1、3:4、4:3、16:9、9:16、2:3、3:2、21:9 |
| 单次数量 | 1–4（逐张提交，可能按张计费） |
| 超时 | 30–600 秒 |
| 参考图 | ≤ 10 MB，≤ 4000 万像素，PNG / JPEG / WebP 静态图 |
| 提示词 | ≤ 8000 字符 |
