# ESP32 BLE 固件发布说明

## v1.1.0

### 新增
- **前端嵌入**: 完全重构前端页面，全部前端资源（HTML/CSS/JS/图片）嵌入固件，无需外部服务器或 SPIFFS
- **OTA**: 移除OTA功能，app 分区扩至 3.875MB，4% 空闲 → 26% 空闲
- **BLE 延时连接**: 启动后 60s 延迟连接 BLE，前端可手动提前触发
- **配置脱敏**: 敏感字段（WiFi 密码、设备密钥、MQTT 密码等）API 返回时自动脱敏，保存时跳过 `****`

### 优化
- **协议检测**: 硬件协议码（PIID 17/18）优先，PDO kind + PPS 开关完整检查链，阈值对齐 `0.05V`
- **WiFi/BLE 共存**: 大文件传输时动态切换 `ESP_COEX_PREFER_WIFI`，发送完恢复 BALANCE
- **内存优化**: NimBLE 缓冲区池裁剪（约节省 17KB），TCP 发送缓冲 8192，MQTT 任务栈 4096
- **大文件分块传输**: 4096 字节分块 + 指数退避重试（最大 8 次，1.27s），抑制 EAGAIN 断连
- **HTTP 超时**: `send_wait_timeout` 5s → 10s，撑过 BLE 繁忙期
- **端口去抖动**: 500ms（原 2000ms），减少断开检测延迟
- **通知队列**: `NOTIF_QUEUE_LEN` 8 → 16，降低推送溢出风险
- **result_queue**: 32 → 48，xQueueSend 增加 50ms 超时 + 丢弃日志
- **NimBLE 内存参数回调**: `ACL_BUF_SIZE` 128→255，`HCI_EVT_BUF_SIZE` 128→256 等，确保服务发现正常
- **HTML gzip**: phone.html 80% 压缩，config.html 76% 压缩

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS

## v1.0.3

### 新增
- **倒计时设置**: Web 仪表盘新增倒计时功能，30/60 分钟预设及自定义倒计时（范围 1-1440 分钟），到期自动关断端口
- **MQTT 断线保护**: 断线时停止 MQTT publish 入队，防止 outbox 溢出导致内存耗尽和 WiFi 崩溃

### 优化
- **Bemfa 熔断机制**: 连续断线 5 次后暂停重连 5 分钟，避免无效重连消耗
- **Bemfa 保活对齐**: ping QoS 0 + `==` 判断 + 递归调度，对齐官方 HA 集成
- **BLE 扫描**: 扫描前 cancel 冲突会话，提升连接成功率
- **Web 仪表盘**: 倒计时输入框不因自动刷新丢失焦点；清除按钮红色高亮

### 修复
- MQTT 断线时 publish 数据堆积导致 `outbox: Memory exhausted` 和 WiFi 崩溃
- HTTP 页面不显示数据（缺少 `setInterval` 调用和 `CDPI` 变量作用域错误）
- Bemfa 长时间运行显示设备离线

## v1.0.2

### 变更
- **保活机制优化**：从 60s 定时发布改为 ping/pong（hassping topic）
  - 每 30s 发送 ping，20s 超时检测
  - 连续 3 次 ping 丢失自动重连
  - 连接后发布初始状态到巴法云
- **启动宽限期**：从 5s 增加到 10s，每次重连/断开重新激活，防止回声导致 BLE 被误禁用
- **DNS 预解析**：HTTP 注册前先解析 `api.bemfa.com`，失败则等待重试，避免 HTTP 0 错误
- **状态缓存保护**：`portMUX` 保护 `_port_state`/`_ble_state` 读写，避免多任务竞态
- **命令失败不更新缓存**：BLE 断连时命令失败，不会错误更新状态缓存

### 修复
- 修复启动时巴法云回声命令导致 BLE 被误 disable
- 修复 HTTP 注册 DNS 解析失败（HTTP 0）
- 修复保活发布总是 off（改为缓存实际状态）

## v1.0.1

### 新增
- **巴法云接入**：支持小爱同学 / 小度语音控制充电器端口开关，无需安装 HA 集成
  - 5 个设备：C口1开关、C口2开关、C口3开关、USB-A开关、蓝牙开关
  - Topic 自动注册（`hass` + MD5 + `006`），设备名自动设置
  - 60 秒保活机制，发布实际端口状态
  - 启动 5 秒宽限期，过滤巴法云回声命令
  
### 优化
- HTTP 注册添加 5 秒超时
- 注册失败改为 WARN 日志并注明 MQTT 仍可用
- UID 日志脱敏（仅显示前 4 位）

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS

## v1.0.0

### 功能
- BLE 连接酷态科充电器（MiOT 协议）
- 加密通信（AES-CCM + HKDF + HMAC-SHA256）
- MQTT 数据发布（QoS 1, retain）
- Web 配置页面：首次启动 AP 配网，浏览器配置凭据
- Web 仪表盘：实时端口电压 / 电流 / 功率
- 端口开关控制
- 协议开关（PD / PPS / UFCS / SCP）
- 场景模式切换
- BLE 连接开关
- HTTP OTA 更新
- 自动重连

### 硬件支持
- ESP32 / ESP32-S3 / ESP32-C3

### 构建
- ESP-IDF v5.3.5
- NimBLE Central
- ESP-MQTT + cJSON + mbedTLS
