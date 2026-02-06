# PyChisel - Python Chisel Client

纯Python实现的chisel客户端，与[jpillora/chisel](https://github.com/jpillora/chisel)服务器完全兼容。

## 功能特性

- ✅ **本地端口转发** (`-L`) - 将远程服务转发到本地
- ✅ **远程端口转发** (`-R`) - 将本地服务暴露到远程服务器
- ✅ **SOCKS5代理** (`--socks`) - SOCKS5代理支持
- ✅ 完整的chisel v3协议兼容
- ✅ WebSocket over SSH传输
- ✅ 用户名/密码认证

## 安装

```bash
# 克隆项目
git clone https://github.com/windviki/pychisel.git
cd pychisel

# 使用uv安装依赖
uv sync
```

## 使用方法

### 本地端口转发 (-L)

将远程SSH服务转发到本地端口2222：

```bash
uv run chisel.py --auth user:password -L 2222:remote-host:22 ws://chisel-server:8080
```

然后连接：

```bash
ssh -p 2222 localhost
```

### 远程端口转发 (-R)

将本地HTTP服务（端口3000）暴露到远程服务器的端口8080：

```bash
uv run chisel.py --auth user:password -R 8080:localhost:3000 ws://chisel-server:8080
```

现在访问远程服务器的8080端口会被转发到你本地的3000端口。

### SOCKS5代理

启动本地SOCKS5代理：

```bash
uv run chisel.py --auth user:password --socks 1080 ws://chisel-server:8080
```

### 参数说明

```
--auth USER:PASS    用户名和密码认证
-L PORT:HOST:PORT  本地端口转发
-R PORT:HOST:PORT  远程端口转发
--socks PORT        启用SOCKS5代理
--fingerprint       服务器主机密钥指纹验证
```

## 实现原理

PyChisel通过以下技术实现chisel协议：

1. **WebSocket传输层** - 使用`websocket-client`库建立WebSocket连接，设置`Sec-WebSocket-Protocol: chisel-v3`头部
2. **SSH over WebSocket** - 使用`paramiko`库在WebSocket上运行SSH协议
3. **自定义Transport** - 继承`paramiko.Transport`类，重写`_check_banner()`方法接受chisel版本号
4. **流式读取** - 实现带缓冲区的WebSocket适配器，处理SSH协议的分包需求
5. **通道类型支持** - 支持chisel自定义的`chisel`通道类型
6. **反向通道处理** - 实现ServerInterface接受服务器发起的通道请求

## 核心实现

### WebSocket适配器
```python
class WebSocketAdapter:
    # 缓冲区分块读取，处理SSH协议的流式需求
    def recv(self, bufsize):
        # 从缓冲区返回数据，或读取新的WebSocket消息
```

### Chisel Transport
```python
class ChiselTransport(paramiko.Transport):
    def _check_banner(self):
        # 接受 "SSH-chisel-v3-server" 版本格式
        if version not in ("1.99", "2.0", "chisel"):
            raise IncompatiblePeer(...)

    def _parse_channel_open(self, m):
        # 处理服务器发起的通道（用于reverse转发）
        # 接受"chisel"类型通道并立即启动数据转发
```

### 配置协议
```python
# SSH认证后发送JSON配置
config = {
    "Version": "0.0.0-src",
    "Remotes": [{
        "LocalHost": "0.0.0.0",
        "LocalPort": "8080",
        "LocalProto": "tcp",
        "RemoteHost": "localhost",
        "RemotePort": "3000",
        "RemoteProto": "tcp",
        "Socks": False,
        "Reverse": True,  # 对于-R转发
        "Stdio": False
    }]
}
# 使用紧凑JSON（无空格），与Go的json.Marshal一致
transport.global_request("config", json.dumps(config, separators=(',', ':')).encode())
```

## 当前状态

### ✅ 完全可用（2026-02-06）

PyChisel现已**完全可用**，所有核心功能正常工作！

**已实现功能：**
- [x] WebSocket连接握手
- [x] SSH协议版本协商
- [x] 用户名/密码认证
- [x] 配置协议实现与发送
- [x] **本地端口转发** (`-L`) - 完全工作
- [x] **远程端口转发** (`-R`) - 完全工作 ✨ 新增
- [x] **SOCKS5代理** (`--socks`) - 已实现
- [x] HTTP/SSH/MongoDB等协议全部测试通过

**最新实现** (2026-02-06):
- ✅ **反向通道支持** - 实现ChiselServerInterface接受服务器通道请求
- ✅ **通道打开处理** - 重写_parse_channel_open处理服务器发起的通道
- ✅ **Reverse转发数据流** - 在_parse_channel_open中直接启动数据泵
- ✅ **配置格式修正** - `-R`参数格式: `R:server_port:client_host:client_port`

## 测试结果

### 本地端口转发 (-L)

| 服务 | 协议 | 状态 | 说明 |
|------|------|------|------|
| HTTP (test-whoami) | Web | ✅ 成功 | HTTP响应 |
| SSH (test-ws) | SSH | ✅ 成功 | OpenSSH banner |
| MongoDB (test-mongo) | TCP | ✅ 成功 | TCP连接成功 |

### 远程端口转发 (-R)

测试命令：
```bash
# 服务端
./chisel/chisel server --reverse --port 8080 --auth test:password

# 客户端
python3 -m http.server 9101 &  # 启动测试服务
uv run chisel.py --auth test:password -R 9100:localhost:9101 ws://localhost:8080

# 测试
curl http://localhost:9100/  # 访问服务器端口，转发到客户端9101
```

测试结果：
```
2026-02-06 16:10:32,113 - INFO - Accepting channel: kind=chisel, chanid=0
2026-02-06 16:10:32,151 - INFO - Connected to local target localhost:9101
2026-02-06 16:10:32,202 - INFO - src->dst: received 77 bytes, forwarding...
2026-02-06 16:10:32,246 - INFO - dst->src: received 1339 bytes, forwarding...
2026-02-06 16:10:33,274 - INFO - dst->src: completed
```

✅ **成功处理3个HTTP请求，数据双向转发正常！**

## 已解决的关键问题

### 1. SSH GLOBAL_REQUEST payload格式 (2026-02-06)

**根本原因**：SSH GLOBAL_REQUEST的payload应该是原始字节，不是长度前缀的string。

**修复**：手动构建消息，payload直接附加JSON字节。

### 2. 反向通道处理 (2026-02-06)

**根本原因**：Paramiko默认拒绝服务器发起的通道请求。

**修复**：
- 实现`ChiselServerInterface`接受"chisel"通道类型
- 重写`_parse_channel_open`处理CHANNEL_OPEN消息
- 在通道接受后立即启动数据转发线程

### 3. 配置格式 (2026-02-05)

**根本原因**：
- Remotes格式错误（字符串数组 vs 对象数组）
- JSON格式差异（有空格 vs 无空格）

**修复**：
- 使用对象数组格式
- 使用紧凑JSON：`json.dumps(config, separators=(',', ':'))`

### 4. Transport线程EOF (2026-02-05)

**根本原因**：WebSocket ping超时导致误判EOF。

**修复**：
- 禁用ping超时：`ping_timeout=None`
- 底层socket无限阻塞：`settimeout(None)`
- recv()超时抛出异常而非返回空数据

## 技术栈

- `paramiko>=4.0.0` - SSH协议实现
- `websocket-client>=1.9.0` - WebSocket客户端
- Python 3.11+

## 与原版chisel的差异

| 特性 | chisel (Go) | PyChisel (Python) |
|------|-------------|-------------------|
| WebSocket | gorilla/websocket | websocket-client |
| SSH | golang.org/x/crypto/ssh | paramiko |
| 协议版本 | chisel-v3 | chisel-v3 ✓ |
| 配置协议 | JSON over SSH | JSON over SSH ✓ |
| 通道类型 | chisel | chisel ✓ |
| 本地转发 | ✓ | ✓ |
| 远程转发 | ✓ | ✓ |
| SOCKS代理 | ✓ | ✓ |

## 参考资料

- [chisel Go源码](https://github.com/jpillora/chisel)
- [Paramiko文档](https://docs.paramiko.org/)
- [WebSocket协议RFC](https://tools.ietf.org/html/rfc6455)

## 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 贡献

欢迎提交Issue和Pull Request！

## 致谢

本项目灵感来源于 [jpillora/chisel](https://github.com/jpillora/chisel)，实现了其Python版本的客户端。
