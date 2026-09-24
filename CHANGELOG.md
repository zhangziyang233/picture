# 变更记录

## [Unreleased]

### 新增

- 个人 API Key 配置：图形界面「Agnes API 设置」栏，支持填写、保存、测试连接与清除。
- `api_config.json`：工具自有凭据存储，保存后持续生效，直到再次修改。
- `test_api_key()`：无成本 Key 校验（提交非法 `size`，依据 401 / 400 判定），不生成图片、不计费。
- 命令行参数 `--set-key` / `--api-url` / `--clear-key` / `--show-key`。
- `_server_message()`：解析上游 `error.message` 并附加到错误提示中。
- 仓库文档：`README.md` 与 `docs/` 下四篇说明。
- `.gitignore`：排除 `src/api_config.json`、生成产物与缓存。

### 修改

- `load_model()` 的 Key 取用顺序调整为：工具保存的 Key → `AGNES_API_KEY` → 系统模型配置中的 Agnes Key。
- HTTP 错误提示由纯状态码映射改为附带服务端返回原因。
- 启动脚本改为通用解释器探测，移除本机专属路径；文件更名为 `启动生图工具.bat`。
- 界面布局调整为顶部 API 栏 + 主体 + 底部状态三行。

### 未变更

- 生图请求体结构与参数位置。
- 任务记录格式、重试语义与并发保护。
- 图片校验规则与体积、像素上限。
