# API Key 配置说明

## 一、为什么必须配置

本工具不包含任何本地生图模型，也不存在离线或模拟生成后端。所有生成请求都会发往 Agnes 云接口：

```
POST https://apihub.agnes-ai.com/v1/images/generations
Authorization: Bearer <YOUR_KEY>
```

因此**没有可用的 API Key 就无法生成图片**。

## 二、Key 的三种来源与优先级

工具按以下顺序取用 Key，**命中即止**：

| 优先级 | 来源 | 存储位置 | 适用场景 |
| --- | --- | --- | --- |
| 1 | 工具保存的个人 Key | `src/api_config.json` | 推荐。保存后一直生效，直到再次修改 |
| 2 | 环境变量 `AGNES_API_KEY` | 系统环境变量 | 临时覆盖、CI 或脚本调用 |
| 3 | 系统模型配置中 Agnes 对话模型的 Key | 用户配置目录下的 `models.json` | 未设置个人 Key 时的回退 |

> 关键点：**只要保存过个人 Key，后续所有生成都使用它**；清除保存的 Key 后，才会回退到环境变量或系统配置。

## 三、图形界面操作

界面顶部为「Agnes API 设置」栏：

| 控件 | 作用 |
| --- | --- |
| API Key 输入框 | 默认以掩码显示，勾选「显示」可查看明文 |
| 保存 | 写入 `api_config.json`，后续生成一直使用 |
| 测试连接 | 用当前输入框（或已保存）的 Key 做一次免费校验 |
| 清除 | 删除保存的 Key，回退到环境变量或系统配置 |
| 状态标签 | 显示当前生效的 Key 来源（掩码）与接口地址 |

状态标签示例：

```
Key sk-xxxx…yyyy · 来源：本工具保存的个人 Key（一直生效，直到再次修改）
接口地址：https://apihub.agnes-ai.com/v1/images/generations
```

## 四、命令行操作

```bash
# 保存或覆盖个人 Key
python src/agnes_image.py --set-key sk-<YOUR_KEY>

# 同时指定自定义接口地址（可选，必须为 HTTPS）
python src/agnes_image.py --set-key sk-<YOUR_KEY> --api-url https://example.com/v1/images/generations

# 查看当前生效来源（Key 以掩码显示）
python src/agnes_image.py --show-key

# 删除已保存的 Key
python src/agnes_image.py --clear-key
```

`--show-key` 的输出示例：

```
本工具保存的 Key：sk-xxxx…yyyy
接口地址：https://apihub.agnes-ai.com/v1/images/generations
```

## 五、测试连接的原理

直接调用生图接口并附带一个**故意非法**的 `size` 值。实测表明 Agnes **先校验鉴权、再校验参数**，因此：

| 返回码 | 含义 |
| --- | --- |
| 401 / 403 | Key 无效、过期或无权限 |
| 400（参数错误提示） | **Key 已被接受** |
| 超时 / 网络错误 | 网络不通或接口地址错误 |

该探针**不会生成任何图片，也不产生计费**。

> 为什么不用 `GET /v1/models` 校验：实测该端点鉴权不严格，对多个无效字符串仍返回 200，无法用于判断 Key 是否有效。

## 六、存储与安全

- Key 明文保存在 `src/api_config.json`，该文件已被 `.gitignore` 排除。
- **请勿将保存了 Key 的 `api_config.json` 提交到仓库或分享给他人。**
- 任务记录中不含 Key，界面与命令行均只展示掩码。
- 写入采用「临时文件 + 原子替换」，避免写入中断产生损坏配置。

## 七、常见问题

**Q：没有自己的 Key，工具还能用吗？**
可以，但会回退使用系统模型配置中 Agnes 对话模型的 Key。该 Key 的额度与权限不由你掌控，出现限流或失效时排查困难，建议配置个人 Key。

**Q：保存 Key 后想临时换一个？**
直接在输入框填入新 Key 并保存即可覆盖；或点击「清除」回退。

**Q：提示「Key 校验失败：HTTP 401」怎么办？**
确认 Key 完整、未包含空格、未过期，且具备生图模型的调用权限。注意输入框会过滤空格与未解析的 `${}` 占位符。

**Q：接口地址可以改吗？**
可以，通过 `--api-url` 传入，必须为 HTTPS 地址，且不含用户名、密码或片段标识符。
