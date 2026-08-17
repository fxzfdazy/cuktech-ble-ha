# 项目修改记录

这份文档记录相对于原版项目的全部修改，供日后维护和升级时对照。

改动编号是固定的，提交记录和其他文档会直接引用编号。新增改动时编号递增，追加到对应分组的小节里即可。

分组规则：前两部分动的都是原版代码——修 bug 或调行为；第三部分是原版没有的东西（新功能及其配套）。

改动范围：全部集中在 BLE Server（`ble_server/`，含 Python 服务端和 Web 页面），`esp32_ble/` 固件未做个人修改。

## 总览

| # | 分组 | 说明 |
|---|------|------|
| 1 | 原版 bug 修复 | 修复断连后程序不察觉、永不重连的 bug |
| 6 | 原版 bug 修复 | 修复服务发现未完成导致认证失败循环 |
| 31 | 原版 bug 修复 | 息屏时间协议值映射修正 |
| 34 | 原版 bug 修复 | 修复 GET 等待期间推送帧被吞 |
| 2 | 原版机制调整 | 断开时只断充电器，不再重置整个蓝牙适配器 |
| 7 | 原版机制调整 | start_notify 失败重试 3 次 |
| 8 | 原版机制调整 | 断开时清理 BlueZ GATT 缓存 |
| 10 | 原版机制调整 | keepalive 恢复 GATT read 探活 |
| 11 | 原版机制调整 | 60s 探活去掉主动 read，避免误判 |
| 12 | 原版机制调整 | 解密失败阈值 3 恢复到 10 |
| 13 | 原版机制调整 | 多帧解密失败不再触发重连 |
| 14 | 原版机制调整 | PIID 读取恢复 100ms 间隔 |
| 32 | 原版机制调整 | 启动后主动补读端口状态 |
| 33 | 原版机制调整 | 启动时协议推断改用启发式 |
| 35 | 原版机制调整 | 插线后协议快照失效窗口 |
| 3 | 新增·构建 | Docker 构建换国内镜像源 |
| 9 | 新增·构建 | 装 tzdata 修日志时区 |
| 42 | 新增·构建 | 静态资源改内容哈希自动版本化，去掉手动递增 |
| 4 | 新增·日志 | 后端记录 BLE 连接生命周期事件 |
| 5 | 新增·日志 | WebUI 查看蓝牙日志 |
| 15 | 新增·充电记录 | 充电会话记录、历史、详情整套功能 |
| 17 | 新增·充电记录 | 明细点接口限流（一天窗口 + 500 点抽稀） |
| 18 | 新增·充电记录 | 长时供电会话凌晨自动结转 |
| 21 | 新增·充电记录 | 起止阈值 C1/C2 分开设置 |
| 24 | 新增·充电记录 | 截止阈值持续时长可配置 |
| 25 | 新增·充电记录 | 修复端口归零后会话卡死不截止 |
| 26 | 新增·充电记录 | 截止判定移到定时器循环开头统一执行 |
| 30 | 新增·充电记录 | 清空记录时保留充电中的会话 |
| 36 | 新增·充电记录 | 拔线容错：等待期内重插延续同一会话，可配置开关与时长 |
| 38 | 新增·充电记录 | 会话协议标签按采样点众数在结束时回写，实时卡片显示当前协议 |
| 16 | 新增·前端 | 迷你图坐标轴、悬停详情、占位提示 |
| 19 | 新增·前端 | 文案、Wh 精度、时长格式、跨天时间 |
| 20 | 新增·前端 | 图表布局微调 |
| 22 | 新增·前端 | HTML 缓存策略修正（no-cache + ETag） |
| 23 | 新增·前端 | 实时功率归零显示等一批小 bug |
| 27 | 新增·前端 | 时长显示精确到秒 |
| 28 | 新增·前端 | 时间显示统一 HH:MM:SS |
| 29 | 新增·前端 | 显示精度开关（秒/分） |
| 37 | 新增·前端 | 修复端口开关按钮有时跳动（DOM 重建与慢响应覆盖） |
| 39 | 新增·前端 | 最高电压统计计入空载挂载态端口 |
| 40 | 新增·前端 | 采样精度行排版与其它行对齐 |
| 41 | 新增·前端 | 修复开关请求返回后旧推送仍闪回 |

---

## 第一部分：原版 bug 修复

### 改动 1：修复断连检测失效

文件：`ble_server/ble_manager.py`

主循环里 `_refresh_settings()` 即使 15 个 PIID 全部读取失败也会正常返回，返回后 `last_notify = now` 照样执行，把"最后收到推送"的时间刷成了当前时间，结果 60 秒探活块永远不触发。链路早就断了，程序还以为连接正常，永不重连。当时的表现是日志每分钟一条 `All 15 PIID reads failed`，持续一整天，没有任何重连动作，BlueZ 侧设备对象都已经被清掉了。

修复分三处：

- 删掉 timeout 分支里那行错误的 `last_notify` 刷新，只有真正收到 BLE 推送才更新它
- `_fetch_settings` 改为返回布尔值（是否至少部分成功），`_refresh_settings` 透传
- 新增 `_refresh_fail_count` 计数，连续 5 次全失败（约 5 分钟）抛 `ConnectionError` 触发重连，`_connect` 成功后清零

阈值定在 5 而不是 3，是因为密集 PIID 读取偶发全部失败的情况存在，拉长到 5 分钟能避免误重连。重连退避策略没动。

### 改动 6：修复服务发现未完成导致认证失败循环

文件：`ble_server/src/cuktech_ble/controller.py`

`client.connect()` 返回后 BlueZ 的服务发现可能还没做完（快速重连时复用了失效缓存），后续 `start_notify` / `write_gatt_char` 报 "Service Discovery has not been performed yet"，认证握手异常，设备状态机不同步，decrypt 收到 1 字节错误响应，然后重连，如此循环。实际抓到过连接 34 秒后仍在报这个错的日志。

现在 `connect()` 里显式 `await get_services()`，15 秒超时。超时或失败就断开连接，让外层退避重试，不带着残缺连接往下走。服务发现正常 2-5 秒，15 秒上限足够。

### 改动 31：息屏时间映射修正

文件：`web/static/app.js` + `phone.js` + `ha_integration` 的 const.py / select.py

PIID 6 的实际含义是抓包实测出来的，和原代码里的映射整体错位：

| 协议值 | 实际含义 |
|--------|---------|
| 1 | 5 分钟 |
| 2 | 10 分钟 |
| 3 | 30 分钟 |
| 4 | 常亮 |
| 5 | 1 分钟 |

协议值 0 无效，已从选项里删掉。因为值不连续（1-5，没有 0），phone.js 的循环切换改成 `SCREEN_VALUES` 数组把标签和协议值分开维护，HA 端 `SELECT_OPTION_MAP` 从同一份映射派生，两边不会再漂移。

### 改动 34：修复 GET 窗口吞推送

文件：`ble_server/src/cuktech_ble/controller.py` + `ble_manager.py`

controller 的 GET/SET 会有一段等待响应的窗口，期间到达的端口推送帧会被暂存、之后再回放。回放条件写的是 `b4 == 0x02`，而实际推送帧的 b4 是 `0x04`，条件永假，暂存的推送全部被静默丢弃。拔手机时的归零推送如果恰好落在 settings 刷新的 GET 窗口里，端口就永远卡在旧功率。

修法：controller 暴露 `on_port_push` 回调，ble_manager 注册 `_try_process_inline_frame` 让推送帧立即处理。试过修回放条件让帧走回放队列，但回放会触发同一处理链再入队、自循环空转，弃用。

---

## 第二部分：原版机制调整

### 改动 2：断开不再重置蓝牙适配器

文件：`ble_server/ble_manager.py`

`_force_disconnect_bluetooth()` 原来在断开充电器后还会 `bluetoothctl power off` + `power on` 重置整个适配器，同一根蓝牙棒上的耳机、键盘全跟着断。改成只 `disconnect` 充电器自己的 MAC，顺带删掉了没人再调用的 `_find_ble_adapter()`。适配器级别的重置留给管理员手动处理。

### 改动 7：start_notify 失败重试

文件：`ble_server/src/cuktech_ble/controller.py`

即使服务发现完成了，BlueZ 偶发的瞬态错误还是可能让 `start_notify` 失败。改成重试 3 次、间隔 1 秒，两批特征（cmd_recv/cmd_send/dev_info 和 auth_data）都适用。

### 改动 8：断开时清理 GATT 缓存

文件：`ble_server/ble_manager.py`

改动 2 删掉 power cycle 之后，BlueZ 残留的 GATT 缓存会在快速重连时让服务发现"假完成"。所以在 `disconnect` 之后追加 `bluetoothctl remove <MAC>`，清掉该设备的缓存和设备对象，下次连接做完整的服务发现。只影响这一个 MAC。cuktech 的认证走应用层 MiOT 预共享 token，不依赖 BlueZ 配对，remove 是安全的。

### 改动 10-14：对齐 1.0.3 的宽容策略

文件：`ble_server/ble_manager.py`

对比 1.0.3 版本后发现，当前版本多处把重连条件设得过敏感，正常波动就触发重连，"用几分钟就重连一次"。逐项改回 1.0.3 的行为：

| 改动 | 退化的行为 | 恢复后 |
|------|-----------|--------|
| 10 | keepalive 用无响应写，链路断了也"成功" | 恢复 GATT read 固件版本，每 10 秒一次 |
| 11 | 60 秒无推送就主动 read 探活，read 抖动即误判 | 只检查 `is_connected` |
| 12 | 解密连续失败 3 次就重连 | 阈值恢复 10 次 |
| 13 | 多帧子帧解密失败重新抛出触发重连 | 吞掉异常继续跑 |
| 14 | 15 个 PIID 连续密集读取，设备过载 | 恢复每条之间 100ms |

几点背景：

- 充电器功率稳定时不推送数据是正常的（事件驱动，见新增部分改动 25 的说明），不能拿"60 秒没推送"当断连证据，真正的断连由 keepalive read 失败加 `is_connected` 变 False 兜底
- 设备偶尔会发解不开的帧（噪声帧、控制帧、多帧子帧格式不同），成功解密会把计数清零，连续累积到 10 次才算会话密钥失步
- 100ms 间隔让 15 个 PIID 多花 1.5 秒，换来设备不积压命令

### 改动 32：启动补读端口状态

文件：`ble_server/ble_manager.py`

认证后的初始快照只包含最近活跃的端口。如果某根线在程序启动前就插着（0A 挂载态），设备不会为它推送任何东西，那个口就一直没数据。加了 `_read_initial_ports`，认证后主动 GET 端口 PIID 1-4 补齐。只补没有数据的口，不覆盖设备快照。

### 改动 33：启动协议推断改启发式

文件：`ble_server/ble_manager.py`

PIID 17/18 的协议码是**插线瞬间**的快照。Lightning 线空载接入时快照停在 5V，程序启动后照着快照显示就是错的（官方 app 显示 PD，我们显示 5V）。所以启动时的初始读取不走快照，改用电压/PDO 启发式推断，和官方 app 一致。运行期的推送仍然用快照，因为协议协商完成后设备会更新快照。

### 改动 35：插线协议快照失效窗口

文件：`ble_server/ble_manager.py`

改动 33 解决了启动时的快照问题，运行期还有个对称的问题：插线瞬间的推送解码用的还是插线前的旧快照（比如 5V），要等 settings 轮询重读到新快照才恢复正确，中间约 10 秒显示错误协议。

加了 `_snapshot_valid` 标志：检测到插线（空闲 → 带电压电流）就把该口的快照标记为不可信，解码改走启发式；settings 轮询重读到 PIID 17/18 后恢复信任。失效窗口的上限就是轮询周期，不产生额外 BLE 流量，代价只是失效期间做一点启发式计算。

---

## 第三部分：新增功能及其他

### 构建部署

#### 改动 3：Docker 构建换国内源

文件：`ble_server/docker/Dockerfile`

apt 换阿里云，pip 换清华。apt 的 sed 同时兼容 Debian 12 的 `sources.list.d/debian.sources` 新格式和旧的 `sources.list`，都改不动就 `|| true` 跳过。要改回官方源，删这两处就行。

#### 改动 9：装 tzdata 修时区

文件：`ble_server/docker/Dockerfile`

compose 里设了 `TZ=Asia/Shanghai`，但 python:3.11-slim 没装 tzdata，这个环境变量没人认领，日志时间一直是 UTC（差 8 小时）。装上 tzdata 并 `ln -sf` 链接 localtime 即可。

#### 改动 42：静态资源自动内容哈希版本化

文件：`ble_server/ha_server.py` + `web/index.html` + `phone.html` + `config.html`

之前前端文件每改一次都要手动把 HTML 里的 `?v=N` 递增，版本号越堆越高还容易漏改。改成启动时（`_cache_static_files`）对每个静态文件算内容 md5 前 8 位存进缓存，加载 HTML 时用正则把引用的 `/static/*.js|.css` 链接统一写成 `?v=<哈希>`，同时对替换后的 HTML 算 ETag。静态文件一改哈希就变，HTML 内容跟着变，浏览器自动拉新文件；三个 HTML 里的手动 `?v=` 全部清空。哈希只在启动算一次，运行期请求仍是纯内存命中。

注意：改动生效依赖启动时重建缓存，需重启服务。

### 事件日志

#### 改动 4：后端 BLE 事件日志

文件：`ble_server/ble_manager.py` + `ble_server/ha_server.py`

加了 `self._ble_events = deque(maxlen=200)` 环形缓冲和 `_log_ble_event()` 方法，在连接成功、认证失败、意外断连、发起重连、探活失败、refresh 全失败、keepalive 失败这些位置记录事件，同时打日志并通过 SSE 推 `ble_event` 给前端。

不做持久化：docker logs 里已经有完整记录，容器重启后这个缓冲从空开始也没关系。

`ha_server.py` 加了 `GET /api/ble-events` 端点返回事件列表。

#### 改动 5：WebUI 蓝牙日志面板

文件：`ble_server/web/index.html` + `ble_server/web/static/app.js`

页面顶部工具栏加"蓝牙日志"按钮，弹出模态框显示事件列表（显示控制用 `classList` 加减 `show`，和现有 portModal 一个路数）。列表倒序、最新在上，按事件类型着色：连接类绿色、断连认证失败类红色、各类失败黄色、重连尝试灰色。SSE 推送到达时只有面板开着才去刷新，省得白做 DOM 操作。

### 充电记录

#### 改动 15：充电记录功能

文件：`ble_server/history.py`（新）、`ha_server.py`、`ble_manager.py`、`config.py`、`web/static/charge_history.js`（新）及 index/phone/config 三个页面和对应 CSS

数据层（history.py）两张表：`charge_sessions` 会话汇总、`charge_points` 采样明细。明细点按 `point_interval_sec`（默认 30 秒）节流降采样，内存缓冲批量落盘。会话结束时回写总 Wh、峰值、平均功率、时长。每口只保留最近 5 条（`prune_sessions`），保留期清理只删已结束且超期的明细，活跃会话的数据不动。

会话生命周期在 `ble_manager.py` 的推送处理链里判定：

- 开始：默认模式 `active && current > 0.1A`；配了功率阈值则 `power > start_power_w`
- 结束：`active` 变 false 强制结束（改动 36 后有容错窗口）；阈值模式功率持续低于 `end_power_w`（持续时长见改动 24）；默认模式低电流去抖
- 能量用梯形积分累积，结束走 `_close_session` 落库并发 MQTT/SSE 事件

API 三个端点：

| 端点 | 说明 |
|------|------|
| `GET /api/sessions?port=c1&limit=20` | 某口会话列表（含活跃会话与实时数据合并） |
| `GET /api/sessions/{id}/points` | 某会话明细点（返回前经改动 17 限流） |
| `POST /api/sessions/clear` | 清空充电记录（改动 30 后保留充电中的会话） |

配置：

```yaml
charge_tracking:
  enabled_ports: [1]        # 可选 1/2
  point_interval_sec: 30
  start_power_w: 0          # >0 启用功率阈值模式
  end_power_w: 0
```

前端 charge_history.js 两个页面共用，三块视图：当前充电卡片（累计电量、实时/平均功率、协议、时长、迷你曲线）、历史列表（折叠式）、单次详情（数据在上曲线在下）。轮询只原地更新文本，会话 id 不变就不重建 DOM。

两个当初定下的规矩：

- **C3 不做记录**：硬件上 C3 和 USB-A 共用电流计，分不清负载是谁，记了也没意义
- **mAh 是折算值**：`mAh = Wh / 3.7 * 1000`，界面标注 `mAh@3.7V`，别当成实测

#### 改动 17：明细点接口限流

文件：`ble_server/ha_server.py`

背景：设备长期插着不拔（比如给固定设备供电），30 秒采样一周就是 2 万个点，`GET /api/sessions/{id}/points` 单次响应 2-4MB，前端还每 5 秒全量拉一遍，页面卡顿流量浪费。

`trim_session_points()` 做两件事：会话超过一天只返回最近一天（滚动窗口）；窗口内超过 500 点就等间隔抽稀，首尾点保留（起点功率爬升和最新状态不丢）。数据库里仍存完整数据，按保留期正常清理。单测在 `tests/test_ha_server_sessions.py` 的 `TestTrimSessionPoints`。

#### 改动 18：长时供电会话结转

文件：`ble_server/ble_manager.py`

给固定设备供电又忘关记录时，`active` 恒真、电流恒大于 0.1A，会话永远不结束，一个月后就是一条 30 天的巨无霸记录。

`session_lifetime_overdue()` 判定：会话超过 3 天，且本地时间在凌晨 3:00-5:00 窗口内，就按正常流程结转旧会话（完整统计加事件），下次数据推送自动开新会话。挂接在会话结束判定链最前面，避免被阈值模式分支吞掉。

选凌晨窗口是因为真实手机不可能连充 3 天，超长会话必然是固定设备供电，凌晨结转不打断白天使用。时区依赖改动 9 的 tzdata。结转落库时触发 `prune_sessions`，长期固定供电下每口稳定在 5 条左右。单测在 `tests/test_ble_manager_rollover.py`。

#### 改动 21：起止阈值按口独立

文件：`ble_server/config.py` + `ha_server.py` + `ble_manager.py` + `web/config.html` + `config.yaml.example`

`start_power_w` / `end_power_w` 拆成 C1/C2 各自设置。YAML 和 API 都兼容旧的标量写法（两口同值），老配置不用改。

#### 改动 24：截止时长可配置

文件：`ble_server/energy.py` + `config.py` + `ha_server.py` + `ble_manager.py` + `web/config.html`

阈值模式下功率低于截止线的持续时长原来写死 30 秒，改成配置项 `end_power_duration_sec`，保存后实时生效。顺带把前端的实时功率和平均功率彻底分成两个数：之前有互相污染的路径。

#### 改动 25：修复归零后会话卡死

文件：`ble_server/ble_manager.py`

这台的推送是事件驱动的：端口 V/I 稳定后就不再推那个口，归零时只推最后一两次。原来定时器会跳过输出已归零的端口，结果阈值截止判定一直没机会跑，会话收不了尾。而且归零后设备常停在挂载态（v>0、i=0），不只 0V/0A。

改法：定时器不再跳过归零端口，0V/0A 时照样评估截止。

#### 改动 26：截止判定统一到循环开头

文件：`ble_server/ble_manager.py` + `web/static/charge_history.js` + `index.css` / `phone.css`

改动 25 的延伸：把阈值截止评估整体挪到 `_port_timer` 循环开头统一执行，覆盖挂载态（v>0/i=0）和完全归零（0V/0A）两种情况，并且评估前不做任何电流电压门控——有门控就会有漏网的状态把会话卡死。

前端配合：截止后卡片保留本次会话数据，显示"本次充电已截止"徽标；图表容器留白，解决瓦时按钮遮挡功率行的问题。

#### 改动 30：清空保留充电中的会话

文件：`ble_server/history.py` + `web/static/charge_history.js`

`clear_sessions()` 原来删全部会话。如果清空时有口正在充电，那条活跃会话行被删掉，充电结束时 `end_session` 的 UPDATE 匹配不到行，整次统计就丢了。改成只删 `end_time` 非空的已结束会话，确认弹窗文案同步改。

#### 改动 36：拔线容错

文件：`ble_server/ble_manager.py` + `config.py` + `ha_server.py` + `web/config.html` + `config.yaml.example`

原来线缆拔出（`active` 变 false）立即结束会话，快速插拔（换口、接触不良、手机重启）会把一次充电拆成多条记录。加了个可配置的容错窗口 `unplug_grace_sec`：拔出后先挂起（`_unplug_pending` 记录拔线时刻），等待期内重新插上就取消挂起、延续同一会话（瓦时和时长都是连续的）；超时未重插才真正结束。0 为关闭，行为同旧版。

两个实现要点：

- 拔线后设备不再推送数据（事件驱动），超时结束不能指望数据路径触发，由 `_port_timer` 每秒检查挂起是否超时，插在阈值判定之后、端口归零 `continue` 之前（归零端口会跳过后半段循环）
- `_close_session` 和 `_close_active_sessions` 里统一清理挂起标记，避免重连等场景残留旧时间戳，把新会话误判为超时

阈值模式和容错并存时互不干扰：挂起期间功率为 0，阈值检测器照常计时，先到期的先结束；重插后功率回升，检测器自动重置。手动关闭端口仍是立即结束，不走容错；verify_port 兜底（15 秒无推送主动 GET）读到 0V/0A 时只清端口状态，挂起中的会话同样留给定时器按容错到期——否则超过 15 秒的容错窗口会被它截断。

#### 改动 38：会话协议标签按众数回写

文件：`ble_server/history.py`

原来 `charge_sessions.protocol` 只在 `start_session` 时写入一次，中途协议 renegotiation 或拔线容错重插换协议后，历史列表/详情仍显示开始时的协议。实时卡片不受影响（活跃会话合并时直接取 `port_state.protocol`，一直显示当前协议）。

结束时在 `end_session` 里用一条 GROUP BY 查询取该会话采样点的协议众数回写标签（走 idx_charge_points_session 索引，扫描范围只有本会话的点，结束时算一次，之后纯读取）。中途换协议时按点数占比取多数；全部点都没协议时用 COALESCE 保留开始时的值。比按时间抽点更简单也更准，3 天长会话也就几十毫秒，且运行在 executor 线程不阻塞事件循环。

### 前端显示

#### 改动 16：迷你图增强

文件：`ble_server/web/static/charge_history.js` + `index.css` / `phone.css`

- 加坐标轴：Y 轴功率刻度（带 W，最多 3 档），X 轴时间刻度（最多 4 个，跨天显示"昨天 HH:MM"/"M/D HH:MM"）
- 悬停提示：时间、功率、电压、电流，`mode: index` 吸附最近点
- 会话刚开始没有采样点时显示"采样中，曲线稍后出现"（首个点默认 30 秒后才落库，这是采集间隔决定的，不是 bug）
- 修了个建图死区：原来 `refreshActiveMiniCharts` 只刷新已建图的口，首次拉取时没点就永不建图，得重新插拔才恢复。改成活跃会话就拉取，数据一到立即建图
- 刷新间隔 10 秒缩到 5 秒；只有一个点时画圆点

#### 改动 19：显示细节

文件：`ble_server/web/static/charge_history.js` + `config.yaml.example`

- 文案补全："均功率/峰功率"这类压缩省字改成"平均功率/峰值功率"
- Wh 动态精度：小于 10 Wh 显 2 位小数，10 Wh 以上显 1 位。5W 慢充时如果只有 1 位小数，72 秒才跳一格，看着像卡死
- 时长支持天：`168h0m` 这类格式改成"7天16小时"、"2小时35分"、"5分"
- 活跃卡片的开始时间每次轮询重算，跨天后不会把"开始于 08:15"误读成今天（复用 fmtTime 的三级显示：今天 → 昨天 → M/D）

#### 改动 20：图表布局微调

文件：`ble_server/web/static/charge_history.js` + `index.css` / `phone.css`

时长并入开始时间那一行；迷你图瓦时轴移到曲线右侧；悬浮框里电压电流合并一行防止出界；图表加高（迷你图 100px，详情图桌面 196px、手机 176px）。

#### 改动 22：HTML 缓存策略修正

文件：`ble_server/ha_server.py`

原先 HTML 页面也发 `max-age=604800, immutable`，结果是浏览器连页面本身都不重新拉取，页面里 `?v=` 递增自然也失效——用户永远看到旧页面引用旧 JS。改成 HTML 发 `no-cache` 加 ETag（内容没变返回 304，代价很小），只有 JS/CSS 这类静态资源继续 immutable 加 `?v=`。

**注意**：以后改 JS/CSS 必须同步递增页面引用的 `?v=N`，否则浏览器照样用缓存旧文件，表现为"改了没生效"。

#### 改动 23：卡片显示小修

文件：`ble_server/web/static/charge_history.js` + `index.css` / `phone.css`

实时功率归零后不再错误回退成平均功率；坐标轴浮点尾数格式化；瓦时按钮上移让出右侧刻度。

#### 改动 27：时长精确到秒

文件：`ble_server/web/static/charge_history.js`

`fmtDuration` 统一加秒级显示，当前卡片、截止卡、历史列表、详情页全生效。1 小时内"X分X秒"，1 小时以上"X小时X分X秒"，1 天以上"X天X小时X分"。

#### 改动 28：时间显示到秒

文件：`ble_server/web/static/charge_history.js`

`fmtTime` 统一 `HH:MM:SS`，开始时间、历史列表、详情标题、图表时间轴和 tooltip 全生效。

#### 改动 29：显示精度开关

文件：`ble_server/web/config.html` + `web/static/charge_history.js`

配置页加"显示精度"下拉（精确到秒/精确到分），选择存 localStorage，每个浏览器设备独立记忆，切换即时生效不用保存配置。开着的页面之间用 storage 事件实时同步。本质上是对改动 27/28 的秒级显示加个总开关。

#### 改动 37：修复端口开关按钮跳动

文件：`ble_server/web/static/app.js` + `phone.js`（index/phone 页面引用版本升至 v7）

两个叠加的原因：

- 桌面版 `renderPorts` 每次轮询/SSE 都 `innerHTML` 全量重建端口卡片，打断开关滑块的 CSS transition，还丢失请求期间的 disabled 状态。改为首次建卡、后续原地更新文本与类，跟 charge_history.js 同一套路
- 两版都靠"点击后 3 秒内不覆盖"保护开关状态，但 `/api/port` 要等 BLE SET 完成才响应，串行命令队列下经常超过 3 秒。窗口一过，下一次轮询就用服务器旧值把开关打回去，SET 完成后又跳回来——"有时跳"正是设备响应慢的时候。改为按口挂 pending 标志：请求期间该口状态不被覆盖，桌面版重建后也保持 disabled，结束后清除

#### 改动 39：最高电压计入挂载态端口

文件：`ble_server/web/static/app.js`（index 页引用版本升至 v8）+ `web/static/device_info.html`（内嵌脚本）

顶部统计的三个数共用"电流>0 或 功率>0"的活动判定，空载挂载的口（v>0/i=0）被整体排除，插着 PD 线空载时最高电压显示 0.0。拆开条件：总功率和活动端口数维持原判定（确实没在输出），最高电压改为只看端口电压是否大于 0。手机版没有这个统计，不用动。

#### 改动 40：采样精度行排版对齐

文件：`ble_server/web/config.html`

配置页"采样精度"行原来跟其它行不一致：label、选择框、说明三样堆在一行，说明还散在下面。改成和其它行统一：左侧大字"采样精度"+小字说明上下叠放，选择框在右侧。纯排版，逻辑没动。

#### 改动 41：修复开关请求返回后旧推送仍闪回

文件：`ble_server/web/static/app.js`

改动 37 的 pending 在 `/api/port` 一返回就删掉，之后靠"3 秒最近改动"窗口挡旧值。设备响应慢时，请求返回后服务器旧推送（enabled 还是改动前的值）会把开关先闪回旧状态，等真值推送再拉回目标，每次复测都闪一下。改成 pending 保留到服务器确认新状态为止：`renderPorts` / `updatePortDOM` 发现服务器 enabled 已等于目标就解除 pending 并采用，等于前不覆盖；只有请求失败才回滚清除。手机版 `applyPortUpdate` 本来就有同样的 pending 保护，不用动。

---

## 验证清单

| 验证项 | 方法 |
|--------|------|
| 编译 | `python -m py_compile ble_manager.py ha_server.py` |
| 单测 | `python -m pytest tests/test_ha_server_sessions.py tests/test_ble_manager_rollover.py -q` |
| 构建 | `docker compose -f docker/docker-compose.yml build`，确认 apt/pip 走镜像源无超时 |
| 时区 | `docker exec cuktech-ble date` 显示 CST |
| 服务发现 | 日志出现 `GATT services discovered`，不出现 `Service Discovery has not been performed yet` |
| 不误重连 | 正常连接跑 30 分钟以上，`/api/ble-events` 无新增断连 |
| 真断连恢复 | 断充电器电源 → 30 秒内自动重连，日志有完整事件链 |
| 不殃及其他设备 | 重连触发时同蓝牙棒的其他设备保持连接 |
| 蓝牙日志面板 | 按钮开面板能看到事件；触发断连重连时面板实时新增 |
| 充电记录 | 插拔设备充电 → 卡片出现并实时刷新 → 结束后进历史列表 |
| 明细限流 | 长时会话 `curl /api/sessions/{id}/points`，点数 ≤500、响应几十 KB |
| 会话结转 | （模拟）会话超 3 天且凌晨 3-5 点 → 日志出现 `Session rollover` |
| 清空逻辑 | 充电中点清空 → 该会话保留，结束后统计完整 |
| 前端缓存 | 部署后强制刷新，确认页面引用的 `?v=` 已递增 |

## 回滚说明

整体回滚优先走 git 历史，比手工逆向改可靠。手工回滚时按改动编号找对应小节，多数改动是局部独立的。几个要点：

- 第一部分的改动 1、6 和第二部分的 2、8、10-14 是连接稳定性的地基，回滚任何一个都要先想清楚会不会回到"频繁误重连"或"断了不重连"的老问题
- 改动 17 回滚后要接受长时会话的接口膨胀（响应几 MB）
- 改动 22 回滚后页面更新不再即时生效
- 改动 25/26/30/34/37 是纯 bug 修复，没有回滚的理由
- 改动 3/5/9 这类体验性的可以随意回滚
