# 太空发射（Space Launch）

<p align="center">
  <img src="assets/icon.jpg" alt="星河拓航工作室 Galaxy Exploration Studio" width="180">
</p>

<p align="center">
  由 <b>星河拓航工作室</b>（Galaxy Exploration Studio）开发与维护
</p>

查询全球**下一次**以及未来即将进行的太空发射任务。

数据来源：[Launch Library 2 API](https://thespacedevs.com/llapi)（The Space Devs，免费开放）。

## 功能

| 入口 | 名称 | 说明 |
| --- | --- | --- |
| `next_launch` | 下一次太空发射 | 返回下一次发射的任务名、火箭、服务商、本地时间、倒计时、发射场与任务简介 |
| `upcoming_launches` | 即将进行的发射列表 | 返回未来 1–20 次发射，可按服务商 / 火箭 / 发射场筛选 |
| `launch_stats` | 发射数据概览 | 返回即将进行的发射总数与当前 API 配置 |

同时注册了两个供猫娘在对话中主动调用的 LLM 工具：

- `get_next_space_launch` —— 回答“下一次火箭发射是什么时候”
- `list_upcoming_space_launches` —— 回答“接下来有哪些发射任务”

## 目录结构

```
space_launch/
├── plugin.toml            # 插件清单（ID、入口类、默认业务配置）
├── config.example.toml    # 用户配置模板
├── pyproject.toml         # 项目元数据（供 neko-plugin CLI 使用）
├── __init__.py            # 插件实现
├── README.md
├── LICENSE                # Apache License 2.0
├── ruff.toml              # 代码检查配置
├── assets/
│   └── icon.jpg           # 星河拓航工作室图标
├── tests/
│   └── test_smoke.py
└── .github/workflows/     # 标准 CI（verify.yml / release.yml）
```

## 配置

首次运行时，N.E.K.O 会把 `config.example.toml` 复制到用户数据目录的
`config/plugin.toml`。可配置项位于 `[space_launch]` 段：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_base_url` | `https://ll.thespacedevs.com/2.3.0/` | LL2 接口基础地址 |
| `timeout_seconds` | `15` | 单次请求超时（3–60 秒） |
| `default_limit` | `5` | 列表默认返回条数 |
| `include_descriptions` | `true` | 是否附带任务描述 |
| `cache_ttl_seconds` | `300` | 响应内存缓存时间 |

### 关于请求频率

Launch Library 2 的免费额度约为**每小时 15 次请求**。插件因此内置了带 TTL 的
内存缓存，默认 5 分钟内相同查询不会重复打接口。如果你需要更高频率，请在
[The Space Devs 的 Patreon](https://thespacedevs.com/supportus) 申请更高额度，
并把 `cache_ttl_seconds` 调小（不建议低于 60）。

## 入口参数

`next_launch`：

```json
{ "provider": "SpaceX", "rocket": "Falcon 9", "location": "Cape Canaveral" }
```

三个参数都是可选的关键字过滤，留空表示不限制。筛选在插件本地完成，
因此不会因为额外的查询参数增加 API 调用。

`upcoming_launches`：

```json
{ "limit": 5, "provider": "", "rocket": "", "location": "" }
```

## 返回示例

`next_launch` 的 `summary` 字段是可直接朗读的中文描述：

> 下一次太空发射是「Falcon 9 Block 5 | USSF-259」，使用 Falcon 9 Block 5 火箭，
> 由 SpaceX 执行，计划于 2026-09-17 09:00:00 中国标准时间发射，发射场：
> Vandenberg SFB, CA, USA（还有 5 小时 3 分钟）。当前状态：Go for Launch，
> 任务类型：Government/Top Secret。

结构化的 `launch` 字段包含：`name`、`rocket`、`provider`、`mission`、
`mission_type`、`orbit`、`pad`、`location`、`country`、`net_utc`、`net_local`、
`window_start_local`、`window_end_local`、`countdown`、`countdown_seconds`、
`status`、`webcast_live`、`url`、`image`。

## 开发与测试

```bash
uv run neko-plugin check space_launch
uv run pytest plugin/plugins/space_launch/tests -q
```

在 N.E.K.O. 的插件页面刷新列表后启动插件，切换到「入口点」即可触发
**下一次太空发射**。

## 关于星河拓航工作室

本插件由 **星河拓航工作室**（**Galaxy Exploration Studio**）开发与维护。

星河拓航工作室是一个由来自五湖四海的航天爱好者组成的非正式线上航天科普组织，
成员多为在校学生。我们希望通过有趣、可靠的方式，让更多人了解真实的航天发射任务。

- 官方网站：<https://xhth.top/>
- B 站主页：<https://space.bilibili.com/3546949529635067>

欢迎航天爱好者加入交流，也欢迎反馈插件的问题与建议。

## 说明

- 数据版权归 [The Space Devs](https://thespacedevs.com/) 所有，本插件仅做查询展示。
- 发射时间（`net`）会随任务动态调整，插件展示的是查询时刻 API 返回的最新值。
- `launches/upcoming/` 端点偶尔会返回刚刚发射完、状态尚未刷新的任务（例如已标记为
  `Launch Successful` 却仍排在列表开头的记录）。插件会过滤掉这些条目，确保
  「下一次太空发射」指向的确实是还没发射的任务；发射时间待定的任务排在列表末尾。

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)（GPL-3.0）许可。

你可以自由地使用、修改和分发本插件；但**如果你分发修改后的版本，必须以同样的
GPL-3.0 协议开源你的修改**，并保留原有的版权声明。

```
Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
```

本程序为自由软件，但**不提供任何担保**。完整条款见 [LICENSE](LICENSE)。
